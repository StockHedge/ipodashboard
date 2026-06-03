#!/usr/bin/env python3
"""
DART OpenAPI 클라이언트 (IPO 지분증권 중심)
- corp_code 캐시 + estkRs + list.json
- timeout, retry, logging, rate-limit 강제
- .env + pathlib (Windows 한글 사용자명 안전)
- 제공 키 사용 시 절대 코드/저장소에 커밋 금지

사용 예:
  from dart_client import DartClient, enrich_with_dart
  dart = DartClient()  # DART_API_KEY 환경변수 또는 .env에서 로드
  records = enrich_with_dart(records, dart, days=90)
"""

from __future__ import annotations

import json
import logging
import os
import time
import zipfile
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any, Optional
from xml.etree import ElementTree as ET

import requests
from dotenv import load_dotenv

# 프로젝트 루트 기준 (Windows 한글 사용자명 대응)
PROJECT_ROOT = Path(__file__).resolve().parents[2] if Path(__file__).parent.name == "scripts" else Path.cwd()
CACHE_DIR = PROJECT_ROOT / "data"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

CORP_CACHE_PATH = CACHE_DIR / "dart_corp_code.json"

# .env 로드 (키 절대 하드코딩 금지)
load_dotenv(dotenv_path=PROJECT_ROOT / ".env")

logger = logging.getLogger(__name__)
if not logger.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

DART_BASE = "https://opendart.fss.or.kr/api"
RATE_LIMIT_SLEEP = 0.45  # 실측 기반 최소 간격
MAX_RETRIES = 4


class DartApiError(Exception):
    """DART API 도메인 에러 (에러코드 포함)."""
    def __init__(self, status: str, message: str, http_status: int | None = None):
        self.status = status
        self.message = message
        self.http_status = http_status
        super().__init__(f"DART {status}: {message}")


class DartClient:
    def __init__(self, api_key: str | None = None, timeout: int = 15):
        self.api_key = api_key or os.getenv("DART_API_KEY")
        if not self.api_key:
            raise DartApiError("CONFIG", "DART_API_KEY가 .env 또는 환경변수에 설정되어 있지 않습니다. 절대 코드에 하드코딩하지 마세요.")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "ipo-dashboard/1.0"})

    def _request(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        params = {**params, "crtfc_key": self.api_key}
        url = f"{DART_BASE}/{endpoint}"

        for attempt in range(MAX_RETRIES):
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
                resp.raise_for_status()
                data: dict[str, Any] = resp.json()

                status = data.get("status", "999")
                if status == "000":
                    time.sleep(RATE_LIMIT_SLEEP)
                    return data

                msg = data.get("message", "알 수 없는 오류")
                if status == "020":  # rate limit
                    wait = (2 ** attempt) * 1.5
                    logger.warning(f"DART rate limit (020). {wait:.1f}s 대기 후 재시도 ({attempt+1}/{MAX_RETRIES})")
                    time.sleep(wait)
                    continue

                raise DartApiError(status, msg, resp.status_code)

            except requests.exceptions.RequestException as e:
                if attempt == MAX_RETRIES - 1:
                    raise DartApiError("NETWORK", str(e)) from e
                time.sleep(1.5 ** attempt)

        raise DartApiError("RETRY_EXHAUSTED", "최대 재시도 횟수 초과")

    # === corp_code 캐시 ===
    def refresh_corp_code_cache(self) -> dict[str, dict[str, str]]:
        """corpCode.xml 다운로드 및 캐시 갱신 (한글 파일명 안전 처리 + 유연한 매칭)."""
        logger.info("corpCode.xml 다운로드 시작")
        params = {"crtfc_key": self.api_key}
        resp = self.session.get(f"{DART_BASE}/corpCode.xml", params=params, timeout=self.timeout)
        resp.raise_for_status()

        # DART가 에러를 ZIP이 아닌 XML/텍스트로 줄 수 있음
        content = resp.content
        if content[:5] == b'<?xml' or b'<error' in content[:200].lower():
            # 에러 응답일 가능성 높음
            logger.error("DART corpCode 다운로드가 에러 응답을 반환했습니다. 키 유효성/할당량 확인 필요.")
            raise DartApiError("DOWNLOAD", "corpCode.xml 다운로드 실패 (에러 응답)")

        with zipfile.ZipFile(BytesIO(content)) as zf:
            namelist = zf.namelist()
            # 더 유연한 매칭 (대소문자, corpcode, CORP_CODE 등)
            xml_name = next(
                (n for n in namelist 
                 if n.lower().endswith('.xml') and ('corp' in n.lower() or 'code' in n.lower())),
                None
            )
            if not xml_name:
                logger.error(f"ZIP 내부 파일 목록: {namelist}")
                raise DartApiError("PARSE", f"CORP_CODE.xml을 찾을 수 없습니다. 실제 파일명: {namelist}")
            xml_content = zf.read(xml_name)

        root = ET.fromstring(xml_content)
        cache: dict[str, dict[str, str]] = {}
        today = datetime.now().strftime("%Y%m%d")

        for item in root.findall("list"):
            corp_code = (item.findtext("corp_code") or "").strip()
            stock_code = (item.findtext("stock_code") or "").strip()
            corp_name = (item.findtext("corp_name") or "").strip()

            if not corp_code:
                continue

            # stock_code가 없어도 저장 (pre-IPO / 신규상장 후보 지원)
            key = stock_code if stock_code else f"PREIPO_{corp_code}"
            cache[key] = {
                "corp_code": corp_code,
                "corp_name": corp_name,
                "stock_code": stock_code,
                "modify_date": (item.findtext("modify_date") or "").strip(),
            }

        full_cache = {today: cache}
        CORP_CACHE_PATH.write_text(json.dumps(full_cache, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(f"corp_code 캐시 갱신 완료: {len(cache)}개 상장사")
        return cache

    def load_corp_code_cache(self) -> dict[str, dict[str, str]]:
        if not CORP_CACHE_PATH.exists():
            return self.refresh_corp_code_cache()
        data = json.loads(CORP_CACHE_PATH.read_text(encoding="utf-8"))
        latest_key = max(data.keys())
        return data[latest_key]

    # === estkRs (IPO 핵심) ===
    def fetch_estk_details(self, corp_code: str, bgn_de: str, end_de: str) -> list[dict[str, Any]]:
        """지분증권 신고서 상세 (공모가, 총액, 청약일정, 주간사)."""
        data = self._request("estkRs.json", {
            "corp_code": corp_code,
            "bgn_de": bgn_de,
            "end_de": end_de,
        })
        results: list[dict[str, Any]] = []
        for group in data.get("list", []):
            results.append({
                "rcept_no": group.get("rcept_no"),
                "corp_name": group.get("corp_name"),
                "sbd": group.get("sbd"),           # 청약기일
                "pymd": group.get("pymd"),         # 납입기일
                "slprc": group.get("slprc"),       # 공모가
                "slta": group.get("slta"),         # 총액
                "actnmn": group.get("actnmn"),     # 주간사
                "stkcnt": group.get("stkcnt"),
            })
        return results

    # === list.json discovery (신규상장 IPO 중심) ===
    IPO_POSITIVE_KEYWORDS = [
        "신규상장", "기업공개", "신규공모", "상장공모",
        "증권신고서(지분증권)(신규상장)", "지분증권(신규상장)"
    ]
    IPO_NEGATIVE_KEYWORDS = [
        "유상증자", "제3자배정", "CB", "BW", "전환사채",
        "신주인수권부사채", "주식배당", "무상증자", "자기주식", "합병", "분할"
    ]

    def is_likely_new_ipo(self, report: dict[str, Any]) -> bool:
        """신규상장(IPO) 후보 여부 판단 (report_nm 기반 강력 필터)"""
        report_nm = (report.get("report_nm") or "").lower()
        corp_cls = (report.get("corp_cls") or "").upper()

        # 긍정 키워드 포함
        has_ipo_signal = any(kw.lower() in report_nm for kw in self.IPO_POSITIVE_KEYWORDS)

        # 부정 키워드 제외 (강한 우선순위)
        has_exclude = any(kw.lower() in report_nm for kw in self.IPO_NEGATIVE_KEYWORDS)

        # 비상장(E) + 지분증권 신고는 pre-IPO 강력 후보
        is_pre_ipo = (corp_cls == "E") and ("지분증권" in report_nm or "증권신고서" in report_nm)

        if has_exclude and not has_ipo_signal:
            return False

        return has_ipo_signal or is_pre_ipo

    def discover_new_ipos(self, bgn_de: str, end_de: str, max_pages: int = 5) -> list[dict[str, Any]]:
        """
        실제 신규상장(IPO) 후보 중심으로 필터링된 목록 반환.
        list.json → report_nm 필터링 → estkRs 호출 최소화.
        """
        candidates = []
        for page in range(1, max_pages + 1):
            data = self._request("list.json", {
                "bgn_de": bgn_de,
                "end_de": end_de,
                "pblntf_ty": "C",
                "pblntf_detail_ty": "C001",
                "page_no": str(page),
                "page_count": "100",
            })
            for item in data.get("list", []):
                if self.is_likely_new_ipo(item):
                    candidates.append({
                        "corp_code": item.get("corp_code"),
                        "corp_name": item.get("corp_name"),
                        "corp_cls": item.get("corp_cls"),
                        "report_nm": item.get("report_nm"),
                        "rcept_no": item.get("rcept_no"),
                        "rcept_dt": item.get("rcept_dt"),
                        "stock_code": item.get("stock_code"),
                    })

        # rcept_no 기준 dedup + 최신 순
        seen = set()
        unique = []
        for c in sorted(candidates, key=lambda x: x["rcept_dt"], reverse=True):
            if c["rcept_no"] not in seen:
                seen.add(c["rcept_no"])
                unique.append(c)

        logger.info(f"DART 신규상장 후보 필터링 완료: {len(unique)}건 (원본 {len(candidates)}건)")
        return unique

    def discover_recent_equity_reports(self, bgn_de: str, end_de: str) -> list[dict[str, Any]]:
        """기존 호환용 (광범위 조회)"""
        data = self._request("list.json", {
            "bgn_de": bgn_de,
            "end_de": end_de,
            "pblntf_ty": "C",
            "pblntf_detail_ty": "C001",
            "page_count": "100",
        })
        return data.get("list", [])

    def discover_recent_reports(self, bgn_de: str, end_de: str, detail_ty: str = "C001") -> list[dict[str, Any]]:
        """기존 호환용"""
        return self.discover_recent_equity_reports(bgn_de, end_de)


# === 기존 fetch_latest.py에 붙이기 쉬운 헬퍼 ===
def enrich_with_dart(records: list[dict], client: DartClient, days: int = 90) -> list[dict]:
    """38.co.kr 등 기존 레코드에 DART estkRs 정보 병합."""
    if not records:
        return records

    corp_map = client.load_corp_code_cache()
    today = datetime.now().strftime("%Y%m%d")
    bgn = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")

    for rec in records:
        ticker = rec.get("ticker")
        if not ticker or ticker not in corp_map:
            continue
        corp_code = corp_map[ticker]["corp_code"]
        try:
            details = client.fetch_estk_details(corp_code, bgn, today)
            if details:
                d = details[0]
                rec["dart_offering_price"] = d.get("slprc")
                rec["dart_offering_amount"] = d.get("slta")
                rec["dart_subscription_date"] = d.get("sbd")
                rec["dart_underwriter"] = d.get("actnmn")
                rec["dart_rcept_no"] = d.get("rcept_no")
        except DartApiError as e:
            logger.warning(f"DART estkRs 실패 ({ticker}): {e.status} - {e.message}")
            continue
    return records


if __name__ == "__main__":
    # 간단 테스트 (키 필요)
    client = DartClient()
    print("DART 클라이언트 초기화 성공")
    cache = client.load_corp_code_cache()
    print(f"캐시된 상장사 수: {len(cache)}")