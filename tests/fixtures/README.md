# 테스트 픽스처

`sample-workspace/` 는 IP 리스크를 **의도적으로 심어둔** 가상 프로젝트다.
탐지기를 이 폴더에 돌려 `expected.json` 과 대조한다.

## 심어둔 리스크 6건

| ID | 위치 | 심은 것 | 기대 판정 |
|----|------|---------|-----------|
| D-01 | `requirements.txt` | PyQt5 (GPL-3.0) | FORBIDDEN |
| D-02 | `requirements.txt` | mysqlclient (GPL-2.0-or-later) | FORBIDDEN |
| D-03 | `requirements.txt` | paramiko (LGPL-2.1) | RESTRICTED |
| D-04 | `src/vendor/gpl_helper.py` | GPL-3.0 헤더가 붙은 파일 | FORBIDDEN |
| D-05 | `src/vendor/mystery_util.py` | 헤더·출처 없는 vendor 코드 | REVIEW |
| D-06 | `README.md` | "MIT이며 공개 의무 없음" 허위 표기 | FORBIDDEN |

**D-02는 회귀 테스트를 겸한다.** 한국저작권위 API의 `optFinishLicenseYn` 플래그는
GPLv2를 누락하므로, 이 항목이 통과하면 SPDX 자체 분류표가 살아있다는 뜻이다.

**D-06이 이 프로젝트의 차별점**이다. 기존 SCA는 의존성만 보고 문서는 읽지 않는다.

## 오탐하면 안 되는 것 5건

`requests`(Apache-2.0), `numpy`(복합 SPDX 표현식), `chardet`(0BSD),
`src/utils.py`(SPDX MIT 정상), `src/app.py`(헤더 없는 자체 코드).

특히 `numpy` 는 deps.dev 가 `"0BSD AND BSD-3-Clause AND CC0-1.0 AND MIT AND Zlib"`
형태의 **표현식**으로 반환한다. 단일 ID로 가정한 파서는 여기서 깨진다.

`src/app.py` 는 헤더가 없지만 자체 코드다. `vendor/` 밖의 헤더 없는 파일을
전부 REVIEW 로 올리면 오탐이 폭발한다.

## 라이선스 정보 출처

패키지 라이선스는 deps.dev API 실측값(2026-08 기준)이다. 추정이 아니다.

## 기준 라이선스

`baseline_license: PROPRIETARY` — 비공개 상용 배포를 가정한다.
이 값이 바뀌면 D-01~D-04 의 판정도 함께 바뀐다.
