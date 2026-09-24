"""Unit tests for MacOSCoreGraphicsActuator, C types, and keycode mappings.

Belongs to M1 test suite (tests/unit/test_actuators_macos.py).
Fully executable with standard library: python3 -m unittest discover.
"""

from __future__ import annotations

import ctypes
import math
import sys
import unittest
from unittest.mock import patch

from src.actuators.macos import (
    MODIFIER_MASKS,
    VIRTUAL_KEYCODES,
    CGPoint,
    CGRect,
    CGSize,
    MacOSActuator,
    MacOSNativeBindings,
    kCGEventFlagMaskAlternate,
    kCGEventFlagMaskCommand,
    kCGEventFlagMaskControl,
    kCGEventFlagMaskShift,
)
from src.actuators.types import (
    ActuatorError,
    InputSynthesisError,
    MouseButton,
    WindowInfo,
)


class TestMacOSStructuresAndMappings(unittest.TestCase):
    """Verifies low-level ctypes structures, 64-bit alignment, and keycode mappings."""

    def test_cgpoint_structure_layout(self) -> None:
        pt = CGPoint(100.5, 200.75)
        self.assertEqual(pt.x, 100.5)
        self.assertEqual(pt.y, 200.75)
        self.assertEqual(ctypes.sizeof(CGPoint), 16)  # 2 x double (8 bytes each)

    def test_cgsize_structure_layout(self) -> None:
        size = CGSize(1920.0, 1080.0)
        self.assertEqual(size.width, 1920.0)
        self.assertEqual(size.height, 1080.0)
        self.assertEqual(ctypes.sizeof(CGSize), 16)

    def test_cgrect_structure_layout(self) -> None:
        rect = CGRect(CGPoint(10.0, 20.0), CGSize(800.0, 600.0))
        self.assertEqual(rect.origin.x, 10.0)
        self.assertEqual(rect.origin.y, 20.0)
        self.assertEqual(rect.size.width, 800.0)
        self.assertEqual(rect.size.height, 600.0)
        self.assertEqual(ctypes.sizeof(CGRect), 32)

    def test_virtual_keycodes_presence(self) -> None:
        self.assertEqual(VIRTUAL_KEYCODES["return"], 36)
        self.assertEqual(VIRTUAL_KEYCODES["enter"], 36)
        self.assertEqual(VIRTUAL_KEYCODES["tab"], 48)
        self.assertEqual(VIRTUAL_KEYCODES["space"], 49)
        self.assertEqual(VIRTUAL_KEYCODES["delete"], 51)
        self.assertEqual(VIRTUAL_KEYCODES["escape"], 53)
        self.assertEqual(VIRTUAL_KEYCODES["cmd"], 55)
        self.assertEqual(VIRTUAL_KEYCODES["shift"], 56)
        self.assertEqual(VIRTUAL_KEYCODES["option"], 58)
        self.assertEqual(VIRTUAL_KEYCODES["control"], 59)
        self.assertEqual(VIRTUAL_KEYCODES["n"], 45)
        self.assertEqual(VIRTUAL_KEYCODES["v"], 9)
        self.assertEqual(VIRTUAL_KEYCODES["c"], 8)

    def test_modifier_masks_values(self) -> None:
        self.assertEqual(MODIFIER_MASKS["cmd"], kCGEventFlagMaskCommand)
        self.assertEqual(MODIFIER_MASKS["command"], kCGEventFlagMaskCommand)
        self.assertEqual(MODIFIER_MASKS["shift"], kCGEventFlagMaskShift)
        self.assertEqual(MODIFIER_MASKS["alt"], kCGEventFlagMaskAlternate)
        self.assertEqual(MODIFIER_MASKS["option"], kCGEventFlagMaskAlternate)
        self.assertEqual(MODIFIER_MASKS["ctrl"], kCGEventFlagMaskControl)
        self.assertEqual(MODIFIER_MASKS["control"], kCGEventFlagMaskControl)


@unittest.skipUnless(sys.platform == "darwin", "MacOSActuator requires macOS Darwin")
class TestMacOSActuatorLiveQueries(unittest.TestCase):
    """Verifies read-only queries and bindings against live macOS WindowServer."""

    @classmethod
    def setUpClass(cls) -> None:
        # Initialize without background watchdog thread to avoid interference during test assertions
        cls.actuator = MacOSActuator(enable_watchdog=False)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.actuator.stop()

    def test_native_bindings_loaded(self) -> None:
        self.assertIsNotNone(self.actuator.native.cg)
        self.assertIsNotNone(self.actuator.native.cf)
        self.assertIsNotNone(self.actuator.native.appkit)
        self.assertIsNotNone(self.actuator.native.objc)

    def test_get_screen_size_live(self) -> None:
        w, h = self.actuator.get_screen_size()
        self.assertGreater(w, 0.0)
        self.assertGreater(h, 0.0)

    def test_get_mouse_position_live(self) -> None:
        x, y = self.actuator.get_mouse_position()
        self.assertTrue(math.isfinite(x))
        self.assertTrue(math.isfinite(y))

    def test_get_frontmost_app_live(self) -> None:
        front = self.actuator.get_frontmost_app()
        self.assertIsInstance(front, str)
        self.assertGreater(len(front), 0)

    def test_get_windows_live(self) -> None:
        wins = self.actuator.get_windows()
        self.assertIsInstance(wins, list)
        self.assertGreater(len(wins), 0)
        # Verify first window is WindowInfo
        first = wins[0]
        self.assertIsInstance(first, WindowInfo)
        self.assertGreater(first.window_id, 0)
        self.assertGreater(first.width, 0.0)
        self.assertGreater(first.height, 0.0)

    def test_invalid_mouse_coordinates_raise_error(self) -> None:
        with self.assertRaises(InputSynthesisError):
            self.actuator.move_mouse(float("nan"), 100.0)

        with self.assertRaises(InputSynthesisError):
            self.actuator.move_mouse(100.0, float("inf"))

    def test_unsupported_hotkey_raises_error(self) -> None:
        with self.assertRaises(InputSynthesisError):
            self.actuator.press_hotkey("cmd", "totally_invalid_key_12345")

    def test_unsupported_mouse_button_raises_error(self) -> None:
        with self.assertRaises(InputSynthesisError):
            self.actuator.click(button="unsupported_button")  # type: ignore

    def test_release_all_synthetic_modifiers_runs_safely(self) -> None:
        # Should execute cleanly without throwing errors
        self.actuator._release_all_synthetic_modifiers()

    def test_context_manager_protocol(self) -> None:
        with MacOSActuator(enable_watchdog=False) as act:
            w, h = act.get_screen_size()
            self.assertGreater(w, 0.0)

    def test_press_hotkey_valid_combination(self) -> None:
        """Verifies press_hotkey resolves keycodes and synthesizes events without NameError."""
        with patch.object(self.actuator, "is_failsafe_triggered", return_value=False), \
             patch.object(self.actuator.native.cg, "CGEventPost"):
            self.actuator.press_hotkey("cmd", "n")
            self.actuator.press_hotkey("cmd", "shift", "3")

    def test_paste_text_execution(self) -> None:
        """Verifies paste_text copies to pasteboard and executes Cmd+V without NameError."""
        with patch.object(self.actuator, "is_failsafe_triggered", return_value=False), \
             patch.object(self.actuator.native.cg, "CGEventPost"):
            self.actuator.paste_text("benchmark_token_test_12345")

    def test_stop_idempotency_double_free_safe(self) -> None:
        """Verifies calling stop() multiple times is completely idempotent and does not segfault."""
        act = MacOSActuator(enable_watchdog=False)
        act.stop()
        self.assertTrue(act._stopped)
        self.assertIsNone(act._kCGWindowNumber)
        # Second and third stop must return cleanly without SIGSEGV / Trace trap
        act.stop()
        act.stop()

    def test_press_hotkey_emits_zero_mouse_events(self) -> None:
        """Verifies that press_hotkey only releases keyboard modifiers and posts NO mouse events."""
        mouse_events = []
        orig_fn = self.actuator.native.cg.CGEventCreateMouseEvent

        def spy_mouse_event(*args):
            mouse_events.append(args)
            return orig_fn(*args)

        self.actuator.native.cg.CGEventCreateMouseEvent = spy_mouse_event
        try:
            with patch.object(self.actuator, "is_failsafe_triggered", return_value=False), \
                 patch.object(self.actuator.native.cg, "CGEventPost"):
                self.actuator.press_hotkey("cmd", "v")
            self.assertEqual(
                len(mouse_events), 0, f"Expected 0 mouse events during hotkey, found {len(mouse_events)}"
            )
        finally:
            self.actuator.native.cg.CGEventCreateMouseEvent = orig_fn


if __name__ == "__main__":
    unittest.main()
