"""데스크톱 감시 앱.

웹 화면이 못 하는 일을 맡는다 — 내 PC 의 폴더를 계속 지켜보다가,
파일을 저장하는 순간 위험을 알린다.

tkinter 를 쓴다. 표준 라이브러리라 배포가 단순하고 라이선스가 깨끗하다.
PyQt5 는 GPL-3.0 이라 이 도구가 자기 자신을 FORBIDDEN 으로 판정하게 되고,
PySide6 는 LGPL 이라 배포 시 의무사항을 떠안는다.

실행:
  python -m ipagent.gui
"""

from __future__ import annotations

import json
import queue
import time
import threading
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from watchdog.observers import Observer

from .theme import (ACCENT, ACCENT_DIM, BG, CARD, FIELD, GREEN, INK, INK_2, INK_3,
                    RED, ROW_NEW, TIER_COLOR, TIER_LABEL, KIND_LABEL, Card,
                    FindingList, Fonts, PillButton,
                    ProgressBar, Scale, enable_dpi_awareness)
from .watcher import DEBOUNCE_SEC, MAX_FILE_BYTES, Debouncer, initial_sync

API_DEFAULT = "http://127.0.0.1:8000"
DRIVE_POLL_SEC = 20   # Drive 는 파일시스템 이벤트가 없어 폴링한다

# Tk() 가 만들어지기 전에 걸어야 효과가 있다. 모듈을 불러오는 순간 적용한다.
enable_dpi_awareness()


# 근거 원문에서 실제로 위험을 만드는 구절들. 여기 걸리면 등급 색으로 칠한다.
# 문서 전체를 회색으로 두면 "무엇 때문에 위험한지" 를 눈으로 못 찾는다.
RISK_PATTERNS = [
    r"소스\s?코드[를을]?\s?(제공|공개)",
    r"소스코드[를을]?\s?(제공|공개)",
    r"파생\s?저작물",
    r"동일한?\s?라이선스",
    r"공개(해야|하여야|할\s?의무|\s?의무)",
    r"제공(해야|하여야|할\s?의무)",
    r"약정서",
    r"설치\s?정보",
    r"인증키",
    r"금지",
    r"의무사항",
    r"양립\s?불가",
    r"\[겹치는 부분\]",
    r"GPL[-\s]?[23]?(\.0)?",
    r"AGPL",
    r"LGPL",
    r"HIGH",
]
# 상쇄 요인. 위험이 아니라 '빠져나갈 구멍' 이므로 다른 색으로 둔다.
RELIEF_PATTERNS = [
    r"\[다른 부분\]",
    r"소스코드\s?제공없이",
    r"소스\s?코드\s?제공\s?없이",
    r"면제",
    r"배포\s?가능",
    r"허용",
]


def i_name(item: dict) -> str:
    """최근 목록 버튼에 쓸 짧은 라벨."""
    return f"{item['name'][:28]}  ·  {item.get('targets', 0)}개"


class Bridge:
    """감시 스레드 -> UI 스레드. tkinter 위젯은 메인 스레드에서만 만질 수 있다."""

    def __init__(self) -> None:
        self.q: queue.Queue = queue.Queue()

    def send(self, kind: str, payload) -> None:
        self.q.put((kind, payload))


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.px = Scale(self)          # DPI 배율. 픽셀 치수는 전부 통과시킨다.
        self.title("IP DeteDog")
        # 고배율 화면에서 px() 결과가 실제 해상도를 넘을 수 있다. 화면 안으로 가둔다.
        w = min(self.px(1080), int(self.winfo_screenwidth() * 0.9))
        h = min(self.px(760), int(self.winfo_screenheight() * 0.88))
        self.geometry(f"{w}x{h}")
        self.minsize(min(self.px(940), w), min(self.px(640), h))
        self.configure(bg=BG)

        self.api = API_DEFAULT
        self.bridge = Bridge()
        self.observer: Observer | None = None
        self.handler: Debouncer | None = None
        self.root_path: Path | None = None
        self.previous: set[str] = set()
        self.findings: dict[str, dict] = {}
        self.scans = 0

        self.F = Fonts()
        self._style()
        self._build()
        self.after(200, self._drain)
        self.after(300, self._check_server)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _style(self) -> None:
        s = ttk.Style()
        s.theme_use("clam")
        s.configure("T.Treeview", background=CARD, fieldbackground=CARD,
                    foreground=INK, rowheight=self.px(40), borderwidth=0,
                    font=self.F.r(10))
        s.configure("T.Treeview.Heading", background=CARD, foreground=INK_3,
                    font=self.F.r(9), borderwidth=0, relief="flat",
                    padding=(self.px(10), self.px(6)))
        s.map("T.Treeview.Heading", background=[("active", CARD)])
        s.map("T.Treeview", background=[("selected", "#EAF2FE")],
              foreground=[("selected", INK)])
        s.layout("T.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])  # 테두리 제거
        s.configure("T.Vertical.TScrollbar", background=BG, troughcolor=CARD,
                    borderwidth=0, arrowsize=12)
        s.configure("T.TEntry", fieldbackground="#F7F8FA", borderwidth=0,
                    relief="flat", padding=self.px(9))
        s.configure("T.TCheckbutton", background=CARD, foreground=INK_2,
                    font=self.F.r(10))
        s.map("T.TCheckbutton", background=[("active", CARD)])
        s.configure("T.TRadiobutton", background=CARD, foreground=INK_2,
                    font=self.F.r(10))
        s.map("T.TRadiobutton", background=[("active", CARD)])

    # ------------------------------------------------------------- 화면

    def _build(self) -> None:
        # ── 헤더
        head = tk.Frame(self, bg=BG)
        head.pack(fill="x", padx=self.px(24), pady=(self.px(18), self.px(14)))
        left = tk.Frame(head, bg=BG)
        left.pack(side="left")
        tk.Label(left, text="IP DeteDog", font=self.F.title, bg=BG, fg=INK).pack(anchor="w")
        tk.Label(left, text="작업공간을 지켜보다 라이선스 위험이 생기면 바로 알려드려요",
                 font=self.F.small, bg=BG, fg=INK_3).pack(anchor="w", pady=(3, 0))

        self.scanned_var = tk.StringVar(value="")
        tk.Label(head, textvariable=self.scanned_var, font=self.F.small,
                 bg=BG, fg=INK_3).pack(side="right", padx=(0, self.px(16)))

        PillButton(head, "?", self._show_help, width=self.px(30), height=self.px(30),
                   fill=BG, hover="#E5E8EB", fg=INK_2, bg=BG, fonts=self.F).pack(
            side="right", padx=(self.px(6), 0))
        PillButton(head, "⚙", self._show_settings, width=self.px(30), height=self.px(30),
                   fill=BG, hover="#E5E8EB", fg=INK_2, bg=BG, fonts=self.F).pack(
            side="right", padx=(self.px(12), 0))

        self.server_var = tk.StringVar(value="서버 확인 중")
        rightbox = tk.Frame(head, bg=BG)
        rightbox.pack(side="right")
        self.server_dot = tk.Label(rightbox, text="●", bg=BG, fg=INK_3, font=self.F.tiny)
        self.server_dot.pack(side="left", padx=(0, 5))
        tk.Label(rightbox, textvariable=self.server_var, font=self.F.small,
                 bg=BG, fg=INK_3).pack(side="left")

        # ── 감시 설정 카드
        bar = Card(self, pad=self.px(18), height=self.px(132))
        bar.pack(fill="x", padx=self.px(24), pady=(0, self.px(12)))
        b = bar.body
        head_row = tk.Frame(b, bg=CARD)
        head_row.pack(fill="x")
        self.src_label = tk.StringVar(value="감시 폴더")
        tk.Label(head_row, textvariable=self.src_label, font=self.F.small,
                 bg=CARD, fg=INK_3).pack(side="left")
        self.source_var = tk.StringVar(value="local")
        for text, val in (("로컬 폴더", "local"), ("Google Drive", "drive")):
            ttk.Radiobutton(head_row, text=text, value=val, variable=self.source_var,
                            command=self._on_source, style="T.TRadiobutton").pack(
                side="right", padx=(self.px(12), 0))

        row = tk.Frame(b, bg=CARD)
        row.pack(fill="x", pady=(self.px(7), self.px(10)))
        self.path_var = tk.StringVar()
        ttk.Entry(row, textvariable=self.path_var, style="T.TEntry",
                  font=self.F.r(10)).pack(side="left", fill="x", expand=True)
        self.browse_btn = PillButton(row, "찾아보기", self._browse, width=self.px(92),
                                     height=self.px(38), fill=BG, hover="#E5E8EB",
                                     fg=INK_2, fonts=self.F)
        self.browse_btn.pack(side="left", padx=(self.px(9), 0))
        self.toggle_btn = PillButton(row, "감시 시작", self._toggle,
                                     width=self.px(104), height=self.px(38), fonts=self.F)
        self.toggle_btn.pack(side="left", padx=(self.px(7), 0))

        # 심층 검토·특허 검토는 기본으로 켠다. 끄려면 톱니바퀴(환경설정)에서.
        self.llm_var = tk.BooleanVar(value=True)
        self.patent_var = tk.BooleanVar(value=True)
        self.poll_var = tk.IntVar(value=DRIVE_POLL_SEC)

        prog = tk.Frame(b, bg=CARD)
        prog.pack(fill="x")
        self.progress = ProgressBar(prog, width=self.px(600), height=self.px(7))
        self.progress.pack(fill="x", side="top")
        self.progress_var = tk.StringVar(value="")
        tk.Label(prog, textvariable=self.progress_var, font=self.F.small,
                 bg=CARD, fg=INK_3, anchor="w").pack(fill="x", pady=(self.px(4), 0))

        # ── 요약
        self.metrics: dict[str, tk.Label] = {}
        mrow = tk.Frame(self, bg=BG)
        mrow.pack(fill="x", padx=self.px(24), pady=(0, self.px(12)))
        for i, (key, label, color) in enumerate((
            ("total", "전체", INK), ("FORBIDDEN", "위험", RED),
            ("RESTRICTED", "주의", TIER_COLOR["RESTRICTED"]), ("REVIEW", "확인", INK_3),
        )):
            card = Card(mrow, pad=self.px(16), height=self.px(96))
            card.pack(side="left", fill="both", expand=True,
                      padx=(0, self.px(10) if i < 3 else 0))
            value = tk.Label(card.body, text="—", font=self.F.metric, bg=CARD, fg=color)
            value.pack(anchor="w")
            tk.Label(card.body, text=label, font=self.F.small, bg=CARD,
                     fg=INK_3).pack(anchor="w", pady=(2, 0))
            self.metrics[key] = value

        # ── 목록 + 상세
        panes = ttk.PanedWindow(self, orient="vertical")
        panes.pack(fill="both", expand=True, padx=self.px(24), pady=(0, self.px(4)))

        lcard = Card(panes, pad=self.px(16))
        tk.Label(lcard.body, text="탐지된 항목", font=self.F.h, bg=CARD,
                 fg=INK).pack(anchor="w", pady=(0, self.px(8)))
        holder = tk.Frame(lcard.body, bg=CARD)
        holder.pack(fill="both", expand=True)
        # 등급 칸만 색을 갖는다. 행 전체를 물들이면 어느 것이 급한지 안 보인다.
        self.tree = FindingList(
            holder,
            columns=(("tier", "등급", 72, "center"), ("locator", "위치", 380, "w"),
                     ("license", "라이선스", 190, "w"), ("kind", "유형", 160, "w")),
            fonts=self.F, sc=self.px, on_open=self._on_open, rows_visible=7)
        self.tree.pack(fill="both", expand=True)
        panes.add(lcard, weight=3)

        # 변경 감지 기록. 탐지 결과가 그대로여도 "검사가 돌았다"가 보여야 한다.
        acard = Card(panes, pad=self.px(16))
        ahead = tk.Frame(acard.body, bg=CARD)
        ahead.pack(fill="x", pady=(0, self.px(8)))
        tk.Label(ahead, text="변경 감지", font=self.F.h, bg=CARD, fg=INK).pack(side="left")
        self.live_dot = tk.Label(ahead, text="●", font=self.F.tiny, bg=CARD, fg=INK_3)
        self.live_dot.pack(side="right")
        aholder = tk.Frame(acard.body, bg=CARD)
        aholder.pack(fill="both", expand=True)
        self.activity = tk.Text(aholder, wrap="word", font=self.F.r(9),
                                bg=CARD, fg=INK_2, relief="flat", padx=2, pady=2,
                                spacing1=1, spacing3=3, highlightthickness=0,
                                borderwidth=0, cursor="arrow")
        asb = ttk.Scrollbar(aholder, orient="vertical", command=self.activity.yview,
                            style="T.Vertical.TScrollbar")
        self.activity.configure(yscrollcommand=asb.set, state="disabled")
        self.activity.pack(side="left", fill="both", expand=True)
        asb.pack(side="right", fill="y")
        self.activity.tag_configure("time", foreground=INK_3, font=self.F.r(8))
        self.activity.tag_configure("file", foreground=INK, font=self.F.b(9))
        self.activity.tag_configure("same", foreground=INK_3)
        self.activity.tag_configure("up", foreground=RED, font=self.F.b(9))
        self.activity.tag_configure("down", foreground=GREEN)
        self.activity.tag_configure("wait", foreground=ACCENT)
        self.activity.tag_configure("head", foreground=INK, font=self.F.b(9))
        panes.add(acard, weight=2)

        self.status_var = tk.StringVar(value="")
        tk.Label(self, textvariable=self.status_var, bg=BG, fg=INK_3,
                 font=self.F.small, anchor="w").pack(fill="x", padx=self.px(26), pady=(0, self.px(10)))

        self._placeholder()

    def _placeholder(self) -> None:
        self._log([("폴더를 고르고 [감시 시작] 을 누르면 파일 변경이 여기에 기록됩니다\n",
                    "same"),
                   ("도움말은 오른쪽 위 ? 버튼\n", "same")])

    HELP = [
        ("이렇게 시작해요\n", "head"),
        ("1.  [찾아보기] 로 프로젝트 폴더를 고릅니다\n", None),
        ("2.  [감시 시작] 을 누르면 폴더 전체를 한 번 검사합니다\n", None),
        ("3.  이후 파일을 저장할 때마다 3초 뒤 자동으로 다시 검사합니다\n\n", None),
        ("표에서 보는 것\n", "head"),
        ("새로 생긴 위험은 노란 배경으로 표시되고 자동으로 선택됩니다.\n", None),
        ("항목을 더블클릭하면 근거와 권장 조치를 볼 수 있습니다.\n\n", None),
        ("등급\n", "head"),
        ("위험    비공개 상용 배포와 양립하기 어렵습니다 (GPL, AGPL 등)\n", "up"),
        ("주의    조건을 지키면 쓸 수 있습니다 (LGPL, MPL 등)\n", "wait"),
        ("확인    자동 판정이 어려워 사람이 봐야 합니다\n\n", "same"),
        ("Gemini 심층 검토\n", "head"),
        ("켜면 각 항목의 근거 조항을 찾아 설명과 조치를 만들어 줍니다.\n"
         "건당 6초쯤 걸리므로 평소에는 꺼두고 필요할 때만 켜세요.\n\n", None),
        ("판정 범위\n", "head"),
        ("법적 결론이 아닙니다. 전문가 검토가 필요한 지점을 짚어주는 도구입니다.\n", "same"),
    ]

    def _show_settings(self) -> None:
        """환경설정. 기본은 모두 켜져 있고 여기서만 끈다."""
        win = tk.Toplevel(self)
        win.title("환경설정")
        win.configure(bg=BG)
        win.transient(self)
        w, h = self.px(480), self.px(360)
        win.geometry(f"{w}x{h}+{self.winfo_rootx() + self.px(200)}+{self.winfo_rooty() + self.px(100)}")

        card = Card(win, pad=self.px(20))
        card.pack(fill="both", expand=True, padx=self.px(14), pady=self.px(14))
        b = card.body

        tk.Label(b, text="검사 옵션", font=self.F.h, bg=CARD, fg=INK).pack(anchor="w")
        ttk.Checkbutton(b, text="Gemini 심층 검토", variable=self.llm_var,
                        style="T.TCheckbutton").pack(anchor="w", pady=(self.px(8), 0))
        tk.Label(b, text="근거 조항을 찾아 설명과 권장 조치를 만듭니다. 건당 약 6초.",
                 font=self.F.small, bg=CARD, fg=INK_3).pack(anchor="w", padx=self.px(22))

        ttk.Checkbutton(b, text="기획서 특허 자동 검토", variable=self.patent_var,
                        style="T.TCheckbutton").pack(anchor="w", pady=(self.px(10), 0))
        tk.Label(b, text="기획서가 바뀌면 KIPRIS 에서 선행 특허를 찾습니다.\n"
                         "내용이 실제로 달라졌을 때만 돌고, 같은 문서는 10분간 재검토하지 않습니다.",
                 font=self.F.small, bg=CARD, fg=INK_3, justify="left").pack(
            anchor="w", padx=self.px(22))

        tk.Label(b, text="Drive 확인 주기", font=self.F.h, bg=CARD,
                 fg=INK).pack(anchor="w", pady=(self.px(18), self.px(2)))
        tk.Label(b, text="로컬 폴더는 파일 저장을 즉시 감지합니다(3초).\n"
                         "Drive 는 파일 이벤트가 없어 주기적으로 목록을 확인합니다.",
                 font=self.F.small, bg=CARD, fg=INK_3, justify="left").pack(
            anchor="w", pady=(0, self.px(6)))
        prow = tk.Frame(b, bg=CARD)
        prow.pack(anchor="w")
        for sec in (5, 10, 20, 60):
            ttk.Radiobutton(prow, text=f"{sec}초", value=sec, variable=self.poll_var,
                            style="T.TRadiobutton").pack(side="left", padx=(0, self.px(12)))

        self._quota_var = tk.StringVar(value="사용량 확인 중…")
        tk.Label(b, textvariable=self._quota_var, font=self.F.small, bg=CARD,
                 fg=INK_3, justify="left").pack(anchor="w", pady=(self.px(18), 0))
        self._load_quota()

        PillButton(win, "닫기", win.destroy, width=self.px(88), height=self.px(36),
                   fill=BG, hover="#E5E8EB", fg=INK_2, bg=BG,
                   fonts=self.F).pack(pady=(0, self.px(12)))
        win.bind("<Escape>", lambda _e: win.destroy())

    def _load_quota(self) -> None:
        def work():
            try:
                with urllib.request.urlopen(f"{self.api}/health", timeout=10) as r:
                    q = json.load(r).get("quota", {})
                k, g = q.get("kipris", {}), q.get("gemini", {})
                self.bridge.send("quota", (
                    f"KIPRIS  {k.get('used', 0)} / {k.get('limit', 0)}  ({k.get('period', '')})\n"
                    f"Gemini  {g.get('used', 0)} / {g.get('limit', 0)}  ({g.get('period', '')})"))
            except Exception:
                self.bridge.send("quota", "사용량을 읽지 못했습니다")

        threading.Thread(target=work, daemon=True).start()

    def _show_help(self) -> None:
        self._popup("도움말", self.HELP, width=560, height=460)

    @staticmethod
    def _highlight(widget: tk.Text, patterns: list[str], tag: str) -> None:
        """정규식에 걸리는 구간마다 태그를 입힌다."""
        for pattern in patterns:
            start = "1.0"
            while True:
                idx = widget.search(pattern, start, stopindex="end", regexp=True, nocase=True)
                if not idx:
                    break
                end = f"{idx}+{len(widget.get(idx, f'{idx} lineend'))}c"
                # 매치 길이를 정확히 알기 위해 count 변수를 쓴다
                cnt = tk.IntVar()
                idx2 = widget.search(pattern, start, stopindex="end",
                                     regexp=True, nocase=True, count=cnt)
                if not idx2:
                    break
                end = f"{idx2}+{cnt.get()}c"
                widget.tag_add(tag, idx2, end)
                start = end

    def _popup(self, title: str, blocks, width: int = 620, height: int = 520,
               tier: str | None = None) -> None:
        """작은 창을 띄운다. 상시 패널을 두는 대신 필요할 때만 연다."""
        win = tk.Toplevel(self)
        win.title(title)
        win.configure(bg=BG)
        win.transient(self)
        w, h = self.px(width), self.px(height)
        x = self.winfo_rootx() + (self.winfo_width() - w) // 2
        y = self.winfo_rooty() + (self.winfo_height() - h) // 3
        win.geometry(f"{w}x{h}+{max(x, 0)}+{max(y, 0)}")

        card = Card(win, pad=self.px(20))
        card.pack(fill="both", expand=True, padx=self.px(16), pady=self.px(16))
        holder = tk.Frame(card.body, bg=CARD)
        holder.pack(fill="both", expand=True)
        text = tk.Text(holder, wrap="word", font=self.F.r(10), bg=CARD, fg=INK_2,
                       relief="flat", padx=2, pady=2, spacing1=2, spacing3=5,
                       highlightthickness=0, borderwidth=0, cursor="arrow")
        bar = ttk.Scrollbar(holder, orient="vertical", command=text.yview,
                            style="T.Vertical.TScrollbar")
        text.configure(yscrollcommand=bar.set)
        text.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")

        text.tag_configure("head", font=self.F.b(11), foreground=INK,
                           spacing1=10, spacing3=4)
        text.tag_configure("same", foreground=INK_3)
        text.tag_configure("up", foreground=RED)
        text.tag_configure("wait", foreground=TIER_COLOR["RESTRICTED"])
        text.tag_configure("quote", foreground=INK_2, lmargin1=14, lmargin2=14,
                           font=self.F.small)
        # 루프 변수를 tier 로 두면 함수 인자 tier 를 덮어써서 강조 색이 엉뚱해진다.
        for key, color in TIER_COLOR.items():
            text.tag_configure(key, foreground=color, font=self.F.b(13), spacing3=6)
        risk_color = TIER_COLOR.get(tier or "", RED)
        text.tag_configure("risk", foreground=risk_color, font=self.F.b(10))
        text.tag_configure("relief", foreground=GREEN)

        for chunk, tag in blocks:
            text.insert("end", chunk, tag or ())

        # 근거 원문 안에서 위험을 만드는 구절만 등급 색으로 칠한다.
        if tier:
            self._highlight(text, RISK_PATTERNS, "risk")
            self._highlight(text, RELIEF_PATTERNS, "relief")
        text.configure(state="disabled")

        PillButton(win, "닫기", win.destroy, width=self.px(88), height=self.px(36),
                   fill=BG, hover="#E5E8EB", fg=INK_2, bg=BG, fonts=self.F).pack(
            pady=(0, self.px(14)))
        win.bind("<Escape>", lambda _e: win.destroy())

    def _log(self, blocks, stamp: bool = False) -> None:
        """왼쪽 활동 로그에 한 줄 붙인다.

        탐지 결과가 그대로여도 '검사가 돌았다'는 사실이 보여야 한다.
        목록만 보고 있으면 변경이 감지됐는지 알 수 없다.
        """
        self.activity.configure(state="normal")
        if stamp:
            self.activity.insert("end", time.strftime("%H:%M:%S  "), "time")
        for text, tag in blocks:
            self.activity.insert("end", text, tag or ())
        # 무한정 쌓이면 메모리와 스크롤이 부담된다.
        if int(self.activity.index("end-1c").split(".")[0]) > 400:
            self.activity.delete("1.0", "120.0")
        self.activity.configure(state="disabled")
        self.activity.see("end")

    # ------------------------------------------------------------- 동작

    def _on_source(self) -> None:
        drive = self.source_var.get() == "drive"
        self.src_label.set("Drive 폴더 ID 또는 공유 링크" if drive else "감시 폴더")
        # Drive 는 탐색기로 고를 수 없다. 링크를 붙여넣어야 한다.
        if drive:
            self._log([("Drive 폴더를 서비스 계정에 '뷰어' 로 공유해야 읽을 수 있습니다\n",
                        "same")])

    def _browse(self) -> None:
        if self.source_var.get() == "drive":
            self._drive_picker()
            return
        chosen = filedialog.askdirectory(title="감시할 프로젝트 폴더 선택")
        if chosen:
            self.path_var.set(chosen)

    RECENT_PATH = "data/drive_recent.json"

    def _recent_drive(self) -> list[dict]:
        p = Path(self.RECENT_PATH)
        if not p.exists():
            return []
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []

    def _remember_drive(self, info: dict) -> None:
        items = [x for x in self._recent_drive() if x["id"] != info["id"]]
        items.insert(0, {"id": info["id"], "name": info["name"],
                         "targets": info.get("targets", 0)})
        p = Path(self.RECENT_PATH)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(items[:8], ensure_ascii=False, indent=2), encoding="utf-8")

    def _drive_picker(self) -> None:
        """Drive 폴더 고르기.

        서비스 계정은 공유받은 폴더를 목록으로 열거할 수 없다 (Drive 사양).
        그래서 링크를 붙여넣게 하되, 확인 버튼으로 폴더 이름을 보여주고
        한 번 쓴 폴더는 기억해 다음부터 클릭으로 고를 수 있게 한다.
        """
        win = tk.Toplevel(self)
        win.title("Google Drive 폴더")
        win.configure(bg=BG)
        win.transient(self)
        w, h = self.px(560), self.px(420)
        win.geometry(f"{w}x{h}+{self.winfo_rootx() + self.px(120)}+{self.winfo_rooty() + self.px(90)}")

        card = Card(win, pad=self.px(20))
        card.pack(fill="both", expand=True, padx=self.px(14), pady=self.px(14))
        body = card.body

        tk.Label(body, text="공유 링크 붙여넣기", font=self.F.h, bg=CARD, fg=INK).pack(anchor="w")
        tk.Label(body, text="Drive 에서 폴더 우클릭 → 공유 → 링크 복사",
                 font=self.F.small, bg=CARD, fg=INK_3).pack(anchor="w", pady=(2, self.px(8)))

        var = tk.StringVar(value=self.path_var.get())
        entry_row = tk.Frame(body, bg=CARD)
        entry_row.pack(fill="x")
        ttk.Entry(entry_row, textvariable=var, style="T.TEntry",
                  font=self.F.r(10)).pack(side="left", fill="x", expand=True)

        status = tk.StringVar(value="")
        status_lbl = tk.Label(body, textvariable=status, font=self.F.small,
                              bg=CARD, fg=INK_3, wraplength=self.px(480), justify="left")
        status_lbl.pack(anchor="w", pady=(self.px(10), 0))

        def verify(then_close: bool = False) -> None:
            folder = var.get().strip()
            if not folder:
                return
            status.set("확인 중…")
            status_lbl.configure(fg=INK_3)
            win.update_idletasks()
            try:
                url = f"{self.api}/api/drive-folder?folder={urllib.parse.quote(folder, safe='')}"
                with urllib.request.urlopen(url, timeout=60) as r:
                    info = json.load(r)
            except urllib.error.HTTPError as exc:
                detail = json.loads(exc.read() or b"{}").get("detail", f"HTTP {exc.code}")
                status.set(str(detail))
                status_lbl.configure(fg=RED)
                return
            except Exception as exc:
                status.set(f"{type(exc).__name__} {exc}")
                status_lbl.configure(fg=RED)
                return
            status.set(f"{info['name']}  ·  검사 대상 {info['targets']}개"
                       f" (Google 문서 {info['google_docs']}개)\n"
                       + "  ".join(info.get("sample", [])[:4]))
            status_lbl.configure(fg=INK_2)
            self._remember_drive(info)
            if then_close:
                self.path_var.set(folder)
                win.destroy()

        PillButton(entry_row, "확인", lambda: verify(False), width=self.px(72),
                   height=self.px(38), fill=BG, hover="#E5E8EB", fg=INK_2,
                   fonts=self.F).pack(side="left", padx=(self.px(8), 0))

        recent = self._recent_drive()
        if recent:
            tk.Label(body, text="최근 사용", font=self.F.h, bg=CARD,
                     fg=INK).pack(anchor="w", pady=(self.px(16), self.px(6)))
            for item in recent:
                def pick(i=item):
                    var.set(i["id"])
                    verify(True)
                PillButton(body, f"{i_name(item)}", pick, width=self.px(460),
                           height=self.px(34), fill="#F7F8FA", hover="#EEF1F4",
                           fg=INK_2, fonts=self.F).pack(anchor="w", pady=2)

        btns = tk.Frame(win, bg=BG)
        btns.pack(pady=(0, self.px(12)))
        PillButton(btns, "이 폴더 사용", lambda: verify(True), width=self.px(118),
                   height=self.px(38), bg=BG, fonts=self.F).pack(side="left", padx=self.px(4))
        PillButton(btns, "닫기", win.destroy, width=self.px(80), height=self.px(38),
                   fill=BG, hover="#E5E8EB", fg=INK_2, bg=BG,
                   fonts=self.F).pack(side="left", padx=self.px(4))
        win.bind("<Escape>", lambda _e: win.destroy())
        win.bind("<Return>", lambda _e: verify(True))

    def _check_server(self) -> None:
        def work():
            try:
                with urllib.request.urlopen(f"{self.api}/health", timeout=8) as r:
                    h = json.load(r)
                self.bridge.send("server", (
                    True, f"라이선스 {h['policy_licenses']}종 · RAG {'ON' if h['rag_index'] else 'OFF'}"))
            except Exception:
                self.bridge.send("server", (False, "서버 연결 안 됨"))

        threading.Thread(target=work, daemon=True).start()

    def _watch_progress(self, workspace: str) -> None:
        """검사가 도는 동안 서버의 진행 상황을 짧은 주기로 읽어 온다."""
        stop = getattr(self, "_prog_stop", None)
        if stop:
            stop.set()
        self._prog_stop = threading.Event()
        ev = self._prog_stop

        def work():
            while not ev.is_set():
                try:
                    with urllib.request.urlopen(
                            f"{self.api}/api/progress/{workspace}", timeout=10) as r:
                        self.bridge.send("progress", json.load(r))
                except Exception:
                    pass
                ev.wait(0.5)

        threading.Thread(target=work, daemon=True).start()

    def _toggle(self) -> None:
        watching = self.observer or getattr(self, '_drive_stop', None)
        self._stop() if watching else self._start()

    def _start(self) -> None:
        if self.source_var.get() == "drive":
            self._start_drive()
            return
        raw = self.path_var.get().strip()
        if not raw or not Path(raw).is_dir():
            messagebox.showwarning("폴더 확인", "감시할 폴더를 먼저 골라주세요.")
            return
        self.root_path = Path(raw).resolve()
        self.previous.clear()
        self.findings.clear()
        self.scans = 0
        self.tree.clear()

        self.handler = Debouncer(self.root_path, self._flush)
        self.observer = Observer()
        self.observer.schedule(self.handler, str(self.root_path), recursive=True)
        self.observer.start()

        self.toggle_btn.set_style(BG, "#E5E8EB", INK_2)
        self.toggle_btn.set_text("감시 중지")
        self._watch_progress("gui")
        self.activity.configure(state="normal")
        self.activity.delete("1.0", "end")
        self.activity.configure(state="disabled")
        self._log([(f"감시 시작  {self.root_path}\n", "file")], stamp=True)
        self.status_var.set(f"감시 중 · {self.root_path}")
        threading.Thread(target=lambda: self._flush(initial_sync(self.root_path)),
                         daemon=True).start()

    def _start_drive(self) -> None:
        """Drive 는 파일시스템 이벤트가 없으므로 주기적으로 목록을 확인한다.

        서버가 목록만 먼저 훑어 변경 여부를 판단하므로, 바뀐 게 없으면
        본문을 내려받지 않고 저장된 결과를 그대로 돌려준다. 폴링이 싸다.
        """
        folder = self.path_var.get().strip()
        if not folder:
            messagebox.showwarning("폴더 확인", "Drive 폴더 ID 또는 공유 링크를 넣어주세요.")
            return
        self.root_path = None
        self.previous.clear()
        self.findings.clear()
        self.scans = 0
        self.tree.clear()
        self.activity.configure(state="normal")
        self.activity.delete("1.0", "end")
        self.activity.configure(state="disabled")

        self._drive_stop = threading.Event()
        self._watch_progress("gui-drive")
        self.toggle_btn.set_style(BG, "#E5E8EB", INK_2)
        self.toggle_btn.set_text("감시 중지")
        self._log([(f"Drive 감시 시작  {folder[:60]}\n", "file"),
                   (f"    {DRIVE_POLL_SEC}초마다 변경을 확인합니다\n", "same")], stamp=True)

        def loop():
            first = True
            while not self._drive_stop.is_set():
                self.bridge.send("status", "Drive 확인 중…")
                self.bridge.send("polling", first)
                started = time.monotonic()
                first = False
                req = urllib.request.Request(
                    f"{self.api}/api/scan-drive",
                    data=json.dumps({"workspace_id": "gui-drive", "folder": folder,
                                     "llm": self.llm_var.get(), "doc_check": True,
                                     "patent": self.patent_var.get()}).encode("utf-8"),
                    headers={"Content-Type": "application/json"})
                try:
                    with urllib.request.urlopen(req, timeout=1800) as resp:
                        payload = json.load(resp)
                    payload.setdefault("stats", {})["poll_sec"] = round(time.monotonic() - started, 1)
                    self.bridge.send("result", payload)
                except urllib.error.HTTPError as exc:
                    body = exc.read().decode("utf-8", "ignore")[:200]
                    self.bridge.send("drive_error", f"HTTP {exc.code} · {body}")
                    self._drive_stop.set()
                    break
                except Exception as exc:
                    self.bridge.send("drive_error", f"{type(exc).__name__} {exc}")
                # 요청에 걸린 시간을 빼야 설정한 주기가 실제 주기가 된다.
                # 안 그러면 (요청 7초 + 대기 20초) = 27초 주기가 된다.
                spent = time.monotonic() - started
                self._drive_stop.wait(max(self.poll_var.get() - spent, 1.0))

        self._drive_thread = threading.Thread(target=loop, daemon=True)
        self._drive_thread.start()

    def _stop(self) -> None:
        if getattr(self, "_prog_stop", None):
            self._prog_stop.set()
            self._prog_stop = None
        self.progress.set(0)
        self.progress_var.set("")
        if getattr(self, "_drive_stop", None):
            self._drive_stop.set()
            self._drive_stop = None
        if self.handler:
            self.handler.stop()
        if self.observer:
            self.observer.stop()
            self.observer.join(timeout=3)
        self.observer = self.handler = None
        self.toggle_btn.set_style(ACCENT, ACCENT_DIM, "#FFFFFF")
        self.toggle_btn.set_text("감시 시작")
        self.live_dot.configure(fg=INK_3)
        self._log([("감시 중지\n", "same")], stamp=True)
        self.status_var.set("감시를 멈췄습니다")

    def _flush(self, batch: dict) -> None:
        """감시 스레드에서 호출된다. 여기서 위젯을 만지면 안 된다."""
        if not self.root_path:
            return
        changes = []
        for path, kind in sorted(batch.items()):
            rel = path.relative_to(self.root_path).as_posix()
            if kind == "deleted" or not path.exists():
                changes.append({"path": rel, "change_type": "deleted"})
                continue
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
                content = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            changes.append({"path": rel, "change_type": "modified", "content": content})
        if not changes:
            return

        names = ", ".join(c["path"] for c in changes[:3])
        more = f" 외 {len(changes) - 3}개" if len(changes) > 3 else ""
        self.bridge.send("status", f"{names}{more} 변경됨 · 검사 중")
        self.bridge.send("activity", (names + more, len(changes)))

        req = urllib.request.Request(
            f"{self.api}/api/scan",
            data=json.dumps({"workspace_id": "gui", "changes": changes,
                             "llm": self.llm_var.get(), "doc_check": True,
                             "patent": self.patent_var.get()}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=900) as resp:
                self.bridge.send("result", json.load(resp))
        except urllib.error.HTTPError as exc:
            self.bridge.send("status", f"검사 실패 (HTTP {exc.code})")
        except Exception as exc:
            self.bridge.send("status", f"검사 실패 · {type(exc).__name__}")

    # ------------------------------------------------------------- UI 갱신

    def _drain(self) -> None:
        # 여기서 예외가 새면 after() 사슬이 끊겨 화면이 영영 멈춘다.
        # 한 건이 실패해도 다음 주기는 살아 있어야 한다.
        try:
            self._drain_once()
        except Exception as exc:
            try:
                self._log([(f"    화면 갱신 오류 · {type(exc).__name__} {exc}\n", "up")])
            except Exception:
                pass
        self.after(200, self._drain)

    def _drain_once(self) -> None:
        try:
            while True:
                kind, payload = self.bridge.q.get_nowait()
                if kind == "status":
                    self.status_var.set(payload)
                elif kind == "server":
                    ok, msg = payload
                    self.server_dot.configure(fg=GREEN if ok else RED)
                    self.server_var.set(msg)
                elif kind == "activity":
                    names, count = payload
                    self._log([(f"{names}\n", "file"),
                               (f"    파일 {count}개 · 검사 중…\n", "wait")], stamp=True)
                    self.live_dot.configure(fg=ACCENT)
                elif kind == "quota":
                    if hasattr(self, "_quota_var"):
                        self._quota_var.set(payload)
                elif kind == "progress":
                    self._show_progress(payload)
                elif kind == "polling":
                    self.live_dot.configure(fg=ACCENT)
                    slow = self.llm_var.get() or self.patent_var.get()
                    note = ("  (심층검토·특허가 켜져 있어 처음엔 몇 분 걸립니다)"
                            if payload and slow else "")
                    self._log([(f"Drive 확인 중…{note}\n", "wait")], stamp=True)
                elif kind == "drive_error":
                    self._log([(f"    Drive 오류 · {payload}\n", "up")])
                    self.live_dot.configure(fg=RED)
                elif kind == "result":
                    self._render(payload)
        except queue.Empty:
            pass

    def _show_progress(self, p: dict) -> None:
        if not p.get("running"):
            self.progress.set(0)
            self.progress_var.set("")
            return
        self.progress.set(p.get("percent", 0))
        label, done, total = p.get("label", ""), p.get("done", 0), p.get("total", 0)
        note = p.get("note", "")
        count = f"  {done}/{total}" if total > 1 else ""
        tail = f"   {note[:44]}" if note else ""
        self.progress_var.set(f"{label}{count}   {p.get('percent', 0):.0f}%{tail}")

    def _render(self, result: dict) -> None:
        findings = result["findings"]
        current = {f["locator"] for f in findings}
        new, gone = current - self.previous, self.previous - current
        self.previous = current
        self.findings = {f["locator"]: f for f in findings}

        tiers = result["stats"]["by_tier"]
        self.metrics["total"].configure(text=str(len(findings)))
        for key in ("FORBIDDEN", "RESTRICTED", "REVIEW"):
            self.metrics[key].configure(text=str(tiers.get(key, 0)))

        highlight = new if self.scans else set()   # 첫 검사는 전부 '새 위험'이 아니다
        rows = []
        for f in findings:
            kind = f.get("kind", "")
            rows.append({
                "iid": f["locator"],
                "new": f["locator"] in highlight,
                "cells": [
                    # 색과 굵은 글씨는 등급에만 준다
                    (TIER_LABEL.get(f["tier"], f["tier"]),
                     TIER_COLOR.get(f["tier"], INK), True),
                    (f["locator"], INK, False),
                    (f["license"] or "미상", INK_2, False),
                    (KIND_LABEL.get(kind, kind), INK_2, False),
                ],
            })
        self.tree.set_rows(rows)

        cov = result.get("coverage") or {}
        stamp = time.strftime("%H:%M:%S")
        self.scanned_var.set(f"마지막 검사 {stamp}")

        # 결과가 그대로면 목록이 안 움직인다. 그래도 검사는 돌았다는 걸 알려야
        # 사용자가 "감지가 안 되나?" 하고 의심하지 않는다.
        if self.scans and not new and not gone:
            head_txt = "변동 없음"
        elif not self.scans:
            head_txt = f"초기 검사 · {len(findings)}건 발견" if findings else "초기 검사 · 이상 없음"
        else:
            bits = []
            if new:
                bits.append(f"새 위험 {len(new)}건")
            if gone:
                bits.append(f"해소 {len(gone)}건")
            head_txt = " · ".join(bits)
        self.scans += 1

        self.live_dot.configure(fg=GREEN)
        drv = (result.get("stats") or {}).get("drive") or {}
        if drv:
            if drv.get("unchanged"):
                self._log([(f"    Drive 변동 없음 · 파일 {drv.get('watched_files', 0)}개\n",
                            "same")], stamp=True)
            else:
                changed = drv.get("changed_paths")
                label = ", ".join(changed[:3]) if changed else f"{drv.get('collected', 0)}개 수집"
                self._log([(f"Drive 변경 · {label}\n", "file"),
                           (f"    Google 문서 {drv.get('google_docs_exported', 0)}개 추출\n",
                            "wait")], stamp=True)
        # 바뀐 것만 다시 본다. 재사용 건수를 남겨야 그게 눈에 보인다.
        reuse = (result.get("stats") or {}).get("llm_reuse") or {}
        if reuse.get("reused"):
            self._log([(f"    라이선스 심층검토 {reuse.get('assessed', 0)}건 실행"
                        f" · {reuse['reused']}건 이전 결과 재사용\n", "wait")])
        pat = (result.get("stats") or {}).get("patent") or {}
        if pat.get("screened"):
            self._log([(f"    기획서 {pat['screened']}건 특허 검토 완료"
                        f" (재사용 {pat.get('reused', 0)}건)\n", "wait")])
        for note in pat.get("notes", []):
            self._log([(f"        · {note}\n", "same")])
        if not self.scans - 1:
            self._log([(f"    초기 검사 완료 · {len(findings)}건\n", "same")])
        elif new or gone:
            for loc in sorted(new):
                self._log([(f"    ▲ 새 위험  {loc}\n", "up")])
            for loc in sorted(gone):
                self._log([(f"    ▼ 해소  {loc}\n", "down")])
        else:
            self._log([(f"    변동 없음 · 총 {len(findings)}건\n", "same")])

        parts = [head_txt]
        if cov:
            parts.append(f"의존성 {cov.get('dependencies_resolved', 0)}개 · "
                         f"소스 {cov.get('code_files', 0)}개 검사")
        parts.append(f"{result['stats']['elapsed_sec']}초")
        self.status_var.set("     ".join(parts))

        if not findings:
            # 검사 범위가 좁았는지는 결과만큼 중요하다. 로그에 남긴다.
            if cov and not cov.get("scannable", True):
                self._log([("    검사할 대상이 없습니다 — 위험이 없다는 뜻이 아닙니다\n", "up")]
                          + [(f"        · {n}\n", "same") for n in cov.get("notes", [])])
            elif cov.get("notes"):
                self._log([("    일부만 검사했습니다\n", "wait")]
                          + [(f"        · {n}\n", "same") for n in cov["notes"]])
        elif highlight:
            first = next(f for f in findings if f["locator"] in highlight)
            self.tree.select(first["locator"])

    def _on_open(self, _event) -> None:
        """항목 더블클릭 -> 상세 팝업. 상시 패널 대신 필요할 때만 연다."""
        sel = self.tree.selection()
        if not sel:
            return
        f = self.findings.get(sel[0])
        if not f:
            return

        blocks = [
            (f"{TIER_LABEL.get(f['tier'], f['tier'])}  ·  {f['locator']}\n", f["tier"]),
            (f"{f['license'] or '라이선스 미상'}\n", "muted"),
            (f"\n{f['why']}\n", "body"),
        ]
        llm = f.get("llm")
        if llm:
            flags = "근거 기반" if llm["grounded"] else "근거 부족 · 참고만"
            if llm["needs_legal_review"]:
                flags += "  ·  전문가 검토 필요"
            blocks += [("판정\n", "h"),
                       (f"{llm['verdict']}   {flags}\n", "muted"),
                       (f"\n{llm['explanation_ko']}\n", "body")]
            if llm.get("obligations_ko"):
                blocks.append(("의무사항\n", "h"))
                blocks += [(f"·  {o}\n", "body") for o in llm["obligations_ko"]]
            if llm.get("actions_ko"):
                blocks.append(("이렇게 해보세요\n", "h"))
                blocks += [(f"·  {a}\n", None) for a in llm["actions_ko"]]
        elif not self.llm_var.get():
            blocks.append(("\nGemini 심층 검토를 켜면 근거와 조치가 표시됩니다\n", "same"))

        if f.get("kind") == "patent_similarity":
            # 요약만 보여주면 "어디가 겹치는지" 를 사용자가 못 짚는다.
            # 양쪽 원문을 그대로 인용하고 짝지어진 문장을 같은 색으로 칠한다.
            self._patent_popup(f, blocks)
            return

        if f.get("evidence_ko"):
            blocks += [("근거 원문\n", "head"), (f["evidence_ko"] + "\n", "quote"),
                       (f"\n출처  {f.get('evidence_source', '')}\n", "same")]
        self._popup(f["locator"], blocks, tier=f["tier"])

    def _patent_popup(self, f: dict, header_blocks) -> None:
        """기획서 원문과 특허 초록을 나란히 놓고 겹치는 문장을 같은 색으로 표시."""
        tier = f["tier"]
        color = TIER_COLOR.get(tier, RED)
        matches = f.get("matches") or []

        win = tk.Toplevel(self)
        win.title(f["locator"])
        win.configure(bg=BG)
        win.transient(self)
        w = min(self.px(1000), int(self.winfo_screenwidth() * 0.9))
        h = min(self.px(700), int(self.winfo_screenheight() * 0.85))
        win.geometry(f"{w}x{h}+{max(self.winfo_rootx() + self.px(40), 0)}"
                     f"+{max(self.winfo_rooty() + self.px(40), 0)}")

        top = Card(win, pad=self.px(18), height=self.px(150))
        top.pack(fill="x", padx=self.px(14), pady=(self.px(14), self.px(8)))
        head_txt = tk.Text(top.body, wrap="word", font=self.F.r(10), bg=CARD,
                           fg=INK_2, relief="flat", padx=2, pady=2, spacing3=4,
                           highlightthickness=0, borderwidth=0, cursor="arrow")
        head_txt.pack(fill="both", expand=True)
        head_txt.tag_configure(tier, foreground=color, font=self.F.b(13), spacing3=6)
        head_txt.tag_configure("head", font=self.F.b(10), foreground=INK, spacing1=8)
        head_txt.tag_configure("same", foreground=INK_3)
        for chunk, tag in header_blocks:
            head_txt.insert("end", chunk, tag or ())
        head_txt.configure(state="disabled")

        # ── 좌우 대조
        panes = ttk.PanedWindow(win, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=self.px(14), pady=(0, self.px(8)))

        def make_side(title: str, subtitle: str):
            card = Card(panes, pad=self.px(16))
            tk.Label(card.body, text=title, font=self.F.h, bg=CARD, fg=INK).pack(anchor="w")
            tk.Label(card.body, text=subtitle, font=self.F.small, bg=CARD,
                     fg=INK_3, wraplength=self.px(420), justify="left").pack(
                anchor="w", pady=(2, self.px(8)))
            holder = tk.Frame(card.body, bg=CARD)
            holder.pack(fill="both", expand=True)
            txt = tk.Text(holder, wrap="word", font=self.F.r(10), bg=CARD,
                          fg=INK_2, relief="flat", padx=2, pady=2, spacing1=1, spacing3=3,
                          highlightthickness=0, borderwidth=0, cursor="arrow")
            sb = ttk.Scrollbar(holder, orient="vertical", command=txt.yview,
                               style="T.Vertical.TScrollbar")
            txt.configure(yscrollcommand=sb.set)
            txt.pack(side="left", fill="both", expand=True)
            sb.pack(side="right", fill="y")
            txt.tag_configure("hit", foreground=color, font=self.F.b(10))
            panes.add(card, weight=1)
            return txt

        pat = f.get("patent") or {}
        left = make_side("검사 문서", f["locator"].split(" ~ ")[0])
        right = make_side("선행 특허",
                          f"{pat.get('application_number', '')}  ·  "
                          f"{(pat.get('title_ko') or pat.get('title_en') or '')[:44]}")

        left.insert("1.0", f.get("document_excerpt") or "(원문을 가져오지 못했습니다)")
        right.insert("1.0", f.get("patent_abstract") or "(초록 없음)")

        shown = 0
        for m in matches:
            if m.get("document_verified"):
                self._mark(left, m["document_quote"], "hit")
            if m.get("patent_verified"):
                self._mark(right, m["patent_quote"], "hit")
            if m.get("document_verified") or m.get("patent_verified"):
                shown += 1
        left.configure(state="disabled")
        right.configure(state="disabled")

        # ── 짝지어진 근거 목록
        if matches:
            bottom = Card(win, pad=self.px(16), height=self.px(150))
            bottom.pack(fill="x", padx=self.px(14), pady=(0, self.px(8)))
            tk.Label(bottom.body, text=f"겹치는 문장 {shown}쌍", font=self.F.h,
                     bg=CARD, fg=INK).pack(anchor="w", pady=(0, self.px(6)))
            hold = tk.Frame(bottom.body, bg=CARD)
            hold.pack(fill="both", expand=True)
            lst = tk.Text(hold, wrap="word", font=self.F.r(9), bg=CARD, fg=INK_2,
                          relief="flat", padx=2, pady=2, spacing3=4,
                          highlightthickness=0, borderwidth=0, cursor="arrow")
            sb2 = ttk.Scrollbar(hold, orient="vertical", command=lst.yview,
                                style="T.Vertical.TScrollbar")
            lst.configure(yscrollcommand=sb2.set)
            lst.pack(side="left", fill="both", expand=True)
            sb2.pack(side="right", fill="y")
            lst.tag_configure("q", foreground=color, font=self.F.b(9))
            lst.tag_configure("mute", foreground=INK_3)
            for i, m in enumerate(matches, 1):
                lst.insert("end", f"{i}. ", "mute")
                lst.insert("end", f"{m.get('why_ko', '')}\n", None)
                lst.insert("end", "   검사 문서  ", "mute")
                lst.insert("end", f"{m.get('document_quote', '')[:150]}\n", "q")
                lst.insert("end", "   선행 특허  ", "mute")
                lst.insert("end", f"{m.get('patent_quote', '')[:150]}\n\n", "q")
            lst.configure(state="disabled")

        foot = tk.Frame(win, bg=BG)
        foot.pack(pady=(0, self.px(12)))
        tk.Label(foot, text="침해 판정이 아닙니다 · 초록만 대조했으며 청구범위는 확인하지 않았습니다",
                 font=self.F.small, bg=BG, fg=INK_3).pack(pady=(0, self.px(6)))
        PillButton(foot, "닫기", win.destroy, width=self.px(88), height=self.px(36),
                   fill=BG, hover="#E5E8EB", fg=INK_2, bg=BG, fonts=self.F).pack()
        win.bind("<Escape>", lambda _e: win.destroy())

    @staticmethod
    def _mark(widget: tk.Text, phrase: str, tag: str) -> None:
        """원문에서 인용 문장을 찾아 태그를 입힌다. 공백 차이는 무시한다."""
        if not phrase:
            return
        cnt = tk.IntVar()
        idx = widget.search(phrase, "1.0", stopindex="end", count=cnt, nocase=True)
        if idx:
            widget.tag_add(tag, idx, f"{idx}+{cnt.get()}c")
            return
        # 줄바꿈·공백이 달라 못 찾는 경우가 있다. 앞부분만으로 다시 시도한다.
        head = " ".join(phrase.split())[:40]
        if len(head) < 10:
            return
        idx = widget.search(head, "1.0", stopindex="end", count=cnt, nocase=True)
        if idx:
            widget.tag_add(tag, idx, f"{idx}+{cnt.get()}c")

    def _on_close(self) -> None:
        self._stop()
        self.destroy()


def main() -> int:
    App().mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
