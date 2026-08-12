"""특허 파이프라인 점검.

사용: python scripts/check_patent.py [문서경로]
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

DEFAULT_DOC = ROOT / "tests" / "fixtures" / "startup-workspace" / "docs" / "기획서.md"
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

    doc_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DOC
    document = doc_path.read_text(encoding="utf-8")
    print(f"문서: {doc_path.name} ({len(document)}자)\n")

    result = screen_document(document, per_query=5, max_assess=8, cache_path=CACHE)
    plan = result["plan"]

    print("=" * 64)
    print("1단계  기획서 -> 검색어")
    print("=" * 64)
    print(f"\n핵심 아이디어\n  {plan['core_idea_ko']}\n")

    print("=" * 64)
    print("2단계  KIPRIS 검색")
    print("=" * 64)
    for s in plan.get("search_stats", []):
        if "error" in s:
            print(f"  {s['query']:34s} 실패: {s['error']}")
        else:
            print(f"  {s['query']:34s} 전체 {s['total']:>6}건 -> {s['taken']}건 수집")
    print(f"\n중복 제거 후보 {result['candidates']}건 / 판정 대상 {result['assessed']}건")

    print("\n" + "=" * 64)
    print(f"3단계  유사도 판정 — 유의미한 특허 {len(result['findings'])}건")
    print("=" * 64)
    for r in result["findings"]:
        p, v = r["patent"], r["verdict"]
        mark = "[!!]" if v["similarity"] == "HIGH" else "[! ]"
        print(f"\n{mark} {v['similarity']}  {p.application_number}  ({p.application_date})")
        print(f"     {p.title_ko or p.title_en}")
        if p.title_ko and p.title_en:
            print(f"     EN: {p.title_en[:74]}")
        print(f"     출원인: {p.applicant[:50]}   IPC: {p.ipc[:40]}")
        print(f"     겹침: {v['overlap_ko'][:190]}")
        print(f"     차이: {v['difference_ko'][:190]}")
        print(f"     변리사 조사 필요={v['needs_expert_review']}  grounded={v['grounded']}")

    out = ROOT / "data" / "patent_screen.json"
    out.write_text(json.dumps({
        "document": doc_path.name,
        "core_idea_ko": plan["core_idea_ko"],
        "queries": plan["queries"],
        "search_stats": plan.get("search_stats", []),
        "candidates": result["candidates"],
        "findings": [{
            "application_number": r["patent"].application_number,
            "title_ko": r["patent"].title_ko,
            "title_en": r["patent"].title_en,
            "applicant": r["patent"].applicant,
            "application_date": r["patent"].application_date,
            "ipc": r["patent"].ipc,
            "abstract_en": r["patent"].abstract_en,
            "verdict": r["verdict"],
        } for r in result["findings"]],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n저장: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
