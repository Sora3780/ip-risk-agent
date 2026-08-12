"""작업공간에서 라이선스 신호를 찾아낸다.

세 갈래:
  1) 의존성   requirements.txt / pyproject.toml -> 패키지 -> deps.dev -> SPDX
  2) 파일헤더 SPDX-License-Identifier / 라이선스 전문 문구
  3) 문서표기 README / LICENSE / pyproject 가 주장하는 프로젝트 라이선스
"""

from __future__ import annotations

import json
import re
import tomllib
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

import requests

DEPS_DEV = "https://api.deps.dev/v3/systems/{system}/packages"
PYPI_JSON = "https://pypi.org/pypi/{name}/json"

# 자유 서술 라이선스 문자열 -> SPDX. deps.dev 가 "non-standard" 를 줄 때 쓰는 2차 경로다.
# 위에서부터 먼저 맞는 것을 채택하므로 구체적인 것을 앞에 둔다.
_TEXT_TO_SPDX: list[tuple[str, str]] = [
    (r"affero", "AGPL-3.0"),
    (r"\blgpl\s*-?\s*3|lesser general public license v?3", "LGPL-3.0"),
    (r"\blgpl|lesser general public", "LGPL-2.1"),
    (r"gpl\s*-?\s*v?3|general public license v?\s*3", "GPL-3.0"),
    (r"gpl\s*-?\s*v?2|general public license v?\s*2", "GPL-2.0"),
    (r"\bgpl\b|general public license", "GPL-3.0"),
    (r"mozilla|(\bmpl\b)", "MPL-2.0"),
    (r"eclipse public", "EPL-1.0"),
    (r"apache", "Apache-2.0"),
    (r"bsd", "BSD-3-Clause"),
    (r"\bmit\b", "MIT"),
    (r"\bisc\b", "ISC"),
    (r"\bzlib\b", "Zlib"),
    (r"python software foundation|\bpsf\b", "PSF-2.0"),
    (r"public domain|unlicense|\bcc0\b", "CC0-1.0"),
    (r"proprietary|commercial|all rights reserved", "LicenseRef-Proprietary"),
]

# 버전은 '==' 로 고정된 경우만 취한다.
# '>=3.5' 같은 하한 표기를 고정 버전으로 오해하면 존재하지 않는 버전을 조회하게 되고,
# 리포트에도 "고정 버전 3.5" 같은 틀린 문구가 나간다.
_REQ_LINE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:==\s*([0-9][^\s;#,]*))?")
_SPDX_TAG = re.compile(r"SPDX-License-Identifier:\s*([^\s*/#]+)", re.I)

# 헤더에 전문이 박혀 있는 경우. SPDX 태그가 없을 때만 본다.
_TEXT_HINTS: list[tuple[str, str]] = [
    (r"GNU Affero General Public", "AGPL-3.0"),
    (r"GNU Lesser General Public|GNU Library General Public", "LGPL-2.1"),
    (r"GNU General Public License.*version 3|GPL.*version 3", "GPL-3.0"),
    (r"GNU General Public License", "GPL-2.0"),
    (r"Mozilla Public License", "MPL-2.0"),
    (r"Eclipse Public License", "EPL-1.0"),
    (r"Apache License", "Apache-2.0"),
    (r"Permission is hereby granted, free of charge", "MIT"),
    (r"Redistribution and use in source and binary forms", "BSD-3-Clause"),
]

SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", ".idea"}
CODE_EXT = {".py", ".js", ".ts", ".java", ".go", ".c", ".h", ".cpp", ".rs"}


@dataclass
class Signal:
    kind: str  # dependency | file_header | unknown_provenance | doc_declaration
    locator: str
    license: str | None
    detail: dict = field(default_factory=dict)


# ---------------------------------------------------------------- 1) 의존성

def parse_requirements(path: Path) -> list[tuple[str, str | None]]:
    out = []
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        m = _REQ_LINE.match(line)
        if m:
            out.append((m.group(1), m.group(2)))
    return out


def parse_pyproject(path: Path) -> tuple[list[tuple[str, str | None]], str | None]:
    data = tomllib.loads(path.read_text(encoding="utf-8", errors="ignore"))
    project = data.get("project", {})
    deps: list[str] = list(project.get("dependencies", []))
    for extra in (project.get("optional-dependencies") or {}).values():
        deps.extend(extra)
    parsed = []
    for spec in deps:
        m = _REQ_LINE.match(spec)
        if m:
            parsed.append((m.group(1), m.group(2)))
    lic = project.get("license")
    declared = lic.get("text") if isinstance(lic, dict) else lic
    return parsed, declared


def _is_unresolved(expr: str | None) -> bool:
    """deps.dev 가 SPDX 로 매핑하지 못한 응답인지."""
    if not expr:
        return True
    parts = [p.strip().lower() for p in re.split(r"\s+(?:AND|OR)\s+", expr, flags=re.I)]
    return all(p in ("", "non-standard", "unknown") for p in parts)


def normalize_license_text(raw: str) -> str:
    """자유 서술 라이선스 문자열에서 SPDX 식별자를 추정한다.

    PyMuPDF 의 'Dual Licensed - GNU AFFERO GPL 3.0 or Artifex Commercial License' 처럼
    deps.dev 가 포기하는 문자열이 많다. 여기서 놓치면 AGPL 같은 최고 위험이 그대로 샌다.
    이중 라이선스는 보수적으로 copyleft 쪽을 채택한다 —
    상용 라이선스를 실제로 구매했는지는 우리가 알 수 없다.
    """
    if not raw:
        return ""
    low = " ".join(raw.split()).lower()[:400]
    for pattern, spdx in _TEXT_TO_SPDX:
        if re.search(pattern, low):
            return spdx
    return ""


def pypi_license(name: str, timeout: int = 20) -> tuple[str, str]:
    """(SPDX 추정, 근거 원문). deps.dev 가 non-standard 일 때의 2차 소스."""
    try:
        r = requests.get(PYPI_JSON.format(name=urllib.parse.quote(name, safe="")), timeout=timeout)
        if r.status_code != 200:
            return "", ""
        info = r.json().get("info", {}) or {}
    except (requests.RequestException, ValueError):
        return "", ""

    # 1) license_expression 은 이미 SPDX 다. 가장 신뢰할 수 있다.
    expr = (info.get("license_expression") or "").strip()
    if expr:
        return expr, f"PyPI license_expression: {expr}"

    # 2) 자유 서술 license 필드
    raw = (info.get("license") or "").strip()
    guess = normalize_license_text(raw)
    if guess:
        return guess, f"PyPI license: {' '.join(raw.split())[:160]}"

    # 3) 트로브 분류자
    for c in info.get("classifiers", []) or []:
        if c.startswith("License ::"):
            guess = normalize_license_text(c.split("::")[-1])
            if guess:
                return guess, f"PyPI classifier: {c}"
    return "", ""


class DepsDevClient:
    """패키지 -> SPDX 라이선스. 응답은 캐시한다 (재실행/오프라인 시연 대비)."""

    def __init__(self, cache_path: Path):
        self.cache_path = cache_path
        self.cache: dict[str, str] = {}
        if cache_path.exists():
            self.cache = json.loads(cache_path.read_text(encoding="utf-8"))

    def _get(self, url: str) -> dict | None:
        try:
            r = requests.get(url, timeout=20)
            if r.status_code != 200:
                return None
            return r.json()
        except requests.RequestException:
            return None

    def _base(self, system: str) -> str:
        return DEPS_DEV.format(system=system)

    def _default_version(self, base: str, quoted: str) -> str | None:
        pkg = self._get(f"{base}/{quoted}")
        if not pkg:
            return None
        default = [v for v in pkg.get("versions", []) if v.get("isDefault")]
        return default[0]["versionKey"]["version"] if default else None

    def _licenses_at(self, base: str, quoted: str, version: str) -> str:
        info = self._get(f"{base}/{quoted}/versions/{urllib.parse.quote(version, safe='')}")
        licenses = (info or {}).get("licenses") or []
        return " AND ".join(licenses)

    def license_of(self, name: str, version: str | None = None,
                   system: str = "pypi") -> tuple[str | None, dict]:
        """(SPDX 표현식, 메타).

        deps.dev 는 패키지 메타데이터의 라이선스 문자열이 SPDX 로 매핑되지 않으면
        리터럴 "non-standard" 를 돌려준다. 구버전을 핀으로 고정한 경우 흔하다
        (예: paramiko 3.5.0, numpy 2.1.3). 이때는 기본(최신) 버전으로 한 번 더
        조회해 추정치를 쓰되, 추정이라는 사실을 리포트에 남긴다.
        """
        key = f"{system}:{name}@{version or 'default'}"
        if key in self.cache:
            hit = self.cache[key]
            return (hit.get("expr") or None), hit.get("meta", {})

        base = self._base(system)
        quoted = urllib.parse.quote(name, safe="")
        meta: dict = {"system": system}
        if not version:
            version = self._default_version(base, quoted)
            if not version:
                return None, meta
        meta["queried_version"] = version

        expr = self._licenses_at(base, quoted, version)
        if _is_unresolved(expr):
            fallback = self._default_version(base, quoted)
            if fallback and fallback != version:
                alt = self._licenses_at(base, quoted, fallback)
                if not _is_unresolved(alt):
                    expr = alt
                    meta["license_from_version"] = fallback
                    meta["estimated"] = True

        # deps.dev 가 끝내 못 풀면 레지스트리 원본을 본다.
        # 여기서 포기하면 PyMuPDF(AGPL-3.0) 같은 최고 위험이 REVIEW 로 묻힌다.
        if _is_unresolved(expr) and system == "pypi":
            guess, evidence = pypi_license(name)
            if guess:
                expr = guess
                meta["estimated"] = True
                meta["license_source"] = evidence

        self.cache[key] = {"expr": expr, "meta": meta}
        return (expr or None), meta

    def save(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(
            json.dumps(self.cache, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def parse_package_json(path: Path) -> tuple[list[tuple[str, str | None]], str | None]:
    """npm 의존성. dependencies + devDependencies 를 모두 본다."""
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except (json.JSONDecodeError, OSError):
        return [], None
    out: list[tuple[str, str | None]] = []
    for field_name in ("dependencies", "devDependencies", "peerDependencies"):
        for name, spec in (data.get(field_name) or {}).items():
            # "^4.19.2" / "~1.2" 는 고정이 아니다. 정확히 고정된 것만 버전으로 인정한다.
            spec = str(spec).strip()
            version = spec if re.fullmatch(r"\d[\w.+-]*", spec) else None
            out.append((name, version))
    return out, (data.get("license") or None)


MANIFEST_DEPTH = 5


def find_manifests(root: Path) -> list[tuple[Path, str]]:
    """의존성 파일을 하위 폴더까지 찾는다.

    루트만 보면 모노레포나 서비스별 하위 프로젝트가 통째로 사각지대가 된다.
    (apps/web/package.json, services/api/requirements.txt 같은 구조가 흔하다)
    """
    found: list[tuple[Path, str]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if len(rel.parts) > MANIFEST_DEPTH:
            continue
        if any(part in SKIP_DIRS or part.startswith(".") for part in rel.parts[:-1]):
            continue
        if path.name == "requirements.txt" or path.name.startswith("requirements-"):
            found.append((path, "pypi"))
        elif path.name == "pyproject.toml":
            found.append((path, "pypi"))
        elif path.name == "package.json":
            found.append((path, "npm"))
    return found


# 같은 패키지가 여러 매니페스트에 선언되면 어느 것을 위치로 표기할지 정해야 한다.
# 정렬 순서에 맡기면 파일이 하나 늘 때마다 리포트의 위치가 바뀐다.
_MANIFEST_RANK = {"requirements.txt": 0, "pyproject.toml": 1, "package.json": 2}


def _manifest_order(item: tuple[Path, str], root: Path) -> tuple:
    path = item[0]
    rel = path.relative_to(root)
    return (len(rel.parts), _MANIFEST_RANK.get(path.name, 3), rel.as_posix())


def collect_dependencies(root: Path, client: DepsDevClient) -> tuple[list[Signal], dict]:
    found = sorted(find_manifests(root), key=lambda it: _manifest_order(it, root))
    manifests = [p.relative_to(root).as_posix() for p, _ in found]

    seen: dict[str, Signal] = {}
    signals: list[Signal] = []

    for path, system in found:
        rel = path.relative_to(root).as_posix()
        if path.name == "package.json":
            deps, _ = parse_package_json(path)
        elif path.name == "pyproject.toml":
            deps, _ = parse_pyproject(path)
        else:
            deps = parse_requirements(path)

        for name, version in deps:
            key = f"{system}:{name.lower()}"
            if key in seen:
                # 중복 조회는 하지 않되, 어디에 또 선언돼 있는지는 남긴다.
                seen[key].detail.setdefault("also_declared_in", []).append(rel)
                continue
            expr, meta = client.license_of(name, version, system=system)
            sig = Signal(
                kind="dependency",
                locator=f"{rel}:{name}",
                license=expr,
                detail={"package": name, "version": version, "manifest": rel, **meta},
            )
            seen[key] = sig
            signals.append(sig)

    coverage = {
        "manifests": manifests,
        "dependencies_resolved": len(signals),
    }
    return signals, coverage


# ------------------------------------------------------------- 2) 파일 헤더

def scan_files(root: Path) -> list[Signal]:
    signals: list[Signal] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in CODE_EXT:
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        rel = path.relative_to(root).as_posix()
        head = path.read_text(encoding="utf-8", errors="ignore")[:4000]

        m = _SPDX_TAG.search(head)
        if m:
            signals.append(Signal("file_header", rel, m.group(1).strip(), {"via": "spdx-tag"}))
            continue

        matched = None
        for pattern, spdx in _TEXT_HINTS:
            if re.search(pattern, head, re.I):
                matched = spdx
                break
        if matched:
            signals.append(Signal("file_header", rel, matched, {"via": "license-text"}))
            continue

        # 라이선스 표시가 전혀 없는 경우: vendor/third_party 안에서만 문제 삼는다.
        # 그 밖의 무헤더 파일은 자체 코드로 본다 (오탐 방지).
        if any(p in ("vendor", "third_party", "thirdparty", "external") for p in path.parts):
            signals.append(
                Signal("unknown_provenance", rel, None, {"reason": "vendor 내 라이선스 표시 없음"})
            )
    return signals


# ------------------------------------------------------------- 3) 문서 표기

def declared_license(root: Path) -> list[Signal]:
    """프로젝트가 스스로 주장하는 라이선스."""
    out: list[Signal] = []

    pyp = root / "pyproject.toml"
    if pyp.exists():
        _, declared = parse_pyproject(pyp)
        if declared:
            out.append(Signal("doc_declaration", "pyproject.toml", declared.strip(), {}))

    lic = root / "LICENSE"
    if lic.exists():
        text = lic.read_text(encoding="utf-8", errors="ignore")[:4000]
        for pattern, spdx in _TEXT_HINTS:
            if re.search(pattern, text, re.I):
                out.append(Signal("doc_declaration", "LICENSE", spdx, {}))
                break

    readme = root / "README.md"
    if readme.exists():
        text = readme.read_text(encoding="utf-8", errors="ignore")
        claim = re.search(r"(MIT|Apache[- ]2\.0|BSD[- ][23][- ]Clause|GPL[^\s]*)\s*(License|라이선스)?", text, re.I)
        # "공개 의무 없음" 류의 단정도 함께 잡는다 — 이게 D-06 의 핵심
        assertion = re.search(r"(공개\s*의무[가는]?\s*없|소스[는를]?\s*공개하지\s*않)", text)
        if claim:
            out.append(
                Signal(
                    "doc_declaration",
                    "README.md",
                    claim.group(1).strip(),
                    {"asserts_no_disclosure": bool(assertion)},
                )
            )
    return out
