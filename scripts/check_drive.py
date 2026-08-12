"""Google Drive 연동 확인.

키만 넣고 실행하면 서비스 계정 이메일을 알려준다.
그 이메일에 Drive 폴더를 '뷰어'로 공유해야 읽을 수 있다.

사용:
  python scripts/check_drive.py                    키·계정 확인
  python scripts/check_drive.py <폴더ID 또는 링크>   폴더까지 읽어보기
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
    from ipagent.drive import DriveClient, DriveError, parse_folder_id

    path = os.environ.get("GOOGLE_DRIVE_CREDENTIALS", "").strip()
    print(f"키 경로: {path or '(비어 있음)'}")
    if path and Path(path).exists():
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
            print(f"  타입   : {raw.get('type')}")
            print(f"  프로젝트: {raw.get('project_id')}")
        except json.JSONDecodeError:
            print("  ! JSON 파일이 아닙니다")

    try:
        client = DriveClient()
    except DriveError as exc:
        print(f"\n[실패] {exc}")
        return 1
    except Exception as exc:
        print(f"\n[실패] {type(exc).__name__}: {exc}")
        return 1

    print("\n" + "=" * 62)
    print("서비스 계정 이메일 — 이 주소에 Drive 폴더를 '뷰어'로 공유하세요")
    print("=" * 62)
    print(f"\n    {client.account_email}\n")

    if len(sys.argv) < 2:
        print("폴더까지 확인하려면:")
        print("    python scripts/check_drive.py <폴더ID 또는 공유링크>")
        return 0

    folder_id = parse_folder_id(sys.argv[1])
    print(f"폴더 확인: {folder_id}")
    try:
        files = client.walk(folder_id)
    except DriveError as exc:
        print(f"\n[실패] {exc}")
        return 1

    if not files:
        print("\n비어 있거나 접근 권한이 없습니다. 공유 설정을 확인하세요.")
        return 1

    targets = [f for f in files if client.is_target(f)]
    print(f"\n파일 {len(files)}개 중 검사 대상 {len(targets)}개\n")
    for f in files:
        mark = "O" if client.is_target(f) else "-"
        kind = "Google 문서" if f.mime.startswith("application/vnd.google-apps") else ""
        print(f"  {mark}  {f.path[:56]:58s} {kind}")

    changes, info = client.collect_changes(folder_id)
    print(f"\n수집 {info['collected']}개 · Google 문서 추출 {info['google_docs_exported']}개")
    for c in changes[:3]:
        preview = " ".join(c["content"].split())[:110]
        print(f"\n  [{c['path']}]\n    {preview}…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
