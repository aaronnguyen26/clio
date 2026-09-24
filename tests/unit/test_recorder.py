"""Unit tests for WorkflowRecorderPipeline (FEAT-MEM-05).

Tests validate:
- Event capture stage lifecycle (start, feed, stop)
- 4-Stage Coalescing Pipeline:
  1. Mouse move jitter decimation and anchor preservation
  2. Keystroke coalescing into TYPE_TEXT and PASTE_TEXT
  3. Hotkey aggregation with modifiers
  4. Mouse down/up click and double-click coalescing
  5. Pause interval clamping for human hesitation
- Window-relative ratio coordinate mapping
- Generation of compliant WorkflowSpec
"""

import unittest

from src.memory.models import ActionType
from src.memory.recorder import (
    EventCaptureStage,
    RawEvent,
    RawEventType,
    RawInputEvent,
    RecorderConfig,
    WindowBounds,
    WorkflowRecorderPipeline,
)


class TestWorkflowRecorder(unittest.TestCase):
    """Test suite for WorkflowRecorderPipeline."""

    def test_event_capture_stage(self):
        """TEST-REC-01: Capture stage lifecycle and event buffering."""
        stage = EventCaptureStage()
        self.assertFalse(stage.is_recording)

        stage.start()
        self.assertTrue(stage.is_recording)

        ev = RawEvent(event_type=RawEventType.KEY_DOWN, timestamp=1.0, key="a")
        stage.feed_event(ev)

        events = stage.stop()
        self.assertFalse(stage.is_recording)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].key, "a")

    def test_keystroke_coalescing_short_text(self):
        """TEST-REC-02: Consecutive keystrokes coalesce into a single TYPE_TEXT action."""
        pipeline = WorkflowRecorderPipeline()
        events = [
            RawInputEvent(event_type="keydown", key="H", timestamp=10.0),
            RawInputEvent(event_type="keydown", key="e", timestamp=10.05),
            RawInputEvent(event_type="keydown", key="l", timestamp=10.10),
            RawInputEvent(event_type="keydown", key="l", timestamp=10.15),
            RawInputEvent(event_type="keydown", key="o", timestamp=10.20),
        ]
        spec = pipeline.compile_workflow("wf_typing", "Typing Demo", events)
        text_steps = [s for s in spec.steps if s.action == ActionType.TYPE_TEXT]
        self.assertEqual(len(text_steps), 1)
        self.assertEqual(text_steps[0].payload.get("text"), "Hello")

    def test_keystroke_coalescing_paste_text(self):
        """TEST-REC-03: Long text exceeding 60 characters coalesces into PASTE_TEXT."""
        pipeline = WorkflowRecorderPipeline()
        long_str = "A" * 70
        events = [
            RawEvent(event_type=RawEventType.KEY_CHAR, key=c, timestamp=1.0 + i * 0.01)
            for i, c in enumerate(long_str)
        ]
        spec = pipeline.process_raw_events(events, name="Paste Demo")
        self.assertEqual(len(spec.steps), 1)
        self.assertEqual(spec.steps[0].action, ActionType.PASTE_TEXT)
        self.assertEqual(spec.steps[0].payload.get("text"), long_str)

    def test_hotkey_aggregation(self):
        """TEST-REC-04: Modifier key combinations coalesce into PRESS_HOTKEY."""
        pipeline = WorkflowRecorderPipeline()
        events = [
            RawEvent(
                event_type=RawEventType.KEY_DOWN,
                timestamp=1.0,
                key="n",
                modifiers=["cmd"],
            ),
            RawEvent(
                event_type=RawEventType.KEY_UP,
                timestamp=1.05,
                key="n",
                modifiers=["cmd"],
            ),
        ]
        spec = pipeline.process_raw_events(events, name="Hotkey Demo")
        self.assertEqual(len(spec.steps), 1)
        self.assertEqual(spec.steps[0].action, ActionType.PRESS_HOTKEY)
        self.assertEqual(spec.steps[0].payload.get("keys"), ["cmd", "n"])

    def test_mouse_jitter_decimation(self):
        """TEST-REC-05: Micro-movements within jitter threshold are decimated."""
        pipeline = WorkflowRecorderPipeline(jitter_pixel_threshold=5.0)
        events = [
            RawInputEvent(event_type="mousemove", x=100.0, y=100.0, timestamp=1.0),
            RawInputEvent(event_type="mousemove", x=101.0, y=102.0, timestamp=1.005),  # jitter
            RawInputEvent(event_type="mousemove", x=102.0, y=101.5, timestamp=1.008),  # jitter
            RawInputEvent(event_type="mousemove", x=250.0, y=300.0, timestamp=1.20),   # intentional move
        ]
        coalesced = pipeline.coalesce_mouse_events(events)
        self.assertEqual(len(coalesced), 2)
        self.assertEqual((coalesced[-1].x, coalesced[-1].y), (250.0, 300.0))

    def test_mouse_anchor_preservation(self):
        """TEST-REC-06: Never decimate mouse move immediately preceding a click."""
        pipeline = WorkflowRecorderPipeline()
        events = [
            RawEvent(event_type=RawEventType.MOUSE_MOVE, x=100.0, y=100.0, timestamp=1.0),
            RawEvent(event_type=RawEventType.MOUSE_MOVE, x=101.0, y=101.0, timestamp=1.005),  # anchor move
            RawEvent(event_type=RawEventType.MOUSE_DOWN, x=101.0, y=101.0, timestamp=1.010),
            RawEvent(event_type=RawEventType.MOUSE_UP, x=101.0, y=101.0, timestamp=1.060),
        ]
        spec = pipeline.process_raw_events(events, name="Anchor Click Demo")
        self.assertEqual(len(spec.steps), 1)
        self.assertEqual(spec.steps[0].action, ActionType.CLICK)
        self.assertEqual(spec.steps[0].target["screen_x"], 101)

    def test_click_and_double_click_coalescing(self):
        """TEST-REC-07: Mouse down/up coalesces into single or double clicks."""
        pipeline = WorkflowRecorderPipeline()
        events = [
            # First click
            RawEvent(event_type=RawEventType.MOUSE_DOWN, x=50.0, y=50.0, timestamp=1.0),
            RawEvent(event_type=RawEventType.MOUSE_UP, x=50.0, y=50.0, timestamp=1.05),
            # Rapid second click at same location -> double click
            RawEvent(event_type=RawEventType.MOUSE_DOWN, x=51.0, y=50.0, timestamp=1.20),
            RawEvent(event_type=RawEventType.MOUSE_UP, x=51.0, y=50.0, timestamp=1.25),
        ]
        spec = pipeline.process_raw_events(events, name="Double Click Demo")
        self.assertEqual(len(spec.steps), 1)
        self.assertEqual(spec.steps[0].action, ActionType.CLICK)
        self.assertEqual(spec.steps[0].payload.get("click_count"), 2)

    def test_triple_click_coalescing(self):
        """TEST-REC-07B: Rapid three clicks coalesce into a triple click."""
        pipeline = WorkflowRecorderPipeline()
        events = [
            RawEvent(event_type=RawEventType.MOUSE_DOWN, x=50.0, y=50.0, timestamp=1.0),
            RawEvent(event_type=RawEventType.MOUSE_UP, x=50.0, y=50.0, timestamp=1.05),
            RawEvent(event_type=RawEventType.MOUSE_DOWN, x=50.0, y=50.0, timestamp=1.20),
            RawEvent(event_type=RawEventType.MOUSE_UP, x=50.0, y=50.0, timestamp=1.25),
            RawEvent(event_type=RawEventType.MOUSE_DOWN, x=50.0, y=50.0, timestamp=1.40),
            RawEvent(event_type=RawEventType.MOUSE_UP, x=50.0, y=50.0, timestamp=1.45),
        ]
        spec = pipeline.process_raw_events(events, name="Triple Click Demo")
        self.assertEqual(len(spec.steps), 1)
        self.assertEqual(spec.steps[0].action, ActionType.CLICK)
        self.assertEqual(spec.steps[0].payload.get("click_count"), 3)

    def test_direct_click_coalescing(self):
        """TEST-REC-07C: Direct CLICK events coalesce into double and triple clicks."""
        pipeline = WorkflowRecorderPipeline()
        events = [
            RawEvent(event_type=RawEventType.CLICK, x=80.0, y=90.0, timestamp=2.0),
            RawEvent(event_type=RawEventType.CLICK, x=81.0, y=90.0, timestamp=2.15),
            RawEvent(event_type=RawEventType.CLICK, x=80.0, y=91.0, timestamp=2.30),
        ]
        spec = pipeline.process_raw_events(events, name="Direct Click Demo")
        self.assertEqual(len(spec.steps), 1)
        self.assertEqual(spec.steps[0].action, ActionType.CLICK)
        self.assertEqual(spec.steps[0].payload.get("click_count"), 3)

    def test_pause_clamping(self):
        """TEST-REC-08: Pauses exceeding max_pause_ms are clamped to realistic replay delays."""
        pipeline = WorkflowRecorderPipeline(max_pause_ms=500)
        # 10-second gap between clicks
        events = [
            RawInputEvent(event_type="click", x=100.0, y=100.0, timestamp=1.0),
            RawInputEvent(event_type="click", x=200.0, y=200.0, timestamp=11.0),
        ]
        spec = pipeline.compile_workflow("wf_pause", "Pause Demo", events)
        self.assertEqual(len(spec.steps), 2)
        step1 = spec.steps[0]
        step2 = spec.steps[1]
        self.assertLessEqual(step1.timing["post_delay_ms"], 500)
        self.assertLessEqual(step2.timing["pre_delay_ms"], 500)

    def test_window_relative_ratio_mapping(self):
        """TEST-REC-09: Window bounds are converted to normalized ratio coordinates."""
        pipeline = WorkflowRecorderPipeline()
        win = WindowBounds(x=100.0, y=200.0, width=400.0, height=200.0)
        # Click at (300, 300) -> norm_x = (300-100)/400 = 0.5, norm_y = (300-200)/200 = 0.5
        events = [
            RawEvent(event_type=RawEventType.MOUSE_DOWN, x=300.0, y=300.0, timestamp=1.0, window_bounds=win),
            RawEvent(event_type=RawEventType.MOUSE_UP, x=300.0, y=300.0, timestamp=1.05, window_bounds=win),
        ]
        spec = pipeline.process_raw_events(events, name="Ratio Demo")
        self.assertEqual(len(spec.steps), 1)
        self.assertEqual(spec.steps[0].target.get("norm_x"), 0.5)
        self.assertEqual(spec.steps[0].target.get("norm_y"), 0.5)
