"""测试：模型识别。"""

from __future__ import annotations

import unittest

from cursorqa.models import EMPTY, OK, TEXT_ONLY, UNKNOWN, check_model, preset_models


class CheckModelTest(unittest.TestCase):
    def test_empty_is_blocking(self) -> None:
        verdict = check_model("   ")
        self.assertEqual(verdict.level, EMPTY)
        self.assertTrue(verdict.is_blocking)

    def test_text_only_deepseek(self) -> None:
        for name in ("deepseek-chat", "deepseek-reasoner", "deepseek-coder"):
            with self.subTest(name=name):
                verdict = check_model(name)
                self.assertEqual(verdict.level, TEXT_ONLY)
                self.assertIn("多模态", verdict.message)

    def test_text_only_other_vendors(self) -> None:
        for name in ("gpt-3.5-turbo", "qwen-max", "llama-3-70b", "glm-4-flash"):
            with self.subTest(name=name):
                self.assertEqual(check_model(name).level, TEXT_ONLY)

    def test_multimodal_detected(self) -> None:
        for name in ("deepseek-flash", "gpt-4o", "gpt-4o-mini", "qwen-vl-max", "glm-4v", "gpt-4.1"):
            with self.subTest(name=name):
                self.assertEqual(check_model(name).level, OK)

    def test_case_insensitive(self) -> None:
        self.assertEqual(check_model("DEEPSEEK-CHAT").level, TEXT_ONLY)
        self.assertEqual(check_model("GPT-4O").level, OK)

    def test_unknown_model(self) -> None:
        verdict = check_model("company-internal-7b")
        self.assertEqual(verdict.level, UNKNOWN)
        self.assertFalse(verdict.is_blocking)
        self.assertIn("测试连接", verdict.message)

    def test_ocr_mode_accepts_text_model(self) -> None:
        verdict = check_model("deepseek-chat", require_vision=False)
        self.assertEqual(verdict.level, OK)
        self.assertIn("OCR", verdict.message)

    def test_ocr_mode_still_needs_a_name(self) -> None:
        self.assertEqual(check_model("", require_vision=False).level, EMPTY)

    def test_presets_are_consistent(self) -> None:
        models = preset_models()
        self.assertIn("deepseek-flash", models)
        self.assertEqual(len(models), len(set(models)))
        for model in models:
            with self.subTest(model=model):
                self.assertNotEqual(check_model(model).level, EMPTY)


if __name__ == "__main__":
    unittest.main()
