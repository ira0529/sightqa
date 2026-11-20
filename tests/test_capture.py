"""测试：截图取景框与朗读文本清洗。"""

from __future__ import annotations

import os
import unittest

from cursorqa.capture import (
    MAX_FULL_WIDTH,
    MIN_EDGE,
    Capture,
    capture_cursor,
    compute_box,
    frame_signature,
    signature_distance,
)
from cursorqa.speech import MAX_SPEECH_CHARS, prepare_for_speech


class ComputeBoxTest(unittest.TestCase):
    def test_centered(self) -> None:
        box = compute_box(500, 400, 1000, 800, 200, 150)
        self.assertEqual(box, (300, 250, 700, 550))

    def test_clamped_to_top_left(self) -> None:
        box = compute_box(10, 10, 1000, 800, 200, 150)
        self.assertEqual(box, (0, 0, 400, 300))

    def test_clamped_to_bottom_right(self) -> None:
        box = compute_box(995, 795, 1000, 800, 200, 150)
        self.assertEqual(box, (600, 500, 1000, 800))

    def test_never_exceeds_screen(self) -> None:
        for x, y in ((0, 0), (999, 799), (500, 400), (-50, -50), (5000, 5000)):
            with self.subTest(x=x, y=y):
                left, top, right, bottom = compute_box(x, y, 1000, 800, 200, 150)
                self.assertGreaterEqual(left, 0)
                self.assertGreaterEqual(top, 0)
                self.assertLessEqual(right, 1000)
                self.assertLessEqual(bottom, 800)
                self.assertLess(left, right)
                self.assertLess(top, bottom)

    def test_min_edge_on_tiny_screen(self) -> None:
        left, top, right, bottom = compute_box(1, 1, 20, 20, 200, 150)
        self.assertGreaterEqual(right - left, MIN_EDGE)
        self.assertGreaterEqual(bottom - top, MIN_EDGE)


def _gradient(width: int = 320, height: int = 200):
    from PIL import Image

    image = Image.new("RGB", (width, height))
    pixels = image.load()
    for y in range(height):
        for x in range(width):
            pixels[x, y] = ((x * 255) // width, (y * 255) // height, ((x + y) * 255) // (width + height))
    return image


class SignatureTest(unittest.TestCase):
    def test_identical_images(self) -> None:
        image = _gradient()
        self.assertEqual(signature_distance(frame_signature(image), frame_signature(image)), 0)

    def test_tiny_change_is_tolerated(self) -> None:
        from PIL import ImageDraw

        image = _gradient()
        tweaked = image.copy()
        ImageDraw.Draw(tweaked).rectangle([0, 196, 24, 200], fill="black")
        self.assertLessEqual(signature_distance(frame_signature(image), frame_signature(tweaked)), 4.0)

    def test_obvious_change_is_detected(self) -> None:
        from PIL import Image, ImageDraw

        image = _gradient()
        altered = image.copy()
        ImageDraw.Draw(altered).rectangle([0, 120, 320, 200], fill="black")
        self.assertGreater(signature_distance(frame_signature(image), frame_signature(altered)), 4.0)

        blank = Image.new("RGB", image.size, "white")
        self.assertGreater(signature_distance(frame_signature(image), frame_signature(blank)), 4.0)

    def test_mismatched_sizes(self) -> None:
        self.assertEqual(signature_distance((1, 2), (1,)), float("inf"))
        self.assertEqual(signature_distance((), ()), float("inf"))


class FullImageTest(unittest.TestCase):
    def _capture(self, with_full: bool = True) -> Capture:
        from PIL import Image

        return Capture(
            image=Image.new("RGB", (550, 350), "white"),
            screen_image=Image.new("RGB", (2000, 1000), "white") if with_full else None,
            cursor_screen=(1000, 500),
        )

    def test_full_is_downscaled(self) -> None:
        image = self._capture().full_image()
        self.assertEqual(image.width, MAX_FULL_WIDTH)
        self.assertEqual(image.height, int(1000 * MAX_FULL_WIDTH / 2000))

    def test_cursor_maps_into_full(self) -> None:
        capture = self._capture()
        x, y = capture.cursor_in_full()
        self.assertLess(abs(x - 700), 3)
        self.assertLess(abs(y - 350), 3)

    def test_full_png_is_png(self) -> None:
        self.assertTrue(self._capture().full_png_base64().startswith("iVBOR"))

    def test_missing_full_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            self._capture(with_full=False).full_image()

    def test_has_full_flag(self) -> None:
        self.assertTrue(self._capture().has_full)
        self.assertFalse(self._capture(with_full=False).has_full)


class CaptureDataclassTest(unittest.TestCase):
    def test_size_property(self) -> None:
        from PIL import Image

        capture = Capture(image=Image.new("RGB", (30, 20)))
        self.assertEqual(capture.size, (30, 20))


class PrepareSpeechTest(unittest.TestCase):
    def test_speaker_rate_is_clamped(self) -> None:
        from cursorqa.speech import Speaker

        self.assertEqual(Speaker(rate=99).rate, 10)
        self.assertEqual(Speaker(rate=-99).rate, -10)
        self.assertEqual(Speaker(rate=3).rate, 3)

    def test_strips_markdown(self) -> None:
        self.assertEqual(prepare_for_speech("**答案：B**"), "答案：B")
        self.assertEqual(prepare_for_speech("## 结论\n选 C"), "结论。选 C")

    def test_collapses_newlines(self) -> None:
        self.assertEqual(prepare_for_speech("第一行\n\n第二行"), "第一行。第二行")

    def test_bench_url_to_text(self) -> None:
        self.assertEqual(prepare_for_speech("见 [文档](https://x.test) 说明"), "见 文档 说明")

    def test_empty(self) -> None:
        self.assertEqual(prepare_for_speech("   "), "")

    def test_truncates(self) -> None:
        result = prepare_for_speech("啊" * (MAX_SPEECH_CHARS + 50))
        self.assertTrue(result.endswith("……"))
        self.assertLessEqual(len(result), MAX_SPEECH_CHARS + 2)


@unittest.skipUnless(os.name == "nt", "截图功能只支持 Windows")
class CaptureSmokeTest(unittest.TestCase):
    def test_capture_returns_png(self) -> None:
        try:
            capture = capture_cursor(60, 40)
        except RuntimeError as error:  # 锁屏 / 无桌面会话
            self.skipTest(str(error))
        self.assertTrue(capture.png_base64.startswith("iVBOR"))
        self.assertEqual(len(capture.digest), 32)
        self.assertGreater(capture.size[0], 0)
        left, top, right, bottom = capture.box
        self.assertLess(left, right)
        self.assertLess(top, bottom)


if __name__ == "__main__":
    unittest.main()
