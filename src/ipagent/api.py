"""검사 서버.

입력 소스(로컬 감시기 / GitHub webhook / 수동 업로드)가 모두 이 API 하나로 들어온다.
서버는 워크스페이스별로 파일 사본을 유지하고, 변경분을 반영한 뒤 전체를 다시 검사한다.

  POST /api/scan              변경 파일 수신 -> 검사 -> 결과 반환
  GET  /api/findings/{ws}     최신 결과
  GET  /api/timeline/{ws}     시간순 이력 (지속 추적)
  GET  /health

실행:
  uvicorn ipagent.api:app --reload --port 8000
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from . import progress
from .env import load_env
from .patent_watch import SCHEMA_VERSION as PATENT_SCHEMA
from .scan import BASELINE, enrich_with_llm, scan

# 셸 방식에 의존하지 않도록 서버가 직접 읽는다 (Windows 경로 깨짐 방지)
load_env()

ROOT = Path(__file__).resolve().parents[2]
STORE = ROOT / "data" / "workspaces"

app = FastAPI(
    title="IP DeteDog",
    description="작업공간 변경을 추적해 오픈소스 라이선스 리스크를 조기 탐지한다",
    version="0.1.0",
)


# ------------------------------------------------------------------ 스키마

class Change(BaseModel):
    path: str = Field(..., description="워크스페이스 기준 상대 경로")
    change_type: Literal["created", "modified", "deleted"] = "modified"
    content: str | None = Field(None, description="deleted 면 생략")


class ScanRequest(BaseModel):
    workspace_id: str = Field(..., min_length=1, max_length=64)
    changes: list[Change] = Field(default_factory=list)
    llm: bool = Field(False, description="RAG+Gemini 심층 검토. 느리다(건당 6초)")
    doc_check: bool = True
    patent: bool = Field(False, description="기획서가 바뀌었을 때만 선행 특허 검토")


class ScanResponse(BaseModel):
    workspace_id: str
    scanned_at: str
    baseline_license: str
    findings: list[dict]
    stats: dict
    # 무엇을 검사했는지. "탐지 0건"이 '안전'인지 '볼 게 없었음'인지 구분하는 근거다.
    coverage: dict = {}
    source_path: str = ""


# ------------------------------------------------------------------ 저장소

def _safe_relpath(raw: str) -> Path:
    """경로 탈출 차단. 받은 경로를 그대로 믿고 쓰면 서버 파일을 덮어쓸 수 있다."""
    p = Path(raw.replace("\\", "/"))
    if p.is_absolute() or any(part in ("..", "") for part in p.parts):
        raise HTTPException(400, f"허용되지 않는 경로: {raw}")
    if p.drive or str(p).startswith("~"):
        raise HTTPException(400, f"허용되지 않는 경로: {raw}")
    return p


def _workspace_dir(ws: str) -> Path:
    if not ws.replace("-", "").replace("_", "").isalnum():
        raise HTTPException(400, "workspace_id 는 영숫자/-/_ 만 허용한다")
    return STORE / ws


def apply_changes(ws: str, changes: list[Change]) -> dict:
    """서버가 들고 있는 사본에 변경분을 반영한다."""
    tree = _workspace_dir(ws) / "tree"
    tree.mkdir(parents=True, exist_ok=True)
    applied = {"created": 0, "modified": 0, "deleted": 0, "skipped": 0}

    for ch in changes:
        target = tree / _safe_relpath(ch.path)
        if ch.change_type == "deleted":
            if target.exists():
                target.unlink()
                applied["deleted"] += 1
            continue
        if ch.content is None:
            applied["skipped"] += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        existed = target.exists()
        target.write_text(ch.content, encoding="utf-8")
        applied["modified" if existed else "created"] += 1
    return applied


def record(ws: str, report: dict) -> None:
    d = _workspace_dir(ws)
    d.mkdir(parents=True, exist_ok=True)
    # 판정 형식이 바뀌면 저장분을 그대로 재사용하면 안 된다 (화면에 빈 칸이 뜬다)
    report["patent_schema"] = PATENT_SCHEMA
    (d / "findings.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # 이력은 append-only. 프로젝트가 발전하는 과정을 남기는 것이 이 서비스의 핵심이다.
    entry = {
        "scanned_at": report["scanned_at"],
        "total": len(report["findings"]),
        "by_tier": report["stats"]["by_tier"],
        "locators": [f["locator"] for f in report["findings"]],
    }
    with (d / "history.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ------------------------------------------------------------------ 엔드포인트

DEMO_WORKSPACE = ROOT / "tests" / "fixtures" / "sample-workspace"


@app.get("/health")
def health() -> dict:
    import os

    index_ok = (ROOT / "data" / "rag_index.npz").exists()
    policy = ROOT / "data" / "license_policy.json"
    return {
        "status": "ok",
        "baseline_license": BASELINE,
        "rag_index": index_ok,
        "policy_licenses": len(json.loads(policy.read_text(encoding="utf-8"))) if policy.exists() else 0,
        # 화면이 어떤 입력 방식을 보여줄지 판단하는 근거.
        # 배포 환경에서 폴더 지정 모드를 띄우면 누구도 쓸 수 없는 입력창이 기본값이 된다.
        "local_path_enabled": os.environ.get("IPAGENT_ALLOW_LOCAL_PATH") == "1",
        "demo_available": DEMO_WORKSPACE.is_dir(),
        "quota": __import__("ipagent.quota", fromlist=["status"]).status(),
    }


class DemoRequest(BaseModel):
    llm: bool = False


@app.post("/api/demo-scan", response_model=ScanResponse)
def demo_scan(req: DemoRequest) -> ScanResponse:
    """동봉된 데모 프로젝트를 검사한다.

    처음 접속한 사람은 올릴 프로젝트가 없다. 빈 입력창만 보고 나가면
    만든 것을 아무것도 보여주지 못한다. 경로가 고정되어 있으므로
    IPAGENT_ALLOW_LOCAL_PATH 와 무관하게 안전하다.
    """
    if not DEMO_WORKSPACE.is_dir():
        raise HTTPException(503, "데모 프로젝트가 설치되어 있지 않다")

    started = time.monotonic()
    report = scan(DEMO_WORKSPACE, with_doc_check=True)
    if req.llm:
        try:
            report = enrich_with_llm(report, workspace_id="demo-sample")
        except FileNotFoundError as exc:
            raise HTTPException(503, f"RAG 인덱스 없음: {exc}") from exc

    by_tier: dict[str, int] = {}
    for f in report["findings"]:
        by_tier[f["tier"]] = by_tier.get(f["tier"], 0) + 1

    report["workspace_id"] = "demo-sample"
    report["scanned_at"] = datetime.now(timezone.utc).isoformat()
    report["source_path"] = "동봉 데모 프로젝트 (nova-dashboard)"
    report["stats"] = {
        "by_tier": by_tier,
        "applied_changes": {"demo": True},
        "elapsed_sec": round(time.monotonic() - started, 2),
        "llm": req.llm,
        # 이번에 새로 검토한 건수와 지난 결과를 그대로 쓴 건수
        "llm_reuse": report.get("llm_stats", {}),
    }
    record("demo-sample", report)
    return ScanResponse(
        workspace_id="demo-sample",
        scanned_at=report["scanned_at"],
        baseline_license=report["baseline_license"],
        findings=report["findings"],
        stats=report["stats"],
        coverage=report.get("coverage", {}),
        source_path=report.get("source_path", ""),
    )


@app.post("/api/scan", response_model=ScanResponse)
def scan_workspace(req: ScanRequest) -> ScanResponse:
    started = time.monotonic()
    progress.start(req.workspace_id)
    applied = apply_changes(req.workspace_id, req.changes)

    tree = _workspace_dir(req.workspace_id) / "tree"
    if not tree.exists() or not any(tree.rglob("*")):
        raise HTTPException(400, "워크스페이스가 비어 있다. changes 를 먼저 보낼 것")

    progress.write(req.workspace_id, "rules", 0, 1)
    report = scan(tree, with_doc_check=req.doc_check)
    if req.llm:
        try:
            report = enrich_with_llm(report, workspace_id=req.workspace_id)
        except FileNotFoundError as exc:
            raise HTTPException(503, f"RAG 인덱스 없음: {exc}") from exc

    patent_info = {}
    if req.patent:
        from .patent_watch import screen_changed_documents

        patent_info = screen_changed_documents(req.workspace_id, tree, report_progress=True)
        report["findings"].extend(patent_info["findings"])
        cov = report.setdefault("coverage", {})
        cov["patent_documents"] = patent_info["documents"]
        cov.setdefault("notes", []).extend(patent_info["notes"])

    by_tier: dict[str, int] = {}
    for f in report["findings"]:
        by_tier[f["tier"]] = by_tier.get(f["tier"], 0) + 1

    report["workspace_id"] = req.workspace_id
    report["scanned_at"] = datetime.now(timezone.utc).isoformat()
    report["stats"] = {
        "by_tier": by_tier,
        "applied_changes": applied,
        "patent": patent_info,
        "elapsed_sec": round(time.monotonic() - started, 2),
        "llm": req.llm,
        # 이번에 새로 검토한 건수와 지난 결과를 그대로 쓴 건수
        "llm_reuse": report.get("llm_stats", {}),
    }
    record(req.workspace_id, report)
    progress.finish(req.workspace_id)

    return ScanResponse(
        workspace_id=req.workspace_id,
        scanned_at=report["scanned_at"],
        baseline_license=report["baseline_license"],
        findings=report["findings"],
        stats=report["stats"],
        coverage=report.get("coverage", {}),
        source_path=report.get("source_path", ""),
    )


@app.get("/api/progress/{workspace_id}")
def get_progress(workspace_id: str) -> dict:
    """검사 진행 상황. 화면이 짧은 주기로 읽어 진행률 막대를 그린다."""
    return progress.read(workspace_id)


@app.get("/api/drive-folder")
def drive_folder(folder: str) -> dict:
    """붙여넣은 링크가 맞는 폴더인지 확인해 준다 (이름·파일 수)."""
    from .drive import DriveClient, DriveError, parse_folder_id

    try:
        client = DriveClient()
        info = client.folder_info(parse_folder_id(folder))
        info["service_account"] = client.account_email
        return info
    except DriveError as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"Drive 연동 실패: {type(exc).__name__} {exc}") from exc


class DriveScanRequest(BaseModel):
    workspace_id: str = Field(..., min_length=1, max_length=64)
    folder: str = Field(..., description="Drive 폴더 ID 또는 공유 URL")
    llm: bool = False
    doc_check: bool = True
    patent: bool = False


@app.post("/api/scan-drive", response_model=ScanResponse)
def scan_drive(req: DriveScanRequest) -> ScanResponse:
    """Google Drive 폴더를 검사한다.

    Google 문서는 파일이 아니라 서비스 객체라서 로컬 감시기로는 내용을 읽을 수 없다.
    여기서 files.export 로 텍스트를 뽑아 일반 파일과 같은 형식으로 서버에 넣는다.
    """
    from .drive import DriveClient, DriveError, parse_folder_id

    folder_id = parse_folder_id(req.folder)
    snap_path = _workspace_dir(req.workspace_id) / "drive_snapshot.json"

    try:
        client = DriveClient()
        # 먼저 목록만 훑어 변경 여부를 본다. 본문을 받지 않으므로 폴링 비용이 싸다.
        current = client.snapshot(folder_id)
        previous = json.loads(snap_path.read_text(encoding="utf-8")) if snap_path.exists() else None

        stored_path = _workspace_dir(req.workspace_id) / "findings.json"
        stored = (json.loads(stored_path.read_text(encoding="utf-8"))
                  if stored_path.exists() else None)
        fresh = bool(stored) and stored.get("patent_schema") == PATENT_SCHEMA

        if previous == current and fresh:
            stored.setdefault("stats", {})["drive"] = {
                "unchanged": True, "watched_files": len(current), "folder_id": folder_id,
                "service_account": client.account_email,
            }
            return ScanResponse(
                workspace_id=req.workspace_id, scanned_at=stored.get("scanned_at", ""),
                baseline_license=stored["baseline_license"], findings=stored["findings"],
                stats=stored["stats"], coverage=stored.get("coverage", {}),
                source_path=stored.get("source_path", ""),
            )

        changes, drive_info = client.collect_changes(folder_id)
        drive_info["unchanged"] = False
        drive_info["folder_id"] = folder_id
        if previous is not None:
            drive_info["changed_paths"] = sorted(
                p for p in set(current) | set(previous) if previous.get(p) != current.get(p))
    except DriveError as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"Drive 연동 실패: {type(exc).__name__} {exc}") from exc

    if not changes:
        raise HTTPException(
            400,
            f"검사할 파일이 없습니다. 폴더에 문서 {drive_info['total_files']}개가 있으나 "
            f"검사 대상 형식이 아닙니다.",
        )

    scan_req = ScanRequest(workspace_id=req.workspace_id,
                           changes=[Change(**c) for c in changes],
                           llm=req.llm, doc_check=req.doc_check, patent=req.patent)
    resp = scan_workspace(scan_req)
    resp.stats = {**resp.stats, "drive": drive_info}
    resp.source_path = f"Google Drive ({drive_info['collected']}개 파일)"

    snap_path.parent.mkdir(parents=True, exist_ok=True)
    snap_path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    # 스냅샷을 findings 에도 반영해 다음 폴링이 캐시를 쓸 수 있게 한다
    stored = json.loads((_workspace_dir(req.workspace_id) / "findings.json").read_text(encoding="utf-8"))
    stored["stats"] = resp.stats
    stored["source_path"] = resp.source_path
    (_workspace_dir(req.workspace_id) / "findings.json").write_text(
        json.dumps(stored, ensure_ascii=False, indent=2), encoding="utf-8")
    return resp


class PathScanRequest(BaseModel):
    workspace_id: str = Field(..., min_length=1, max_length=64)
    path: str = Field(..., description="서버에서 접근 가능한 폴더 경로")
    llm: bool = False
    doc_check: bool = True


@app.post("/api/scan-path", response_model=ScanResponse)
def scan_local_path(req: PathScanRequest) -> ScanResponse:
    """서버가 직접 읽을 수 있는 폴더를 검사한다.

    로컬 실행 전용이다. 배포 환경에서 켜면 서버의 임의 경로를 읽을 수 있으므로
    IPAGENT_ALLOW_LOCAL_PATH=1 이 설정된 경우에만 동작한다.
    """
    import os

    if os.environ.get("IPAGENT_ALLOW_LOCAL_PATH") != "1":
        raise HTTPException(
            403,
            "폴더 경로 검사는 로컬 실행에서만 허용된다. "
            "IPAGENT_ALLOW_LOCAL_PATH=1 로 서버를 띄울 것",
        )

    target = Path(req.path).expanduser()
    if not target.is_dir():
        raise HTTPException(400, f"폴더가 아니거나 존재하지 않는다: {req.path}")

    # 서버 사본으로 복사하지 않고 원본을 그대로 검사한다.
    started = time.monotonic()
    report = scan(target, with_doc_check=req.doc_check)
    if req.llm:
        try:
            report = enrich_with_llm(report, workspace_id=req.workspace_id)
        except FileNotFoundError as exc:
            raise HTTPException(503, f"RAG 인덱스 없음: {exc}") from exc

    by_tier: dict[str, int] = {}
    for f in report["findings"]:
        by_tier[f["tier"]] = by_tier.get(f["tier"], 0) + 1

    report["workspace_id"] = req.workspace_id
    report["scanned_at"] = datetime.now(timezone.utc).isoformat()
    report["source_path"] = str(target)
    report["stats"] = {
        "by_tier": by_tier,
        "applied_changes": {"scanned_in_place": True},
        "elapsed_sec": round(time.monotonic() - started, 2),
        "llm": req.llm,
        # 이번에 새로 검토한 건수와 지난 결과를 그대로 쓴 건수
        "llm_reuse": report.get("llm_stats", {}),
    }
    record(req.workspace_id, report)

    return ScanResponse(
        workspace_id=req.workspace_id,
        scanned_at=report["scanned_at"],
        baseline_license=report["baseline_license"],
        findings=report["findings"],
        stats=report["stats"],
        coverage=report.get("coverage", {}),
        source_path=report.get("source_path", ""),
    )


class PatentRequest(BaseModel):
    document: str = Field(..., min_length=50, description="기획서 본문")
    per_query: int = Field(5, ge=1, le=20)
    max_assess: int = Field(8, ge=1, le=30, description="유사도 판정할 상위 후보 수")


@app.post("/api/screen-patent")
def screen_patent(req: PatentRequest) -> dict:
    """기획서 -> 선행 특허 대조.

    라이선스 검사와 파이프라인 구조는 같고 Grounding Source 만 다르다.
    비용이 크므로(KIPRIS 월 1,000회 + Gemini 10 RPM) 자동 실행하지 않고 별도 호출로 둔다.
    """
    from .patent import KiprisError, screen_document

    try:
        result = screen_document(
            req.document,
            per_query=req.per_query,
            max_assess=req.max_assess,
            cache_path=ROOT / "data" / "kipris_cache.json",
        )
    except KiprisError as exc:
        raise HTTPException(503, f"KIPRIS 연동 불가: {exc}") from exc

    return {
        "core_idea_ko": result["plan"]["core_idea_ko"],
        "queries": result["plan"]["queries"],
        "search_stats": result["plan"].get("search_stats", []),
        "candidates": result["candidates"],
        "assessed": result["assessed"],
        "not_assessed": result["not_assessed"],
        "coverage_note": result["coverage_note"],
        "findings": [
            {
                "application_number": r["patent"].application_number,
                "title_ko": r["patent"].title_ko,
                "title_en": r["patent"].title_en,
                "applicant": r["patent"].applicant,
                "application_date": r["patent"].application_date,
                "ipc": r["patent"].ipc,
                "abstract_en": r["patent"].abstract_en,
                "matched_queries": r["patent"].raw.get("_matched_queries", []),
                **r["verdict"],
            }
            for r in result["findings"]
        ],
        "scanned_at": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/workspace/{workspace_id}/files")
def workspace_files(workspace_id: str) -> dict:
    """서버가 보관 중인 파일 목록.

    업로드한 파일만 검사된다고 오해하기 쉽다. 서버는 사본을 유지하고
    변경분을 덮어쓴 뒤 '전체'를 다시 검사한다. 그 사실을 화면에 드러내야 한다.
    """
    tree = _workspace_dir(workspace_id) / "tree"
    if not tree.exists():
        return {"workspace_id": workspace_id, "files": []}
    files = sorted(p.relative_to(tree).as_posix() for p in tree.rglob("*") if p.is_file())
    return {"workspace_id": workspace_id, "files": files}


@app.delete("/api/workspace/{workspace_id}")
def reset_workspace(workspace_id: str) -> dict:
    """워크스페이스 사본과 이력을 지운다. 시연 중 깨끗한 상태에서 다시 시작할 때 쓴다."""
    import shutil

    d = _workspace_dir(workspace_id)
    if d.exists():
        shutil.rmtree(d)
    return {"workspace_id": workspace_id, "reset": True}


@app.get("/api/findings/{workspace_id}")
def latest(workspace_id: str) -> dict:
    path = _workspace_dir(workspace_id) / "findings.json"
    if not path.exists():
        raise HTTPException(404, "검사 이력이 없다")
    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/api/timeline/{workspace_id}")
def timeline(workspace_id: str) -> dict:
    path = _workspace_dir(workspace_id) / "history.jsonl"
    if not path.exists():
        raise HTTPException(404, "검사 이력이 없다")
    entries = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    # 직전 대비 새로 생긴 항목 = 이번 변경이 만든 리스크
    for prev, cur in zip(entries, entries[1:]):
        cur["new_locators"] = sorted(set(cur["locators"]) - set(prev["locators"]))
        cur["resolved_locators"] = sorted(set(prev["locators"]) - set(cur["locators"]))
    return {"workspace_id": workspace_id, "scans": len(entries), "timeline": entries}
