"""测试：朗读期间暂停截图，读完再排下一题（用假的语音进程驱动状态机）。"""

from __future__ import annotations

import tempfile
import tkinter as tk
import unittest
from pathlib import Path

from cursorqa.config import Settings

try:
    from cursorqa import ui as ui_module
except Exception as error:  # noqa: BLE001 - 没有 tkinter 的环境直接跳过
    ui_module = None
    IMPORT_ERROR = str(error)
else:
    IMPORT_ERROR = ""


class FakeSpeaker:
    """模拟 SAPI 子进程：busy/poll 由测试控制。"""

    def __init__(self) -> None:
        self.rate = 0
        self.running = True
        self.spoken: list[str] = []
        self.stopped = False

    def busy(self) -> bool:
        return self.running

    def speak(self, text: str) -> None:
        self.spoken.append(text)
        self.running = True

    def poll(self) -> int | None:
        return None if self.running else 0

    def last_error(self) -> str:
        return ""

    def stop(self) -> None:
        self.stopped = True
        self.running = False


@unittest.skipIf(ui_module is None, f"没有可用的 tkinter：{IMPORT_ERROR}")
class SpeechGateTest(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.tmp = tempfile.TemporaryDirectory()
            self.app = ui_module.App(
                Settings(interval=10, speak=True, api_key="sk-test", model="deepseek-flash"),
                config_path=Path(self.tmp.name) / "config.json",
            )
        except tk.TclError as error:
            raise unittest.SkipTest(f"无法创建窗口：{error}") from None
        self.app._running = True
        self.app._session = Settings(interval=10, speak=True, api_key="sk-test")
        self.speaker = FakeSpeaker()
        self.app._speaker = self.speaker
        self.app._last_capture_at = ui_module.time.monotonic() - 2.0

    def tearDown(self) -> None:
        with self.app_suppress():
            self.app._stop("测试结束")
            self.app.destroy()
        self.tmp.cleanup()

    def app_suppress(self):
        import contextlib

        return contextlib.suppress(tk.TclError, AttributeError)

    def _answer(self, text: str = "答案：A。因为……") -> None:
        self.app._handle(("answer", self.app._generation, text, 1.2, False))

    def test_answer_holds_next_capture(self) -> None:
        self._answer()
        self.assertEqual(self.speaker.spoken, ["答案：A。因为……"])
        self.assertTrue(self.app._waiting_for_speech)
        self.assertIsNone(self.app._after_id, "朗读期间不应排下一次截图")

    def test_resume_after_speech_ends(self) -> None:
        self._answer()
        self.speaker.running = False
        self.app._check_speech()
        self.assertFalse(self.app._waiting_for_speech)
        self.assertIsNotNone(self.app._after_id, "读完后应重新排下一次截图")

    def test_no_speaker_resumes_immediately(self) -> None:
        self.app._speaker = None
        self._answer()
        self.assertFalse(self.app._waiting_for_speech)
        self.assertIn("答案：A", self.app.answer_text.get("1.0", "end"))

    def test_stop_clears_waiting(self) -> None:
        self._answer()
        self.app._stop("已停止")
        self.assertFalse(self.app._waiting_for_speech)
        self.assertIsNone(self.app._after_id)

    def test_stuck_speech_is_forced_to_resume(self) -> None:
        from unittest import mock

        self._answer()
        self.app._speech_since = ui_module.time.monotonic() - 999
        with mock.patch.object(ui_module, "SPEECH_HOLD_LIMIT", 1.0):
            self.app._check_speech()
        self.assertFalse(self.app._waiting_for_speech)
        self.assertTrue(self.speaker.stopped, "卡住的朗读应被强制停止")
        self.assertIsNotNone(self.app._after_id)

    def test_skip_answer_does_not_hold(self) -> None:
        self.app._handle(("answer", self.app._generation, "SKIP", 0.5, False))
        self.assertFalse(self.app._waiting_for_speech)
        self.assertEqual(self.speaker.spoken, [])


if __name__ == "__main__":
    unittest.main()
