"""Gemini 연결 확인.

1) 사용 가능한 모델 ID 목록  — 모델명은 자주 바뀌므로 실제 목록으로 확인한다
2) 스캔 결과 1건을 실제로 검토시켜 구조화 출력이 나오는지 확인

사용:
  python scripts/check_gemini.py
"""

from __future__ import annotations

import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def load_env() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def main() -> int:
    load_env()
    from ipagent.llm import DEFAULT_MODEL, assess, list_models

    print(f"backend = {os.environ.get('GEMINI_BACKEND', 'aistudio')}")
    print(f"model   = {DEFAULT_MODEL}\n")

    try:
        models = list_models()
    except Exception as exc:
        print(f"[FAIL] 클라이언트 생성/조회 실패: {exc}")
        return 1

    flash = [m for m in models if "flash" in m.lower()]
    print(f"[OK] 사용 가능 모델 {len(models)}개. flash 계열:")
    for m in flash[:12]:
        print(f"     {m}")
    # 부분일치로 보면 gemini-3-flash 가 gemini-3-flash-preview 에 걸려 통과해버린다.
    # 반드시 정확히 대조할 것.
    bare = {m.split("/", 1)[-1] for m in models}
    if DEFAULT_MODEL not in bare:
        print(f"\n[주의] 설정된 '{DEFAULT_MODEL}' 는 존재하지 않는 ID다.")
        near = sorted(m for m in bare if DEFAULT_MODEL in m)
        if near:
            print(f"       비슷한 ID: {', '.join(near)}")
        print("       .env 의 GEMINI_MODEL 을 위 목록의 정확한 값으로 맞출 것.")
        return 1

    findings_path = ROOT / "data" / "findings.json"
    if not findings_path.exists():
        print("\nfindings.json 없음 - 먼저 스캔을 돌릴 것")
        return 0

    report = json.loads(findings_path.read_text(encoding="utf-8"))
    target = next((f for f in report["findings"] if f.get("evidence_ko")), None)
    if not target:
        print("\n근거가 붙은 탐지 건이 없다")
        return 0

    print(f"\n--- 검토 대상: {target['locator']} ({target['license']}) ---")
    result = assess(target, report["baseline_license"])
    print(json.dumps(result, ensure_ascii=False, indent=2))

    if result.get("_error"):
        print("\n[FAIL] 호출 실패")
        return 1
    if not result.get("grounded"):
        print("\n[주의] grounded=false — 근거가 부족하다고 모델이 판단함")
    print("\n[OK] 구조화 출력 정상")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
