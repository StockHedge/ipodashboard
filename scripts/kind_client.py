#!/usr/bin/env python3
"""
KIND 공모주 일정 클라이언트 — 38.co.kr 모바일 스크래핑 기반.

KIND(kind.krx.co.kr) AJAX POST 엔드포인트는 서버측 CSRF 방어로 접근 불가.
대안: 38커뮤니케이션 모바일(m.38.co.kr/ipo/fund.php) — EUC-KR HTML 파싱.
목록 페이지 → 상세 페이지 순차 요청 (1s 간격 준수).

반환 형태: list[dict]
    {"name", "code", "stage", "demand_start", "demand_end",
     "sub_start", "sub_end", "listing", "band_low", "band_high",
     "price", "underwriter"}
"""
from __future__ import annotations

import logging
import re
import sys
import time
import urllib.request
import urllib.parse
from datetime import date, datetime
from typing import Optional

logger = logging.getLogger("kind_client")
if not logger.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("[kind] %(levelname)s %(message)s"))
    logger.addHandler(h)
logger.setLevel(logging.INFO)

_BASE_HTTP = "http://m.38.co.kr"
_BASE_HTTPS = "https://m.38.co.kr"
LIST_URL = f"{_BASE_HTTPS}/ipo/fund.php"    # HTTPS 우선; 실패 시 _fetch 내부에서 http 폴백
DETAIL_URL = f"{_BASE_HTTPS}/ipo/fund_view.php"
DETAIL_DELAY = 1.0  # 상세 페이지 요청 간격 (초)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": f"{_BASE_HTTPS}/",
    "Accept": "text/html,*/*",
    "Accept-Language": "ko-KR,ko;q=0.9",
}


def _fetch(url: str, timeout: int = 10, retries: int = 1) -> str:
    """EUC-KR HTML을 UTF-8 str로 반환. 실패 시 빈 문자열.

    HTTPS 우선: url 이 https:// 로 시작하면 먼저 시도하고,
    SSL 오류·연결 거부(OSError/SSLError) 발생 시 http:// 로 1회 자동 폴백.
    """
    def _try_url(target: str) -> str | None:
        for attempt in range(retries + 1):
            try:
                req = urllib.request.Request(target, headers=_HEADERS)
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return r.read().decode("euc-kr", errors="replace")
            except Exception as e:
                if attempt < retries:
                    time.sleep(2)
                    continue
                return None  # 호출자에서 판단
        return None

    result = _try_url(url)
    if result is not None:
        return result

    # HTTPS 실패 시 HTTP 폴백 시도 (단, 이미 http 면 재시도 불필요)
    if url.startswith("https://"):
        http_url = url.replace("https://", "http://", 1)
        logger.warning("HTTPS 실패 — HTTP 폴백 시도: %s", http_url)
        result = _try_url(http_url)
        if result is not None:
            return result

    logger.error("fetch 최종 실패 (%s)", url)
    return ""


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s).strip()


def _parse_price(s: str) -> Optional[int]:
    """'21,500원' / '21500' / '-' → int or None."""
    clean = re.sub(r"[^\d]", "", s)
    return int(clean) if clean else None


def _parse_date_str(raw: str) -> Optional[str]:
    """
    '2026.06.17' / '2026/06/17' / '06/17' → 'YYYY-MM-DD' or None.
    연도 없는 경우 현재 연도 적용.
    """
    raw = raw.strip()
    # YYYY.MM.DD 또는 YYYY/MM/DD
    m = re.match(r"(\d{4})[./](\d{1,2})[./](\d{1,2})", raw)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    # MM/DD 또는 MM.DD (연도 없음)
    m = re.match(r"(\d{1,2})[./](\d{1,2})$", raw)
    if m:
        year = date.today().year
        return f"{year}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return None


def _parse_date_range(raw: str) -> tuple[Optional[str], Optional[str]]:
    """
    '2026.06.17 ~ 2026.06.23' 또는 '07/01~07/02' 형식 파싱.
    (start, end) 반환.
    """
    raw = raw.strip()
    # 범위 구분자는 '~' 만 사용. 하이픈은 날짜 내부 구분자(2026-06-17)이므로 분할 금지.
    parts = raw.split("~", 1)
    start = _parse_date_str(parts[0].strip()) if parts else None
    end = _parse_date_str(parts[1].strip()) if len(parts) > 1 else start
    return start, end


def _determine_stage(
    today: date,
    demand_start: Optional[str],
    demand_end: Optional[str],
    sub_start: Optional[str],
    sub_end: Optional[str],
    listing: Optional[str],
) -> str:
    """오늘 날짜 기준으로 공모 단계 판정."""

    def to_date(s: Optional[str]) -> Optional[date]:
        if not s:
            return None
        try:
            return datetime.strptime(s, "%Y-%m-%d").date()
        except ValueError:
            return None

    ds = to_date(demand_start)
    de = to_date(demand_end)
    ss = to_date(sub_start)
    se = to_date(sub_end)
    ld = to_date(listing)

    if ld and today >= ld:
        return "상장"
    if se and today > se:
        # 청약 종료 후 상장일 이전 = 상장대기
        return "상장대기"
    if ss and ss <= today <= (se or ss):
        return "청약"
    if ds and de and ds <= today <= de:
        return "수요예측"
    if ss and today < ss:
        # 청약 시작 전: 수요예측 완료 여부에 따라 구분
        if de and today > de:
            return "수요예측"  # 수요예측 완료, 청약 준비 중 — 소비자 관점에서 가장 가까운 단계
        return "수요예측" if ds else "청약"
    if ld:
        return "상장대기"
    return "심사"


def _parse_detail(html: str, name: str) -> dict:
    """상세 페이지 HTML에서 수요예측일·청약일·상장일·공모가·주간사 추출."""
    result: dict = {
        "demand_start": None,
        "demand_end": None,
        "sub_start": None,
        "sub_end": None,
        "listing": None,
        "band_low": None,
        "band_high": None,
        "price": None,
        "underwriter": None,
        "code": None,
    }

    def _td_after(label: str) -> str:
        """테이블에서 레이블 다음 값 셀 텍스트."""
        m = re.search(
            re.escape(label) + r"[^<]*</t[dh]>\s*<t[dh][^>]*>(.*?)</t[dh]>",
            html,
            re.DOTALL | re.IGNORECASE,
        )
        return _strip_tags(m.group(1)) if m else ""

    def _full_row(label: str) -> str:
        """레이블과 값이 같은 td 안에 있는 행 전체 텍스트."""
        m = re.search(
            re.escape(label) + r"(.*?)</tr>",
            html,
            re.DOTALL | re.IGNORECASE,
        )
        return _strip_tags(m.group(0)) if m else ""

    # 종목코드
    code_raw = _td_after("종목코드")
    if code_raw and code_raw.isdigit():
        result["code"] = code_raw.zfill(6)

    # 희망공모가액 "17,800~20,700원"
    band_raw = _td_after("희망공모가액")
    m_band = re.search(r"([\d,]+)\s*~\s*([\d,]+)", band_raw)
    if m_band:
        result["band_low"] = _parse_price(m_band.group(1))
        result["band_high"] = _parse_price(m_band.group(2))

    # 확정공모가
    price_raw = _td_after("확정공모가")
    p = _parse_price(price_raw)
    result["price"] = p if p else None

    # 주간사 (첫 번째 td만 — 복수 주간사는 세미콜론 구분)
    uw_raw = _td_after("주간사")
    if uw_raw:
        # "KB증권|주식수:..." 같은 오염 제거
        uw_clean = re.split(r"[|:]", uw_raw)[0].strip()
        result["underwriter"] = uw_clean if uw_clean else None

    # 수요예측일 "2026.06.17 ~ 2026.06.23"
    demand_row = _full_row("수요예측일")
    dm = re.search(r"(\d{4}[./]\d{1,2}[./]\d{1,2})\s*~\s*(\d{4}[./]\d{1,2}[./]\d{1,2})", demand_row)
    if dm:
        result["demand_start"] = _parse_date_str(dm.group(1))
        result["demand_end"] = _parse_date_str(dm.group(2))

    # 공모청약일
    sub_row = _full_row("공모청약일")
    sm = re.search(r"(\d{4}[./]\d{1,2}[./]\d{1,2})\s*~\s*(\d{4}[./]\d{1,2}[./]\d{1,2})", sub_row)
    if sm:
        result["sub_start"] = _parse_date_str(sm.group(1))
        result["sub_end"] = _parse_date_str(sm.group(2))

    # 상장일
    listing_row = _full_row("상장일")
    lm = re.search(r"(\d{4}[./]\d{1,2}[./]\d{1,2})", listing_row)
    if lm:
        result["listing"] = _parse_date_str(lm.group(1))

    return result


def _parse_list_row(tds: list[str]) -> Optional[dict]:
    """목록 행 td 리스트 → 기본 dict. None이면 스킵."""
    if len(tds) < 4:
        return None
    name = _strip_tags(tds[0])
    if not name:
        return None

    # 링크에서 no= 파라미터 추출
    no_m = re.search(r"no=(\d+)", tds[0])
    detail_no = no_m.group(1) if no_m else None

    # 공모일정 "07/01~07/02"
    sched_raw = _strip_tags(tds[1]).replace("\n", "").replace(" ", "")
    sub_start, sub_end = _parse_date_range(sched_raw)

    # 확정공모가 / 희망공모가 "17,800~20,700"
    price_raw = _strip_tags(tds[2])
    band_raw = _strip_tags(tds[3])

    price = _parse_price(price_raw)
    m_band = re.search(r"([\d,]+)\s*~\s*([\d,]+)", band_raw)
    band_low = _parse_price(m_band.group(1)) if m_band else None
    band_high = _parse_price(m_band.group(2)) if m_band else None

    underwriter = _strip_tags(tds[5]) if len(tds) > 5 else None

    return {
        "name": name,
        "code": None,
        "detail_no": detail_no,
        "sub_start": sub_start,
        "sub_end": sub_end,
        "demand_start": None,
        "demand_end": None,
        "listing": None,
        "band_low": band_low,
        "band_high": band_high,
        "price": price,
        "underwriter": underwriter,
    }


def get_ipo_schedule(fetch_detail: bool = True, max_detail: int = 20, budget_s: float = 45.0) -> list[dict]:
    """
    38.co.kr 모바일 공모주 청약일정 목록 반환.
    fetch_detail=True 이면 상세 페이지에서 수요예측일·상장일 보완.
    실패 시 빈 리스트 반환 (예외 전파 없음).
    """
    html = _fetch(LIST_URL)
    if not html:
        logger.error("목록 페이지 로드 실패 — 빈 리스트 반환")
        return []

    # <tr> 단위 파싱
    rows_html = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.DOTALL | re.IGNORECASE)
    items: list[dict] = []
    today = date.today()
    start = time.monotonic()       # 상세 조회 총 예산 추적 (H-1: 230s 폭주 방지)
    detail_fetched = 0
    capped = False

    for row_html in rows_html:
        tds = re.findall(r"<td[^>]*>(.*?)</td>", row_html, re.DOTALL | re.IGNORECASE)
        if not tds:
            continue
        item = _parse_list_row(tds)
        if not item:
            continue

        if fetch_detail and item.get("detail_no"):
            # 상세 조회는 건당 1s + 네트워크 → 항목 수×시간 폭주 방지: 건수·총시간 예산 적용
            if detail_fetched < max_detail and (time.monotonic() - start) < budget_s:
                detail_url = f"{DETAIL_URL}?no={item['detail_no']}&page=1"
                time.sleep(DETAIL_DELAY)
                detail_html = _fetch(detail_url)
                if detail_html:
                    detail = _parse_detail(detail_html, item["name"])
                    for k, v in detail.items():
                        if v is not None:
                            item[k] = v
                detail_fetched += 1
            else:
                capped = True  # 예산 초과 — 나머지는 목록 데이터로 stage 판정

        item["stage"] = _determine_stage(
            today,
            item.get("demand_start"),
            item.get("demand_end"),
            item.get("sub_start"),
            item.get("sub_end"),
            item.get("listing"),
        )
        # detail_no는 내부 필드 — 노출 불필요
        item.pop("detail_no", None)
        items.append(item)

    if capped:
        logger.warning("상세 조회 예산 초과 — %d건만 상세 보강, 나머지는 목록 데이터 기준", max_detail)
    logger.info("공모주 일정 %d건 수집 완료 (상세 %d건)", len(items), detail_fetched)
    return items


# ─── CLI 자가 테스트 ───────────────────────────────────────────────
if __name__ == "__main__":
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    import json

    # --- 단위 테스트 ---
    # _parse_date_str
    assert _parse_date_str("2026.06.17") == "2026-06-17", "_parse_date_str YYYY.MM.DD"
    assert _parse_date_str("2026/06/17") == "2026-06-17", "_parse_date_str YYYY/MM/DD"
    assert _parse_date_str("06/17") == f"{date.today().year}-06-17", "_parse_date_str MM/DD"
    assert _parse_date_str("") is None, "_parse_date_str empty"

    # _determine_stage — 오늘 기준 2026-06-12
    d = date(2026, 6, 12)
    # 수요예측 중 (오늘이 수요예측 기간 내)
    assert _determine_stage(d, "2026-06-10", "2026-06-15", "2026-07-01", "2026-07-02", "2026-07-10") == "수요예측", "수요예측 중"
    # 수요예측 완료, 청약 전 (오늘이 수요예측 종료 후 청약 시작 전)
    assert _determine_stage(d, "2026-06-01", "2026-06-05", "2026-07-01", "2026-07-02", "2026-07-10") == "수요예측", "수요예측 완료(청약준비)"
    # 청약 종료, 상장 전 (상장대기)
    assert _determine_stage(d, "2026-06-01", "2026-06-05", "2026-06-10", "2026-06-11", "2026-07-10") == "상장대기", "상장대기"
    # 상장일 당일 이후
    assert _determine_stage(d, "2026-06-01", "2026-06-05", "2026-06-10", "2026-06-11", "2026-06-12") == "상장", "상장"
    # 청약 당일
    assert _determine_stage(d, None, None, "2026-06-12", "2026-06-13", None) == "청약", "청약 중"
    print("단위 테스트 모두 통과")

    # --- 실데이터 수집 ---
    print("\n공모주 일정 조회 중... (상세 페이지 포함, 시간 소요)")
    result = get_ipo_schedule(fetch_detail=True)
    print(f"\n수집 건수: {len(result)}")
    for item in result[:5]:
        print(json.dumps(item, ensure_ascii=False))
