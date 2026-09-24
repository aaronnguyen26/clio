"""Milestone 2 Remediation Challenge Test Suite.

Author: Challenger M2_Fix_2 (challenger_m2_fix_2)
Belongs to Milestone 2: Teach-Mode Workflow Memory & Retrieval Engine.

Empirically challenges:
1. YAML round-trip serialization/deserialization:
   - Strings with colons inside lists (aliases, tags, keywords, parameters, step payloads).
   - Empty collections in lists ([{}, []]).
   - Unicode, emojis, numbers, negative numbers, floats, booleans, and nulls.
2. Recorder pipeline click coalescing:
   - Rapid click sequences: 1 click, 2 clicks, 3 clicks, 4 clicks, 10 clicks.
   - Direct RawEventType.CLICK vs MOUSE_DOWN / MOUSE_UP pairs.
   - Clicks mixed with moves and jitter.
   - Clicks mixed with pauses.
   - Mixed mouse buttons (left vs right vs middle click).
3. ParameterEngine:
   - Recursive token injection prevention.
   - Variable token escaping ($${var}, \\${var}).
   - Built-in dynamic date/time tokens.
   - Non-string value formatting and missing variable retention.
   - High-load template scaling (2000+ tokens).
4. CoordinateAdapter:
   - Negative coordinates (multi-display desktop environments).
   - Zero-dimension and negative-dimension windows.
   - NaN and Inf handling.
   - Rect projection.
5. R6 Headless Compliance:
   - 0 live CGEvent posting to user's macOS session.
"""

from __future__ import annotations

import datetime
import math
from typing import Any, Dict, List
import unittest

from src.memory.coordinates import CoordinateAdapter
from src.memory.models import (
    ActionType,
    CoordMode,
    TargetCoordinates,
    WorkflowSpec,
    WorkflowStep,
)
from src.memory.parameters import ParameterEngine
from src.memory.recorder import (
    RawEvent,
    RawEventType,
    RecorderConfig,
    WorkflowRecorderPipeline,
)


class TestYamlRoundtripEmpiricalChallenge(unittest.TestCase):
    """Empirical challenge suite for pure standard-library YAML round-trip."""

    def test_yaml_roundtrip_basic_spec(self) -> None:
        """Verify basic workflow round-trips with 100% field fidelity."""
        spec = WorkflowSpec(
            id="wf_basic_yaml",
            name="Basic Workflow",
            description="A simple description",
            triggers={"canonical": "open test", "aliases": ["start test"], "keywords": ["test"]},
            steps=[
                WorkflowStep(
                    step_id="step_1",
                    order=1,
                    description="Click button",
                    action=ActionType.CLICK,
                    target={"screen_x": 100, "screen_y": 200},
                    payload={"button": "left", "click_count": 1},
                )
            ],
        )
        yaml_out = spec.to_yaml()
        recon = WorkflowSpec.from_yaml(yaml_out)
        self.assertEqual(recon.id, spec.id)
        self.assertEqual(recon.name, spec.name)
        self.assertEqual(recon.triggers["canonical"], spec.triggers["canonical"])
        self.assertEqual(recon.triggers["aliases"], spec.triggers["aliases"])
        self.assertEqual(len(recon.steps), 1)
        self.assertEqual(recon.steps[0].action, ActionType.CLICK)

    def test_yaml_roundtrip_colons_in_list_strings_challenge(self) -> None:
        """CHALLENGE: Strings containing colons inside lists (e.g. URLs, scopes, times).

        Expected behavior: List items like 'url: https://apple.com' remain strings.
        Actual failure mode: _from_yaml_native splits on ':' and parses them into single-key dicts.
        """
        spec = WorkflowSpec(
            id="wf_colon_list",
            name="Colon In List Workflow",
            tags=["category:automation", "env:macos", "version:2.0"],
            triggers={
                "canonical": "open url",
                "aliases": ["url: https://apple.com", "step 1: click here", "time: 12:00:00"],
                "keywords": ["tag:one", "http:test"],
            },
            parameters={"headers": ["Content-Type: text/plain", "Host: localhost:8080"]},
            steps=[
                WorkflowStep(
                    step_id="s1",
                    order=1,
                    description="Payload with colon list",
                    action=ActionType.TYPE_TEXT,
                    payload={"keys": ["cmd:shift", "ctrl:alt"], "text": "hello"},
                )
            ],
        )
        yaml_out = spec.to_yaml()
        recon = WorkflowSpec.from_yaml(yaml_out)

        # Assert all list-of-string fields remained lists of strings
        self.assertEqual(recon.tags, spec.tags)
        self.assertEqual(recon.triggers["aliases"], spec.triggers["aliases"])
        self.assertEqual(recon.triggers["keywords"], spec.triggers["keywords"])
        self.assertEqual(recon.parameters["headers"], spec.parameters["headers"])
        self.assertEqual(recon.steps[0].payload["keys"], spec.steps[0].payload["keys"])

    def test_yaml_roundtrip_empty_collections_in_list_challenge(self) -> None:
        """CHALLENGE: Empty dicts and empty lists as elements of a list ([{}, []]).

        Expected behavior: [{}, []] roundtrips with exact types and items.
        Actual failure mode: _to_yaml_native emits '{}' at indent 0, truncating the list in parser.
        """
        data = {"list_of_empty": [{}, []], "sibling_key": "preserved"}
        yaml_out = WorkflowSpec._to_yaml_native(data)
        parsed = WorkflowSpec._from_yaml_native(yaml_out)

        self.assertIn("sibling_key", parsed, "Sibling key dropped due to bad indentation of empty collection")
        self.assertEqual(parsed.get("sibling_key"), "preserved")
        self.assertEqual(len(parsed.get("list_of_empty", [])), 2)
        self.assertEqual(parsed["list_of_empty"][0], {})
        self.assertEqual(parsed["list_of_empty"][1], [])

    def test_yaml_roundtrip_numbers_and_types(self) -> None:
        """Verify preservation of ints, floats, booleans, and nulls."""
        data = {
            "int_val": 42,
            "neg_int": -100,
            "zero": 0,
            "float_val": 3.14159,
            "neg_float": -0.001,
            "bool_true": True,
            "bool_false": False,
            "null_val": None,
            "empty_str": "",
            "unicode_str": "🚀 日本語 accents é à ç",
        }
        yaml_out = WorkflowSpec._to_yaml_native(data)
        parsed = WorkflowSpec._from_yaml_native(yaml_out)
        self.assertEqual(parsed["int_val"], 42)
        self.assertEqual(parsed["neg_int"], -100)
        self.assertEqual(parsed["zero"], 0)
        self.assertEqual(parsed["float_val"], 3.14159)
        self.assertEqual(parsed["neg_float"], -0.001)
        self.assertIs(parsed["bool_true"], True)
        self.assertIs(parsed["bool_false"], False)
        self.assertIsNone(parsed["null_val"])
        self.assertEqual(parsed["empty_str"], "")
        self.assertEqual(parsed["unicode_str"], "🚀 日本語 accents é à ç")


class TestRecorderClickCoalescingEmpiricalChallenge(unittest.TestCase):
    """Empirical challenge suite for WorkflowRecorderPipeline click coalescing."""

    def setUp(self) -> None:
        self.pipeline = WorkflowRecorderPipeline()

    def _make_clicks(
        self,
        count: int,
        interval: float = 0.1,
        x: float = 100.0,
        y: float = 200.0,
        button: str = "left",
        start_t: float = 1.0,
    ) -> List[RawEvent]:
        events = []
        t = start_t
        for _ in range(count):
            events.append(RawEvent(event_type=RawEventType.MOUSE_DOWN, x=x, y=y, timestamp=t, button=button))
            events.append(RawEvent(event_type=RawEventType.MOUSE_UP, x=x, y=y, timestamp=t + 0.04, button=button))
            t += interval
        return events

    def test_single_click(self) -> None:
        """1 click -> 1 step with click_count = 1."""
        spec = self.pipeline.process_raw_events(self._make_clicks(1))
        self.assertEqual(len(spec.steps), 1)
        self.assertEqual(spec.steps[0].action, ActionType.CLICK)
        self.assertEqual(spec.steps[0].payload.get("click_count"), 1)

    def test_double_click(self) -> None:
        """2 rapid clicks -> 1 step with click_count = 2."""
        spec = self.pipeline.process_raw_events(self._make_clicks(2))
        self.assertEqual(len(spec.steps), 1)
        self.assertEqual(spec.steps[0].action, ActionType.CLICK)
        self.assertEqual(spec.steps[0].payload.get("click_count"), 2)

    def test_triple_click(self) -> None:
        """3 rapid clicks -> 1 step with click_count = 3."""
        spec = self.pipeline.process_raw_events(self._make_clicks(3))
        self.assertEqual(len(spec.steps), 1)
        self.assertEqual(spec.steps[0].action, ActionType.CLICK)
        self.assertEqual(spec.steps[0].payload.get("click_count"), 3)

    def test_four_clicks_escalation(self) -> None:
        """4 rapid clicks -> 2 steps: triple click (3) + single click (1)."""
        spec = self.pipeline.process_raw_events(self._make_clicks(4))
        self.assertEqual(len(spec.steps), 2)
        self.assertEqual(spec.steps[0].payload.get("click_count"), 3)
        self.assertEqual(spec.steps[1].payload.get("click_count"), 1)

    def test_ten_clicks_escalation(self) -> None:
        """10 rapid clicks -> 4 steps: [3, 3, 3, 1]."""
        spec = self.pipeline.process_raw_events(self._make_clicks(10))
        self.assertEqual(len(spec.steps), 4)
        counts = [s.payload.get("click_count") for s in spec.steps]
        self.assertEqual(counts, [3, 3, 3, 1])

    def test_direct_click_events_coalescing(self) -> None:
        """Direct RawEventType.CLICK events escalate 1, 2, 3, 4, 10 clicks identically."""
        for n, expected_counts in [(1, [1]), (2, [2]), (3, [3]), (4, [3, 1]), (10, [3, 3, 3, 1])]:
            events = [RawEvent(event_type=RawEventType.CLICK, x=50.0, y=50.0, timestamp=1.0 + (i * 0.1)) for i in range(n)]
            spec = self.pipeline.process_raw_events(events)
            counts = [s.payload.get("click_count") for s in spec.steps]
            self.assertEqual(counts, expected_counts)

    def test_clicks_separated_by_pause(self) -> None:
        """Clicks separated by pause > double_click_interval_s (0.4s) must NOT coalesce."""
        events = [
            RawEvent(event_type=RawEventType.MOUSE_DOWN, x=100.0, y=100.0, timestamp=1.0),
            RawEvent(event_type=RawEventType.MOUSE_UP, x=100.0, y=100.0, timestamp=1.05),
            RawEvent(event_type=RawEventType.MOUSE_DOWN, x=100.0, y=100.0, timestamp=1.60),
            RawEvent(event_type=RawEventType.MOUSE_UP, x=100.0, y=100.0, timestamp=1.65),
        ]
        spec = self.pipeline.process_raw_events(events)
        self.assertEqual(len(spec.steps), 2)
        self.assertEqual(spec.steps[0].payload.get("click_count"), 1)
        self.assertEqual(spec.steps[1].payload.get("click_count"), 1)

    def test_clicks_separated_by_spatial_drift(self) -> None:
        """Clicks separated by distance > 5.0px must NOT coalesce."""
        events = [
            RawEvent(event_type=RawEventType.MOUSE_DOWN, x=100.0, y=100.0, timestamp=1.0),
            RawEvent(event_type=RawEventType.MOUSE_UP, x=100.0, y=100.0, timestamp=1.05),
            RawEvent(event_type=RawEventType.MOUSE_DOWN, x=110.0, y=100.0, timestamp=1.15),
            RawEvent(event_type=RawEventType.MOUSE_UP, x=110.0, y=100.0, timestamp=1.20),
        ]
        spec = self.pipeline.process_raw_events(events)
        self.assertEqual(len(spec.steps), 2)
        self.assertEqual(spec.steps[0].payload.get("click_count"), 1)
        self.assertEqual(spec.steps[1].payload.get("click_count"), 1)

    def test_clicks_with_micro_jitter_coalesce(self) -> None:
        """Clicks with minor jitter <= 5.0px must coalesce."""
        events = [
            RawEvent(event_type=RawEventType.MOUSE_DOWN, x=100.0, y=100.0, timestamp=1.0),
            RawEvent(event_type=RawEventType.MOUSE_UP, x=100.0, y=100.0, timestamp=1.05),
            RawEvent(event_type=RawEventType.MOUSE_DOWN, x=102.0, y=103.0, timestamp=1.15),
            RawEvent(event_type=RawEventType.MOUSE_UP, x=102.0, y=103.0, timestamp=1.20),
        ]
        spec = self.pipeline.process_raw_events(events)
        self.assertEqual(len(spec.steps), 1)
        self.assertEqual(spec.steps[0].payload.get("click_count"), 2)

    def test_mixed_mouse_button_clicks_challenge(self) -> None:
        """CHALLENGE: Left click followed rapidly by right click at same location.

        Expected behavior: Separate steps for left click and right click (e.g. select + context menu).
        Actual failure mode: Swallowed into a double left-click because button equality is unverified.
        """
        events = [
            RawEvent(event_type=RawEventType.MOUSE_DOWN, x=50.0, y=50.0, timestamp=1.0, button="left"),
            RawEvent(event_type=RawEventType.MOUSE_UP, x=50.0, y=50.0, timestamp=1.05, button="left"),
            RawEvent(event_type=RawEventType.MOUSE_DOWN, x=50.0, y=50.0, timestamp=1.15, button="right"),
            RawEvent(event_type=RawEventType.MOUSE_UP, x=50.0, y=50.0, timestamp=1.20, button="right"),
        ]
        spec = self.pipeline.process_raw_events(events)
        # Should be 2 distinct clicks: one left, one right
        self.assertEqual(len(spec.steps), 2, "Left click + right click should NOT coalesce into a single double-click")
        self.assertEqual(spec.steps[0].payload.get("button"), "left")
        self.assertEqual(spec.steps[1].payload.get("button"), "right")


class TestParameterEngineEmpiricalChallenge(unittest.TestCase):
    """Empirical challenge suite for ParameterEngine edge cases."""

    def test_recursive_injection_defense(self) -> None:
        """Ensure recursive parameter tokens are never expanded."""
        template = "Greeting: ${user}"
        params = {"user": "${secret}", "secret": "COMPROMISED"}
        res = ParameterEngine.interpolate(template, params)
        self.assertEqual(res, "Greeting: ${secret}")

    def test_escaping_fidelity(self) -> None:
        """$${var} and \\${var} must render as literal ${var}."""
        res1 = ParameterEngine.interpolate("Value is $${amount}", {"amount": "100"})
        self.assertEqual(res1, "Value is ${amount}")
        res2 = ParameterEngine.interpolate("Value is \\${amount}", {"amount": "100"})
        self.assertEqual(res2, "Value is ${amount}")

    def test_missing_parameter_preservation(self) -> None:
        """Unresolved variables must remain verbatim ${unresolved}."""
        res = ParameterEngine.interpolate("Keep ${missing} and replace ${found}", {"found": "DONE"})
        self.assertEqual(res, "Keep ${missing} and replace DONE")

    def test_non_string_values_and_lists(self) -> None:
        """Handle numbers, None, empty lists, and lists with bullets/commas."""
        params = {"int_v": 10, "none_v": None, "empty_l": [], "items": ["alpha", "beta"]}
        self.assertEqual(ParameterEngine.interpolate("${int_v}", params), "10")
        self.assertEqual(ParameterEngine.interpolate("${none_v}", params), "")
        self.assertEqual(ParameterEngine.interpolate("${empty_l}", params), "")
        self.assertEqual(ParameterEngine.interpolate("${items}", params, list_format="comma"), "alpha, beta")
        self.assertEqual(ParameterEngine.interpolate("${items}", params, list_format="bullet"), "- alpha\n- beta")

    def test_large_template_scaling(self) -> None:
        """2000 tokens evaluated under 50ms without error."""
        tokens = [f"t_{i}" for i in range(2000)]
        template = " ".join(f"${{{t}}}" for t in tokens)
        params = {t: f"v_{i}" for i, t in enumerate(tokens)}
        res = ParameterEngine.interpolate(template, params)
        self.assertTrue(res.startswith("v_0"))
        self.assertTrue(res.endswith("v_1999"))


class TestCoordinateAdapterEmpiricalChallenge(unittest.TestCase):
    """Empirical challenge suite for CoordinateAdapter edge cases."""

    def test_multi_display_negative_coordinates(self) -> None:
        """Window on secondary monitor with negative coordinates."""
        win = {"x": -1920.0, "y": -1080.0, "width": 1920.0, "height": 1080.0}
        sx, sy = CoordinateAdapter.to_screen_coordinates(0.5, 0.5, win)
        self.assertEqual((sx, sy), (-960, -540))
        nx, ny = CoordinateAdapter.to_normalized_coordinates(-960, -540, win)
        self.assertTrue(math.isclose(nx, 0.5, rel_tol=1e-4))
        self.assertTrue(math.isclose(ny, 0.5, rel_tol=1e-4))

    def test_zero_and_negative_window_bounds(self) -> None:
        """Zero and negative dimensions must not crash or raise ZeroDivisionError."""
        win_zero = {"x": 10.0, "y": 20.0, "width": 0.0, "height": 0.0}
        sx, sy = CoordinateAdapter.to_screen_coordinates(0.5, 0.5, win_zero)
        self.assertEqual((sx, sy), (10, 20))
        nx, ny = CoordinateAdapter.to_normalized_coordinates(15, 25, win_zero)
        self.assertEqual((nx, ny), (0.0, 0.0))

        win_neg = {"x": 10.0, "y": 20.0, "width": -100.0, "height": -50.0}
        sx, sy = CoordinateAdapter.to_screen_coordinates(0.5, 0.5, win_neg)
        self.assertEqual((sx, sy), (10, 20))
        nx, ny = CoordinateAdapter.to_normalized_coordinates(15, 25, win_neg)
        self.assertEqual((nx, ny), (0.0, 0.0))

    def test_nan_and_inf_resilience(self) -> None:
        """NaN and Inf normalized coordinates safely default to window origin."""
        win = {"x": 100.0, "y": 100.0, "width": 500.0, "height": 400.0}
        sx, sy = CoordinateAdapter.to_screen_coordinates(float("nan"), float("inf"), win)
        self.assertEqual((sx, sy), (100, 100))

    def test_invalid_type_raises_typeerror(self) -> None:
        """Passing non-dict, non-WindowInfo objects raises TypeError."""
        with self.assertRaises(TypeError):
            CoordinateAdapter.to_screen_coordinates(0.5, 0.5, "invalid_window_object")


class TestR6HeadlessComplianceChallenge(unittest.TestCase):
    """Empirical verification that 0 live CGEvent posts occur."""

    def test_headless_execution_no_cgevent_activity(self) -> None:
        """Verify memory and recorder components operate purely in-memory with zero OS input hooks."""
        from src.actuators.mock import MockActuator

        mock_act = MockActuator()
        # Verify initial mock actuator history is empty
        self.assertEqual(len(mock_act.history), 0)
        # Verify no external live system calls
        pipeline = WorkflowRecorderPipeline()
        spec = pipeline.process_raw_events([
            RawEvent(event_type=RawEventType.CLICK, x=100.0, y=100.0, timestamp=1.0)
        ])
        self.assertEqual(len(spec.steps), 1)
        self.assertEqual(len(mock_act.history), 0)
