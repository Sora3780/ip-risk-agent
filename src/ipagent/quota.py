"""외부 API 호출량 예산.

KIPRIS 는 상품당 월 1,000회, Gemini 무료 등급은 10 RPM / 1,500 RPD 다.
특허 검토 1회에 KIPRIS 약 22회가 나가므로, 자동 실행을 걸면
편집 중 몇 시간 만에 한 달치를 태울 수 있다.

한도에 닿으면 조용히 건너뛰지 않고 예외를 던진다.
"검사했는데 결과가 없다" 와 "한도 때문에 검사를 못 했다" 는 전혀 다른 이야기다.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "data" / "quota.json"

# 여유를 남긴다. 시연 당일 한도가 비어 있어야 한다.
LIMITS = {"kipris": 800, "gemini_day": 1200}

_lock = threading.Lock()


class QuotaExceeded(RuntimeError):
    pass


def _load() -> dict:
    now = datetime.now(timezone.utc)
    month, day = now.strftime("%Y-%m"), now.strftime("%Y-%m-%d")
    data = {}
    if STATE.exists():
        try:
            data = json.loads(STATE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    if data.get("month") != month:
        data = {"month": month, "kipris": 0, "day": day, "gemini": 0}
    if data.get("day") != day:
        data["day"], data["gemini"] = day, 0
    return data


def _save(data: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def spend(kind: str, n: int = 1) -> None:
    """kind: 'kipris' (월) 또는 'gemini' (일). 한도를 넘으면 QuotaExceeded."""
    with _lock:
        data = _load()
        limit = LIMITS["kipris"] if kind == "kipris" else LIMITS["gemini_day"]
        used = data.get(kind, 0)
        if used + n > limit:
            period = "이번 달" if kind == "kipris" else "오늘"
            raise QuotaExceeded(
                f"{kind} 호출 한도에 도달했습니다 ({used}/{limit}, {period} 기준). "
                f"자동 특허 검토를 끄거나 한도를 조정하세요."
            )
        data[kind] = used + n
        _save(data)


def status() -> dict:
    with _lock:
        data = _load()
        return {
            "kipris": {"used": data.get("kipris", 0), "limit": LIMITS["kipris"],
                       "period": data.get("month")},
            "gemini": {"used": data.get("gemini", 0), "limit": LIMITS["gemini_day"],
                       "period": data.get("day")},
        }


def remaining(kind: str) -> int:
    s = status()[kind]
    return max(s["limit"] - s["used"], 0)
