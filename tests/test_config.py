"""测试：配置读写。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cursorqa import config
from cursorqa.config import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    INTERVAL_DEFAULT,
    Settings,
    api_key_from_env,
    default_config_path,
    from_mapping,
    load,
    parse_interval,
    save,
)


class ParseIntervalTest(unittest.TestCase):
    def test_valid(self) -> None:
        self.assertEqual(parse_interval("5"), 5)
        self.assertEqual(parse_interval(" 3600 "), 3600)

    def test_invalid(self) -> None:
        for text in ("", "  ", "0", "3601", "abc", "-3", "1.5"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_interval(text)


class SettingsTest(unittest.TestCase):
    def test_with_defaults_fixes_bad_values(self) -> None:
        settings = Settings(interval=0, base_url="  ", model="", region="huge").with_defaults()
        self.assertEqual(settings.interval, INTERVAL_DEFAULT)
        self.assertEqual(settings.base_url, DEFAULT_BASE_URL)
        self.assertEqual(settings.model, DEFAULT_MODEL)
        self.assertEqual(settings.region, "standard")

    def test_new_fields_default_and_clamp(self) -> None:
        settings = Settings(speak_rate=99, hide_window=True, ocr_mode=True).with_defaults()
        self.assertEqual(settings.speak_rate, 10)
        self.assertTrue(settings.hide_window)
        self.assertTrue(settings.ocr_mode)
        self.assertEqual(Settings(speak_rate=-99).with_defaults().speak_rate, -10)

    def test_region_size(self) -> None:
        self.assertEqual(Settings(region="wide").region_size(), (760, 480))

    def test_for_disk_drops_key_by_default(self) -> None:
        settings = Settings(api_key="sk-secret")
        self.assertEqual(settings.for_disk().api_key, "")
        kept = Settings(api_key="sk-secret", remember_key=True)
        self.assertEqual(kept.for_disk().api_key, "sk-secret")


class MappingTest(unittest.TestCase):
    def test_tolerates_junk(self) -> None:
        settings = from_mapping(
            {
                "interval": "15",
                "model": 42,
                "region": "nope",
                "speak": "no",
                "speak_rate": "-4",
                "hide_window": "on",
                "ocr_mode": "true",
                "remember_key": "yes",
                "api_key": None,
                "unknown": object(),
            }
        )
        self.assertEqual(settings.interval, 15)
        self.assertEqual(settings.model, DEFAULT_MODEL)
        self.assertEqual(settings.region, "standard")
        self.assertFalse(settings.speak)
        self.assertEqual(settings.speak_rate, -4)
        self.assertTrue(settings.hide_window)
        self.assertTrue(settings.ocr_mode)
        self.assertTrue(settings.remember_key)
        self.assertEqual(settings.api_key, "")


class PersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "nested" / "config.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_round_trip(self) -> None:
        original = Settings(
            interval=7,
            model="gpt-4o",
            api_key="sk-abc",
            remember_key=True,
            speak_rate=-3,
            hide_window=True,
            ocr_mode=True,
        )
        save(original, self.path)
        self.assertTrue(self.path.is_file())
        loaded = load(self.path)
        self.assertEqual(loaded.interval, 7)
        self.assertEqual(loaded.model, "gpt-4o")
        self.assertEqual(loaded.api_key, "sk-abc")
        self.assertEqual(loaded.speak_rate, -3)
        self.assertTrue(loaded.hide_window)
        self.assertTrue(loaded.ocr_mode)

    def test_key_not_written_when_not_remembered(self) -> None:
        save(Settings(api_key="sk-abc", remember_key=False), self.path)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["api_key"], "")

    def test_corrupted_file_falls_back(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(load(self.path).model, DEFAULT_MODEL)

    def test_missing_file_falls_back(self) -> None:
        self.assertEqual(load(self.path).interval, INTERVAL_DEFAULT)

    def test_json_is_not_dict(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text("[1, 2, 3]", encoding="utf-8")
        self.assertEqual(load(self.path).base_url, DEFAULT_BASE_URL)


class EnvKeyTest(unittest.TestCase):
    def test_priority(self) -> None:
        env = {"OPENAI_API_KEY": "sk-openai", "DEEPSEEK_API_KEY": "sk-deepseek"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(api_key_from_env(), "sk-deepseek")

    def test_cursor_qa_wins(self) -> None:
        env = {"CURSOR_QA_API_KEY": "sk-own", "DEEPSEEK_API_KEY": "sk-deepseek"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(api_key_from_env(), "sk-own")

    def test_no_env(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(api_key_from_env(), "")


class ConfigPathTest(unittest.TestCase):
    def test_appdata_used_on_windows(self) -> None:
        with mock.patch.dict(os.environ, {"APPDATA": r"C:\Users\tester\AppData\Roaming"}, clear=True):
            path = default_config_path()
        self.assertIn("cursor-qa", str(path))

    def test_constant_module_is_importable(self) -> None:
        self.assertEqual(config.APP_NAME, "cursor-qa")


if __name__ == "__main__":
    unittest.main()
