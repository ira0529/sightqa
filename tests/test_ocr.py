"""测试：图片转文字（OCR）里的纯逻辑，以及一次真实识别冒烟。"""

from __future__ import annotations

import unittest

from cursorqa.ocr import (
    OCR_PROMPT,
    OcrLine,
    OcrResult,
    _parse,
    compose_prompt,
    nearest_line,
    normalize_text,
    ocr_support,
    recognize,
)


class NormalizeTest(unittest.TestCase):
    def test_joins_cjk_spaces(self) -> None:
        self.assertEqual(normalize_text("物 块 下 滑"), "物块下滑")

    def test_keeps_latin_words(self) -> None:
        self.assertEqual(normalize_text("速度 2 m/s 增至"), "速度2 m/s增至")

    def test_fullwidth_punctuation(self) -> None:
        self.assertEqual(normalize_text("答案 ： B 。"), "答案：B。")

    def test_empty(self) -> None:
        self.assertEqual(normalize_text("   "), "")
        self.assertEqual(normalize_text(""), "")


def _lines() -> tuple[OcrLine, ...]:
    return (
        OcrLine("题干第一行", left=10, top=10, width=300, height=20),
        OcrLine("题干第二行", left=10, top=40, width=280, height=20),
        OcrLine("A. 1  B. 2  C. 3", left=10, top=80, width=240, height=20),
    )


class NearestLineTest(unittest.TestCase):
    def test_inside_line(self) -> None:
        self.assertEqual(nearest_line(_lines(), (100, 50)), 1)

    def test_below_all_lines(self) -> None:
        self.assertEqual(nearest_line(_lines(), (100, 200)), 2)

    def test_above_all_lines(self) -> None:
        self.assertEqual(nearest_line(_lines(), (100, 0)), 0)

    def test_no_lines(self) -> None:
        self.assertIsNone(nearest_line((), (0, 0)))


class ComposePromptTest(unittest.TestCase):
    def test_marks_nearest_line(self) -> None:
        prompt = compose_prompt(OcrResult(_lines()), cursor=(50, 45))
        body = prompt.split("OCR 文字开始")[1]
        self.assertIn("▶ 题干第二行", body)
        self.assertEqual(body.count("▶"), 1)
        self.assertIn("OCR 文字结束", prompt)

    def test_falls_back_to_first_line(self) -> None:
        prompt = compose_prompt(OcrResult(_lines()))
        self.assertIn("▶ 题干第一行", prompt)

    def test_empty_result_raises(self) -> None:
        with self.assertRaises(ValueError):
            compose_prompt(OcrResult())

    def test_prompt_mentions_skip_rule(self) -> None:
        self.assertIn("SKIP", OCR_PROMPT)


class ParseTest(unittest.TestCase):
    def test_sorts_and_skips_empty(self) -> None:
        data = {
            "lang": "zh-Hans-CN",
            "lines": [
                {"t": "第二行", "x": 0, "y": 50, "w": 100, "h": 10},
                {"t": "   ", "x": 0, "y": 10, "w": 10, "h": 10},
                {"t": "第一行", "x": 0, "y": 10, "w": 100, "h": 10},
            ],
        }
        result = _parse(data)
        self.assertEqual([line.text for line in result.lines], ["第一行", "第二行"])
        self.assertEqual(result.language, "zh-Hans-CN")
        self.assertEqual(result.text, "第一行\n第二行")

    def test_missing_fields_are_tolerated(self) -> None:
        result = _parse({"lines": [{"t": "只有文字"}]})
        self.assertEqual(result.lines[0].left, 0)
        self.assertEqual(result.lines[0].width, 0)


class SupportTest(unittest.TestCase):
    def test_support_is_cached_tuple(self) -> None:
        available, detail = ocr_support()
        self.assertIsInstance(available, bool)
        self.assertIsInstance(detail, str)
        self.assertEqual((available, detail), ocr_support())


class RealOcrSmokeTest(unittest.TestCase):
    """真实调用系统 OCR；没有桌面/语言包时自动跳过。"""

    @classmethod
    def setUpClass(cls) -> None:
        available, detail = ocr_support()
        if not available:
            raise unittest.SkipTest(f"系统 OCR 不可用：{detail}")
        from PIL import Image, ImageDraw, ImageFont

        image = Image.new("RGB", (520, 160), "white")
        draw = ImageDraw.Draw(image)
        try:
            font = ImageFont.truetype("arial.ttf", 32)
        except OSError:
            font = ImageFont.load_default()
        draw.text((20, 30), "HELLO OCR 12345", fill="black", font=font)
        draw.text((20, 90), "SECOND LINE abc", fill="black", font=font)
        cls.image = image

    def test_recognize_returns_lines(self) -> None:
        result = recognize(self.image)
        text = result.text.upper()
        self.assertIn("HELLO", text)
        self.assertGreaterEqual(len(result.lines), 2)
        self.assertTrue(result.language)


if __name__ == "__main__":
    unittest.main()
