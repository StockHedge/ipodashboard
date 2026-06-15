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
import os
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import requests

NAVER_BASE = "https://m.stock.naver.com/api"
NAVER_OPENAPI_BASE = "https://openapi.naver.com/v1/search"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://m.stock.naver.com/",
    "Accept": "application/json",
}

def _clean_html(s: str) -> str:
    """HTML 태그·&quot;·HTML 엔티티 제거."""
    s = re.sub(r"<[^>]+>", "", s)
    s = s.replace("&quot;", '"').replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&nbsp;", " ")
    return s.strip()


def _rfc2822_to_iso(raw: str) -> str:
    """'Mon, 12 Jun 2026 14:30:00 +0900' → '2026-06-12T14:30:00+09:00'."""
    if not raw:
        return ""
    try:
        dt = parsedate_to_datetime(raw)
        return dt.isoformat()
    except Exception:
        return raw


logger = logging.getLogger("naver_client")
if not logger.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("[naver] %(levelname)s %(message)s"))
    logger.addHandler(h)
logger.setLevel(logging.INFO)


class NaverError(Exception):
    pass


class NaverConfigError(NaverError):
    """키 미설정·인증 실패 등 설정 문제. .code 로 프론트 전달용 식별자 보유.

    런타임 네트워크 오류(NaverError)와 구분 — 설정 오류는 캐시하지 않고
    프론트에 그대로 코드를 전달해 사용자 안내(키 입력 등)를 띄운다.
    """
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


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

    def search_news(self, query: str, display: int = 10) -> list[dict]:
        """
        네이버 오픈API 뉴스 검색.
        환경변수 NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 필요.
        키 미설정/인증 실패 시 NaverConfigError(code) raise (호출자가 코드 전달).
        """
        # NAVER_NEWS_CLIENT_ID 우선, 레거시 NAVER_CLIENT_ID 폴백 (변수명 호환)
        client_id = os.environ.get("NAVER_NEWS_CLIENT_ID") or os.environ.get("NAVER_CLIENT_ID", "")
        client_secret = os.environ.get("NAVER_NEWS_CLIENT_SECRET") or os.environ.get("NAVER_CLIENT_SECRET", "")
        if not client_id or not client_secret:
            logger.warning("NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 미설정")
            raise NaverConfigError("naver_keys_missing")

        url = f"{NAVER_OPENAPI_BASE}/news.json"
        headers = {
            "X-Naver-Client-Id": client_id,
            "X-Naver-Client-Secret": client_secret,
        }
        params = {
            "query": query,
            "display": min(max(1, display), 100),
            "sort": "date",
        }
        # self.session 은 모바일 API 용 Referer/Accept 헤더를 보유 → openapi 호출에
        # 그 헤더가 섞이지 않도록 독립 requests.get 사용 (불필요 헤더 누출 방지).
        try:
            r = requests.get(url, headers=headers, params=params, timeout=10)
        except requests.exceptions.RequestException as e:
            raise NaverError(f"네이버 뉴스 네트워크 오류: {e}") from e
        if r.status_code == 401:
            logger.error("네이버 API 인증 실패 (401) — 키 확인 필요")
            raise NaverConfigError("naver_auth_failed")
        if r.status_code != 200:
            raise NaverError(f"네이버 뉴스 HTTP {r.status_code}: {r.text[:120]}")

        data = r.json()
        items = []
        for it in data.get("items", []):
            title = _clean_html(it.get("title", ""))
            desc = _clean_html(it.get("description", ""))
            pub_raw = it.get("pubDate", "")
            pub_iso = _rfc2822_to_iso(pub_raw)
            items.append({
                "title": title,
                "link": it.get("link", ""),
                "pubDate": pub_iso,
                "desc": desc,
                "source": "naver",
            })
        return items


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

    print("\n=== 뉴스 검색 테스트 (키 없으면 naver_keys_missing 신호 확인) ===")
    news = c.search_news("공모주", display=3)
    print(json.dumps(news, ensure_ascii=False, indent=2))
