"""GUI 공용 스타일.

tkinter 에는 둥근 모서리도 그림자도 없다. Canvas 로 직접 그려서 만든다.
여백과 타이포그래피가 인상의 대부분을 결정하므로 그쪽에 더 신경 썼다.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import font
from tkinter import font as tkfont
from tkinter import ttk

# 배경은 회색, 카드는 흰색. 선을 긋지 않고 명도 차이로 영역을 나눈다.
BG = "#F2F4F6"
CARD = "#FFFFFF"
INK = "#191F28"        # 본문
INK_2 = "#4E5968"      # 보조
INK_3 = "#8B95A1"      # 흐린 설명
LINE = "#E5E8EB"
FIELD = "#F7F8FA"
ACCENT = "#3182F6"
ACCENT_DIM = "#1B64DA"
RED = "#F04452"
ORANGE = "#FF8A00"
GREEN = "#15C47E"
ROW_NEW = "#FFF7E6"

TIER_COLOR = {"FORBIDDEN": RED, "RESTRICTED": ORANGE, "REVIEW": INK_3}
TIER_LABEL = {"FORBIDDEN": "위험", "RESTRICTED": "주의", "REVIEW": "확인"}

# 탐지 유형. 내부 코드값을 그대로 보여주면 무슨 뜻인지 알 수 없다.
KIND_LABEL = {
    "patent_similarity": "표절 위험",
    "dependency": "라이선스 충돌",
    "doc_mismatch": "표기 불일치",
    "file_header": "소스 라이선스",
    "unknown_provenance": "출처 불명",
}
HOVER = "#F7F8FA"
SELECT = "#EBF3FE"


def enable_dpi_awareness() -> None:
    """반드시 Tk() 생성 전에 부른다.

    이걸 안 하면 Windows 가 96DPI 로 그린 창을 통째로 확대해서 글자가 뭉갠다.
    고배율 모니터에서 폰트가 흐려 보이는 원인이 대부분 이것이다.
    """
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # system DPI aware
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass  # Windows 가 아니면 무시


# Pretendard 의 가장 무거운 굵기는 Black(900) 이다. ExtraBlack 이라는 굵기는 없다.
# 제목·숫자는 Black, 본문은 그 다음인 ExtraBold(800) 로 잡는다.
# 너무 무거우면 아래 두 줄만 SemiBold/Medium 으로 바꾸면 전체에 반영된다.
# 한 패밀리만 쓰고 굵기는 weight 로 준다.
# 굵기별 패밀리(Pretendard Black 등)를 섞으면 자간과 획 대비가 화면마다 달라진다.
FAMILY_CANDIDATES = ("Pretendard", "맑은 고딕")


def _first_installed(candidates: tuple[str, ...]) -> str:
    families = set(font.families())
    for name in candidates:
        if name in families:
            return name
    return "TkDefaultFont"


def pick_family() -> str:
    """유일한 글꼴 패밀리. 강조는 굵기로만 한다."""
    return _first_installed(FAMILY_CANDIDATES)


class Scale:
    """DPI 배율. 픽셀 단위 치수는 전부 이걸 통과시킨다."""

    def __init__(self, root: tk.Misc):
        dpi = root.winfo_fpixels("1i")
        self.factor = max(dpi / 96.0, 1.0)
        # 포인트 단위 폰트도 같은 배율로 커지도록 Tk 스케일링을 맞춘다.
        root.tk.call("tk", "scaling", dpi / 72.0)

    def __call__(self, n: float) -> int:
        return int(round(n * self.factor))


class Fonts:
    """Pretendard 정체와 볼드, 두 가지만 쓴다.

    Pretendard 패밀리 안에 진짜 Bold 자소가 있으므로 weight="bold" 가
    가짜 볼드로 뭉개지 않는다. r()/b() 로 (패밀리, 크기[, 굵기]) 튜플을 만든다.
    """

    def __init__(self) -> None:
        f = pick_family()
        self.family = f

        self.title = font.Font(family=f, size=17, weight="bold")
        self.metric = font.Font(family=f, size=26, weight="bold")
        self.h = font.Font(family=f, size=11, weight="bold")
        self.body = font.Font(family=f, size=10)
        self.body_b = font.Font(family=f, size=10, weight="bold")
        self.small = font.Font(family=f, size=9)
        self.tiny = font.Font(family=f, size=8)

    def r(self, size: int) -> tuple:
        """정체."""
        return (self.family, size)

    def b(self, size: int) -> tuple:
        """볼드."""
        return (self.family, size, "bold")


def round_rect(canvas: tk.Canvas, x1, y1, x2, y2, r, **kw):
    """둥근 사각형. smooth 폴리곤으로 근사한다."""
    r = min(r, abs(x2 - x1) / 2, abs(y2 - y1) / 2)
    pts = [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return canvas.create_polygon(pts, smooth=True, splinesteps=24, **kw)


class Card(tk.Frame):
    """흰 둥근 카드. 내용은 self.body 에 넣는다.

    height 를 주면 그 높이로 고정된다. 안 주면 부모가 정하는 만큼 늘어난다.
    (Canvas 는 스스로 크기를 못 정하므로 높이를 안 주면 부모를 다 먹는다)
    """

    def __init__(self, master, radius: int = 16, pad: int = 20,
                 fill: str = CARD, bg: str = BG, height: int | None = None):
        super().__init__(master, bg=bg)
        if height:
            self.configure(height=height)
            self.pack_propagate(False)
        self.radius, self.pad, self.fill = radius, pad, fill
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.body = tk.Frame(self.canvas, bg=fill)
        self._win = self.canvas.create_window(pad, pad, window=self.body, anchor="nw")
        self.canvas.bind("<Configure>", self._draw)

    def _draw(self, e) -> None:
        self.canvas.delete("bg")
        round_rect(self.canvas, 1, 1, e.width - 1, e.height - 1, self.radius,
                   fill=self.fill, outline=self.fill, tags="bg")
        self.canvas.tag_lower("bg")
        self.canvas.itemconfigure(
            self._win, width=max(e.width - self.pad * 2, 1),
            height=max(e.height - self.pad * 2, 1),
        )


class PillButton(tk.Canvas):
    """알약 버튼. ttk 버튼은 각지고 OS 테마를 타서 직접 그린다."""

    def __init__(self, master, text: str, command, width: int = 108, height: int = 40,
                 fill: str = ACCENT, hover: str = ACCENT_DIM, fg: str = "#FFFFFF",
                 bg: str = CARD, fonts: Fonts | None = None):
        super().__init__(master, width=width, height=height, bg=bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self.command, self.fill, self.hover, self.fg = command, fill, hover, fg
        self._font = (fonts or Fonts()).body_b
        # _w 는 tkinter 가 위젯 경로 이름으로 쓰는 예약 속성이다. 덮어쓰면 위젯이 깨진다.
        self.bw, self.bh = width, height
        self._text = text          # _paint 보다 먼저. 순서가 바뀌면 첫 화면에 글자가 없다.
        self._paint(fill)
        self.bind("<Button-1>", lambda _e: self.command())
        self.bind("<Enter>", lambda _e: self._paint(self.hover))
        self.bind("<Leave>", lambda _e: self._paint(self.fill))

    def _paint(self, color: str) -> None:
        self.delete("all")
        round_rect(self, 0, 0, self.bw, self.bh, self.bh / 2, fill=color, outline=color)
        self.create_text(self.bw / 2, self.bh / 2, text=self._text,
                         fill=self.fg, font=self._font)

    def set_text(self, text: str) -> None:
        self._text = text
        self._paint(self.fill)

    def set_style(self, fill: str, hover: str, fg: str) -> None:
        self.fill, self.hover, self.fg = fill, hover, fg
        self._paint(fill)


class ProgressBar(tk.Canvas):
    """진행률 막대. ttk.Progressbar 는 각지고 테마를 타서 직접 그린다."""

    def __init__(self, master, width: int = 320, height: int = 8,
                 bg: str = CARD, track: str = "#E8EBEE", fill: str = ACCENT):
        super().__init__(master, width=width, height=height, bg=bg,
                         highlightthickness=0, bd=0)
        self.bw, self.bh = width, height
        self.track, self.fill_color = track, fill
        self._percent = 0.0
        self.bind("<Configure>", self._on_resize)
        self._paint()

    def _on_resize(self, e) -> None:
        self.bw = e.width
        self._paint()

    def set(self, percent: float) -> None:
        self._percent = max(0.0, min(float(percent), 100.0))
        self._paint()

    def _paint(self) -> None:
        self.delete("all")
        r = self.bh / 2
        round_rect(self, 0, 0, self.bw, self.bh, r,
                   fill=self.track, outline=self.track)
        filled = self.bw * self._percent / 100.0
        if filled > 1:
            round_rect(self, 0, 0, max(filled, self.bh), self.bh, r,
                       fill=self.fill_color, outline=self.fill_color)


class FindingList(tk.Frame):
    """탐지 목록. ttk.Treeview 는 태그 색이 행 전체에 걸리기 때문에 직접 그린다.

    등급(위험/주의)만 색을 갖고 나머지 칸은 본문색이어야 눈이 등급으로 먼저 간다.
    행 전체가 빨간 목록은 어느 것이 급한지 오히려 안 보인다.
    """

    def __init__(self, parent, columns, fonts: Fonts, sc: Scale,
                 on_open=None, rows_visible: int = 7, **kw):
        super().__init__(parent, bg=CARD, highlightthickness=0, **kw)
        self.cols = columns           # [(key, 제목, 너비px, anchor)]
        self.F, self.px = fonts, sc      # Scale 은 그 자체가 호출 가능하다
        self.on_open = on_open
        self.rows: list[dict] = []
        self.sel: str | None = None
        self._hover: int | None = None
        self.row_h = self.px(34)

        self.head = tk.Canvas(self, bg=CARD, height=self.px(30),
                              highlightthickness=0, bd=0)
        self.head.pack(fill="x")
        body = tk.Frame(self, bg=CARD)
        body.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(body, bg=CARD, highlightthickness=0, bd=0,
                                height=self.row_h * rows_visible)
        self.sb = ttk.Scrollbar(body, orient="vertical", command=self.canvas.yview,
                                style="T.Vertical.TScrollbar")
        self.canvas.configure(yscrollcommand=self.sb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.sb.pack(side="right", fill="y")

        self.canvas.bind("<Configure>", lambda _e: self._draw())
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", lambda _e: self._set_hover(None))
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<Double-1>", self._on_double)
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self._draw_header()

    # ---------------------------------------------------------------- 좌표
    def _xs(self) -> list[int]:
        x, out = self.px(14), []
        for _k, _t, w, _a in self.cols:
            out.append(x)
            x += self.px(w)
        return out

    def _draw_header(self) -> None:
        self.head.delete("all")
        y = self.px(16)
        for (key, title, w, anchor), x in zip(self.cols, self._xs()):
            cx = x + self.px(w) // 2 if anchor == "center" else x
            self.head.create_text(cx, y, text=title, font=self.F.r(9),
                                  fill=INK_3, anchor="center" if anchor == "center" else "w")
        self.head.create_line(self.px(14), self.px(29), 4000, self.px(29), fill=LINE)

    # ---------------------------------------------------------------- 내용
    def set_rows(self, rows: list[dict]) -> None:
        """rows: [{"iid":str, "cells":[(글자, 색 또는 None, 굵게 여부)], "new":bool}]"""
        self.rows = rows
        if self.sel not in {r["iid"] for r in rows}:
            self.sel = None
        self._hover = None
        self._draw()

    def clear(self) -> None:
        self.set_rows([])

    def selection(self) -> list[str]:
        return [self.sel] if self.sel else []

    def select(self, iid: str) -> None:
        self.sel = iid
        for i, r in enumerate(self.rows):
            if r["iid"] == iid:
                self._scroll_into_view(i)
                break
        self._draw()

    def _scroll_into_view(self, i: int) -> None:
        total = max(len(self.rows) * self.row_h, 1)
        view_h = self.canvas.winfo_height() or 1
        if total <= view_h:
            return
        top = self.canvas.canvasy(0)
        y = i * self.row_h
        if y < top:
            self.canvas.yview_moveto(y / total)
        elif y + self.row_h > top + view_h:
            self.canvas.yview_moveto((y + self.row_h - view_h) / total)

    def _draw(self) -> None:
        c = self.canvas
        c.delete("all")
        width = max(c.winfo_width(), 200)
        if not self.rows:
            c.create_text(width // 2, self.px(60), text="탐지된 항목이 없습니다",
                          font=self.F.r(10), fill=INK_3)
            c.configure(scrollregion=(0, 0, width, self.px(120)))
            return

        xs = self._xs()
        for i, r in enumerate(self.rows):
            y = i * self.row_h
            if r["iid"] == self.sel:
                bg = SELECT
            elif i == self._hover:
                bg = HOVER
            elif r.get("new"):
                bg = ROW_NEW
            else:
                bg = None
            if bg:
                round_rect(c, self.px(8), y + self.px(2), width - self.px(8),
                           y + self.row_h - self.px(2), self.px(8), fill=bg, outline="")
            ty = y + self.row_h // 2
            for (text, color, bold), (_k, _t, w, anchor), x in zip(r["cells"], self.cols, xs):
                font = self.F.b(10) if bold else self.F.r(10)
                if anchor == "center":
                    c.create_text(x + self.px(w) // 2, ty, text=text, font=font,
                                  fill=color or INK, anchor="center")
                else:
                    c.create_text(x, ty, text=self._fit(text, self.px(w) - self.px(12), font),
                                  font=font, fill=color or INK, anchor="w")
        c.configure(scrollregion=(0, 0, width, len(self.rows) * self.row_h))

    def _fit(self, text: str, limit: int, font) -> str:
        """칸을 넘치면 뒤를 자른다. 넘친 글자가 옆 칸을 덮으면 표가 아니게 된다."""
        f = tkfont.Font(family=font[0], size=font[1])
        if f.measure(text) <= limit:
            return text
        while text and f.measure(text + "…") > limit:
            text = text[:-1]
        return text + "…"

    # ---------------------------------------------------------------- 입력
    def _row_at(self, ev) -> int | None:
        i = int(self.canvas.canvasy(ev.y) // self.row_h)
        return i if 0 <= i < len(self.rows) else None

    def _set_hover(self, i) -> None:
        if i != self._hover:
            self._hover = i
            self.canvas.configure(cursor="hand2" if i is not None else "")
            self._draw()

    def _on_motion(self, ev) -> None:
        self._set_hover(self._row_at(ev))

    def _on_click(self, ev) -> None:
        i = self._row_at(ev)
        self.sel = self.rows[i]["iid"] if i is not None else None
        self.canvas.focus_set()
        self._draw()

    def _on_double(self, ev) -> None:
        if self._row_at(ev) is not None and self.on_open:
            self.on_open(ev)

    def _on_wheel(self, ev) -> None:
        if len(self.rows) * self.row_h > self.canvas.winfo_height():
            self.canvas.yview_scroll(-1 if ev.delta > 0 else 1, "units")
