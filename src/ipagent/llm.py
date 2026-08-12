"""Gemini 연결.

google-genai SDK 하나로 AI Studio / Vertex AI 를 모두 다룬다.
(구 Vertex AI SDK 의 생성 모듈은 2026-06-24 제거됨)

전환은 환경변수로만 한다 — 코드 변경 없음.
    GEMINI_BACKEND=aistudio  + GEMINI_API_KEY
    GEMINI_BACKEND=vertex    + GOOGLE_CLOUD_PROJECT / GOOGLE_CLOUD_LOCATION

호출은 1단계 규칙 게이트를 통과한 건에 대해서만 한다.
무료 등급이 10 RPM 이라 무차별 호출은 즉시 막힌다 (SECTION 08).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache

from google import genai
from google.genai import types

DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3-flash-preview")

# Gemini 3 은 답을 만들기 전에 '생각' 토큰을 쓰고, 그게 출력 요금으로 과금된다.
# 우리 작업은 근거가 이미 주어진 상태에서 비교·인용하는 일이라 길게 추론할 여지가 적다.
# 실측: 특허 판정 1건이 8.6초/thinking 1274 -> 2.4초/thinking 0 이 되면서 결과는 동일했다.
# 값: low(기본) | high | off
THINKING = os.environ.get("GEMINI_THINKING", "low").lower()


def thinking_config():
    """GenerateContentConfig 에 넣을 thinking 설정. 필요 없으면 빈 dict."""
    if THINKING in ("off", "none", "0"):
        return {"thinking_config": types.ThinkingConfig(thinking_budget=0)}
    if THINKING in ("low", "high"):
        return {"thinking_config": types.ThinkingConfig(thinking_level=THINKING)}
    return {}

# 출력 스키마. 모델이 자유 서술로 새는 것을 막는다.
ASSESSMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["CONFIRMED", "NEEDS_REVIEW", "NOT_A_RISK"],
            "description": "규칙 판정에 동의하는지",
        },
        "explanation_ko": {
            "type": "string",
            "description": "왜 위험한지 2~3문장. 반드시 제공된 근거 문서에 있는 내용만 사용",
        },
        "obligations_ko": {
            "type": "array",
            "items": {"type": "string"},
            "description": "이 라이선스가 요구하는 의무사항. 근거 문서에 명시된 것만",
        },
        "actions_ko": {
            "type": "array",
            "items": {"type": "string"},
            "description": "개발팀이 취할 수 있는 조치. 구체적으로",
        },
        "needs_legal_review": {
            "type": "boolean",
            "description": "변호사 검토가 필요한 사안인지",
        },
        "grounded": {
            "type": "boolean",
            "description": "제공된 근거만으로 판단했으면 true. 추측이 섞였으면 false",
        },
    },
    "required": [
        "verdict",
        "explanation_ko",
        "obligations_ko",
        "actions_ko",
        "needs_legal_review",
        "grounded",
    ],
}

SYSTEM_PROMPT = """당신은 소프트웨어 프로젝트의 오픈소스 라이선스 리스크를 설명하는 도우미다.

역할
- 규칙 엔진이 이미 내린 등급 판정을 검토하고, 개발팀이 이해할 수 있게 설명한다.
- 제공된 "근거 문서"에 실제로 적힌 내용만 사용한다.

금지
- 근거 문서에 없는 조항, 판례, 법조문을 지어내지 않는다.
- 침해 여부에 대한 법적 결론을 내리지 않는다. 판단은 "검토가 필요한 지점"까지다.
- 근거가 부족하면 verdict 를 NEEDS_REVIEW 로 하고 grounded 를 false 로 둔다.

불확실성 처리
- 근거 문서가 비어 있거나 질문과 무관하면 추측하지 말고 그 사실을 explanation_ko 에 적는다.
"""

USER_TEMPLATE = """## 검사 대상 프로젝트
배포 형태: {baseline}

## 규칙 엔진 판정
- 위치: {locator}
- 라이선스: {license}
- 등급: {tier}
- 판정 사유: {why}

## 근거 문서 (출처: {source})
{evidence}

## 요청
위 근거만 사용해 이 항목을 검토하고 지정된 JSON 형식으로 답하라.
근거 문서가 비어 있으면 grounded=false, verdict=NEEDS_REVIEW 로 하라."""


@dataclass
class LLMConfig:
    backend: str
    model: str


@lru_cache(maxsize=1)
def get_client() -> tuple[genai.Client, LLMConfig]:
    backend = os.environ.get("GEMINI_BACKEND", "aistudio").lower()
    model = DEFAULT_MODEL

    if backend == "vertex":
        project = os.environ.get("GOOGLE_CLOUD_PROJECT")
        location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
        if not project:
            raise RuntimeError("GEMINI_BACKEND=vertex 인데 GOOGLE_CLOUD_PROJECT 가 없다")
        client = genai.Client(vertexai=True, project=project, location=location)
    else:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY 가 없다. .env 를 확인할 것")
        client = genai.Client(api_key=api_key)

    return client, LLMConfig(backend=backend, model=model)


def list_models() -> list[str]:
    """사용 가능한 모델 ID 확인용. 모델명은 자주 바뀌므로 실제 목록으로 검증한다."""
    client, _ = get_client()
    out = []
    for m in client.models.list():
        actions = getattr(m, "supported_actions", None) or []
        if not actions or "generateContent" in actions:
            out.append(m.name)
    return out


def assess(finding: dict, baseline: str, evidence: str = "", source: str = "") -> dict:
    """규칙 판정 1건을 Gemini 로 검토한다. 실패해도 파이프라인을 막지 않는다."""
    client, cfg = get_client()
    prompt = USER_TEMPLATE.format(
        baseline=baseline,
        locator=finding.get("locator", ""),
        license=finding.get("license") or "(미상)",
        tier=finding.get("tier", ""),
        why=finding.get("why", ""),
        source=source or finding.get("evidence_source") or "(없음)",
        evidence=(evidence or finding.get("evidence_ko") or "(제공된 근거 없음)"),
    )

    try:
        resp = client.models.generate_content(
            model=cfg.model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                temperature=0.2,
                response_mime_type="application/json",
                response_schema=ASSESSMENT_SCHEMA,
                **thinking_config(),
            ),
        )
        data = json.loads(resp.text)
        data["_model"] = cfg.model
        return data
    except Exception as exc:  # 모델 실패 시 규칙 판정만으로 리포트가 나가야 한다
        return {
            "verdict": "NEEDS_REVIEW",
            "explanation_ko": f"LLM 검토를 수행하지 못했다: {type(exc).__name__}",
            "obligations_ko": [],
            "actions_ko": ["규칙 판정 결과만으로 수동 확인 필요"],
            "needs_legal_review": True,
            "grounded": False,
            "_error": str(exc)[:300],
            "_model": cfg.model,
        }


# 무료 등급은 분당 10회다. 결제를 켜면 훨씬 올라가므로 값으로 빼둔다.
# 이 값만 올리면 아래 병렬 실행이 자동으로 빨라진다.
RPM = int(os.environ.get("GEMINI_RPM", "10"))


class RateLimiter:
    """분당 호출 수를 지키는 토큰 버킷.

    호출 '시작' 시각을 기준으로 간격을 벌린다. 여러 스레드가 동시에 들어와도
    이 잠금을 통과하는 순서대로만 나간다.
    """

    def __init__(self, rpm: int):
        import threading

        self.interval = 60.0 / max(rpm, 1)
        self._lock = threading.Lock()
        self._next = 0.0

    def acquire(self) -> None:
        import time as _t

        with self._lock:
            now = _t.monotonic()
            wait = self._next - now
            if wait > 0:
                _t.sleep(wait)
                now = _t.monotonic()
            self._next = now + self.interval


def run_limited(jobs, rpm: int | None = None, workers: int | None = None,
                on_done=None) -> list:
    """작업 목록을 레이트리밋을 지키며 동시에 실행한다.

    jobs: 인자 없는 호출 가능 객체들.
    반환은 입력 순서를 유지한다. 개별 실패는 None 으로 남기고 전체를 멈추지 않는다.
    """
    from concurrent.futures import ThreadPoolExecutor

    rpm = rpm or RPM
    limiter = RateLimiter(rpm)
    # 분당 한도가 낮으면 스레드를 많이 띄워도 대기만 늘어난다.
    workers = workers or max(1, min(8, rpm // 4 or 1))
    results: list = [None] * len(jobs)

    def wrapped(i, fn):
        limiter.acquire()
        try:
            results[i] = fn()
        except Exception:
            results[i] = None
        finally:
            if on_done:
                on_done(i)

    if workers <= 1:
        for i, fn in enumerate(jobs):
            wrapped(i, fn)
        return results

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(lambda p: wrapped(*p), enumerate(jobs)))
    return results
