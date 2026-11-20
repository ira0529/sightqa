"""测试：命令行参数解析与合并逻辑（不启动界面、不发请求）。"""

from __future__ import annotations

import contextlib
import io
import unittest

from cursorqa.cli import build_parser, merge_settings, run_check
from cursorqa.config import Settings


class ParserTest(unittest.TestCase):
    def test_defaults(self) -> None:
        args = build_parser().parse_args([])
        self.assertIsNone(args.interval)
        self.assertIsNone(args.rate)
        self.assertFalse(args.ocr)
        self.assertFalse(args.hide)
        self.assertFalse(args.check)

    def test_version_exits(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()) as buffer, self.assertRaises(SystemExit) as ctx:
            build_parser().parse_args(["--version"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn("sightqa", buffer.getvalue())


class MergeSettingsTest(unittest.TestCase):
    def test_cli_overrides_everything(self) -> None:
        args = build_parser().parse_args(
            [
                "--interval",
                "3",
                "--model",
                "gpt-4o",
                "--base-url",
                "https://api.openai.com/v1",
                "--rate",
                "-4",
                "--ocr",
                "--hide",
                "--no-voice",
                "--api-key",
                "sk-cli",
            ]
        )
        settings = merge_settings(args, Settings())
        self.assertEqual(settings.interval, 3)
        self.assertEqual(settings.model, "gpt-4o")
        self.assertEqual(settings.base_url, "https://api.openai.com/v1")
        self.assertEqual(settings.speak_rate, -4)
        self.assertTrue(settings.ocr_mode)
        self.assertTrue(settings.hide_window)
        self.assertFalse(settings.speak)
        self.assertEqual(settings.api_key, "sk-cli")

    def test_empty_args_keep_config(self) -> None:
        base = Settings(interval=7, model="deepseek-chat", ocr_mode=True)
        settings = merge_settings(build_parser().parse_args([]), base)
        self.assertEqual(settings.interval, 7)
        self.assertEqual(settings.model, "deepseek-chat")
        self.assertTrue(settings.ocr_mode)

    def test_bad_interval(self) -> None:
        args = build_parser().parse_args(["--interval", "0"])
        with self.assertRaises(ValueError):
            merge_settings(args, Settings())

    def test_bad_rate(self) -> None:
        args = build_parser().parse_args(["--rate", "99"])
        with self.assertRaises(ValueError):
            merge_settings(args, Settings())


class RunCheckTest(unittest.TestCase):
    def test_missing_key_returns_2(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()) as buffer:
            code = run_check(Settings(api_key=""))
        self.assertEqual(code, 2)
        self.assertIn("API 密钥", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
