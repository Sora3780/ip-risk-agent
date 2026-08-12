"""특허 판정 회귀 테스트.

같은 도메인(보이스피싱)의 기획서를 유사도 3단계로 벌려두고,
판정이 그 차이를 구분하는지 본다.

  1-blatant    실제 등록특허의 기술 구성을 그대로 서술  -> HIGH 여야 한다
  2-partial    일부 구성은 같고 새 구성이 추가됨        -> HIGH/MEDIUM
  3-ambiguous  문제만 같고 기술 경로가 다름             -> HIGH 면 안 된다

P-01 과 P-03 이 갈리는지가 핵심이다. 둘 다 HIGH 면 도메인만 보고 있는 것이다.

사용: python scripts/test_patents.py
"""

from __future__ import annotations

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

BASE = ROOT / "tests" / "fixtures" / "patent-cases"
CACHE = ROOT / "data" / "kipris_cache.json"


def load_env() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            if v.strip():
                os.environ.setdefault(k.strip(), v.strip())


def main() -> int:
    load_env()
    from ipagent.patent import screen_document
    from ipagent.quota import status

    spec = json.loads((BASE / "expected.json").read_text(encoding="utf-8"))
    print("예산(전):", status(), "\n")

    failures = 0
    summary = []
    for case in spec["cases"]:
        doc = (BASE / case["dir"] / "docs" / "기획서.md").read_text(encoding="utf-8")
        result = screen_document(doc, per_query=5, max_assess=6, cache_path=CACHE)

        hits = result["findings"]
        top = hits[0]["verdict"]["similarity"] if hits else "UNRELATED"
        apps = [h["patent"].application_number for h in hits]

        problems = []
        if top not in case["expect_similarity"]:
            problems.append(f"최고 유사도 {top} (기대 {'/'.join(case['expect_similarity'])})")
        for want in case.get("expect_hits_include", []):
            if want not in apps:
                problems.append(f"기대한 특허 {want} 미검출")
        for bad in case.get("expect_not_similarity", []):
            if top == bad:
                problems.append(f"{bad} 로 과대 판정")
        ungrounded = [h for h in hits if not h["verdict"]["grounded"]]
        if ungrounded:
            problems.append(f"grounded=false {len(ungrounded)}건")

        mark = "PASS" if not problems else "FAIL"
        if problems:
            failures += 1
        print(f"[{mark}] {case['id']}  {case['label']:8s} ({case['product']})")
        print(f"       후보 {result['candidates']}건 / 판정 {result['assessed']}건 "
              f"/ 유사 {len(hits)}건 / 최고 {top}")
        for h in hits[:3]:
            p, v = h["patent"], h["verdict"]
            print(f"         {v['similarity']:7s} {p.application_number}  "
                  f"{(p.title_ko or p.title_en)[:44]}")
            print(f"                 겹침: {v['overlap_ko'][:100]}")
            print(f"                 차이: {v['difference_ko'][:100]}")
        for p_ in problems:
            print(f"       ! {p_}")
        print()
        summary.append({"id": case["id"], "label": case["label"], "top": top,
                        "hits": len(hits), "candidates": result["candidates"],
                        "applications": apps, "problems": problems})

    print("=" * 64)
    print(f"{len(spec['cases']) - failures}/{len(spec['cases'])} 통과")
    print("예산(후):", status())
    (ROOT / "data" / "patent_cases.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
