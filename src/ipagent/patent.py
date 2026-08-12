"""기획서 -> 선행 특허 대조.

라이선스 쪽과 파이프라인 구조는 같다. 다른 것은 Grounding Source 뿐이다.
  라이선스: 규칙 게이트 -> RAG(84종) -> Gemini 검토
  특허    : 키워드 추출 -> KIPRIS 검색 -> 초록 조회 -> Gemini 유사도 판정

범위 제한: 침해 여부를 판정하지 않는다. "전문가 조사가 필요한 지점"까지다.

실측으로 확인한 KIPRIS 스펙 (2026-08):
  KpaGeneralSearchService/anySearch      검색. 응답 항목 태그는 <searchResult>
                                         searchAny 는 단어 AND 조건이라 5단어면 0건이 된다
  KpaBibliographicService/bibliographicInfo   영문초록(astrtCont) — 검색 응답에는 초록이 없다
  KorAbstractInfoService/korAbstractInfo      국문 '명칭'만 준다. 국문 초록 본문은 없다
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

import requests

from .quota import spend

ROOT = __import__('pathlib').Path(__file__).resolve().parents[2]

SEARCH_URL = "https://plus.kipris.or.kr/openapi/rest/KpaGeneralSearchService/anySearch"
BIB_URL = "https://plus.kipris.or.kr/openapi/rest/KpaBibliographicService/bibliographicInfo"
KOR_URL = "https://plus.kipris.or.kr/openapi/rest/KorAbstractInfoService/korAbstractInfo"

# --------------------------------------------------------------- 키워드 추출

QUERY_SCHEMA = {
    "type": "object",
    "properties": {
        "core_idea_ko": {
            "type": "string",
            "description": "기획서가 주장하는 핵심 기술 아이디어를 1~2문장으로",
        },
        "queries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "en": {"type": "string", "description": "영문 검색어. 반드시 2~3단어."},
                    "ko": {"type": "string"},
                    "aspect": {"type": "string", "description": "겨냥하는 기술 측면"},
                },
                "required": ["en", "ko", "aspect"],
            },
        },
    },
    "required": ["core_idea_ko", "queries"],
}

QUERY_PROMPT = """다음은 스타트업의 서비스 기획서다.
유사한 선행 특허를 찾기 위한 **영문 검색어**를 만들어라.

가장 중요한 제약
- 검색 엔진이 단어를 AND 로 묶는다. **단어가 많을수록 결과가 0건이 된다.**
- 따라서 각 검색어는 **반드시 2~3단어**로 하라. 4단어 이상은 실패한다.
  나쁜 예: "real-time voice phishing detection using speech-to-text"  (0건)
  좋은 예: "voice phishing detection"  (69건)
- 하이픈으로 이어진 복합어는 한 단어로 세지만 피하는 편이 낫다.

나머지 규칙
- 마케팅 표현이 아니라 기술 구성 요소를 골라라.
- 서로 다른 기술 측면을 겨냥해라. 같은 말 바꿔쓰기는 쓸모없다.
- 특허 문헌에서 실제로 쓰이는 용어로 골라라.
- 5~6개 만들어라. 넓은 것(2단어)과 좁은 것(3단어)을 섞어라.

기획서:
---
{document}
---"""

# --------------------------------------------------------------- 유사도 판정

SIMILARITY_SCHEMA = {
    "type": "object",
    "properties": {
        "similarity": {
            "type": "string",
            "enum": ["HIGH", "MEDIUM", "LOW", "UNRELATED"],
        },
        "overlap_ko": {"type": "string", "description": "겹치는 기술 구성. 초록에 실제 있는 내용만"},
        "difference_ko": {"type": "string", "description": "기획서에 있으나 이 특허에 없어 보이는 부분"},
        # 요약만으로는 "어디가 겹치는지" 를 사용자가 짚을 수 없다.
        # 양쪽에서 실제 문장을 그대로 떠와 짝을 지어야 화면에서 같은 색으로 칠할 수 있다.
        "matches": {
            "type": "array",
            "description": "겹치는 부분을 문장 단위로 짝지은 목록. 최대 5쌍.",
            "items": {
                "type": "object",
                "properties": {
                    "document_quote": {
                        "type": "string",
                        "description": "기획서에서 그대로 복사한 문장. 한 글자도 바꾸지 말 것.",
                    },
                    "patent_quote": {
                        "type": "string",
                        "description": "특허 초록에서 그대로 복사한 문장. 한 글자도 바꾸지 말 것.",
                    },
                    "why_ko": {"type": "string", "description": "이 둘이 왜 같은 구성인지 한 문장"},
                },
                "required": ["document_quote", "patent_quote", "why_ko"],
            },
        },
        "needs_expert_review": {"type": "boolean"},
        "grounded": {"type": "boolean", "description": "제공된 초록만으로 판단했으면 true"},
    },
    "required": ["similarity", "overlap_ko", "difference_ko", "matches",
                 "needs_expert_review", "grounded"],
}

SIMILARITY_PROMPT = """기획서와 아래 선행 특허를 비교하라.

## 우리 기획서 (핵심 아이디어)
{idea}

## 우리 기획서 (원문)
{document}

## 선행 특허
출원번호: {app_no}
명칭(국문): {title_ko}
명칭(영문): {title_en}
초록(영문):
{abstract}

## 지시
- 제공된 초록에 실제로 적힌 내용만 근거로 삼아라. 전문을 본 것처럼 쓰지 마라.
- **침해 여부를 판정하지 마라.** 기술적 유사도와 조사가 필요한 지점까지만 말하라.
- 청구범위를 보지 않았으므로 확정적 표현을 쓰지 마라.
- 초록이 비었거나 판단에 부족하면 grounded=false, similarity=UNRELATED, matches=[].
- 설명은 한국어로 쓰라.

## matches 작성 규칙 (가장 중요)
겹치는 부분을 **양쪽 원문에서 문장을 그대로 복사해** 짝지어라.
- document_quote 는 위 '우리 기획서 (원문)' 에 **글자 그대로 존재하는** 연속된 문장이어야 한다.
- patent_quote 는 위 '초록(영문)' 에 **글자 그대로 존재하는** 연속된 문장이어야 한다.
- 요약하거나 다듬지 마라. 복사-붙여넣기라고 생각하라. 한 글자라도 바꾸면 화면에서 표시되지 않는다.
- 기술 구성이 겹치는 것만 짝지어라. 문제의식이나 목적이 같다고 짝짓지 마라.
- 겹치는 게 없으면 빈 배열로 두어라."""


@dataclass
class Patent:
    application_number: str
    title_en: str = ""
    title_ko: str = ""
    abstract_en: str = ""
    applicant: str = ""
    application_date: str = ""
    ipc: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def title(self) -> str:
        return self.title_ko or self.title_en


class KiprisError(RuntimeError):
    pass


def _text(el: ET.Element) -> dict:
    return {c.tag: (c.text or "").strip() for c in el}


class KiprisClient:
    """KIPRIS Plus. 무료 등급은 상품당 월 1,000회이므로 응답을 캐시한다."""

    def __init__(self, access_key: str | None = None, cache_path=None):
        self.key = access_key or os.environ.get("KIPRIS_ACCESS_KEY", "")
        if not self.key:
            raise KiprisError(
                "KIPRIS_ACCESS_KEY 가 비어 있다. KIPRIS Plus 에서 "
                "'한국특허영문초록(KPA)' 과 '기계번역용 국문초록' 승인 후 .env 에 넣을 것"
            )
        from pathlib import Path

        self.cache_path = Path(cache_path) if cache_path else None
        self.cache: dict = {}
        if self.cache_path and self.cache_path.exists():
            self.cache = json.loads(self.cache_path.read_text(encoding="utf-8"))

    def _get(self, url: str, params: dict) -> ET.Element:
        key = f"{url}?{sorted(params.items())}"
        if key in self.cache:
            return ET.fromstring(self.cache[key])
        # 캐시에 없을 때만 실제 호출이므로 여기서만 예산을 깎는다.
        spend("kipris")
        r = requests.get(url, params={**params, "accessKey": self.key}, timeout=30)
        r.raise_for_status()
        code = re.search(r"<resultCode>(\d+)</resultCode>", r.text)
        if code and code.group(1) not in ("00", "0"):
            msg = re.search(r"<resultMsg>([^<]*)</resultMsg>", r.text)
            raise KiprisError(f"{msg.group(1) if msg else ''} (code={code.group(1)})")
        self.cache[key] = r.text
        return ET.fromstring(r.text)

    def save_cache(self) -> None:
        if self.cache_path:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(
                json.dumps(self.cache, ensure_ascii=False), encoding="utf-8"
            )

    # -- 검색 -----------------------------------------------------------
    def search(self, query: str, rows: int = 5) -> tuple[list[Patent], int]:
        root = self._get(SEARCH_URL, {"searchAny": query, "docsCount": rows, "currentPage": 1})
        total = int(root.findtext(".//totalSearchCount") or 0)
        out = []
        for el in root.iter("searchResult"):  # <item> 이 아니다
            raw = _text(el)
            app = raw.get("applicationNo", "")
            if not app:
                continue
            out.append(
                Patent(
                    application_number=app,
                    title_en=raw.get("inventionName", ""),
                    applicant=raw.get("applicant", "").split("|")[0],
                    application_date=raw.get("applicationDate", ""),
                    ipc=raw.get("ipc", ""),
                    raw=raw,
                )
            )
        return out, total

    # -- 상세 -----------------------------------------------------------
    def english_abstract(self, app_no: str) -> str:
        """검색 응답에는 초록이 없다. 서지정보에서 따로 가져와야 한다."""
        try:
            root = self._get(BIB_URL, {"applicationNumber": app_no})
        except (KiprisError, requests.RequestException):
            return ""
        return (root.findtext(".//astrtCont") or "").strip()

    def korean_title(self, app_no: str) -> str:
        """기계번역용 국문초록 서비스는 국문 '명칭'만 준다 (초록 본문 없음)."""
        try:
            root = self._get(KOR_URL, {"applicationNumber": app_no})
        except (KiprisError, requests.RequestException):
            return ""
        return (root.findtext(".//inventionName") or "").strip().rstrip("@")

    def enrich(self, p: Patent) -> Patent:
        p.abstract_en = self.english_abstract(p.application_number)
        p.title_ko = self.korean_title(p.application_number)
        return p


# --------------------------------------------------------------- 파이프라인

QUERY_CACHE = ROOT / "data" / "patent_query_cache.json"


def extract_queries(document: str) -> dict:
    """문서 -> 검색어. 같은 문서면 같은 검색어를 쓴다.

    temperature 가 0 이 아니라 매번 다른 검색어가 나오면 KIPRIS 캐시가 전부 빗나가
    호출량이 그대로 늘어난다. 문서 해시로 계획 자체를 캐시한다.
    """
    import hashlib

    from google.genai import types

    from .llm import get_client, thinking_config

    digest = hashlib.sha256(document.encode("utf-8")).hexdigest()[:16]
    cache = {}
    if QUERY_CACHE.exists():
        try:
            cache = json.loads(QUERY_CACHE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cache = {}
    if digest in cache:
        return cache[digest]

    spend("gemini")
    client, cfg = get_client()
    resp = client.models.generate_content(
        model=cfg.model,
        contents=QUERY_PROMPT.format(document=document[:12000]),
        config=types.GenerateContentConfig(
            temperature=0.3,
            response_mime_type="application/json",
            response_schema=QUERY_SCHEMA,
            **thinking_config(),
        ),
    )
    plan = json.loads(resp.text)
    # 프롬프트로 막아도 긴 검색어가 새어 나오면 앞 3단어만 쓴다.
    for q in plan["queries"]:
        words = q["en"].split()
        if len(words) > 3:
            q["en_original"] = q["en"]
            q["en"] = " ".join(words[:3])

    cache[digest] = plan
    QUERY_CACHE.parent.mkdir(parents=True, exist_ok=True)
    QUERY_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    return plan


def _normalize(s: str) -> str:
    return " ".join((s or "").split()).lower()


def verify_matches(verdict: dict, document: str, abstract: str) -> dict:
    """LLM 이 인용했다는 문장이 정말 원문에 있는지 확인한다.

    모델은 요약하거나 살짝 다듬는 버릇이 있다. 그대로 믿고 화면에서 강조하려 하면
    찾지 못해 아무 표시도 안 되거나, 없는 문장을 '원문' 이라고 보여주게 된다.
    실제로 존재하는 인용만 verified 로 표시하고, 나머지는 걸러낸다.
    """
    doc_n, abs_n = _normalize(document), _normalize(abstract)
    kept, dropped = [], 0
    for m in verdict.get("matches") or []:
        d, a = m.get("document_quote", ""), m.get("patent_quote", "")
        m["document_verified"] = bool(d) and _normalize(d) in doc_n
        m["patent_verified"] = bool(a) and _normalize(a) in abs_n
        if m["document_verified"] or m["patent_verified"]:
            kept.append(m)
        else:
            dropped += 1
    verdict["matches"] = kept[:5]
    if dropped:
        verdict["_dropped_matches"] = dropped
    return verdict


def assess_similarity(idea: str, patent: Patent, document: str = "") -> dict:
    from google.genai import types

    from .llm import get_client, thinking_config

    if not patent.abstract_en.strip():
        return {
            "similarity": "UNRELATED", "overlap_ko": "", "difference_ko": "",
            "matches": [], "needs_expert_review": False, "grounded": False,
            "_note": "초록 없음",
        }
    spend("gemini")
    client, cfg = get_client()
    try:
        resp = client.models.generate_content(
            model=cfg.model,
            contents=SIMILARITY_PROMPT.format(
                idea=idea,
                document=document[:6000] or "(원문 없음)",
                app_no=patent.application_number,
                title_ko=patent.title_ko or "(없음)",
                title_en=patent.title_en,
                abstract=patent.abstract_en[:4000],
            ),
            config=types.GenerateContentConfig(
                temperature=0.2,
                response_mime_type="application/json",
                response_schema=SIMILARITY_SCHEMA,
                **thinking_config(),
            ),
        )
        return verify_matches(json.loads(resp.text), document, patent.abstract_en)
    except Exception as exc:
        return {
            "similarity": "UNRELATED", "overlap_ko": "", "difference_ko": "",
            "matches": [], "needs_expert_review": True, "grounded": False,
            "_error": str(exc)[:200],
        }


def screen_document(document: str, per_query: int = 5, max_assess: int = 8,
                    rpm: int | None = None, cache_path=None) -> dict:
    """기획서 하나를 선행 특허와 대조한다."""
    plan = extract_queries(document)
    client = KiprisClient(cache_path=cache_path)

    seen: dict[str, Patent] = {}
    plan["search_stats"] = []
    for q in plan["queries"]:
        try:
            hits, total = client.search(q["en"], rows=per_query)
        except KiprisError as exc:
            plan["search_stats"].append({"query": q["en"], "error": str(exc)})
            continue
        plan["search_stats"].append({"query": q["en"], "total": total, "taken": len(hits)})
        for p in hits:
            existing = seen.setdefault(p.application_number, p)
            # 여러 검색어에 겹쳐 나온 특허 = 여러 기술 측면에서 걸린 특허
            existing.raw["_hits"] = existing.raw.get("_hits", 0) + 1
            existing.raw.setdefault("_matched_queries", []).append(q["en"])

    # 후보가 판정 한도를 넘으면 무엇을 기준으로 자를지 정해야 한다.
    # 여러 검색어에 중복 등장한 특허일수록 관련성이 높다고 보고 그 순으로 정렬한다.
    ranked = sorted(seen.values(), key=lambda p: -p.raw.get("_hits", 1))
    targets = ranked[:max_assess]
    skipped = [
        {"application_number": p.application_number, "title_en": p.title_en}
        for p in ranked[max_assess:]
    ]

    # 상세 조회는 대부분 캐시에 걸리므로 먼저 한 번에 끝낸다.
    for p in targets:
        client.enrich(p)

    # 유사도 판정만 레이트리밋을 지키며 동시에 돌린다.
    # 여기가 특허 파이프라인에서 가장 오래 걸리는 구간이다.
    from .llm import run_limited

    verdicts = run_limited(
        [(lambda pt=p: assess_similarity(plan["core_idea_ko"], pt, document=document))
         for p in targets],
        rpm=rpm,
    )
    results = [
        {"patent": p, "verdict": v}
        for p, v in zip(targets, verdicts)
        if v and v.get("similarity") in ("HIGH", "MEDIUM")
    ]
    client.save_cache()

    order = {"HIGH": 0, "MEDIUM": 1}
    results.sort(key=lambda r: order.get(r["verdict"]["similarity"], 9))
    return {
        "plan": plan,
        "candidates": len(seen),
        "assessed": len(targets),
        # 조용히 자르면 "전부 검사했다"로 읽힌다. 미판정 건을 명시적으로 돌려준다.
        "not_assessed": len(skipped),
        "not_assessed_list": skipped,
        "coverage_note": (
            f"검색 후보 {len(seen)}건 중 상위 {len(targets)}건만 유사도 판정했다. "
            f"나머지 {len(skipped)}건은 미판정 — 위험 없음을 뜻하지 않는다."
            if skipped else f"검색 후보 {len(seen)}건 전부 판정했다."
        ),
        "findings": results,
    }


DOC_PATTERNS = re.compile(r"(기획|제안|명세|설계|proposal|spec|design|prd)", re.I)


def find_documents(root) -> list:
    """기획서로 보이는 문서만 고른다. 모든 마크다운을 태우면 호출이 폭발한다."""
    from pathlib import Path

    out = []
    for p in Path(root).rglob("*"):
        if p.suffix.lower() not in (".md", ".txt"):
            continue
        if any(part.startswith(".") for part in p.parts):
            continue
        if p.name.upper().startswith(("README", "LICENSE", "CHANGELOG", "NOTICE")):
            continue
        if DOC_PATTERNS.search(p.name) or DOC_PATTERNS.search(p.parent.name):
            out.append(p)
    return out
