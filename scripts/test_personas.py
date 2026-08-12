"""페르소나 회귀 테스트.

사용자 유형 5종으로 검사를 돌려, 각 유형에서 한 번 발견된 결함이
다시 새지 않는지 확인한다. 기대값은 실측으로 확정한 것이다.

  A-indie     Node+Python 혼합 — package.json 을 읽는가
  B-startup   의존성 다수      — deps.dev 가 포기한 AGPL 을 잡는가
  C-bootcamp  의존성 파일 없음 — 부분 검사임을 밝히는가
  D-pm        기획서만 있음    — 검사 불가임을 밝히는가
  E-empty     빈 폴더          — 빈 결과를 안전으로 오독하지 않는가

사용: python scripts/test_personas.py
"""

from __future__ import annotations

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from ipagent.scan import scan  # noqa: E402

BASE = ROOT / "tests" / "fixtures" / "personas"

# (페르소나, 기대 조건, 설명)
CASES = [
    (
        "A-indie",
        {
            "manifests": {"requirements.txt", "package.json"},
            "min_deps": 5,
            "scannable": True,
            "must_include": set(),
            "must_not_flag": {"package.json:express", "package.json:canvas"},
        },
        "npm 의존성을 읽지 못하면 JS 프로젝트가 통째로 사각지대가 된다",
    ),
    (
        "B-startup",
        {
            "manifests": {"requirements.txt"},
            "min_deps": 14,
            "scannable": True,
            # deps.dev 가 non-standard 를 반환하는 패키지다. PyPI 폴백이 없으면 놓친다.
            "must_include": {"requirements.txt:PyMuPDF"},
            "must_not_flag": {"requirements.txt:pandas", "requirements.txt:reportlab"},
        },
        "AGPL 패키지를 놓치면 최고 위험이 REVIEW 로 묻힌다",
    ),
    (
        "C-bootcamp",
        {"manifests": set(), "min_deps": 0, "scannable": True,
         "must_include": set(), "must_not_flag": set(), "expect_notes": True},
        "의존성 파일이 없으면 '부분 검사'임을 밝혀야 한다",
    ),
    (
        "D-pm",
        {"manifests": set(), "min_deps": 0, "scannable": False,
         "must_include": set(), "must_not_flag": set(), "expect_notes": True},
        "기획서만 있으면 검사 불가임을 밝혀야 한다",
    ),
    (
        "E-empty",
        {"manifests": set(), "min_deps": 0, "scannable": False,
         "must_include": set(), "must_not_flag": set(), "expect_notes": True},
        "빈 폴더의 0건을 '안전'으로 보고하면 안 된다",
    ),
]


def main() -> int:
    failures = 0
    for name, expect, why in CASES:
        report = scan(BASE / name, with_doc_check=True)
        cov = report["coverage"]
        flagged = {f["locator"] for f in report["findings"]}
        problems = []

        if set(cov["manifests"]) != expect["manifests"]:
            problems.append(f"매니페스트 {cov['manifests']} != {sorted(expect['manifests'])}")
        if cov["dependencies_resolved"] < expect["min_deps"]:
            problems.append(f"의존성 {cov['dependencies_resolved']}개 < {expect['min_deps']}개")
        if cov["scannable"] != expect["scannable"]:
            problems.append(f"scannable={cov['scannable']} != {expect['scannable']}")
        for loc in expect["must_include"] - flagged:
            problems.append(f"미탐: {loc}")
        for loc in expect["must_not_flag"] & flagged:
            problems.append(f"오탐: {loc}")
        if expect.get("expect_notes") and not cov["notes"]:
            problems.append("검사 범위 경고가 비어 있음")

        tiers = {}
        for f in report["findings"]:
            tiers[f["tier"]] = tiers.get(f["tier"], 0) + 1

        mark = "PASS" if not problems else "FAIL"
        if problems:
            failures += 1
        print(f"[{mark}] {name:12s} 탐지 {len(report['findings'])}건 {tiers or '{}'}"
              f"  의존성 {cov['dependencies_resolved']}개  scannable={cov['scannable']}")
        print(f"        {why}")
        for p in problems:
            print(f"        ! {p}")

    print(f"\n{len(CASES) - failures}/{len(CASES)} 통과")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
