"""Unit tests for Failsafe emergency stop geometry, exception, watchdog daemon, and inline check.

Belongs to M1 test suite (tests/unit/test_failsafe.py).
Fully executable with standard library: python3 -m unittest discover.
"""

from __future__ import annotations

import threading
import time
import unittest
from typing import List, Tuple

from src.actuators.failsafe import FailsafeCornerWatchdog, FailsafeWatchdog
from src.actuators.mock import MockActuator
from src.actuators.types import (
    ActuatorError,
    CornerLocation,
    FailsafeEmergencyStop,
    FailsafeTriggerInfo,
    MouseButton,
    get_screen_corner,
    is_screen_corner,
)


class TestFailsafeException(unittest.TestCase):
    """Verifies FailsafeEmergencyStop exception data structure and hierarchy."""

    def test_exception_inheritance(self) -> None:
        exc = FailsafeEmergencyStop()
        self.assertIsInstance(exc, ActuatorError)
        self.assertIsInstance(exc, Exception)

    def test_exception_attributes_and_string_representation(self) -> None:
        exc = FailsafeEmergencyStop(
            message="Test abort",
            position=(0.0, 0.0),
            trigger_reason="mouse_corner",
        )
        self.assertEqual(exc.message, "Test abort")
        self.assertEqual(exc.position, (0.0, 0.0))
        self.assertEqual(exc.trigger_reason, "mouse_corner")
        self.assertGreater(exc.timestamp, 0.0)
        self.assertIn("Test abort", str(exc))
        self.assertIn("(0.0, 0.0)", str(exc))


class TestFailsafeCornerGeometry(unittest.TestCase):
    """Verifies mathematical correctness of corner detection across the 4 screen corners."""

    def setUp(self) -> None:
        self.w = 1920.0
        self.h = 1080.0
        self.margin = 5.0

    def test_all_four_exact_corners(self) -> None:
        # Top-Left (0, 0)
        self.assertTrue(is_screen_corner(0.0, 0.0, self.w, self.h, self.margin))
        self.assertEqual(get_screen_corner(0.0, 0.0, self.w, self.h, self.margin), CornerLocation.TOP_LEFT)

        # Top-Right (w, 0)
        self.assertTrue(is_screen_corner(self.w, 0.0, self.w, self.h, self.margin))
        self.assertEqual(get_screen_corner(self.w, 0.0, self.w, self.h, self.margin), CornerLocation.TOP_RIGHT)

        # Bottom-Left (0, h)
        self.assertTrue(is_screen_corner(0.0, self.h, self.w, self.h, self.margin))
        self.assertEqual(get_screen_corner(0.0, self.h, self.w, self.h, self.margin), CornerLocation.BOTTOM_LEFT)

        # Bottom-Right (w, h)
        self.assertTrue(is_screen_corner(self.w, self.h, self.w, self.h, self.margin))
        self.assertEqual(get_screen_corner(self.w, self.h, self.w, self.h, self.margin), CornerLocation.BOTTOM_RIGHT)

    def test_within_margin_boundaries(self) -> None:
        # Top-Left threshold (5, 5)
        self.assertTrue(is_screen_corner(5.0, 5.0, self.w, self.h, self.margin))
        self.assertEqual(get_screen_corner(5.0, 5.0, self.w, self.h, self.margin), CornerLocation.TOP_LEFT)

        # Top-Right threshold (1915, 5)
        self.assertTrue(is_screen_corner(self.w - self.margin, 5.0, self.w, self.h, self.margin))
        self.assertEqual(get_screen_corner(self.w - self.margin, 5.0, self.w, self.h, self.margin), CornerLocation.TOP_RIGHT)

        # Bottom-Left threshold (5, 1075)
        self.assertTrue(is_screen_corner(5.0, self.h - self.margin, self.w, self.h, self.margin))
        self.assertEqual(get_screen_corner(5.0, self.h - self.margin, self.w, self.h, self.margin), CornerLocation.BOTTOM_LEFT)

        # Bottom-Right threshold (1915, 1075)
        self.assertTrue(is_screen_corner(self.w - self.margin, self.h - self.margin, self.w, self.h, self.margin))
        self.assertEqual(get_screen_corner(self.w - self.margin, self.h - self.margin, self.w, self.h, self.margin), CornerLocation.BOTTOM_RIGHT)

    def test_negative_or_overflow_coordinates_trigger_failsafe(self) -> None:
        # Shoving cursor hard past top-left (-2, -2)
        self.assertTrue(is_screen_corner(-2.0, -2.0, self.w, self.h, self.margin))
        # Shoving cursor hard past bottom-right (1925, 1085)
        self.assertTrue(is_screen_corner(self.w + 5.0, self.h + 5.0, self.w, self.h, self.margin))

    def test_non_corners_do_not_trigger(self) -> None:
        # Screen center
        self.assertFalse(is_screen_corner(960.0, 540.0, self.w, self.h, self.margin))
        self.assertIsNone(get_screen_corner(960.0, 540.0, self.w, self.h, self.margin))

        # Barely outside threshold
        self.assertFalse(is_screen_corner(6.0, 6.0, self.w, self.h, self.margin))
        self.assertFalse(is_screen_corner(self.w - 6.0, 6.0, self.w, self.h, self.margin))
        self.assertFalse(is_screen_corner(6.0, self.h - 6.0, self.w, self.h, self.margin))
        self.assertFalse(is_screen_corner(self.w - 6.0, self.h - 6.0, self.w, self.h, self.margin))

        # Screen edges (middle of edges, NOT corners)
        self.assertFalse(is_screen_corner(0.0, 540.0, self.w, self.h, self.margin))  # Left edge middle
        self.assertFalse(is_screen_corner(self.w, 540.0, self.w, self.h, self.margin))  # Right edge middle
        self.assertFalse(is_screen_corner(960.0, 0.0, self.w, self.h, self.margin))  # Top edge middle
        self.assertFalse(is_screen_corner(960.0, self.h, self.w, self.h, self.margin))  # Bottom edge middle


class TestMockActuatorFailsafeInterception(unittest.TestCase):
    """Verifies that MockActuator enforces emergency stop and blocks all subsequent actions."""

    def setUp(self) -> None:
        self.actuator = MockActuator(
            screen_size=(1920.0, 1080.0),
            initial_pos=(500.0, 500.0),
            failsafe_margin=5.0,
            enforce_corner_failsafe=True,
        )

    def test_manual_trigger_and_check(self) -> None:
        self.assertFalse(self.actuator.is_failsafe_triggered())
        self.actuator.trigger_failsafe(position=(0.0, 0.0))
        self.assertTrue(self.actuator.is_failsafe_triggered())

        with self.assertRaises(FailsafeEmergencyStop) as ctx:
            self.actuator.check_failsafe()

        self.assertEqual(ctx.exception.position, (0.0, 0.0))

    def test_corner_movement_sets_failsafe_triggered(self) -> None:
        self.assertFalse(self.actuator.is_failsafe_triggered())
        # Move into top-left corner
        self.actuator.move_mouse(0.0, 0.0)
        self.assertTrue(self.actuator.is_failsafe_triggered())

        # Next action must immediately abort
        with self.assertRaises(FailsafeEmergencyStop):
            self.actuator.click()

    def test_failsafe_blocks_all_actuator_operations(self) -> None:
        self.actuator.trigger_failsafe(position=(0.0, 0.0))

        # Every single actuator method must raise FailsafeEmergencyStop
        with self.assertRaises(FailsafeEmergencyStop):
            self.actuator.move_mouse(100.0, 100.0)

        with self.assertRaises(FailsafeEmergencyStop):
            self.actuator.click(100.0, 100.0)

        with self.assertRaises(FailsafeEmergencyStop):
            self.actuator.drag(10.0, 10.0, 50.0, 50.0)

        with self.assertRaises(FailsafeEmergencyStop):
            self.actuator.scroll(dx=0, dy=10)

        with self.assertRaises(FailsafeEmergencyStop):
            self.actuator.type_text("should not type")

        with self.assertRaises(FailsafeEmergencyStop):
            self.actuator.paste_text("should not paste")

        with self.assertRaises(FailsafeEmergencyStop):
            self.actuator.press_hotkey("cmd", "n")

        with self.assertRaises(FailsafeEmergencyStop):
            self.actuator.launch_app("com.apple.Notes")

        with self.assertRaises(FailsafeEmergencyStop):
            self.actuator.focus_app("Notes")

    def test_reset_failsafe_resumes_normal_operation(self) -> None:
        self.actuator.trigger_failsafe()
        self.assertTrue(self.actuator.is_failsafe_triggered())

        self.actuator.reset_failsafe()
        # Move cursor to safe position
        self.actuator.cursor_pos = (500.0, 500.0)
        self.assertFalse(self.actuator.is_failsafe_triggered())

        # Actions succeed now
        self.actuator.type_text("Resumed typing")
        self.actuator.assert_text_typed("Resumed")

    def test_workflow_execution_halt_sequence(self) -> None:
        """Simulates an autonomous workflow executor being halted mid-sequence."""
        steps_executed: List[str] = []

        def execute_step(step_name: str, action_func):
            try:
                action_func()
                steps_executed.append(step_name)
            except FailsafeEmergencyStop:
                return False
            return True

        res1 = execute_step("launch", lambda: self.actuator.launch_app("com.apple.Notes"))
        self.assertTrue(res1)

        self.actuator.trigger_failsafe()

        res3 = execute_step("hotkey", lambda: self.actuator.press_hotkey("cmd", "n"))
        self.assertFalse(res3)

        res4 = execute_step("type", lambda: self.actuator.type_text("To-Do"))
        self.assertFalse(res4)

        self.assertEqual(steps_executed, ["launch"])


class TestFailsafeWatchdogDaemon(unittest.TestCase):
    """Verifies FailsafeWatchdog daemon thread, callbacks, and injection hooks."""

    def test_watchdog_lifecycle_and_injection(self) -> None:
        simulated_pos = [500.0, 500.0]
        releaser_called = []
        callbacks_received = []

        def mock_pos() -> Tuple[float, float]:
            return (simulated_pos[0], simulated_pos[1])

        def mock_bounds() -> List[Tuple[float, float, float, float]]:
            return [(0.0, 0.0, 1920.0, 1080.0)]

        def mock_releaser(current_pos=None) -> None:
            releaser_called.append(True)

        watchdog = FailsafeWatchdog(
            margin=5.0,
            frequency=50.0,
            position_getter=mock_pos,
            bounds_getter=mock_bounds,
            modifier_releaser=mock_releaser,
            auto_release_modifiers=True,
            trap_signals=False,
        )

        watchdog.add_callback(lambda info: callbacks_received.append(info))

        self.assertFalse(watchdog.is_running)
        self.assertFalse(watchdog.is_triggered)

        watchdog.start()
        self.assertTrue(watchdog.is_running)

        # Normal position -> no trigger
        time.sleep(0.05)
        self.assertFalse(watchdog.is_triggered)

        # Move into corner
        simulated_pos[0] = 0.0
        simulated_pos[1] = 0.0

        # Wait up to 0.5s for 50Hz thread to detect corner and dispatch callbacks
        deadline = time.time() + 0.5
        while time.time() < deadline and (
            not watchdog.is_triggered or len(callbacks_received) == 0 or len(releaser_called) == 0
        ):
            time.sleep(0.005)

        self.assertTrue(watchdog.is_triggered)
        self.assertIsNotNone(watchdog.trigger_info)
        self.assertEqual(watchdog.trigger_info.corner, "top-left")
        self.assertTrue(len(callbacks_received) >= 1)
        self.assertTrue(len(releaser_called) >= 1)

        watchdog.stop()
        self.assertFalse(watchdog.is_running)

    def test_watchdog_inline_check_failsafe(self) -> None:
        simulated_pos = [500.0, 500.0]

        watchdog = FailsafeWatchdog(
            margin=5.0,
            position_getter=lambda: (simulated_pos[0], simulated_pos[1]),
            bounds_getter=lambda: [(0.0, 0.0, 1920.0, 1080.0)],
            trap_signals=False,
        )

        # Safe position -> check passes
        watchdog.check_failsafe()

        # Move to corner
        simulated_pos[0] = 1920.0
        simulated_pos[1] = 1080.0

        with self.assertRaises(FailsafeEmergencyStop) as ctx:
            watchdog.check_failsafe()

        self.assertIn("bottom-right", ctx.exception.message)
        self.assertTrue(watchdog.is_triggered)

    def test_watchdog_context_manager(self) -> None:
        with FailsafeWatchdog(
            position_getter=lambda: (500.0, 500.0),
            bounds_getter=lambda: [(0.0, 0.0, 1920.0, 1080.0)],
            trap_signals=False,
        ) as wd:
            self.assertTrue(wd.is_running)
        self.assertFalse(wd.is_running)


if __name__ == "__main__":
    unittest.main()
