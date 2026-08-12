"""탐지 결과를 정답 세트(expected.json)와 대조해 재현율/정밀도를 낸다.

문서대조 OFF/ON 을 각각 돌려 차별점의 기여도를 숫자로 보여준다.
결과는 SECTION 05 성공 기준 / SECTION 13 테스트 표에 그대로 들어간다.

사용: python scripts/score.py
"""

from __future__ import annotations

import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from ipagent.scan import scan  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"


def norm(locator: str) -> str:
    return locator.strip().lower()


def evaluate(expected: dict, report: dict) -> dict:
    found = {norm(f["locator"]): f for f in report["findings"]}
    hits, misses = [], []

    for item in expected["must_detect"]:
        got = found.get(norm(item["locator"]))
        if got and got["tier"] == item["tier"]:
            hits.append((item, got, "정확"))
        elif got:
            hits.append((item, got, f"등급불일치({got['tier']})"))
        else:
            misses.append(item)

    expected_locs = {norm(i["locator"]) for i in expected["must_detect"]}
    false_positives = [f for k, f in found.items() if k not in expected_locs]

    exact = [h for h in hits if h[2] == "정확"]
    recall = len(exact) / len(expected["must_detect"])
    precision = len(exact) / len(found) if found else 0.0

    # 근거 인용률: 탐지 건 중 한국어 근거가 붙은 비율
    cited = sum(1 for f in report["findings"] if f.get("evidence_ko"))
    citation_rate = cited / len(report["findings"]) if report["findings"] else 0.0

    return {
        "hits": hits,
        "misses": misses,
        "false_positives": false_positives,
        "recall": recall,
        "precision": precision,
        "citation_rate": citation_rate,
        "total_findings": len(found),
    }


def run(with_doc_check: bool) -> dict:
    expected = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))
    report = scan(FIXTURES / expected["workspace"], with_doc_check)
    return evaluate(expected, report)


def main() -> int:
    lines: list[str] = []

    def out(s: str = "") -> None:
        lines.append(s)

    results = {}
    for label, flag in (("문서대조 OFF", False), ("문서대조 ON", True)):
        r = run(flag)
        results[label] = r
        out(f"===== {label} =====")
        out(f"  재현율   {r['recall']*100:5.1f}%   ({len([h for h in r['hits'] if h[2]=='정확'])}/6)")
        out(f"  정밀도   {r['precision']*100:5.1f}%   (탐지 {r['total_findings']}건)")
        out(f"  근거인용 {r['citation_rate']*100:5.1f}%")
        if r["misses"]:
            out("  미탐:")
            for m in r["misses"]:
                out(f"    - {m['id']} {m['locator']} ({m['tier']})")
        if r["false_positives"]:
            out("  오탐:")
            for f in r["false_positives"]:
                out(f"    - {f['locator']} ({f['tier']})")
        out()

    off, on = results["문서대조 OFF"], results["문서대조 ON"]
    out("===== 차별점 기여도 =====")
    out(f"  재현율 {off['recall']*100:.1f}% -> {on['recall']*100:.1f}%"
        f"  (+{(on['recall']-off['recall'])*100:.1f}%p)")
    out()
    out("  문서대조 ON 에서만 잡히는 항목:")
    off_locs = {norm(h[0]['locator']) for h in off["hits"] if h[2] == "정확"}
    for h in on["hits"]:
        if h[2] == "정확" and norm(h[0]["locator"]) not in off_locs:
            out(f"    {h[0]['id']}  {h[0]['locator']}")
            out(f"       {h[1]['why'][:130]}")

    text = "\n".join(lines)
    (ROOT / "data" / "score.txt").write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
