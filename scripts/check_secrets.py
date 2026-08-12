"""제출 전 비밀정보 점검.

.gitignore 는 git 에만 적용된다. 폴더를 통째로 압축해 제출하면 키가 그대로 따라간다.
제출 체크리스트(SECTION 18)의 "환경 변수와 Secret 의 실제 값이 노출되지 않았다" 항목을
사람 눈으로 확인하지 않아도 되게 만든다.

사용:
  python scripts/check_secrets.py            점검만
  python scripts/check_secrets.py --zip      안전한 제출용 ZIP 생성
"""

from __future__ import annotations

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import argparse
import json
import re
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 파일 자체가 비밀인 것
SECRET_FILES = [
    (".env", "API 키 모음"),
    ("*-key.json", "서비스 계정 키로 보임"),
    ("credentials*.json", "서비스 계정 키로 보임"),
    ("service-account*.json", "서비스 계정 키"),
    ("*.pem", "개인키"),
    ("*.p12", "개인키"),
]

# 파일 안에 들어 있으면 안 되는 값
SECRET_PATTERNS = [
    (r'"private_key"\s*:\s*"-----BEGIN', "서비스 계정 개인키"),
    (r"AIza[0-9A-Za-z_-]{30,}", "Google API 키"),
    (r"-----BEGIN (RSA |EC )?PRIVATE KEY-----", "개인키 블록"),
    (r"\bghp_[0-9A-Za-z]{30,}", "GitHub 토큰"),
    (r"sk-[0-9A-Za-z]{20,}", "OpenAI 형식 키"),
]

SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", ".idea", "data"}
TEXT_SUFFIX = {".py", ".md", ".txt", ".json", ".toml", ".cfg", ".yml", ".yaml",
               ".js", ".ts", ".html", ".bat", ".sh", ""}
MAX_SCAN_BYTES = 2_000_000


def walk_files():
    for p in sorted(ROOT.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(ROOT)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        yield p, rel


def find_secret_files() -> list[tuple[Path, str]]:
    hits = []
    for pattern, why in SECRET_FILES:
        for p in ROOT.rglob(pattern):
            if p.is_file() and not any(part in SKIP_DIRS for part in p.relative_to(ROOT).parts):
                # 서비스 계정 키는 내용으로 한 번 더 확인한다 (이름만 비슷한 파일 제외)
                if p.suffix == ".json":
                    try:
                        data = json.loads(p.read_text(encoding="utf-8"))
                    except Exception:
                        continue
                    if data.get("type") != "service_account":
                        continue
                hits.append((p, why))
    return hits


def find_secret_content() -> list[tuple[Path, str, int]]:
    hits = []
    for p, rel in walk_files():
        if p.suffix.lower() not in TEXT_SUFFIX or p.stat().st_size > MAX_SCAN_BYTES:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pattern, why in SECRET_PATTERNS:
            for m in re.finditer(pattern, text):
                line = text[: m.start()].count("\n") + 1
                hits.append((rel, why, line))
                break
    return hits


def safe_zip(out: Path, exclude: set[Path]) -> int:
    count = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for p, rel in walk_files():
            if p in exclude or p.resolve() == out.resolve():
                continue
            zf.write(p, rel.as_posix())
            count += 1
    return count


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip", action="store_true", help="비밀정보를 뺀 제출용 ZIP 생성")
    args = ap.parse_args()

    files = find_secret_files()
    content = find_secret_content()

    print(f"점검 대상: {ROOT}\n")
    if files:
        print(f"[비밀 파일 {len(files)}개] — 제출물에 포함되면 안 됩니다")
        for p, why in files:
            print(f"   {p.relative_to(ROOT).as_posix():44s} {why}")
    else:
        print("[비밀 파일] 없음")

    print()
    if content:
        print(f"[파일 내용에 노출된 비밀 {len(content)}건] — 반드시 제거하세요")
        for rel, why, line in content:
            print(f"   {rel.as_posix()}:{line}  {why}")
    else:
        print("[파일 내용] 노출된 키 없음")

    # 키가 프로젝트 폴더 밖에 있으면 애초에 압축에 안 딸려간다
    outside = []
    for name in ("GOOGLE_DRIVE_CREDENTIALS",):
        env = ROOT / ".env"
        if env.exists():
            for line in env.read_text(encoding="utf-8").splitlines():
                if line.startswith(name + "="):
                    val = line.split("=", 1)[1].strip()
                    if val and not Path(val).resolve().is_relative_to(ROOT):
                        outside.append(name)

    print()
    if files or content:
        print("권장 조치")
        print("   1. 서비스 계정 키를 프로젝트 밖으로 옮기고 .env 경로만 수정")
        print("      예: C:\\Users\\<이름>\\.secrets\\drive-key.json")
        print("   2. 제출은 아래 명령으로 만든 ZIP 을 쓰세요")
        print("      python scripts/check_secrets.py --zip")
    if outside:
        print(f"   (참고) {', '.join(outside)} 는 이미 프로젝트 밖에 있습니다")

    if args.zip:
        out = ROOT.parent / f"{ROOT.name}-제출용.zip"
        n = safe_zip(out, exclude={p for p, _ in files})
        print(f"\n제출용 ZIP 생성: {out}")
        print(f"   파일 {n}개 · 비밀 파일 {len(files)}개 제외 · data/ 제외")
        if content:
            print("   ! 파일 '내용'에 남은 비밀은 ZIP 에도 들어갑니다. 위 목록을 먼저 지우세요.")

    return 1 if (files or content) else 0


if __name__ == "__main__":
    raise SystemExit(main())
