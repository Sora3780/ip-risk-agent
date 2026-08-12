"""라이선스 근거 검색 (RAG).

84종 라이선스의 의무사항 원문을 조항 단위로 쪼개 임베딩하고,
탐지 상황에 맞는 조항만 검색해 Gemini 에 넘긴다.

라이선스 전체를 통째로 넘기면 프롬프트가 길어지고 무관한 조항이 섞인다.
검색 단계에서 좁혀야 근거 인용이 정확해진다.

검색은 하이브리드다:
  1) 문제가 된 라이선스로 후보를 먼저 좁히고 (정확도)
  2) 그 안에서 상황 질의로 의미 검색한다 (관련성)
후보가 없으면 전체에서 의미 검색으로 폴백한다.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
from google import genai
from google.genai import types

ROOT = Path(__file__).resolve().parents[2]
INDEX_PATH = ROOT / "data" / "rag_index.npz"
META_PATH = ROOT / "data" / "rag_meta.json"

EMBED_MODEL = os.environ.get("GEMINI_EMBED_MODEL", "gemini-embedding-001")
EMBED_DIM = 768  # MRL 로 잘라 쓴다. 인덱스 파일이 작아지고 검색 품질 차이는 미미하다.
MAX_CHARS = 320
BATCH = 16

_SECTION = re.compile(r"^\s*(주요\s*특징|배포\s*시?\s*의무사항|의무사항|특징)\s*[:：]\s*$")


@dataclass
class Chunk:
    id: str
    lid: str
    name: str
    spdx: str
    tier: str
    section: str
    text: str


def _split_sections(raw: str) -> list[tuple[str, list[str]]]:
    """'주요 특징:' / '배포시 의무사항:' 머리말 기준으로 나눈다."""
    sections: list[tuple[str, list[str]]] = [("개요", [])]
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        m = _SECTION.match(line)
        if m:
            sections.append((m.group(1).replace(" ", ""), []))
        else:
            sections[-1][1].append(line)
    return [(name, lines) for name, lines in sections if lines]


def build_chunks(policy: list[dict]) -> list[Chunk]:
    chunks: list[Chunk] = []
    for entry in policy:
        raw = entry.get("evidence_ko") or ""
        for section, lines in _split_sections(raw):
            buf: list[str] = []
            size = 0
            for line in lines + [None]:  # None = flush 신호
                if line is not None and size + len(line) <= MAX_CHARS:
                    buf.append(line)
                    size += len(line)
                    continue
                if buf:
                    idx = len(chunks)
                    chunks.append(
                        Chunk(
                            id=f"{entry['lid']}-{idx}",
                            lid=entry["lid"],
                            name=entry["name"],
                            spdx=entry.get("spdx", ""),
                            tier=entry.get("tier", ""),
                            section=section,
                            # 라이선스명을 본문에 넣어야 의미 검색이 라이선스를 구분한다
                            text=f"[{entry['name']} / {section}] " + " ".join(buf),
                        )
                    )
                if line is not None:
                    buf, size = [line], len(line)
    return chunks


def _client() -> genai.Client:
    from .llm import get_client

    client, _ = get_client()
    return client


def embed_texts(texts: list[str], task_type: str) -> np.ndarray:
    """배치 임베딩. 무료 등급 레이트리밋을 고려해 사이사이 쉰다."""
    client = _client()
    vectors: list[list[float]] = []
    for i in range(0, len(texts), BATCH):
        batch = texts[i : i + BATCH]
        resp = client.models.embed_content(
            model=EMBED_MODEL,
            contents=batch,
            config=types.EmbedContentConfig(
                task_type=task_type,
                output_dimensionality=EMBED_DIM,
            ),
        )
        vectors.extend(e.values for e in resp.embeddings)
        if i + BATCH < len(texts):
            time.sleep(0.4)
    arr = np.asarray(vectors, dtype=np.float32)
    # output_dimensionality 로 자른 벡터는 정규화가 풀려 있어 다시 정규화해야 한다.
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    return arr / np.clip(norms, 1e-9, None)


def build_index(policy_path: Path | None = None) -> int:
    policy = json.loads(
        (policy_path or ROOT / "data" / "license_policy.json").read_text(encoding="utf-8")
    )
    chunks = build_chunks(policy)
    if not chunks:
        raise RuntimeError("청크가 0개다. license_policy.json 을 확인할 것")

    vectors = embed_texts([c.text for c in chunks], "RETRIEVAL_DOCUMENT")
    np.savez_compressed(INDEX_PATH, vectors=vectors)
    META_PATH.write_text(
        json.dumps([asdict(c) for c in chunks], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return len(chunks)


class Retriever:
    def __init__(self) -> None:
        if not INDEX_PATH.exists() or not META_PATH.exists():
            raise FileNotFoundError("인덱스가 없다. python scripts/build_index.py 를 먼저 실행할 것")
        self.vectors: np.ndarray = np.load(INDEX_PATH)["vectors"]
        self.meta: list[dict] = json.loads(META_PATH.read_text(encoding="utf-8"))

    def _candidates(self, spdx: str | None, license_name: str | None) -> np.ndarray:
        """문제가 된 라이선스에 해당하는 청크 인덱스."""
        if not spdx and not license_name:
            return np.arange(len(self.meta))
        key = (spdx or "").lower().replace("-only", "").replace("-or-later", "")
        hits = []
        for i, m in enumerate(self.meta):
            ms = (m.get("spdx") or "").lower().replace("-only", "").replace("-or-later", "")
            if key and ms and (ms.startswith(key) or key.startswith(ms)):
                hits.append(i)
            elif license_name and license_name.lower() in m["name"].lower():
                hits.append(i)
        return np.asarray(hits, dtype=int) if hits else np.arange(len(self.meta))

    def search(self, query: str, spdx: str | None = None, k: int = 3) -> list[dict]:
        qv = embed_texts([query], "RETRIEVAL_QUERY")[0]
        idx = self._candidates(spdx, None)
        scores = self.vectors[idx] @ qv
        order = np.argsort(-scores)[:k]
        out = []
        for pos in order:
            m = dict(self.meta[idx[pos]])
            m["score"] = round(float(scores[pos]), 4)
            out.append(m)
        return out

    def evidence_for(self, spdx: str | None, situation: str, k: int = 3) -> tuple[str, str]:
        """(근거 텍스트, 출처 표기). Gemini 프롬프트에 그대로 넣는다."""
        hits = self.search(situation, spdx=spdx, k=k)
        if not hits:
            return "", ""
        body = "\n\n".join(f"- ({h['name']} / {h['section']}) {h['text'].split('] ', 1)[-1]}" for h in hits)
        cites = ", ".join(sorted({h["name"] for h in hits}))
        return body, f"한국저작권위원회 오픈소스SW 라이선스정보 서비스 — {cites}"
