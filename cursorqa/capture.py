"""鼠标位置截图（光标小块 + 整屏）。

要点：

* 进程声明 DPI 感知，避免 125% / 150% 缩放下截到“虚”的图；
* 每次只抓一次虚拟桌面，再裁出光标附近的小块——顺带把整屏留作“题干被截断”时补全；
* 靠边时自动收拢取景框，不让裁剪越界；
* 输出 PNG（base64）并算 sha256 + 16×10 灰度指纹，用来跳过完全相同的画面。
"""

from __future__ import annotations

import base64
import contextlib
import ctypes
import hashlib
import io
from ctypes import wintypes
from dataclasses import dataclass, field

from PIL import Image, ImageDraw, ImageGrab

MARKER_COLOR = "#ff3355"
MARKER_RADIUS = 11
MARKER_WIDTH = 3
MIN_EDGE = 48
MAX_WIDTH = 1600
MAX_FULL_WIDTH = 1400
SIGNATURE_SIZE = (16, 10)

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79


@dataclass
class Capture:
    """一次截图的结果：光标附近的小块 +（可选的）整屏，用来补全题干。"""

    image: Image.Image = field(repr=False)
    digest: bytes = b""
    png_base64: str = ""
    box: tuple[int, int, int, int] = (0, 0, 0, 0)
    cursor: tuple[int, int] = (0, 0)  # 裁剪图内坐标
    signature: tuple[int, ...] = ()
    cursor_screen: tuple[int, int] = (0, 0)  # 虚拟桌面内坐标
    screen_image: Image.Image | None = field(default=None, repr=False)
    screen_origin: tuple[int, int] = (0, 0)
    max_full_width: int = MAX_FULL_WIDTH
    _full: Image.Image | None = field(default=None, repr=False, compare=False)
    _full_b64: str = field(default="", repr=False, compare=False)

    @property
    def size(self) -> tuple[int, int]:
        return self.image.size

    @property
    def has_full(self) -> bool:
        return self.screen_image is not None

    def full_image(self) -> Image.Image:
        """整屏图（缩放到 ≤1400px 宽，带红圈），按需生成并缓存。"""
        if self._full is None:
            if self.screen_image is None:
                raise RuntimeError("这次没有截到整屏")
            image = self.screen_image
            if self.max_full_width and image.width > self.max_full_width:
                scale = self.max_full_width / image.width
                image = image.resize(
                    (self.max_full_width, max(1, int(round(image.height * scale)))),
                    Image.Resampling.BILINEAR,
                )
            image = image.copy()
            scale = image.width / self.screen_image.width if self.screen_image.width else 1.0
            _draw_marker(image, self.cursor_screen[0] * scale, self.cursor_screen[1] * scale, scale)
            self._full = image
        return self._full

    def full_png_base64(self) -> str:
        if not self._full_b64:
            buffer = io.BytesIO()
            self.full_image().save(buffer, format="PNG", optimize=True)
            self._full_b64 = base64.b64encode(buffer.getvalue()).decode("ascii")
        return self._full_b64

    def cursor_in_full(self) -> tuple[int, int]:
        """光标在整屏图（缩放后）里的坐标。"""
        image = self.full_image()
        if not self.screen_image or not self.screen_image.width:
            return (0, 0)
        scale = image.width / self.screen_image.width
        return (
            int(round(self.cursor_screen[0] * scale)),
            int(round(self.cursor_screen[1] * scale)),
        )


def set_dpi_awareness() -> None:
    """尽量拿到物理像素坐标；老系统退回旧 API，再不行就当没设置。"""
    user32 = getattr(ctypes.windll, "user32", None)
    if user32 is None:
        return
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except (AttributeError, OSError):
        pass
    with contextlib.suppress(AttributeError, OSError):
        user32.SetProcessDPIAware()


def virtual_screen() -> tuple[int, int, int, int]:
    """虚拟桌面 (x, y, width, height)，多显示器时 x/y 可能为负。"""
    user32 = ctypes.windll.user32
    return (
        user32.GetSystemMetrics(SM_XVIRTUALSCREEN),
        user32.GetSystemMetrics(SM_YVIRTUALSCREEN),
        user32.GetSystemMetrics(SM_CXVIRTUALSCREEN),
        user32.GetSystemMetrics(SM_CYVIRTUALSCREEN),
    )


def cursor_position() -> tuple[int, int]:
    point = wintypes.POINT()
    if not ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
        raise RuntimeError("读取鼠标位置失败，请确认程序运行在桌面会话中")
    return point.x, point.y


def frame_signature(image: Image.Image, size: tuple[int, int] = SIGNATURE_SIZE) -> tuple[int, ...]:
    """把画面压成 16×10 的灰度网格（每格取平均），用于判断“还是同一道题吗”。"""
    small = image.convert("L").resize(size, Image.Resampling.BOX)
    return tuple(small.tobytes())


def signature_distance(left: tuple[int, ...], right: tuple[int, ...]) -> float:
    """两帧的平均绝对差（0–255）。0 表示完全一致，越小越像。"""
    if not left or not right or len(left) != len(right):
        return float("inf")
    return sum(abs(a - b) for a, b in zip(left, right, strict=True)) / len(left)


def compute_box(
    cursor_x: int,
    cursor_y: int,
    screen_width: int,
    screen_height: int,
    half_width: int,
    half_height: int,
) -> tuple[int, int, int, int]:
    """以光标为中心算裁剪框，自动收拢到屏幕内并保证最小边长。"""
    width = max(MIN_EDGE, screen_width)
    height = max(MIN_EDGE, screen_height)
    cx = min(max(cursor_x, 0), width - 1)
    cy = min(max(cursor_y, 0), height - 1)

    left, right = cx - half_width, cx + half_width
    if left < 0:
        right -= left
        left = 0
    if right > width:
        left -= right - width
        right = width
    left = max(0, left)
    right = min(width, right)
    if right - left < MIN_EDGE:
        right = min(width, left + MIN_EDGE)

    top, bottom = cy - half_height, cy + half_height
    if top < 0:
        bottom -= top
        top = 0
    if bottom > height:
        top -= bottom - height
        bottom = height
    top = max(0, top)
    bottom = min(height, bottom)
    if bottom - top < MIN_EDGE:
        bottom = min(height, top + MIN_EDGE)

    return int(left), int(top), int(right), int(bottom)


def _draw_marker(image: Image.Image, x: float, y: float, scale: float = 1.0) -> None:
    radius = max(6, int(round(MARKER_RADIUS * scale)))
    draw = ImageDraw.Draw(image)
    draw.ellipse(
        (x - radius, y - radius, x + radius, y + radius),
        outline=MARKER_COLOR,
        width=MARKER_WIDTH,
    )


def _grab_screen() -> Image.Image:
    try:
        return ImageGrab.grab(all_screens=True).convert("RGB")
    except (OSError, ValueError) as error:
        raise RuntimeError("截图失败，可能被安全策略拦截或屏幕正在锁屏") from error


def capture_scene(
    half_width: int = 275,
    half_height: int = 175,
    *,
    max_crop_width: int = MAX_WIDTH,
    max_full_width: int = MAX_FULL_WIDTH,
    include_full: bool = True,
    marker: bool = True,
) -> Capture:
    """截一次整屏，裁出光标附近的小块；整屏留着给“题干被截断”时补全用。"""
    origin_x, origin_y, screen_width, screen_height = virtual_screen()
    if screen_width <= 0 or screen_height <= 0:
        raise RuntimeError("无法读取显示器尺寸，请确认没有锁屏")
    try:
        cursor_x, cursor_y = cursor_position()
    except (OSError, ctypes.ArgumentError) as error:
        raise RuntimeError("读取鼠标位置失败，请确认程序运行在桌面会话中") from error

    local_x = cursor_x - origin_x
    local_y = cursor_y - origin_y
    if not (0 <= local_x < screen_width and 0 <= local_y < screen_height):
        raise RuntimeError("鼠标不在可截图范围内")

    screen = _grab_screen()
    screen_width = min(screen_width, screen.width)
    screen_height = min(screen_height, screen.height)

    box = compute_box(local_x, local_y, screen_width, screen_height, half_width, half_height)
    image = screen.crop(box)

    scale = 1.0
    if max_crop_width and image.width > max_crop_width:
        scale = max_crop_width / image.width
        image = image.resize(
            (max_crop_width, max(1, int(round(image.height * scale)))),
            Image.LANCZOS,
        )

    cursor_local = (local_x - box[0], local_y - box[1])
    if marker:
        _draw_marker(image, cursor_local[0] * scale, cursor_local[1] * scale, scale)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    png = buffer.getvalue()

    return Capture(
        image=image,
        digest=hashlib.sha256(png).digest(),
        png_base64=base64.b64encode(png).decode("ascii"),
        box=box,
        cursor=cursor_local,
        signature=frame_signature(image),
        cursor_screen=(local_x, local_y),
        screen_image=screen if include_full else None,
        screen_origin=(origin_x, origin_y),
        max_full_width=max_full_width,
    )


def capture_cursor(
    half_width: int = 275,
    half_height: int = 175,
    *,
    max_width: int = MAX_WIDTH,
    marker: bool = True,
) -> Capture:
    """只要光标附近的小块（兼容旧调用）。"""
    return capture_scene(
        half_width,
        half_height,
        max_crop_width=max_width,
        include_full=False,
        marker=marker,
    )
