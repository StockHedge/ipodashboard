# IPO 공모주 동적 분석 대시보드 (v1.3)

최근 3년 한국 IPO 실데이터 + **실시간 시세**(네이버/KIS) 기반 인터랙티브 분석 대시보드.
단일 HTML SPA (5-view) + Python 로컬 API 서버. **impeccable-foundation** 디자인.

## 주요 기능 (v1.3)
- **5-view SPA**: 대시보드 / 고급분석(6탭) / 종목지정 / 포트폴리오 / 이때팔걸 (사이드바 + hash 라우팅)
- **실시간 시세**: KOSPI/KOSDAQ 지수 + 투자자별 순매수(개인/외인/기관) — 네이버 금융 (키 불필요)
- **AI 점수** (룰 기반 0~100): 기관경쟁률·락업·섹터·규모·주관사 가중 percentile
- **종목 지정 + 이때팔걸**: 회사 참여 종목 등록 → 매도단가 vs 실시간/최고가 회한 분석
- **포트폴리오**: KIS 실시간 평가손익 / **수익률 계산기** / **락업 만료 캘린더** (15/30/90/180일)
- **고급분석 6탭**: 히트맵(업종×분기) · 상관매트릭스(Pearson) · Violin · Parallel · 밴드vs확정가 · 락업해제후
- 차트 4종, 백테스팅 워크벤치, 종목 비교(radar), 주관사 랭킹, 네이버/DART 직링크
- 다크 모드, 키보드 단축키(Ctrl+K 등), URL 상태 공유, CSV/JSON export, 알림 토스트
- **데이터**: XLSX 마스터(`extract_ipo_data.py`) + DART 보강(`fetch_latest.py`) + 실시간(네이버/KIS)
- 헤더 "새로고침" → XLSX 재추출 + DART 보강 자동 (serve.py 필요)

## 빠른 시작

### 0. 의존성 설치 (최초 1회)
```powershell
cd ipo-dashboard
pip install -r requirements.txt
```

### 1. 대시보드 실행

**옵션 A — 정적 서버 (가장 빠름)**:
```powershell
python -m http.server 8000
# http://localhost:8000/index.html
```

**옵션 B — API 서버 (실시간 새로고침 버튼 지원, 권장)**:
```powershell
python scripts/serve.py 8000
# http://localhost:8000
```
헤더의 **"새로고침"** 버튼 클릭 → 자동으로 XLSX 재추출 + DART 보강 → 데이터 즉시 갱신.

상단 필터를 조작하면 모든 차트/KPI/테이블이 즉시 업데이트됩니다.

### 2. 실데이터 추출 (XLSX → JSON)
```powershell
python scripts/extract_ipo_data.py `
  --xlsx "$env:USERPROFILE\Downloads\새 계정 프로그램 시트 (1).xlsx" `
  --years 3 `
  --out data/ipo-recent.json
```
브라우저 새로고침으로 즉시 반영됩니다.

### 3. DART 보강 (강력 추천)
```powershell
# `.env` 에 DART_API_KEY 를 저장한 뒤 (.env.example 참조), 자동 로드됩니다.
# 또는 일회성 환경변수 주입:
$env:DART_API_KEY = "여기에_본인의_DART_키"

# Dry-run 으로 먼저 확인 (실행 시 파일 변경 없음, 통계만 출력)
python scripts/fetch_latest.py --append-to data/ipo-recent.json --days 30 --dry-run

# 실제 보강 + 실패 후보 검토용 별도 JSON 저장
python scripts/fetch_latest.py `
  --append-to data/ipo-recent.json `
  --days 30 `
  --candidates-out data/dart_candidates_review.json
```

### 4. 핵심 토글 — "진짜 신규상장 운영회사 IPO만"
대시보드 상단 "신규상장 엄격 필터" 칩(`role="switch"`, 키보드 접근 가능).
- ON: DART estkRs 성공 데이터 + 금융/SPAC 제외만 표시
- OFF (기본): 전체 데이터
- Reset 버튼은 본 토글도 함께 OFF 로 복구

### 5. 키보드·테마·공유 (v1.3)

| 단축키 | 동작 |
|---|---|
| `Ctrl + K` | Command Palette (종목 fuzzy 검색 + 명령 실행) |
| `1 ~ 5` | 기간 필터 (전체 / 6개월 / 1년 / 2년 / 3년) |
| `/` | 종목 검색 입력 포커스 |
| `t` | 라이트/다크 테마 전환 |
| `?` | 단축키 도움말 |
| `Esc` | 열린 모달/팔레트 닫기 |

- **다크 모드**: 헤더 우측 sun/moon 토글 또는 `t` 단축키. OKLCH lightness 만 invert 하여 색감(chroma) 유지. `localStorage` 저장 + `prefers-color-scheme` 자동 감지.
- **URL 상태 공유**: 모든 필터가 query string 으로 동기화되어 링크를 공유하면 동일 필터가 적용됨 (예: `?period=1y&markets=KOSDAQ&q=반도체&strict=1`).
- **KPI sparkline**: 각 KPI 카드 우측에 최근 12개월 추세 시계열. 숫자 한 점 → 추세선으로 인식 확장.

## Impeccable Foundation Audit 결과 (2026-05)
**최종 점수: 97/100** (0 critical, 2 minor)

상세 보고서: [audit/impeccable-audit-report.md](audit/impeccable-audit-report.md)

### 적용 토큰 (전체 준수)
- OKLCH 색상 공간 (accent/pos/neg/bg/card/border)
- 4px/8px 그리드 + --space-* 커스텀
- Pretendard 우선 + clamp 타이포
- 120~160ms transform/opacity 모션 + prefers-reduced-motion
- focus-visible, semantic HTML, 충분한 대비

### 남은 minor (v2 추천)
- 일부 Tailwind rounded-lg (의도적)
- 모바일 테이블 card-view 대안

## 파일 구조
```
ipo-dashboard/
├── index.html                       # 단일 파일 대시보드 (모든 로직 포함)
├── README.md
├── MIGRATION.md                     # 마이그레이션·발전 SSoT
├── requirements.txt                 # Python 의존성 명세
├── .env.example                     # DART 키 placeholder (실키는 .env, gitignore)
├── .gitignore                       # .env / 생성 데이터 / 캐시 등 표준 제외
├── audit/
│   └── impeccable-audit-report.md
├── data/
│   ├── ipo-recent.json              # 3년 실데이터 (gitignore)
│   ├── dart_candidates_review.json  # estkRs 실패 후보 검토용 (선택, gitignore)
│   └── dart_corp_code.json          # corp_code 캐시 (gitignore)
├── scripts/
│   ├── extract_ipo_data.py          # xlsx → JSON (마스터 추출)
│   ├── fetch_latest.py              # DART estkRs 성공 데이터 보강 + stats + candidates
│   └── dart_client.py               # DART OpenAPI 클라이언트 (corp_code cache, rate limit)
└── .github/workflows/
    └── update-ipo-data.yml          # 주간 자동 보강 (DART_API_KEY secret 필수)
```

## 기술 스택 & 제약
- Vanilla HTML + Tailwind CDN + Chart.js CDN (빌드 없음, 즉시 실행)
- Windows + 한글 사용자명 완전 지원 (pathlib, $env:USERPROFILE)
- 데이터는 모두 클라이언트 (보안/개인정보 주의)

## 향후 확장 아이디어 (이미 일부 구현됨)
- ✅ pykrx 보강 + fuzzy name matching (fetch_latest.py)
- ✅ upcoming/예정 종목 뱃지 (대시보드)
- ✅ DART OpenAPI 실제 연동 (scripts/dart_client.py + fetch_latest.py, 제공 키 사용)
- GitHub Actions 자동 업데이트 (`.github/workflows/update-ipo-data.yml` — 스크래핑 주의)
- Parallel Coordinates + 제한 3D PoC (이미 "고급 분석" 버튼으로 제공)

**보안 주의 (DART 키)**
- 실제 키는 절대 코드/저장소에 커밋 금지.
- `.env` 파일 사용 (`.env.example` 참고, `.gitignore` 에 의해 자동 제외됨).
- 로깅/에러 메시지에 키 노출 금지.
- 상업적 사용 전 DART 이용약관 확인.

> **주의 (2026-05 시점)**: 이전에 사용하던 키는 본 저장소 초기 버전의
> `.env.example` / `README.md` / `MIGRATION.md` 에 verbatim 노출되어 있었습니다.
> 현재 모든 파일에서 마스킹 처리되었으나 외부 노출 이력이 있을 수 있으므로,
> opendart.fss.or.kr 콘솔에서 **즉시 폐기 + 신규 발급** 후 로컬 `.env` 만 교체하세요.

## 라이선스 & 책임
- 샘플/추출 데이터는 사용자 본인 xlsx 기준
- 스크래핑(fetch_latest.py 확장 시) 사용자는 robots.txt 및 관련 법규 준수
- 본 대시보드는 투자 조언이 아님

---

**Impeccable Score 97/100 달성** — production-grade 품질.  
추가 polish 또는 기능 요청은 언제든 말씀해주세요.