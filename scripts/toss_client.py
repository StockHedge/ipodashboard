#!/usr/bin/env python3
"""토스증권 Open API 클라이언트 — 시세(candles/prices) 조회 전용.

인증: OAuth 2.0 client credentials (TOSS_APP_KEY/TOSS_APP_SECRET, .env).
토큰은 data/.toss_token.json 에 캐시(발급 응답 expires_in 기준, 실측 약 24h).

주의: 토스 Open API 는 IP allowlist 방식 — 토스 개발자센터에 등록한 공인 IP 에서만 호출 가능.
미등록 IP 는 403 access_denied("IP address not allowed"). Render 무료(비고정·해외 IP)에서는
직접 호출 불가하므로, 본 클라이언트는 '로컬 배치 수집'(fetch_prices.py) 용도다.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except Exception:
    pass

logger = logging.getLogger("toss_client")
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("[toss] %(levelname)s %(message)s"))
    logger.addHandler(_h)
logger.setLevel(logging.INFO)

BASE = "https://openapi.tossinvest.com"
_TOKEN_PATH = PROJECT_ROOT / "data" / ".toss_token.json"
_UA = {"User-Agent": "ipo-dashboard/1.0"}


class TossError(RuntimeError):
    """토스 API 일반 오류."""


class TossIPError(TossError):
    """IP allowlist 거부 (403 access_denied) — 등록 IP 에서만 호출 가능."""


def _f(v) -> Optional[float]:
    """문자열/숫자 → float. 빈값·파싱불가 시 None."""
    if v is None or str(v) == "":
        return None
    try:
        return float(str(v).replace(",", ""))
    except (ValueError, TypeError):
        return None


def _request(method: str, path: str, *, data=None, headers=None,
             form: bool = False, timeout: int = 15, retries: int = 2) -> dict:
    url = BASE + path
    h = dict(_UA)
    h.update(headers or {})
    body = None
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            h["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode()
            h["Content-Type"] = "application/json"
    last = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            if e.code == 403 and "IP" in raw:
                raise TossIPError(
                    f"IP 미허용 — 토스 개발자센터에 호출 IP 등록 필요: {raw[:140]}")
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                wait = 1.5 * (attempt + 1)
                logger.warning("%s %s → HTTP %s 재시도(%.1fs): %s", method, path, e.code, wait, raw[:80])
                time.sleep(wait); last = e; continue
            raise TossError(f"{method} {path} HTTP {e.code}: {raw[:160]}")
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1)); last = e; continue
            raise TossError(f"{method} {path} 연결 실패: {e!r}")
    raise TossError(f"{method} {path} 실패: {last!r}")


def _load_cached_token() -> Optional[str]:
    try:
        d = json.loads(_TOKEN_PATH.read_text(encoding="utf-8"))
        if d.get("access_token") and float(d.get("expires_at", 0)) > time.time() + 60:
            return d["access_token"]
    except Exception:
        pass
    return None


def get_token(force: bool = False) -> str:
    if not force:
        cached = _load_cached_token()
        if cached:
            return cached
    key = os.environ.get("TOSS_APP_KEY", "")
    sec = os.environ.get("TOSS_APP_SECRET", "")
    if not key or not sec:
        raise TossError("TOSS_APP_KEY/TOSS_APP_SECRET 미설정 (.env 확인)")
    j = _request("POST", "/oauth2/token", form=True,
                 data={"grant_type": "client_credentials", "client_id": key, "client_secret": sec})
    token = j.get("access_token")
    if not token:
        raise TossError(f"토큰 미발급: {json.dumps(j)[:160]}")
    expires_at = time.time() + _f(j.get("expires_in")) if j.get("expires_in") else time.time() + 3600
    try:
        _TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        _TOKEN_PATH.write_text(json.dumps({"access_token": token, "expires_at": expires_at}), encoding="utf-8")
    except Exception as e:
        logger.warning("토큰 캐시 저장 실패(무시): %s", e)
    return token


def _auth() -> dict:
    return {"Authorization": f"Bearer {get_token()}"}


def get_candles(symbol: str, interval: str = "1d", count: int = 200,
                before: Optional[str] = None) -> list[dict]:
    """일봉(기본) 조회 → 과거→최근 정렬. 원소: {date, open, high, low, close, volume}.
    count 최대 200. before(ISO 8601, 예 '2025-10-12T00:00:00+09:00') 지정 시 그 시각 이전 200봉
    → 과거 상장 종목의 상장일 구간을 1회로 커버(상장일+약290일을 before 로 주면 상장일 포함)."""
    path = (f"/api/v1/candles?symbol={urllib.parse.quote(symbol)}"
            f"&interval={interval}&count={min(count, 200)}")
    if before:
        path += f"&before={urllib.parse.quote(before, safe='')}"
    j = _request("GET", path, headers=_auth())
    res = j.get("result", j)
    raw = res.get("candles") if isinstance(res, dict) else (res if isinstance(res, list) else [])
    out = [{
        "date": str(c.get("timestamp", ""))[:10],
        "open": _f(c.get("openPrice")),
        "high": _f(c.get("highPrice")),
        "low": _f(c.get("lowPrice")),
        "close": _f(c.get("closePrice")),
        "volume": _f(c.get("volume")),
    } for c in (raw or [])]
    out.sort(key=lambda x: x["date"])  # 과거→최근 (out[0]=상장일 근처)
    return out


def get_prices(symbols: list[str]) -> dict[str, float]:
    """현재가 조회 → {symbol: lastPrice}. 최대 200종목."""
    if not symbols:
        return {}
    q = ",".join(s for s in symbols[:200] if s)
    j = _request("GET", f"/api/v1/prices?symbols={urllib.parse.quote(q)}", headers=_auth())
    res = j.get("result", j)
    rows = res if isinstance(res, list) else (res.get("prices") or res.get("items") or [])
    out: dict[str, float] = {}
    for r in rows or []:
        sym = str(r.get("symbol", "")).strip()
        px = _f(r.get("lastPrice"))
        if sym and px is not None:
            out[sym] = px
    return out


if __name__ == "__main__":
    import sys
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    try:
        print("토큰:", "OK" if get_token() else "FAIL")
        c = get_candles("477850")
        print(f"마키나락스 일봉 {len(c)}봉: {c[0]['date']}~{c[-1]['date']}" if c else "봉 없음")
        print("현재가:", get_prices(["005930", "477850"]))
    except TossIPError as e:
        print("IP 미허용:", e)
