"""生成 assets/cursor-qa.ico（图标同时被窗口和 README 使用）。

    python tools/make_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "assets" / "cursor-qa.ico"
SIZE = 256


def render(size: int = SIZE) -> Image.Image:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    scale = size / 256

    def px(value: float) -> float:
        return value * scale

    draw.rounded_rectangle(
        (px(8), px(8), px(248), px(248)),
        radius=px(56),
        fill=(22, 26, 34, 255),
        outline=(91, 140, 255, 255),
        width=int(max(2, px(10))),
    )
    ring = int(max(2, px(9)))
    draw.ellipse(
        (px(84), px(84), px(172), px(172)),
        outline=(255, 107, 107, 255),
        width=ring,
    )
    cross = (255, 233, 237, 255)
    draw.line((px(128), px(52), px(128), px(204)), fill=cross, width=int(max(2, px(8))))
    draw.line((px(52), px(128), px(204), px(128)), fill=cross, width=int(max(2, px(8))))
    draw.ellipse((px(120), px(120), px(136), px(136)), fill=(255, 51, 85, 255))
    return image


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    base = render()
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    base.save(OUT, format="ICO", sizes=sizes)
    preview = OUT.with_name("cursor-qa.png")
    base.save(preview, format="PNG")
    print(f"已生成 {OUT} 与 {preview}")


if __name__ == "__main__":
    main()
