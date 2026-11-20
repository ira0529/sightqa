"""两阶段作答：先发光标小块，题干被截断再补发整屏。

流程（多模态模式）::

    小块 + 提示词 ──► 答案        （一般情况下到这里就结束）
                └──► FULL  ──► 小块 + 整屏 + 补全提示词 ──► 答案

OCR 模式同理：先识别小块文字；模型回 FULL 就识别整屏、按光标所在行重新提问。

放在这里是为了能脱离 Tkinter 单独测试。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .capture import Capture
from .client import ANSWER_PROMPT_FULL, Reply, is_full_request
from .ocr import compose_prompt, recognize


class EmptyScene(RuntimeError):
    """截图里没有可识别/可发送的内容。"""


@dataclass(frozen=True)
class Answer:
    text: str
    latency: float
    escalated: bool = False
    steps: int = 1


def _ocr_prompt(image, cursor: tuple[int, int]) -> str:
    result = recognize(image)
    if not result.lines:
        raise EmptyScene("截图里没有识别到文字")
    return compose_prompt(result, cursor)


def answer(
    client,
    capture: Capture,
    *,
    ocr_mode: bool = False,
    auto_full: bool = True,
    on_note: Callable[[str], None] | None = None,
) -> Answer:
    """跟随 ``client`` 完成一次作答，必要时自动补发整屏。"""

    def note(message: str) -> None:
        if on_note is not None:
            on_note(message)

    if ocr_mode:
        first = client.ask_text(_ocr_prompt(capture.image, capture.cursor))
        if not (auto_full and capture.has_full and is_full_request(first.text)):
            return Answer(first.text, first.latency, escalated=False, steps=1)

        note("题干似乎被截断，正在识别整屏补全…")
        second = client.ask_text(_ocr_prompt(capture.full_image(), capture.cursor_in_full()))
        return Answer(second.text, first.latency + second.latency, escalated=True, steps=2)

    first: Reply = client.ask(capture.png_base64)
    if not (auto_full and capture.has_full and is_full_request(first.text)):
        return Answer(first.text, first.latency, escalated=False, steps=1)

    note("题干似乎被截断，正在补发整屏…")
    second = client.ask_images(
        [capture.png_base64, capture.full_png_base64()],
        prompt=ANSWER_PROMPT_FULL,
    )
    return Answer(second.text, first.latency + second.latency, escalated=True, steps=2)


__all__ = ["Answer", "EmptyScene", "answer"]
