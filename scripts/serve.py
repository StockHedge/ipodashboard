#!/usr/bin/env python3
"""
Local dev server with /api/refresh-* endpoints.

표준 라이브러리만 사용 (외부 의존성 0). `python -m http.server` 대신 이 스크립트로 실행하면
브라우저의 "새로고침" 버튼이 extract_ipo_data.py / fetch_latest.py 를 subprocess 로 호출.

사용법 (ipo-dashboard 디렉토리 기준):
    python scripts/serve.py 8000
    # 브라우저: http://localhost:8000

환경변수:
    IPO_XLSX_PATH  — 사용자 XLSX 경로 (기본: ~/Downloads/새 계정 프로그램 시트 (1).xlsx)
    DART_API_KEY   — DART 보강 시 필수 (.env 자동 로드도 가능)

엔드포인트 (모두 POST):
    /api/refresh-xlsx  — XLSX → data/ipo-recent.json + .js 재추출
    /api/refresh-dart  — DART estkRs 보강 (XLSX 결과에 append)
    /api/refresh-all   — XLSX → DART 순차 실행
"""
from __future__ import annotations
import http.server
import json
import os
import re
import socketserver
import subprocess
import sys
import time
import threading
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from xml.etree import ElementTree as ET
import urllib.request

ROOT = Path(__file__).parent.parent.resolve()
SCRIPTS = ROOT / "scripts"
DATA = ROOT / "data"

# .env 자동 로드 (선택적)
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

# KIS import (실 사용 lazy init 은 아래 get_kis()). 죽은 _get_kis 제거.
sys.path.insert(0, str(SCRIPTS))


# ---- KIS client (lazy import, lazy init) ----
_kis_client = None
_kis_lock = threading.Lock()
_kis_error: str | None = None

# ============================================================
# KRX 종목 마스터 (ticker → name) — fdr 일별 캐시
# KIS 모의투자는 hts_kor_isnm 이 KOSPI200/null 로 잘못 옴 → fdr 로 정확한 한글명 주입
# ============================================================
_krx_names: dict | None = None
_krx_names_ts: float = 0.0
_krx_lock = threading.Lock()
_krx_client_lock = threading.Lock()

# ============================================================
# KRX OpenAPI 클라이언트 lazy init — 시장 투자자별 매매동향용
# ============================================================
_krx_client = None
_krx_error: str | None = None
def get_krx():
    global _krx_client, _krx_error
    if _krx_client is not None or _krx_error is not None:
        return _krx_client
    with _krx_client_lock:
        if _krx_client is not None or _krx_error is not None:
            return _krx_client
        try:
            from krx_client import KrxClient
            _krx_client = KrxClient()
        except Exception as e:
            _krx_error = f"{e.__class__.__name__}: {e}"
            sys.stderr.write(f"[serve] KRX 초기화 실패 (시장 투자자 동향 비활성): {_krx_error}\n")
    return _krx_client


# ============================================================
# 네이버 금융 클라이언트 lazy init — 실시간 지수 + 투자자별 매매 (인증 불필요)
# KRX 키 미활성 / KIS 모의투자 부정확 우회. 가장 안정적인 시장 데이터 소스.
# ============================================================
_naver_client = None
_naver_lock = threading.Lock()
def get_naver():
    global _naver_client
    if _naver_client is not None:
        return _naver_client
    with _naver_lock:  # ThreadingMixIn race 방지 (double-checked locking)
        if _naver_client is not None:
            return _naver_client
        try:
            from naver_client import NaverClient
            _naver_client = NaverClient()
        except Exception as e:
            sys.stderr.write(f"[serve] 네이버 클라이언트 초기화 실패: {e}\n")
    return _naver_client


def get_krx_name(ticker: str) -> str | None:
    global _krx_names, _krx_names_ts
    now = time.time()
    if _krx_names is None or (now - _krx_names_ts) > 86400:
        with _krx_lock:
            if _krx_names is None or (now - _krx_names_ts) > 86400:
                try:
                    import FinanceDataReader as fdr
                    df = fdr.StockListing("KRX")[["Code", "Name"]]
                    _krx_names = dict(zip(df["Code"].astype(str), df["Name"].astype(str)))
                    _krx_names_ts = now
                    sys.stderr.write(f"[serve] KRX 종목 마스터 캐시 갱신: {len(_krx_names)}건\n")
                except Exception as e:
                    sys.stderr.write(f"[serve] KRX 마스터 로드 실패 (fdr 필요): {e}\n")
                    _krx_names = {}
    return _krx_names.get(str(ticker).zfill(6))

def get_kis():
    """KIS 클라이언트 lazy init. .env / 토큰 / 모드 변경 시에도 안전."""
    global _kis_client, _kis_error
    if _kis_client is not None or _kis_error is not None:
        return _kis_client
    with _kis_lock:
        if _kis_client is not None or _kis_error is not None:
            return _kis_client
        try:
            from kis_client import KisClient  # type: ignore
            _kis_client = KisClient()
            return _kis_client
        except Exception as e:
            _kis_error = f"{e.__class__.__name__}: {e}"
            sys.stderr.write(f"[serve] KIS 초기화 실패 (가격 API 비활성): {_kis_error}\n")
            return None

# 가격 캐시 — (key) → (timestamp, payload). TTL 60초 (장 마감 후엔 더 길게).
_price_cache: dict[str, tuple[float, dict]] = {}
_price_cache_lock = threading.Lock()
PRICE_TTL = 60  # seconds (default — KIS 실시간)
KRX_TTL = 900   # seconds (15분 — KRX 일별 데이터)

def _cache_get(key: str):
    with _price_cache_lock:
        e = _price_cache.get(key)
        if not e: return None
        # entry: (timestamp, payload, ttl)
        ttl = e[2] if len(e) > 2 else PRICE_TTL
        if time.time() - e[0] > ttl: return None
        return e[1]

def _cache_set(key: str, payload: dict, ttl: int = PRICE_TTL):
    """ttl 키워드 인자 지원 (KRX 같은 일별 데이터는 더 긴 TTL 필요)."""
    with _price_cache_lock:
        _price_cache[key] = (time.time(), payload, ttl)


# ============================================================
# 뉴스/RSS 헬퍼
# ============================================================

def _clean_html_text(s: str) -> str:
    """HTML 태그·HTML 엔티티 제거."""
    s = re.sub(r"<[^>]+>", "", s)
    return s.replace("&quot;", '"').replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&nbsp;", " ").strip()


def _rfc2822_to_iso(raw: str) -> str:
    """RFC 2822 날짜 → ISO 8601."""
    if not raw:
        return ""
    try:
        dt = parsedate_to_datetime(raw)
        return dt.isoformat()
    except Exception:
        return raw


_DART_IPO_KEYWORDS = ["증권신고서", "투자설명서", "정정신고서"]


def _fetch_dart_ipo_news(days: int = 7) -> list[dict]:
    """
    DART list.json — IPO 관련 공시(증권신고서/투자설명서/정정) 필터 후 반환.
    DART_API_KEY 없으면 빈 리스트.
    """
    api_key = os.environ.get("DART_API_KEY", "")
    if not api_key:
        sys.stderr.write("[serve] DART_API_KEY 미설정 — dart 뉴스 스킵\n")
        return []

    from datetime import timedelta
    end_dt = datetime.now()
    bgn_dt = end_dt - timedelta(days=days)
    bgn_de = bgn_dt.strftime("%Y%m%d")
    end_de = end_dt.strftime("%Y%m%d")

    url = (
        f"https://opendart.fss.or.kr/api/list.json"
        f"?crtfc_key={api_key}&bgn_de={bgn_de}&end_de={end_de}"
        f"&pblntf_ty=C&pblntf_detail_ty=C001&page_count=100"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ipo-dashboard/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        sys.stderr.write(f"[serve] DART list.json 요청 실패: {e}\n")
        return []

    if data.get("status") != "000":
        sys.stderr.write(f"[serve] DART API 오류: {data.get('status')} {data.get('message')}\n")
        return []

    items = []
    for it in data.get("list", []):
        report_nm = it.get("report_nm", "")
        if not any(kw in report_nm for kw in _DART_IPO_KEYWORDS):
            continue
        rcept_no = it.get("rcept_no", "")
        items.append({
            "corp_name": it.get("corp_name", ""),
            "report_nm": report_nm,
            "rcept_no": rcept_no,
            "rcept_dt": it.get("rcept_dt", ""),
            "link": f"https://dart.fss.or.kr/dsaf001/main.do?rcptNo={rcept_no}",
            "source": "dart",
        })
    return items


_HK_RSS_URLS = [
    "https://www.hankyung.com/feed/finance",
    "https://www.hankyung.com/rss/financial-investment",
]


def _fetch_rss_news() -> list[dict]:
    """한국경제 RSS 파싱. 작동하는 첫 URL 사용, 실패 시 빈 리스트."""
    for rss_url in _HK_RSS_URLS:
        try:
            req = urllib.request.Request(
                rss_url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; ipo-dashboard/1.0)"},
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                raw = r.read()
        except Exception as e:
            sys.stderr.write(f"[serve] RSS fetch 실패 ({rss_url}): {e}\n")
            continue

        try:
            root = ET.fromstring(raw)
        except ET.ParseError as e:
            sys.stderr.write(f"[serve] RSS XML 파싱 실패 ({rss_url}): {e}\n")
            continue

        ns = {"content": "http://purl.org/rss/1.0/modules/content/"}
        items = []
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub_raw = (item.findtext("pubDate") or "").strip()
            items.append({
                "title": title,
                "link": link,
                "pubDate": _rfc2822_to_iso(pub_raw),
                "source": "한국경제",
            })
        sys.stderr.write(f"[serve] RSS {rss_url} — {len(items)}건\n")
        return items

    sys.stderr.write("[serve] 모든 한국경제 RSS URL 실패\n")
    return []


def _safe_xlsx_path(raw: str) -> Path | None:
    """XLSX 경로 traversal 방어 — 허용된 디렉토리(~/Downloads, ~/Desktop, ROOT/data) 하위만 허용."""
    try:
        p = Path(raw).expanduser().resolve()
        allowed_roots = [
            Path.home() / "Downloads",
            Path.home() / "Desktop",
            ROOT / "data",
            ROOT,
        ]
        for root in allowed_roots:
            try:
                root_resolved = root.resolve()
                if p.is_relative_to(root_resolved):
                    return p
            except (OSError, ValueError):
                continue
        return None
    except (OSError, ValueError):
        return None


def default_xlsx() -> str:
    explicit = os.environ.get("IPO_XLSX_PATH")
    if explicit:
        return explicit
    candidate = Path.home() / "Downloads" / "새 계정 프로그램 시트 (1).xlsx"
    return str(candidate)


def run_cmd(cmd: list[str], timeout: int = 300) -> dict:
    """
    subprocess 실행 후 표준 응답 dict 반환.

    중요: Windows + Python 의 기본 stdout 은 cp949. 한글 print 시 subprocess 가
    UnicodeEncodeError 로 crash → returncode != 0 으로 표시되는 흔한 함정.
    PYTHONIOENCODING=utf-8 + PYTHONUTF8=1 로 자식 프로세스의 stdout/stderr 를
    UTF-8 로 강제하여 한글 출력 안전.
    """
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(ROOT),
            timeout=timeout,
            env=env,
        )
        ok = result.returncode == 0
        payload = {
            "ok": ok,
            "returncode": result.returncode,
            "stdout": (result.stdout or "")[-4000:],
            "stderr": (result.stderr or "")[-4000:],
            "cmd": cmd,
        }
        # 서버 로그에 결과 한 줄 요약 (디버깅 편의)
        tail = (result.stdout or "").strip().splitlines()[-1:] or [""]
        sys.stderr.write(f"[serve] cmd done rc={result.returncode} tail={tail[0][:160]!r}\n")
        if not ok:
            stderr_tail = (result.stderr or "").strip().splitlines()[-3:]
            for line in stderr_tail:
                sys.stderr.write(f"[serve]   stderr> {line[:160]}\n")
        return payload
    except subprocess.TimeoutExpired as e:
        return {"ok": False, "error": "timeout", "timeout": timeout, "cmd": cmd, "detail": str(e)}
    except Exception as e:
        return {"ok": False, "error": e.__class__.__name__, "detail": str(e), "cmd": cmd}


class Handler(http.server.SimpleHTTPRequestHandler):
    # 정적 파일 root 를 프로젝트 루트로 고정
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self):
        # 데이터 파일은 캐시 무력화 (refresh 후 즉시 반영)
        if self.path.startswith("/data/"):
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        super().end_headers()

    def _send_json(self, payload: dict, status: int = 200):
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # CORS — 로컬 dev 전용. 외부 노출 시 환경변수로 제한 권장
        self.send_header("Access-Control-Allow-Origin", os.environ.get("ALLOWED_ORIGIN", "http://localhost:8000"))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        # CORS — 로컬 dev 전용. 외부 노출 시 환경변수로 제한 권장
        self.send_header("Access-Control-Allow-Origin", os.environ.get("ALLOWED_ORIGIN", "http://localhost:8000"))
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/ping":
            kis = get_kis()
            self._send_json({
                "ok": True,
                "service": "ipo-dashboard-serve",
                "root": str(ROOT),
                "endpoints": [
                    "/api/ping (GET)",
                    "/api/price?ticker=NNNNNN (GET, 캐시 60s)",
                    "/api/prices?tickers=N,N,N (GET, 배치 최대 50)",
                    "/api/market (GET, KOSPI+KOSDAQ)",
                    "/api/refresh-xlsx (POST)", "/api/refresh-dart (POST)", "/api/refresh-all (POST)",
                ],
                "dart_loaded": bool(os.environ.get("DART_API_KEY")),
                "kis_loaded": kis is not None,
                "kis_mode": "paper" if (kis and kis.use_paper) else ("real" if kis else None),
                "kis_error": _kis_error,
                "xlsx_path": default_xlsx(),
                "xlsx_exists": Path(default_xlsx()).exists(),
            })
            return
        if path == "/api/price":
            self._send_json(self._handle_price(parse_qs(parsed.query)))
            return
        if path == "/api/prices":
            self._send_json(self._handle_prices(parse_qs(parsed.query)))
            return
        if path == "/api/market":
            self._send_json(self._handle_market())
            return
        if path == "/api/daily":
            self._send_json(self._handle_daily(parse_qs(parsed.query)))
            return
        if path == "/api/index-daily":
            # 지수 일별 OHLCV (KOSPI=0001, KOSDAQ=1001) — sparkline / MA200 용
            qs = parse_qs(parsed.query)
            code = (qs.get("code") or ["0001"])[0]
            try:
                count = max(5, min(int((qs.get("count") or ["120"])[0]), 250))
            except (ValueError, TypeError): count = 120
            cache_key = f"idx-daily:{code}:{count}"
            cached = _cache_get(cache_key)
            if cached:
                self._send_json({"ok": True, "cached": True, "data": cached})
                return
            kis = get_kis()
            if not kis:
                self._send_json({"ok": False, "error": "KIS_NOT_CONFIGURED"}, status=503)
                return
            try:
                rows = kis.get_index_daily(code, count=count)
                payload = {"code": code, "count": len(rows), "rows": rows}
                _cache_set(cache_key, payload)
                self._send_json({"ok": True, "cached": False, "data": payload})
            except Exception as e:
                self._send_json({"ok": False, "error": e.__class__.__name__, "detail": str(e)}, status=500)
            return
        if path == "/api/krx/investor":
            # 시장 투자자별 매매동향 (KOSPI + KOSDAQ)
            qs = parse_qs(parsed.query)
            date = (qs.get("date") or [""])[0].strip() or None
            cache_key = f"krx:investor:{date or 'auto'}"
            cached = _cache_get(cache_key)
            if cached:
                self._send_json({"ok": True, "cached": True, "data": cached})
                return
            krx = get_krx()
            if not krx:
                self._send_json({"ok": False, "error": "KRX_NOT_CONFIGURED", "detail": _krx_error,
                                 "hint": ".env 에 KRX_API_KEY 추가 후 serve.py 재시작"}, status=503)
                return
            result = {}
            errors = {}
            for mk in ("KOSPI", "KOSDAQ"):
                try:
                    result[mk] = krx.get_market_investor(date, mk)
                except Exception as e:
                    errors[mk] = f"{e.__class__.__name__}: {e}"
            payload = {"date": date, "indices": result, "errors": errors}
            _cache_set(cache_key, payload, ttl=KRX_TTL)
            self._send_json({"ok": True, "cached": False, "data": payload})
            return
        if path == "/api/krx/index":
            # KRX 시리즈 일별시세 (확정 endpoint krx_dd_trd) — KOSPI/KOSDAQ 지수
            qs = parse_qs(parsed.query)
            date = (qs.get("date") or [""])[0].strip() or None
            cache_key = f"krx:index:{date or 'auto'}"
            cached = _cache_get(cache_key)
            if cached:
                self._send_json({"ok": True, "cached": True, "data": cached})
                return
            krx = get_krx()
            if not krx:
                self._send_json({"ok": False, "error": "KRX_NOT_CONFIGURED", "detail": _krx_error}, status=503)
                return
            try:
                data = krx.get_index_series(date)
                _cache_set(cache_key, data, ttl=KRX_TTL)
                self._send_json({"ok": True, "cached": False, "data": data})
            except Exception as e:
                self._send_json({"ok": False, "error": e.__class__.__name__, "detail": str(e)}, status=500)
            return
        if path == "/api/stock-info":
            # ticker → 종목명 일괄 조회 (fdr 캐시 사용, KIS API 호출 없이 빠름)
            qs = parse_qs(parsed.query)
            raw = (qs.get("ticker") or qs.get("tickers") or [""])[0]
            tickers = [t.strip().zfill(6) for t in raw.split(",") if t.strip()]
            tickers = [t for t in tickers if t.isdigit() and len(t) == 6][:100]
            names = {t: get_krx_name(t) for t in tickers}
            self._send_json({"ok": True, "names": names})
            return
        if path == "/api/ipo/schedule":
            self._send_json(self._handle_ipo_schedule(parse_qs(parsed.query)))
            return
        if path == "/api/news/search":
            self._send_json(self._handle_news_search(parse_qs(parsed.query)))
            return
        if path == "/api/news/dart":
            self._send_json(self._handle_news_dart(parse_qs(parsed.query)))
            return
        if path == "/api/news/rss":
            self._send_json(self._handle_news_rss())
            return
        super().do_GET()

    # ---------- price/market endpoints ----------
    # 응답 형식 — 클라이언트(index.html)와 통일:
    #   /api/price : { ok, results:[{ticker,name,price,value,change,change_pct,open,high,low,...}], errors:[{ticker,error}] }
    #     ?ticker=NNNNNN 단일 / ?tickers=N,N,N 배치 (둘 다 results 배열로 반환)
    #   /api/market: { ok, indices:{ KOSPI:{...}, KOSDAQ:{...} }, errors:{...} }
    def _quote_to_dict(self, p: dict) -> dict:
        """KisClient 응답을 클라이언트 호환 형식으로 변환 (change_pct / value 별칭).
        KIS 모의투자는 name 이 잘못 옴 → fdr 종목 마스터로 정확한 한글명 주입."""
        kis_name = p.get("name")
        ticker = p.get("ticker")
        # KIS 모의투자 잘못된 name (KOSPI200, null, 빈 문자열) 감지 → fdr 우선
        if not kis_name or kis_name in ("KOSPI200", "KOSDAQ", "KOSPI", ""):
            fdr_name = get_krx_name(ticker)
            if fdr_name:
                kis_name = fdr_name
        return {
            "ticker": ticker,
            "name": kis_name,
            "price": p.get("price"),
            "value": p.get("price"),  # alias for index-style consumers
            "change": p.get("change"),
            "change_pct": p.get("changeRate"),
            "open": p.get("open"), "high": p.get("high"), "low": p.get("low"),
            "volume": p.get("volume"), "marketCap": p.get("marketCap"),
            "per": p.get("per"), "pbr": p.get("pbr"),
            "high52w": p.get("high52w"), "low52w": p.get("low52w"),
        }
    def _index_to_dict(self, p: dict) -> dict:
        return {
            "code": p.get("code"), "name": p.get("name"),
            "value": p.get("price"), "price": p.get("price"),
            "change": p.get("change"), "change_pct": p.get("changeRate"),
            "open": p.get("open"), "high": p.get("high"), "low": p.get("low"),
            "volume": p.get("volume"),
        }

    def _handle_price(self, qs):
        # single (ticker) 또는 batch (tickers) 모두 받아 results 배열로 통일
        raw = (qs.get("tickers") or qs.get("ticker") or [""])[0]
        tickers = [t.strip().zfill(6) for t in raw.split(",") if t.strip()]
        tickers = [t for t in tickers if t.isdigit() and len(t) == 6][:50]
        if not tickers:
            return {"ok": False, "error": "ticker / tickers 6자리 숫자 필수"}
        kis = get_kis()
        if not kis:
            return {"ok": False, "error": "KIS_NOT_CONFIGURED",
                    "hint": ".env 에 KIS_APP_KEY/KIS_APP_SECRET 설정 + serve.py 재시작",
                    "detail": _kis_error}
        results, errors = [], []
        sleep_iv = 0.5 if kis.use_paper else 0.06
        for t in tickers:
            cached = _cache_get(f"price:{t}")
            if cached:
                results.append(self._quote_to_dict(cached))
                continue
            try:
                p = kis.get_current_price(t)
                _cache_set(f"price:{t}", p)
                results.append(self._quote_to_dict(p))
                time.sleep(sleep_iv)
            except Exception as e:
                errors.append({"ticker": t, "error": f"{e.__class__.__name__}: {e}"})
        return {"ok": True, "results": results, "errors": errors}

    def _handle_prices(self, qs):
        # /api/prices 는 /api/price 와 동일 동작 (alias for backward compat)
        return self._handle_price(qs)

    def _handle_daily(self, qs):
        """종목 일별 OHLCV — post-IPO drift 차트용. 캐시 5분 (장 마감 후 더 길게 해도 OK)."""
        ticker = (qs.get("ticker") or [""])[0].strip().zfill(6) if qs.get("ticker") else ""
        if not ticker.isdigit() or len(ticker) != 6:
            return {"ok": False, "error": "ticker 6자리 숫자 필수"}
        try:
            days = int((qs.get("days") or ["60"])[0])
            days = max(5, min(days, 250))
        except (ValueError, TypeError):
            days = 60
        end_date = (qs.get("end") or [""])[0].strip()  # YYYYMMDD 옵션 (락업해제일 등 과거 기준)
        cache_key = f"daily:{ticker}:{days}:{end_date or 'now'}"
        cached = _cache_get(cache_key)
        if cached:
            return {"ok": True, "cached": True, "data": cached}
        kis = get_kis()
        if not kis:
            return {"ok": False, "error": "KIS_NOT_CONFIGURED", "detail": _kis_error}
        try:
            rows = kis.get_daily(ticker, days=days, end_date=(end_date or None))
            payload = {"ticker": ticker, "days": days, "end_date": end_date or None, "rows": rows}
            _cache_set(cache_key, payload)
            return {"ok": True, "cached": False, "data": payload}
        except Exception as e:
            return {"ok": False, "error": e.__class__.__name__, "detail": str(e)}

    def _handle_market(self):
        """지수 + 투자자별 매매 — 네이버 우선(실시간·정확), KIS fallback."""
        cached = _cache_get("market")
        if cached:
            return {"ok": True, "cached": True, "indices": cached.get("indices", {}), "errors": {}}
        indices, errors = {}, {}
        # 1순위: 네이버 (실시간 지수 + 투자자별 순매수, 인증 불필요)
        nv = get_naver()
        if nv:
            for code in ("KOSPI", "KOSDAQ"):
                try:
                    idx = nv.get_index(code)
                    entry = {
                        "code": code, "name": idx.get("name"),
                        "value": idx.get("value"), "price": idx.get("value"),
                        "change": idx.get("change"), "change_pct": idx.get("change_pct"),
                        "market_status": idx.get("market_status"),
                        "traded_at": idx.get("traded_at"),
                        "source": "naver",
                    }
                    try:
                        inv = nv.get_investor_trend(code)
                        entry["investor"] = {
                            "date": inv.get("date"),
                            "personal": inv.get("personal"),
                            "foreign": inv.get("foreign"),
                            "institutional": inv.get("institutional"),
                            "unit": inv.get("unit"),
                        }
                    except Exception as inv_err:
                        sys.stderr.write(f"[serve] 네이버 투자자 동향 실패 ({code}): {inv_err.__class__.__name__}: {inv_err}\n")
                    indices[code] = entry
                except Exception as e:
                    errors[code] = f"naver: {e.__class__.__name__}: {e}"
        # 2순위: 네이버 실패 시 KIS fallback (모의투자 — 부정확 가능)
        if not indices:
            kis = get_kis()
            if kis:
                for code, alias in (("0001", "KOSPI"), ("1001", "KOSDAQ")):
                    try:
                        idx = kis.get_index_price(code)
                        indices[alias] = {**self._index_to_dict(idx), "source": "kis"}
                        time.sleep(0.5 if kis.use_paper else 0.06)
                    except Exception as e:
                        errors[alias] = f"kis: {e.__class__.__name__}: {e}"
        if not indices:
            return {"ok": False, "error": "NO_MARKET_SOURCE", "detail": "네이버/KIS 모두 실패", "errors": errors}
        payload = {"indices": indices, "ts": time.time()}
        _cache_set("market", payload, ttl=PRICE_TTL)
        return {"ok": True, "cached": False, "indices": indices, "errors": errors}

    def _handle_ipo_schedule(self, qs: dict) -> dict:
        """GET /api/ipo/schedule — TTL 6시간, ?refresh=1 캐시 무시."""
        cache_key = "ipo:schedule"
        force = (qs.get("refresh") or [""])[0] == "1"
        if not force:
            cached = _cache_get(cache_key)
            if cached:
                return {"updated": cached.get("updated"), "source": "KIND", "items": cached.get("items", [])}
        try:
            from kind_client import get_ipo_schedule
            items = get_ipo_schedule(fetch_detail=True)
        except Exception as e:
            sys.stderr.write(f"[serve] KIND 클라이언트 오류: {e}\n")
            return {"error": "kind_fetch_failed", "updated": None, "source": "KIND", "items": []}
        updated = datetime.now(timezone.utc).isoformat()
        payload = {"updated": updated, "items": items}
        _cache_set(cache_key, payload, ttl=21600)
        return {"updated": updated, "source": "KIND", "items": items}

    def _handle_news_search(self, qs: dict) -> dict:
        """GET /api/news/search?q=종목명&display=10 — TTL 600."""
        q = (qs.get("q") or [""])[0].strip()
        if not q:
            return {"error": "q_param_required", "items": []}
        try:
            display = max(1, min(int((qs.get("display") or ["10"])[0]), 100))
        except (ValueError, TypeError):
            display = 10
        cache_key = f"news:search:{q}:{display}"
        cached = _cache_get(cache_key)
        if cached:
            return {"items": cached}
        nv = get_naver()
        if not nv:
            return {"error": "naver_client_init_failed", "items": []}
        try:
            items = nv.search_news(q, display=display)
        except Exception as e:
            code = getattr(e, "code", None)  # NaverConfigError 는 식별 코드 보유
            if code:
                # 설정 오류(키 미설정/인증 실패)는 캐시하지 않고 프론트에 코드 전달
                return {"error": code, "items": []}
            sys.stderr.write(f"[serve] 네이버 뉴스 검색 오류: {e}\n")
            return {"error": "naver_search_failed", "items": []}
        _cache_set(cache_key, items, ttl=600)
        return {"items": items}

    def _handle_news_dart(self, qs: dict) -> dict:
        """GET /api/news/dart?days=7 — TTL 1800."""
        try:
            days = max(1, min(int((qs.get("days") or ["7"])[0]), 90))
        except (ValueError, TypeError):
            days = 7
        cache_key = f"news:dart:{days}"
        cached = _cache_get(cache_key)
        if cached:
            return {"items": cached}
        try:
            items = _fetch_dart_ipo_news(days=days)
        except Exception as e:
            sys.stderr.write(f"[serve] DART 뉴스 오류: {e}\n")
            return {"error": "dart_fetch_failed", "items": []}
        _cache_set(cache_key, items, ttl=1800)
        return {"items": items}

    def _handle_news_rss(self) -> dict:
        """GET /api/news/rss — TTL 900."""
        cache_key = "news:rss"
        cached = _cache_get(cache_key)
        if cached:
            return {"items": cached}
        try:
            items = _fetch_rss_news()
        except Exception as e:
            sys.stderr.write(f"[serve] RSS 오류: {e}\n")
            return {"error": "rss_fetch_failed", "items": []}
        _cache_set(cache_key, items, ttl=900)
        return {"items": items}

    def do_POST(self):
        if self.path == "/api/refresh-xlsx":
            self._send_json(self._refresh_xlsx())
        elif self.path == "/api/refresh-dart":
            self._send_json(self._refresh_dart())
        elif self.path == "/api/refresh-all":
            self._send_json(self._refresh_all())
        else:
            self._send_json({"ok": False, "error": "unknown endpoint", "path": self.path}, status=404)

    # ---- 실제 작업 ----
    def _refresh_xlsx(self) -> dict:
        xlsx = default_xlsx()
        if not Path(xlsx).exists():
            return {"ok": False, "error": "xlsx_not_found", "path": xlsx,
                    "hint": "IPO_XLSX_PATH 환경변수로 경로 지정 가능"}
        return {
            "stage": "xlsx",
            **run_cmd([
                sys.executable, str(SCRIPTS / "extract_ipo_data.py"),
                "--xlsx", xlsx, "--years", "3",
                "--out", str(DATA / "ipo-recent.json"),
            ]),
        }

    def _refresh_dart(self) -> dict:
        if not os.environ.get("DART_API_KEY"):
            return {"ok": False, "error": "DART_API_KEY missing", "stage": "dart"}
        return {
            "stage": "dart",
            **run_cmd([
                sys.executable, str(SCRIPTS / "fetch_latest.py"),
                "--append-to", str(DATA / "ipo-recent.json"),
                "--days", "30",
                "--candidates-out", str(DATA / "dart_candidates_review.json"),
            ]),
        }

    def _refresh_all(self) -> dict:
        x = self._refresh_xlsx()
        d = self._refresh_dart() if x.get("ok") else {"ok": False, "skipped": True, "reason": "xlsx failed"}
        return {"ok": x.get("ok"), "xlsx": x, "dart": d}

    # 로깅 깔끔하게
    def log_message(self, format, *args):
        sys.stderr.write(f"[serve] {self.address_string()} - {format % args}\n")


def main():
    # Windows cp949 콘솔에서 한글/특수문자 출력 안전
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    addr = ("127.0.0.1", port)
    xlsx = default_xlsx()
    dart_loaded = bool(os.environ.get("DART_API_KEY"))
    print(f"[serve] http://{addr[0]}:{port}   (root={ROOT})")
    print(f"[serve] XLSX 경로: {xlsx}  (exists={Path(xlsx).exists()})")
    print(f"[serve] DART_API_KEY: {'loaded' if dart_loaded else 'MISSING (DART 보강 스킵)'}")
    kis_loaded = bool(os.environ.get("KIS_APP_KEY") and os.environ.get("KIS_APP_SECRET"))
    kis_mode = "paper" if (os.environ.get("KIS_USE_PAPER", "").split("#")[0].strip().lower() in ("true","1","yes")) else "real"
    print(f"[serve] KIS_APP_KEY: {'loaded ('+kis_mode+')' if kis_loaded else 'MISSING (가격 API 비활성)'}")
    krx_loaded = bool(os.environ.get("KRX_API_KEY"))
    print(f"[serve] KRX_API_KEY: {'loaded' if krx_loaded else 'MISSING'} (지수 API용 — 키 활성화 필요)")
    print(f"[serve] 네이버 금융: 실시간 지수 + 투자자별 매매 (인증 불필요, /api/market 주 소스)")
    print(f"[serve] endpoints:")
    print(f"  GET  /api/ping                  — health check + 키 상태")
    print(f"  GET  /api/price?ticker=NNN      — 종목 현재가 (캐시 60s)")
    print(f"  GET  /api/prices?tickers=,,     — 배치 (최대 50, throttle)")
    print(f"  GET  /api/daily?ticker=NNN&days=60  — 종목 일별 OHLCV")
    print(f"  GET  /api/market                — KOSPI + KOSDAQ 지수")
    print(f"  GET  /api/index-daily?code=0001 — 지수 일별 (sparkline)")
    print(f"  GET  /api/stock-info?ticker=NNN — 종목명 (fdr 캐시)")
    print(f"  GET  /api/krx/investor          — KRX 시장 투자자별 매매동향")
    print(f"  GET  /api/ipo/schedule          — 공모주 일정 (TTL 6h, ?refresh=1)")
    print(f"  GET  /api/news/search?q=...     — 네이버 뉴스 검색 (TTL 10m)")
    print(f"  GET  /api/news/dart?days=7      — DART IPO 공시 (TTL 30m)")
    print(f"  GET  /api/news/rss              — 한국경제 RSS (TTL 15m)")
    print(f"  POST /api/refresh-xlsx          — XLSX 재추출")
    print(f"  POST /api/refresh-dart          — DART estkRs 보강")
    print(f"  POST /api/refresh-all           — 둘 다 순차")
    print(f"[serve] Ctrl+C 로 종료")

    # 공모주 일정 warm-up — 첫 요청자가 38.co.kr 상세 조회로 수십 초 대기하지 않도록
    # 백그라운드에서 캐시 선적재 (H-1). 실패해도 서버 기동에 영향 없음.
    def _warm_ipo_schedule():
        try:
            from kind_client import get_ipo_schedule
            items = get_ipo_schedule(fetch_detail=True)
            if items:
                _cache_set("ipo:schedule",
                           {"updated": datetime.now(timezone.utc).isoformat(), "items": items},
                           ttl=21600)
                sys.stderr.write(f"[serve] IPO 일정 warm-up 완료: {len(items)}건\n")
        except Exception as e:
            sys.stderr.write(f"[serve] IPO 일정 warm-up 실패 (무시): {e}\n")
    threading.Thread(target=_warm_ipo_schedule, daemon=True).start()

    class ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    with ThreadedServer(addr, Handler) as server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n[serve] stopping...")


if __name__ == "__main__":
    main()
