"""라이선스 위험 판정.

판정 근거는 SPDX 식별자에 대한 자체 규칙이다.
한국저작권위원회 API의 optFinishLicenseYn 플래그는 GPLv2를 누락하고
ISC를 오탐하므로 판정에 쓰지 않는다 (84종 전수 검증, SECTION 11.1).
공공데이터는 "근거 인용문" 제공에만 사용한다.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = ROOT / "data" / "license_policy.json"

TIER_ORDER = ["NOTICE", "REVIEW", "RESTRICTED", "FORBIDDEN"]
UNKNOWN = "REVIEW"

# SPDX 식별자(소문자, 접미사 제거 전 원본) 접두 규칙. 위에서부터 먼저 맞는 것.
SPDX_RULES: list[tuple[str, tuple[str, ...]]] = [
    (
        "FORBIDDEN",
        (
            "agpl-", "gpl-1.0", "gpl-2.0", "gpl-3.0",
            "osl-", "npos", "rpl-", "cpal-", "eupl-", "cecill-2", "cecill-1",
            "sleepycat", "sspl-", "watcom-",
        ),
    ),
    (
        "RESTRICTED",
        (
            "lgpl-", "mpl-", "epl-", "cddl-", "cpl-", "ipl-", "apsl-",
            "ms-rl", "spl-", "nokos", "rscpl", "rpsl-", "motosoto", "jabberpl",
            "wxwindows", "lppl-", "ofl-", "ipa", "nasa-", "sissl",
            "artistic-1", "qpl-",
        ),
    ),
    (
        "NOTICE",
        (
            "mit", "bsd-", "0bsd", "apache-", "isc", "zlib", "libpng",
            "python-", "psf-", "cnri-", "hpnd", "ntp", "miros", "multics",
            "unlicense", "cc0-", "boost", "bsl-", "w3c", "ncsa", "postgresql",
            "ms-pl", "ecl-", "afl-", "artistic-2", "curl", "x11", "vim",
            "openssl", "wtfpl", "zpl-", "intel", "eudatagrid", "fair",
        ),
    ),
]

# deps.dev 가 접미사 없이 주는 경우가 있어 기저 ID 로 정규화해 대조한다.
_SUFFIX = re.compile(r"-(only|or-later)$")
_SPLIT = re.compile(r"\s+(AND|OR)\s+", re.I)
_CLEAN = re.compile(r"[()]")


def _severity(tier: str) -> int:
    return TIER_ORDER.index(tier) if tier in TIER_ORDER else 1


def tier_for_id(spdx: str) -> str:
    """단일 SPDX 식별자의 티어."""
    if not spdx:
        return UNKNOWN
    low = _SUFFIX.sub("", spdx.strip().lower())
    for tier, prefixes in SPDX_RULES:
        for p in prefixes:
            if low == p or low.startswith(p):
                return tier
    return UNKNOWN


def atoms(expression: str) -> list[str]:
    """SPDX 표현식에서 원자 식별자만 뽑는다. WITH 예외는 앞부분만 취한다."""
    if not expression:
        return []
    expr = _CLEAN.sub(" ", expression)
    parts = [p.strip() for p in _SPLIT.split(expr) if p and p.upper() not in ("AND", "OR")]
    out = []
    for p in parts:
        out.append(re.split(r"\s+WITH\s+", p, flags=re.I)[0].strip())
    return [x for x in out if x]


def tier_for_expression(expression: str) -> tuple[str, list[str]]:
    """복합 표현식의 티어.

    AND = 모든 조건이 함께 적용 → 가장 무거운 것을 따른다.
    OR  = 하나를 고를 수 있음   → 가장 가벼운 것을 고를 수 있으나,
          MVP 에서는 안전하게 AND 와 동일하게 보수적으로 처리한다.
    numpy 처럼 permissive 만 AND 로 묶인 경우는 NOTICE 로 떨어진다.
    """
    ids = atoms(expression)
    if not ids:
        return UNKNOWN, []
    tiers = [tier_for_id(i) for i in ids]
    worst = max(tiers, key=_severity)
    return worst, ids


class Policy:
    """근거 인용문 조회. license_policy.json (한국저작권위원회 84종) 기반."""

    def __init__(self, path: Path | None = None):
        raw = json.loads((path or POLICY_PATH).read_text(encoding="utf-8"))
        self._by_spdx: dict[str, dict] = {}
        self._by_name: list[dict] = raw
        for entry in raw:
            if entry.get("spdx"):
                self._by_spdx[_SUFFIX.sub("", entry["spdx"].lower())] = entry

    def evidence(self, spdx: str) -> dict | None:
        """SPDX 식별자에 대응하는 한국어 근거 인용문."""
        if not spdx:
            return None
        key = _SUFFIX.sub("", spdx.strip().lower())
        hit = self._by_spdx.get(key)
        if hit:
            return hit
        # 접두 일치로 완화 (GPL-2.0 -> GPL-2.0-only 항목 등)
        for k, v in self._by_spdx.items():
            if k.startswith(key) or key.startswith(k):
                return v
        return None

    def cite(self, spdx: str, limit: int = 300) -> tuple[str, str]:
        """(인용문, 출처). 근거가 없으면 빈 문자열."""
        e = self.evidence(spdx)
        if not e:
            return "", ""
        text = (e.get("evidence_ko") or "").strip()
        if len(text) > limit:
            text = text[:limit].rstrip() + "…"
        return text, e.get("source", "")
