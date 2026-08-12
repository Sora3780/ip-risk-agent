"""검사 진행 상황 보고.

검사 한 번이 몇 분 걸릴 수 있는데(LLM 건당 6초, 특허 문서당 50초)
그동안 화면이 조용하면 멈춘 것처럼 보인다.
서버가 단계별로 여기에 써두고 화면이 짧은 주기로 읽어간다.

워크스페이스별 파일 하나로 관리한다. 서버가 재시작돼도 남지 않도록
검사 시작 시 초기화하고, 끝나면 done 으로 표시한다.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# 단계별 가중치. 실제 소요 시간 비율에 맞춰 진행률이 고르게 움직이도록 한다.
PHASES = [
    ("collect", "파일 수집", 5),
    ("rules", "규칙 검사", 10),
    ("llm", "근거 검색·설명 생성", 45),
    ("patent", "선행 특허 대조", 38),
    ("done", "완료", 2),
]
_WEIGHT = {key: w for key, _, w in PHASES}
_LABEL = {key: label for key, label, _ in PHASES}
_ORDER = [key for key, _, _ in PHASES]


def _path(workspace_id: str) -> Path:
    return ROOT / "data" / "workspaces" / workspace_id / "progress.json"


def start(workspace_id: str) -> None:
    write(workspace_id, "collect", 0, 1)


def write(workspace_id: str, phase: str, done: int, total: int, note: str = "") -> None:
    """현재 단계와 그 안에서의 진행도를 기록한다."""
    total = max(total, 1)
    ratio = min(done / total, 1.0)

    # 앞 단계들의 가중치를 모두 채운 것으로 보고, 현재 단계는 비율만큼만 더한다.
    before = sum(_WEIGHT[k] for k in _ORDER[: _ORDER.index(phase)]) if phase in _ORDER else 0
    percent = before + _WEIGHT.get(phase, 0) * ratio
    total_weight = sum(_WEIGHT.values())

    p = _path(workspace_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "phase": phase,
        "label": _LABEL.get(phase, phase),
        "done": done,
        "total": total,
        "percent": round(percent / total_weight * 100, 1),
        "note": note,
        "running": phase != "done",
        "updated_at": time.time(),
    }, ensure_ascii=False), encoding="utf-8")


def finish(workspace_id: str) -> None:
    write(workspace_id, "done", 1, 1)


def read(workspace_id: str) -> dict:
    p = _path(workspace_id)
    if not p.exists():
        return {"running": False, "percent": 0, "label": "", "done": 0, "total": 0}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"running": False, "percent": 0, "label": "", "done": 0, "total": 0}
    # 오래 갱신이 없으면 죽은 것으로 본다 (서버가 중간에 죽은 경우)
    if data.get("running") and time.time() - data.get("updated_at", 0) > 300:
        data["running"] = False
        data["note"] = "응답 없음"
    return data
