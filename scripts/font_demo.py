"""폰트 비교용 데모. 같은 슬라이드를 폰트만 바꿔 3벌 만든다.

고르는 기준이 되도록 성격이 다른 3장을 뽑았다.
  1) 표지        — 큰 제목에서 자소 균형이 드러난다
  2) 실측 결과    — 숫자 모양과 폭이 드러난다 (100% / 83.3% / 5 / 5)
  3) 데모 목록    — 작은 글씨 가독성과 줄 정렬이 드러난다

사용:  python scripts/font_demo.py
"""

from __future__ import annotations

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from pathlib import Path

import build_deck as bd

OUT_DIR = Path(__file__).resolve().parent.parent.parent / "폰트비교"

# (파일명에 쓸 이름, FONT_SETS 키, 화면에 표시할 설명)
VARIANTS = [
    ("Pretendard", "Pretendard", "Pretendard  ·  Regular + Bold"),
    ("SUIT", "SUIT", "SUIT  ·  Regular + ExtraBold"),
    ("에이투지체", "A2Z", "A2Z(에이투지체)  ·  4 Regular + 8 ExtraBold"),
]

SLIDES = [bd.s01_cover, bd.s11_results, bd.s09_demo_license]


def stamp(slide, label: str, on_dark: bool) -> None:
    """어떤 폰트로 그린 장인지 슬라이드 안에 남긴다. 나중에 헷갈리지 않게.

    아래쪽은 카드가 차 있어 겹친다. 오른쪽 위가 어느 장에서나 비어 있다.
    """
    color = bd.RGBColor(0x6B, 0x74, 0x80) if on_dark else bd.INK_3
    bd.text(slide, bd.W - 5.05, 0.62, 4.2, 0.3,
            [(label, 11, color, False)], align=bd.PP_ALIGN.RIGHT)


def main() -> int:
    OUT_DIR.mkdir(exist_ok=True)
    for filename, font_key, label in VARIANTS:
        bd.use_font(font_key)
        prs = bd.deck()
        for fn in SLIDES:
            fn(prs)
        for i, slide in enumerate(prs.slides):
            stamp(slide, label, on_dark=(i == 0))
        out = OUT_DIR / f"{filename}.pptx"
        prs.save(str(out))
        reg, bold, syn = bd.FONT_SETS[font_key]
        how = "bold 속성" if syn else f"'{bold}' 패밀리로 교체"
        print(f"{out.name:20s}  본문 '{reg}'  ·  굵게는 {how}")
    print(f"\n{OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
