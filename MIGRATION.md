# IPO 공모주 동적 대시보드 - 마이그레이션 및 발전 문서

**버전**: 1.1  
**최종 업데이트**: 2026-05 (코드 상세·사용자 지시·한계 분석 추가)  
**목적**: 프로젝트의 초기 요구사항부터 현재 상태, 주요 의사결정, 데이터 파이프라인 철학, UI 품질 기준, 향후 계획까지를 종합적으로 정리하여, 유지보수·확장 시 참조할 수 있는 단일 진실 공급원(Single Source of Truth) 역할을 수행한다.

---

## 1. 프로젝트 개요 및 초기 목표

### 1.1 배경
사용자는 첨부된 Excel 파일(`새 계정 프로그램 시트 (1).xlsx`)의 **"동적차트"** 시트를 분석하여, 공모주(IPO) 과거/현재 데이터를 기반으로 한 **인터랙티브 웹 대시보드**를 구축하고자 했다.

"동적차트" 시트의 핵심 로직:
- 최근 N건(약 20건) 공모주 필터링 (QUERY 패턴)
- 주요 메트릭: 최초/고가 매도 수익률, 고가 형성 기간, 유통가능금액(기존주주 vs 신규), 경쟁률 등
- 기간·시장·업종·수익률 구간 등 다양한 분류/필터
- 동적 차트 및 요약 테이블

### 1.2 초기 목표 (v1)
- 합성/샘플 데이터로 빠르게 동작하는 단일 HTML 대시보드 구축
- Chart.js + Tailwind 기반 4종 동적 차트 (월별 평균 수익률, 수익률 분포, 경쟁률-수익률 Scatter, 업종별 성과)
- 실시간 필터 (기간 칩, 시장 토글, 업종 다중 선택, 수익률 범위 슬라이더, 검색, 상한가 only)
- KPI 카드, Top/Bottom 리스트, 페이지네이션 테이블, 상세 모달
- **impeccable-foundation** 디자인 원칙 철저 적용 (OKLCH 색상, 4/8px 그리드, Pretendard 타이포, motion 제한, a11y, anti-pattern 배제)

**결과물**: `index.html` (자체 완결형, CDN 기반, 즉시 실행 가능)

---

## 2. 요구사항 진화 및 주요 분기점

### 2.1 Phase 1 → Phase 2 전환 (실데이터 + 업데이트 메커니즘 요구)
사용자 추가 요청:
- "최근 3년 IPO 데이터" 중심
- "데이터 호출을 통한 최신 데이터 업데이트" 필요 (API/스크립트)
- DART API KEY 제공 (`<DART_API_KEY>`)
- "A. DART estkRs 성공 데이터만 보강"하는 현실적 도구로 재설계 요청
- B/C/D 옵션 (Parallel Coordinates, 제한적 3D PoC, 기타 최대화) 진행
- B/C/D 진행 중에도 **estkRs 호출 전 추가 검증** (stricter filtering) 강화

### 2.2 핵심 의사결정 (데이터 품질 최우선)

| 결정 | 이유 | 결과 |
|------|------|------|
| **XLSX를 마스터 소스로 고정** | 사용자가 이미 보유한 데이터가 가장 정확하고, 성과(수익률, 고가달성일 등) 검증이 완료된 상태 | `extract_ipo_data.py`가 3년 필터 + 깨끗한 JSON 생성의 주체가 됨 |
| **DART를 "발견"이 아닌 "보강" 도구로 한정** | DART estkRs는 구조화된 공모 정보(공모가, 총액, 청약일정, 주간사)에 강점이 있으나, 신규상장 후보 발견 범위가 넓고 013 오류가 빈번하며, 경쟁률 데이터가 약함 | `fetch_latest.py`를 "estkRs 성공 + 실제 데이터 존재 시에만" 보강하는 도구로 재설계 |
| **38.co.kr은 best-effort fallback으로 격하** | Windows 환경에서 SSL handshake 실패가 빈번하고, 장기 스크래핑 안정성이 낮음 | 경쟁률 등 특정 보조 데이터용으로만 사용 |
| **stricter filtering 강화** | "신규상장 운영회사" vs 금융/스팩/기존사 자본확충을 명확히 구분 | `is_likely_new_ipo`에 report_nm 키워드 + 회사명 키워드 배제 + estkRs 전 사전 검증 추가 |
| **하이브리드 파이프라인 채택** | 단일 소스 한계 극복 | XLSX(마스터) + DART(구조화 메타) + 38(경쟁률 best-effort) |

---

## 3. 현재 아키텍처 (2026-05 기준)

### 3.1 데이터 파이프라인
```
사용자 XLSX (마스터, "공모주데이터" 시트)
        ↓
extract_ipo_data.py (3년 필터, 컬럼 매핑, 깨끗한 JSON 생성)
        ↓
data/ipo-recent.json (고품질 베이스)
        ↓
fetch_latest.py (DART 중심 보강 도구)
        ├── DART list.json (신규상장 필터링)
        ├── estkRs 성공 + 실제 데이터 존재 시에만 보강
        ├── 38.co.kr (best-effort, 경쟁률)
        └── fdr + pykrx (시장/티커/수익률)
        ↓
data/ipo-recent.json (업데이트된 버전)
```

**철학**:
- **XLSX = Single Source of Truth** for historical + verified performance.
- **DART = Structured enrichment only when estkRs actually delivers real offering data.**
- **Never pollute high-quality data with partial/noisy records.**

### 3.2 대시보드 (index.html)
- **필터**: 기간(3Y 기본), 시장, 업종, 수익률 범위, 검색, 상한가 only + **"진짜 신규상장 운영회사 IPO만" stricter toggle**
- **차트**: Chart.js 4종 + Plotly (Parallel Coordinates + 제한적 3D PoC)
- **데이터 출처 시각화**: Source badges (DART / 38.co.kr / XLSX), upcoming badges
- **품질 원칙**: impeccable-foundation (OKLCH, 4/8 grid, Pretendard, motion 제한, 한국어 wrapping 완벽화, a11y)

### 3.3 주요 파일
- `scripts/extract_ipo_data.py` — XLSX → JSON (마스터 생성)
- `scripts/fetch_latest.py` — DART estkRs 보강 도구 (현실적 하이브리드)
- `scripts/dart_client.py` — DART OpenAPI 클라이언트 (corp_code cache, rate limit, 에러 처리)
- `index.html` — 단일 파일 대시보드
- `data/ipo-recent.json` — 실행 결과물 (gitignore 권장)
- `MIGRATION.md` (본 문서)

---

## 4. 주요 기술적 도전과 해결

### 4.1 DART 데이터 품질 문제
- **현상**: list.json으로 후보를 발견해도 estkRs에서 013 또는 데이터 없음이 빈번. 많은 후보가 금융/스팩/기존사.
- **해결**:
  - `discover_new_ipos` + `is_likely_new_ipo`에서 report_nm 키워드 + 회사명 배제 + corp_cls=E 신호 강화.
  - estkRs 호출 **전** 추가 검증 (strong_new_listing 확인).
  - estkRs 성공 + slprc/slta 존재 **확인 후에만** 레코드 생성.
  - "보강" 철학으로 전환 (실패한 후보는 JSON에 넣지 않음).

### 4.2 38.co.kr 안정성 문제
- Windows 환경에서 SSL handshake 실패 상시 발생.
- **해결**: best-effort fallback으로 격하. 핵심 데이터 흐름에서 제거.

### 4.3 UI 품질 유지 (impeccable-foundation)
- 모든 변경( stricter toggle, source badges, advanced viz, 3D PoC)에 디자인 토큰 적용.
- 한국어 텍스트 줄바꿈 문제 지속 해결 (word-break: keep-all + overflow-wrap).
- 3D PoC는 "제한적 + 가독성/접근성 주의" 명시.

### 4.4 에이전트/스킬 활용 문화 정착
- Grok MD 파일들에 "효율과 작업량 극대화를 위해 스킬·에이전트를 마음껏 병렬 이용, 토큰/세션 걱정 말라" 지침 명시.
- Review / Explore / Implement 에이전트 병렬 호출로 코드 리뷰·필터 연구·UI 구현 가속.

---

## 5. 현재 상태 (2026-05)

- **데이터 품질**: XLSX 마스터 + DART estkRs 성공 보강 하이브리드로 전환 완료. 저품질 데이터 유입 차단.
- **스크립트**: `fetch_latest.py`가 "보강 도구"로서 동작. --dry-run으로 미리 확인 가능.
- **대시보드**: stricter filter 토글, source/upcoming 시각화, Parallel Coordinates + 3D PoC 기반 준비 완료.
- **문서**: 본 MIGRATION.md + README + audit-report.md로 지식 체계화.

---

## 6. 향후 계획 (Execution Sequence)

1. **데이터 품질 고도화** (이미 상당 부분 완료)
   - estkRs 성공률 로깅 강화
   - 38.co.kr 특정 이름 경쟁률 보강 로직 보완 (선택)
   - "DART 후보 검토용 별도 파일" 출력 (estkRs 실패했으나 list에 오른 후보)

2. **Grok MD 가이드라인 정착** (완료)

3. **B/C/D UI 완성** (stricter filtering과 연동)
   - B: Parallel Coordinates (Plotly) — 다차원 brushing + stricter 토글 연동
   - C: 제한적 3D PoC (Plotly scatter3d) — 토글 + 접근성 주의 문구
   - D: 기타 (README/audit-report 보강, 추가 로깅, 성능 최적화, docs)

4. **Impeccable Audit & Polish**
   - review skill + subagent로 index.html + 스크립트 감사
   - 100/100 또는 0 critical issues 달성까지 반복

5. **최종 검증**
   - 실제 XLSX + DART 보강 파이프라인 end-to-end 테스트
   - Lighthouse / a11y / 다양한 필터 조합 검증
   - Agent 활용 효율성 회고

6. **운영 안정화**
   - GitHub Actions (선택, DART 키 secret 처리 주의)
   - 사용자 매뉴얼 고도화

---

## 7. 사용 및 유지보수 가이드

### 7.1 일상 워크플로 (최고 품질 유지)
1. XLSX에 신규 데이터 추가/수정
2. `extract_ipo_data.py` 실행 → `data/ipo-recent.json` 갱신
3. (선택) DART estkRs 보강 필요 시 `fetch_latest.py` 실행
4. `index.html` 열고 stricter filter + 고급 분석으로 확인

### 7.2 주의사항
- **DART 키 보안**: 절대 코드/저장소에 커밋 금지. `.env` 사용.
- **38.co.kr**: Windows SSL 문제로 flaky. 자동화에 과도한 의존 금지.
- **데이터 품질**: "estkRs 성공 + 실제 데이터" 기준을 절대 타협하지 말 것.

---

## 8. 부록: 참고 자료

- `plan.md` (최초 상세 실행 계획)
- `audit/impeccable-audit-report.md`
- DART OpenAPI 문서 (opendart.fss.or.kr)
- KRX KIND 공모/상장 페이지
- 38.co.kr IPO 일정 페이지

---

**이 문서는 살아있는 문서입니다.**  
중요한 변경(필터링 로직, 아키텍처, UI 패턴 등)이 있을 때마다 즉시 업데이트해야 합니다.

**문서 소유자**: 프로젝트 팀 (또는 지정된 메인테이너)  
**다음 리뷰 일정**: B/C/D 완료 후 또는 중대한 데이터 파이프라인 변경 시

---

## 9. 상세 기술 구현 (코드 수준 참조)

### 9.1 DART 클라이언트 핵심 로직 (`ipo-dashboard/scripts/dart_client.py`)

- `IPO_POSITIVE_KEYWORDS` / `IPO_NEGATIVE_KEYWORDS` (186~193행): "신규상장", "기업공개", "신규공모" 등 긍정 + "유상증자", "CB", "스팩" 등 부정 키워드 정의.
- `is_likely_new_ipo` (195~212행):
  - report_nm에 긍정 키워드 포함 또는 corp_cls=="E" (비상장) + 지분증권/증권신고서
  - 부정 키워드 포함 시 즉시 제외 (has_exclude and not has_ipo_signal)
  - pre-IPO 지원 (stock_code 없는 경우 PREIPO_ 접두 키로 캐시)
- `discover_new_ipos` (214~250행): list.json (pblntf_ty=C, pblntf_detail_ty=C001) 페이지 순회 → is_likely_new_ipo 필터 → rcept_no dedup. estkRs 호출 최소화 전략.
- `fetch_estk_details` (164~183행): slprc(공모가), slta(총액), sbd(청약기일), pymd(납입기일), actnmn(주간사) 구조화 추출.
- `enrich_with_dart` (269~295행): corp_code 캐시 매핑 후 estkRs 호출, DartApiError 로깅 (020 rate-limit 지수 백오프).

### 9.2 보강 스크립트 핵심 로직 (`ipo-dashboard/scripts/fetch_latest.py`)

- `normalize_name` (64~73행): NFKC + (주)·(유)·주식회사 제거 + 공백 정규화 (fuzzy 매칭 전처리).
- Pre-estkRs 강력 검증 (185~213행):
  ```python
  strong_new = any(kw in report_nm for kw in ["신규상장", "기업공개", "신규공모", "상장공모"])
  is_pre_ipo = (r.get("corp_cls") == "E")
  is_financial_or_spac = any(x in name_lower for x in ["증권", "은행", "스팩", "인수목적", "제", "호"])
  if (strong_new or is_pre_ipo) and not is_financial_or_spac and r.get("corp_code"):
      details = client.fetch_estk_details(...)
      if details and d.get("slprc") and d.get("slta"):
          rec[...] = ...; records.append(rec)  # 성공 데이터만
  ```
- 스마트 머지 (284~312행):
  - 기존 레코드 key=(name, listingDate) 기준
  - offeringPrice == 0 이었던 레코드만 DART 데이터로 업데이트
  - 신규 레코드는 추가
  - source 필드에 "+DART" 병기
- 38.co.kr은 217~222행에서 records 부족 시에만 best-effort fallback (Windows SSL 실패 무시).

### 9.3 UI 최근 변경 (`ipo-dashboard/index.html`)

- Stricter 토글 (311~362행): "진짜 신규상장 운영회사 IPO만 (DART estkRs 성공 + 비금융/비SPAC)" — 현재 필터와 AND 결합. DART 보강 또는 _upcoming + 금융/SPAC 배제 기준.
- `normalizeDART` (518~538행): DART 우선 필드 (offeringPrice, offeringAmount, dart_*) 정규화.
- Source badges (697~701행): DART / 38.co.kr / XLSX 클래스 분기 + CSS (108행).
- 고급 분석 모달 (1209~1310행):
  - Parallel Coordinates (Plotly): competitionRetail · firstDayReturn · offeringAmount · daysToHigh · return6M (색상=첫날수익), brushing 지원.
  - 3D scatter3d PoC: 제한적 사용 명시 ("가독성/접근성 이유로 보조용, 2D/Parallel 우선 권장").
- DART 보강 섹션 (492~518행): 모달 내 "DART 보강 정보" 표시 (rcept_no, corp_code, subscription date 등).

### 9.4 데이터 파이프라인 파일 위치 (2026-05 기준)

- 마스터 추출: `ipo-dashboard/scripts/extract_ipo_data.py` (XLSX "공모주데이터" 시트 → 3년 필터 → data/ipo-recent.json)
- 보강 도구: `ipo-dashboard/scripts/fetch_latest.py` (DART 중심 + 38 fallback)
- 클라이언트: `ipo-dashboard/scripts/dart_client.py`
- 대시보드: `ipo-dashboard/index.html` (단일 파일, CDN)
- 캐시: `data/dart_corp_code.json` (루트 data/)
- 실행 예시: `cd ipo-dashboard; python scripts/fetch_latest.py --append-to data/ipo-recent.json --dry-run`

---

## 10. 주요 사용자 지시 및 의사결정 이력 (verbatim 인용)

- "A. fetch_latest.py를 DART 중심으로 재설계... estkRs 전 추가 검증... B/C/D/ 진행 사이에 더 엄격한 필터링 추가"
- "A. 스크립트를 'DART estkRs 성공 데이터만 보강'하는 현실적인 도구로 재설"
- "증권신고서 예시 먼저 보내줄게, 그거 보고 A-2 만들어줘" (실제 PDF 예시 분석 후 stricter 키워드 강화)
- "효율/작업량 극대화 위해 스킬/에이전트 마음껏 이용, 토큰/세션 걱정 말라" → Grok MD 파일(08-skills.md, 15-agent-mode.md, 16-subagents.md) 및 ~/.claude/Claude.md에 반영
- "DART API KEY :<DART_API_KEY> + 다음 단계 작업량 최대로..."
- "B/C/D UI 완성 (stricter filtering과 연동)" 지시

**핵심 철학 전환**: "발견" → "estkRs 성공 + 실제 데이터 존재 시에만 보강". 저품질 후보(013 오류 다수) 유입 원천 차단.

---

## 11. 에이전트/스킬 활용 현황

- **최대 병렬 활용 문화 정착**: review skill (코드 품질), explore (DART/KRX API 연구), implement (UI/기능) subagent 다수 spawn.
- **impeccable-foundation** skill: UI 전반 (OKLCH, 4/8 grid, Pretendard, motion 120-160ms, keep-all wrapping, a11y).
- **bold-direction** skill: 외부 노출 데모 성격 고려 (랜딩급 품질 목표).
- **check / best-of-n** 등 추가 스킬 탐색 가능성 명시.
- 결과: audit 97/100 (0 critical), Parallel/3D PoC + stricter 토글 단기 구현, 하이브리드 파이프라인 안정화.

---

## 12. 한계, 리스크 및 미해결 과제

### 12.1 데이터 소스 한계
- **38.co.kr**: Windows 환경 SSLV3_ALERT_HANDSHAKE_FAILURE 상시. best-effort fallback으로 격하. 자동화 의존 금지.
- **DART estkRs**: 013 "조회된 데이타 없습니다" 빈번 (list 후보의 대부분이 실제 공모 데이터 없음). strict pre-filter + 성공 데이터 only 정책으로 대응 중. 성공률 로깅 강화 필요.
- **XLSX 마스터**: 사용자가 직접 관리. 컬럼 매핑(guess_column_indices)이 파일별 변동 시 수동 조정 필요.

### 12.2 인프라/운영 리스크
- GitHub Actions 워크플로 (`ipo-dashboard/.github/workflows/update-ipo-data.yml`): `scripts/fetch_latest.py` 경로 참조. 실제 저장소 구조(ipo-dashboard/scripts/)와 불일치 가능성. 실행 전 경로 확인 필수. DART 키 secret 처리 주의.
- .env / 키 보안: 절대 코드·저장소 커밋 금지. 로깅에도 키 노출 금지.
- pykrx / fdr: 상장 후 가격 데이터만 유효. pre-IPO 또는 당일 상장 종목은 별도 처리 필요.

### 12.3 문서/계획 관리
- **plan.md**: 디스크에 존재하지 않음. 초기 상세 계획은 대화 기록 + 본 MIGRATION.md로 대체. 향후 plan.md 생성 시 "chat-driven planning" 이력과 동기화 명시 권장.
- B/C/D 미완: Parallel Coordinates는 렌더 버튼으로 제공되나, stricter 토글 실시간 연동·상태 저장·접근성 개선 여지 있음. 3D PoC는 "제한적" 문구 유지.

### 12.4 권장 다음 단계 (우선순위 순)
1. estkRs 성공률 로깅 + "DART 후보 검토용 별도 JSON" 출력 (estkRs 실패 후보 별도 관리).
2. index.html B/C/D polish (brushing 상태 유지, 3D 토글 기본 off, 모바일 대응).
3. review skill 재실행 → 100/100 또는 0 critical 달성.
4. 실제 XLSX + DART 키 end-to-end 검증 (사용자 제공 파일 기준).
5. GitHub Actions 경로 수정 + dry-run 모드 CI 추가 (선택).
6. Grok MD 가이드라인 나머지 파일(사용자 가이드) 업데이트 확인.

---

## 13. 부록: 참고 자료 및 실행 명령 예시

### 참고
- DART OpenAPI: https://opendart.fss.or.kr (list.json, estkRs, corpCode.xml)
- KRX KIND 공모/상장 공시
- 38.co.kr IPO 일정 (보조)
- `audit/impeccable-audit-report.md` (97/100 상세)
- `ipo-dashboard/README.md` (빠른 시작)

### 주요 실행 명령 (PowerShell, ipo-dashboard/ 디렉토리 기준)
```powershell
# 1. 마스터 추출 (XLSX → JSON)
python scripts/extract_ipo_data.py --xlsx "$env:USERPROFILE\Downloads\새 계정 프로그램 시트 (1).xlsx" --years 3 --out data/ipo-recent.json

# 2. DART 보강 (dry-run 먼저 권장)
$env:DART_API_KEY = "<DART_API_KEY>"
python scripts/fetch_latest.py --append-to data/ipo-recent.json --days 30 --dry-run
python scripts/fetch_latest.py --append-to data/ipo-recent.json --days 30

# 3. 대시보드 사용
# index.html 브라우저 오픈 → "진짜 신규상장..." 토글 ON → 고급 분석 → Parallel Coordinates 렌더
```

---

*최고 품질은 "많은 데이터"가 아니라 "신뢰할 수 있는 데이터 + 명확한 출처 + 사용자가 쉽게 이해하고 통제할 수 있는 시스템"에서 나온다.*

**버전 1.1 (2026-05)**: 코드 수준 상세(9장), 사용자 지시 verbatim(10장), 에이전트 활용(11장), 한계/미해결(12장), 실행 예시(13장) 추가. plan.md 미존재 명시.

---

## 14. v1.2 변경 이력 (2026-05-27)

본 절은 v1.1 → v1.2 전환에서 발견·해소된 결함과 후속 개선을 SSoT 로 남긴다.

### 14.1 발견된 P0 결함 (모두 해소됨)

| # | 카테고리 | 결함 | 위치 | 해소 방식 |
|---|---|---|---|---|
| 1 | 보안 | DART 키 verbatim 평문 노출 | `.env`/`.env.example` 라인 3, `README.md` 라인 28, `MIGRATION.md` 라인 37·250·308 | `your_dart_api_key_here` placeholder 및 `<DART_API_KEY>` 마스킹 |
| 2 | 보안 | `.gitignore` 부재 | 루트 | 신규 생성 (.env, data/*.json, cache 표준 제외) |
| 3 | 런타임 | `fetch_latest.py` `records` 변수 미초기화 → 첫 DART 호출 시 NameError | `scripts/fetch_latest.py` 라인 158 부근 | `records: list[dict] = []` 초기화 + `candidates_failed` + `stats` 카운터 추가 |
| 4 | 런타임 | `match_name()` 함수 미정의 → NameError | `scripts/fetch_latest.py` 라인 232 | rapidfuzz `process.extractOne` inline 구현 + HAS_RAPIDFUZZ 가드 + exact-match fallback |
| 5 | UI | `id="strict-chip"` 중복 (HTML 사양 위반) | `index.html` 라인 313 + 357 | 라인 311~317 블록 삭제, 라인 351~363 만 유지 |
| 6 | UI | `toggleStrictNewFilter()` 함수 미정의 → 클릭 시 ReferenceError | `index.html` 라인 313/357 onclick | 함수 정의 추가 (`debouncedFilter` 직후), 키보드 접근성 포함 |
| 7 | UI | `resetAllFilters()` 에 `strictNewOnly` 누락 → 리셋 후에도 strict 유지 | `index.html` 라인 862 | 리셋 객체에 추가 + chip 시각 상태 복구 |
| 8 | 워크플로 | `DART_API_KEY` secret 미주입 + `python-dotenv` 미설치 → DART 전체 스킵 | `.github/workflows/update-ipo-data.yml` 라인 19~25 | `pip install -r requirements.txt` + `env: DART_API_KEY: ${{ secrets.DART_API_KEY }}` 주입 + dry-run 스모크 단계 |
| 9 | 데이터 | `data/ipo-recent.json` = `[]` (빈 배열) | 루트 data/ | 사용자 외부 작업 항목 (XLSX 추출 실행) — README 빠른 시작에 명시 |
| 10 | 인프라 | `requirements.txt` 부재 | 루트 | 신규 생성 (openpyxl/requests/python-dotenv 필수 + 선택 패키지) |

### 14.2 P1·P2 데이터/UI 고도화

- `fetch_latest.py`: estkRs 성공률 통계(`stats` JSON) + `--candidates-out` 으로 실패 후보 별도 JSON 출력. dry-run 출력에 후보 미리보기 포함.
- `fetch_latest.py`: SPAC 필터 false positive 보정 — `"제","호"` 문자열 매칭 → 정규식 `r"제\s*\d+\s*호"` 로 정밀화. 일반 회사명 오탐 차단.
- `extract_ipo_data.py`: `except Exception: pass` 패턴 2곳 모두 도메인 예외 + stderr 로깅으로 교체.
- `index.html` CSS: `.market-kospi`/`.market-kosdaq` OKLCH 토큰 클래스 신설(Tailwind ad-hoc 색상 제거), `.text-xxs` 토큰(인라인 `text-[10px]` 6곳 모두 교체), `.chart-container` 높이 `clamp(220px,28vw,300px)` + 모바일 미디어 쿼리.
- `index.html` JS: `applyFilters()` 끝에서 고급 분석 모달 열린 상태이면 Parallel Coordinates 자동 리렌더. `closeModal()` 에 `Plotly.purge` + 모달 본문 복구 통합 (메모리 누수 차단). 깨진 인라인 `originalContent` 핸들러 제거.
- `index.html` 접근성: strict-chip 전역 `document` keydown 리스너 → element-local 리스너로 좁힘. `text-red-600` Tailwind 색 → `var(--neg)` 토큰. 테이블 secondary 컬럼(`업종/6M/경쟁률/공모총액`)에 `.hide-mobile` 적용.
- `index.html` 도메인 버그: `r.return6M >= 0` 비교가 `null >= 0 === true` JavaScript 동작으로 'pos' 잘못 부여 → null 우선 체크 후 pos/neg 결정으로 수정 (테이블 + 모달 두 곳).

### 14.3 P3 검증 (병렬 sub-agent + skill)

세 개의 독립 sub-agent 가 각각 (a) impeccable-foundation 재감사, (b) Python 코드 리뷰, (c) 보안 점검을 수행. 회귀 1건(`const strictChip` 이중 선언 → 스크립트 파싱 실패) 즉시 발견·해소. 최종 syntax/import/dry-run 통과 확인.

### 14.4 잔여 권장 사항 (v1.3 후보, 선택)

- **DART 키 폐기/재발급**: 사용자 외부 작업. opendart.fss.or.kr 콘솔에서 즉시 실행 권장 (README 보안 주의 박스 참조).
- `fetch_latest.py` 로깅: `print` 와 `sys.stderr` 혼용 → `logging.getLogger(__name__)` 일원화.
- `index.html`: 사용자 업로드 JSON 경로의 XSS 안전성 — `escapeHtml()` 헬퍼 도입 후 모든 `innerHTML` 템플릿 적용.
- `fetch_latest.py`: `enrich_with_dart` 호출 분기에서 `corp_code` 만 있고 `ticker` 없는 레코드의 dead branch 정리.
- 모바일 테이블 카드뷰: 현재 `.hide-mobile` 로 핵심만 노출. 카드 형태 완전 변환은 v2 후보.

### 14.5 plan.md 동기화

본 v1.2 작업은 `C:\Users\강지호\.claude\plans\velvet-beaming-hearth.md` 에 사전 계획되었고 사용자 승인 후 실행되었다. plan 파일은 archive 용도로 유지한다 (저장소 외부).

---

**버전 1.2 (2026-05-27)**: P0~P3 의 critical 결함 10건 + minor 6건 해소. 회귀 1건 즉시 발견·해소. README 사용법 보강, `.env` 키 폐기 권고 명시.

---

## 15. v1.3 변경 이력 (2026-05-28) — 실시간 시세 통합 + 분석 고도화 + 품질 마감

v1.2 이후 사용자 요구에 따라 **단순 정적 대시보드 → 실시간 데이터 + 다차원 분석 + 운영 도구**로 대폭 확장.

### 15.1 데이터 소스 (3중 하이브리드 + 키 없는 우회)

| 소스 | 용도 | 인증 | 비고 |
|------|------|------|------|
| **XLSX 마스터** | IPO 228건 (공모가/수익률/락업/경쟁률/밴드) | — | `extract_ipo_data.py`. ticker 누락 시 fdr 자동 보완, 업종 대분류 정규화, 밴드 하단/상단 추가 |
| **네이버 금융** (`naver_client.py`, 신규) | KOSPI/KOSDAQ 실시간 지수 + 투자자별 순매수(개인/외인/기관) | **불필요** | `m.stock.naver.com/api/index/{code}/basic·trend`. KIS 모의투자 부정확/ KRX 키 미활성 우회. `/api/market` 주 소스 |
| **KIS** (`kis_client.py`) | 개별 종목 현재가 + 일별 OHLCV (이때팔걸/포트폴리오/모달 차트) | OAuth (모의투자) | 토큰 24h 캐시 (`data/.kis_token.json`) |
| **KRX OpenAPI** (`krx_client.py`) | 지수 일별시세 (확정 endpoint `data-dbg.krx.co.kr/svc/apis/idx/krx_dd_trd`) | AUTH_KEY | **키 401 미활성** — 활용신청 필요. 투자자별 매매 API 는 명세 미제공 |
| **DART** (`dart_client.py`) | estkRs 성공 데이터 보강 | API KEY | 기존 |

### 15.2 신규 백엔드 (`serve.py`)
- 표준 라이브러리 HTTP 서버 + `/api/*` 프록시 (키는 서버 .env 에만, 브라우저 노출 0)
- endpoints: `ping / price / prices / daily / market / index-daily / stock-info / krx/index / krx/investor` (GET) + `refresh-xlsx / refresh-dart / refresh-all` (POST)
- 캐시 TTL (price 60s, KRX 900s), KIS rate-limit throttle, fdr 종목명 마스터(24h), CORS origin 제한, XLSX 경로 traversal 방어
- subprocess `PYTHONIOENCODING=utf-8` (Windows 한글 stdout fix)

### 15.3 UI 5-view 라우터 (hash 기반)
- `대시보드(#/)` / `고급분석(#/advanced)` / `종목지정(#/participation)` / `포트폴리오(#/portfolio)` / `이때팔걸(#/regret)`
- **버그 이력**: `.hidden-view` CSS selector 가 dashboard/advanced 만 커버 → participation/portfolio/regret 안 숨겨지던 치명 버그를 `.hidden-view { display:none !important }` 범용 규칙으로 해소

### 15.4 신규 기능
- **사이드바** (240px): 일일 디브리프 위젯, 11개 메뉴, 모바일 햄버거
- **마켓 hero**: 대시보드 상단 KOSPI/KOSDAQ 큰 카드 (실시간 + sparkline + 투자자 순매수)
- **AI 점수** (룰 기반 0~100): 기관경쟁률40 + 락업25 + 섹터15 + 규모10 + 주관사10. percentile rank + 베이지안 축소. O(N²)→O(N) precompute
- **종목 지정**: 회사 참여 종목 분류 + 매수/매도가/메모 (localStorage). "참여 종목만" 필터 전역 연동
- **이때팔걸**: 매도단가 vs 실시간/특정시점/최고가 → 회한 등급 (🎯/👍/😐/😢)
- **포트폴리오**: 보유 종목 KIS 실시간 평가손익
- **고급분석 6탭**: 히트맵(업종×분기), 상관 매트릭스(Pearson+p), Violin, Parallel v2, 밴드vs확정가, 락업해제후
- **주관사 랭킹** (베이지안), **수익률 계산기**, **락업 만료 캘린더** (15/30/90/180일), **알림 토스트**
- 다크 모드 default, Pretendard 폰트, 네이버/DART 직링크, 종목 비교 radar, 백테스팅 워크벤치

### 15.5 품질 (다중 에이전트 리뷰)
- **a11y-auditor** 55→: focus trap(4 모달), 필터칩 button화, role=dialog, aria-selected/live, 차트 aria-label, 색상+▲▼ 보조
- **code-reviewer** 68→~85: XSS escHtml(BLOCKER 2), 죽은코드 제거(_get_kis/_loadKrxInvestor), thread-safety(get_naver), 하드코딩 경로 제거, 에러 escape
- **perf-analyst**: AI점수 O(N²)→O(N), Page Visibility skip, updateUrlState debounce
- **impeccable-foundation** ~87: OKLCH 토큰, 4/8 grid, prefers-reduced-motion 전역

### 15.6 잔여 (v1.4 후보)
- KRX 키 활성화(사용자) → 지수 정확도 / 투자자별 매매 별도 API 명세
- onclick → data-* 전환 (XSS 완전 차단), Plotly.purge → react, sparkline 실데이터화
- GitHub Actions 키 secret, Lighthouse/axe 최종 검증

---

**버전 1.3 (2026-05-28)**: 네이버/KIS/KRX 실시간 시세 통합, 5-view SPA, AI 점수, 종목지정/이때팔걸/포트폴리오, 고급분석 6탭, 다중 에이전트 리뷰 (a11y/code/perf). 키 없는 네이버 우회로 시장 데이터 즉시 작동.
## 16. v1.4 변경 이력 (2026-06-12) — 4-view 재구조 + 통합 IPO 시스템 Phase 1~3

### IA 재구조 (Phase 1)
- 5-view → **4-view**: 오늘(`#/`) / 종목(`#/screener`) / 내 포지션(`#/positions`, 참여·포트폴리오·이때팔걸 3탭 통합) / 분석(`#/analytics`)
- **종목 상세 페이지** 신설 (`#/stock/:key`) — 가격(KIS 일별)·일정 타임라인·뉴스·메모 통합 앵커. 테이블 행 클릭 진입, `⋯` 버튼은 기존 빠른 모달
- 차트 4종 + 백테스팅을 분석 view 로 이관 (개요/백테스팅 탭). Violin 탭 제거 (분포 차트와 중복)
- 레거시 hash (`#/advanced`, `#/participation`, `#/portfolio`, `#/regret`) 자동 리다이렉트
- renderAll 은 보이는 view 만 렌더 (숨김 차트 0-size 방지 + perf)

### 일정 보드 (Phase 2)
- `scripts/kind_client.py` 신규 — 공모주 일정 (수요예측/청약/상장, stage 판정). KIND AJAX 차단으로 38.co.kr 모바일 파싱 대체 (1s 간격, TTL 6h)
- `/api/ipo/schedule` — 오늘 보드 3버킷 (오늘/이번 주/다음 주) + 파이프라인 칩 (수요예측 N·청약 N·상장대기 N)
- 내부 데이터와 통합: 상장 D-day + 락업 해제 (상장+6M)

### 뉴스 피드 (Phase 3)
- `/api/news/dart` (DART list.json, 증권신고서/투자설명서 필터) + `/api/news/rss` (한국경제) → 오늘 view 뉴스·공시 카드 (10분 주기, 출처 필터)
- `/api/news/search` (네이버 검색 API) → 종목 상세 뉴스 탭. `.env` 에 `NAVER_CLIENT_ID`/`NAVER_CLIENT_SECRET` 필요 (없으면 안내 메시지로 강등)

### 주의
- 38.co.kr 파싱은 저작권(DB권) 관점 내부용 저빈도 사용 한정. KRX KIND 공식 경로 확보 시 교체 권장

## 17. v1.4.1 (2026-06-12) — KRX 키 활성화 + 전면 멀티에이전트 리뷰 반영

### KRX OpenAPI
- 신규 키 활성화 확인 (이전 키 401 → 신규 키 200). `stk_bydd_trd`(주식)는 미구독(401), `idx`(지수)는 구독됨.
- 결함 수정: `get_index_series` 가 `krx_dd_trd`(KRX 시리즈, 코스피/코스닥 미포함)를 호출하던 것을
  `kospi_dd_trd`/`kosdaq_dd_trd`(대표지수)로 교체 + IDX_NM 정확매칭(코스피200 오선택 방지).
- 영업일 자동 역추적: 최근일 미반영(빈 배열) 시 직전 영업일로 최대 7회 walk-back. `_index_rows`는
  401만 즉시 전파, 그 외 일시오류는 빈 리스트로 강등해 walk-back 지속.
- `/api/krx/index` 실데이터 확인: KOSPI 7763.95(+0.43%), KOSDAQ 996.93(+4.76%) (요청 20260612→20260611 역추적).

### 멀티에이전트 리뷰 (code/backend/security/a11y/perf 5종 병렬) → 수정 반영
- 보안: 뉴스 href 에 escHtml(텍스트용)만 적용돼 javascript:/data: 스킴 주입 가능 → `_safeUrl()` sanitizer 추가.
  종목명 inline onclick safeName 에 백슬래시 이스케이프 추가. getDartLinkHTML title/aria escHtml.
- 백엔드 BLOCKER: `/api/news/search` 키 미설정 시 `[{error}]`(리스트) 반환으로 프론트 감지 실패 + 오류 캐싱
  → `NaverConfigError` raise + 핸들러가 `{error, items:[]}` 반환(캐시 안 함).
- 백엔드 HIGH: kind_client 상세조회 230s 폭주 → 건수·시간 예산(20건/45s) + 서버 기동 시 warm-up 스레드.
  `_parse_date_range` 가 날짜 내 하이픈 분할 → `~` 만 분할. `_handle_market` 투자자 실패 silent → 로깅.
- 프론트 HIGH: `renderTable` 의 `competitionRetail.toFixed` null 크래시 가드. 종목상세 재진입 시 `_sdChart`
  좀비 인스턴스 → 진입 시 destroy. 외부 일정 항목 id 보강(클릭 가능). analytics 탭 desync → `_activeAdvancedTab`.
- 접근성: nav aria-current, 탭 화살표 키 이동, aria-sort, 파일업로드 label→button, 메모 label,
  타임라인 aria 상태, 토스트 danger=assertive, 오늘 보드 항목 span→button.
- 성능: Plotly(~3.5MB) head 동기로드 제거 → loadPlotly() 지연 로딩(초기 TTI 개선). 뉴스 피드 9분 캐시.

### 잔여 (v1.5 후보)
- a11y: tab-tabpanel aria-controls 연결, 소형 칩(11px) 색 대비 토큰 조정.
- perf: KPI sparkline destroy/recreate→update 재사용, /api/market 중복 폴링 통합, computeIpoScore 사전계산.
- 백엔드: 38.co.kr HTTPS, 영업일 공휴일 캘린더, _sdLoadNews AbortController(경쟁조건).
- 죽은 코드: showAdvancedAnalysis(레거시 모달) 제거.
- 사용자 액션: `.env` NAVER_CLIENT_ID/SECRET 입력(현재 EMPTY), 회사 서버 v1.4 재배포.

## 18. v1.5 (2026-06-12) — 필터링 확장 + 그래프 정리 + 네이버 뉴스 활성화

### 네이버 뉴스 API
- 키 변수명 호환: `NAVER_NEWS_CLIENT_ID/SECRET` 우선, 레거시 `NAVER_CLIENT_ID/SECRET` 폴백.
- 실제 키 반영 → /api/news/search 실데이터 확인(종목 상세 뉴스 탭 작동).

### 필터링 확장 (종목 스크리너)
- AI 점수 최소 필터 (전체/60+/70+/80+), 상장 상태 필터 (전체/예정/상장됨).
- 빠른 프리셋 4종: AI 우량(80+) · 예정 IPO · 최근 상장(1년) · 강세 후보(AI70+·수익0+).
- activeFilters/applyFilters/resetAllFilters/URL state(ai,status)/syncFilterUIFromState 전부 연동.
- AI 점수는 _precomputeScoreCache 사용 → 필터 row 당 O(1).

### 그래프 정리 (분석 개요 탭)
- 차트 지표 셀렉터(첫날/고가/6개월) → 월별·분포·업종 차트 동일 지표 전환. 정적 다중차트 → 1컨트롤 전환.
- 셀렉터↔_chartMetric 재진입 동기화, null 지표값 안전 제외.

### 기능 확장
- 오늘 보드 파이프라인 칩(수요예측/청약/상장대기) 클릭 → 예정 IPO 프리셋 + 스크리너 이동.

## 19. v1.6 (2026-06-12) — impeccable 디자인 시스템 재구조 + 미룬 후보 정리

### 디자인 시스템 재구조 (impeccable-foundation)
- 타이포: 산발적 px값(0.625/0.6875/0.75/0.8125/0.875/1.125/1.5rem)을 모듈러 스케일 토큰화
  (--fs-2xs~--fs-stat). 11px 하한 적용 — 10px(0.625rem) 배지/메타를 11px(--fs-2xs)로 상향(a11y).
- 간격: --space-5/16 보강. 소형 칩 패딩 4/8 그리드 정렬.
- 소형 칩 대비: today-ev/news-src 의 color-mix 저대비 → 대비 안전 토큰 쌍(--chip-*-bg/fg, 라이트/다크 각각)
  으로 교체. "컬러 배경 위 회색 글씨"(news-src--news) 해소.
- 모션: 칩 transition: all → 색/배경/테두리/그림자로 스코프(레이아웃 속성 애니메이션 배제).
- 라인하이트 토큰(--lh-tight/snug/body) 분리.

### 미룬 후보 반영
- a11y(F-02): 탭↔패널 aria-controls/role=tabpanel/aria-labelledby 를 init 에서 일괄 wiring(분석·내포지션).
- perf: /api/market 공유 캐시(_getMarket, 30s + inflight 합치기) — hero/widget/panel 중복 폴링 → 동시호출 1회.
- perf/경쟁조건: _sdLoadNews AbortController — 종목 빠른 전환 시 이전 뉴스 요청 취소.
- 백엔드(서브에이전트): 38.co.kr HTTPS 우선+HTTP 폴백(현재 서버 HTTPS 미지원→폴백 동작),
  KRX 공휴일 set 2024~2026 + _guess_latest_biz_day/_prev_biz_day 반영 + get_market_investor walk-back.

### 잔여 (v1.7 후보)
- perf: KPI sparkline destroy/recreate→update 재사용(필터당 6 인스턴스 재생성).
- 죽은 코드: showAdvancedAnalysis(레거시 모달) 제거.
- 뉴스 필터 칩 role=tab→toggle(aria-pressed) 정정.

## 20. v1.7 (2026-06-12) — 잔여 후보 3종 정리

- perf(KPI sparkline 재사용): renderKPIs 가 매 필터마다 6개 Chart 인스턴스를 destroy/recreate 하던 것을
  build-once + update('none') 로 전환. 카드 구조·캔버스·Chart 인스턴스 1회 생성 후 값/시계열만 갱신.
  검증: 필터 변경 전후 _kpiSparkInstances[0] 동일 인스턴스(재생성 없음).
- dead code: 레거시 고급분석 모달 클러스터 제거 — showAdvancedAnalysis + renderParallelCoordinates +
  render3DPoC (3함수 127줄) + applyFilters 내 #advanced-viz 자동 리렌더 죽은 블록. analytics view(renderAdvParallel 등)로 대체됨.
- a11y: 뉴스 출처 필터 칩을 role=tab/aria-selected(별도 패널 없는데 tab 의미 오용) → role=group + aria-pressed 토글로 정정.

## 21. v1.8 (2026-06-16) — 신규상장 라이브 파이프라인 (XLSX 비의존) + UI

### 신규상장 라이브 수집 (XLSX 비의존, 30분 캐시)
문제: 마지막 상장사가 4/30(XLSX 수기 갱신 의존). 무료 단일 소스로는 "신규상장 + 분석 스키마" 완전 확보 불가
(실측: fdr 상장일 컬럼 없음 / KRX OpenAPI 종목 base-info 401 / 38 데스크탑 SSL 실패 / 네이버 신규상장은 ETN·ETF 노이즈).
→ 조합 파이프라인 `scripts/listings_client.py`:
  - 38(m.38.co.kr) fund.php: 공모주 정체성 + 확정/희망 공모가 + 주관사 (리스트행에 공모가 → 상세 fetch 생략)
  - fdr.StockListing("KRX"): 종목명→종목코드/시장/현재가, 매칭=상장확인 (30분 캐시)
  - KIS get_daily: 첫 거래일=상장일, 시초/종가/고가 → 첫날·고가 수익률
  - since(2026-04-30) 이후 상장분만, 미상장(예정)은 skip(오늘 보드가 담당)
- `serve.py`: `/api/listings/recent` (TTL 30분, ?refresh=1) + warmup 데몬 스레드.
- `index.html`: `mergeRecentListings()` — baseline(ipo-recent.js)에 라이브 병합(이름 dedupe, AI점수 캐시 무효화, 재렌더). serve 미가동 시 baseline 만(graceful).
- 검증: 4건 라이브 수집 (피스피스스튜디오 6/08 +48.8% / 마키나락스 5/20 +300% / 폴레드 5/14 / 코스모로보틱스 5/11).
- 한계: 38 롤링 윈도 ∩ fdr ∩ KIS 범위(현재 4건). competition/락업/공모금액은 null(38 상세 미파싱). KIS 필수(상장일).

### 런타임 XLSX 비의존
- 런타임은 커밋된 baseline(ipo-recent.js) + 라이브 신규상장. XLSX 파일 불필요(Render에서도 동작).

### UI
- 기본 테마 라이트 (이전 dark) — 부트스트랩 기본값 light, 저장값 'dark'만 dark.
- 사이드바 토글: 데스크탑 접기(본문 240px 회수)/모바일 오버레이, 상태 localStorage 영속, aria-expanded.

### 잔여
- 38 상세 파서 확장(경쟁률·공모금액·의무보유) → live record 필드 보강.
- 신규상장 커버리지 확대(38 윈도 밖 종목) 소스 보강 검토.
