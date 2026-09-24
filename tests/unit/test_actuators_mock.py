"""Unit tests for MockActuator, BaseActuator interface, and ActuatorFactory.

Belongs to M1 test suite (tests/unit/test_actuators_mock.py).
Fully executable with standard library: python3 -m unittest discover.
"""

from __future__ import annotations

import os
import unittest
from typing import List

from src.actuators.base import BaseActuator
from src.actuators.factory import ActuatorFactory, get_actuator
from src.actuators.mock import MockActuator
from src.actuators.types import (
    ActuatorError,
    ActuatorMode,
    ApplicationLaunchError,
    FailsafeEmergencyStop,
    InputSynthesisError,
    MouseButton,
    WindowInfo,
)


class TestMockActuator(unittest.TestCase):
    """Verifies all functional capabilities of MockActuator."""

    def setUp(self) -> None:
        self.actuator = MockActuator(
            screen_size=(1920.0, 1080.0),
            initial_pos=(500.0, 500.0),
            failsafe_margin=5.0,
            enforce_corner_failsafe=True,
        )

    def tearDown(self) -> None:
        self.actuator.stop()

    def test_default_state_and_properties(self) -> None:
        self.assertEqual(self.actuator.get_screen_size(), (1920.0, 1080.0))
        self.assertEqual(self.actuator.get_mouse_position(), (500.0, 500.0))
        self.assertIn(self.actuator.get_frontmost_app(), ("", "com.apple.finder"))
        self.assertEqual(len(self.actuator.action_history), 0)
        self.assertFalse(self.actuator.is_failsafe_triggered())

    def test_launch_app_success_and_history(self) -> None:
        result = self.actuator.launch_app("com.apple.Notes", timeout=3.0)
        self.assertTrue(result)
        self.assertEqual(self.actuator.get_frontmost_app(), "com.apple.Notes")
        self.assertIn("com.apple.Notes", self.actuator.running_apps)

        # Verifies automatic window creation
        windows = self.actuator.get_windows("com.apple.Notes")
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0].owner_name, "com.apple.Notes")

        # Verifies action history
        self.actuator.assert_app_launched("com.apple.Notes", count=1)
        history = self.actuator.get_history("launch_app")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].parameters["app"], "com.apple.Notes")
        self.assertEqual(history[0].parameters["timeout"], 3.0)
        self.assertTrue(history[0].success)

    def test_launch_app_failure_configuration(self) -> None:
        self.actuator.configure_launch_failure("com.broken.App")
        with self.assertRaises(ApplicationLaunchError):
            self.actuator.launch_app("com.broken.App")

        history = self.actuator.get_history("launch_app")
        self.assertEqual(len(history), 1)
        self.assertFalse(history[0].success)
        self.assertIn("Configured launch failure", str(history[0].error))

    def test_focus_app(self) -> None:
        self.actuator.focus_app("Notes")
        self.assertEqual(self.actuator.get_frontmost_app(), "Notes")
        self.actuator.assert_app_focused("Notes", count=1)

    def test_move_mouse_and_coordinates(self) -> None:
        self.actuator.move_mouse(250.0, 350.0, smooth=True, duration=0.1)
        self.assertEqual(self.actuator.get_mouse_position(), (250.0, 350.0))

        history = self.actuator.get_history("move_mouse")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].parameters["x"], 250.0)
        self.assertEqual(history[0].parameters["y"], 350.0)
        self.assertTrue(history[0].parameters["smooth"])
        self.assertEqual(history[0].parameters["duration"], 0.1)

    def test_mouse_click_default_and_coordinates(self) -> None:
        # Click at current position
        self.actuator.click()
        self.actuator.assert_clicked_at(500.0, 500.0, tolerance=0.1, count=1)

        # Click at explicit coordinates
        self.actuator.click(x=150.0, y=250.0)
        self.assertEqual(self.actuator.get_mouse_position(), (150.0, 250.0))
        self.actuator.assert_clicked_at(150.0, 250.0, tolerance=0.1, count=1)

    def test_mouse_click_buttons_and_counts(self) -> None:
        self.actuator.click(button=MouseButton.RIGHT, click_count=1)
        self.actuator.click(button=MouseButton.CENTER, click_count=2)

        clicks = self.actuator.get_history("click")
        self.assertEqual(len(clicks), 2)
        self.assertEqual(clicks[0].parameters["button"], "right")
        self.assertEqual(clicks[0].parameters["click_count"], 1)
        self.assertEqual(clicks[1].parameters["button"], "center")
        self.assertEqual(clicks[1].parameters["click_count"], 2)

    def test_double_click_convenience(self) -> None:
        self.actuator.double_click(x=300.0, y=400.0)
        clicks = self.actuator.get_history("click")
        self.assertEqual(len(clicks), 1)
        self.assertEqual(clicks[0].parameters["click_count"], 2)
        self.assertEqual(clicks[0].parameters["x"], 300.0)
        self.assertEqual(clicks[0].parameters["y"], 400.0)

    def test_right_click_convenience(self) -> None:
        self.actuator.right_click(x=400.0, y=500.0)
        clicks = self.actuator.get_history("click")
        self.assertEqual(len(clicks), 1)
        self.assertEqual(clicks[0].parameters["button"], "right")
        self.assertEqual(clicks[0].parameters["x"], 400.0)

    def test_drag_and_scroll(self) -> None:
        self.actuator.drag(start_x=10.0, start_y=20.0, end_x=100.0, end_y=200.0, duration=0.5)
        self.assertEqual(self.actuator.get_mouse_position(), (100.0, 200.0))
        drags = self.actuator.get_history("drag")
        self.assertEqual(len(drags), 1)
        self.assertEqual(drags[0].parameters["start_x"], 10.0)
        self.assertEqual(drags[0].parameters["end_x"], 100.0)

        self.actuator.scroll(dx=5, dy=-10)
        scrolls = self.actuator.get_history("scroll")
        self.assertEqual(len(scrolls), 1)
        self.assertEqual(scrolls[0].parameters["dx"], 5)
        self.assertEqual(scrolls[0].parameters["dy"], -10)

    def test_type_text(self) -> None:
        self.actuator.type_text("Hello World! 🚀", interval=0.01)
        self.actuator.assert_text_typed("Hello")
        self.actuator.assert_text_typed("Hello World! 🚀", exact=True)
        types = self.actuator.get_history("type_text")
        self.assertEqual(len(types), 1)
        self.assertEqual(types[0].parameters["text"], "Hello World! 🚀")
        self.assertEqual(types[0].parameters["interval"], 0.01)

    def test_paste_text_and_clipboard(self) -> None:
        self.actuator.paste_text("- [ ] Task 1\n- [ ] Task 2")
        self.assertEqual(self.actuator.clipboard, "- [ ] Task 1\n- [ ] Task 2")
        self.actuator.assert_text_pasted("Task 1")
        self.actuator.assert_text_pasted("- [ ] Task 1\n- [ ] Task 2", exact=True)

    def test_press_hotkey_case_normalization(self) -> None:
        self.actuator.press_hotkey("Cmd", "N")
        self.actuator.assert_hotkey_pressed("cmd", "n", count=1)
        self.actuator.assert_hotkey_pressed("CMD", "N", count=1)

        hotkeys = self.actuator.get_history("press_hotkey")
        self.assertEqual(len(hotkeys), 1)
        self.assertEqual(hotkeys[0].parameters["keys"], ("cmd", "n"))

    def test_click_window_ratio_projection(self) -> None:
        win = WindowInfo(
            window_id=42,
            owner_name="Notes",
            title="Notes Window",
            x=100.0,
            y=200.0,
            width=500.0,
            height=400.0,
        )
        # Click center of window: 100 + 0.5*500 = 350; 200 + 0.5*400 = 400
        self.actuator.click_window_ratio(win, norm_x=0.5, norm_y=0.5)
        self.assertEqual(self.actuator.get_mouse_position(), (350.0, 400.0))
        self.actuator.assert_clicked_at(350.0, 400.0, tolerance=0.1)

    def test_window_info_queries_and_filtering(self) -> None:
        w1 = WindowInfo(1, "Notes", "Weekly Tasks", 0.0, 0.0, 600.0, 400.0)
        w2 = WindowInfo(2, "Terminal", "zsh", 600.0, 0.0, 600.0, 400.0)
        self.actuator.add_window(w1)
        self.actuator.add_window(w2)

        self.assertEqual(len(self.actuator.get_windows()), 2)
        notes_wins = self.actuator.get_windows("Notes")
        self.assertEqual(len(notes_wins), 1)
        self.assertEqual(notes_wins[0].title, "Weekly Tasks")

        term_wins = self.actuator.get_windows("terminal")  # case-insensitive
        self.assertEqual(len(term_wins), 1)

        self.actuator.clear_windows()
        self.assertEqual(len(self.actuator.get_windows()), 0)

    def test_window_info_helper_properties(self) -> None:
        w = WindowInfo(1, "App", "Title", 10.0, 20.0, 100.0, 200.0)
        self.assertEqual(w.bounds, (10.0, 20.0, 100.0, 200.0))
        self.assertEqual(w.center, (60.0, 120.0))
        self.assertTrue(w.contains_point(15.0, 25.0))
        self.assertFalse(w.contains_point(5.0, 25.0))
        self.assertFalse(w.contains_point(15.0, 250.0))
        self.assertEqual(w.ratio_to_screen(0.1, 0.2), (20.0, 60.0))

    def test_assertion_helpers_success(self) -> None:
        self.actuator.launch_app("com.apple.Notes")
        self.actuator.press_hotkey("cmd", "n")
        self.actuator.paste_text("Important meeting notes")

        self.actuator.assert_action_called("launch_app", count=1, app="com.apple.Notes")
        self.actuator.assert_action_called("press_hotkey", count=2)
        self.actuator.assert_hotkey_pressed("cmd", "n", count=1)
        self.actuator.assert_hotkey_pressed("cmd", "v", count=1)
        self.actuator.assert_action_called("paste_text", count=1)
        self.actuator.assert_no_errors()

    def test_assertion_helpers_failures_raise_assertion_error(self) -> None:
        with self.assertRaises(AssertionError):
            self.actuator.assert_app_launched("com.apple.Safari")

        with self.assertRaises(AssertionError):
            self.actuator.assert_hotkey_pressed("cmd", "q")

        with self.assertRaises(AssertionError):
            self.actuator.assert_text_typed("Non-existent text")

        with self.assertRaises(AssertionError):
            self.actuator.assert_clicked_at(10.0, 10.0)

    def test_context_manager_calls_stop(self) -> None:
        with MockActuator() as act:
            act.click(100.0, 100.0)
        act.assert_action_called("stop", count=1)

    def test_clear_history_and_get_history(self) -> None:
        self.actuator.click(100.0, 100.0)
        self.actuator.type_text("abc")
        self.assertEqual(len(self.actuator.action_history), 2)

        self.actuator.clear_history()
        self.assertEqual(len(self.actuator.action_history), 0)

    def test_invalid_mouse_coordinates_raise_error(self) -> None:
        """Verifies non-finite coordinates raise InputSynthesisError."""
        with self.assertRaises(InputSynthesisError):
            self.actuator.move_mouse(float("nan"), 100.0)
        with self.assertRaises(InputSynthesisError):
            self.actuator.move_mouse(100.0, float("inf"))
        with self.assertRaises(InputSynthesisError):
            self.actuator.move_mouse(float("-inf"), 100.0)
        with self.assertRaises(InputSynthesisError):
            self.actuator.click(x=float("nan"), y=100.0)
        with self.assertRaises(InputSynthesisError):
            self.actuator.drag(0.0, 0.0, float("nan"), 100.0)

    def test_click_duck_typing_external_mouse_button(self) -> None:
        """Verifies click handles foreign enums and string button names cleanly."""
        from enum import Enum

        class ForeignMouseButton(Enum):
            LEFT = "left"
            RIGHT = "right"
            CENTER = "center"

        self.actuator.click(button=ForeignMouseButton.LEFT)
        self.actuator.assert_action_called("click", button="left")

        self.actuator.click(button="LEFT")
        self.actuator.assert_action_called("click", button="left")

        self.actuator.click(button="middle")
        self.actuator.assert_action_called("click", button="center")

    def test_assert_clicked_at_external_enum(self) -> None:
        """Verifies assert_clicked_at succeeds with foreign enums."""
        from enum import Enum

        class ForeignMouseButton(Enum):
            LEFT = "left"

        self.actuator.click(x=200.0, y=300.0, button="left")
        self.actuator.assert_clicked_at(200.0, 300.0, button=ForeignMouseButton.LEFT)


class TestActuatorFactory(unittest.TestCase):
    """Verifies environment autodetection and instantiation logic of ActuatorFactory."""

    def setUp(self) -> None:
        self._orig_env = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._orig_env)

    def test_create_explicit_mock_mode(self) -> None:
        act = ActuatorFactory.create(mode=ActuatorMode.MOCK)
        self.assertIsInstance(act, MockActuator)

        act2 = ActuatorFactory.create(mode="mock")
        self.assertIsInstance(act2, MockActuator)

    def test_env_var_override_mock(self) -> None:
        os.environ["TASK_AUTOMATOR_ACTUATOR"] = "mock"
        act = ActuatorFactory.create()
        self.assertIsInstance(act, MockActuator)

    def test_ci_detection(self) -> None:
        os.environ["CI"] = "true"
        os.environ.pop("TASK_AUTOMATOR_ACTUATOR", None)
        self.assertTrue(ActuatorFactory.is_ci())
        act = ActuatorFactory.create()
        self.assertIsInstance(act, MockActuator)

    def test_unknown_mode_raises(self) -> None:
        with self.assertRaises(ActuatorError):
            ActuatorFactory.create(mode="invalid_mode")

    def test_get_actuator_shortcut(self) -> None:
        act = get_actuator(mode="mock")
        self.assertIsInstance(act, MockActuator)


if __name__ == "__main__":
    unittest.main()
