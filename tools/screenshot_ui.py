"""生成 assets/ui.png 界面示意图（不影响用户真实配置）。

    python tools/screenshot_ui.py
"""

from __future__ import annotations

import contextlib
import sys
import tempfile
import tkinter as tk
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageGrab

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cursorqa.capture import Capture, set_dpi_awareness  # noqa: E402
from cursorqa.config import Settings  # noqa: E402
from cursorqa.ui import App  # noqa: E402

OUT = ROOT / "assets" / "ui.png"
FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhl.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
)


def _load_font(size: int) -> ImageFont.ImageFont:
    for path in FONT_CANDIDATES:
        if Path(path).is_file():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def mock_question(width: int = 550, height: int = 350) -> Image.Image:
    """画一张假的题目截图，避免把真实屏幕内容放进仓库。"""
    image = Image.new("RGB", (width, height), "#fbfbfd")
    draw = ImageDraw.Draw(image)
    body = _load_font(21)
    small = _load_font(20)
    draw.rectangle((0, 0, width - 1, height - 1), outline="#c8ccd8")
    lines = (
        "7. 物块沿粗糙斜面下滑，速度由 2 m/s 增至 6 m/s，",
        "    位移 8 m。若质量 2 kg，则合外力做功为：",
    )
    for index, line in enumerate(lines):
        draw.text((28, 26 + index * 32), line, fill="#1b1f2a", font=body)
    for index, option in enumerate(("A. 8 J", "B. 16 J", "C. 32 J", "D. 64 J")):
        draw.text((56, 118 + index * 40), option, fill="#1b1f2a", font=small)
    draw.ellipse((196, 142, 226, 172), outline="#ff3355", width=4)
    return image


def main() -> None:
    set_dpi_awareness()
    with tempfile.TemporaryDirectory() as tmp:
        app = App(
            Settings(model="deepseek-flash", interval=10, api_key=""),
            config_path=Path(tmp) / "config.json",
        )
        app.lift()
        with contextlib.suppress(tk.TclError):
            app.attributes("-topmost", True)

        def fill() -> None:
            """等启动日志（热键/OCR/语音）落定后再补演示内容，时间顺序才自然。"""
            app._set_answer(
                "答案：B。动能增加 1/2 × 2 × (6² − 2²) = 32 J，合外力做功即动能变化量。"
            )
            app._clear_log()
            app._log("ok", "系统 OCR 可用：zh-Hans-CN")
            app._log("ok", "中文语音：Microsoft Huihui Desktop - Chinese (Simplified)")
            app._log("ok", "作答完成（1.6s）：答案 B，动能变化 32 J")
            app._log("info", "朗读中：读完这一题的答案再截下一题")
            app._stats.update({"ok": 4, "skip": 1, "fail": 0, "last": 1.6})
            app._set_status("busy", "正在朗读…")
            app._show_preview(Capture(image=mock_question()))

        def snap() -> None:
            app.update_idletasks()
            box = (
                app.winfo_rootx(),
                app.winfo_rooty(),
                app.winfo_rootx() + app.winfo_width(),
                app.winfo_rooty() + app.winfo_height(),
            )
            ImageGrab.grab(bbox=box).save(OUT)
            print(f"已保存 {OUT}")
            app.destroy()

        app.after(1300, fill)
        app.after(2300, snap)
        app.after(9000, app.destroy)
        app.mainloop()


if __name__ == "__main__":
    main()
