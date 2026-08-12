"""로컬 폴더 감시기.

파일이 저장되면 바뀐 파일만 검사 서버로 보낸다.

에디터는 저장 한 번에 이벤트를 여러 개 뿜고 임시파일(.swp, ~, 4913)도 만든다.
그대로 쏘면 초당 수십 번 호출되므로 디바운스와 필터가 필수다.

실행:
  python -m ipagent.watcher <감시할폴더> --workspace demo
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .detect import CODE_EXT, SKIP_DIRS

DEBOUNCE_SEC = 3.0
MAX_FILE_BYTES = 1_000_000

# 이름이 정확히 일치하면 감시한다 (확장자가 없거나 특수한 것들)
WATCH_NAMES = {
    "requirements.txt", "pyproject.toml", "setup.py", "setup.cfg",
    "package.json", "README.md", "LICENSE", "LICENSE.md", "NOTICE",
}
# 기획서·설계서 같은 문서. 특허 검토 대상이므로 반드시 서버로 보내야 한다.
# 이게 빠져 있으면 특허 기능이 감시기 경로에서 아예 동작하지 않는다.
DOC_EXT = {".md", ".txt", ".rst"}
# 에디터 임시파일
TEMP_SUFFIXES = (".swp", ".swx", ".tmp", "~", ".part", ".crdownload")


def is_watched(path: Path) -> bool:
    if any(part in SKIP_DIRS or part.startswith(".") for part in path.parts[:-1]):
        return False
    name = path.name
    if name.startswith(".") or name.endswith(TEMP_SUFFIXES):
        return False
    if name.isdigit():  # vim 이 만드는 4913 같은 파일
        return False
    suffix = path.suffix.lower()
    return name in WATCH_NAMES or suffix in CODE_EXT or suffix in DOC_EXT


class Debouncer(FileSystemEventHandler):
    """이벤트를 모았다가 조용해지면 한 번에 넘긴다."""

    def __init__(self, root: Path, flush):
        self.root = root
        self.flush = flush
        self.pending: dict[Path, str] = {}
        self.last_event = 0.0
        self.lock = threading.Lock()
        self._stop = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory or event.event_type not in ("created", "modified", "deleted", "moved"):
            return
        # moved 는 (삭제 + 생성) 으로 나눠 처리한다
        pairs = [(Path(event.src_path), "deleted" if event.event_type == "moved" else event.event_type)]
        if event.event_type == "moved" and getattr(event, "dest_path", None):
            pairs.append((Path(event.dest_path), "created"))

        with self.lock:
            for path, kind in pairs:
                if not is_watched(path):
                    continue
                self.pending[path] = kind
                self.last_event = time.monotonic()

    def _loop(self) -> None:
        while not self._stop.is_set():
            time.sleep(0.4)
            with self.lock:
                quiet = self.last_event and (time.monotonic() - self.last_event) >= DEBOUNCE_SEC
                if not (quiet and self.pending):
                    continue
                batch, self.pending, self.last_event = dict(self.pending), {}, 0.0
            try:
                self.flush(batch)
            except Exception as exc:  # 감시는 계속되어야 한다
                print(f"  ! 전송 실패: {type(exc).__name__}: {exc}")

    def stop(self) -> None:
        self._stop.set()


class Client:
    def __init__(self, api: str, workspace: str, use_llm: bool, use_patent: bool = False):
        self.api = api.rstrip("/")
        self.workspace = workspace
        self.use_llm = use_llm
        self.use_patent = use_patent
        self.previous: set[str] = set()

    def _post(self, payload: dict) -> dict:
        req = urllib.request.Request(
            f"{self.api}/api/scan",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.load(resp)

    def send(self, root: Path, batch: dict[Path, str]) -> None:
        changes = []
        for path, kind in sorted(batch.items()):
            rel = path.relative_to(root).as_posix()
            if kind == "deleted" or not path.exists():
                changes.append({"path": rel, "change_type": "deleted"})
                continue
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            changes.append(
                {
                    "path": rel,
                    "change_type": kind if kind in ("created", "modified") else "modified",
                    "content": path.read_text(encoding="utf-8", errors="ignore"),
                }
            )
        if not changes:
            return

        names = ", ".join(c["path"] for c in changes[:4])
        more = f" 외 {len(changes)-4}개" if len(changes) > 4 else ""
        print(f"\n[{time.strftime('%H:%M:%S')}] 변경 감지: {names}{more}")

        result = self._post(
            {
                "workspace_id": self.workspace,
                "changes": changes,
                "llm": self.use_llm,
                "doc_check": True,
                "patent": self.use_patent,
            }
        )
        self._report(result)

    def _report(self, result: dict) -> None:
        findings = result["findings"]
        current = {f["locator"] for f in findings}
        new = current - self.previous
        gone = self.previous - current
        self.previous = current

        tiers = result["stats"]["by_tier"]
        summary = " / ".join(f"{k} {v}" for k, v in sorted(tiers.items())) or "없음"
        print(f"  검사 완료 {result['stats']['elapsed_sec']}초 — 총 {len(findings)}건 ({summary})")

        icon = {"FORBIDDEN": "[X]", "RESTRICTED": "[!]", "REVIEW": "[?]"}
        for f in findings:
            if f["locator"] not in new:
                continue
            print(f"  {icon.get(f['tier'],'   ')} 새 위험  {f['tier']}  {f['locator']}")
            print(f"      {f['license']} — {f['why'][:100]}")
            llm = f.get("llm")
            if llm and llm.get("actions_ko"):
                print(f"      조치: {llm['actions_ko'][0][:100]}")
        for loc in sorted(gone):
            print(f"  [v] 해소  {loc}")
        if not new and not gone:
            print("  변동 없음")


def initial_sync(root: Path) -> dict[Path, str]:
    return {p: "created" for p in root.rglob("*") if p.is_file() and is_watched(p)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", type=Path)
    ap.add_argument("--workspace", default="local")
    ap.add_argument("--api", default="http://127.0.0.1:8000")
    ap.add_argument("--llm", action="store_true", help="Gemini 심층 검토 (건당 6초)")
    ap.add_argument("--patent", action="store_true", help="기획서가 바뀌면 선행 특허 검토")
    args = ap.parse_args()

    root = args.folder.resolve()
    if not root.is_dir():
        print(f"폴더 없음: {root}", file=sys.stderr)
        return 1

    client = Client(args.api, args.workspace, args.llm, args.patent)
    try:
        with urllib.request.urlopen(f"{args.api.rstrip('/')}/health", timeout=10) as r:
            health = json.load(r)
        print(f"서버 연결 OK — 라이선스 {health['policy_licenses']}종, RAG {health['rag_index']}")
    except urllib.error.URLError as exc:
        print(f"서버에 연결할 수 없다 ({args.api}): {exc}", file=sys.stderr)
        return 1

    print(f"감시 시작: {root}")
    print(f"워크스페이스: {args.workspace} / 디바운스 {DEBOUNCE_SEC}초 / LLM {'ON' if args.llm else 'OFF'}")

    first = initial_sync(root)
    print(f"\n[초기 스캔] 대상 파일 {len(first)}개")
    client.send(root, first)

    handler = Debouncer(root, lambda batch: client.send(root, batch))
    observer = Observer()
    observer.schedule(handler, str(root), recursive=True)
    observer.start()
    print("\n대기 중... (Ctrl+C 로 종료)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n종료")
    finally:
        handler.stop()
        observer.stop()
        observer.join()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
