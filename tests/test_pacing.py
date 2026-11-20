"""测试：朗读结束后下一次截图的等待时间。"""

from __future__ import annotations

import unittest

from cursorqa.pacing import DEFAULT_FLOOR_MS, resume_delay


class ResumeDelayTest(unittest.TestCase):
    def test_reading_took_less_than_interval(self) -> None:
        # 间隔 10s，朗读用了 3s → 再等约 7s
        self.assertEqual(resume_delay(10_000, 3_000), 7_000)

    def test_reading_took_longer_than_interval(self) -> None:
        # 间隔 10s，朗读用了 14s → 只保底停顿
        self.assertEqual(resume_delay(10_000, 14_000), DEFAULT_FLOOR_MS)

    def test_nothing_started_yet(self) -> None:
        self.assertEqual(resume_delay(2_000, 0), 2_000)

    def test_negative_elapsed_is_clamped(self) -> None:
        self.assertEqual(resume_delay(1_000, -5_000), 1_000)

    def test_custom_floor(self) -> None:
        self.assertEqual(resume_delay(1_000, 5_000, floor_ms=1_200), 1_200)

    def test_zero_interval(self) -> None:
        self.assertEqual(resume_delay(0, 0), DEFAULT_FLOOR_MS)

    def test_result_is_int(self) -> None:
        self.assertIsInstance(resume_delay(3_000, 1_234.567), int)


if __name__ == "__main__":
    unittest.main()
