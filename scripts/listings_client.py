#!/usr/bin/env python3
"""
신규상장 라이브 수집 v2 — 38.co.kr 데스크탑 아카이브 기반 (XLSX 비의존).

교차 실측(2026-06)으로 확정한 소스:
  - 38 데스크탑 o=nw (신규상장 결과): 상장 후에도 목록에서 빠지지 않는 '아카이브' →
    상장일·확정공모가·첫날 시초/종가 수익률. (이전 fund.php '진행중' 파이프라인은 상장 후
    롤오프되어 최근 상장분/스팩을 통째로 누락했음 → 이 모듈로 교체.)
  - 38 데스크탑 o=r1 (수요예측결과): 기관경쟁률 + 의무보유확약 비율(lockupRate) + 주간사 + 밴드.
  - fdr.StockListing: 종목명→종목코드/시장 보조 (선택, SPAC 미매칭 시 KOSDAQ 기본).

www.38.co.kr 은 레거시 TLS(cipher) 라 Python 기본 ssl 이 SSLV3_ALERT_HANDSHAKE_FAILURE →
SECLEVEL=1 컨텍스트로 우회(검증 우선, 실패 시 CERT_NONE fallback).

주의: 의무보유확약은 총 확약 비율만 제공(38 목록 단). 구간별(15/30/90/180일) 비율은 종목 상세
(DART/38 company.htm)에 있으나 본 모듈 미수집 — 프런트는 구간별 '해제일'을 상장일로 산출.
"""
from __future__ import annotations
import json
import logging
import os
import re
import ssl
import sys
import threading
import time
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(Path(__file__).parent))
try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except Exception:
    pass

logger = logging.getLogger("listings_client")
if not logger.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("[listings] %(levelname)s %(message)s"))
    logger.addHandler(h)
logger.setLevel(logging.INFO)

_38_BASE = "https://www.38.co.kr/html/fund/index.htm"
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
MAX_LIVE_LISTINGS = 60

_FDR_TTL = 1800.0
_fdr_cache: dict[str, Any] = {"ts": 0.0, "df": None}
_fdr_lock = threading.Lock()


def _ssl_ctx(verify: bool) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        ctx.set_ciphers("DEFAULT@SECLEVEL=1")  # 38 레거시 cipher 허용
    except ssl.SSLError:
        pass
    return ctx


def _fetch_38(o: str, timeout: int = 20) -> str:
    """38 데스크탑 목록 페이지(EUC-KR) → str. 검증 우선, 실패 시 CERT_NONE fallback."""
    url = f"{_38_BASE}?o={o}"
    req = urllib.request.Request(url, headers=_HEADERS)
    for verify in (True, False):
        try:
            with urllib.request.urlopen(req, context=_ssl_ctx(verify), timeout=timeout) as r:
                return r.read().decode("euc-kr", errors="replace")
        except (ssl.SSLError, urllib.error.URLError) as e:
            if verify:
                logger.warning("38 o=%s TLS 검증 실패 → CERT_NONE 재시도: %s", o, repr(e)[:80])
                continue
            logger.error("38 o=%s fetch 실패: %s", o, repr(e)[:120])
        except Exception as e:
            logger.error("38 o=%s 오류: %s", o, repr(e)[:120])
            break
    return ""


def _cells(tr: str) -> list[str]:
    return [re.sub(r"<[^>]+>", "", td).replace("&nbsp;", " ").strip()
            for td in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.DOTALL | re.IGNORECASE)]


def _num(s: str) -> Optional[float]:
    if not s:
        return None
    m = re.search(r"-?[\d,]+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _norm(s: str) -> str:
    s = re.sub(r"\(\s*구[.\s].*?\)", "", s or "")
    return re.sub(r"\s+", "", s).strip()


def _parse_date(s: str) -> Optional[str]:
    m = re.search(r"(20\d{2})[/.\-](\d{1,2})[/.\-](\d{1,2})", s or "")
    return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else None


def _parse_nw(html: str) -> dict[str, dict]:
    """o=nw → {norm_name: {name, listingDate, offeringPrice, firstDayReturn, firstDayClose, listed}}.
    상장 완료 행(상장일 <= 오늘)만 컬럼 정렬이 안정적: [명,상장일,현재가,등락,공모가,공모대비,시초가,시초%,종가]."""
    today = date.today().isoformat()
    out: dict[str, dict] = {}
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.DOTALL | re.IGNORECASE):
        tds = _cells(tr)
        if len(tds) < 9:
            continue
        name = tds[0]
        ld = _parse_date(tds[1])
        if not name or not ld:
            continue
        if ld > today:  # 상장 예정 → 컬럼 정렬 다름 + 미상장 → skip (오늘 보드가 담당)
            continue
        key = _norm(name)
        if not key or key in out:
            continue
        out[key] = {
            "name": re.sub(r"\(\s*구[.\s].*?\)", "", name).strip(),
            "listingDate": ld,
            "offeringPrice": _num(tds[4]),
            "firstDayReturn": _num(tds[7]),   # 시초가 수익률(공모가 대비)
            "firstDayClose": _num(tds[8]),    # 첫날 종가
        }
    return out


def _parse_r1(html: str) -> dict[str, dict]:
    """o=r1 (수요예측결과) → {norm_name: {bandLow,bandHigh,offeringPrice,competitionInst,lockupRate,underwriter}}.
    컬럼: [명, 수요예측일, 밴드(n~n), 확정공모가, 공모주식수, 경쟁률(n:1), 의무보유확약(n%), 주간사]."""
    out: dict[str, dict] = {}
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.DOTALL | re.IGNORECASE):
        tds = _cells(tr)
        if len(tds) < 8:
            continue
        name = tds[0]
        key = _norm(name)
        band = tds[2]
        if not key or "~" not in band:  # 밴드 셀로 데이터행 식별 (헤더/광고행 제외)
            continue
        bm = re.search(r"([\d,]+)\s*~\s*([\d,]+)", band)
        comp = next((c for c in tds if ":1" in c or "：1" in c), "")
        lock = next((c for c in tds[5:] if re.fullmatch(r"\d+(?:\.\d+)?\s*%", c.strip())), "")
        uw = next((c for c in tds if ("증권" in c or "투자" in c or "캐피탈" in c)), None)
        out[key] = {
            "bandLow": _num(bm.group(1)) if bm else None,
            "bandHigh": _num(bm.group(2)) if bm else None,
            "offeringPrice": _num(tds[3]),
            "competitionInst": _num(comp),
            "lockupRate": _num(lock),
            "underwriter": uw,
        }
    return out


def _load_fdr():
    now = time.monotonic()
    if _fdr_cache["df"] is not None and (now - _fdr_cache["ts"]) < _FDR_TTL:
        return _fdr_cache["df"]
    with _fdr_lock:
        if _fdr_cache["df"] is not None and (time.monotonic() - _fdr_cache["ts"]) < _FDR_TTL:
            return _fdr_cache["df"]
        import FinanceDataReader as fdr
        df = fdr.StockListing("KRX")
        _fdr_cache["df"] = df
        _fdr_cache["ts"] = time.monotonic()
        return df


def _fdr_lookup(df, name: str) -> tuple[Optional[str], Optional[str]]:
    """종목명 → (종목코드, 시장). 미매칭 시 (None, None)."""
    if df is None:
        return None, None
    target = _norm(name)
    try:
        normed = df["Name"].astype(str).map(_norm)
        hit = df[normed == target]
        if not len(hit):
            for i, n2 in normed.items():
                if n2 and len(n2) >= 3 and (n2 in target or target in n2):
                    hit = df.loc[[i]]
                    break
        if len(hit):
            row = hit.iloc[0]
            m = str(row.get("Market") or "").upper()
            mk = "KOSPI" if "KOSPI" in m else ("KONEX" if "KONEX" in m else "KOSDAQ")
            return str(row["Code"]).strip(), mk
    except Exception as e:
        logger.warning("fdr lookup(%s) 오류: %s", name, repr(e)[:80])
    return None, None


def _merge_recent_perf(rows: list[dict]) -> None:
    """data/recent-perf.json(토스 일봉 산출, fetch_prices.py 생성)을 ticker 매칭으로 병합.
    상장 후 성과(고가·기간수익률) — 라이브 목록엔 없는 값. 파일 없으면 조용히 skip(정상 강등)."""
    path = PROJECT_ROOT / "data" / "recent-perf.json"
    try:
        by_ticker = json.loads(path.read_text(encoding="utf-8")).get("by_ticker", {})
    except FileNotFoundError:
        return
    except Exception as e:
        logger.warning("recent-perf.json 로드 실패(무시): %s", e)
        return
    keys = ("highReturn", "daysToHigh", "return1M", "return3M", "return6M")
    merged = 0
    for r in rows:
        p = by_ticker.get((r.get("ticker") or "").strip())
        if not p:
            continue
        for k in keys:
            if p.get(k) is not None and r.get(k) is None:
                r[k] = p[k]
        merged += 1
    if merged:
        logger.info("recent-perf 병합 %d건", merged)


def get_recent_listings(since: str = "2026-04-30", max_items: int = MAX_LIVE_LISTINGS) -> list[dict]:
    """38 o=nw(상장 아카이브) ∪ o=r1(수요예측결과) 병합. since 이후 상장분 전체."""
    nw = _parse_nw(_fetch_38("nw"))
    if not nw:
        logger.error("38 신규상장(o=nw) 수집 0건 — 빈 결과")
        return []
    r1 = _parse_r1(_fetch_38("r1"))
    try:
        df = _load_fdr()
    except Exception as e:
        logger.warning("fdr 로드 실패 (티커/시장 생략): %s", e)
        df = None

    out: list[dict] = []
    for key, n in nw.items():
        ld = n["listingDate"]
        if ld <= since:
            continue
        fund = r1.get(key, {})
        name = n["name"]
        offering = n.get("offeringPrice") or fund.get("offeringPrice")
        ticker, market = _fdr_lookup(df, name)
        is_spac = "스팩" in name
        close = n.get("firstDayClose")
        close_ret = round((close / offering - 1) * 100, 2) if (offering and close) else None
        out.append({
            "name": name,
            "ticker": ticker,
            "listingDate": ld,
            "market": market or "KOSDAQ",
            "sector": "스팩" if is_spac else "기타",
            "sectorRaw": "스팩" if is_spac else "기타",
            "offeringPrice": offering,
            "firstDayReturn": n.get("firstDayReturn"),
            "firstDayCloseReturn": close_ret,
            "highReturn": None,           # o=nw 에 고가 미포함 (구간별 락업과 무관)
            "daysToHigh": None,
            "return1M": None,
            "return3M": None,
            "return6M": None,
            "competitionRetail": None,
            "competitionInst": fund.get("competitionInst"),
            "offeringAmount": None,
            "lockupRate": fund.get("lockupRate"),     # 의무보유확약 총 비율 (o=r1)
            "upperLimitHit": False,
            "underwriter": fund.get("underwriter"),
            "source": "live",
        })
        if len(out) >= max_items:
            break

    _merge_recent_perf(out)
    out.sort(key=lambda r: r["listingDate"], reverse=True)
    logger.info("신규상장 수집 %d건 (since %s, o=nw %d / o=r1 %d)", len(out), since, len(nw), len(r1))
    return out


if __name__ == "__main__":
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    import json
    rows = get_recent_listings()
    print(f"\n=== 신규상장(라이브 v2) {len(rows)}건 ===")
    for r in rows:
        print(f"  {r['listingDate']} {r['name'][:20]:20} 공모{r['offeringPrice']} "
              f"첫날{r['firstDayReturn']}% 확약{r['lockupRate']}% 경쟁{r['competitionInst']} {r['market']} {r['ticker']}")
