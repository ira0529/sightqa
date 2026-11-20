"""测试：全局热键的常量与注册生命周期。"""

from __future__ import annotations

import os
import unittest

from cursorqa.hotkey import (
    DEFAULT_BINDINGS,
    MOD_NOREPEAT,
    TOGGLE_RUN,
    TOGGLE_WINDOW,
    HotkeyManager,
)


class BindingTest(unittest.TestCase):
    def test_ids_are_unique(self) -> None:
        ids = [binding.id for binding in DEFAULT_BINDINGS]
        self.assertEqual(len(ids), len(set(ids)))

    def test_names_are_readable(self) -> None:
        for binding in DEFAULT_BINDINGS:
            self.assertIn("Ctrl", binding.name)
            self.assertIn("Alt", binding.name)

    def test_no_repeat_set(self) -> None:
        for binding in DEFAULT_BINDINGS:
            self.assertTrue(binding.modifiers & MOD_NOREPEAT)

    def test_expected_shortcuts(self) -> None:
        self.assertEqual(TOGGLE_WINDOW.name, "Ctrl+Alt+Q")
        self.assertEqual(TOGGLE_RUN.name, "Ctrl+Alt+S")


@unittest.skipUnless(os.name == "nt", "全局热键只支持 Windows")
class ManagerTest(unittest.TestCase):
    def test_start_and_stop(self) -> None:
        triggered: list[int] = []
        manager = HotkeyManager(triggered.append)
        manager.start()
        try:
            self.assertIsInstance(manager.failures, tuple)
            self.assertIsInstance(manager.active, tuple)
        finally:
            manager.stop()

    def test_double_start_is_noop(self) -> None:
        manager = HotkeyManager(lambda _binding_id: None)
        manager.start()
        manager.start()
        manager.stop()


if __name__ == "__main__":
    unittest.main()
