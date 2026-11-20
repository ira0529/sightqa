"""截图节奏。

朗读答案期间不截图；读完之后再决定下一次截图要等多久——
以「上一次截图」为起点算间隔，朗读花掉的时间算数，
所以读得久就不必再等，读得快就补足剩余间隔（并保底一小段停顿）。
"""

from __future__ import annotations

DEFAULT_FLOOR_MS = 350


def resume_delay(interval_ms: int, elapsed_ms: float, floor_ms: int = DEFAULT_FLOOR_MS) -> int:
    """朗读结束后距离下一次截图还要等多少毫秒（恒为正）。"""
    interval = max(0, int(interval_ms))
    floor = max(0, int(floor_ms))
    remaining = interval - max(0.0, float(elapsed_ms))
    return max(floor, int(round(remaining)))
