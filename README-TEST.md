# 파이프라인 검증 파일 안내

이 폴더의 파일들은 IP Risk Agent 의 분석 파이프라인을 검증하기 위한 것이다.
감시 중인 GitHub 저장소(main)에 push 하면 각각 아래 경로를 자극한다.

| 파일 | 자극하는 경로 | 기대 결과 |
|---|---|---|
| `requirements.txt` | 라이선스 분석 (manifest) | AGPL/GPL 의존성 → 정책 충돌, LGPL → 검토 필요, Apache → 고지 의무 |
| `vendor/pdf_render.py` | 라이선스 분석 (SPDX 스캔) | 파일 내 AGPL-3.0 헤더 검출 |
| `docs/제품기획서_스마트스크롤.md` | 특허 분석 (KIPRIS + Gemini) | 시선 추적·잠금 해제류 국내 특허와 유사성 검토 |

## 의존성별 예상 판정

- `PyMuPDF` — **AGPL-3.0** (강한 카피레프트, 정책 충돌 예상)
- `ansible-core` — **GPL-3.0** (강한 카피레프트)
- `mysql-connector-python` — **GPL-2.0** (강한 카피레프트)
- `chardet`, `paramiko` — **LGPL-2.1** (약한 카피레프트, 검토 필요)
- `requests` — Apache-2.0 (고지 의무, 대조군)
- `requests-oauthlib` — 버전 미고정 (VERSION_RANGE_NOT_PINNED 검출 확인용)

이 파일들은 실제 제품 코드가 아니다. 검증이 끝나면 지워도 된다.
