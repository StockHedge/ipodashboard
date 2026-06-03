#!/usr/bin/env python3
"""
KIS Developers (한국투자증권 OpenAPI) — 현재가 + 지수 조회 클라이언트.

표준 라이브러리 + requests 만 사용 (외부 SDK 없음, 직접 REST 호출).

환경변수 (.env 자동 로드):
    KIS_APP_KEY      — 발급받은 앱 키
    KIS_APP_SECRET   — 발급받은 앱 시크릿
    KIS_USE_PAPER    — 'true' 면 모의투자 도메인 (기본 false=실전)

토큰 캐시:
    data/.kis_token.json — access_token + 만료시각. 24시간 유효, 만료 전 자동 재발급.

엔드포인트:
    - oauth2/tokenP            : 토큰 발급 (1일 1회 권장, 캐시됨)
    - inquire-price            : 종목 현재가 (FHKST01010100)
    - inquire-index-price      : 지수 현재가 (FHPUP02100000)
    - inquire-index-daily-price: 지수 일별 OHLCV (FHPUP02120000)
"""
from __future__ import annotations
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

TOKEN_PATH = PROJECT_ROOT / "data" / ".kis_token.json"
TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)

PROD_BASE = "https://openapi.koreainvestment.com:9443"
PAPER_BASE = "https://openapivts.koreainvestment.com:29443"  # 모의투자

logger = logging.getLogger("kis_client")
if not logger.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("[kis] %(levelname)s %(message)s"))
    logger.addHandler(h)
logger.setLevel(logging.INFO)


class KisError(Exception):
    def __init__(self, message: str, code: str | None = None, http_status: int | None = None):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


class KisClient:
    def __init__(
        self,
        app_key: str | None = None,
        app_secret: str | None = None,
        use_paper: bool | None = None,
    ):
        self.app_key = app_key or os.environ.get("KIS_APP_KEY")
        self.app_secret = app_secret or os.environ.get("KIS_APP_SECRET")
        if not self.app_key or not self.app_secret:
            raise KisError("KIS_APP_KEY / KIS_APP_SECRET 미설정. .env 또는 환경변수 확인.")
        if use_paper is None:
            raw = (os.environ.get("KIS_USE_PAPER", "") or "").split("#")[0].strip().lower()
            use_paper = raw in ("true", "1", "yes")
        self.use_paper = use_paper
        self.base = PAPER_BASE if self.use_paper else PROD_BASE
        self.session = requests.Session()
        logger.info(f"init mode={'paper' if self.use_paper else 'real'} base={self.base}")

    # ----------------------------------------------------------------
    # OAuth 토큰 (24시간 유효, 캐시)
    # ----------------------------------------------------------------
    def _get_token(self) -> str:
        if TOKEN_PATH.exists():
            try:
                cached = json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
                expires_at = cached.get("expires_at", 0)
                # 만료 30분 전 갱신 + 같은 모드 (paper/real) 확인
                if (cached.get("token") and time.time() < expires_at - 1800
                        and cached.get("mode") == ("paper" if self.use_paper else "real")):
                    return cached["token"]
            except (json.JSONDecodeError, KeyError):
                pass

        url = f"{self.base}/oauth2/tokenP"
        payload = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
        }
        try:
            r = self.session.post(url, json=payload, timeout=10)
            r.raise_for_status()
            data = r.json()
        except requests.exceptions.RequestException as e:
            raise KisError(f"토큰 발급 실패: {e}") from e
        token = data.get("access_token")
        expires_in = int(data.get("expires_in", 86400))
        if not token:
            raise KisError(f"토큰 응답에 access_token 없음: {data}")
        cache = {
            "token": token,
            "token_type": data.get("token_type", "Bearer"),
            "expires_at": time.time() + expires_in,
            "issued_at": datetime.now().isoformat(timespec="seconds"),
            "mode": "paper" if self.use_paper else "real",
        }
        TOKEN_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(f"토큰 발급 성공 (만료 {expires_in}초 후)")
        return token

    def _headers(self, tr_id: str) -> dict:
        return {
            "Content-Type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self._get_token()}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id,
        }

    def _get(self, path: str, params: dict, tr_id: str, timeout: int = 10) -> dict:
        url = f"{self.base}{path}"
        try:
            r = self.session.get(url, params=params, headers=self._headers(tr_id), timeout=timeout)
        except requests.exceptions.RequestException as e:
            raise KisError(f"네트워크 오류 ({path}): {e}") from e
        if r.status_code != 200:
            raise KisError(f"HTTP {r.status_code} {path}: {r.text[:200]}", http_status=r.status_code)
        data = r.json()
        rt_cd = data.get("rt_cd")
        if rt_cd not in ("0", None):
            raise KisError(f"KIS API 오류 rt_cd={rt_cd} msg={data.get('msg1', '')}", code=rt_cd)
        return data

    # ----------------------------------------------------------------
    # 종목 현재가 (TR ID: FHKST01010100)
    # ----------------------------------------------------------------
    def get_current_price(self, ticker: str) -> dict:
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": str(ticker).zfill(6)}
        data = self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-price",
            params, tr_id="FHKST01010100",
        )
        out = data.get("output", {}) or {}
        return {
            "ticker": ticker,
            "name": out.get("hts_kor_isnm"),
            "price": _f(out.get("stck_prpr")),
            "change": _f(out.get("prdy_vrss")),
            "changeRate": _f(out.get("prdy_ctrt")),
            "open": _f(out.get("stck_oprc")),
            "high": _f(out.get("stck_hgpr")),
            "low": _f(out.get("stck_lwpr")),
            "volume": _i(out.get("acml_vol")),
            "marketCap": _i(out.get("hts_avls")),
            "per": _f(out.get("per")),
            "pbr": _f(out.get("pbr")),
            "high52w": _f(out.get("w52_hgpr")),
            "low52w": _f(out.get("w52_lwpr")),
        }

    # ----------------------------------------------------------------
    # 지수 현재가 (KOSPI=0001, KOSDAQ=1001)
    # TR ID: FHPUP02100000
    # ----------------------------------------------------------------
    def get_index_price(self, code: str = "0001") -> dict:
        params = {"FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": code}
        data = self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-index-price",
            params, tr_id="FHPUP02100000",
        )
        out = data.get("output", {}) or {}
        return {
            "code": code,
            "name": {"0001": "KOSPI", "1001": "KOSDAQ", "2001": "KOSPI200"}.get(code, code),
            "price": _f(out.get("bstp_nmix_prpr")),
            "change": _f(out.get("bstp_nmix_prdy_vrss")),
            "changeRate": _f(out.get("bstp_nmix_prdy_ctrt")),
            "open": _f(out.get("bstp_nmix_oprc")),
            "high": _f(out.get("bstp_nmix_hgpr")),
            "low": _f(out.get("bstp_nmix_lwpr")),
            "volume": _i(out.get("acml_vol")),
        }

    # ----------------------------------------------------------------
    # 국내주식 기간별 일봉 OHLCV (TR ID: FHKST03010100)
    # 상장일+30일 같은 post-IPO drift 차트 데이터 소스
    # ----------------------------------------------------------------
    def get_daily(self, ticker: str, days: int = 60, end_date: str | None = None) -> list[dict]:
        """
        종목 일별 OHLCV (최신 → 과거 순 최대 100건).
        end_date 기본은 오늘. days 만큼 거슬러 조회.
        """
        if end_date is None:
            end_date = datetime.now().strftime("%Y%m%d")
        from datetime import timedelta as _td
        start_dt = datetime.strptime(end_date, "%Y%m%d") - _td(days=max(int(days), 1) + 14)
        start_date = start_dt.strftime("%Y%m%d")
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": str(ticker).zfill(6),
            "FID_INPUT_DATE_1": start_date,
            "FID_INPUT_DATE_2": end_date,
            "FID_PERIOD_DIV_CODE": "D",
            "FID_ORG_ADJ_PRC": "0",  # 수정주가 미적용 (원본)
        }
        try:
            data = self._get(
                "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
                params, tr_id="FHKST03010100",
            )
        except KisError as e:
            logger.warning(f"get_daily({ticker}) 실패: {e}")
            return []
        rows = data.get("output2") or []
        if isinstance(rows, dict):
            rows = [rows]
        out = []
        for r in rows[:days]:
            d = r.get("stck_bsop_date")
            if not d:
                continue
            out.append({
                "date": f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(str(d)) == 8 else d,
                "close": _f(r.get("stck_clpr")),
                "open": _f(r.get("stck_oprc")),
                "high": _f(r.get("stck_hgpr")),
                "low": _f(r.get("stck_lwpr")),
                "volume": _i(r.get("acml_vol")),
            })
        # 시간순 (과거→최신)
        out.sort(key=lambda x: x["date"])
        return out

    # ----------------------------------------------------------------
    # 지수 일별 OHLCV (MA200 계산용, TR ID: FHPUP02120000)
    # ----------------------------------------------------------------
    def get_index_daily(self, code: str = "0001", count: int = 250) -> list[dict]:
        params = {
            "FID_COND_MRKT_DIV_CODE": "U",
            "FID_INPUT_ISCD": code,
            "FID_INPUT_DATE_1": "",
            "FID_INPUT_DATE_2": "",
            "FID_PERIOD_DIV_CODE": "D",
        }
        try:
            data = self._get(
                "/uapi/domestic-stock/v1/quotations/inquire-index-daily-price",
                params, tr_id="FHPUP02120000",
            )
        except KisError as e:
            logger.warning(f"index_daily 실패 (모의투자에선 미지원 가능): {e}")
            return []
        rows = data.get("output2") or data.get("output") or []
        if isinstance(rows, dict):
            rows = [rows]
        out = []
        for r in rows[:count]:
            out.append({
                "date": r.get("stck_bsop_date"),
                "close": _f(r.get("bstp_nmix_prpr") or r.get("ovrs_nmix_prpr")),
                "open": _f(r.get("bstp_nmix_oprc")),
                "high": _f(r.get("bstp_nmix_hgpr")),
                "low": _f(r.get("bstp_nmix_lwpr")),
                "volume": _i(r.get("acml_vol")),
            })
        return out


def _f(v: Any) -> float | None:
    try:
        if v is None or v == "":
            return None
        return float(str(v).replace(",", ""))
    except (ValueError, TypeError):
        return None


def _i(v: Any) -> int | None:
    f = _f(v)
    return int(f) if f is not None else None


# CLI 테스트
if __name__ == "__main__":
    c = KisClient()
    print("토큰 발급/캐시 확인...")
    c._get_token()
    print("\n삼성전자(005930) 현재가:")
    print(json.dumps(c.get_current_price("005930"), ensure_ascii=False, indent=2, default=str))
    print("\nKOSPI 지수:")
    print(json.dumps(c.get_index_price("0001"), ensure_ascii=False, indent=2, default=str))
    print("\nKOSDAQ 지수:")
    print(json.dumps(c.get_index_price("1001"), ensure_ascii=False, indent=2, default=str))
