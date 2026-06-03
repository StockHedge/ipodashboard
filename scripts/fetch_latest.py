#!/usr/bin/env python3
"""
IPO 최신 데이터 수집 스크립트 (DART 중심 + 38.co.kr 보조)

설계 철학:
- DART OpenAPI를 primary discovery 소스로 사용 (법적 안정성 높음)
- 38.co.kr은 경쟁률 등 보조 데이터용 (best-effort, 실패해도 동작)
- fdr + pykrx로 가격/수익률 보강
- 사용자의 기존 XLSX 데이터를 마스터로 유지하면서 신규 데이터만 보강하는 하이브리드 방식 권장

DART_API_KEY 필요 (환경변수 또는 .env)

사용법:
  python scripts/fetch_latest.py --append-to data/ipo-recent.json --days 30
"""

from __future__ import annotations
import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import requests

# 선택 의존성
try:
    import pandas as pd
except ImportError:
    pd = None

try:
    import FinanceDataReader as fdr
except ImportError:
    fdr = None

try:
    from pykrx import stock as pykrx_stock
except ImportError:
    pykrx_stock = None

try:
    from rapidfuzz import process, fuzz
    HAS_RAPIDFUZZ = True
except ImportError:
    HAS_RAPIDFUZZ = False

# 내부 모듈
try:
    from dart_client import DartClient, enrich_with_dart
    HAS_DART_CLIENT = True
except ImportError:
    HAS_DART_CLIENT = False

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
}
SLEEP = 1.0


def normalize_name(name: str) -> str:
    """간단한 회사명 정규화"""
    import unicodedata
    import re
    if not isinstance(name, str):
        return ""
    name = unicodedata.normalize("NFKC", name)
    name = re.sub(r'\(주\)|\(유\)|\(사\)|주식회사|㈜', '', name, flags=re.IGNORECASE)
    name = re.sub(r'[\(\)\[\]\s·•ㆍ]+', ' ', name).strip().lower()
    return name


def fetch_38_co_kr_fallback(days: int = 90) -> list[dict]:
    """38.co.kr 스크래핑 (best-effort, 실패해도 무시)"""
    if pd is None:
        return []
    results = []
    base = "https://www.38.co.kr/html/fund/index.htm"
    for param in ["o=k", "o=nw"]:
        try:
            resp = requests.get(f"{base}?{param}", headers=HEADERS, timeout=15)
            tables = pd.read_html(resp.text, flavor="lxml")
            for tbl in tables:
                if "종목" not in str(tbl.columns):
                    continue
                for _, row in tbl.iterrows():
                    name = str(row.get("종목명") or row.get("종목") or "").strip()
                    if not name or name == "nan":
                        continue
                    results.append({
                        "name": name,
                        "source": "38.co.kr",
                    })
            time.sleep(SLEEP)
        except Exception as e:
            print(f"[38.co.kr] {param} 스크래핑 실패 (무시): {e}", file=sys.stderr)
            continue
    return results


def enrich_with_fdr_and_pykrx(records: list[dict]) -> list[dict]:
    """fdr + pykrx로 Market, Ticker, 수익률 보강"""
    if fdr is None or not records:
        return records

    try:
        krx = fdr.StockListing("KRX")[["Code", "Name", "Market"]].copy()
        krx["norm"] = krx["Name"].apply(normalize_name)
        norm_map = {row["norm"]: row for _, row in krx.iterrows()}

        for rec in records:
            norm = normalize_name(rec.get("name", ""))
            if norm in norm_map:
                r = norm_map[norm]
                rec["market"] = "KOSPI" if r["Market"] == "KOSPI" else "KOSDAQ"
                rec["ticker"] = r["Code"]

        # pykrx 수익률 보강 (ticker 있는 상장 완료 종목만)
        if pykrx_stock:
            for rec in records:
                if not rec.get("ticker") or rec.get("firstDayReturn") is not None:
                    continue
                try:
                    ld = rec.get("listingDate")
                    if not ld:
                        continue
                    ohlcv = pykrx_stock.get_market_ohlcv(ld.replace("-", ""), "20991231", rec["ticker"])
                    if not ohlcv.empty and rec.get("offeringPrice"):
                        first = ohlcv.iloc[0]["종가"]
                        rec["firstDayReturn"] = round((first - rec["offeringPrice"]) / rec["offeringPrice"] * 100, 1)
                except Exception:
                    pass
                time.sleep(0.2)
    except Exception as e:
        print(f"[fdr/pykrx] 보강 중 오류 (무시): {e}", file=sys.stderr)
    return records


def main():
    parser = argparse.ArgumentParser(description="DART estkRs 성공 데이터 보강 도구 (현실적 하이브리드)")
    parser.add_argument("--append-to", required=True, help="기존 고품질 JSON (XLSX 추출 결과)을 로드하고 DART estkRs 성공 데이터로 보강")
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--candidates-out",
        default=None,
        help="estkRs 실패/필터된 DART 후보를 검토용 JSON 파일로 저장 (메인 데이터 오염 방지). 기본 None=저장 안함.",
    )
    args = parser.parse_args()

    print("=== IPO 데이터 보강 (DART estkRs 성공 데이터만) ===")
    print("철학: XLSX = 마스터 (역사+검증된 성과). DART = estkRs 성공 시에만 구조화 메타 보강.")
    print(f"DART 클라이언트: {HAS_DART_CLIENT}, pykrx: {pykrx_stock is not None}, fdr: {fdr is not None}")

    target_path = Path(args.append_to).resolve()
    existing = json.loads(target_path.read_text(encoding="utf-8")) if target_path.exists() else []
    print(f"기존 데이터 로드: {len(existing)}건")

    dart_key = os.getenv("DART_API_KEY")
    records: list[dict] = []  # estkRs 성공 또는 38 fallback 데이터 누적
    candidates_failed: list[dict] = []  # estkRs 실패/필터된 후보 (검토용 별도 저장)
    stats = {
        "discovered": 0,
        "filtered_out_pre_estk": 0,
        "estk_called": 0,
        "estk_success_with_data": 0,
        "estk_no_data": 0,
        "estk_error": 0,
    }

    # 1. DART로 신규 종목 발견 (primary)
    if HAS_DART_CLIENT and dart_key:
        try:
            client = DartClient(dart_key)
            bgn = (datetime.now() - timedelta(days=args.days)).strftime("%Y%m%d")
            end = datetime.now().strftime("%Y%m%d")

            # DART 신규상장(IPO) 중심 발견 (강력 필터링 적용)
            new_ipos = client.discover_new_ipos(bgn, end, max_pages=3)
            stats["discovered"] = len(new_ipos)
            print(f"DART 신규상장 후보 필터링: {len(new_ipos)}건")

            for r in new_ipos:
                corp_name = r.get("corp_name") or ""
                if not corp_name:
                    continue

                rec = {
                    "name": corp_name,
                    "source": "DART",
                    "rcept_no": r.get("rcept_no"),
                    "corp_code": r.get("corp_code"),
                    "stock_code": r.get("stock_code"),
                    "report_nm": r.get("report_nm"),
                }

                # estkRs 호출 전 추가 엄격 검증 (highest quality: only true new operating IPOs)
                report_nm = (r.get("report_nm") or "").lower()
                strong_new = any(kw in report_nm for kw in ["신규상장", "기업공개", "신규공모", "상장공모"])
                is_pre_ipo = (r.get("corp_cls") == "E")
                # SPAC/금융 필터링 — "제", "호"는 일반 회사명에서 빈번하므로 SPAC 패턴
                # "제N호 ..." 형태로 정밀화 (예: "엔에이치제20호기업인수목적")
                import re as _re
                is_financial_or_spac = (
                    any(x in corp_name for x in ["증권", "은행", "스팩", "인수목적"])
                    or bool(_re.search(r"제\s*\d+\s*호", corp_name))
                )

                if (strong_new or is_pre_ipo) and not is_financial_or_spac and r.get("corp_code"):
                    try:
                        bgn = (datetime.now() - timedelta(days=args.days)).strftime("%Y%m%d")
                        end = datetime.now().strftime("%Y%m%d")
                        stats["estk_called"] += 1
                        details = client.fetch_estk_details(r["corp_code"], bgn, end)
                        if details:
                            d = details[0]
                            if d.get("slprc") and d.get("slta"):  # only real data
                                rec["offeringPrice"] = d.get("slprc")
                                rec["offeringAmount"] = d.get("slta")
                                rec["dart_subscription_date"] = d.get("sbd")
                                rec["dart_underwriter"] = d.get("actnmn")
                                rec["dart_rcept_no"] = d.get("rcept_no")
                                records.append(rec)  # only append if estkRs succeeded with data
                                stats["estk_success_with_data"] += 1
                            else:
                                stats["estk_no_data"] += 1
                                candidates_failed.append({
                                    **rec,
                                    "_reason": "estkRs returned but no slprc/slta",
                                })
                                print(f"[DART] {corp_name} estkRs had no price/amount data")
                        else:
                            stats["estk_no_data"] += 1
                            candidates_failed.append({**rec, "_reason": "estkRs returned 0 rows"})
                            print(f"[DART] {corp_name} estkRs returned no data")
                    except Exception as e:
                        stats["estk_error"] += 1
                        candidates_failed.append({**rec, "_reason": f"estkRs exception: {e.__class__.__name__}"})
                        print(f"[DART] estkRs for {corp_name} exception: {e}", file=sys.stderr)
                else:
                    stats["filtered_out_pre_estk"] += 1
                    candidates_failed.append({
                        **rec,
                        "_reason": "filtered pre-estkRs (not strong-new or finance/SPAC)",
                    })
                    print(f"[DART] {corp_name} filtered out pre-estkRs (not strong new or is financial/SPAC)")
        except Exception as e:
            print(f"[DART] 신고서 검색 실패: {e}", file=sys.stderr)

    # 2. 38.co.kr 보조 (경쟁률 등) - best effort, records 가 5건 미만일 때만
    if len(records) < 5:
        print("38.co.kr 보조 수집 시도...")
        extra = fetch_38_co_kr_fallback(args.days)
        records.extend(extra)

    # 3. DART 레코드에 대한 fuzzy name matching (corp_code 없는 경우 대비)
    if fdr is not None and records:
        try:
            krx = fdr.StockListing("KRX")[["Code", "Name", "Market"]].copy()
            krx["norm"] = krx["Name"].apply(normalize_name)
            norm_list = krx["norm"].tolist()

            for rec in records:
                if rec.get("corp_code"):
                    continue  # 이미 corp_code 있으면 스킵
                target_norm = normalize_name(rec.get("name", ""))
                if not target_norm:
                    continue
                # rapidfuzz 우선, 미설치 시 exact match 만
                row = None
                score = 0.0
                if HAS_RAPIDFUZZ and norm_list:
                    best = process.extractOne(target_norm, norm_list, scorer=fuzz.WRatio, score_cutoff=80)
                    if best:
                        matched_norm, raw_score, idx = best
                        score = raw_score / 100.0
                        row = krx.iloc[idx]
                else:
                    exact = krx[krx["norm"] == target_norm]
                    if not exact.empty:
                        row = exact.iloc[0]
                        score = 1.0
                if row is not None:
                    rec["market"] = "KOSPI" if row["Market"] == "KOSPI" else "KOSDAQ"
                    rec["ticker"] = row["Code"]
                    if score < 0.9:
                        rec["_fuzzy_match"] = round(score, 3)
        except Exception as e:
            print(f"[fdr] DART 레코드 fuzzy 매칭 실패 (무시): {e}", file=sys.stderr)

    # 3. fdr + pykrx 보강
    records = enrich_with_fdr_and_pykrx(records)

    # 4. DART 상세 보강 (corp_code 또는 ticker 있는 경우)
    if HAS_DART_CLIENT and dart_key and records:
        try:
            client = DartClient(dart_key)
            # 직접 corp_code가 있는 레코드는 우선 사용
            for rec in records:
                if rec.get("corp_code") and not rec.get("ticker"):
                    # corp_code로 직접 estkRs 시도 가능하도록 임시 ticker 필드 활용
                    pass
            records = enrich_with_dart(records, client, days=args.days)
        except Exception as e:
            print(f"[DART] 상세 보강 실패: {e}", file=sys.stderr)

    # 스키마 정규화
    normalized = []
    for i, r in enumerate(records, 1):
        normalized.append({
            "id": i,
            "name": r.get("name"),
            "listingDate": r.get("listingDate"),
            "market": r.get("market", "KOSDAQ"),
            "sector": r.get("sector", "기타"),
            "offeringPrice": r.get("offeringPrice") or 0,
            "firstDayReturn": r.get("firstDayReturn"),
            "highReturn": r.get("highReturn"),
            "competitionRetail": r.get("competitionRetail"),
            "underwriter": r.get("underwriter") or r.get("dart_underwriter"),
            "source": r.get("source"),
        })

    print(f"최종 수집: {len(normalized)}건")

    dart_count = sum(1 for r in normalized if r.get("source") == "DART")
    print(f"  - DART 출처: {dart_count}건")
    print(f"  - 38.co.kr 출처: {len(normalized) - dart_count}건")

    print("\n=== DART estkRs 통계 ===")
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    success_ratio = (
        stats["estk_success_with_data"] / stats["estk_called"] * 100
        if stats["estk_called"] else 0.0
    )
    print(f"estkRs 성공률 (호출 대비 데이터 있음): {success_ratio:.1f}%")
    print(f"검토용 후보 (실패/필터됨): {len(candidates_failed)}건")

    # 검토용 후보 JSON 저장 (메인 데이터와 분리)
    if args.candidates_out and candidates_failed:
        cpath = Path(args.candidates_out).resolve()
        cpath.parent.mkdir(parents=True, exist_ok=True)
        cpath.write_text(
            json.dumps(
                {
                    "generated_at": datetime.now().isoformat(),
                    "stats": stats,
                    "candidates": candidates_failed,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"검토용 후보 저장: {cpath}")

    if args.dry_run:
        print("\n=== Dry-run preview (first 3 normalized records) ===")
        print(json.dumps(normalized[:3], ensure_ascii=False, indent=2))
        if candidates_failed:
            print("\n=== Dry-run preview (first 3 failed candidates) ===")
            print(json.dumps(candidates_failed[:3], ensure_ascii=False, indent=2))
        return

    # 스마트 보강: DART estkRs 성공 데이터로 기존 고품질 레코드 업데이트 또는 추가
    p = Path(args.append_to).resolve()
    existing = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    merged = {(e.get("name"), e.get("listingDate")): e for e in existing}

    added = 0
    updated = 0
    for r in normalized:
        key = (r.get("name"), r.get("listingDate"))
        if key in merged:
            ex = merged[key]
            # DART 데이터가 더 좋으면 보강 (offeringPrice가 0이거나 없을 때)
            if (ex.get("offeringPrice") or 0) == 0 and r.get("offeringPrice"):
                for k in ["offeringPrice", "offeringAmount", "dart_subscription_date", "dart_underwriter"]:
                    if r.get(k):
                        ex[k] = r[k]
                if "DART" not in str(ex.get("source", "")):
                    ex["source"] = f"{ex.get('source', 'XLSX')}+DART"
                updated += 1
        else:
            merged[key] = r
            added += 1

    final = list(merged.values())
    p.write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n보강 완료: 신규 {added}건, 기존 업데이트 {updated}건, 총 {len(final)}건")
    print(f"파일: {p}")
    print("대시보드 새로고침 추천. (XLSX 마스터 + DART estkRs 성공 보강)")


if __name__ == "__main__":
    main()