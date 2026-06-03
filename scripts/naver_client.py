#!/usr/bin/env python3
"""
네이버 금융 모바일 API 클라이언트 — 인증 불필요, 실시간 지수 + 투자자별 매매동향.

KRX OpenAPI 키 미활성 / KIS 모의투자 부정확 문제를 우회하는 안정적 소스.
공개 모바일 API (m.stock.naver.com) 사용. robots.txt 및 과도한 호출 자제 (캐시 권장).

엔드포인트:
    GET /api/index/{code}/basic  — 지수 현재가/등락 (code: KOSPI, KOSDAQ, KPI200 등)
    GET /api/index/{code}/trend  — 투자자별 순매수 (개인/외국인/기관, 백만원 단위)
"""
from __future__ import annotations
import logging
from typing import Any

import requests

NAVER_BASE = "https://m.stock.naver.com/api"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://m.stock.naver.com/",
    "Accept": "application/json",
}

logger = logging.getLogger("naver_client")
if not logger.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("[naver] %(levelname)s %(message)s"))
    logger.addHandler(h)
logger.setLevel(logging.INFO)


class NaverError(Exception):
    pass


def _num(s: Any) -> float | None:
    """'+34,524' / '8,083.46' / '-1.77' → float."""
    if s is None:
        return None
    try:
        return float(str(s).replace(",", "").replace("+", "").strip())
    except (ValueError, TypeError):
        return None


class NaverClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    def _get(self, path: str, timeout: int = 10) -> dict:
        url = f"{NAVER_BASE}{path}"
        try:
            r = self.session.get(url, timeout=timeout)
        except requests.exceptions.RequestException as e:
            raise NaverError(f"네트워크 오류 ({path}): {e}") from e
        if r.status_code != 200:
            raise NaverError(f"HTTP {r.status_code} {path}: {r.text[:120]}")
        try:
            return r.json()
        except ValueError:
            raise NaverError(f"JSON decode 실패 ({path})")

    def get_index(self, code: str = "KOSPI") -> dict:
        """지수 현재가 + 등락. code: KOSPI | KOSDAQ | KPI200."""
        d = self._get(f"/index/{code}/basic")
        return {
            "code": code,
            "name": d.get("stockName") or code,
            "value": _num(d.get("closePrice")),
            "change": _num(d.get("compareToPreviousClosePrice")),
            "change_pct": _num(d.get("fluctuationsRatio")),
            "market_status": d.get("marketStatus"),
            "traded_at": d.get("localTradedAt"),
            "delay": d.get("delayTimeName"),
        }

    def get_investor_trend(self, code: str = "KOSPI") -> dict:
        """투자자별 순매수 (개인/외국인/기관, 백만원 단위)."""
        d = self._get(f"/index/{code}/trend")
        return {
            "code": code,
            "date": d.get("bizdate"),
            "personal": _num(d.get("personalValue")),       # 개인 (백만원)
            "foreign": _num(d.get("foreignValue")),          # 외국인
            "institutional": _num(d.get("institutionalValue")),  # 기관
            "unit": "백만원",
        }


# CLI 테스트
if __name__ == "__main__":
    import sys, json
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    c = NaverClient()
    for code in ("KOSPI", "KOSDAQ"):
        print(f"\n=== {code} ===")
        try:
            print("지수:", json.dumps(c.get_index(code), ensure_ascii=False))
            print("투자자:", json.dumps(c.get_investor_trend(code), ensure_ascii=False))
        except NaverError as e:
            print("실패:", e)
