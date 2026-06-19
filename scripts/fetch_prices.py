#!/usr/bin/env python3
"""신규상장 종목의 상장 후 성과(고가·기간수익률)를 토스 일봉으로 산출 → data/recent-perf.json.

로컬(토스에 등록한 공인 IP)에서 주기 실행한다. serve.py / listings_client 는 이 정적 파일만
읽어 병합하므로(토스 직접 호출 X), Render(비고정·해외 IP) 배포 환경에서도 성과가 노출된다.

정의 (baseline 데이터와 의미 일치, 토스 일봉으로 실측):
  - highReturn  = 공모가 대비 상장 후 기간 최고가 수익률(%)
  - daysToHigh  = 상장일(=1)부터 최고가 도달까지 거래일
  - return1M/3M/6M = 상장일 + 30/90/180일(달력) 이후 첫 거래일 종가 수익률(%); 미도래 시 null
한계: 토스 일봉의 상장일 첫 봉은 OHLC 동일(품질 이슈) → 장중 첫날 고가는 38 firstDayReturn 사용,
      본 모듈은 상장 이후 고가/기간수익률 보강에 한정.
"""
from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).parent))

import toss_client as toss
from listings_client import get_recent_listings

logging.basicConfig(level=logging.INFO, format="[perf] %(levelname)s %(message)s")
logger = logging.getLogger("fetch_prices")

OUT_PATH = PROJECT_ROOT / "data" / "recent-perf.json"
_TICKER_RE = re.compile(r"^\d{6}$")  # 6자리 숫자만 (스팩 'P' 코드 등 제외)


def _ret(close, offering):
    return round((close / offering - 1) * 100, 2) if (close and offering) else None


def _return_at(candles, listing_dt, days, offering):
    """상장일 + days(달력) 이후 첫 거래일 종가 수익률. 미도래/데이터부족 시 None."""
    target = listing_dt + timedelta(days=days)
    for c in candles:  # 과거→최근
        d = datetime.strptime(c["date"], "%Y-%m-%d").date()
        if d >= target and c["close"]:
            return _ret(c["close"], offering)
    return None


def compute_perf(candles: list[dict], offering: float) -> dict | None:
    if not candles or not offering:
        return None
    listing_dt = datetime.strptime(candles[0]["date"], "%Y-%m-%d").date()
    highs = [(i, c["high"]) for i, c in enumerate(candles) if c["high"]]
    high_ret = days_to_high = None
    if highs:
        idx, hi = max(highs, key=lambda x: x[1])
        high_ret = _ret(hi, offering)
        days_to_high = idx + 1  # 상장일 = 1
    return {
        "highReturn": high_ret,
        "daysToHigh": days_to_high,
        "return1M": _return_at(candles, listing_dt, 30, offering),
        "return3M": _return_at(candles, listing_dt, 90, offering),
        "return6M": _return_at(candles, listing_dt, 180, offering),
        "lastClose": candles[-1]["close"],
        "lastDate": candles[-1]["date"],
    }


def main() -> int:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    listings = get_recent_listings()
    logger.info("신규상장 %d건 — 토스 일봉 성과 산출 시작", len(listings))
    perf: dict[str, dict] = {}
    skipped: list[str] = []
    for r in listings:
        ticker = (r.get("ticker") or "").strip()
        offering = r.get("offeringPrice")
        name = r.get("name")
        if not _TICKER_RE.match(ticker):
            skipped.append(f"{name}(ticker={ticker or '없음'})")
            continue
        if not offering:
            skipped.append(f"{name}(공모가없음)")
            continue
        try:
            candles = toss.get_candles(ticker)
        except toss.TossIPError as e:
            logger.error("IP 미허용 — 토스에 등록한 IP 에서 실행하세요: %s", e)
            return 2
        except toss.TossError as e:
            logger.warning("%s(%s) 일봉 실패: %s", name, ticker, e)
            skipped.append(f"{name}(조회실패)")
            continue
        p = compute_perf(candles, offering)
        if not p:
            skipped.append(f"{name}(산출불가)")
            continue
        perf[ticker] = {"name": name, **p}
        logger.info("  %s(%s): 고가 %+.1f%% (D+%s) / 6M=%s / 봉%d",
                    name, ticker, p["highReturn"] or 0.0, p["daysToHigh"], p["return6M"], len(candles))

    payload = {"generated_at": datetime.now().isoformat(timespec="seconds"), "by_ticker": perf}
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("저장 %s — 성공 %d건 / skip %d건: %s",
                OUT_PATH.name, len(perf), len(skipped), ", ".join(skipped) or "-")
    return 0


if __name__ == "__main__":
    sys.exit(main())
