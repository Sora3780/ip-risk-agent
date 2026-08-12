"""데스크톱 앱 실행기.

검사 서버를 띄우고 GUI 를 연다. 창을 닫으면 서버도 함께 정리한다.
터미널 두 개를 열 필요가 없다.

사용:
  python scripts/launch.py            서버 + GUI
  python scripts/launch.py --no-server   이미 서버가 떠 있을 때
"""

from __future__ import annotations

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import argparse
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = "http://127.0.0.1:8000"


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


def server_alive(timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(f"{API}/health", timeout=timeout) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError):
        return False


def start_server() -> subprocess.Popen | None:
    if server_alive():
        print("이미 떠 있는 서버를 사용합니다.")
        return None

    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    env["PYTHONIOENCODING"] = "utf-8"
    # 데스크톱 앱은 로컬 실행이므로 폴더 경로 검사를 허용한다.
    env["IPAGENT_ALLOW_LOCAL_PATH"] = "1"

    log = (ROOT / "data" / "server.log").open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "uvicorn", "ipagent.api:app",
         "--port", "8000", "--log-level", "warning"],
        cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT,
    )

    print("서버를 시작합니다", end="", flush=True)
    for _ in range(40):  # 최대 20초
        if server_alive(timeout=1.0):
            print(" — 준비 완료")
            return proc
        if proc.poll() is not None:
            print("\n서버가 종료되었습니다. data/server.log 를 확인하세요.")
            return None
        print(".", end="", flush=True)
        time.sleep(0.5)
    print("\n서버가 응답하지 않습니다. data/server.log 를 확인하세요.")
    return proc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-server", action="store_true", help="서버를 띄우지 않는다")
    args = ap.parse_args()

    load_env()
    if not os.environ.get("GEMINI_API_KEY"):
        print("[주의] GEMINI_API_KEY 가 없습니다. 심층 검토 없이 규칙 판정만 동작합니다.")

    proc = None if args.no_server else start_server()
    if not args.no_server and proc is None and not server_alive():
        return 1

    sys.path.insert(0, str(ROOT / "src"))
    from ipagent.gui import App

    try:
        App().mainloop()
    finally:
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            print("서버를 종료했습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
