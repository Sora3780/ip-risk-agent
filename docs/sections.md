# 제출 문서 초안 — SECTION 06 / 08 / 09 / 11

작성 기준일: 2026-08-09 · 모든 수치는 실측값이다.
측정 대상: `tests/fixtures/sample-workspace` (심어둔 리스크 6건 / 오탐 방지 5건)

---

# SECTION 06 · 시스템 아키텍처와 API 설계

## 6.1 시스템 아키텍처 다이어그램

```
[로컬 워크스페이스]                    [Cloud Run]
  파일 저장 감지                  ┌──────────────────────────┐
  (watchdog, 3~5초 디바운스)      │  FastAPI                 │
        │                        │   POST /api/scan         │
        │  변경 파일 POST  ─────► │        │                 │
        │                        │        ▼                 │
        │                        │  ① 규칙 게이트            │
        │                        │   의존성 파싱 / 헤더 스캔  │
        │                        │   SPDX 분류표 조회        │
        │                        │        │ FORBIDDEN 등만   │
        │                        │        ▼                 │
        │                        │  ② RAG 검색              │
        │                        │   라이선스 필터 + 코사인   │
        │                        │        │ 관련 조항 3건    │
        │                        │        ▼                 │
        │                        │  ③ Gemini 검토           │
        │                        │   구조화 출력 + grounded  │
        │                        └───────────┬──────────────┘
        │                                    ▼
   [Streamlit 대시보드] ◄───────────  findings.json
   (업로드 경로 = 시연 대체 경로)
                    ▲
        [외부] 한국저작권위원회 _GW · deps.dev · KIPRIS(KPA + 국문초록)
```

특허 검토는 같은 구조에 Grounding Source 만 교체한 경로다. 비용이 커서 자동이 아니라
`POST /api/screen-patent` 로 명시 호출한다.

```
기획서 문서
  → Gemini 검색어 추출 (2~3단어 영문 × 5~6개)
  → KPA anySearch          검색  → 출원번호
  → KpaBibliographicService 조회 → 영문초록
  → KorAbstractInfoService  조회 → 국문 명칭
  → Gemini 유사도 판정 (HIGH/MEDIUM/LOW/UNRELATED)
```

**핵심 설계: 2단 게이트.** ①은 비용 0의 규칙 판정이고, 통과한 건에만 ②③이 붙는다.
Gemini 무료 등급이 10 RPM 이라 게이트 없이 저장할 때마다 호출하면 즉시 막힌다.

## 6.2 컴포넌트

| 컴포넌트 | 기술/서비스 | 책임 | 입력 | 출력 | 상태 |
|---|---|---|---|---|---|
| Watcher | Python `watchdog` | 로컬 폴더 변경 감지, 디바운스 | 파일시스템 이벤트 | 변경 파일 목록 | 예정 |
| Web UI | Streamlit | 리스크 타임라인, 파일 업로드 | 사용자 조작 | 리포트 화면 | 예정 |
| Backend API | FastAPI (Cloud Run) | 입력 정규화, 오케스트레이션 | `POST /api/scan` | findings JSON | 예정 |
| 규칙 게이트 | `ipagent.detect` + `ipagent.policy` | 라이선스 탐지·등급 판정 | 파일/의존성 | tier + locator | **구현 완료** |
| RAG | `ipagent.rag` (`gemini-embedding-001`) | 관련 조항 검색 | 상황 질의 + SPDX | 조항 3건 + 출처 | **구현 완료** |
| Gemini | `gemini-3-flash-preview` | 근거 종합, 조치 생성 | 판정 + 검색 조항 | 구조화 JSON | **구현 완료** |
| Data/API | 한국저작권위 `_GW`, deps.dev | 라이선스 원문·패키지 라이선스 | 라이선스 ID / 패키지명 | 의무사항 / SPDX | **연동 완료** |

## 6.3 API 명세

| Method | Endpoint | 목적 | 주요 요청 | 주요 응답/오류 |
|---|---|---|---|---|
| GET | `/health` | 상태 확인 | - | `{status, index_loaded, model}` |
| POST | `/api/scan` | 워크스페이스 검사 | `{workspace_id, changes:[{path, change_type, content}]}` | `{findings:[...], stats}` / 400 스키마오류, 429 레이트리밋, 502 LLM실패 |
| GET | `/api/findings/{workspace_id}` | 누적 리스크 조회 | - | 타임라인 |

**입력 어댑터 추상화** — watcher · GitHub webhook · 수동 업로드가 모두 `POST /api/scan`
하나로 정규화된다. 입력 소스를 추가해도 백엔드는 바뀌지 않는다.

## 6.4 오류 처리

| 상황 | 처리 |
|---|---|
| deps.dev 응답 없음 | 해당 패키지 `REVIEW` 로 표시, 스캔은 계속 |
| deps.dev `non-standard` | 기본 버전으로 재조회, **추정임을 리포트에 명시** |
| Gemini 호출 실패 | 규칙 판정만으로 리포트 발행, `_error` 기록 |
| RAG 인덱스 없음 | `FileNotFoundError` 로 조기 실패 (조용한 품질 저하 금지) |

---

# SECTION 07 · 데이터 및 데이터 모델 설계

## 7.1 사용 데이터

| 데이터/API | 제공기관 | 수집 방식·주기 | 핵심 필드 | 품질 이슈 | 이용 조건 |
|---|---|---|---|---|---|
| **오픈소스SW 라이선스정보 `_GW`** | 한국저작권위원회 (공공데이터포털) | REST, 최초 1회 전량 덤프 후 파일 보관 | `licenseName`, `lid`, `summaryKo`, `keyFeature`, `opt*Yn` | HTML 조각(`<P>`,`<BR>`,`&nbsp;`)이 섞여 옴. **`opt*Yn` 플래그는 GPLv2 누락·ISC 오탐** | 개발계정 일 1,000회. 출처 표시 조건 |
| **deps.dev** | Google | REST, 검사 시점마다 조회 + 로컬 캐시 | SPDX 라이선스 표현식, 기본 버전 | 복합 표현식(`A AND B AND C`) 반환. 구버전은 `"non-standard"` | 무료, 인증 불필요 |
| **한국특허영문초록(KPA)** | 지식재산처 (KIPRIS Plus) | REST, 기획서 검토 시 호출 + 캐시 | `applicationNo`, `inventionName`, `astrtCont` | **검색 응답에 초록이 없어** 서지정보를 따로 조회해야 함 | 무료 월 1,000회 |
| **기계번역용 국문초록** | 지식재산처 (KIPRIS Plus) | REST, 출원번호 단위 조회 | `inventionName`(국문) | **국문 '명칭'만 제공. 초록 본문 없음** | 무료 월 1,000회 |
| SPDX License List | SPDX (Linux Foundation) | 참조용 (식별자 체계) | SPDX ID | — | CC-BY-3.0 |

## 7.2 전처리·검증 규칙

| 대상 | 규칙 |
|---|---|
| 라이선스 원문 | `<BR>`→개행, `<P>` 제거, `&nbsp;`→공백, 연속 공백 압축 (`strip_html()`) |
| SPDX 식별자 | `-only` / `-or-later` 접미사 제거 후 대조. `AND`/`OR`/`WITH` 표현식은 원자 단위로 분해해 **가장 무거운 등급**을 채택 |
| 의존성 버전 | **`==` 로 고정된 것만** 버전으로 인정. `>=3.5` 를 고정으로 오해하면 없는 버전을 조회하게 됨 |
| deps.dev 미해결 | `"non-standard"` 반환 시 기본 버전으로 재조회하고 **추정임을 리포트에 명시** |
| 특허 검색어 | KIPRIS 는 단어를 AND 로 묶음. **2~3단어 강제**, 초과 시 앞 3단어만 사용 |
| 문서 선별 | 특허 검토 대상은 `기획·제안·명세·설계·proposal·spec·prd` 패턴 파일만. README·LICENSE 제외 |

## 7.3 데이터 모델

```
licenses.json          원본 84종        {lid, name, summary_ko, key_feature, flags{}}
license_policy.json    판정표 84종      {lid, name, spdx, tier, evidence_ko, source}
rag_index.npz          벡터 149×768     float32, L2 정규화
rag_meta.json          청크 메타 149    {id, lid, name, spdx, tier, section, text}
depsdev_cache.json     패키지 캐시      {"pkg@ver": {expr, meta{estimated, license_from_version}}}
kipris_cache.json      특허 응답 캐시    {"url?params": "<xml>"}

workspaces/{ws}/tree/       서버가 보관하는 워크스페이스 사본
workspaces/{ws}/findings.json   최신 검사 결과
workspaces/{ws}/history.jsonl   append-only 이력 {scanned_at, total, by_tier, locators}
```

`findings.json` 의 항목 하나:

```json
{
  "kind": "dependency | file_header | unknown_provenance | doc_mismatch",
  "locator": "requirements.txt:PyQt5",
  "license": "GPL-3.0",
  "tier": "FORBIDDEN",
  "why": "PROPRIETARY 배포 기준으로 GPL-3.0 는 FORBIDDEN 등급",
  "evidence_ko": "...",
  "evidence_source": "한국저작권위원회 ... — GPLv3",
  "llm": { "verdict": "CONFIRMED", "grounded": true, "obligations_ko": [], "actions_ko": [] }
}
```

## 7.4 데이터 위험 점검

| 위험 항목 | 해당 여부 | 대응 |
|---|---|---|
| 개인정보/민감정보 포함 | **아니오** | 검사 대상은 코드·문서. 사용자 개인정보를 수집·저장하지 않음 |
| 출처·라이선스 제한 | **예** | 공공누리 조건에 따라 출처 표시. 리포트 각 항목에 기관명 병기, SECTION 17 에 명시 |
| 편향 또는 대표성 부족 | **예** | 라이선스 84종은 주요 라이선스 위주라 마이너 라이선스는 미수록. 미수록 시 `REVIEW` 로 표시 |
| 최신성·품질 불확실 | **예** | 라이선스 데이터는 덤프 시점 고정(2026-08-09). 갱신 주기 미정 → 재덤프 스크립트 제공 |
| **소스코드 외부 전송** | **예** | 로컬 감시기가 파일 내용을 서버로 전송. 배포 시 사내망 한정 운영 또는 온프레미스 설치를 전제로 함 (SECTION 14 운영 주의사항) |

> 마지막 항목이 실서비스에서 가장 큰 제약이다. IP 리스크를 막으려고 소스를 외부로 보내는
> 구조라, 고객사가 받아들이기 어렵다. 온디바이스 판정 비중을 늘리는 것이 고도화 과제다.

---

# SECTION 08 · Gemini 기능 및 프롬프트 설계

## 8.1 AI 기능

| AI 기능 | 모델/설정 | 입력 | 출력 형식 | 일반 로직과의 경계 |
|---|---|---|---|---|
| 리스크 검토·설명 | `gemini-3-flash-preview` / temp 0.2 / `response_schema` 강제 | 규칙 판정 + RAG 검색 조항 3건 | JSON (6필드 고정) | **등급 판정은 LLM이 하지 않는다.** 규칙 엔진이 이미 내린 판정을 검토·설명할 뿐 |
| 조치 제안 | 동일 호출 | 위와 동일 | `actions_ko[]` | 법적 결론은 내리지 않음. `needs_legal_review` 로 위임 |
| **특허 검색어 추출** | 동일 모델 / temp 0.3 | 기획서 본문 | `{core_idea_ko, queries[{en,ko,aspect}]}` | 검색은 KIPRIS 가 한다. LLM 은 한국어 기획서를 영문 특허 용어로 옮기는 역할 |
| **특허 유사도 판정** | 동일 모델 / temp 0.2 | 핵심 아이디어 + 특허 초록 1건 | `{similarity, overlap_ko, difference_ko, needs_expert_review, grounded}` | **침해 여부를 판정하지 않는다.** 유사도와 "조사가 필요한 지점"까지 |

**LLM이 하지 않는 것**
- 라이선스 등급 판정 (SPDX 분류표가 담당)
- 침해 여부에 대한 법적 결론
- 근거 문서에 없는 조항·판례 인용

## 8.2 핵심 프롬프트

시스템 프롬프트 (`src/ipagent/llm.py`):

```
당신은 소프트웨어 프로젝트의 오픈소스 라이선스 리스크를 설명하는 도우미다.

역할
- 규칙 엔진이 이미 내린 등급 판정을 검토하고, 개발팀이 이해할 수 있게 설명한다.
- 제공된 "근거 문서"에 실제로 적힌 내용만 사용한다.

금지
- 근거 문서에 없는 조항, 판례, 법조문을 지어내지 않는다.
- 침해 여부에 대한 법적 결론을 내리지 않는다. 판단은 "검토가 필요한 지점"까지다.
- 근거가 부족하면 verdict 를 NEEDS_REVIEW 로 하고 grounded 를 false 로 둔다.

불확실성 처리
- 근거 문서가 비어 있거나 질문과 무관하면 추측하지 말고 그 사실을 explanation_ko 에 적는다.
```

사용자 프롬프트 템플릿:

```
## 검사 대상 프로젝트
배포 형태: {baseline}

## 규칙 엔진 판정
- 위치: {locator}
- 라이선스: {license}
- 등급: {tier}
- 판정 사유: {why}

## 근거 문서 (출처: {source})
{evidence}

## 요청
위 근거만 사용해 이 항목을 검토하고 지정된 JSON 형식으로 답하라.
근거 문서가 비어 있으면 grounded=false, verdict=NEEDS_REVIEW 로 하라.
```

출력 스키마 (`response_schema` 로 강제):

| 필드 | 타입 | 용도 |
|---|---|---|
| `verdict` | enum(CONFIRMED / NEEDS_REVIEW / NOT_A_RISK) | 규칙 판정 동의 여부 |
| `explanation_ko` | string | 왜 위험한지 2~3문장 |
| `obligations_ko` | string[] | 근거에 명시된 의무사항만 |
| `actions_ko` | string[] | 구체적 조치 |
| `needs_legal_review` | boolean | 전문가 검토 필요 여부 |
| `grounded` | boolean | **근거만으로 판단했는지 자기보고** |

## 8.3 프롬프트 테스트 (실측)

| 테스트 ID | 대표 입력 | 기대 결과 | 실제 결과 | 판정 |
|---|---|---|---|---|
| P-01 | 정상 — GPL-3.0 의존성 + 해당 조항 | CONFIRMED, grounded=true | CONFIRMED / grounded=true. GPL 제7조·설치정보 조항 인용 | 통과 |
| P-02 | 정보 부족 — 라이선스 미상 파일(`mystery_util.py`) | 모른다고 표현 | NEEDS_REVIEW / 근거 없음 명시 | 통과 |
| P-03 | **잘못된 근거** — GPL 리스크에 MIT 조항 제공 | 추측 금지, 근거 부족 신고 | grounded=**false**, *"제공된 근거 문서에는 MIT 의무사항만 있을 뿐 GPL-3.0 관련 내용이 포함되어 있지 않습니다"* | 통과 |
| P-04 | 모델 오류 — 존재하지 않는 모델 ID 호출 | 안전 실패 | 규칙 판정만으로 리포트 발행, `_error` 기록, 파이프라인 유지 | 통과 |

> **P-03 은 실제 개발 중 발생한 버그로 검증됐다.** `doc_mismatch` 항목에 선언 라이선스(MIT)의
> 근거를 넘기는 실수가 있었고, 모델이 `grounded=false` 로 거부했다. 근거 라우팅을 충돌
> 라이선스(GPL) 쪽으로 고친 뒤 `grounded=true` 로 전환됐다. 환각 대신 거부가 나온 사례다.

## 8.4 환각·안전·오류 대응

| 위험 | 대응 |
|---|---|
| 근거 없는 답변 | `grounded` 필드 자기보고. false 면 리포트에 "검토 필요"로 표시하고 조치를 제안하지 않음 |
| 무관한 근거 검색 | RAG 검색을 문제 라이선스로 1차 필터링. 그래도 새면 `grounded` 가 2차 방어 |
| 법적 결론 월권 | 프롬프트에 명시적 금지 + `needs_legal_review` 강제 출력 |
| 출력 파싱 실패 | `response_schema` 로 구조 강제. 그래도 실패하면 예외 처리로 규칙 판정 폴백 |
| API 실패 / 레이트리밋 | 10 RPM 스로틀. 실패 시 규칙 판정만으로 리포트 발행 |

---

# SECTION 09 · RAG 설계와 검색 품질

## 9.1 설계 항목

| 설계 항목 | 팀의 선택 | 선택 이유 / 검증 방법 |
|---|---|---|
| 지식 문서 범위 | 한국저작권위원회 오픈소스SW 라이선스정보 **84종**의 요약·의무사항 | 판정 대상이 오픈소스 라이선스로 한정됨. 한국어 원문이라 리포트 인용에 그대로 쓸 수 있음 |
| 문서 정제 | `<P>`/`<BR>`/`&nbsp;` HTML 조각 제거, 공백 정규화 | API 응답이 HTML 파편으로 옴. `strip_html()` 로 처리 |
| Chunk 전략 | `주요특징` / `배포시의무사항` 머리말 기준 분할 후 **320자** 단위, 총 **149청크** | 조항이 줄 단위로 나열돼 있어 의미 경계가 명확함. 라이선스명을 청크 본문에 포함시켜 검색 시 라이선스가 구분되게 함 |
| Embedding / Vector Store | `gemini-embedding-001`, **768차원**(MRL 절단 후 재정규화), numpy `.npz` **416KB** | 청크 149개 규모에 벡터DB는 과함. 파일 하나로 배포·재현이 단순해짐 |
| 검색 방식 | **하이브리드** — ① 문제 라이선스로 후보 필터 ② 코사인 top-3 | 순수 의미검색은 유사 라이선스(GPL↔LGPL)를 혼동함. 필터가 정확도를, 코사인이 관련성을 담당 |
| 재정렬/후처리 | 사용 안 함 | 후보가 라이선스당 1~5청크로 이미 작음 |
| 출처 표시 | 라이선스명 + 섹션명 + 기관명을 리포트에 병기 | 예: `한국저작권위원회 오픈소스SW 라이선스정보 서비스 — GNU Library or Lesser General Public License (LGPLv2)` |

## 9.2 RAG 처리 흐름

```
탐지 건 (locator, license, tier, why)
   │
   ├─ 원인 라이선스 결정   ← doc_mismatch 는 선언(MIT)이 아니라 충돌(GPL) 쪽
   │
   ├─ 상황 질의 생성
   │    "PROPRIETARY 배포 프로젝트에서 {locator} 의 {license} 라이선스가
   │     {tier} 로 판정됨. {why}"
   │
   ├─ ① 후보 필터   SPDX 접미사(-only/-or-later) 정규화 후 라이선스 일치 청크만
   ├─ ② 질의 임베딩  task_type=RETRIEVAL_QUERY
   ├─ ③ 코사인 top-3
   │
   └─ 근거 텍스트 + 출처 → Gemini 프롬프트
```

## 9.3 평가 질문 (실측)

| 평가 질문 | 필터 | 검색된 근거 | 점수 | 답변 품질 |
|---|---|---|---|---|
| Q1 비공개 상용 제품에 넣어 배포하면 소스를 공개해야 하나 | GPL-3.0 | (배포시의무사항) "각 복제본에 저작권 고지…GPL 3.0의 조건 및 제7조의 조건을 있는 그대로 유지" | 0.703 | 정확 |
| Q2 동적 링크만 하면 소스 공개를 피할 수 있나 | LGPL-2.1 | (주요특징) **"LGPL 라이브러리를 이용한 응용프로그램의 경우 소스코드 제공없이 배포가능(제6조)"** | 0.715 | 정확 — 질문에 직접 답하는 조항 |
| Q3 재배포할 때 무엇을 지켜야 하나 | MIT | (배포시의무사항) "저작권 안내문구, MIT 라이선스 문구가 모든 복제본에 포함" | 0.745 | 정확 |
| Q4 특허 관련 조항이 있나 | Apache-2.0 | (배포시의무사항) "저작권, 특허, 상표, attribution에 대한 고지사항을 소스코드 또는 NOTICE 파일에 포함" | 0.705 | 정확 |
| Q5 **(네거티브)** 상표권 등록 절차는 | 없음 | Ms-PL / AFL 의 "상표 고지사항 유지" 조항 | 0.657 | **오답** — 지식범위 밖 질의인데 결과가 반환됨 |

**Q5 가 드러낸 한계.** 지식범위 밖 질의(0.657)와 정상 질의(0.703~0.745)의 점수 차가
0.05 수준이라 **코사인 임계값만으로는 걸러낼 수 없다.** 따라서 검색 단계에서 차단하지 않고,
LLM 의 `grounded` 자기검증을 2차 방어선으로 둔다. 실제로 P-03 에서 이 방어선이 작동했다.

향후 개선: 검색 결과를 넘기기 전 "이 조항이 질의에 답하는가"를 판별하는 단계 추가
(SECTION 16 백로그).

## 9.4 특허 경로 — RAG 가 아닌 이유

라이선스는 지식 범위가 84종으로 고정돼 있어 전량 임베딩이 가능하다.
특허는 국내 등록분만 수백만 건이라 자체 색인이 불가능하므로, **KIPRIS 검색 API 를
검색기로 쓰고 그 결과를 근거로 넘긴다.** 구조는 같고 검색 계층만 다르다.

| | 라이선스 | 특허 |
|---|---|---|
| 지식 범위 | 84종 (유한) | 수백만 건 (무한) |
| 검색 | 자체 벡터 인덱스 149청크 | KIPRIS `anySearch` |
| 질의 | 상황 문장 → 임베딩 | 기획서 → **Gemini 가 2~3단어 영문 검색어로 변환** |
| 근거 | 한국어 조항 원문 | 영문 초록 (국문은 명칭만 제공) |
| 커버리지 | 전량 | **후보 중 상위 N건만** — `coverage_note` 로 미판정 건수 명시 |

### 특허 검색 실측 (기획서: SafeCall 보이스피싱 탐지, 967자)

| 검색어 | 전체 건수 | 비고 |
|---|---|---|
| `voice phishing detection` | 69 | 적정 |
| `real-time call analysis` | 144 | 적정 |
| `on-device speech recognition` | 442 | 다소 넓음 |
| `fraudulent call classification` | **0** | 너무 좁음 |
| `automatic call termination` | 2,756 | 너무 넓음 |
| `emergency contact notification` | 74 | 적정 |

후보 25건 → 상위 8건 판정 → **MEDIUM 3건**, 전부 `grounded=true`.
세 건 모두 실제 등록 특허이며, 신호처리(GMM/SMV/LSF) 계열이라
문맥 분석 기반인 기획서와의 차이를 판정문이 정확히 짚었다.

**단어 수가 결과를 좌우한다.** 5단어 검색어는 전부 0건이었다.
`searchAny` 가 단어를 AND 로 묶기 때문이며, 프롬프트에 "2~3단어" 를 강제하고
초과 시 앞 3단어만 쓰도록 잘라서 해결했다.

---

# SECTION 11 · 구현 기록 · 코드 리뷰 · 변경 관리

## 11.1 진행 기록

| 진행 단계 | 완료한 내용 | 문제/막힘 | 결정·다음 행동 |
|---|---|---|---|
| 기획·설계 | 주제 확정, 범위 축소(라이선스 우선), 기준 라이선스 PROPRIETARY 고정 | 기존 제품(FOSSA·SCANOSS) 조사 결과 코드 라이선스 탐지는 레드오션 | 비코드 산출물(문서) 대조를 차별점으로 설정 |
| 데이터 확보 | 한국저작권위 `_GW` 84종 덤프, deps.dev 연동, RAG 149청크 인덱싱 | KIPRIS 검색 상품을 목록에서 찾지 못함 (50개 전수 확인) | 한국특허영문초록(KPA)이 검색 서비스임을 확인, 신청 완료. 승인 대기 |
| 핵심 기능 구현 | 규칙 게이트 · RAG · Gemini 전 구간 동작. 재현율 100% / 정밀도 100% | API 플래그 신뢰 불가, 근거 라우팅 버그, 모델 ID 오류 (아래 11.3) | 전부 수정 완료, 회귀 테스트 추가 |
| 배포·검증 | 미착수 | - | watcher → FastAPI → Streamlit → Cloud Run 순서 |

## 11.2 코드 리뷰

| 리뷰 항목 | 발견 내용 | 조치 | 반영 |
|---|---|---|---|
| 구조·책임 분리 | 판정·검색·생성이 한 함수에 섞일 뻔함 | `policy` / `rag` / `llm` 모듈 분리. 판정은 규칙, 설명은 LLM | [x] |
| 오류·예외 처리 | LLM 실패 시 스캔 전체가 죽는 구조였음 | `assess()` 를 예외 흡수형으로 변경, 규칙 판정 폴백 | [x] |
| 키·개인정보·보안 | API 키를 코드에 넣을 뻔함 | `.env` + `.gitignore`, `.env.example` 제공. 배포는 Secret Manager 예정 | [x] |
| 프롬프트/RAG 품질 | 라이선스 전체를 통째로 넘기면 무관 조항이 섞임 | 조항 단위 청킹 + 라이선스 필터 하이브리드 검색 | [x] |
| 재현성·문서화 | 심사자가 API 키 없이 실행 불가 | `licenses.json` / `license_policy.json` 커밋 (출처는 SECTION 17 표기) | [x] |
| 오탐 방지 | 무헤더 파일을 전부 위험으로 올리면 오탐 폭발 | `vendor/` 안에서만 출처불명 판정. 정밀도 100% 달성 | [x] |

## 11.3 주요 변경 결정

| # | 초기 계획 | 변경 내용 | 변경 근거 | 영향 범위 |
|---|---|---|---|---|
| 1 | 한국저작권위 API 의 `optFinishLicenseYn` 플래그로 copyleft 판정 | **SPDX 기반 자체 분류표**로 판정, 공공데이터는 근거 인용 전용 | 84종 전수 검증에서 **GPLv2 누락**(가장 흔하고 위험한 케이스), **ISC 오탐** 확인 | SECTION 06 컴포넌트, 08 경계 정의 |
| 2 | 법제처 국가법령정보 API 로 저작권법 조문 인용 | **제외** | 라이선스 위반의 근거는 법조문이 아니라 라이선스 조항 자체. 검색 노이즈만 증가 | 데이터 소스 1개 감소. 이미지·특허 확장 시 재검토 (SECTION 16) |
| 3 | GitHub Webhook 으로 변경 감지 | **로컬 watcher + Cloud Run 백엔드** 분리 | 커밋 시점은 기존 SCA 와 동일. 파일 저장 시점이 한 단계 앞서고 "조기 탐지"에 부합 | SECTION 06 아키텍처. 입력 어댑터 추상화로 webhook 은 나중에 추가 가능 |
| 4 | 탐지 건의 라이선스로 근거 조회 | `doc_mismatch` 는 **충돌 라이선스**로 조회 | 문서(MIT) 근거를 GPL 리스크에 넘겨 모델이 `grounded=false` 반환 | `_finding(cite_for=...)` 추가 |
| 5 | deps.dev 응답을 단일 SPDX ID 로 가정 | **표현식 파서** + 구버전 폴백 | numpy 가 `"0BSD AND BSD-3-Clause AND CC0-1.0 AND MIT AND Zlib"` 반환. 구버전 핀은 `"non-standard"` 반환 | 폴백 시 "추정임"을 리포트에 명시 |
| 6 | 모델 `gemini-3-flash` | `gemini-3-flash-preview` 고정 | 존재하지 않는 ID. 부분일치 검사가 이를 통과시켜 못 잡았음 → 정확일치로 수정 | 모델 검증 로직 |
| 7 | KPA 검색 응답을 `<item>` 으로 파싱, 초록도 함께 올 것으로 가정 | `<searchResult>` 로 파싱 + **서지정보 API 별도 조회** | 실측 결과 항목 태그가 다르고 검색 응답에 초록 필드가 없음. 첫 실행 0건의 원인 | `patent.py` 전면 수정 |
| 8 | 기획서 키워드를 5단어 구문으로 생성 | **2~3단어 강제** + 초과 시 절단 | `searchAny` 가 단어 AND. 5단어 검색어 5개가 모두 0건, 3단어는 69건 | 검색어 추출 프롬프트 |
| 9 | 국문초록으로 한국어 원문 인용 | **영문초록으로 판정, 설명만 한국어 생성** | `KorAbstractInfoService` 는 국문 '명칭'만 반환. 초록 본문 없음 | SECTION 07·09 서술 정정 |
| 10 | 후보 상위 N건만 조용히 판정 | `not_assessed` · `coverage_note` 로 **미판정 건수 명시** | "8건 중 3건"과 "25건 중 8건만 보고 3건"은 전혀 다른 주장. 침묵하면 전수 검사로 읽힘 | API 응답·UI·리포트 |

## 11.4 미해결 문제

| 문제 | 현재 상태 | 다음 조치 |
|---|---|---|
| 특허 후보 25건 중 8건만 판정 | `coverage_note` 로 미판정 건수를 리포트·화면에 명시 중 | 판정 한도를 늘리려면 KIPRIS 월 1,000회와 Gemini 10 RPM 을 함께 고려해야 함 |
| 국문 초록 본문을 얻을 수 없음 | 영문초록으로 판정, 설명만 한국어로 생성 | BULK 서비스(TXT)로 국문 요약서를 받아 자체 색인 (SECTION 16) |
| 특허 판정이 초록 기준 | 청구범위 미확인 | 프롬프트로 확정 표현 금지 + `needs_expert_review` 강제 |
| RAG 가 지식범위 밖 질의를 거르지 못함 | `grounded` 로 방어 중 | 관련성 판별 단계 추가 (SECTION 16) |
| 파일 해시 기반 출처 추적 미구현 | `vendor/` 헤더 유무로만 판단 | deps.dev hash query / Software Heritage 연동 (SECTION 16) |
| watcher · API · UI 미착수 | CLI 만 동작 | 3일차 작업 |
