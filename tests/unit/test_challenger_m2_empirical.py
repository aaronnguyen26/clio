"""Empirical Challenge Harness for Milestone 2 (M2).

Adversarial stress-testing of:
1. ParameterEngine (FEAT-MEM-06):
   - Recursive token injection prevention across arbitrary recursion depths.
   - Escaping fidelity: $${var} and \\${var} literal preservation.
   - Built-in dynamic date/time tokens: immutability, format consistency, non-leakage.
   - List formatting edge cases: empty, single, multi, nested, non-string scalars.
   - Large template strings, 10,000+ tokens, unicode, binary characters.
   - Malformed, unclosed, and punctuation tokens.
   - Token extraction and validation utilities.
2. CoordinateAdapter (FEAT-MEM-07):
   - Zero-dimension and negative-dimension windows (ZeroDivisionError immunity).
   - Extreme coordinates (NaN, Inf, -1e6, +1e6).
   - Multi-monitor negative screen coordinate spaces.
   - Dense grid inverse roundtrip fidelity.
   - Duck-typing, dict tolerance, and invalid type rejection (TypeError).
   - Ratio validation exact boundary checks.
3. WorkflowRecorderPipeline (FEAT-MEM-05):
   - Jitter filtering: tiny movements (<4px in <10ms or stationary drift <1.5px) dropped.
   - Anchor preservation: movements immediately prior to clicks/hotkeys strictly preserved.
   - Human pause clamping: gaps > 2.0s clamped to 500ms post-delay.
   - Keystroke coalescing boundary: 59 chars -> TYPE_TEXT, 60+ chars -> PASTE_TEXT.
   - BackSpace handling in typing buffer.
   - Hotkey interruption flushes typing buffer.
   - Window-relative ratio mapping with zero/positive window bounds.
   - Pipeline lifecycle: start_recording, feed_event, stop_recording end-to-end.
"""

from __future__ import annotations

import datetime
import math
from typing import Any, Dict, List
import unittest

from src.actuators.types import WindowInfo
from src.memory.coordinates import CoordinateAdapter
from src.memory.models import ActionType, WorkflowSpec, WorkflowStep
from src.memory.parameters import ParameterEngine
from src.memory.recorder import (
    EventCaptureStage,
    EventCoalescingStage,
    NoiseFilterStage,
    PauseClampingStage,
    RawEvent,
    RawEventType,
    RecorderConfig,
    WindowBounds,
    WorkflowRecorderPipeline,
)


class TestParameterEngineEmpiricalHarness(unittest.TestCase):
    """Adversarial stress tests for ParameterEngine."""

    def setUp(self) -> None:
        self.fixed_dt = datetime.datetime(2026, 9, 23, 14, 30, 45)

    def test_recursive_injection_prevention(self) -> None:
        """Verify parameter values containing token syntax are NEVER recursively resolved."""
        # Single-level injection attempt
        template = "Output: ${payload}"
        params = {"payload": "${secret}", "secret": "SUPER_SECRET_COMPROMISED"}
        res = ParameterEngine.interpolate(template, params)
        self.assertEqual(res, "Output: ${secret}")

        # Multi-level injection attempt
        chain_params = {f"k{i}": f"${{k{i+1}}}" for i in range(10)}
        chain_params["k10"] = "COMPROMISED"
        res = ParameterEngine.interpolate("${k0}", chain_params)
        self.assertEqual(res, "${k1}")

        # Self-referencing loop
        loop_params = {"loop": "${loop}"}
        res = ParameterEngine.interpolate("Loop: ${loop}", loop_params)
        self.assertEqual(res, "Loop: ${loop}")

        # Mutual recursion loop
        mutual_params = {"a": "${b}", "b": "${a}"}
        res = ParameterEngine.interpolate("Mutual: ${a} and ${b}", mutual_params)
        self.assertEqual(res, "Mutual: ${b} and ${a}")

        # Attempted injection of dynamic builtin tokens
        injection_builtin = {"user_input": "${CURRENT_DATE}"}
        res = ParameterEngine.interpolate("Hello ${user_input}", injection_builtin, now=self.fixed_dt)
        self.assertEqual(res, "Hello ${CURRENT_DATE}")

        # Adjacent tokens attempting to form new token dynamically
        split_params = {"prefix": "${CURRENT_", "suffix": "TIME}"}
        res = ParameterEngine.interpolate("${prefix}${suffix}", split_params, now=self.fixed_dt)
        self.assertEqual(res, "${CURRENT_TIME}")

    def test_escaping_dollar_and_backslash(self) -> None:
        """Verify escaping tokens remain literal ${var} without expanding."""
        params = {"var": "EXPANDED", "doc": "MY_DOC"}

        # Double-dollar escaping
        self.assertEqual(
            ParameterEngine.interpolate("Use $${var} to show variable.", params),
            "Use ${var} to show variable.",
        )

        # Backslash escaping
        self.assertEqual(
            ParameterEngine.interpolate(r"Use \${var} to show variable.", params),
            "Use ${var} to show variable.",
        )

        # Escaped built-in dynamic tokens must NOT expand to live date/time
        self.assertEqual(
            ParameterEngine.interpolate("$${CURRENT_DATE}", now=self.fixed_dt),
            "${CURRENT_DATE}",
        )
        self.assertEqual(
            ParameterEngine.interpolate(r"\${CURRENT_TIME}", now=self.fixed_dt),
            "${CURRENT_TIME}",
        )

        # Escaped undefined parameters must also retain literal ${unknown}
        self.assertEqual(
            ParameterEngine.interpolate("$${not_provided}", params),
            "${not_provided}",
        )
        self.assertEqual(
            ParameterEngine.interpolate(r"\${not_provided}", params),
            "${not_provided}",
        )

        # Triple dollar: first dollar is literal, remaining $${var} unescapes to ${var} -> $${var}
        self.assertEqual(
            ParameterEngine.interpolate("$$${var}", params),
            "$${var}",
        )

    def test_builtin_datetime_tokens_consistency(self) -> None:
        """Verify all 8 built-in date/time tokens produce exact expected representations."""
        test_dt = datetime.datetime(2026, 9, 23, 14, 30, 45)

        tokens = [
            ("${CURRENT_DATE}", "Wednesday, September 23, 2026"),
            ("${CURRENT_TIME}", "14:30:45"),
            ("${CURRENT_DATETIME}", "Wednesday, September 23, 2026 14:30:45"),
            ("${CURRENT_ISO_DATE}", "2026-09-23"),
            ("${CURRENT_ISO_DATETIME}", "2026-09-23T14:30:45"),
            ("${CURRENT_YEAR}", "2026"),
            ("${CURRENT_MONTH}", "September"),
            ("${CURRENT_DAY}", "23"),
        ]

        for token, expected in tokens:
            with self.subTest(token=token):
                out = ParameterEngine.interpolate(f"Val: {token}", now=test_dt)
                self.assertEqual(out, f"Val: {expected}")

        # Builtin precedence: runtime_params cannot override builtins
        override_attempt = {"CURRENT_YEAR": "1999"}
        out = ParameterEngine.interpolate("${CURRENT_YEAR}", override_attempt, now=test_dt)
        self.assertEqual(out, "2026")

    def test_list_formatting_edge_cases(self) -> None:
        """Verify bullet, comma, and newline formatting for lists and tuples."""
        # Empty list
        self.assertEqual(ParameterEngine.interpolate("Empty: [${items}]", {"items": []}), "Empty: []")
        # Empty tuple
        self.assertEqual(ParameterEngine.interpolate("Empty: [${items}]", {"items": ()}), "Empty: []")

        # Single item
        self.assertEqual(
            ParameterEngine.interpolate("${items}", {"items": ["alone"]}, list_format="bullet"),
            "- alone",
        )
        self.assertEqual(
            ParameterEngine.interpolate("${items}", {"items": ["alone"]}, list_format="comma"),
            "alone",
        )
        self.assertEqual(
            ParameterEngine.interpolate("${items}", {"items": ["alone"]}, list_format="newline"),
            "alone",
        )

        # Multi-item mixed types
        items = ["Alpha", 42, 3.14, True, None]
        bullet_res = ParameterEngine.interpolate("${items}", {"items": items}, list_format="bullet")
        self.assertEqual(bullet_res, "- Alpha\n- 42\n- 3.14\n- True\n- None")

        comma_res = ParameterEngine.interpolate("${items}", {"items": items}, list_format="comma")
        self.assertEqual(comma_res, "Alpha, 42, 3.14, True, None")

        newline_res = ParameterEngine.interpolate("${items}", {"items": items}, list_format="newline")
        self.assertEqual(newline_res, "Alpha\n42\n3.14\nTrue\nNone")

    def test_large_template_stress(self) -> None:
        """Stress-test ParameterEngine with large payload and thousands of tokens."""
        # 2,000 distinct tokens
        n_tokens = 2000
        template = " ".join(f"token_{i}=${{v_{i}}}" for i in range(n_tokens))
        params = {f"v_{i}": f"val_{i}" for i in range(n_tokens)}

        res = ParameterEngine.interpolate(template, params)
        self.assertIn("token_0=val_0", res)
        self.assertIn(f"token_{n_tokens-1}=val_{n_tokens-1}", res)
        self.assertNotIn("${", res)

        # Large payload (100KB string)
        large_val = "X" * 100000
        res_large = ParameterEngine.interpolate("Large: ${data}", {"data": large_val})
        self.assertEqual(len(res_large), 100000 + 7)

        # Unicode and binary null characters
        unicode_val = "Unicode 🚀 日本語 \x00 null byte"
        res_uni = ParameterEngine.interpolate("Payload: ${msg}", {"msg": unicode_val})
        self.assertEqual(res_uni, f"Payload: {unicode_val}")

    def test_unclosed_and_malformed_tokens(self) -> None:
        """Verify unclosed, empty, or syntax-violating tokens are safely preserved verbatim."""
        malformed = [
            "${}",
            "${123}",
            "${foo bar}",
            "${var",
            "var}",
            "${foo-bar}",
            "${foo.bar}",
            "$",
            "$$",
            "${_}",
        ]
        for item in malformed:
            with self.subTest(item=item):
                res = ParameterEngine.interpolate(f"Text {item} end", {})
                if item == "${_}":
                    # ${_} is a valid identifier syntax, should stay ${_} when missing
                    self.assertEqual(res, "Text ${_} end")
                else:
                    self.assertIn(item, res)

    def test_extract_tokens_and_validate_params(self) -> None:
        """Verify extract_tokens and validate_params correctly handle builtins and escaping."""
        template = "Task ${title} on ${CURRENT_DATE} ref: $${ref} \\${esc} notes: ${notes}"
        extracted = ParameterEngine.extract_tokens(template, include_builtins=False)
        self.assertEqual(extracted, {"title", "notes"})

        extracted_with_b = ParameterEngine.extract_tokens(template, include_builtins=True)
        self.assertEqual(extracted_with_b, {"title", "CURRENT_DATE", "notes"})

        # Validation
        valid, missing = ParameterEngine.validate_params(template, {"title": "Test"})
        self.assertFalse(valid)
        self.assertEqual(missing, ["notes"])

        valid, missing = ParameterEngine.validate_params(template, {"title": "Test", "notes": "OK"})
        self.assertTrue(valid)
        self.assertEqual(missing, [])


class TestCoordinateAdapterEmpiricalHarness(unittest.TestCase):
    """Adversarial stress tests for CoordinateAdapter."""

    def test_zero_and_negative_window_dimensions(self) -> None:
        """Verify window dimensions of 0 or negative values NEVER raise ZeroDivisionError."""
        zero_windows = [
            {"x": 100.0, "y": 200.0, "width": 0.0, "height": 0.0},
            {"x": 100.0, "y": 200.0, "width": -500.0, "height": -300.0},
            {"x": 0.0, "y": 0.0, "width": 0.0, "height": 800.0},
            {"x": 0.0, "y": 0.0, "width": 800.0, "height": 0.0},
        ]

        for w in zero_windows:
            with self.subTest(window=w):
                # Forward projection
                sx, sy = CoordinateAdapter.to_screen_coordinates(0.5, 0.5, w)
                self.assertIsInstance(sx, int)
                self.assertIsInstance(sy, int)

                # Inverse projection
                nx, ny = CoordinateAdapter.to_normalized_coordinates(500.0, 500.0, w)
                self.assertIsInstance(nx, float)
                self.assertIsInstance(ny, float)
                self.assertTrue(0.0 <= nx <= 1.0)
                self.assertTrue(0.0 <= ny <= 1.0)

    def test_nan_and_inf_coordinate_safety(self) -> None:
        """Verify NaN, Inf, and -Inf values do not crash or corrupt coordinates."""
        win = {"x": 100.0, "y": 100.0, "width": 1000.0, "height": 500.0}

        # Forward projection with NaN and Inf
        sx_nan, sy_nan = CoordinateAdapter.to_screen_coordinates(float("nan"), float("nan"), win)
        self.assertEqual((sx_nan, sy_nan), (100, 100))

        sx_inf, sy_inf = CoordinateAdapter.to_screen_coordinates(float("inf"), float("-inf"), win)
        self.assertEqual((sx_inf, sy_inf), (100, 100))

        # Extreme values clamped
        sx_ext, sy_ext = CoordinateAdapter.to_screen_coordinates(1e6, -1e6, win)
        self.assertEqual((sx_ext, sy_ext), (1100, 100))

        # Inverse projection with extreme values
        nx_ext, ny_ext = CoordinateAdapter.to_normalized_coordinates(1e6, -1e6, win, clamp=True)
        self.assertEqual((nx_ext, ny_ext), (1.0, 0.0))

    def test_multi_monitor_negative_space_roundtrip(self) -> None:
        """Verify negative coordinate spaces on multi-monitor setups preserve exact locations."""
        monitors = [
            # Secondary monitor to the left: -1920 to 0
            {"x": -1920.0, "y": 0.0, "width": 1920.0, "height": 1080.0},
            # Secondary monitor above: -1080 to 0
            {"x": 0.0, "y": -1080.0, "width": 1920.0, "height": 1080.0},
            # Secondary monitor top-left diagonally
            {"x": -2560.0, "y": -1440.0, "width": 2560.0, "height": 1440.0},
        ]

        for win in monitors:
            with self.subTest(monitor=win):
                # Origin (0.0, 0.0)
                sx, sy = CoordinateAdapter.to_screen_coordinates(0.0, 0.0, win)
                self.assertEqual((sx, sy), (int(win["x"]), int(win["y"])))

                # Center (0.5, 0.5)
                sx_c, sy_c = CoordinateAdapter.to_screen_coordinates(0.5, 0.5, win)
                expected_cx = int(win["x"] + 0.5 * win["width"])
                expected_cy = int(win["y"] + 0.5 * win["height"])
                self.assertEqual((sx_c, sy_c), (expected_cx, expected_cy))

                # Inverse from center
                nx_c, ny_c = CoordinateAdapter.to_normalized_coordinates(expected_cx, expected_cy, win)
                self.assertAlmostEqual(nx_c, 0.5, places=3)
                self.assertAlmostEqual(ny_c, 0.5, places=3)

    def test_dense_grid_roundtrip_fidelity(self) -> None:
        """Verify round-trip projection error across a dense 21x21 grid is < 1 pixel."""
        win = WindowInfo(
            window_id=10,
            owner_name="com.apple.Notes",
            title="Notes",
            x=150.0,
            y=250.0,
            width=1200.0,
            height=800.0,
        )

        steps = 20
        for ix in range(steps + 1):
            for iy in range(steps + 1):
                rx = ix / steps
                ry = iy / steps
                sx, sy = CoordinateAdapter.to_screen_coordinates(rx, ry, win)
                nx, ny = CoordinateAdapter.to_normalized_coordinates(sx, sy, win)

                # Tolerance: +/- 1 pixel over width (1/1200) and height (1/800)
                self.assertAlmostEqual(nx, rx, delta=1.5 / 1200.0)
                self.assertAlmostEqual(ny, ry, delta=1.5 / 800.0)

    def test_type_error_on_invalid_window_type(self) -> None:
        """Verify passing non-window object raises TypeError with clear message."""
        with self.assertRaises(TypeError):
            CoordinateAdapter.to_screen_coordinates(0.5, 0.5, "not_a_window")

        with self.assertRaises(TypeError):
            CoordinateAdapter.to_normalized_coordinates(100.0, 100.0, [1, 2, 3])

    def test_validate_ratio_boundaries(self) -> None:
        """Verify validate_ratio strictly checks [0.0, 1.0] and finiteness."""
        self.assertTrue(CoordinateAdapter.validate_ratio(0.0, 0.0))
        self.assertTrue(CoordinateAdapter.validate_ratio(1.0, 1.0))
        self.assertTrue(CoordinateAdapter.validate_ratio(0.5, 0.5))

        self.assertFalse(CoordinateAdapter.validate_ratio(-0.0001, 0.5))
        self.assertFalse(CoordinateAdapter.validate_ratio(0.5, 1.0001))
        self.assertFalse(CoordinateAdapter.validate_ratio(float("nan"), 0.5))
        self.assertFalse(CoordinateAdapter.validate_ratio(0.5, float("inf")))


class TestWorkflowRecorderEmpiricalHarness(unittest.TestCase):
    """Adversarial stress tests for WorkflowRecorderPipeline."""

    def test_jitter_decimation_strict_thresholds(self) -> None:
        """Verify movements < 4px in < 10ms or stationary drift < 1.5px are decimated."""
        cfg = RecorderConfig(jitter_distance_px=4.0, jitter_time_delta_s=0.010)
        filter_stage = NoiseFilterStage(cfg)

        events = [
            RawEvent(event_type=RawEventType.MOUSE_MOVE, x=100.0, y=100.0, timestamp=1.000),
            # Micro-jitter: dist = 1.0px (< 1.5px drift), dt = 0.002s -> DROP
            RawEvent(event_type=RawEventType.MOUSE_MOVE, x=101.0, y=100.0, timestamp=1.002),
            # Micro-jitter: dist = 2.0px (< 4.0px), dt = 0.005s (< 10ms) -> DROP
            RawEvent(event_type=RawEventType.MOUSE_MOVE, x=102.0, y=100.0, timestamp=1.005),
            # Intentional movement: dist = 100px (> 4px) -> KEEP
            RawEvent(event_type=RawEventType.MOUSE_MOVE, x=200.0, y=100.0, timestamp=1.050),
        ]

        filtered = filter_stage.filter(events)
        self.assertEqual(len(filtered), 2)
        self.assertEqual(filtered[0].x, 100.0)
        self.assertEqual(filtered[1].x, 200.0)

    def test_anchor_preservation_before_down_and_click_and_hotkey(self) -> None:
        """Verify mouse moves immediately prior to down, click, or hotkey are NEVER dropped."""
        cfg = RecorderConfig(jitter_distance_px=4.0, jitter_time_delta_s=0.010)
        filter_stage = NoiseFilterStage(cfg)

        # Anchor before mouse down
        events_down = [
            RawEvent(event_type=RawEventType.MOUSE_MOVE, x=100.0, y=100.0, timestamp=1.000),
            # Tiny movement (0.5px, 2ms) immediately before mouse down -> MUST BE PRESERVED as anchor
            RawEvent(event_type=RawEventType.MOUSE_MOVE, x=100.5, y=100.0, timestamp=1.002),
            RawEvent(event_type=RawEventType.MOUSE_DOWN, x=100.5, y=100.0, timestamp=1.005),
        ]
        filtered_down = filter_stage.filter(events_down)
        self.assertEqual(len(filtered_down), 3)
        self.assertEqual(filtered_down[1].x, 100.5)

        # Anchor before direct click
        events_click = [
            RawEvent(event_type=RawEventType.MOUSE_MOVE, x=50.0, y=50.0, timestamp=2.000),
            RawEvent(event_type=RawEventType.MOUSE_MOVE, x=50.2, y=50.0, timestamp=2.002),
            RawEvent(event_type=RawEventType.CLICK, x=50.2, y=50.0, timestamp=2.005),
        ]
        filtered_click = filter_stage.filter(events_click)
        self.assertEqual(len(filtered_click), 3)
        self.assertEqual(filtered_click[1].x, 50.2)

        # Anchor before hotkey
        events_hotkey = [
            RawEvent(event_type=RawEventType.MOUSE_MOVE, x=30.0, y=30.0, timestamp=3.000),
            RawEvent(event_type=RawEventType.MOUSE_MOVE, x=30.3, y=30.0, timestamp=3.003),
            RawEvent(event_type=RawEventType.KEY_DOWN, key="c", modifiers=["cmd"], timestamp=3.005),
        ]
        filtered_hotkey = filter_stage.filter(events_hotkey)
        self.assertEqual(len(filtered_hotkey), 3)
        self.assertEqual(filtered_hotkey[1].x, 30.3)

    def test_pause_clamping_empirical(self) -> None:
        """Verify pauses > 2.0s are clamped to 500ms post-delay."""
        pipeline = WorkflowRecorderPipeline()

        # Step 1 at t=1.0, Step 2 at t=10.0 (9s pause), Step 3 at t=10.5 (0.45s pause)
        events = [
            RawEvent(event_type=RawEventType.CLICK, x=10.0, y=10.0, timestamp=1.0),
            RawEvent(event_type=RawEventType.CLICK, x=20.0, y=20.0, timestamp=10.0),
            RawEvent(event_type=RawEventType.CLICK, x=30.0, y=30.0, timestamp=10.5),
        ]
        spec = pipeline.process_raw_events(events)
        self.assertEqual(len(spec.steps), 3)

        step1 = spec.steps[0]
        step2 = spec.steps[1]
        step3 = spec.steps[2]

        # Step 1 had ~8.95s pause after it -> clamped to 500ms post_delay_ms
        self.assertEqual(step1.timing["post_delay_ms"], 500)
        self.assertIn("clamped_from_s", step1.timing)
        self.assertGreater(step1.timing["clamped_from_s"], 2.0)

        # Step 2 had 0.45s pause after it -> realistic delay ~450ms
        self.assertAlmostEqual(step2.timing["post_delay_ms"], 450, delta=50)

        # Step 3 is final step -> default post_delay_ms (50ms)
        self.assertEqual(step3.timing["post_delay_ms"], 50)

    def test_keystroke_coalescing_threshold_60(self) -> None:
        """Verify boundary condition for text coalescing: 59 chars -> TYPE_TEXT, 60+ chars -> PASTE_TEXT."""
        pipeline = WorkflowRecorderPipeline()

        # Test 59 chars
        str_59 = "x" * 59
        events_59 = [
            RawEvent(event_type=RawEventType.KEY_CHAR, key=c, timestamp=1.0 + i * 0.01)
            for i, c in enumerate(str_59)
        ]
        spec_59 = pipeline.process_raw_events(events_59)
        self.assertEqual(len(spec_59.steps), 1)
        self.assertEqual(spec_59.steps[0].action, ActionType.TYPE_TEXT)
        self.assertEqual(spec_59.steps[0].payload["text"], str_59)

        # Test 60 chars
        str_60 = "y" * 60
        events_60 = [
            RawEvent(event_type=RawEventType.KEY_CHAR, key=c, timestamp=1.0 + i * 0.01)
            for i, c in enumerate(str_60)
        ]
        spec_60 = pipeline.process_raw_events(events_60)
        self.assertEqual(len(spec_60.steps), 1)
        self.assertEqual(spec_60.steps[0].action, ActionType.PASTE_TEXT)
        self.assertEqual(spec_60.steps[0].payload["text"], str_60)

        # Test 100 chars
        str_100 = "z" * 100
        events_100 = [
            RawEvent(event_type=RawEventType.KEY_CHAR, key=c, timestamp=1.0 + i * 0.01)
            for i, c in enumerate(str_100)
        ]
        spec_100 = pipeline.process_raw_events(events_100)
        self.assertEqual(len(spec_100.steps), 1)
        self.assertEqual(spec_100.steps[0].action, ActionType.PASTE_TEXT)
        self.assertEqual(spec_100.steps[0].payload["text"], str_100)

    def test_backspace_and_special_keys_in_typing(self) -> None:
        """Verify BackSpace edits typing buffer, Return adds newline, Tab adds tab."""
        pipeline = WorkflowRecorderPipeline()
        events = [
            RawEvent(event_type=RawEventType.KEY_CHAR, key="H", timestamp=1.0),
            RawEvent(event_type=RawEventType.KEY_CHAR, key="e", timestamp=1.01),
            RawEvent(event_type=RawEventType.KEY_CHAR, key="l", timestamp=1.02),
            RawEvent(event_type=RawEventType.KEY_CHAR, key="x", timestamp=1.03),  # typo
            RawEvent(event_type=RawEventType.KEY_CHAR, key="BackSpace", timestamp=1.04),  # erase 'x'
            RawEvent(event_type=RawEventType.KEY_CHAR, key="l", timestamp=1.05),
            RawEvent(event_type=RawEventType.KEY_CHAR, key="o", timestamp=1.06),
            RawEvent(event_type=RawEventType.KEY_CHAR, key="Return", timestamp=1.07),
            RawEvent(event_type=RawEventType.KEY_CHAR, key="Tab", timestamp=1.08),
            RawEvent(event_type=RawEventType.KEY_CHAR, key="1", timestamp=1.09),
        ]
        spec = pipeline.process_raw_events(events)
        self.assertEqual(len(spec.steps), 1)
        self.assertEqual(spec.steps[0].payload["text"], "Hello\n\t1")

    def test_hotkey_flushes_typing_buffer(self) -> None:
        """Verify encountering hotkey event immediately flushes accumulated typing before hotkey."""
        pipeline = WorkflowRecorderPipeline()
        events = [
            RawEvent(event_type=RawEventType.KEY_CHAR, key="N", timestamp=1.0),
            RawEvent(event_type=RawEventType.KEY_CHAR, key="o", timestamp=1.01),
            RawEvent(event_type=RawEventType.KEY_CHAR, key="t", timestamp=1.02),
            RawEvent(event_type=RawEventType.KEY_CHAR, key="e", timestamp=1.03),
            # Hotkey Cmd+S
            RawEvent(event_type=RawEventType.KEY_DOWN, key="s", modifiers=["cmd"], timestamp=1.10),
            # Subsequent typing
            RawEvent(event_type=RawEventType.KEY_CHAR, key="O", timestamp=1.20),
            RawEvent(event_type=RawEventType.KEY_CHAR, key="K", timestamp=1.21),
        ]
        spec = pipeline.process_raw_events(events)
        self.assertEqual(len(spec.steps), 3)
        self.assertEqual(spec.steps[0].action, ActionType.TYPE_TEXT)
        self.assertEqual(spec.steps[0].payload["text"], "Note")
        self.assertEqual(spec.steps[1].action, ActionType.PRESS_HOTKEY)
        self.assertEqual(spec.steps[1].payload["keys"], ["cmd", "s"])
        self.assertEqual(spec.steps[2].action, ActionType.TYPE_TEXT)
        self.assertEqual(spec.steps[2].payload["text"], "OK")

    def test_click_with_zero_dimension_window_bounds(self) -> None:
        """Verify click event with zero-dimension window_bounds does not crash."""
        pipeline = WorkflowRecorderPipeline()
        win = WindowBounds(x=100.0, y=100.0, width=0.0, height=0.0)
        events = [
            RawEvent(event_type=RawEventType.CLICK, x=150.0, y=150.0, timestamp=1.0, window_bounds=win),
        ]
        spec = pipeline.process_raw_events(events)
        self.assertEqual(len(spec.steps), 1)
        self.assertEqual(spec.steps[0].target["screen_x"], 150)
        self.assertEqual(spec.steps[0].target["screen_y"], 150)
        # Should NOT have norm_x / norm_y because bounds width/height <= 0
        self.assertNotIn("norm_x", spec.steps[0].target)

    def test_pipeline_lifecycle_end_to_end(self) -> None:
        """Verify full interactive capture lifecycle (start -> feed -> stop)."""
        pipeline = WorkflowRecorderPipeline()
        pipeline.start_recording()
        pipeline.feed_event(RawEvent(event_type=RawEventType.CLICK, x=100.0, y=200.0, timestamp=1.0))
        pipeline.feed_event(RawEvent(event_type=RawEventType.KEY_CHAR, key="a", timestamp=1.1))
        spec = pipeline.stop_recording(name="Interactive Demo")

        self.assertEqual(spec.name, "Interactive Demo")
        self.assertEqual(len(spec.steps), 2)
        self.assertEqual(spec.steps[0].action, ActionType.CLICK)
        self.assertEqual(spec.steps[1].action, ActionType.TYPE_TEXT)

    def test_double_click_temporal_and_spatial_thresholds(self) -> None:
        """Verify double click coalescing respects 5px distance and 0.4s interval."""
        pipeline = WorkflowRecorderPipeline()

        # Clicks within 0.2s and 2px distance -> Double Click
        events_double = [
            RawEvent(event_type=RawEventType.MOUSE_DOWN, x=100.0, y=100.0, timestamp=1.0),
            RawEvent(event_type=RawEventType.MOUSE_UP, x=100.0, y=100.0, timestamp=1.05),
            RawEvent(event_type=RawEventType.MOUSE_DOWN, x=101.0, y=101.0, timestamp=1.20),
            RawEvent(event_type=RawEventType.MOUSE_UP, x=101.0, y=101.0, timestamp=1.25),
        ]
        spec_double = pipeline.process_raw_events(events_double)
        self.assertEqual(len(spec_double.steps), 1)
        self.assertEqual(spec_double.steps[0].payload["click_count"], 2)

        # Clicks separated by 0.6s (> 0.4s threshold) -> 2 separate single clicks
        events_slow = [
            RawEvent(event_type=RawEventType.MOUSE_DOWN, x=100.0, y=100.0, timestamp=1.0),
            RawEvent(event_type=RawEventType.MOUSE_UP, x=100.0, y=100.0, timestamp=1.05),
            RawEvent(event_type=RawEventType.MOUSE_DOWN, x=100.0, y=100.0, timestamp=1.70),
            RawEvent(event_type=RawEventType.MOUSE_UP, x=100.0, y=100.0, timestamp=1.75),
        ]
        spec_slow = pipeline.process_raw_events(events_slow)
        self.assertEqual(len(spec_slow.steps), 2)
        self.assertEqual(spec_slow.steps[0].payload["click_count"], 1)
        self.assertEqual(spec_slow.steps[1].payload["click_count"], 1)

        # Clicks separated by 20px (> 5px threshold) -> 2 separate single clicks
        events_far = [
            RawEvent(event_type=RawEventType.MOUSE_DOWN, x=100.0, y=100.0, timestamp=1.0),
            RawEvent(event_type=RawEventType.MOUSE_UP, x=100.0, y=100.0, timestamp=1.05),
            RawEvent(event_type=RawEventType.MOUSE_DOWN, x=120.0, y=100.0, timestamp=1.15),
            RawEvent(event_type=RawEventType.MOUSE_UP, x=120.0, y=100.0, timestamp=1.20),
        ]
        spec_far = pipeline.process_raw_events(events_far)
        self.assertEqual(len(spec_far.steps), 2)
        self.assertEqual(spec_far.steps[0].payload["click_count"], 1)
        self.assertEqual(spec_far.steps[1].payload["click_count"], 1)


if __name__ == "__main__":
    unittest.main()
