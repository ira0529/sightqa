"""测试：两阶段作答（小块 → 必要时补发整屏）。"""

from __future__ import annotations

import unittest
from unittest import mock

from PIL import Image

from cursorqa import pipeline
from cursorqa.capture import Capture
from cursorqa.client import ANSWER_PROMPT_FULL, Reply, is_full_request, is_skip_reply
from cursorqa.ocr import OcrLine, OcrResult


class FakeClient:
    def __init__(self, *replies: str) -> None:
        self._replies = [Reply(text, 0.5, "https://example.invalid") for text in replies]
        self.calls: list[tuple] = []

    def ask(self, png: str, *, prompt: str | None = None) -> Reply:
        self.calls.append(("ask", 1, prompt))
        return self._replies.pop(0)

    def ask_images(self, pngs, *, prompt: str | None = None) -> Reply:
        self.calls.append(("ask_images", len(list(pngs)), prompt))
        return self._replies.pop(0)

    def ask_text(self, prompt: str) -> Reply:
        self.calls.append(("ask_text", prompt))
        return self._replies.pop(0)


def make_capture(with_full: bool = True, cursor: tuple[int, int] = (275, 175)) -> Capture:
    screen = Image.new("RGB", (2000, 1000), "white") if with_full else None
    return Capture(
        image=Image.new("RGB", (550, 350), "white"),
        png_base64="CROP",
        digest=b"digest",
        cursor=cursor,
        cursor_screen=(1000, 500),
        screen_image=screen,
    )


class ReplyKindTest(unittest.TestCase):
    def test_skip_detection(self) -> None:
        self.assertTrue(is_skip_reply("SKIP"))
        self.assertTrue(is_skip_reply(" skip \n"))
        self.assertFalse(is_skip_reply("答案：A"))

    def test_full_detection(self) -> None:
        for text in ("FULL", "full", "FULL。", "需要 FULL", "请给全屏"):
            with self.subTest(text=text):
                self.assertTrue(is_full_request(text))
        self.assertFalse(is_full_request("答案：B。因为动能变化 32 J。"))
        self.assertFalse(is_full_request(""))
        # 长回答里提到 full 不算（避免误判）
        self.assertFalse(is_full_request("答案：C。" + "很长的解释" * 20 + " full screen"))


class PipelineTest(unittest.TestCase):
    def test_single_request_when_answer_is_complete(self) -> None:
        client = FakeClient("答案：A。")
        result = pipeline.answer(client, make_capture())
        self.assertFalse(result.escalated)
        self.assertEqual(result.steps, 1)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0][0], "ask")

    def test_escalates_to_full_screen(self) -> None:
        client = FakeClient("FULL", "答案：B。")
        notes: list[str] = []
        result = pipeline.answer(client, make_capture(), on_note=notes.append)
        self.assertTrue(result.escalated)
        self.assertEqual(result.steps, 2)
        self.assertEqual(result.text, "答案：B。")
        self.assertEqual(client.calls[0][0], "ask")
        kind, count, prompt = client.calls[1]
        self.assertEqual(kind, "ask_images")
        self.assertEqual(count, 2, "补发时应同时带小块和整屏")
        self.assertEqual(prompt, ANSWER_PROMPT_FULL)
        self.assertTrue(notes, "补发时应该给界面一条提示")

    def test_no_escalation_when_disabled(self) -> None:
        client = FakeClient("FULL", "答案：B。")
        result = pipeline.answer(client, make_capture(), auto_full=False)
        self.assertFalse(result.escalated)
        self.assertEqual(len(client.calls), 1)

    def test_no_escalation_without_full_image(self) -> None:
        client = FakeClient("FULL")
        result = pipeline.answer(client, make_capture(with_full=False))
        self.assertFalse(result.escalated)
        self.assertEqual(result.text, "FULL")

    def test_ocr_mode_escalates_with_full_ocr(self) -> None:
        crop_result = OcrResult(lines=(OcrLine("7. 题干", 10, 10, 200, 20),), language="zh-Hans-CN")
        full_result = OcrResult(
            lines=(OcrLine("7. 完整题干", 10, 10, 400, 20), OcrLine("A. 1 B. 2", 10, 40, 200, 20)),
            language="zh-Hans-CN",
        )
        with mock.patch.object(pipeline, "recognize", side_effect=[crop_result, full_result]) as spy:
            client = FakeClient("FULL", "答案：C。")
            result = pipeline.answer(client, make_capture(), ocr_mode=True)
        self.assertTrue(result.escalated)
        self.assertEqual(spy.call_count, 2)
        self.assertEqual(client.calls[0][0], "ask_text")
        self.assertIn("▶", client.calls[1][1])
        self.assertIn("完整题干", client.calls[1][1])

    def test_ocr_mode_empty_scene(self) -> None:
        with mock.patch.object(pipeline, "recognize", return_value=OcrResult()):
            client = FakeClient()
            with self.assertRaises(pipeline.EmptyScene):
                pipeline.answer(client, make_capture(), ocr_mode=True)
        self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
