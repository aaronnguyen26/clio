"""Empirical Verification & Stress Harness for M1 Actuators (Fix 2).

Written by Challenger M1 Fix 2 (empirical_challenger).
Stress-tests MacOSActuator, MockActuator, FailsafeWatchdog, and ActuatorFactory.
"""

from __future__ import annotations

import concurrent.futures
import math
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from src.actuators.base import BaseActuator
from src.actuators.factory import ActuatorFactory, get_actuator
from src.actuators.failsafe import FailsafeWatchdog
from src.actuators.mock import MockActuator
from src.actuators.types import (
    ActuatorError,
    ActuatorMode,
    ApplicationLaunchError,
    CornerLocation,
    FailsafeEmergencyStop,
    InputSynthesisError,
    MouseButton,
    WindowInfo,
    is_screen_corner,
    normalize_mouse_button,
)

if sys.platform == "darwin":
    from src.actuators.macos import (
        MODIFIER_MASKS,
        VIRTUAL_KEYCODES,
        MacOSActuator,
        MacOSNativeBindings,
        kCGEventFlagMaskAlternate,
        kCGEventFlagMaskCommand,
        kCGEventFlagMaskControl,
        kCGEventFlagMaskShift,
        kCGEventLeftMouseDown,
        kCGEventLeftMouseDragged,
        kCGEventLeftMouseUp,
        kCGEventMouseMoved,
        kCGEventOtherMouseDown,
        kCGEventOtherMouseUp,
        kCGEventRightMouseDown,
        kCGEventRightMouseUp,
        kCGMouseButtonCenter,
        kCGMouseButtonLeft,
        kCGMouseButtonRight,
    )


class TestMockActuatorEmpiricalHarness(unittest.TestCase):
    """Stress tests and boundary checks for MockActuator."""

    def setUp(self) -> None:
        self.actuator = MockActuator(
            screen_size=(1920.0, 1080.0),
            initial_pos=(500.0, 500.0),
            failsafe_margin=5.0,
            enforce_corner_failsafe=True,
        )

    def tearDown(self) -> None:
        self.actuator.stop()

    def test_repeated_stop_idempotence(self) -> None:
        """Calling stop() 100 times must be completely safe and idempotent."""
        for _ in range(100):
            self.actuator.stop()
            self.assertEqual(len(self.actuator._active_modifiers), 0)

    def test_mouse_move_boundaries_and_failsafe(self) -> None:
        """Moving into all 4 corners must trigger failsafe when enforce_corner_failsafe is True."""
        corners = [
            (0.0, 0.0),       # top-left
            (1920.0, 0.0),    # top-right
            (0.0, 1080.0),    # bottom-left
            (1920.0, 1080.0), # bottom-right
        ]
        for cx, cy in corners:
            act = MockActuator(screen_size=(1920.0, 1080.0), initial_pos=(500.0, 500.0), enforce_corner_failsafe=True)
            act.move_mouse(cx, cy)
            self.assertTrue(act.is_failsafe_triggered())
            with self.assertRaises(FailsafeEmergencyStop):
                act.check_failsafe()
            # Action following failsafe must immediately raise FailsafeEmergencyStop
            with self.assertRaises(FailsafeEmergencyStop):
                act.click()
            act.stop()

    def test_drag_and_scroll_stress(self) -> None:
        """Stress test multiple drags and scroll operations."""
        for i in range(20):
            self.actuator.drag(100.0 + i, 100.0 + i, 200.0 + i, 200.0 + i, duration=0.01)
            self.assertEqual(self.actuator.get_mouse_position(), (200.0 + i, 200.0 + i))
            self.actuator.scroll(dx=i, dy=-i)

        drags = self.actuator.get_history("drag")
        scrolls = self.actuator.get_history("scroll")
        self.assertEqual(len(drags), 20)
        self.assertEqual(len(scrolls), 20)

    def test_text_typing_and_pasteboard_stress(self) -> None:
        """Verify handling of complex Unicode strings, newlines, and emojis in Mock."""
        complex_text = "Weekly Review 🚀\n- [ ] Task #1: 100% 🎯\n- [ ] Task #2: 日本語 テスト\n- [ ] Task #3: 'quotes' & \"double\" <xml>"
        self.actuator.type_text(complex_text)
        self.assertEqual(self.actuator.typed_text, complex_text)
        self.actuator.assert_text_typed("Weekly Review 🚀")
        self.actuator.assert_text_typed("日本語 テスト")

        self.actuator.paste_text("PASTE_BUFFER_CONTENT")
        self.assertEqual(self.actuator.clipboard, "PASTE_BUFFER_CONTENT")
        self.actuator.assert_text_pasted("PASTE_BUFFER_CONTENT", exact=True)
        # Verify paste triggered Cmd+V
        self.actuator.assert_hotkey_pressed("cmd", "v", count=1)

    def test_hotkey_isolation_no_mouse_actions(self) -> None:
        """Verify that pressing hotkeys produces ZERO mouse action events in history."""
        self.actuator.press_hotkey("cmd", "n")
        self.actuator.press_hotkey("shift", "cmd", "s")
        self.actuator.press_hotkey("alt", "tab")
        self.actuator.press_hotkey("ctrl", "c")

        mouse_actions = [a for a in self.actuator.action_history if a.action_type in ("click", "move_mouse", "drag", "scroll")]
        self.assertEqual(len(mouse_actions), 0, f"Expected 0 mouse actions during hotkeys, got: {mouse_actions}")
        self.assertEqual(len(self.actuator._active_modifiers), 0, "Modifiers should be cleared after hotkeys")

    def test_window_geometry_ratio_queries(self) -> None:
        """Verify ratio-to-screen projection with varied geometry."""
        win = WindowInfo(
            window_id=99,
            owner_name="TestApp",
            title="Main",
            x=200.0,
            y=150.0,
            width=1000.0,
            height=800.0,
        )
        self.actuator.add_window(win)
        # Top-left corner ratio
        self.actuator.click_window_ratio(win, 0.0, 0.0)
        self.assertEqual(self.actuator.get_mouse_position(), (200.0, 150.0))
        # Center ratio
        self.actuator.click_window_ratio(win, 0.5, 0.5)
        self.assertEqual(self.actuator.get_mouse_position(), (700.0, 550.0))
        # Bottom-right corner ratio
        self.actuator.click_window_ratio(win, 1.0, 1.0)
        self.assertEqual(self.actuator.get_mouse_position(), (1200.0, 950.0))


@unittest.skipUnless(sys.platform == "darwin", "MacOSActuator requires macOS Darwin")
class TestMacOSActuatorEmpiricalHarness(unittest.TestCase):
    """Deep empirical verification and stress testing of MacOSActuator."""

    def setUp(self) -> None:
        self._orig_init = MacOSActuator.__init__

        def safe_init(act_self, *args, **kwargs):
            self._orig_init(act_self, *args, **kwargs)
            act_self.native.cg.CGEventPost = lambda *a: None

        self._patcher = patch.object(MacOSActuator, "__init__", safe_init)
        self._patcher.start()

    def tearDown(self) -> None:
        self._patcher.stop()

    def test_repeated_stop_stress_single_instance(self) -> None:
        """Stress-test calling stop() 500 times consecutively on a single instance."""
        act = MacOSActuator(enable_watchdog=False)
        self.assertFalse(act._stopped)
        for _ in range(500):
            act.stop()
            self.assertTrue(act._stopped)
            self.assertIsNone(act._kCGWindowNumber)
            self.assertIsNone(act._kCGWindowOwnerName)

    def test_repeated_create_and_stop_cycles(self) -> None:
        """Stress-test allocating and tearing down 50 distinct MacOSActuator instances."""
        for i in range(50):
            act = MacOSActuator(enable_watchdog=False)
            # Call query before stop
            w, h = act.get_screen_size()
            self.assertGreater(w, 0.0)
            act.stop()
            # Calling get_windows() after stop must return [] safely without segfaulting
            wins_post_stop = act.get_windows()
            self.assertEqual(wins_post_stop, [])
            act.stop()  # Second stop

    def test_concurrent_stop_calls(self) -> None:
        """Calling stop() concurrently from 10 threads must not crash or cause race conditions."""
        act = MacOSActuator(enable_watchdog=False)
        errors = []

        def worker():
            try:
                for _ in range(50):
                    act.stop()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Concurrent stop errors: {errors}")
        self.assertTrue(act._stopped)

    def test_press_hotkey_strictly_zero_mouse_events_stress(self) -> None:
        """Verify that press_hotkey emits exactly ZERO mouse events across diverse hotkey combos."""
        act = MacOSActuator(enable_watchdog=False)
        created_mouse_events = []
        posted_mouse_events = []

        orig_create_mouse = act.native.cg.CGEventCreateMouseEvent
        orig_post = act.native.cg.CGEventPost

        # Stub CGEventPost so tests do not send live key events to the user's macOS session
        act.native.cg.CGEventPost = lambda *args: None

        def spy_create_mouse(source, mouse_type, point, mouse_button):
            created_mouse_events.append((mouse_type, point.x, point.y, mouse_button))
            return orig_create_mouse(source, mouse_type, point, mouse_button)

        act.native.cg.CGEventCreateMouseEvent = spy_create_mouse

        hotkey_list = [
            ("cmd", "n"),
            ("cmd", "v"),
            ("cmd", "c"),
            ("cmd", "shift", "4"),
            ("alt", "space"),
            ("ctrl", "a"),
            ("cmd", "return"),
            ("shift", "tab"),
            ("cmd", "z"),
            ("cmd", "shift", "z"),
        ]

        try:
            with patch.object(act, "is_failsafe_triggered", return_value=False):
                for keys in hotkey_list:
                    act.press_hotkey(*keys)
        finally:
            act.native.cg.CGEventCreateMouseEvent = orig_create_mouse
            act.native.cg.CGEventPost = orig_post
            act.stop()

        self.assertEqual(
            len(created_mouse_events),
            0,
            f"Expected 0 mouse events during hotkeys, but found {len(created_mouse_events)}: {created_mouse_events}",
        )

    def test_mouse_event_creation_fidelity(self) -> None:
        """Verify mouse action primitives create expected CoreGraphics event types and release events."""
        act = MacOSActuator(enable_watchdog=False)
        created_events = []
        released_events = []

        orig_create_mouse = act.native.cg.CGEventCreateMouseEvent
        orig_release = act.native.cf.CFRelease

        def spy_create_mouse(source, mouse_type, point, mouse_button):
            ev = orig_create_mouse(source, mouse_type, point, mouse_button)
            created_events.append((ev, mouse_type, point.x, point.y, mouse_button))
            return ev

        def spy_release(cf_ref):
            released_events.append(cf_ref)
            orig_release(cf_ref)

        act.native.cg.CGEventCreateMouseEvent = spy_create_mouse
        act.native.cf.CFRelease = spy_release

        try:
            with patch.object(act, "is_failsafe_triggered", return_value=False):
                # 1. move_mouse
                act.move_mouse(300.0, 400.0)
                # Verify last event was kCGEventMouseMoved
                self.assertGreater(len(created_events), 0)
                self.assertEqual(created_events[-1][1], kCGEventMouseMoved)
                self.assertEqual(created_events[-1][2], 300.0)
                self.assertEqual(created_events[-1][3], 400.0)

                # 2. click (left)
                pre_click_len = len(created_events)
                act.click(button=MouseButton.LEFT, click_count=1)
                click_evs = created_events[pre_click_len:]
                self.assertEqual(len(click_evs), 2)  # Down, Up
                self.assertEqual(click_evs[0][1], kCGEventLeftMouseDown)
                self.assertEqual(click_evs[1][1], kCGEventLeftMouseUp)

                # 3. right click
                pre_rc_len = len(created_events)
                act.click(button=MouseButton.RIGHT, click_count=1)
                rc_evs = created_events[pre_rc_len:]
                self.assertEqual(len(rc_evs), 2)
                self.assertEqual(rc_evs[0][1], kCGEventRightMouseDown)
                self.assertEqual(rc_evs[1][1], kCGEventRightMouseUp)

                # 4. center click
                pre_cc_len = len(created_events)
                act.click(button=MouseButton.CENTER, click_count=1)
                cc_evs = created_events[pre_cc_len:]
                self.assertEqual(len(cc_evs), 2)
                self.assertEqual(cc_evs[0][1], kCGEventOtherMouseDown)
                self.assertEqual(cc_evs[1][1], kCGEventOtherMouseUp)

                # 5. double click
                pre_dc_len = len(created_events)
                act.click(button=MouseButton.LEFT, click_count=2)
                dc_evs = created_events[pre_dc_len:]
                self.assertEqual(len(dc_evs), 4)  # Down, Up, Down, Up

                # 6. drag
                pre_drag_len = len(created_events)
                act.drag(100.0, 100.0, 200.0, 200.0, duration=0.05)
                drag_evs = created_events[pre_drag_len:]
                # Should have MouseMoved (to start) + Down + multiple Dragged + Up
                self.assertEqual(drag_evs[0][1], kCGEventMouseMoved)
                self.assertEqual(drag_evs[1][1], kCGEventLeftMouseDown)
                self.assertEqual(drag_evs[-1][1], kCGEventLeftMouseUp)
                dragged_types = [e[1] for e in drag_evs[2:-1]]
                self.assertTrue(all(t == kCGEventLeftMouseDragged for t in dragged_types))
        finally:
            act.native.cg.CGEventCreateMouseEvent = orig_create_mouse
            act.native.cf.CFRelease = spy_release
            act.stop()

        # Verify that all created CGEventRef handles were properly released via CFRelease
        created_ptrs = set(e[0] for e in created_events if e[0])
        released_ptrs = set(released_events)
        unreleased = created_ptrs - released_ptrs
        self.assertEqual(len(unreleased), 0, f"Memory leak: {len(unreleased)} CGEvents were not CFReleased!")

    def test_scroll_event_creation_fidelity(self) -> None:
        """Verify scroll wheel synthesizes and frees CGEventCreateScrollWheelEvent2."""
        act = MacOSActuator(enable_watchdog=False)
        scroll_events = []
        orig_scroll = act.native.cg.CGEventCreateScrollWheelEvent2

        def spy_scroll(source, units, wheel_count, dy, dx, dz):
            ev = orig_scroll(source, units, wheel_count, dy, dx, dz)
            scroll_events.append((ev, dy, dx))
            return ev

        act.native.cg.CGEventCreateScrollWheelEvent2 = spy_scroll
        try:
            with patch.object(act, "is_failsafe_triggered", return_value=False):
                act.scroll(dx=10, dy=-25)
                self.assertEqual(len(scroll_events), 1)
                self.assertEqual(scroll_events[0][1], -25)  # dy
                self.assertEqual(scroll_events[0][2], 10)   # dx
        finally:
            act.native.cg.CGEventCreateScrollWheelEvent2 = orig_scroll
            act.stop()

    def test_type_text_unicode_and_control_keys(self) -> None:
        """Verify type_text creates Unicode string events for text and keycodes for return/tab."""
        act = MacOSActuator(enable_watchdog=False)
        unicode_calls = []
        keyboard_events = []

        orig_kb = act.native.cg.CGEventCreateKeyboardEvent
        orig_uni = act.native.cg.CGEventKeyboardSetUnicodeString

        def spy_kb(source, keycode, down):
            ev = orig_kb(source, keycode, down)
            keyboard_events.append((keycode, down))
            return ev

        def spy_uni(ev, length, chars):
            # Read UTF-16 code units
            char_list = [chars[i] for i in range(length)]
            unicode_calls.append((length, char_list))
            orig_uni(ev, length, chars)

        act.native.cg.CGEventCreateKeyboardEvent = spy_kb
        act.native.cg.CGEventKeyboardSetUnicodeString = spy_uni

        test_str = "A\n\t🚀"
        try:
            with patch.object(act, "is_failsafe_triggered", return_value=False):
                act.type_text(test_str, interval=0.0)
        finally:
            act.native.cg.CGEventCreateKeyboardEvent = orig_kb
            act.native.cg.CGEventKeyboardSetUnicodeString = orig_uni
            act.stop()

        # 'A' -> unicode
        # '\n' -> keycode 36
        # '\t' -> keycode 48
        # '🚀' -> surrogate pair unicode (length 2)
        keycodes_down = [k for k, d in keyboard_events if d]
        self.assertIn(36, keycodes_down, "Expected keycode 36 (return) for newline")
        self.assertIn(48, keycodes_down, "Expected keycode 48 (tab) for tab")
        self.assertGreater(len(unicode_calls), 0)
        # Check emoji surrogate pair length
        emoji_call = [c for c in unicode_calls if c[0] == 2]
        self.assertGreater(len(emoji_call), 0, "Emoji should have produced 2 UTF-16 code units")

    def test_pasteboard_injection_live(self) -> None:
        """Verify paste_text populates the real macOS pasteboard and triggers Cmd+V."""
        orig_clip = subprocess.run(["pbpaste"], capture_output=True, text=True, check=False).stdout
        act = MacOSActuator(enable_watchdog=False)
        test_payload = f"CHALLENGER_M1_FIX2_TEST_{time.time()}"

        hotkeys_pressed = []

        def spy_hotkey(*keys):
            hotkeys_pressed.append(keys)

        act.press_hotkey = spy_hotkey
        try:
            with patch.object(act, "is_failsafe_triggered", return_value=False):
                act.paste_text(test_payload)

            # Check that Cmd+V was triggered
            self.assertEqual(hotkeys_pressed, [("cmd", "v")])

            # Verify clipboard content via pbpaste command
            res = subprocess.run(["pbpaste"], capture_output=True, text=True, check=False)
            self.assertEqual(res.stdout, test_payload, "Pasteboard does not match injected text!")
        finally:
            act.stop()
            # Restore user's original clipboard content
            subprocess.run(["pbcopy"], input=orig_clip, text=True, check=False)

    def test_window_geometry_live_inspection(self) -> None:
        """Verify get_windows() returns valid WindowInfo list with correct properties."""
        act = MacOSActuator(enable_watchdog=False)
        try:
            windows = act.get_windows()
            self.assertIsInstance(windows, list)
            self.assertGreater(len(windows), 0, "Expected at least one window on screen")

            # Check structure of each returned WindowInfo
            for w in windows:
                self.assertIsInstance(w, WindowInfo)
                self.assertGreater(w.window_id, 0)
                self.assertGreater(w.width, 0.0)
                self.assertGreater(w.height, 0.0)
                self.assertTrue(math.isfinite(w.x))
                self.assertTrue(math.isfinite(w.y))
                self.assertIsInstance(w.owner_name, str)
                self.assertIsInstance(w.title, str)

            # Test filtering by known app or non-existent app
            non_existent = act.get_windows("non_existent_app_xyz_987654321")
            self.assertEqual(len(non_existent), 0)

            # Test filtering by frontmost app
            front_app = act.get_frontmost_app()
            if front_app:
                matched = act.get_windows(front_app)
                # May have windows or not, but call should succeed cleanly
                self.assertIsInstance(matched, list)
        finally:
            act.stop()

    def test_window_ordering_layer_zero_first(self) -> None:
        """Verify get_windows() prioritizes layer 0 windows over background/system layers."""
        act = MacOSActuator(enable_watchdog=False)
        try:
            windows = act.get_windows()
            if len(windows) > 1:
                layers = [w.layer for w in windows]
                # If both layer 0 and layer != 0 exist, layer 0 must appear before layer != 0
                has_zero = 0 in layers
                has_nonzero = any(l != 0 for l in layers)
                if has_zero and has_nonzero:
                    first_nonzero_idx = next(i for i, l in enumerate(layers) if l != 0)
                    last_zero_idx = max(i for i, l in enumerate(layers) if l == 0)
                    self.assertLess(
                        last_zero_idx,
                        first_nonzero_idx,
                        f"Layer 0 windows must precede non-zero layers! Layers: {layers}",
                    )
        finally:
            act.stop()

    def test_large_pasteboard_payload_stress(self) -> None:
        """Verify paste_text handles large multi-line payloads (50KB) with Unicode."""
        act = MacOSActuator(enable_watchdog=False)
        lines = [f"- [ ] Step {i:04d}: Line with Unicode 🚀 日本語 🎯 and token_{i}" for i in range(1000)]
        large_payload = "\n".join(lines)
        self.assertGreater(len(large_payload), 50000)

        with patch.object(act, "press_hotkey") as mock_hotkey, \
             patch.object(act, "is_failsafe_triggered", return_value=False):
            act.paste_text(large_payload)
            mock_hotkey.assert_called_once_with("cmd", "v")

        # Verify clipboard content
        res = subprocess.run(["pbpaste"], capture_output=True, text=True, check=False)
        self.assertEqual(res.stdout, large_payload)
        act.stop()

    def test_type_and_paste_empty_string(self) -> None:
        """Verify type_text and paste_text with empty string return immediately without side effects."""
        act = MacOSActuator(enable_watchdog=False)
        with patch.object(act, "press_hotkey") as mock_hotkey, \
             patch.object(act, "is_failsafe_triggered", return_value=False):
            act.type_text("")
            act.paste_text("")
            mock_hotkey.assert_not_called()
        act.stop()

    def test_hotkey_empty_or_modifiers_only(self) -> None:
        """Verify press_hotkey with no keys or modifier only returns cleanly."""
        act = MacOSActuator(enable_watchdog=False)
        with patch.object(act, "is_failsafe_triggered", return_value=False):
            # No keys
            act.press_hotkey()
            # Modifiers only (no primary key)
            act.press_hotkey("cmd", "shift")
        act.stop()

    def test_concurrent_window_queries(self) -> None:
        """Verify get_windows() called concurrently across 10 threads works safely."""
        act = MacOSActuator(enable_watchdog=False)
        errors = []

        def query_worker():
            try:
                for _ in range(5):
                    wins = act.get_windows()
                    if not isinstance(wins, list):
                        errors.append("Invalid return type")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=query_worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        act.stop()
        self.assertEqual(len(errors), 0, f"Concurrent window query errors: {errors}")


class TestFailsafeWatchdogEmpiricalHarness(unittest.TestCase):
    """Stress testing of FailsafeWatchdog concurrency, reset, and signal safety."""

    def test_watchdog_thread_start_stop_cycles(self) -> None:
        """Verify that starting and stopping the watchdog thread repeatedly does not leak threads."""
        for _ in range(20):
            wd = FailsafeWatchdog(frequency=100.0, trap_signals=False)
            wd.start()
            self.assertTrue(wd.is_running)
            wd.stop()
            self.assertFalse(wd.is_running)

    def test_watchdog_manual_trigger_and_reset(self) -> None:
        """Verify trigger, trigger_info persistence, and clean reset."""
        wd = FailsafeWatchdog(frequency=50.0, trap_signals=False)
        wd.trigger(position=(0.0, 0.0), corner="top_left", reason="Test trigger")
        self.assertTrue(wd.is_triggered)
        self.assertIsNotNone(wd.trigger_info)
        self.assertEqual(wd.trigger_info.corner, "top_left")

        # check_failsafe raises
        with self.assertRaises(FailsafeEmergencyStop):
            wd.check_failsafe()

        wd.reset()
        self.assertFalse(wd.is_triggered)
        self.assertIsNone(wd.trigger_info)


if __name__ == "__main__":
    unittest.main()
