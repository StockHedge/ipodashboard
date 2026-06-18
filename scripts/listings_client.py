#!/usr/bin/env python3
"""
신규상장 라이브 수집 — XLSX 비의존, 다중 소스 조합.

단일 무료 소스로는 "4/30 이후 신규상장 + 우리 분석 스키마"를 완전히 못 얻으므로 조합한다
(교차 실측 2026-06 기준):
  - 38(m.38.co.kr) fund.php : 공모주 정체성 + 확정/희망 공모가 + 주간사 (ETN/ETF 노이즈 없음).
                              리스트행에 확정공모가가 있어 상세 fetch 불필요(속도↑). 단 상장일은 None.
  - fdr.StockListing("KRX") : 종목명→종목코드/시장/현재가. 이 매칭이 곧 "이미 상장됨" 확인.
  - KIS get_daily           : 첫 거래일 = 실제 상장일, 시초/종가/고가로 첫날·고가 수익률 산출.

미상장(청약 예정)은 fdr 에 없으므로 자동 skip → /api/ipo/schedule(오늘 보드)이 담당.
KIS 미가용 시 상장일 확정 불가 → 해당 건 skip(부분 강등). baseline 은 항상 표시되므로 안전.

serve.py 가 30분 캐시로 호출하고, 프런트가 baseline(ipo-recent.js)에 병합한다.
"""
from __future__ import annotations
import logging
import os
import re
import sys
import threading
import time
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

_FDR_TTL = 1800.0  # fdr 종목 마스터 캐시 30분 (재다운로드 비용 절감)
_fdr_cache: dict[str, Any] = {"ts": 0.0, "df": None}
_fdr_lock = threading.Lock()  # ThreadingMixIn + warmup 동시 호출 경합 방지
MAX_LIVE_LISTINGS = 40


def _f(v: Any) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        return float(str(v).replace(",", ""))
    except (ValueError, TypeError):
        return None


def _norm(s: str) -> str:
    """종목명 정규화 — '(구.대영채비)' 등 부기 제거 + 공백 제거."""
    s = re.sub(r"\(\s*구[.\s].*?\)", "", s or "")
    return re.sub(r"\s+", "", s).strip()


def _load_fdr():
    now = time.monotonic()
    cached = _fdr_cache["df"]
    if cached is not None and (now - _fdr_cache["ts"]) < _FDR_TTL:
        return cached
    with _fdr_lock:  # double-checked locking — 동시 만료 시 중복 다운로드 방지
        now = time.monotonic()
        if _fdr_cache["df"] is not None and (now - _fdr_cache["ts"]) < _FDR_TTL:
            return _fdr_cache["df"]
        import FinanceDataReader as fdr
        df = fdr.StockListing("KRX")
        _fdr_cache["df"] = df
        _fdr_cache["ts"] = now
        logger.info("fdr 종목 마스터 로드 %d건", len(df))
        return df


def _match_fdr(df, name: str):
    """종목명으로 fdr 행 매칭 (정확 → 부분 양방향). 매칭되면 '이미 상장됨'."""
    target = _norm(name)
    if not target or df is None:
        return None
    names = df["Name"].astype(str)
    normed = names.map(_norm)
    exact = df[normed == target]
    if len(exact):
        return exact.iloc[0]
    # 부분 일치는 짧은 쪽 3자 이상 + 길이비 0.6 이상일 때만 ("SK" 등 단편 오매칭 방지)
    for i, n2 in normed.items():
        if not n2:
            continue
        short, long = (n2, target) if len(n2) <= len(target) else (target, n2)
        if len(short) >= 3 and short in long and len(short) / len(long) >= 0.6:
            return df.loc[i]
    return None


def _norm_market(raw: Any) -> str:
    m = str(raw or "").upper()
    if "KOSPI" in m or m == "STK":
        return "KOSPI"
    if "KOSDAQ" in m or m == "KSQ":
        return "KOSDAQ"
    if "KONEX" in m or m == "KNX":
        return "KONEX"
    return "KOSDAQ"


def get_recent_listings(since: str = "2026-04-30", max_items: int = MAX_LIVE_LISTINGS) -> list[dict]:
    """38 파이프라인 ∩ fdr(상장확인) → KIS 첫날 수익률. since 이후 상장분만 반환."""
    import kind_client
    try:
        # fetch_detail=False: 리스트행에 확정공모가/주간사 존재 → 상세 fetch 생략(속도)
        items = kind_client.get_ipo_schedule(fetch_detail=False)
    except Exception as e:
        logger.error("kind 일정 수집 실패: %s", e)
        return []

    try:
        df = _load_fdr()
    except Exception as e:
        logger.error("fdr 로드 실패 (신규상장 비활성): %s", e)
        return []

    kis = None
    try:
        if os.environ.get("KIS_APP_KEY") and os.environ.get("KIS_APP_SECRET"):
            from kis_client import KisClient
            kis = KisClient()
    except Exception as e:
        logger.warning("KIS 초기화 실패 — 첫날 수익률/상장일 확정 불가: %s", e)

    out: list[dict] = []
    seen: set[str] = set()
    for it in items:
        if len(out) >= max_items:
            break
        name = (it.get("name") or "").strip()
        key = _norm(name)
        if not key or key in seen:
            continue
        row = _match_fdr(df, name)
        if row is None:
            continue  # fdr 에 없음 = 미상장(예정) → skip

        code = str(row["Code"]).strip()
        market = _norm_market(row.get("Market"))
        offering = it.get("price")  # 확정공모가만 — 희망밴드 폴백 제거(수익률 기준 일관성)

        listing_date = None
        first_open = first_close = high = None
        days_to_high = None
        if kis and code:
            try:
                daily = kis.get_daily(code, days=130)
                time.sleep(0.4 if getattr(kis, "use_paper", True) else 0.05)
            except Exception as e:
                logger.warning("KIS get_daily(%s) 실패: %s", code, e)
                daily = []
            if daily:
                listing_date = daily[0]["date"]
                first_open = daily[0].get("open")
                first_close = daily[0].get("close")
                # 고가 + 도달일수 — argmax (list.index 는 중복 고가 시 항상 0 반환하는 버그)
                hi_idx = max(range(len(daily)), key=lambda i: daily[i].get("high") or 0)
                if daily[hi_idx].get("high") is not None:
                    high = daily[hi_idx].get("high")
                    days_to_high = hi_idx

        # 상장일 미확정(KIS 무) 또는 baseline 범위(since 이전) → 제외
        if not listing_date or listing_date <= since:
            continue
        seen.add(key)

        def _ret(p):
            return round((p / offering - 1) * 100, 2) if (offering and p) else None

        is_spac = "스팩" in name
        out.append({
            "name": re.sub(r"\(\s*구[.\s].*?\)", "", name).strip(),
            "ticker": code,
            "listingDate": listing_date,
            "market": market,
            "sector": "스팩" if is_spac else "기타",
            "sectorRaw": "스팩" if is_spac else "기타",
            "offeringPrice": offering,
            "firstDayReturn": _ret(first_open),
            "firstDayCloseReturn": _ret(first_close),
            "highReturn": _ret(high),
            "daysToHigh": days_to_high,
            "return1M": None,
            "return3M": None,
            "return6M": None,
            "competitionRetail": None,
            "competitionInst": None,
            "offeringAmount": None,
            "lockupRate": None,
            "upperLimitHit": False,  # 신규상장 ±400% 룰 — 신뢰 산출 불가, 보수적 False
            "underwriter": it.get("underwriter"),
            "source": "live",
        })

    out.sort(key=lambda r: r["listingDate"], reverse=True)
    logger.info("신규상장 수집 %d건 (since %s, KIS=%s)", len(out), since, bool(kis))
    return out


if __name__ == "__main__":
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    import json
    rows = get_recent_listings()
    print(f"\n=== 신규상장(라이브) {len(rows)}건 ===")
    print(json.dumps(rows, ensure_ascii=False, indent=2)[:4000])
