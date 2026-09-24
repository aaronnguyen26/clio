"""Tests for Live Demonstration Capture and Auto-Dissection Subsystem.

Validates:
1. Recording lifecycle: start, feed events, status, stop.
2. Auto-dissection into WorkflowSpec with noise reduction, event coalescing, and pause clamping.
3. Natural language trigger extraction and automatic persistence into TaskMemoryEngine.
4. Natural language retrieval of demonstrated workflows (e.g. 'write todolist' -> 'write weekly todo list').
5. Background-mode execution with independent virtual cursor & keyboard (zero hardware mouse displacement).
"""

from __future__ import annotations

import time
import unittest

from src.actuators.mock import MockActuator
from src.actuators.virtual_cursor import VirtualCursor
from src.executor.executor import AutonomousWorkflowExecutor
from src.memory.capture import LiveDemonstrationCapture
from src.memory.engine import TaskMemoryEngine
from src.memory.recorder import RawEvent, RawEventType, WindowBounds
from src.memory.retrieval import NLRetrievalEngine


class TestDemonstrationCaptureAndBackgroundMode(unittest.TestCase):
    def setUp(self) -> None:
        self.memory = TaskMemoryEngine(db_path=":memory:")
        self.capture = LiveDemonstrationCapture(memory=self.memory, mock=True)

    def tearDown(self) -> None:
        if self.capture.is_recording:
            self.capture.stop_recording()
        self.memory.close()

    def test_recording_lifecycle(self) -> None:
        self.assertFalse(self.capture.is_recording)
        self.assertEqual(self.capture.event_count, 0)

        started = self.capture.start_recording()
        self.assertTrue(started)
        self.assertTrue(self.capture.is_recording)

        # Feed events
        now = time.time()
        self.capture.feed_event(
            RawEvent(
                event_type=RawEventType.KEY_DOWN,
                timestamp=now,
                key="h",
                modifiers=[],
            )
        )
        self.assertEqual(self.capture.event_count, 1)

        events = self.capture.stop_recording()
        self.assertFalse(self.capture.is_recording)
        self.assertEqual(len(events), 1)

    def test_dissection_of_demonstrated_todo_workflow(self) -> None:
        """Demonstrates launching app, shortcut Cmd+N, typing weekly todo, and clicks."""
        self.capture.start_recording()
        t = time.time()

        # Step 1: App activation (Apple Notes)
        self.capture.feed_event(
            RawEvent(
                event_type=RawEventType.APP_ACTIVATE,
                timestamp=t,
                bundle_id="com.apple.Notes",
            )
        )

        # Step 2: User presses Cmd+N (New Note)
        t += 0.1
        self.capture.feed_event(
            RawEvent(
                event_type=RawEventType.KEY_DOWN,
                timestamp=t,
                key="n",
                modifiers=["cmd"],
            )
        )

        # Step 3: User types to-do list characters
        t += 0.2
        for char in "Weekly To-Do List:\n- Complete architecture\n- Test automation":
            self.capture.feed_event(
                RawEvent(
                    event_type=RawEventType.KEY_DOWN,
                    timestamp=t,
                    key=char,
                    modifiers=[],
                )
            )
            t += 0.02

        # Step 4: User clicks a button in the window
        t += 0.3
        bounds = WindowBounds(x=100.0, y=100.0, width=800.0, height=600.0)
        self.capture.feed_event(
            RawEvent(
                event_type=RawEventType.MOUSE_DOWN,
                timestamp=t,
                x=500.0,
                y=400.0,
                button="left",
                window_bounds=bounds,
            )
        )
        self.capture.feed_event(
            RawEvent(
                event_type=RawEventType.MOUSE_UP,
                timestamp=t + 0.05,
                x=500.0,
                y=400.0,
                button="left",
                window_bounds=bounds,
            )
        )

        # Dissect and save
        spec = self.capture.dissect_and_save(
            name="write weekly todo list",
            canonical_trigger="write todolist",
            description="Auto-dissected to-do list demonstration",
        )

        self.assertIsNotNone(spec)
        self.assertEqual(spec.name, "write weekly todo list")
        self.assertEqual(spec.triggers["canonical"], "write todolist")
        self.assertGreater(len(spec.steps), 0)

        # Verify steps were semantic (focus -> hotkey -> type_text -> click)
        actions = [s.action for s in spec.steps]
        self.assertIn("focus_app", actions)
        self.assertIn("press_hotkey", actions)
        self.assertTrue("type_text" in actions or "paste_text" in actions)
        self.assertIn("click", actions)

        # Verify normalized ratio coordinates were calculated on the click step
        click_step = next(s for s in spec.steps if s.action == "click")
        self.assertIn("norm_x", click_step.target)
        self.assertIn("norm_y", click_step.target)
        self.assertAlmostEqual(click_step.target["norm_x"], (500.0 - 100.0) / 800.0, places=3)
        self.assertAlmostEqual(click_step.target["norm_y"], (400.0 - 100.0) / 600.0, places=3)

        # Verify saved in TaskMemoryEngine and retrievable by natural language query
        retrieval = NLRetrievalEngine(self.memory)
        matches = retrieval.query("write todolist")
        self.assertGreater(len(matches), 0)
        self.assertEqual(matches[0].workflow_id, spec.id)

    def test_background_mode_execution_with_independent_virtual_cursor(self) -> None:
        """Verifies that running in background_mode uses virtual cursor without stealing focus."""
        mock_actuator = MockActuator()
        virtual_cursor = VirtualCursor(mock=True, target_pid=12345)
        executor = AutonomousWorkflowExecutor(
            actuator=mock_actuator,
            virtual_cursor=virtual_cursor,
            memory_engine=self.memory,
            background_mode=True,
            zero_delay=True,
        )

        # Build a sample workflow
        self.capture.start_recording()
        t = time.time()
        self.capture.feed_event(
            RawEvent(event_type=RawEventType.APP_ACTIVATE, timestamp=t, bundle_id="com.apple.Notes")
        )
        for char in "Notes in background":
            self.capture.feed_event(
                RawEvent(event_type=RawEventType.KEY_DOWN, timestamp=t + 0.01, key=char)
            )
        spec = self.capture.dissect_and_save(
            name="write notes background",
            canonical_trigger="write notes background",
        )

        # Execute with background_mode=True
        result = executor.execute_workflow(spec, background_mode=True)
        self.assertTrue(result.success)
        self.assertEqual(result.steps_completed, len(spec.steps))

        # Check virtual cursor recorded the background typing actions
        vc_types = [ev.event_type for ev in virtual_cursor.history]
        self.assertIn("type_text", vc_types)

        # Verify actuator did NOT steal focus
        focus_actions = [a for a in mock_actuator.history if a.action_type in ("focus_app", "activate_app")]
        self.assertEqual(len(focus_actions), 0)


if __name__ == "__main__":
    unittest.main()
