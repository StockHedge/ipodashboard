#!/usr/bin/env python3
"""baseline(data/ipo-recent.js)의 상장후 성과를 토스 일봉으로 재산출(in-place).

ticker(6자리) 종목별로 candles(before=상장일+290일, 1회 호출)를 받아 상장일~+9개월 구간을
커버하고 highReturn/daysToHigh/return1M/3M/6M 을 실측값으로 갱신한다(기존 시트값 대체).
토스는 IP allowlist 방식이라 로컬(등록 IP)에서 실행. extract_ipo_data.py 로 시트를 재생성한
뒤에는 본 스크립트를 다시 돌려 성과를 덮으면 된다.

정의는 fetch_prices.compute_perf 와 동일(고가=기간 최고가, daysToHigh=상장일1 기준).
"""
from __future__ import annotations

import json
import logging
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).parent))

import toss_client as toss
from fetch_prices import compute_perf

logging.basicConfig(level=logging.INFO, format="[baseline] %(levelname)s %(message)s")
logger = logging.getLogger("refresh_baseline")

JS_PATH = PROJECT_ROOT / "data" / "ipo-recent.js"
_TICKER_RE = re.compile(r"^\d{6}$")
PERF_KEYS = ("highReturn", "daysToHigh", "return1M", "return3M", "return6M")


def _load():
    txt = JS_PATH.read_text(encoding="utf-8")
    m = re.search(r"window\.IPO_DATA_OVERRIDE\s*=\s*(\[.*?\])\s*;", txt, re.DOTALL)
    if not m:
        raise SystemExit("ipo-recent.js 의 IPO_DATA_OVERRIDE 배열 파싱 실패")
    return txt, m.span(1), json.loads(m.group(1))


def main() -> int:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    txt, (start, end), data = _load()
    logger.info("baseline %d건 로드 — 토스 일봉 성과 재산출 시작", len(data))
    updated = skipped = failed = 0
    for d in data:
        t = (d.get("ticker") or "").strip()
        ld = d.get("listingDate")
        off = d.get("offeringPrice")
        if not _TICKER_RE.match(t) or not ld or not off:
            skipped += 1
            continue
        before = (date.fromisoformat(ld) + timedelta(days=290)).isoformat() + "T00:00:00+09:00"
        try:
            candles = toss.get_candles(t, before=before)
            time.sleep(0.1)  # MARKET_DATA 10 TPS 여유
        except toss.TossIPError as ex:
            logger.error("IP 미허용 — 토스 등록 IP 에서 실행하세요: %s", ex)
            return 2
        except toss.TossError as ex:
            logger.warning("%s(%s) 일봉 실패: %s", d.get("name"), t, ex)
            failed += 1
            continue
        # 상장일 봉이 범위에 없으면(거래정지·상폐·코드변경 등) 기존값 유지
        if not candles or candles[0]["date"][:7] != ld[:7]:
            logger.warning("%s(%s) 상장일 봉 누락 (candles[0]=%s, 상장=%s) → 기존값 유지",
                           d.get("name"), t, candles[0]["date"] if candles else "-", ld)
            failed += 1
            continue
        p = compute_perf(candles, off)
        if not p:
            failed += 1
            continue
        for k in PERF_KEYS:
            if p[k] is not None:
                d[k] = p[k]
        updated += 1

    new_txt = txt[:start] + json.dumps(data, ensure_ascii=False, indent=2) + txt[end:]
    JS_PATH.write_text(new_txt, encoding="utf-8")
    logger.info("재산출 완료 — 갱신 %d / skip(ticker없음·정보부족) %d / 실패 %d", updated, skipped, failed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
