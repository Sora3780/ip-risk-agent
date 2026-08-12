"""기획서 변경 -> 특허 자동 검토.

라이선스 검사와 달리 특허 검토는 비싸다 (검토 1회에 KIPRIS 약 22회, Gemini 약 9회).
파일을 저장할 때마다 돌리면 월 한도가 몇 시간 만에 사라진다.

그래서 세 겹으로 막는다.
  1) 내용 해시  — 글자가 실제로 바뀌었을 때만. 저장만 다시 눌러도 안 돈다
  2) 쿨다운     — 같은 문서를 짧은 간격으로 반복 검토하지 않는다
  3) 예산       — 월/일 한도에 닿으면 실행하지 않고 그 사실을 알린다

검토 결과는 문서별로 저장해두고, 재검토하지 않을 때는 저장분을 그대로 쓴다.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from .patent import find_documents, screen_document
from .quota import QuotaExceeded, remaining

ROOT = Path(__file__).resolve().parents[2]
COOLDOWN_SEC = 600          # 같은 문서 재검토 최소 간격
# 저장된 판정 결과의 형식 버전. 판정 스키마를 바꾸면 올린다.
# 안 올리면 옛 형식으로 저장된 결과를 그대로 재사용해 화면에 빈 칸이 뜬다.
SCHEMA_VERSION = 2
KIPRIS_PER_RUN = 25         # 검토 1회의 대략적인 KIPRIS 호출 수


def _state_path(workspace_id: str) -> Path:
    return ROOT / "data" / "workspaces" / workspace_id / "patent_state.json"


def _load_state(workspace_id: str) -> dict:
    p = _state_path(workspace_id)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _save_state(workspace_id: str, state: dict) -> None:
    p = _state_path(workspace_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _tier_for(v: dict) -> str:
    """유사도 -> 등급.

    등급은 '침해 확정' 이 아니라 '지금 무엇을 해야 하는가' 를 뜻한다.
    라이선스의 FORBIDDEN 도 위반 확정이 아니라 '이대로 배포하면 안 된다' 는 정책 판단이다.
    같은 기준을 특허에 적용하면, 기술 구성이 그대로 겹치는 건은 위험으로 올려야 한다.
    빼박 수준을 '주의' 로 두면 GPL 의존성보다 덜 중요해 보인다.

    다만 무조건 올리지 않는다. 모델이 스스로 근거가 부족하다고 했거나
    전문가 조사가 불필요하다고 판단했으면 한 단계 낮춘다.
    """
    strong = v.get("grounded") and v.get("needs_expert_review")
    if v["similarity"] == "HIGH":
        return "FORBIDDEN" if strong else "RESTRICTED"
    if v["similarity"] == "MEDIUM":
        return "RESTRICTED" if v.get("needs_expert_review") else "REVIEW"
    return "REVIEW"


def _to_findings(doc_rel: str, result: dict, document: str = "") -> list[dict]:
    """특허 유사도를 라이선스와 같은 형식의 항목으로 바꾼다.

    화면과 리포트를 하나로 유지하기 위해 등급 체계를 공유한다.
    """
    out = []
    for item in result.get("findings", []):
        p, v = item["patent"], item["verdict"]
        tier = _tier_for(v)
        title = p.title_ko or p.title_en
        out.append({
            "kind": "patent_similarity",
            "locator": f"{doc_rel} ~ {p.application_number}",
            "license": None,
            "tier": tier,
            "why": (f"기획서 아이디어가 선행 특허 {p.application_number} "
                    f"({title[:40]}) 와 기술 구성이 {v['similarity']} 수준으로 겹칩니다. "
                    f"{'변리사 조사가 필요합니다. ' if v.get('needs_expert_review') else ''}"
                    f"침해 판정이 아니며 초록만 대조했습니다 (청구범위 미확인)"),
            "evidence_ko": (f"[겹치는 부분] {v['overlap_ko']}\n\n"
                            f"[다른 부분] {v['difference_ko']}\n\n"
                            f"[초록] {p.abstract_en[:900]}"),
            "evidence_source": f"KIPRIS Plus 한국특허영문초록(KPA) — {p.application_number}",
            "document_excerpt": document[:4000],
            "patent_abstract": p.abstract_en,
            "matches": v.get("matches", []),
            "patent": {
                "application_number": p.application_number,
                "title_ko": p.title_ko, "title_en": p.title_en,
                "applicant": p.applicant, "application_date": p.application_date,
                "ipc": p.ipc, "similarity": v["similarity"],
                "needs_expert_review": v["needs_expert_review"], "grounded": v["grounded"],
            },
        })
    return out


def screen_changed_documents(workspace_id: str, root: Path,
                             cooldown_sec: int = COOLDOWN_SEC,
                             report_progress: bool = False) -> dict:
    """기획서로 보이는 문서 중 '내용이 바뀐 것'만 특허 검토한다."""
    state = _load_state(workspace_id)
    docs = find_documents(root)
    findings: list[dict] = []
    notes: list[str] = []
    ran = skipped = 0

    from . import progress

    for idx, doc in enumerate(docs):
        rel = doc.relative_to(root).as_posix()
        if report_progress:
            progress.write(workspace_id, "patent", idx, len(docs), rel)
        try:
            text = doc.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        prev = state.get(rel, {})
        now = time.time()

        unchanged = prev.get("hash") == digest
        cooling = now - prev.get("screened_at", 0) < cooldown_sec
        # 형식이 낡았으면 내용이 같아도 다시 판정한다 (쿨다운도 무시)
        stale = prev.get("schema") != SCHEMA_VERSION
        if stale:
            unchanged = cooling = False

        if unchanged or cooling:
            # 저장해둔 결과를 그대로 쓴다. 새 호출은 하지 않는다.
            findings.extend(prev.get("findings", []))
            skipped += 1
            if not unchanged and cooling:
                left = int((cooldown_sec - (now - prev["screened_at"])) / 60) + 1
                notes.append(f"{rel} 는 방금 검토해서 건너뜁니다 (약 {left}분 뒤 재검토 가능)")
            continue

        if remaining("kipris") < KIPRIS_PER_RUN:
            notes.append(f"KIPRIS 월 한도가 부족해 {rel} 특허 검토를 건너뜁니다")
            findings.extend(prev.get("findings", []))
            continue

        try:
            result = screen_document(text, per_query=5, max_assess=6,
                                     cache_path=ROOT / "data" / "kipris_cache.json")
        except QuotaExceeded as exc:
            notes.append(str(exc))
            findings.extend(prev.get("findings", []))
            continue
        except Exception as exc:  # 특허가 실패해도 라이선스 결과는 나가야 한다
            notes.append(f"{rel} 특허 검토 실패: {type(exc).__name__}")
            findings.extend(prev.get("findings", []))
            continue

        doc_findings = _to_findings(rel, result, document=text)
        state[rel] = {"schema": SCHEMA_VERSION, "hash": digest, "screened_at": now,
                      "findings": doc_findings, "candidates": result["candidates"],
                      "not_assessed": result["not_assessed"]}
        findings.extend(doc_findings)
        ran += 1
        if result["not_assessed"]:
            notes.append(f"{rel}: {result['coverage_note']}")

    if ran:
        _save_state(workspace_id, state)

    return {
        "findings": findings,
        "documents": len(docs),
        "screened": ran,
        "reused": skipped,
        "notes": notes,
    }
