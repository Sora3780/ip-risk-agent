"""RAG 인덱스 생성.

data/license_policy.json (84종) -> 조항 단위 청킹 -> 임베딩 -> data/rag_index.npz

사용: python scripts/build_index.py
"""

from __future__ import annotations

import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

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
            os.environ.setdefault(k.strip(), v.strip())


def main() -> int:
    load_env()
    from ipagent.rag import EMBED_DIM, EMBED_MODEL, INDEX_PATH, Retriever, build_index

    print(f"모델 {EMBED_MODEL} / 차원 {EMBED_DIM}")
    n = build_index()
    size_kb = INDEX_PATH.stat().st_size / 1024
    print(f"청크 {n}개 임베딩 완료 -> {INDEX_PATH.name} ({size_kb:.0f} KB)\n")

    # 검색이 실제로 되는지 확인. 라이선스별로 다른 조항이 나와야 한다.
    r = Retriever()
    probes = [
        ("GPL-3.0", "비공개 상용 제품에 이 라이브러리를 넣어 배포하면 소스를 공개해야 하나"),
        ("LGPL-2.1", "동적 링크만 하면 소스 공개를 피할 수 있나"),
        ("MIT", "재배포할 때 무엇을 지켜야 하나"),
    ]
    for spdx, q in probes:
        print(f"[{spdx}] {q}")
        for h in r.search(q, spdx=spdx, k=2):
            body = h["text"].split("] ", 1)[-1]
            print(f"   {h['score']:.3f} ({h['section']}) {body[:88]}…")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
