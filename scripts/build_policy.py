"""
SPDX 기반 copyleft 분류표를 만들고, 한국저작권위원회 84종에 매핑한다.

배경:
  API가 주는 optFinishLicenseYn 플래그는 GPLv2를 누락하고 ISC를 오탐한다.
  (84종 전수 검증 결과 — SECTION 11.1 참조)
  따라서 위험 판정은 아래 자체 분류표로 하고,
  한국저작권위 keyFeature 는 "근거 인용문"으로만 쓴다.

기준 프로젝트: 비공개 상용 배포 (MVP 고정값)

출력: data/license_policy.json
"""

import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 기준(비공개 상용 배포) 대비 위험도.
# 패턴은 라이선스 '이름'에 대한 소문자 부분일치 규칙이다.
TIERS = [
    (
        "FORBIDDEN",  # 결합/배포 시 전체 소스 공개 의무 → 비공개 상용과 양립 불가
        [
            "affero",
            "gnu general public license",
            "open software license",
            "reciprocal public license",
            "common public attribution",
            "sleepycat",
            "european union public",
            "cecill",  # 프랑스판 GPL. GPL 호환 strong copyleft
        ],
    ),
    (
        "RESTRICTED",  # 파일/모듈 단위 공개 의무 → 조건부 사용 가능, 검토 필요
        [
            "lesser general public",
            "library or lesser",
            "mozilla public license",
            "eclipse public license",
            "common development and distribution",
            "common public license",
            "ibm public license",
            "microsoft reciprocal",
            "apple public source",
            "sun public license",
            "nokia open source",
            "lucent public license",
            "ricoh source code",
            "realnetworks public source",
            "jabber open source",
            "motosoto",
            "cua office public",
            "oclc research public",
            "sybase open watcom",
            "computer associates trusted",
            "non-profit open software",
            # 아래는 UNKNOWN 수동 분류분. 애매하면 안전한 쪽(상위 티어)으로 올린다.
            "wxwindows",  # LGPL + 바이너리 배포 예외
            "simple public license",  # GPL 계열 단순화판
            "nasa open source agreement",  # 수정본에 상호주의 의무
            "mitre collaborative virtual workspace",  # MPL 파생
            "latex project public license",  # 재배포 조건 존재
            "sil open font license",  # 파생 폰트 동일 라이선스
            "ipa font license",  # 파생 폰트 동일 라이선스
        ],
    ),
    (
        "NOTICE",  # 고지 의무만 → 저작권 표기·라이선스 사본 첨부로 해소
        [
            "mit license",
            "bsd",
            "apache",
            "isc license",
            "zlib",
            "artistic license",
            "academic free license",
            "microsoft public license",
            "python software foundation",
            "w3c",
            "boost",
            "postgresql",
            "ncsa",
            "x.net",
            "vovida",
            "zope public",
            "intel open source",
            "adaptive public",
            "attribution assurance",
            "eiffel forum",
            "entessa",
            "fair license",
            "frameworx",
            "lucid",
            "naumen",
            "nethack",
            "open group test suite",
            "php license",
            "qt public",
            "sun industry standards",
            "university of illinois",
            # UNKNOWN 수동 분류분 - 고지 의무만 있는 permissive
            "educational community license",  # Apache-2.0 기반
            "historical permission notice",  # HPND
            "eu datagrid",
            "multics license",
            "python license",  # CNRI
            "miros license",
            "ntp license",
        ],
    ),
    (
        "REVIEW",  # 자동 판정 불가 - 사람이 봐야 함
        ["other/proprietary"],
    ),
]

# 이름 → SPDX 식별자 (근거 인용/보고서 표기에 사용)
SPDX_HINTS = {
    "gnu general public license (gplv2)": "GPL-2.0-only",
    "gnu general public license version 3.0 (gplv3)": "GPL-3.0-only",
    "gnu library or lesser general public license (lgplv2)": "LGPL-2.1-only",
    "gnu library or lesser general public license version 3.0 (lgplv3)": "LGPL-3.0-only",
    "affero gnu public license": "AGPL-3.0-only",
    "mit license": "MIT",
    "apache license 2.0": "Apache-2.0",
    "apache software license 1.1": "Apache-1.1",
    "bsd 3-clause": "BSD-3-Clause",
    "bsd 2-clause": "BSD-2-Clause",
    "new and simplified bsd licenses": "BSD-3-Clause",
    "isc license": "ISC",
    "mozilla public license version 2.0": "MPL-2.0",
    "mozilla public license 1.1 (mpl)": "MPL-1.1",
    "mozilla public license 1.0 (mpl)": "MPL-1.0",
    "eclipse public license": "EPL-1.0",
    "common development and distribution license": "CDDL-1.0",
    "common public license 1.0": "CPL-1.0",
    "microsoft public license (ms-pl)": "MS-PL",
    "microsoft reciprocal license (ms-rl)": "MS-RL",
}


def classify(name: str) -> str:
    low = name.lower().strip()
    for tier, patterns in TIERS:
        if any(p in low for p in patterns):
            return tier
    return "UNKNOWN"


def main() -> int:
    licenses = json.loads((ROOT / "data" / "licenses.json").read_text(encoding="utf-8"))
    policy = []
    for lic in licenses:
        low = lic["name"].lower().strip()
        policy.append(
            {
                "lid": lic["lid"],
                "name": lic["name"],
                "spdx": SPDX_HINTS.get(low, ""),
                "tier": classify(lic["name"]),
                # 근거 인용문. 리포트에서 이 텍스트를 그대로 인용한다.
                "evidence_ko": lic["key_feature"] or lic["summary_ko"],
                "source": "한국저작권위원회 오픈소스SW 라이선스정보 서비스",
            }
        )

    out = ROOT / "data" / "license_policy.json"
    out.write_text(json.dumps(policy, ensure_ascii=False, indent=2), encoding="utf-8")

    counts: dict[str, int] = {}
    for p in policy:
        counts[p["tier"]] = counts.get(p["tier"], 0) + 1
    print(f"저장: {out} ({len(policy)}종)\n")
    for tier in ("FORBIDDEN", "RESTRICTED", "NOTICE", "UNKNOWN"):
        print(f"  {tier:11s} {counts.get(tier,0):3d}종")

    print("\n[검증 - 반드시 이렇게 나와야 함]")
    expect = {
        "GNU General Public License (GPLv2)": "FORBIDDEN",
        "GNU General Public License version 3.0 (GPLv3)": "FORBIDDEN",
        "Affero GNU Public License": "FORBIDDEN",
        "GNU Library or Lesser General Public License (LGPLv2)": "RESTRICTED",
        "Mozilla Public License version 2.0": "RESTRICTED",
        "MIT License": "NOTICE",
        "Apache License 2.0": "NOTICE",
        "ISC License": "NOTICE",
    }
    by_name = {p["name"]: p for p in policy}
    ok = True
    for name, want in expect.items():
        got = by_name.get(name, {}).get("tier", "MISSING")
        mark = "PASS" if got == want else "FAIL"
        if got != want:
            ok = False
        print(f"  [{mark}] {name[:52]:54s} {got}")

    unknown = [p["name"] for p in policy if p["tier"] == "UNKNOWN"]
    if unknown:
        print(f"\n[UNKNOWN {len(unknown)}종 - 수동 분류 필요]")
        for n in unknown:
            print(f"  - {n}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
