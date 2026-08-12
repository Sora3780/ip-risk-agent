"""작업공간 스캔 -> 리스크 판정 -> 근거 인용 리포트.

사용:
  python -m ipagent.scan tests/fixtures/sample-workspace
  python -m ipagent.scan tests/fixtures/sample-workspace --with-doc-check
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .detect import (CODE_EXT, SKIP_DIRS, DepsDevClient, collect_dependencies,
                     declared_license, scan_files)
from .policy import Policy, tier_for_expression, tier_for_id

ROOT = Path(__file__).resolve().parents[2]
BASELINE = "PROPRIETARY"  # MVP 고정: 비공개 상용 배포
REPORTABLE = {"FORBIDDEN", "RESTRICTED", "REVIEW"}


def _finding(kind, locator, license_, tier, why, policy: Policy, extra=None, cite_for=None):
    # 근거는 "위험의 원인이 되는 라이선스" 것을 붙인다.
    # doc_mismatch 는 선언 라이선스(MIT)가 아니라 충돌 라이선스(GPL)가 원인이므로
    # cite_for 로 명시해 넘긴다. 이걸 틀리면 모델이 grounded=false 를 돌려준다.
    cite, source = policy.cite(cite_for or license_ or "")
    return {
        "kind": kind,
        "locator": locator,
        "license": license_,
        "tier": tier,
        "why": why,
        "evidence_ko": cite,
        "evidence_source": source,
        **(extra or {}),
    }


def scan(workspace: Path, with_doc_check: bool = False) -> dict:
    policy = Policy()
    client = DepsDevClient(ROOT / "data" / "depsdev_cache.json")

    findings: list[dict] = []
    resolved: list[dict] = []  # 판정된 전체 (오탐 검사용)

    # 1) 의존성
    dep_signals, dep_coverage = collect_dependencies(workspace, client)
    for sig in dep_signals:
        tier, ids = tier_for_expression(sig.license or "")
        primary = next((i for i in ids if tier_for_id(i) == tier), ids[0] if ids else "")
        resolved.append({"locator": sig.locator, "license": sig.license, "tier": tier})
        if tier in REPORTABLE:
            why = f"{BASELINE} 배포 기준으로 {primary} 는 {tier} 등급"
            # 추정 경로가 둘이다: 다른 버전으로 대체 조회 / 레지스트리 원문에서 추론.
            # 어느 쪽이든 "추정"이라는 사실을 리포트에 남겨야 한다.
            if sig.detail.get("license_from_version"):
                why += (
                    f". 고정 버전 {sig.detail.get('version')} 은 라이선스가 SPDX 로 "
                    f"매핑되지 않아 {sig.detail['license_from_version']} 기준으로 추정함"
                )
            elif sig.detail.get("license_source"):
                why += f". deps.dev 가 SPDX 로 매핑하지 못해 {sig.detail['license_source']} 에서 추정함"
            findings.append(
                _finding(
                    "dependency",
                    sig.locator,
                    sig.license,
                    tier,
                    why,
                    policy,
                    {
                        "matched_id": primary,
                        "package": sig.detail.get("package"),
                        "estimated": bool(sig.detail.get("estimated")),
                    },
                )
            )
    client.save()

    # 2) 파일 헤더 / 출처 불명
    for sig in scan_files(workspace):
        if sig.kind == "unknown_provenance":
            resolved.append({"locator": sig.locator, "license": None, "tier": "REVIEW"})
            findings.append(
                _finding(
                    "unknown_provenance",
                    sig.locator,
                    None,
                    "REVIEW",
                    sig.detail.get("reason", "출처 확인 필요"),
                    policy,
                )
            )
            continue
        tier = tier_for_id(sig.license or "")
        resolved.append({"locator": sig.locator, "license": sig.license, "tier": tier})
        if tier in REPORTABLE:
            findings.append(
                _finding(
                    "file_header",
                    sig.locator,
                    sig.license,
                    tier,
                    f"소스트리에 {sig.license} 코드가 직접 포함됨 ({sig.detail.get('via')})",
                    policy,
                )
            )

    # 3) 문서 표기 대조 — 기존 SCA 가 보지 않는 영역
    declarations = declared_license(workspace)
    if with_doc_check and declarations:
        worst = max(
            (f for f in findings if f["tier"] in ("FORBIDDEN", "RESTRICTED")),
            key=lambda f: f["tier"] == "FORBIDDEN",
            default=None,
        )
        # 문서가 이미 copyleft 를 표방하면 모순이 아니다.
        permissive = [d for d in declarations if tier_for_id(d.license or "") == "NOTICE"]
        if worst and permissive:
            # 여러 문서가 같은 주장을 해도 모순은 하나다. README 를 대표로 묶는다.
            primary = next((d for d in permissive if d.locator == "README.md"), permissive[0])
            asserts = any(d.detail.get("asserts_no_disclosure") for d in permissive)
            why = (
                f"문서는 {primary.license} 로 표기하나 실제로는 "
                f"{worst['license']} ({worst['locator']}) 가 포함되어 있음"
            )
            if asserts:
                why += ". 문서가 '소스 공개 의무 없음'을 명시적으로 주장함"
            findings.append(
                _finding(
                    "doc_mismatch",
                    primary.locator,
                    primary.license,
                    worst["tier"],
                    why,
                    policy,
                    {
                        "declared": primary.license,
                        "declared_in": [d.locator for d in permissive],
                        "conflicts_with": worst["locator"],
                        "conflicting_license": worst["license"],
                    },
                    cite_for=worst["license"],
                )
            )

    order = {"FORBIDDEN": 0, "RESTRICTED": 1, "REVIEW": 2}
    findings.sort(key=lambda f: (order.get(f["tier"], 9), f["locator"]))

    # "탐지 0건" 은 두 가지 뜻이 될 수 있다 — 위험이 없거나, 볼 것이 없었거나.
    # 구분하지 않으면 빈 폴더를 검사하고도 안전하다고 읽힌다.
    code_files = [
        p for p in workspace.rglob("*")
        if p.is_file() and p.suffix.lower() in CODE_EXT
        and not any(part in SKIP_DIRS or part.startswith(".") for part in p.parts)
    ]
    coverage = {
        **dep_coverage,
        "code_files": len(code_files),
        "declarations": len(declarations),
    }
    notes: list[str] = []
    if not dep_coverage["manifests"]:
        notes.append(
            "의존성 파일(requirements.txt / pyproject.toml / package.json)을 찾지 못했다. "
            "외부 라이브러리 라이선스는 검사되지 않았다."
        )
    if not code_files:
        notes.append("검사 가능한 소스 파일이 없다.")
    if not declarations:
        notes.append("README·LICENSE·pyproject 에 라이선스 표기가 없어 문서 대조를 수행하지 못했다.")
    coverage["notes"] = notes
    coverage["scannable"] = bool(dep_coverage["manifests"] or code_files)

    return {
        "workspace": workspace.name,
        "baseline_license": BASELINE,
        "doc_check": with_doc_check,
        "coverage": coverage,
        "findings": findings,
        "resolved": resolved,
        "declarations": [{"locator": d.locator, "license": d.license} for d in declarations],
    }


# LLM 검토 결과 캐시의 형식 버전. 프롬프트나 출력 스키마를 바꾸면 올린다.
LLM_CACHE_VERSION = 1


def _llm_key(f: dict) -> str:
    """판정이 같으면 설명도 같다. 그 동일성을 나타내는 지문.

    근거(evidence)는 이 값들로부터 결정적으로 검색되므로 키에 넣지 않아도 된다.
    """
    import hashlib

    import os

    raw = "|".join([
        str(LLM_CACHE_VERSION),
        # 모델을 바꾸면 설명도 달라진다. 옛 모델 답을 물려주지 않는다.
        os.environ.get("GEMINI_MODEL", ""), os.environ.get("GEMINI_THINKING", ""),
        f.get("locator", ""), str(f.get("license")),
        f.get("tier", ""), f.get("why", ""), str(f.get("conflicting_license", "")),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _load_llm_cache(workspace_id: str | None) -> dict:
    if not workspace_id:
        return {}
    p = ROOT / "data" / "workspaces" / workspace_id / "llm_cache.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _save_llm_cache(workspace_id: str | None, cache: dict) -> None:
    if not workspace_id:
        return
    p = ROOT / "data" / "workspaces" / workspace_id / "llm_cache.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    # 무한정 쌓이지 않게 최근 것 위주로 자른다
    if len(cache) > 500:
        cache = dict(list(cache.items())[-500:])
    p.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")


def enrich_with_llm(report: dict, rpm: int | None = None,
                    workspace_id: str | None = None) -> dict:
    """2단계 심층 검토. 규칙 게이트를 통과한 건에만 RAG + Gemini 를 태운다.

    이미 같은 판정으로 검토한 항목은 저장된 결과를 그대로 쓴다.
    파일 하나를 고쳤을 때 12건을 전부 다시 돌리면 시간도 비용도 그만큼 나간다.

    호출은 레이트리밋을 지키며 동시에 나간다. 무료 등급(10 RPM)에서는 순차와 큰 차이가
    없지만, 결제를 켜고 GEMINI_RPM 만 올리면 그대로 빨라진다.
    """
    import threading

    from . import progress
    from .llm import assess, run_limited
    from .rag import Retriever

    findings = report["findings"]
    cache = _load_llm_cache(workspace_id)

    pending, reused = [], 0
    for f in findings:
        hit = cache.get(_llm_key(f))
        if hit:
            f["llm"] = hit["llm"]
            f["evidence_ko"] = hit.get("evidence_ko", f.get("evidence_ko", ""))
            f["evidence_source"] = hit.get("evidence_source", f.get("evidence_source", ""))
            f["llm_cached"] = True
            reused += 1
        else:
            pending.append(f)

    retriever = Retriever() if pending else None
    total = len(pending)
    lock = threading.Lock()
    done = [0]

    def make_job(f: dict):
        def job():
            # 위험의 '원인'이 되는 라이선스로 검색한다 (doc_mismatch 는 선언 쪽이 아님)
            cause = f.get("conflicting_license") or f.get("license")
            evidence, source = "", ""
            # 라이선스가 미상이면 검색 필터가 없어 84종에서 무관한 조항을 긁어온다.
            # 근거 없이 검색하느니 "근거 없음"으로 넘기는 편이 리포트가 정직하다.
            if cause:
                situation = (
                    f"{report['baseline_license']} 배포 프로젝트에서 {f['locator']} 의 "
                    f"{cause} 라이선스가 {f['tier']} 로 판정됨. {f['why']}"
                )
                evidence, source = retriever.evidence_for(cause, situation, k=3)
            f["evidence_ko"] = evidence
            f["evidence_source"] = source
            f["llm"] = assess(f, report["baseline_license"], evidence=evidence, source=source)
            cache[_llm_key(f)] = {"llm": f["llm"], "evidence_ko": evidence,
                                  "evidence_source": source}
            return f["llm"]

        return job

    def tick(_i: int) -> None:
        with lock:
            done[0] += 1
            if workspace_id:
                progress.write(workspace_id, "llm", done[0], total or 1)

    if workspace_id:
        progress.write(workspace_id, "llm", 0, total or 1)
    if pending:
        run_limited([make_job(f) for f in pending], rpm=rpm, on_done=tick)
        _save_llm_cache(workspace_id, cache)

    print(f"  LLM 검토 {len(pending)}건 실행 · {reused}건 재사용")
    report["llm_enriched"] = True
    report["llm_stats"] = {"assessed": len(pending), "reused": reused}
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("workspace", type=Path)
    ap.add_argument("--with-doc-check", action="store_true", help="문서 표기와 실제 라이선스 대조")
    ap.add_argument("--llm", action="store_true", help="RAG 검색 + Gemini 심층 검토")
    ap.add_argument("--rpm", type=int, default=10, help="분당 최대 LLM 호출 (무료등급 10)")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    if not args.workspace.is_dir():
        print(f"작업공간 없음: {args.workspace}", file=sys.stderr)
        return 1

    report = scan(args.workspace, args.with_doc_check)
    print(f"기준: {BASELINE} / 문서대조: {'ON' if args.with_doc_check else 'OFF'}")
    print(f"규칙 게이트 통과 {len(report['findings'])}건\n")

    if args.llm:
        print(f"RAG 검색 + Gemini 검토 ({args.rpm} RPM 제한)")
        report = enrich_with_llm(report, rpm=args.rpm)
        print()

    out = args.out or ROOT / "data" / "findings.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    icon = {"FORBIDDEN": "[X]", "RESTRICTED": "[!]", "REVIEW": "[?]"}
    for f in report["findings"]:
        print(f"{icon.get(f['tier'],'   ')} {f['tier']:11s} {f['locator']}")
        print(f"    라이선스 : {f['license']}")
        print(f"    사유     : {f['why']}")
        llm = f.get("llm")
        if llm:
            print(f"    LLM판정  : {llm['verdict']}  (grounded={llm['grounded']}, "
                  f"법률검토필요={llm['needs_legal_review']})")
            print(f"    설명     : {llm['explanation_ko'][:150]}")
            for a in llm.get("actions_ko", [])[:2]:
                print(f"    조치     : {a[:110]}")
        elif f["evidence_ko"]:
            print(f"    근거     : {f['evidence_ko'].replace(chr(10), ' ')[:110]}…")
        if f.get("evidence_source"):
            print(f"    출처     : {f['evidence_source'][:110]}")
        print()
    print(f"리포트: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
