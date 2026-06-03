#!/usr/bin/env python3
"""
KRX OpenAPI 클라이언트 — 한국거래소 공식 OpenAPI (https://openapi.krx.co.kr)
시장 투자자별 매매동향 + 종목 일별 매매정보 등 KIS personal 등급에서 미공개 영역 보강.

환경변수:
    KRX_API_KEY  — KRX 발급 AUTH_KEY (HTTP Header 로 전달)

엔드포인트 (KRX OpenAPI 공식 2024 기준):
    GET /svc/apis/sto/stk_bydd_trd?basDd=YYYYMMDD       — 주식 일별매매정보 (전체)
    GET /svc/apis/sto/ksp_bydd_trd?basDd=YYYYMMDD       — KOSPI 일별매매
    GET /svc/apis/sto/ksq_bydd_trd?basDd=YYYYMMDD       — KOSDAQ 일별매매
    GET /svc/apis/sto/inv_isu_invr_summary?basDd=...&isuCd=...   — 종목 투자자별 매매
    GET /svc/apis/idx/kosx_invr_summary?basDd=YYYYMMDD  — KOSPI 시장 투자자별 매매
    GET /svc/apis/idx/ksqx_invr_summary?basDd=YYYYMMDD  — KOSDAQ 시장 투자자별 매매

참고: KRX OpenAPI 는 영업일 기준 T+1 또는 장 종료 후 일별 데이터 제공 (실시간 X).
"""
from __future__ import annotations
import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import requests

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

# 명세서(Spec.docx) + 교차테스트 확정: data-dbg.krx.co.kr 가 정답 (openapi.krx.co.kr 는 404)
KRX_BASE = "https://data-dbg.krx.co.kr"
KRX_BASE_FALLBACK = "https://data-dbg.krx.co.kr"  # openapi 는 404 확정 → 단일 도메인
# 지수 일별시세 — "KRX 시리즈 일별시세정보". 한 날짜 전체 KRX 시리즈 지수 반환
# response OutBlock_1: BAS_DD/IDX_CLSS/IDX_NM/CLSPRC_IDX/CMPPREVDD_IDX/FLUC_RT/OPNPRC_IDX/HGPRC_IDX/LWPRC_IDX/ACC_TRDVOL/ACC_TRDVAL/MKTCAP
KRX_INDEX_DD = "/svc/apis/idx/krx_dd_trd"

logger = logging.getLogger("krx_client")
if not logger.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("[krx] %(levelname)s %(message)s"))
    logger.addHandler(h)
logger.setLevel(logging.INFO)


class KrxError(Exception):
    def __init__(self, message: str, http_status: int | None = None):
        super().__init__(message)
        self.http_status = http_status


class KrxClient:
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("KRX_API_KEY")
        if not self.api_key:
            raise KrxError("KRX_API_KEY 미설정. .env 또는 환경변수 확인.")
        self.session = requests.Session()
        logger.info(f"init key=***{self.api_key[-6:]}")

    def _headers(self) -> dict:
        return {
            "AUTH_KEY": self.api_key,
            "Content-Type": "application/json; charset=utf-8",
        }

    def _get(self, path: str, params: dict, timeout: int = 15) -> dict:
        # 교차테스트 확정: data-dbg.krx.co.kr 가 정답 (openapi 는 404). 단일 도메인.
        last_err = None
        bases = [KRX_BASE] if KRX_BASE == KRX_BASE_FALLBACK else [KRX_BASE, KRX_BASE_FALLBACK]
        for base in bases:
            url = f"{base}{path}"
            try:
                r = self.session.get(url, params=params, headers=self._headers(), timeout=timeout)
            except requests.exceptions.RequestException as e:
                last_err = KrxError(f"네트워크 오류 ({base}{path}): {e}")
                continue
            if r.status_code == 401:
                # 키 미등록/미활성/활용신청 미완 — 명확한 안내
                raise KrxError(
                    "KRX 키 인증 실패 (401 Unauthorized Key). 다음을 확인하세요:\n"
                    "  1) openapi.krx.co.kr 콘솔에서 키 발급 + 활성화 완료 여부\n"
                    "  2) 'KRX 시리즈 일별시세정보' API 의 [활용신청] 완료 여부 (API별 개별 신청)\n"
                    "  3) 키 복사 시 앞뒤 공백/오타\n"
                    "  4) 발급 직후면 활성화까지 수 분~시간 소요 가능",
                    http_status=401,
                )
            if r.status_code != 200:
                last_err = KrxError(f"HTTP {r.status_code} {base}{path}: {r.text[:150]}", http_status=r.status_code)
                if r.status_code in (404, 403, 500):
                    continue
                raise last_err
            try:
                return r.json()
            except json.JSONDecodeError:
                last_err = KrxError(f"JSON decode 실패 ({base}{path}): {r.text[:150]}")
                continue
        raise last_err or KrxError(f"모든 base URL 실패: {path}")

    # ----------------------------------------------------------------
    # 시장 투자자별 매매동향
    # KRX OpenAPI 공식 endpoint 명이 추측 기반 → fallback chain 으로 자동 시도
    # 사용자가 정확한 endpoint 알려주면 path_chain 우선순위 조정
    # ----------------------------------------------------------------
    def get_market_investor(self, date: str | None = None, market: str = "KOSPI") -> dict:
        if not date:
            date = self._guess_latest_biz_day()
        is_kospi = market.upper() == "KOSPI"
        # 여러 가능 endpoint chain (404 시 다음 시도)
        prefix = "kosp" if is_kospi else "kosdaq"
        prefix_short = "ksp" if is_kospi else "ksq"
        path_chain = [
            f"/svc/apis/sto/{prefix}_invr_summary",       # 일반 추측
            f"/svc/apis/sto/{prefix_short}_invr_summary",  # 단축 형식
            f"/svc/apis/idx/{prefix}_invr_summary",
            f"/svc/apis/idx/{prefix_short}_invr_summary",
            f"/svc/apis/sto/{prefix}_invsr_t",             # 별도 형식
            f"/svc/apis/sto/inv_value_tr",                 # 통합 endpoint (시장 X)
            f"/svc/apis/sto/{prefix}_dly_invsr",
            f"/svc/apis/sto/inv_isu_invr_summary",
        ]
        last_err = None
        for path in path_chain:
            try:
                data = self._get(path, {"basDd": date})
                # 정상 응답 시 정규화 + 사용된 path 표시
                norm = self._normalize_investor(data, market, date)
                norm["_endpoint"] = path
                if norm.get("investors"):  # 비어있지 않을 때만 채택
                    return norm
                last_err = KrxError(f"{path} 응답에 investors 없음")
            except KrxError as e:
                last_err = e
                if e.http_status not in (404, 400):
                    # 404/400 이 아니면 (예: 401 인증, 500 서버) 즉시 중단
                    break
        # 모든 시도 실패
        raise KrxError(f"시장 투자자 매매 endpoint 자동 탐색 실패 (시도 {len(path_chain)}개) — 마지막 오류: {last_err}")

    def _normalize_investor(self, raw: dict, market: str, date: str) -> dict:
        """KRX OpenAPI 응답을 일관 dict 로 정규화."""
        rows = raw.get("OutBlock_1") or raw.get("output") or raw.get("result") or []
        if isinstance(rows, dict):
            rows = [rows]
        # 외인/기관/개인 순매수 추출 (KRX 필드명 다양 — 가능한 변형 모두 시도)
        out = {"market": market, "date": date, "raw_count": len(rows), "investors": []}
        for r in rows:
            name = r.get("INVST_NM") or r.get("invst_nm") or r.get("ivst_nm") or r.get("INV_NM") or r.get("invst_no")
            net = _f(r.get("NETBY_TR_PBL") or r.get("netby_tr_pbl") or r.get("net_buy_amt") or r.get("NET_AMT"))
            buy = _f(r.get("ASK_TR_PBL") or r.get("ask_tr_pbl"))
            sell = _f(r.get("BID_TR_PBL") or r.get("bid_tr_pbl"))
            if name:
                out["investors"].append({"name": str(name), "net": net, "buy": buy, "sell": sell})
        return out

    def _guess_latest_biz_day(self) -> str:
        """오늘 기준 가장 가까운 영업일 (월~금). 시간 14:30 이전이면 전일."""
        d = datetime.now()
        if d.hour < 17:  # 장 마감 + 데이터 반영 시각 추정
            d -= timedelta(days=1)
        while d.weekday() >= 5:  # 토(5), 일(6) 건너뜀
            d -= timedelta(days=1)
        return d.strftime("%Y%m%d")

    # ----------------------------------------------------------------
    # KRX 시리즈 일별시세 (확정 endpoint: /svc/apis/idx/krx_dd_trd)
    # 한 날짜의 전체 KRX 지수 시세 → KOSPI/KOSDAQ 추출
    # ----------------------------------------------------------------
    def get_index_series(self, date: str | None = None) -> dict:
        if not date:
            date = self._guess_latest_biz_day()
        data = self._get(KRX_INDEX_DD, {"basDd": date})
        rows = data.get("OutBlock_1") or data.get("output") or []
        if isinstance(rows, dict):
            rows = [rows]
        # KOSPI / KOSDAQ 지수 추출 (IDX_NM 또는 유사 필드)
        out = {"date": date, "indices": {}, "raw_count": len(rows)}
        for r in rows:
            name = str(r.get("IDX_NM") or r.get("idx_nm") or r.get("INDX_NM") or "")
            # 대표 지수만
            if name in ("코스피", "KOSPI"):
                out["indices"]["KOSPI"] = _extract_idx(r, "KOSPI", date)
            elif name in ("코스닥", "KOSDAQ"):
                out["indices"]["KOSDAQ"] = _extract_idx(r, "KOSDAQ", date)
        return out

    # ----------------------------------------------------------------
    # 종목 일별 매매정보 (참고용)
    # ----------------------------------------------------------------
    def get_stock_daily(self, date: str | None = None, market: str = "ALL") -> list:
        if not date:
            date = self._guess_latest_biz_day()
        path_map = {
            "ALL": "/svc/apis/sto/stk_bydd_trd",
            "KOSPI": "/svc/apis/sto/ksp_bydd_trd",
            "KOSDAQ": "/svc/apis/sto/ksq_bydd_trd",
        }
        path = path_map.get(market.upper(), path_map["ALL"])
        data = self._get(path, {"basDd": date})
        rows = data.get("OutBlock_1") or data.get("output") or []
        if isinstance(rows, dict):
            rows = [rows]
        return rows


def _extract_idx(r: dict, name: str, date: str) -> dict:
    """KRX 지수 row → 표준 dict (필드명 변형 대응)."""
    return {
        "name": name, "date": date,
        "close": _f(r.get("CLSPRC_IDX") or r.get("clsprc_idx") or r.get("TDD_CLSPRC")),
        "change": _f(r.get("CMPPREVDD_IDX") or r.get("cmpprevdd_idx")),
        "change_pct": _f(r.get("FLUC_RT") or r.get("fluc_rt")),
        "open": _f(r.get("OPNPRC_IDX") or r.get("opnprc_idx")),
        "high": _f(r.get("HGPRC_IDX") or r.get("hgprc_idx")),
        "low": _f(r.get("LWPRC_IDX") or r.get("lwprc_idx")),
        "volume": _f(r.get("ACC_TRDVOL") or r.get("acc_trdvol")),
        "value": _f(r.get("ACC_TRDVAL") or r.get("acc_trdval")),
    }


def _f(v: Any) -> float | None:
    try:
        if v is None or v == "":
            return None
        return float(str(v).replace(",", ""))
    except (ValueError, TypeError):
        return None


if __name__ == "__main__":
    import sys
    # Windows cp949 콘솔에서 한글/특수문자(— ✓) 출력 안전하게
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass
    c = KrxClient()
    date = sys.argv[1] if len(sys.argv) > 1 else None

    # 1. 지수 일별시세 (명세서 확정 endpoint) - 키 활성화 검증의 핵심
    print(f"\n=== [1] KRX 시리즈 일별시세 (지수) - {date or '직전 영업일'} ===")
    print(f"    endpoint: {KRX_BASE}{KRX_INDEX_DD}")
    try:
        r = c.get_index_series(date)
        print(f"    ✓ 성공! date={r.get('date')}, 전체 지수 {r.get('raw_count')}개")
        idx = r.get("indices", {})
        if idx:
            print(f"    추출된 KOSPI/KOSDAQ: {list(idx.keys())}")
            print(json.dumps(idx, ensure_ascii=False, indent=2))
        else:
            print("    ⚠ KOSPI/KOSDAQ 매칭 실패 — IDX_NM 실제 값 확인 필요 (raw sample 아래):")
            # 디버그: 원본 첫 3개 row 의 IDX_NM 표시
            raw = c._get(KRX_INDEX_DD, {"basDd": date or c._guess_latest_biz_day()})
            rows = raw.get("OutBlock_1") or []
            for rr in rows[:5]:
                print(f"      IDX_NM={rr.get('IDX_NM')!r} CLSPRC={rr.get('CLSPRC_IDX')!r}")
    except KrxError as e:
        print(f"    ✗ 실패: {e}")

    # 2. 투자자별 매매 — 명세서 미제공 (참고용, 404 예상)
    print(f"\n=== [2] 투자자별 매매동향 (명세 미제공 — 별도 API 필요) ===")
    print(f"    Spec.docx 에는 지수 시세만 포함. 투자자별 매매는 KRX 콘솔 '주식' 카테고리의 별도 API.")
    print(f"    해당 API 명세(endpoint)를 제공하면 get_market_investor 의 path_chain 에 추가 가능.")
