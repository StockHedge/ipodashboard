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
KRX_INDEX_DD = "/svc/apis/idx/krx_dd_trd"  # KRX 시리즈(KRX 300/TMI 등) — 코스피/코스닥 미포함
# 대표지수: 코스피/코스닥은 krx_dd_trd 에 없음 → 별도 endpoint (교차테스트 확정 2026-06)
KRX_KOSPI_DD = "/svc/apis/idx/kospi_dd_trd"    # 코스피 시리즈 (IDX_NM='코스피' 가 대표)
KRX_KOSDAQ_DD = "/svc/apis/idx/kosdaq_dd_trd"  # 코스닥 시리즈 (IDX_NM='코스닥' 가 대표)

logger = logging.getLogger("krx_client")
if not logger.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("[krx] %(levelname)s %(message)s"))
    logger.addHandler(h)
logger.setLevel(logging.INFO)

# ─── 한국 증시 공휴일 (KRX 휴장일) ─────────────────────────────────
# 출처: KRX 공식 공시(trading calendar) + 기획재정부 임시공휴일 고시.
# 음력 설·추석은 전·당일·익일 3일, 연도별 음력→양력 변환 적용.
# 보수적 원칙: 불확실한 날짜는 포함하지 않음(오탐이 미탐보다 안전).
# 임시공휴일은 고시 확정분만 포함.
KRX_HOLIDAYS: frozenset[str] = frozenset({
    # 2024
    "20240101",  # 신정
    "20240209", "20240210", "20240211", "20240212",  # 설연휴(2/9~12, 2/12 대체공휴일)
    "20240301",  # 삼일절
    "20240410",  # 국회의원 선거일 (임시공휴일)
    "20240501",  # 근로자의 날
    "20240505",  # 어린이날
    "20240506",  # 어린이날 대체공휴일
    "20240515",  # 부처님오신날
    "20240606",  # 현충일
    "20240815",  # 광복절
    "20240916", "20240917", "20240918",  # 추석연휴(9/16~18)
    "20240930",  # 임시공휴일(추석 대체)
    "20241003",  # 개천절
    "20241009",  # 한글날
    "20241225",  # 성탄절
    "20241231",  # KRX 연말 휴장
    # 2025
    "20250101",  # 신정
    "20250128", "20250129", "20250130",  # 설연휴(1/28~30)
    "20250301",  # 삼일절
    "20250303",  # 3·1절 대체공휴일
    "20250505",  # 어린이날 + 부처님오신날
    "20250506",  # 어린이날 대체공휴일
    "20250606",  # 현충일
    "20250815",  # 광복절
    "20251003",  # 개천절
    "20251005", "20251006", "20251007",  # 추석연휴(10/5~7)
    "20251008",  # 추석 대체공휴일
    "20251009",  # 한글날
    "20251225",  # 성탄절
    "20251231",  # KRX 연말 휴장
    # 2026
    "20260101",  # 신정
    "20260216", "20260217", "20260218",  # 설연휴(2/16~18)
    "20260301",  # 삼일절
    "20260302",  # 삼일절 대체공휴일
    "20260505",  # 어린이날
    "20260524",  # 부처님오신날
    "20260606",  # 현충일
    "20260608",  # 지방선거일 (임시공휴일, 2026-06-08 확정)
    "20260815",  # 광복절
    "20260924", "20260925", "20260926",  # 추석연휴(9/24~26)
    "20261003",  # 개천절
    "20261009",  # 한글날
    "20261225",  # 성탄절
    "20261231",  # KRX 연말 휴장
})


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
    def get_market_investor(
        self,
        date: str | None = None,
        market: str = "KOSPI",
        _walk_back: int = 5,
    ) -> dict:
        """시장 투자자별 매매동향.

        date 미지정 시 최근 영업일에서 시작해 데이터가 없으면 직전 영업일로
        최대 _walk_back 회 역추적 (공휴일·T+1 지연 대응).
        401 인증 실패는 즉시 전파 (날짜 무관하게 모든 날짜에서 동일 실패).
        """
        requested = date or self._guess_latest_biz_day()
        explicit = date is not None
        is_kospi = market.upper() == "KOSPI"
        prefix = "kosp" if is_kospi else "kosdaq"
        prefix_short = "ksp" if is_kospi else "ksq"
        path_chain = [
            f"/svc/apis/sto/{prefix}_invr_summary",
            f"/svc/apis/sto/{prefix_short}_invr_summary",
            f"/svc/apis/idx/{prefix}_invr_summary",
            f"/svc/apis/idx/{prefix_short}_invr_summary",
            f"/svc/apis/sto/{prefix}_invsr_t",
            f"/svc/apis/sto/inv_value_tr",
            f"/svc/apis/sto/{prefix}_dly_invsr",
            f"/svc/apis/sto/inv_isu_invr_summary",
        ]

        cur = requested
        for walk_attempt in range(_walk_back + 1):
            last_err: KrxError | None = None
            for path in path_chain:
                try:
                    data = self._get(path, {"basDd": cur})
                    norm = self._normalize_investor(data, market, cur)
                    norm["_endpoint"] = path
                    norm["requested_date"] = requested
                    if norm.get("investors"):
                        return norm
                    last_err = KrxError(f"{path} 응답에 investors 없음")
                except KrxError as e:
                    last_err = e
                    if e.http_status not in (404, 400):
                        # 401(인증)·500 등은 날짜 바꿔도 해결 안 됨 — 즉시 전파
                        raise
            # 해당 날짜에서 모든 path 실패 → 역추적 조건 판단
            if explicit or walk_attempt >= _walk_back:
                break
            prev = self._prev_biz_day(cur)
            logger.warning(
                "get_market_investor: %s 데이터 없음 — %s 로 역추적 (%d/%d)",
                cur, prev, walk_attempt + 1, _walk_back,
            )
            cur = prev

        raise KrxError(
            f"시장 투자자 매매 endpoint 자동 탐색 실패 "
            f"(시도 날짜={cur}, path {len(path_chain)}개) — 마지막 오류: {last_err}"
        )

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

    @staticmethod
    def _is_krx_holiday(d: datetime) -> bool:
        """주말 또는 KRX 공휴일 여부."""
        return d.weekday() >= 5 or d.strftime("%Y%m%d") in KRX_HOLIDAYS

    def _guess_latest_biz_day(self) -> str:
        """오늘 기준 가장 가까운 영업일 (월~금, 공휴일 제외). 17:00 이전이면 전일."""
        d = datetime.now()
        if d.hour < 17:  # 장 마감 + 데이터 반영 시각 추정
            d -= timedelta(days=1)
        while self._is_krx_holiday(d):  # 주말 + 공휴일 건너뜀
            d -= timedelta(days=1)
        return d.strftime("%Y%m%d")

    # ----------------------------------------------------------------
    # KRX 시리즈 일별시세 (확정 endpoint: /svc/apis/idx/krx_dd_trd)
    # 한 날짜의 전체 KRX 지수 시세 → KOSPI/KOSDAQ 추출
    # ----------------------------------------------------------------
    def get_index_series(self, date: str | None = None, _walk_back: int = 7) -> dict:
        """KOSPI/KOSDAQ 대표지수 일별시세.

        주의: krx_dd_trd 는 KRX 시리즈(KRX 300 등)만 반환 → 코스피/코스닥은
        kospi_dd_trd / kosdaq_dd_trd 별도 endpoint 사용 (교차테스트 확정).
        date 미지정 시 최근 영업일이 아직 미반영(빈 배열)이면 직전 영업일로
        최대 _walk_back 회 역추적 (T+1 지연·휴장 대응). 명시 date 는 역추적 안 함.
        """
        requested = date or self._guess_latest_biz_day()
        cur = requested
        explicit = date is not None
        for _ in range(_walk_back + 1):
            kospi_rows = self._index_rows(KRX_KOSPI_DD, cur)
            if kospi_rows:
                kosdaq_rows = self._index_rows(KRX_KOSDAQ_DD, cur)
                out = {
                    "date": cur, "requested_date": requested,
                    "indices": {}, "raw_count": len(kospi_rows) + len(kosdaq_rows),
                }
                kospi = _pick_index(kospi_rows, "코스피")
                kosdaq = _pick_index(kosdaq_rows, "코스닥")
                if kospi:
                    out["indices"]["KOSPI"] = _extract_idx(kospi, "KOSPI", cur)
                if kosdaq:
                    out["indices"]["KOSDAQ"] = _extract_idx(kosdaq, "KOSDAQ", cur)
                return out
            if explicit:
                break  # 명시 날짜는 역추적 안 함 (호출자 의도 존중)
            cur = self._prev_biz_day(cur)
        return {"date": requested, "requested_date": requested, "indices": {}, "raw_count": 0}

    def _index_rows(self, path: str, date: str) -> list:
        # 인증 실패(401)는 모든 날짜에서 동일하게 실패하므로 즉시 전파(역추적 무의미).
        # 그 외 일시 오류(404/500/네트워크)는 빈 리스트로 강등해 walk-back 이 계속되게 함.
        try:
            data = self._get(path, {"basDd": date})
        except KrxError as e:
            if e.http_status == 401:
                raise
            return []
        rows = data.get("OutBlock_1") or data.get("output") or []
        return [rows] if isinstance(rows, dict) else rows

    @staticmethod
    def _prev_biz_day(yyyymmdd: str) -> str:
        """직전 영업일 (주말 + 공휴일 건너뜀)."""
        d = datetime.strptime(yyyymmdd, "%Y%m%d") - timedelta(days=1)
        while d.weekday() >= 5 or d.strftime("%Y%m%d") in KRX_HOLIDAYS:
            d -= timedelta(days=1)
        return d.strftime("%Y%m%d")

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


def _pick_index(rows: list, name: str) -> dict | None:
    """IDX_NM 정확 일치 행 선택 (코스피/코스닥 대표지수). 부분일치 금지 — 코스피200 등 오선택 방지."""
    for r in rows:
        if str(r.get("IDX_NM") or r.get("idx_nm") or "").strip() == name:
            return r
    return None


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

    # 1. KOSPI/KOSDAQ 대표지수 (kospi_dd_trd / kosdaq_dd_trd) - 키 활성화 검증의 핵심
    print(f"\n=== [1] KOSPI/KOSDAQ 대표지수 - {date or '직전 영업일 (미반영 시 자동 역추적)'} ===")
    print(f"    endpoint: {KRX_BASE}{KRX_KOSPI_DD} · {KRX_KOSDAQ_DD}")
    try:
        r = c.get_index_series(date)
        idx = r.get("indices", {})
        if idx:
            note = "" if r.get("date") == r.get("requested_date") else f" (요청 {r.get('requested_date')} → 반영일 {r.get('date')} 로 역추적)"
            print(f"    ✓ 성공! date={r.get('date')}{note}, 지수 {r.get('raw_count')}개 중 대표 {len(idx)}종 추출")
            print(json.dumps(idx, ensure_ascii=False, indent=2))
        else:
            print(f"    ⚠ date={r.get('date')} 빈 결과 — 해당 기간 데이터 미존재 (KRX 실서비스 기준 미래일 가능)")
    except KrxError as e:
        print(f"    ✗ 실패: {e}")

    # 2. 투자자별 매매 — 명세서 미제공 (참고용, 404 예상)
    print(f"\n=== [2] 투자자별 매매동향 (명세 미제공 — 별도 API 필요) ===")
    print(f"    Spec.docx 에는 지수 시세만 포함. 투자자별 매매는 KRX 콘솔 '주식' 카테고리의 별도 API.")
    print(f"    해당 API 명세(endpoint)를 제공하면 get_market_investor 의 path_chain 에 추가 가능.")
