"""Tkinter 界面。

界面约定：

* 所有网络请求、语音检测都在后台线程里跑，结果通过队列回到主线程；
* 任何时刻只有一次请求在飞，画面没变就跳过；
* 关闭窗口会打断朗读并落盘配置，不留僵尸进程。
"""

from __future__ import annotations

import contextlib
import queue
import threading
import time
import tkinter as tk
from dataclasses import replace
from pathlib import Path
from tkinter import font as tkfont
from tkinter import messagebox, ttk
from typing import Any

from PIL import ImageTk

from . import __version__
from .capture import Capture, capture_scene, cursor_position, signature_distance
from .client import ApiError, VisionClient, is_skip_reply
from .config import (
    DEFAULT_BASE_URL,
    REGION_LABELS,
    REGIONS,
    SPEAK_RATE_MAX,
    SPEAK_RATE_MIN,
    Settings,
    api_key_from_env,
    default_config_path,
    parse_interval,
    save,
)
from .hotkey import TOGGLE_RUN, TOGGLE_WINDOW, HotkeyManager
from .models import EMPTY, OK, TEXT_ONLY, check_model, preset_models
from .ocr import OcrResult, ocr_support, recognize
from .pacing import resume_delay
from .pipeline import EmptyScene
from .pipeline import answer as answer_scene
from .speech import Speaker, VoiceInfo, detect_voice

BACKGROUND = "#0e1117"
SURFACE = "#161a22"
SURFACE_ALT = "#1e242f"
BORDER = "#2b3240"
TEXT = "#e9edf5"
MUTED = "#98a2b3"
ACCENT = "#5b8cff"
ACCENT_HOVER = "#7aa3ff"
ON_ACCENT = "#0b1020"
DISABLED = "#3a4256"
SUCCESS = "#3ddc97"
WARNING = "#ffcc66"
DANGER = "#ff6b6b"
STATUS_COLORS = {
    "idle": MUTED,
    "running": SUCCESS,
    "busy": ACCENT,
    "warn": WARNING,
    "error": DANGER,
}
SIMILAR_DISTANCE = 4.0
# 朗读卡住（例如音频设备异常）时的兜底：超过这个秒数就强制继续截图
SPEECH_HOLD_LIMIT = 180.0
UI_FONT_CANDIDATES = ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", "Noto Sans CJK SC")
MONO_FONT_CANDIDATES = ("Cascadia Mono", "Consolas", "DejaVu Sans Mono", "Courier New")


def _pick_font(root: tk.Misc, candidates: tuple[str, ...]) -> str:
    try:
        families = {name.lower(): name for name in tkfont.families(root)}
    except tk.TclError:
        return "TkDefaultFont"
    for name in candidates:
        if name.lower() in families:
            return families[name.lower()]
    return "TkDefaultFont"


class Toggle(tk.Canvas):
    """自绘的开关。ttk 的复选框在深色主题下太丑，这里给一个干净的胶囊开关。"""

    def __init__(
        self,
        parent: tk.Misc,
        text: str,
        variable: tk.BooleanVar,
        *,
        font: tuple[str, int, str],
        scale: float = 1.0,
        command: Any = None,
    ) -> None:
        self._track_w = int(round(36 * scale))
        self._track_h = int(round(20 * scale))
        self._gap = int(round(10 * scale))
        self._text = text
        self._font = font
        self._scale = scale
        self._variable = variable
        self._command = command
        self._enabled = True
        self._ui_font = font[0]

        measure = tkfont.Font(font=font)
        width = self._track_w + self._gap + measure.measure(text) + int(round(4 * scale))
        height = max(self._track_h, measure.metrics("linespace")) + int(round(4 * scale))
        super().__init__(
            parent,
            width=width,
            height=height,
            bg=SURFACE,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        )
        self.bind("<Button-1>", self._on_click)
        self._trace = variable.trace_add("write", lambda *_: self._draw())
        self._draw()

    def destroy(self) -> None:
        with contextlib.suppress(tk.TclError, AttributeError):
            self._variable.trace_remove("write", self._trace)
        super().destroy()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        self.configure(cursor="hand2" if enabled else "arrow")
        self._draw()

    def _on_click(self, _event: tk.Event) -> None:
        if not self._enabled:
            return
        self._variable.set(not self._variable.get())
        if self._command is not None:
            self._command()

    def _draw(self) -> None:
        if not self.winfo_exists():
            return
        self.delete("all")
        on = bool(self._variable.get())
        track = ACCENT if on else BORDER
        knob = "#ffffff" if on else MUTED
        if not self._enabled:
            track = SURFACE_ALT
            knob = DISABLED

        h = self._track_h
        w = self._track_w
        radius = h / 2
        self.create_oval(0, 0, h, h, fill=track, outline=track)
        self.create_oval(w - h, 0, w, h, fill=track, outline=track)
        self.create_rectangle(radius, 0, w - radius, h, fill=track, outline=track)

        inset = max(2.0, h * 0.16)
        knob_size = h - inset * 2
        knob_x = w - h + inset if on else inset
        self.create_oval(
            knob_x, inset, knob_x + knob_size, inset + knob_size, fill=knob, outline=knob
        )
        self.create_text(
            w + self._gap,
            h / 2,
            text=self._text,
            anchor="w",
            fill=TEXT if self._enabled else MUTED,
            font=self._font,
        )


class App(tk.Tk):
    """主窗口。"""

    def __init__(self, settings: Settings, *, config_path: Path | None = None) -> None:
        super().__init__()
        self.withdraw()
        self.config_path = config_path or default_config_path()
        self._events: queue.Queue[tuple[Any, ...]] = queue.Queue()
        self._generation = 0
        self._running = False
        self._inflight = False
        self._after_id: str | None = None
        self._last_digest: bytes | None = None
        self._last_signature: tuple[int, ...] | None = None
        self._last_reply = ""
        self._speaker: Speaker | None = None
        self._session: Settings | None = None
        self._voice: VoiceInfo | None = None
        self._preview_photo: ImageTk.PhotoImage | None = None
        self._inputs: list[tk.Widget] = []
        self._stats = {"ok": 0, "skip": 0, "fail": 0, "full": 0, "last": 0.0}
        self._stats_text = ""
        self._half = (275, 175)
        self._ocr: tuple[bool, str] | None = None
        self._hide_timer: str | None = None
        self._waiting_for_speech = False
        self._speech_since = 0.0
        self._last_capture_at = 0.0
        self._hidden = False

        self._scale = max(1.0, min(2.0, self.winfo_fpixels("1i") / 96.0))
        self.tk.call("tk", "scaling", max(1.0, self.winfo_fpixels("1i") / 72.0))
        self._ui_font = _pick_font(self, UI_FONT_CANDIDATES)
        self._mono_font = _pick_font(self, MONO_FONT_CANDIDATES)
        self._tune_default_fonts()

        self._hotkeys = HotkeyManager(self._on_hotkey)

        self._configure_window()
        self._configure_styles()
        self._build()
        self._apply(settings)
        self._bind_keys()
        self._set_status("idle", "已就绪：把鼠标放到题目上，然后点“开始”")
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.deiconify()

        self.after(120, self._pump)
        threading.Thread(target=self._detect_voice_worker, daemon=True).start()
        threading.Thread(target=self._ocr_support_worker, daemon=True).start()
        self._hotkeys.start()
        self.after(300, self._report_hotkeys)

    # ------------------------------------------------------------------ 基础
    def s(self, value: float) -> int:
        """按 DPI 缩放像素尺寸。"""
        return int(round(value * self._scale))

    def _font(self, size: int, weight: str = "normal", *, mono: bool = False) -> tuple[str, int, str]:
        return ((self._mono_font if mono else self._ui_font), -self.s(size), weight)

    def _tune_default_fonts(self) -> None:
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            try:
                tkfont.nametofont(name).configure(family=self._ui_font, size=-self.s(10))
            except tk.TclError:
                continue

    def _configure_window(self) -> None:
        self.title(f"鼠标指题答疑 · v{__version__}")
        self.configure(bg=BACKGROUND)
        width, height = self.s(700), self.s(900)
        self.minsize(self.s(620), self.s(780))
        screen_w, screen_h = self.winfo_screenwidth(), self.winfo_screenheight()
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 2 - self.s(20))
        self.geometry(f"{width}x{height}+{x}+{y}")
        icon = Path(__file__).resolve().parent.parent / "assets" / "cursor-qa.ico"
        with contextlib.suppress(tk.TclError, OSError):
            if icon.is_file():
                self.iconbitmap(default=str(icon))

    def _configure_styles(self) -> None:
        style = ttk.Style(self)
        with contextlib.suppress(tk.TclError):
            style.theme_use("clam")
        style.configure(".", background=SURFACE, foreground=TEXT, font=self._font(10))
        style.configure("TFrame", background=SURFACE)
        style.configure("TLabel", background=SURFACE, foreground=TEXT)
        style.configure(
            "Field.TEntry",
            fieldbackground=SURFACE_ALT,
            foreground=TEXT,
            insertcolor=TEXT,
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
            borderwidth=1,
            padding=self.s(6),
        )
        style.map(
            "Field.TEntry",
            bordercolor=[("focus", ACCENT)],
            lightcolor=[("focus", ACCENT)],
            darkcolor=[("focus", ACCENT)],
            foreground=[("disabled", MUTED)],
            fieldbackground=[("disabled", SURFACE)],
        )
        style.configure(
            "Field.TCombobox",
            fieldbackground=SURFACE_ALT,
            background=SURFACE_ALT,
            foreground=TEXT,
            arrowcolor=MUTED,
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
            padding=self.s(5),
        )
        style.map(
            "Field.TCombobox",
            fieldbackground=[("readonly", SURFACE_ALT), ("disabled", SURFACE)],
            foreground=[("readonly", TEXT), ("disabled", MUTED)],
            arrowcolor=[("disabled", BORDER)],
            bordercolor=[("focus", ACCENT)],
        )
        style.configure(
            "Field.TSpinbox",
            fieldbackground=SURFACE_ALT,
            foreground=TEXT,
            arrowcolor=MUTED,
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
            padding=self.s(5),
        )
        style.map(
            "Field.TSpinbox",
            bordercolor=[("focus", ACCENT)],
            fieldbackground=[("disabled", SURFACE)],
            foreground=[("disabled", MUTED)],
        )
        style.configure(
            "Accent.TButton",
            background=ACCENT,
            foreground=ON_ACCENT,
            borderwidth=0,
            focuscolor=ACCENT,
            padding=(self.s(16), self.s(9)),
            font=self._font(10, "bold"),
        )
        style.map(
            "Accent.TButton",
            background=[("active", ACCENT_HOVER), ("disabled", DISABLED)],
            foreground=[("disabled", MUTED)],
        )
        style.configure(
            "Ghost.TButton",
            background=SURFACE_ALT,
            foreground=TEXT,
            borderwidth=0,
            focuscolor=SURFACE_ALT,
            padding=(self.s(14), self.s(8)),
            font=self._font(10),
        )
        style.map(
            "Ghost.TButton",
            background=[("active", BORDER), ("disabled", SURFACE)],
            foreground=[("disabled", DISABLED)],
        )
        style.configure(
            "Slim.Vertical.TScrollbar",
            background=SURFACE_ALT,
            troughcolor=SURFACE,
            lightcolor=SURFACE_ALT,
            darkcolor=SURFACE_ALT,
            bordercolor=SURFACE,
            arrowcolor=MUTED,
            gripcount=0,
            arrowsize=self.s(10),
            sliderlength=self.s(24),
        )
        style.map(
            "Slim.Vertical.TScrollbar",
            background=[("active", BORDER), ("pressed", ACCENT)],
            arrowcolor=[("active", TEXT)],
        )
        self.option_add("*TCombobox*Listbox.background", SURFACE_ALT)
        self.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.option_add("*TCombobox*Listbox.selectForeground", ON_ACCENT)
        self.option_add("*TCombobox*Listbox.font", self._font(10))

    def _card(
        self,
        parent: tk.Misc,
        row: int,
        column: int,
        *,
        rowspan: int = 1,
        columnspan: int = 1,
        title: str = "",
        padx: tuple[int, int] = (0, 0),
        pady: tuple[int, int] = (0, 0),
    ) -> tuple[tk.Frame, tk.Frame, tk.Frame | None]:
        card = tk.Frame(parent, bg=SURFACE, highlightbackground=BORDER, highlightthickness=1, bd=0)
        card.grid(
            row=row,
            column=column,
            rowspan=rowspan,
            columnspan=columnspan,
            sticky="nsew",
            padx=(self.s(padx[0]), self.s(padx[1])),
            pady=(self.s(pady[0]), self.s(pady[1])),
        )
        head: tk.Frame | None = None
        if title:
            head = tk.Frame(card, bg=SURFACE)
            head.pack(fill="x", padx=self.s(14), pady=(self.s(10), 0))
            tk.Label(head, text=title, bg=SURFACE, fg=MUTED, font=self._font(9, "bold")).pack(
                side="left"
            )
        body = tk.Frame(card, bg=SURFACE)
        body.pack(fill="both", expand=True, padx=self.s(14), pady=(self.s(8), self.s(14)))
        return card, body, head

    # ------------------------------------------------------------------ 构建
    def _build(self) -> None:
        outer = tk.Frame(self, bg=BACKGROUND, padx=self.s(18), pady=self.s(14))
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(4, weight=1)

        self._build_header(outer)
        self._build_connection(outer)
        self._build_runtime(outer)
        self._build_actions(outer)
        self._build_content(outer)
        self._build_statusbar(outer)

    def _build_header(self, parent: tk.Frame) -> None:
        header = tk.Frame(parent, bg=BACKGROUND)
        header.grid(row=0, column=0, sticky="ew", pady=(0, self.s(12)))
        header.columnconfigure(1, weight=1)

        badge = tk.Canvas(
            header, width=self.s(38), height=self.s(38), bg=BACKGROUND, highlightthickness=0
        )
        badge.grid(row=0, column=0, rowspan=2, sticky="w", padx=(0, self.s(12)))
        badge.create_oval(
            self.s(2), self.s(2), self.s(36), self.s(36), fill=SURFACE, outline=ACCENT, width=2
        )
        badge.create_oval(
            self.s(12), self.s(12), self.s(26), self.s(26), outline=DANGER, width=2
        )

        tk.Label(
            header, text="鼠标指题答疑", bg=BACKGROUND, fg=TEXT, font=self._font(17, "bold")
        ).grid(row=0, column=1, sticky="w")
        tk.Label(
            header,
            text="鼠标停在题目上 → 自动截图 → 多模态模型作答 → 本地语音朗读",
            bg=BACKGROUND,
            fg=MUTED,
            font=self._font(9),
        ).grid(row=1, column=1, sticky="w", pady=(self.s(2), 0))

    def _build_connection(self, parent: tk.Frame) -> None:
        _card, body, _head = self._card(parent, 1, 0, title="接口与模型", pady=(0, self.s(10)))
        body.columnconfigure(1, weight=1)

        def label(row: int, text: str) -> None:
            tk.Label(body, text=text, bg=SURFACE, fg=TEXT, font=self._font(10)).grid(
                row=row, column=0, sticky="w", pady=(self.s(6), 0), padx=(0, self.s(10))
            )

        label(0, "接口地址")
        self.var_base_url = tk.StringVar(value=DEFAULT_BASE_URL)
        self.base_entry = ttk.Entry(body, textvariable=self.var_base_url, style="Field.TEntry")
        self.base_entry.grid(row=0, column=1, sticky="ew", pady=(self.s(6), 0))

        self.model_label = tk.Label(
            body, text="多模态模型", bg=SURFACE, fg=TEXT, font=self._font(10)
        )
        self.model_label.grid(
            row=1, column=0, sticky="w", pady=(self.s(6), 0), padx=(0, self.s(10))
        )
        self.var_model = tk.StringVar()
        self.model_box = ttk.Combobox(
            body, textvariable=self.var_model, values=preset_models(), style="Field.TCombobox"
        )
        self.model_box.grid(row=1, column=1, sticky="ew", pady=(self.s(6), 0))
        self.model_box.bind("<<ComboboxSelected>>", lambda _event: self._refresh_model_hint())
        self.model_box.bind("<KeyRelease>", lambda _event: self._refresh_model_hint())

        self.var_model_hint = tk.StringVar()
        self.model_hint = tk.Label(
            body,
            textvariable=self.var_model_hint,
            bg=SURFACE,
            fg=MUTED,
            font=self._font(9),
            justify="left",
            anchor="w",
            wraplength=self.s(460),
        )
        self.model_hint.grid(row=2, column=1, columnspan=2, sticky="ew", pady=(self.s(5), 0))

        label(3, "API 密钥")
        self.var_api_key = tk.StringVar()
        self.key_entry = ttk.Entry(
            body, textvariable=self.var_api_key, style="Field.TEntry", show="•"
        )
        self.key_entry.grid(row=3, column=1, sticky="ew", pady=(self.s(6), 0))
        self.var_show_key = tk.BooleanVar(value=False)
        self.show_key_check = Toggle(
            body,
            "显示",
            self.var_show_key,
            font=self._font(9),
            scale=self._scale,
            command=self._toggle_key_mask,
        )
        self.show_key_check.grid(row=3, column=2, sticky="w", padx=(self.s(10), 0), pady=(self.s(6), 0))

        self.var_remember = tk.BooleanVar(value=False)
        self.remember_check = Toggle(
            body,
            "在本机明文记住密钥（配置存于 %APPDATA%\\cursor-qa\\config.json）",
            self.var_remember,
            font=self._font(9),
            scale=self._scale,
        )
        self.remember_check.grid(row=4, column=1, columnspan=2, sticky="w", pady=(self.s(6), 0))

        self._inputs.extend(
            [
                self.base_entry,
                self.model_box,
                self.key_entry,
                self.show_key_check,
                self.remember_check,
            ]
        )

    def _build_runtime(self, parent: tk.Frame) -> None:
        _card, body, _head = self._card(parent, 2, 0, title="运行设置", pady=(0, self.s(10)))
        body.columnconfigure(1, weight=1)

        tk.Label(body, text="截图间隔", bg=SURFACE, fg=TEXT, font=self._font(10)).grid(
            row=0, column=0, sticky="w", padx=(0, self.s(10))
        )
        interval_row = tk.Frame(body, bg=SURFACE)
        interval_row.grid(row=0, column=1, sticky="w")
        self.var_interval = tk.StringVar(value="10")
        self.interval_spin = ttk.Spinbox(
            interval_row,
            from_=1,
            to=3600,
            increment=1,
            width=6,
            textvariable=self.var_interval,
            style="Field.TSpinbox",
        )
        self.interval_spin.pack(side="left")
        tk.Label(interval_row, text="秒（1–3600）", bg=SURFACE, fg=MUTED, font=self._font(9)).pack(
            side="left", padx=(self.s(8), 0)
        )

        tk.Label(body, text="取景范围", bg=SURFACE, fg=TEXT, font=self._font(10)).grid(
            row=0, column=2, sticky="w", padx=(self.s(20), self.s(10))
        )
        self.var_region = tk.StringVar(value=REGION_LABELS["standard"])
        self.region_box = ttk.Combobox(
            body,
            textvariable=self.var_region,
            values=list(REGION_LABELS.values()),
            state="readonly",
            width=17,
            style="Field.TCombobox",
        )
        self.region_box.grid(row=0, column=3, sticky="w")

        # 第二行：朗读 / 语速 / 试听
        speak_row = tk.Frame(body, bg=SURFACE)
        speak_row.grid(row=1, column=0, columnspan=4, sticky="w", pady=(self.s(8), 0))

        self.var_speak = tk.BooleanVar(value=True)
        self.speak_check = Toggle(
            speak_row,
            "朗读答案",
            self.var_speak,
            font=self._font(10),
            scale=self._scale,
        )
        self.speak_check.pack(side="left")

        tk.Label(speak_row, text="语速", bg=SURFACE, fg=MUTED, font=self._font(9)).pack(
            side="left", padx=(self.s(16), self.s(6))
        )
        self.var_rate = tk.StringVar(value="0")
        self.rate_spin = ttk.Spinbox(
            speak_row,
            from_=SPEAK_RATE_MIN,
            to=SPEAK_RATE_MAX,
            increment=1,
            width=4,
            textvariable=self.var_rate,
            style="Field.TSpinbox",
        )
        self.rate_spin.pack(side="left")
        tk.Label(
            speak_row,
            text=f"（{SPEAK_RATE_MIN} 最慢 … {SPEAK_RATE_MAX} 最快）",
            bg=SURFACE,
            fg=MUTED,
            font=self._font(9),
        ).pack(side="left", padx=(self.s(6), 0))
        self.voice_button = ttk.Button(
            speak_row, text="试听", style="Ghost.TButton", command=self._preview_voice
        )
        self.voice_button.pack(side="left", padx=(self.s(10), 0))

        # 第三行：隐藏窗口 / OCR / 整屏补全
        option_row = tk.Frame(body, bg=SURFACE)
        option_row.grid(row=2, column=0, columnspan=4, sticky="w", pady=(self.s(10), 0))
        self.var_hide = tk.BooleanVar(value=False)
        self.hide_check = Toggle(
            option_row,
            "运行时隐藏窗口",
            self.var_hide,
            font=self._font(10),
            scale=self._scale,
        )
        self.hide_check.pack(side="left")

        self.var_ocr = tk.BooleanVar(value=False)
        self.ocr_check = Toggle(
            option_row,
            "图片转文字（OCR）",
            self.var_ocr,
            font=self._font(10),
            scale=self._scale,
            command=self._refresh_model_hint,
        )
        self.ocr_check.pack(side="left", padx=(self.s(20), 0))

        self.var_full = tk.BooleanVar(value=True)
        self.full_check = Toggle(
            option_row,
            "题干截断时补发整屏",
            self.var_full,
            font=self._font(10),
            scale=self._scale,
        )
        self.full_check.pack(side="left", padx=(self.s(20), 0))

        self.var_ocr_hint = tk.StringVar()
        self.ocr_hint = tk.Label(
            body,
            textvariable=self.var_ocr_hint,
            bg=SURFACE,
            fg=MUTED,
            font=self._font(9),
            justify="left",
            anchor="w",
            wraplength=self.s(600),
        )
        self.ocr_hint.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(self.s(6), 0))

        self._inputs.extend(
            [
                self.interval_spin,
                self.region_box,
                self.speak_check,
                self.rate_spin,
                self.voice_button,
                self.hide_check,
                self.ocr_check,
                self.full_check,
            ]
        )

    def _build_actions(self, parent: tk.Frame) -> None:
        bar = tk.Frame(parent, bg=BACKGROUND)
        bar.grid(row=3, column=0, sticky="ew", pady=(0, self.s(10)))
        bar.columnconfigure(3, weight=1)

        self.start_button = ttk.Button(bar, text="开始", style="Accent.TButton", command=self._toggle)
        self.start_button.grid(row=0, column=0, sticky="w")
        self.test_button = ttk.Button(
            bar, text="测试连接", style="Ghost.TButton", command=self._test_connection
        )
        self.test_button.grid(row=0, column=1, sticky="w", padx=(self.s(8), 0))
        self.preview_button = ttk.Button(
            bar, text="预览截图", style="Ghost.TButton", command=self._preview
        )
        self.preview_button.grid(row=0, column=2, sticky="w", padx=(self.s(8), 0))
        self.extract_button = ttk.Button(
            bar, text="只提取文字", style="Ghost.TButton", command=self._extract_text
        )
        self.extract_button.grid(row=0, column=3, sticky="w", padx=(self.s(8), 0))
        tk.Label(
            bar,
            text="F5 开始/停止 · Ctrl+T 测试 · Ctrl+P 预览 · Ctrl+Alt+Q 隐藏/显示 · Ctrl+Alt+S 开始/停止",
            bg=BACKGROUND,
            fg=MUTED,
            font=self._font(9),
            anchor="w",
        ).grid(row=1, column=0, columnspan=4, sticky="ew", pady=(self.s(6), 0))

        self._inputs.extend([self.test_button, self.preview_button, self.extract_button])

    def _build_content(self, parent: tk.Frame) -> None:
        content = tk.Frame(parent, bg=BACKGROUND)
        content.grid(row=4, column=0, sticky="nsew")
        content.columnconfigure(0, weight=1)
        content.columnconfigure(1, weight=0, minsize=self.s(240))
        content.rowconfigure(0, weight=3)
        content.rowconfigure(1, weight=2)

        _answer_card, answer_body, answer_head = self._card(
            content, 0, 0, title="答案", padx=(0, 10)
        )
        assert answer_head is not None
        ttk.Button(
            answer_head, text="复制", style="Ghost.TButton", command=self._copy_answer
        ).pack(side="right")
        self.answer_text = self._make_text(answer_body, height=5, font=self._font(12))
        self.answer_text.pack(fill="both", expand=True)

        _preview_card, preview_body, _ = self._card(content, 0, 1, title="最近截图")
        self.preview_label = tk.Label(
            preview_body,
            text="尚未截图\n点“预览截图”可以先看取景范围",
            bg=SURFACE_ALT,
            fg=MUTED,
            font=self._font(9),
            justify="center",
            bd=0,
        )
        self.preview_label.pack(fill="both", expand=True)

        _log_card, log_body, log_head = self._card(
            content, 1, 0, columnspan=2, title="日志", pady=(self.s(10), 0)
        )
        assert log_head is not None
        ttk.Button(log_head, text="清空", style="Ghost.TButton", command=self._clear_log).pack(
            side="right"
        )
        self.log_text = self._make_text(log_body, height=6, font=self._font(9, mono=True))
        self.log_text.pack(fill="both", expand=True)
        for level, color in (
            ("info", MUTED),
            ("ok", SUCCESS),
            ("warn", WARNING),
            ("error", DANGER),
        ):
            self.log_text.tag_configure(level, foreground=color)

    def _make_text(self, parent: tk.Misc, *, height: int, font: tuple[str, int, str]) -> tk.Text:
        wrapper = tk.Frame(parent, bg=SURFACE)
        wrapper.pack(fill="both", expand=True)
        text = tk.Text(
            wrapper,
            height=height,
            wrap="word",
            bg=SURFACE_ALT,
            fg=TEXT,
            relief="flat",
            bd=0,
            padx=self.s(10),
            pady=self.s(8),
            font=font,
            insertbackground=TEXT,
            selectbackground=ACCENT,
            selectforeground=ON_ACCENT,
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=BORDER,
            state="disabled",
        )
        scroll = ttk.Scrollbar(
            wrapper, orient="vertical", command=text.yview, style="Slim.Vertical.TScrollbar"
        )
        text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        return text

    def _build_statusbar(self, parent: tk.Frame) -> None:
        bar = tk.Frame(parent, bg=BACKGROUND)
        bar.grid(row=5, column=0, sticky="ew", pady=(self.s(10), 0))
        bar.columnconfigure(1, weight=1)

        self.dot = tk.Canvas(
            bar, width=self.s(10), height=self.s(10), bg=BACKGROUND, highlightthickness=0
        )
        self.dot.grid(row=0, column=0, padx=(self.s(2), self.s(8)))
        self._dot_id = self.dot.create_oval(0, 0, self.s(9), self.s(9), fill=MUTED, outline="")

        self.var_status = tk.StringVar(value="已停止")
        tk.Label(
            bar,
            textvariable=self.var_status,
            bg=BACKGROUND,
            fg=TEXT,
            font=self._font(9),
            anchor="w",
            justify="left",
        ).grid(row=0, column=1, sticky="ew")

        self.var_stats = tk.StringVar(value="")
        tk.Label(
            bar, textvariable=self.var_stats, bg=BACKGROUND, fg=MUTED, font=self._font(9)
        ).grid(row=0, column=2, sticky="e")

    # ------------------------------------------------------------------ 状态
    def _apply(self, settings: Settings) -> None:
        self.var_base_url.set(settings.base_url)
        self.var_model.set(settings.model)
        self.var_interval.set(str(settings.interval))
        self.var_region.set(REGION_LABELS.get(settings.region, REGION_LABELS["standard"]))
        self.var_speak.set(settings.speak)
        self.var_rate.set(str(settings.speak_rate))
        self.var_hide.set(settings.hide_window)
        self.var_ocr.set(settings.ocr_mode)
        self.var_full.set(settings.auto_full)
        self.var_remember.set(settings.remember_key)
        self.var_api_key.set(settings.api_key or api_key_from_env())
        self._refresh_model_hint()

    def _toggle_key_mask(self) -> None:
        self.key_entry.configure(show="" if self.var_show_key.get() else "•")

    def _refresh_model_hint(self) -> None:
        require_vision = not self.var_ocr.get()
        verdict = check_model(self.var_model.get(), require_vision=require_vision)
        if verdict.level == OK:
            color = SUCCESS
        elif verdict.level in (EMPTY, TEXT_ONLY):
            color = DANGER
        else:
            color = WARNING
        self.model_hint.configure(fg=color)
        self.model_label.configure(text="多模态模型" if require_vision else "模型（OCR 文字）")
        self.var_model_hint.set(verdict.message)
        self._refresh_ocr_hint()

    def _set_status(self, kind: str, message: str) -> None:
        self.var_status.set(message)
        self.dot.itemconfigure(self._dot_id, fill=STATUS_COLORS.get(kind, MUTED))

    def _update_stats(self) -> None:
        last = self._stats["last"]
        full = int(self._stats.get("full", 0))
        tail = f" · 上次 {last:.1f}s" if last else ""
        extra = f" · 整屏 {full}" if full else ""
        text = (
            f"成功 {self._stats['ok']} · 跳过 {self._stats['skip']} · 失败 {self._stats['fail']}"
            f"{extra}{tail}"
        )
        if text != self._stats_text:
            self._stats_text = text
            self.var_stats.set(text)

    def _log(self, level: str, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{stamp}] {message}\n", level)
        self.log_text.see("end")
        lines = int(self.log_text.index("end-1c").split(".")[0])
        if lines > 400:
            self.log_text.delete("1.0", "100.0")
        self.log_text.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _set_answer(self, text: str) -> None:
        self.answer_text.configure(state="normal")
        self.answer_text.delete("1.0", "end")
        self.answer_text.insert("1.0", text)
        self.answer_text.configure(state="disabled")

    def _copy_answer(self) -> None:
        text = self.answer_text.get("1.0", "end").strip()
        if not text:
            self._set_status("warn", "还没有可复制的答案")
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self._set_status("running", "答案已复制到剪贴板")

    def _region_key(self, label: str) -> str:
        for key, value in REGION_LABELS.items():
            if value == label:
                return key
        return "standard"

    def _snapshot(self) -> Settings:
        try:
            interval = parse_interval(self.var_interval.get())
            rate = self._parse_rate(self.var_rate.get())
        except ValueError as error:
            raise ValueError(str(error)) from None
        model = self.var_model.get().strip()
        ocr_mode = bool(self.var_ocr.get())
        if not model:
            raise ValueError("请填写模型名称（多模态模式需要支持图片输入）")
        return Settings(
            interval=interval,
            base_url=self.var_base_url.get().strip() or DEFAULT_BASE_URL,
            model=model,
            region=self._region_key(self.var_region.get()),
            speak=bool(self.var_speak.get()),
            speak_rate=rate,
            hide_window=bool(self.var_hide.get()),
            ocr_mode=ocr_mode,
            auto_full=bool(self.var_full.get()),
            remember_key=bool(self.var_remember.get()),
            api_key=self.var_api_key.get().strip(),
        ).with_defaults()

    @staticmethod
    def _parse_rate(text: str) -> int:
        raw = (text or "").strip()
        if not raw:
            raise ValueError(f"语速请填 {SPEAK_RATE_MIN}–{SPEAK_RATE_MAX} 的整数")
        try:
            value = int(raw)
        except ValueError:
            raise ValueError(f"语速必须是 {SPEAK_RATE_MIN}–{SPEAK_RATE_MAX} 的整数") from None
        if not SPEAK_RATE_MIN <= value <= SPEAK_RATE_MAX:
            raise ValueError(f"语速需在 {SPEAK_RATE_MIN}–{SPEAK_RATE_MAX} 之间")
        return value

    def _persist(self) -> None:
        try:
            settings = self._snapshot()
        except ValueError:
            return
        try:
            save(settings, self.config_path)
        except OSError as error:
            self._log("warn", f"配置保存失败：{error}")

    # ------------------------------------------------------------------ 交互
    def _bind_keys(self) -> None:
        self.bind("<F5>", lambda _event: self._toggle())
        self.bind("<Escape>", lambda _event: self._stop("已停止"))
        self.bind("<Control-t>", lambda _event: self._test_connection())
        self.bind("<Control-p>", lambda _event: self._preview())

    def _toggle(self) -> None:
        if self._running:
            self._stop("已停止")
        else:
            self._start()

    def _start(self) -> None:
        if self._running:
            return
        try:
            settings = self._snapshot()
        except ValueError as error:
            self._set_status("error", str(error))
            self._log("error", str(error))
            return
        if not settings.api_key:
            self._set_status("error", "请填写 API 密钥，或设置环境变量 DEEPSEEK_API_KEY")
            self.key_entry.focus_set()
            return

        if settings.ocr_mode and self._ocr is None:
            # 检测还没跑完就点了开始：同步确认一次（结果会缓存，只慢这一下）
            self._set_status("busy", "正在检测系统 OCR…")
            self.update_idletasks()
            available, detail = ocr_support()
            self._ocr = (available, detail)
            self._refresh_model_hint()

        if settings.ocr_mode and not (self._ocr and self._ocr[0]):
            self._set_status("error", "系统 OCR 不可用，请关掉“图片转文字”或先安装中文语言包")
            self._log("error", "系统 OCR 不可用，无法进入 OCR 模式")
            return

        verdict = check_model(settings.model, require_vision=not settings.ocr_mode)
        if verdict.level == TEXT_ONLY:
            proceed = messagebox.askyesno(
                "这个模型读不到截图",
                verdict.message + "\n\n仍然要用它试试吗？（可以改用“图片转文字”模式）",
                icon="warning",
            )
            if not proceed:
                self.model_box.focus_set()
                self._set_status("warn", "已取消：请先换成多模态模型，或打开 OCR")
                return
        elif not verdict.is_ok:
            self._log("warn", verdict.message)

        if settings.speak and self._voice is not None and not self._voice.available:
            self._log("warn", "未检测到可用的中文语音，本次只显示文字答案")
            settings = replace(settings, speak=False)
        if self._speaker is not None:
            self._speaker.rate = settings.speak_rate

        self._persist()
        self._session = settings
        self._running = True
        self._generation += 1
        self._last_digest = None
        self._last_signature = None
        self._last_reply = ""
        self._stats = {"ok": 0, "skip": 0, "fail": 0, "full": 0, "last": 0.0}
        self._half = (settings.region_size()[0] // 2, settings.region_size()[1] // 2)

        self._set_inputs_enabled(False)
        self.start_button.configure(text="停止")
        mode = "OCR 文字" if settings.ocr_mode else "多模态图片"
        self._log(
            "ok",
            f"开始运行：每 {settings.interval} 秒截图，取景 {settings.region_size()[0]}×"
            f"{settings.region_size()[1]}，{mode}模式，模型 {settings.model}",
        )
        self._set_status("running", "运行中，首次截图即将开始…")
        self._waiting_for_speech = False
        self._last_capture_at = time.monotonic()
        if settings.hide_window:
            self._hide_timer = self.after(self.s(500), self._auto_hide)
        self._schedule(self.s(300))

    def _auto_hide(self) -> None:
        self._hide_timer = None
        if self._running and self.var_hide.get():
            self._hide_window()

    def _set_inputs_enabled(self, enabled: bool) -> None:
        for widget in self._inputs:
            try:
                if isinstance(widget, Toggle):
                    widget.set_enabled(enabled)
                elif isinstance(widget, ttk.Combobox):
                    widget.configure(state="readonly" if enabled else "disabled")
                else:
                    widget.configure(state="normal" if enabled else "disabled")
            except tk.TclError:
                continue
        if enabled:
            self._update_ocr_widgets()

    def _schedule(self, delay_ms: int | None = None) -> None:
        if self._after_id is not None:
            with contextlib.suppress(tk.TclError):
                self.after_cancel(self._after_id)
            self._after_id = None
        if not self._running or self._session is None:
            return
        interval = self._session.interval * 1000
        self._after_id = self.after(interval if delay_ms is None else delay_ms, self._tick)

    def _tick(self) -> None:
        self._after_id = None
        if not self._running or self._session is None:
            return
        if self._inflight or (self._speaker is not None and self._speaker.busy()):
            self._schedule()
            return

        if self._cursor_on_self():
            self._set_status("warn", "鼠标在程序窗口上，移到题目上才会截图")
            self._schedule()
            return

        half_w, half_h = self._half
        try:
            capture = capture_scene(half_w, half_h, include_full=self._session.auto_full)
        except RuntimeError as error:
            self._log("error", str(error))
            self._set_status("error", str(error))
            self._schedule()
            return
        if self._is_same_frame(capture):
            self._set_status("running", "画面没变化，跳过本次截图")
            self._schedule()
            return

        self._last_digest = capture.digest
        self._last_signature = capture.signature
        self._last_capture_at = time.monotonic()
        self._show_preview(capture)
        self._inflight = True
        self._set_status("busy", "正在识别…" if not self._session.ocr_mode else "正在识别文字…")
        session = self._session
        client = VisionClient(session.api_key, base_url=session.base_url, model=session.model)
        threading.Thread(
            target=self._ask_worker,
            args=(self._generation, client, capture, session.ocr_mode, session.auto_full),
            daemon=True,
        ).start()
        self._schedule()

    def _cursor_on_self(self) -> bool:
        """鼠标在自己窗口上时不截图，否则界面自己的变化会引发无限请求。"""
        if self._hidden or not self.winfo_viewable():
            return False
        try:
            x, y = cursor_position()
            left, top = self.winfo_rootx(), self.winfo_rooty()
            right, bottom = left + self.winfo_width(), top + self.winfo_height()
        except (tk.TclError, RuntimeError, OSError):
            return False
        return left <= x <= right and top <= y <= bottom

    def _is_same_frame(self, capture: Capture) -> bool:
        """完全一致或只差几个像素（时钟、光标闪烁）都算同一帧。"""
        if self._last_digest is not None and capture.digest == self._last_digest:
            return True
        if self._last_signature is None:
            return False
        return signature_distance(self._last_signature, capture.signature) <= SIMILAR_DISTANCE

    # ------------------------------------------------------------------ 朗读节奏
    def _hold_for_speech(self) -> None:
        """朗读期间不截图：先撤掉已排好的下一次截图，读完再排。"""
        if self._after_id is not None:
            with contextlib.suppress(tk.TclError):
                self.after_cancel(self._after_id)
            self._after_id = None
        self._waiting_for_speech = True
        self._speech_since = time.monotonic()

    def _resume_after_speech(self) -> None:
        """读完答案后再决定等多久：朗读花掉的时间算进间隔。"""
        self._waiting_for_speech = False
        if not self._running or self._session is None:
            return
        elapsed_ms = max(0.0, (time.monotonic() - self._last_capture_at) * 1000)
        delay = resume_delay(self._session.interval * 1000, elapsed_ms, self.s(350))
        self._schedule(delay)

    def _ask_worker(
        self,
        generation: int,
        client: VisionClient,
        capture: Capture,
        ocr_mode: bool,
        auto_full: bool,
    ) -> None:
        try:
            result = answer_scene(
                client,
                capture,
                ocr_mode=ocr_mode,
                auto_full=auto_full,
                on_note=lambda message: self._events.put(("note", generation, message)),
            )
        except ApiError as error:
            self._events.put(("error", generation, error.full_message()))
        except EmptyScene as error:
            self._events.put(("notice", generation, str(error)))
        except RuntimeError as error:
            self._events.put(("error", generation, f"图片转文字失败：{error}"))
        except Exception as error:  # noqa: BLE001 - 后台线程不能把异常抛给 Tk
            self._events.put(("error", generation, f"请求异常：{error}"))
        else:
            self._events.put(
                ("answer", generation, result.text, result.latency, result.escalated)
            )

    def _stop(self, message: str) -> None:
        if not self._running and not self._inflight:
            return
        self._running = False
        self._generation += 1
        if self._after_id is not None:
            with contextlib.suppress(tk.TclError):
                self.after_cancel(self._after_id)
            self._after_id = None
        if self._hide_timer is not None:
            with contextlib.suppress(tk.TclError):
                self.after_cancel(self._hide_timer)
            self._hide_timer = None
        if self._speaker is not None:
            self._speaker.stop()
        self._inflight = False
        self._waiting_for_speech = False
        self._session = None
        self.start_button.configure(text="开始")
        self._set_inputs_enabled(True)
        if self._hidden:
            self._show_window()
        self._set_status("idle", message)
        self._log("info", message)

    def _test_connection(self) -> None:
        if self._running:
            self._log("warn", "运行中不能测试连接，先按 F5 停止")
            self._set_status("warn", "请先停止当前运行，再测试连接")
            return
        try:
            settings = self._snapshot()
        except ValueError as error:
            self._set_status("error", str(error))
            self._log("error", str(error))
            return
        if not settings.api_key:
            self._set_status("error", "请先填写 API 密钥")
            self.key_entry.focus_set()
            return
        self.test_button.configure(state="disabled")
        self._set_status("busy", "正在测试连接…")
        self._log("info", f"测试连接：{settings.model} @ {settings.base_url}")
        threading.Thread(
            target=self._test_worker, args=(settings,), daemon=True
        ).start()

    def _test_worker(self, settings: Settings) -> None:
        client = VisionClient(settings.api_key, base_url=settings.base_url, model=settings.model)
        vision = not settings.ocr_mode
        try:
            reply = client.test() if vision else client.test_text()
        except ApiError as error:
            self._events.put(("test", False, error.full_message(), vision))
        except Exception as error:  # noqa: BLE001
            self._events.put(("test", False, f"测试异常：{error}", vision))
        else:
            ok = "ok" in reply.text.lower()
            if ok:
                what = "能读取图片" if vision else "文字接口可用"
                message = f"连接正常（{reply.latency:.1f}s）：{settings.model} {what}"
            else:
                message = (
                    f"模型返回「{reply.text[:60]}」，没有按测试要求回复 OK，可能接口或模型不对"
                )
            self._events.put(("test", ok, message, vision))

    def _preview(self) -> None:
        """只截图、不请求；不校验模型与接口，方便先确认取景范围。"""
        if self._running:
            self._log("warn", "运行中不能预览取景，先按 F5 停止")
            return
        width, height = REGIONS.get(self._region_key(self.var_region.get()), REGIONS["standard"])
        half_w, half_h = width // 2, height // 2
        try:
            capture = capture_scene(half_w, half_h, include_full=False)
        except RuntimeError as error:
            self._set_status("error", str(error))
            self._log("error", str(error))
            return
        self._show_preview(capture)
        left, top, right, bottom = capture.box
        self._set_status("running", f"预览：{right - left}×{bottom - top} @ ({left},{top})")
        self._log("info", f"预览截图 {capture.size[0]}×{capture.size[1]}，光标 {capture.cursor}")

    # ------------------------------------------------------------------ 图片转文字
    def _refresh_ocr_hint(self) -> None:
        if self._ocr is None:
            self.var_ocr_hint.set("正在检测系统 OCR …")
            self.ocr_hint.configure(fg=MUTED)
            return
        available, detail = self._ocr
        if available:
            if self.var_ocr.get():
                self.var_ocr_hint.set(
                    f"OCR 已就绪（{detail}）：截图先转文字再答题，纯文本模型也能用；"
                    "公式、图表类题目建议关掉 OCR 改用多模态。"
                )
                self.ocr_hint.configure(fg=SUCCESS)
            else:
                self.var_ocr_hint.set(
                    f"系统 OCR 可用（{detail}）。打开后可以选纯文本模型，省流量；"
                    "鼠标指向靠识别出的行位置判断。"
                )
                self.ocr_hint.configure(fg=MUTED)
        else:
            self.var_ocr_hint.set(f"系统 OCR 不可用：{detail}")
            self.ocr_hint.configure(fg=WARNING)

    def _update_ocr_widgets(self) -> None:
        """OCR 不可用时锁住开关，避免开了却跑不起来。"""
        if self._running:
            return
        available = bool(self._ocr and self._ocr[0])
        self.ocr_check.set_enabled(available or self._ocr is None)
        if self._ocr is not None and not available:
            self.var_ocr.set(False)

    def _ocr_support_worker(self) -> None:
        try:
            available, detail = ocr_support(refresh=True)
        except Exception as error:  # noqa: BLE001
            available, detail = False, str(error)
        self._events.put(("ocr", available, detail))

    def _extract_text(self) -> None:
        """只截图 + OCR，把文字放进答案栏，不调用模型。"""
        if self._running:
            self._log("warn", "运行中不能单独提取文字，先按 F5 停止")
            return
        if not (self._ocr and self._ocr[0]):
            message = "系统 OCR 不可用" + (f"：{self._ocr[1]}" if self._ocr else "（还在检测）")
            self._log("warn", message)
            self._set_status("warn", message)
            return
        width, height = REGIONS.get(self._region_key(self.var_region.get()), REGIONS["standard"])
        try:
            capture = capture_scene(width // 2, height // 2, include_full=False)
        except RuntimeError as error:
            self._set_status("error", str(error))
            self._log("error", str(error))
            return
        self._show_preview(capture)
        self.extract_button.configure(state="disabled")
        self._set_status("busy", "正在提取文字…")
        threading.Thread(target=self._extract_worker, args=(capture,), daemon=True).start()

    def _extract_worker(self, capture: Capture) -> None:
        try:
            result = recognize(capture.image)
        except RuntimeError as error:
            self._events.put(("ocr_text", None, str(error)))
        except Exception as error:  # noqa: BLE001
            self._events.put(("ocr_text", None, f"提取失败：{error}"))
        else:
            self._events.put(("ocr_text", result, None))

    # ------------------------------------------------------------------ 语音
    def _preview_voice(self) -> None:
        if self._speaker is None:
            self._set_status("warn", "没有可用的中文语音，无法试听")
            return
        try:
            rate = self._parse_rate(self.var_rate.get())
        except ValueError as error:
            self._set_status("error", str(error))
            return
        self._speaker.rate = rate
        if self._speaker.busy():
            self._speaker.stop()
        try:
            self._speaker.speak("这是当前语速，答案会以这个速度朗读。")
        except RuntimeError as error:
            self._log("warn", f"试听失败：{error}")
            self._set_status("warn", "试听失败，请检查 Windows 语音设置")
        else:
            self._set_status("busy", f"试听中（语速 {rate}）…")

    # ------------------------------------------------------------------ 窗口与热键
    def _hide_window(self) -> None:
        if self._hidden:
            return
        self.withdraw()
        self._hidden = True
        self._log("info", "窗口已隐藏：Ctrl+Alt+Q 唤回窗口，Ctrl+Alt+S 开始/停止")

    def _show_window(self) -> None:
        if not self._hidden:
            self.lift()
            return
        self.deiconify()
        self._hidden = False
        with contextlib.suppress(tk.TclError):
            self.lift()
            self.focus_force()

    def _toggle_window(self) -> None:
        if self._hidden or not self.winfo_viewable():
            self._show_window()
            self._set_status("running" if self._running else "idle", "窗口已唤回")
        else:
            self._hide_window()

    def _on_hotkey(self, binding_id: int) -> None:
        # 这个回调跑在热键线程里，只能塞队列
        self._events.put(("hotkey", binding_id))

    def _report_hotkeys(self) -> None:
        active = self._hotkeys.active
        failures = self._hotkeys.failures
        if active:
            self._log("ok", f"全局快捷键：{', '.join(active)}")
        if failures:
            self._log(
                "warn",
                f"快捷键注册失败（可能被别的程序占用）：{', '.join(failures)}，可只用界面按钮",
            )

    def _show_preview(self, capture: Capture) -> None:
        thumb = capture.image.copy()
        thumb.thumbnail((self.s(228), self.s(150)))
        photo = ImageTk.PhotoImage(thumb)
        self._preview_photo = photo
        self.preview_label.configure(image=photo, text="")

    # ------------------------------------------------------------------ 事件泵
    def _pump(self) -> None:
        try:
            while True:
                try:
                    event = self._events.get_nowait()
                except queue.Empty:
                    break
                self._handle(event)
            self._check_speech()
            self._update_stats()
        except tk.TclError:
            return
        self.after(120, self._pump)

    def _handle(self, event: tuple[Any, ...]) -> None:
        kind = event[0]
        if kind == "answer":
            _, generation, text, latency, escalated = event
            if generation != self._generation:
                return
            self._inflight = False
            self._stats["last"] = float(latency)
            if is_skip_reply(text):
                self._stats["skip"] += 1
                self._log("info", "模型答复 SKIP：鼠标没有指到题干")
                self._set_status("warn", "把鼠标指向题干，答案会自动出现")
                return
            self._stats["ok"] += 1
            if escalated:
                self._stats["full"] = int(self._stats.get("full", 0)) + 1
            self._set_answer(text)
            suffix = "（已用整屏补全）" if escalated else ""
            self._log("ok", f"作答完成（{float(latency):.1f}s）{suffix}：{text[:40]}")
            if self._speak(text):
                # 朗读期间不截图，读完再排下一题
                self._hold_for_speech()
                self._log("info", "朗读中：读完这一题的答案再截下一题")
            else:
                self._set_status("running", "已作答，等待下一次截图")
        elif kind == "note":
            _, generation, message = event
            if generation != self._generation:
                return
            self._log("info", message)
            self._set_status("busy", message)
        elif kind == "error":
            _, generation, message = event
            if generation != self._generation:
                return
            self._inflight = False
            self._stats["fail"] += 1
            self._log("error", message)
            self._set_status("error", message[:80])
        elif kind == "test":
            _, ok, message, vision = event
            self.test_button.configure(state="disabled" if self._running else "normal")
            self._log("ok" if ok else "error", message)
            self._set_status("running" if ok else "error", message[:80])
            if ok and vision:
                self.var_model_hint.set(f"✓ 实测通过：{self.var_model.get().strip()} 能读取图片")
                self.model_hint.configure(fg=SUCCESS)
            elif ok:
                self.var_model_hint.set(f"✓ 实测通过：{self.var_model.get().strip()} 文字接口正常")
                self.model_hint.configure(fg=SUCCESS)
        elif kind == "voice":
            info: VoiceInfo = event[1]
            self._voice = info
            if info.available:
                self._speaker = Speaker(info.voice_id, rate=self._safe_rate())
                self._log("ok", f"中文语音：{info.name}")
            else:
                self._log("warn", f"朗读不可用：{info.detail}")
                self.var_speak.set(False)
        elif kind == "ocr":
            available, detail = event[1], event[2]
            self._ocr = (bool(available), str(detail))
            self._refresh_model_hint()
            self._update_ocr_widgets()
            if available:
                self._log("ok", f"系统 OCR 可用：{detail}")
            else:
                self._log("warn", f"系统 OCR 不可用：{detail}")
        elif kind == "ocr_text":
            result, error = event[1], event[2]
            self.extract_button.configure(state="normal")
            if error:
                self._log("error", error)
                self._set_status("error", str(error)[:80])
                return
            assert isinstance(result, OcrResult)
            if not result.lines:
                self._log("info", "截图里没有识别到文字")
                self._set_status("warn", "截图里没有识别到文字")
                return
            self._set_answer(result.text)
            self._log("ok", f"提取到 {len(result.lines)} 行文字（{result.language}）")
            self._set_status("running", "文字已放进答案栏，点“复制”即可")
        elif kind == "notice":
            _, generation, message = event
            if generation != self._generation:
                return
            self._inflight = False
            self._stats["skip"] += 1
            self._log("info", message)
            self._set_status("warn", message)
        elif kind == "hotkey":
            binding_id = int(event[1])
            if binding_id == TOGGLE_WINDOW.id:
                self._toggle_window()
            elif binding_id == TOGGLE_RUN.id:
                self._toggle()

    def _detect_voice_worker(self) -> None:
        try:
            info = detect_voice()
        except Exception as error:  # noqa: BLE001
            info = VoiceInfo(False, detail=str(error))
        self._events.put(("voice", info))

    def _check_speech(self) -> None:
        if self._speaker is None:
            return
        code = self._speaker.poll()
        if code is None:
            if (
                self._waiting_for_speech
                and self._running
                and time.monotonic() - self._speech_since > SPEECH_HOLD_LIMIT
            ):
                self._log("warn", "朗读迟迟没有结束，已强制继续截图")
                self._speaker.stop()
                self._resume_after_speech()
            return
        if code != 0:
            detail = self._speaker.last_error() or f"返回码 {code}"
            self._log("warn", f"朗读失败：{detail}")
            if self._waiting_for_speech:
                self._resume_after_speech()
            if self._running:
                self._set_status("warn", "朗读失败，答案已显示")
        elif self._running:
            if self._waiting_for_speech:
                self._resume_after_speech()
                self._set_status("running", "朗读完成，继续看下一题")
            else:
                self._set_status("running", "朗读完成，等待下一次截图")

    def _speak(self, text: str) -> bool:
        """开始朗读；返回是否真的起了朗读进程（决定要不要暂停截图）。"""
        if self._session is None or not self._session.speak or self._speaker is None:
            return False
        if text == self._last_reply:
            return False
        self._last_reply = text
        self._speaker.rate = self._session.speak_rate
        try:
            self._speaker.speak(text)
        except RuntimeError as error:
            self._log("warn", f"朗读失败：{error}")
            return False
        self._set_status("busy", "正在朗读…")
        return True

    def _safe_rate(self) -> int:
        try:
            return self._parse_rate(self.var_rate.get())
        except ValueError:
            return 0

    # ------------------------------------------------------------------ 收尾
    def _on_close(self) -> None:
        self._running = False
        if self._after_id is not None:
            with contextlib.suppress(tk.TclError):
                self.after_cancel(self._after_id)
            self._after_id = None
        if self._hide_timer is not None:
            with contextlib.suppress(tk.TclError):
                self.after_cancel(self._hide_timer)
            self._hide_timer = None
        if self._speaker is not None:
            self._speaker.stop()
        self._hotkeys.stop()
        self._persist()
        self.destroy()


def run(settings: Settings | None = None, *, config_path: Path | None = None) -> None:
    """创建窗口并进入事件循环。调用前应先 ``set_dpi_awareness()``。"""
    app = App(settings or Settings(), config_path=config_path)
    app.mainloop()
