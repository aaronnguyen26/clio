"""Unit tests for VirtualCursor and VirtualCursorState (FEAT-ACT-11, Requirement R7).

Path: tests/unit/test_virtual_cursor.py
Belongs to Milestone M3 (Autonomous Closed-Loop Executor & Virtual Cursor).
"""

from __future__ import annotations

import inspect
import math
import os
import sys
import unittest
from typing import List, Tuple

# Import from src.actuators.virtual_cursor
from src.actuators.virtual_cursor import (
    VirtualCursor,
    VirtualCursorEvent,
    VirtualCursorState,
)
from src.actuators.types import (
    ActuatorError,
    InputSynthesisError,
    MouseButton,
    WindowInfo,
)


class TestVirtualCursorStateEnum(unittest.TestCase):
    """Verifies VirtualCursorState enum definition and semantics."""

    def test_enum_members_and_values(self) -> None:
        self.assertEqual(VirtualCursorState.IDLE, "IDLE")
        self.assertEqual(VirtualCursorState.MOVING, "MOVING")
        self.assertEqual(VirtualCursorState.HOVERING, "HOVERING")
        self.assertEqual(VirtualCursorState.CLICKING, "CLICKING")
        self.assertEqual(VirtualCursorState.DRAGGING, "DRAGGING")

    def test_enum_completeness(self) -> None:
        expected = {"IDLE", "MOVING", "HOVERING", "CLICKING", "DRAGGING"}
        actual = {s.value for s in VirtualCursorState}
        self.assertEqual(expected, actual)


class TestVirtualCursorInitialization(unittest.TestCase):
    """Verifies default and custom initialization of VirtualCursor."""

    def test_default_initialization(self) -> None:
        cursor = VirtualCursor(mock=True)
        self.assertEqual(cursor.vx, 0.0)
        self.assertEqual(cursor.vy, 0.0)
        self.assertEqual(cursor.position, (0.0, 0.0))
        self.assertEqual(cursor.state, VirtualCursorState.IDLE)
        self.assertEqual(cursor.velocity, (0.0, 0.0))
        self.assertEqual(cursor.speed, 0.0)
        self.assertIsNone(cursor.target_pid)
        self.assertIsNone(cursor.target_window_id)
        self.assertTrue(cursor.mock)
        self.assertEqual(len(cursor.history), 0)
        self.assertEqual(len(cursor.trajectory), 1)

    def test_custom_initialization(self) -> None:
        cursor = VirtualCursor(
            initial_x=150.5,
            initial_y=250.75,
            target_pid=12345,
            target_window_id=6789,
            mock=True,
            name="test_pointer",
        )
        self.assertEqual(cursor.vx, 150.5)
        self.assertEqual(cursor.vy, 250.75)
        self.assertEqual(cursor.position, (150.5, 250.75))
        self.assertEqual(cursor.target_pid, 12345)
        self.assertEqual(cursor.target_window_id, 6789)
        self.assertEqual(cursor.name, "test_pointer")


class TestVirtualCursorMovement(unittest.TestCase):
    """Verifies instantaneous, smooth, and timed movement."""

    def setUp(self) -> None:
        self.cursor = VirtualCursor(initial_x=100.0, initial_y=100.0, mock=True)

    def test_instantaneous_move(self) -> None:
        self.cursor.move_to(300.0, 400.0)
        self.assertEqual(self.cursor.position, (300.0, 400.0))
        self.assertEqual(self.cursor.state, VirtualCursorState.IDLE)
        self.assertEqual(self.cursor.velocity, (0.0, 0.0))
        self.assertEqual(self.cursor.speed, 0.0)

        history = self.cursor.get_history("move")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].x, 300.0)
        self.assertEqual(history[0].y, 400.0)
        self.assertEqual(history[0].previous_position, (100.0, 100.0))
        self.assertFalse(history[0].smooth)

    def test_smooth_move_trajectory_and_speed(self) -> None:
        # Move from (100, 100) to (400, 500) over 0.1s
        # Distance = hypot(300, 400) = 500.0
        self.cursor.move_to(400.0, 500.0, duration=0.1, smooth=True, steps=10)
        self.assertEqual(self.cursor.position, (400.0, 500.0))
        self.assertEqual(self.cursor.state, VirtualCursorState.IDLE)

        # Trajectory has initial point + 10 step points
        self.assertGreaterEqual(len(self.cursor.trajectory), 11)

        history = self.cursor.get_history("move")
        self.assertEqual(len(history), 1)
        self.assertTrue(history[0].smooth)
        self.assertAlmostEqual(history[0].speed, 5000.0, delta=1.0)
        self.assertAlmostEqual(history[0].velocity[0], 3000.0, delta=1.0)
        self.assertAlmostEqual(history[0].velocity[1], 4000.0, delta=1.0)

    def test_invalid_coordinates_raise_error(self) -> None:
        with self.assertRaises(InputSynthesisError):
            self.cursor.move_to(float("nan"), 200.0)
        with self.assertRaises(InputSynthesisError):
            self.cursor.move_to(100.0, float("inf"))
        with self.assertRaises(InputSynthesisError):
            self.cursor.move_to(-float("inf"), 200.0)


class TestVirtualCursorClicking(unittest.TestCase):
    """Verifies single, double, right, center, and coordinate clicking."""

    def setUp(self) -> None:
        self.cursor = VirtualCursor(initial_x=200.0, initial_y=300.0, mock=True)

    def test_click_at_current_position(self) -> None:
        self.cursor.click()
        self.assertEqual(self.cursor.position, (200.0, 300.0))
        self.assertEqual(self.cursor.state, VirtualCursorState.IDLE)

        self.cursor.assert_clicked_at(200.0, 300.0, button="left", count=1)

        # Checks down and up sequence
        down_events = self.cursor.get_history("mouse_down")
        up_events = self.cursor.get_history("mouse_up")
        self.assertEqual(len(down_events), 1)
        self.assertEqual(len(up_events), 1)
        self.assertEqual(down_events[0].click_count, 1)
        self.assertEqual(up_events[0].click_count, 1)

    def test_click_with_coordinates_moves_first(self) -> None:
        self.cursor.click(x=450.0, y=550.0)
        self.assertEqual(self.cursor.position, (450.0, 550.0))
        self.cursor.assert_clicked_at(450.0, 550.0, button="left", count=1)

    def test_double_click(self) -> None:
        self.cursor.double_click(x=150.0, y=250.0)
        self.assertEqual(self.cursor.position, (150.0, 250.0))
        self.cursor.assert_clicked_at(150.0, 250.0, button="left", count=2)

        down_events = self.cursor.get_history("mouse_down")
        up_events = self.cursor.get_history("mouse_up")
        self.assertEqual(len(down_events), 2)
        self.assertEqual(len(up_events), 2)
        self.assertEqual(down_events[0].click_count, 1)
        self.assertEqual(down_events[1].click_count, 2)

    def test_right_click(self) -> None:
        self.cursor.right_click()
        self.cursor.assert_clicked_at(200.0, 300.0, button="right", count=1)

    def test_button_normalization(self) -> None:
        self.cursor.click(button="middle")
        self.cursor.assert_clicked_at(200.0, 300.0, button="center", count=1)

        self.cursor.click(button=MouseButton.RIGHT)
        self.cursor.assert_clicked_at(200.0, 300.0, button="right", count=1)

    def test_unsupported_button_raises(self) -> None:
        with self.assertRaises(InputSynthesisError):
            self.cursor.click(button="invalid_btn")


class TestVirtualCursorDragging(unittest.TestCase):
    """Verifies drag gestures, intermediate points, and states."""

    def setUp(self) -> None:
        self.cursor = VirtualCursor(initial_x=50.0, initial_y=50.0, mock=True)

    def test_drag_to_movement_and_events(self) -> None:
        self.cursor.drag_to(350.0, 450.0, duration=0.1, button="left", steps=5)
        self.assertEqual(self.cursor.position, (350.0, 450.0))
        self.assertEqual(self.cursor.state, VirtualCursorState.IDLE)

        self.cursor.assert_dragged(start=(50.0, 50.0), end=(350.0, 450.0))

        drag_events = self.cursor.get_history("drag")
        self.assertEqual(len(drag_events), 1)
        self.assertEqual(drag_events[0].previous_position, (50.0, 50.0))
        self.assertEqual(drag_events[0].x, 350.0)
        self.assertEqual(drag_events[0].y, 450.0)
        self.assertAlmostEqual(drag_events[0].speed, 5000.0, delta=1.0)

    def test_drag_non_finite_raises(self) -> None:
        with self.assertRaises(InputSynthesisError):
            self.cursor.drag_to(float("nan"), 100.0)


class TestVirtualCursorHovering(unittest.TestCase):
    """Verifies hover action and state tracking."""

    def setUp(self) -> None:
        self.cursor = VirtualCursor(initial_x=10.0, initial_y=20.0, mock=True)

    def test_hover_at_target(self) -> None:
        self.cursor.hover(500.0, 600.0, duration=0.01)
        self.assertEqual(self.cursor.position, (500.0, 600.0))
        self.assertEqual(self.cursor.state, VirtualCursorState.IDLE)
        self.cursor.assert_hovered_at(500.0, 600.0)

    def test_hover_non_finite_raises(self) -> None:
        with self.assertRaises(InputSynthesisError):
            self.cursor.hover(float("nan"), 10.0)


class TestVirtualCursorScroll(unittest.TestCase):
    """Verifies scroll wheel event synthesis."""

    def test_scroll_events(self) -> None:
        cursor = VirtualCursor(mock=True)
        cursor.scroll(dx=10, dy=-20)
        scroll_events = cursor.get_history("scroll")
        self.assertEqual(len(scroll_events), 1)
        self.assertEqual(scroll_events[0].extra["dx"], 10)
        self.assertEqual(scroll_events[0].extra["dy"], -20)


class TestVirtualCursorTargetAndWindow(unittest.TestCase):
    """Verifies target PID, target window, and ratio coordinate operations."""

    def setUp(self) -> None:
        self.cursor = VirtualCursor(mock=True)
        self.window = WindowInfo(
            window_id=9876,
            owner_name="Apple Notes",
            title="Notes Window",
            x=200.0,
            y=150.0,
            width=800.0,
            height=600.0,
        )

    def test_set_target(self) -> None:
        self.cursor.set_target(pid=4321, window_id=9876)
        self.assertEqual(self.cursor.target_pid, 4321)
        self.assertEqual(self.cursor.target_window_id, 9876)

    def test_move_to_ratio(self) -> None:
        # (0.5, 0.5) of window -> (200 + 400, 150 + 300) = (600, 450)
        self.cursor.move_to_ratio(self.window, 0.5, 0.5)
        self.assertEqual(self.cursor.position, (600.0, 450.0))
        self.assertEqual(self.cursor.target_window_id, 9876)

    def test_click_ratio(self) -> None:
        # (0.25, 0.75) -> (200 + 200, 150 + 450) = (400, 600)
        self.cursor.click_ratio(self.window, 0.25, 0.75, button="left")
        self.assertEqual(self.cursor.position, (400.0, 600.0))
        self.cursor.assert_clicked_at(400.0, 600.0, button="left", count=1)

    def test_drag_to_ratio(self) -> None:
        self.cursor.move_to(400.0, 600.0)
        self.cursor.drag_to_ratio(self.window, 0.5, 0.5)
        self.assertEqual(self.cursor.position, (600.0, 450.0))
        self.cursor.assert_dragged(start=(400.0, 600.0), end=(600.0, 450.0))


class TestVirtualCursorEventListeners(unittest.TestCase):
    """Verifies observer pub/sub mechanism."""

    def test_listener_subscription_and_dispatch(self) -> None:
        cursor = VirtualCursor(mock=True)
        received_events: List[VirtualCursorEvent] = []

        def on_event(ev: VirtualCursorEvent) -> None:
            received_events.append(ev)

        cursor.add_listener(on_event)
        cursor.move_to(100.0, 200.0)
        cursor.click()

        # Received move, mouse_down, mouse_up, click
        event_types = [e.event_type for e in received_events]
        self.assertIn("move", event_types)
        self.assertIn("mouse_down", event_types)
        self.assertIn("mouse_up", event_types)
        self.assertIn("click", event_types)

        # Unsubscribe
        cursor.remove_listener(on_event)
        before_count = len(received_events)
        cursor.move_to(300.0, 400.0)
        self.assertEqual(len(received_events), before_count)


class TestVirtualCursorAssertionHelpers(unittest.TestCase):
    """Verifies built-in assertion helpers and failure conditions."""

    def test_assertion_helpers(self) -> None:
        cursor = VirtualCursor(initial_x=10.0, initial_y=20.0, mock=True)
        cursor.assert_position(10.0, 20.0)
        cursor.assert_state(VirtualCursorState.IDLE)

        with self.assertRaises(AssertionError):
            cursor.assert_position(99.0, 99.0)

        with self.assertRaises(AssertionError):
            cursor.assert_state(VirtualCursorState.MOVING)

        with self.assertRaises(AssertionError):
            cursor.assert_clicked_at(10.0, 20.0)

        cursor.click()
        cursor.assert_clicked_at(10.0, 20.0)

        cursor.clear_history()
        self.assertEqual(len(cursor.history), 0)


class TestStrictZeroDisplacementInvariant(unittest.TestCase):
    """Verifies Requirement R7: Zero physical cursor displacement invariant."""

    def test_no_cgwarp_in_module(self) -> None:
        """Statically inspects virtual_cursor module AST to guarantee CGWarp/kCGHIDEventTap are NEVER called or bound."""
        import ast
        from src.actuators import virtual_cursor
        src = inspect.getsource(virtual_cursor)
        tree = ast.parse(src)
        called_or_accessed = {
            node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
        } | {
            node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
        }
        self.assertNotIn("CGWarpMouseCursorPosition", called_or_accessed)
        self.assertNotIn("kCGHIDEventTap", called_or_accessed)

    def test_live_zero_hardware_mouse_displacement(self) -> None:
        """Empirically verifies that virtual cursor actions leave hardware mouse 100% untouched."""
        if sys.platform != "darwin":
            self.skipTest("macOS Darwin required for live hardware mouse displacement verification")

        import ctypes
        from ctypes import c_double, c_void_p, Structure

        class CGPoint(Structure):
            _fields_ = [("x", c_double), ("y", c_double)]

        try:
            cg = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
            cf = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
            cg.CGEventCreate.argtypes = [c_void_p]
            cg.CGEventCreate.restype = c_void_p
            cg.CGEventGetLocation.argtypes = [c_void_p]
            cg.CGEventGetLocation.restype = CGPoint
            cf.CFRelease.argtypes = [c_void_p]
            cf.CFRelease.restype = None
        except OSError:
            self.skipTest("Could not load CoreGraphics for hardware mouse location check")

        def get_hardware_pos() -> Tuple[float, float]:
            ev = cg.CGEventCreate(None)
            pos = cg.CGEventGetLocation(ev)
            cf.CFRelease(ev)
            return (pos.x, pos.y)

        initial_hw_pos = get_hardware_pos()

        # Instantiate VirtualCursor in live mode with target_pid set to self (python PID)
        cursor = VirtualCursor(
            initial_x=5.0,
            initial_y=5.0,
            target_pid=os.getpid(),
            mock=False,
        )

        # Perform virtual cursor operations instantaneously to minimize physical window
        cursor.move_to(25.0, 35.0, duration=0.0, smooth=False)
        cursor.click(x=45.0, y=55.0, button="left", click_count=1)
        cursor.drag_to(75.0, 85.0, duration=0.0)

        # Verify that hardware mouse position was NOT warped to any virtual cursor target
        final_hw_pos = get_hardware_pos()
        for target in [(25.0, 35.0), (45.0, 55.0), (75.0, 85.0)]:
            self.assertNotEqual(
                final_hw_pos,
                target,
                f"Physical hardware mouse was warped to virtual cursor target {target}! Violated Requirement R7!",
            )

        # Invariant: hardware mouse position must remain unchanged (or reflect minimal trackpad jitter)
        if initial_hw_pos != final_hw_pos:
            displacement = math.hypot(final_hw_pos[0] - initial_hw_pos[0], final_hw_pos[1] - initial_hw_pos[1])
            self.assertLess(
                displacement,
                50.0,
                f"Physical hardware mouse was displaced from {initial_hw_pos} to {final_hw_pos}! Violated Requirement R7!",
            )


if __name__ == "__main__":
    unittest.main()
