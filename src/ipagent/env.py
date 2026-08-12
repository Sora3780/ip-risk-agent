"""`.env` 로더.

셸로 `source .env` 하면 Windows 경로의 역슬래시와 공백이 깨진다.
(C:\\Users\\...\\바탕 화면\\... 가 명령어로 해석된다)
서버가 어떻게 기동되든 같은 값을 읽도록 파이썬에서 직접 읽는다.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_env(path: Path | None = None, override: bool = False) -> dict[str, str]:
    """.env 를 읽어 os.environ 에 넣는다. 이미 설정된 값은 건드리지 않는다."""
    env_path = path or ROOT / ".env"
    loaded: dict[str, str] = {}
    if not env_path.exists():
        return loaded

    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        # 값을 따옴표로 감싼 경우 벗겨낸다
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not value:
            continue
        if override or key not in os.environ:
            os.environ[key] = value
        loaded[key] = value
    return loaded
