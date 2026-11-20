"""鼠标位置截图。

要点：

* 进程声明 DPI 感知，避免 125% / 150% 缩放下截到“虚”的图；
* 多显示器用 ``all_screens=True`` 抓虚拟桌面，坐标减掉虚拟桌面原点；
* 靠边时自动收拢取景框，不让裁剪越界；
* 输出 PNG（base64）并顺手算 sha256，用来跳过完全相同的画面。
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
SIGNATURE_SIZE = (16, 10)

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79


@dataclass
class Capture:
    """一次截图的结果。"""

    image: Image.Image = field(repr=False)
    digest: bytes = b""
    png_base64: str = ""
    box: tuple[int, int, int, int] = (0, 0, 0, 0)
    cursor: tuple[int, int] = (0, 0)
    signature: tuple[int, ...] = ()

    @property
    def size(self) -> tuple[int, int]:
        return self.image.size


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


def _grab(box: tuple[int, int, int, int]) -> Image.Image:
    """抓取虚拟桌面上的指定区域。"""
    try:
        return ImageGrab.grab(bbox=box, all_screens=True)
    except (OSError, ValueError):
        # 部分远程桌面 / 旧系统不支持 bbox，退化为整屏再裁。
        full = ImageGrab.grab(all_screens=True)
        origin_x, origin_y, _, _ = virtual_screen()
        return full.crop((
            box[0] - origin_x,
            box[1] - origin_y,
            box[2] - origin_x,
            box[3] - origin_y,
        ))


def capture_cursor(
    half_width: int = 275,
    half_height: int = 175,
    *,
    max_width: int = MAX_WIDTH,
    marker: bool = True,
) -> Capture:
    """截取光标周围区域，画上标记圈，返回可用于请求的 PNG。"""
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

    box = compute_box(local_x, local_y, screen_width, screen_height, half_width, half_height)
    try:
        image = _grab((box[0] + origin_x, box[1] + origin_y, box[2] + origin_x, box[3] + origin_y))
    except OSError as error:
        raise RuntimeError("截图失败，可能被安全策略拦截或屏幕正在锁屏") from error

    image = image.convert("RGB")
    scale = 1.0
    if max_width and image.width > max_width:
        scale = max_width / image.width
        new_size = (max_width, max(1, int(round(image.height * scale))))
        image = image.resize(new_size, Image.LANCZOS)

    if marker:
        cx = (local_x - box[0]) * scale
        cy = (local_y - box[1]) * scale
        radius = max(6, int(round(MARKER_RADIUS * scale)))
        draw = ImageDraw.Draw(image)
        draw.ellipse(
            (cx - radius, cy - radius, cx + radius, cy + radius),
            outline=MARKER_COLOR,
            width=MARKER_WIDTH,
        )

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    png = buffer.getvalue()

    return Capture(
        image=image,
        digest=hashlib.sha256(png).digest(),
        png_base64=base64.b64encode(png).decode("ascii"),
        box=box,
        cursor=(local_x, local_y),
        signature=frame_signature(image),
    )
