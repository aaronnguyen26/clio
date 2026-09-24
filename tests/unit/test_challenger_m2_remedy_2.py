"""Milestone 2 Remedy 2 Empirical Challenger Test Suite.

Author: Challenger M2 Remedy 2 (Replacement)
Target Milestone: M2 Teach-Mode Workflow Memory & Retrieval Engine Remediation

Empirical Verification and Stress Testing:
1. Deep YAML serialization roundtrips for arbitrary nested structures containing
   empty dicts, empty lists, sibling keys, colons, unicode, and multiline text.
2. Click coalescing boundary values: exact timing <= 0.4s and > 0.4s, distance <= 5px and > 5px,
   button matching across all permutations.
3. Retrieval fault isolation: corrupt specs, foreign types, missing keys, FTS5 operator injections,
   and uncorrupted retrieval of valid workflows.
4. Zero live desktop event dispatch (R6 compliance verification).
"""

from __future__ import annotations

import json
import math
from typing import Any, Dict, List, Optional
import unittest
from unittest.mock import MagicMock, patch

from src.actuators.mock import MockActuator
from src.actuators.types import MouseButton
from src.memory.engine import TaskMemoryEngine
from src.memory.models import (
    ActionType,
    ExecutionRecord,
    MatchResult,
    TargetCoordinates,
    ValidationError,
    WorkflowSpec,
    WorkflowStep,
)
from src.memory.recorder import (
    RawEvent,
    RawEventType,
    RecorderConfig,
    WorkflowRecorderPipeline,
)
from src.memory.retrieval import NLRetrievalEngine


# ==============================================================================
# Challenge Area 1: Deep YAML Serialization Roundtrips
# ==============================================================================


class TestDeepYamlRoundtripEmpiricalChallenge(unittest.TestCase):
    """Adversarial stress-testing of pure Python YAML serialization and deserialization."""

    def test_yaml_roundtrip_deep_nested_arbitrary_structures(self) -> None:
        """Arbitrary 7-level nested dictionaries and lists containing empty collections and sibling keys."""
        complex_data = {
            "root_key": "root_value",
            "empty_dict_root": {},
            "sibling_1": "after_empty_dict",
            "empty_list_root": [],
            "sibling_2": "after_empty_list",
            "level_1": {
                "l1_empty_dict": {},
                "l1_sibling": "val_1",
                "level_2": {
                    "level_3": [
                        {},
                        [],
                        {
                            "level_4": {
                                "level_5": [
                                    {"level_6": {"level_7": "deep_leaf_value"}},
                                    {},
                                    [],
                                    {"sibling_leaf": 42},
                                ]
                            }
                        },
                        [],
                        {},
                    ]
                },
                "l1_final_sibling": "val_final",
            },
            "root_final": "end_of_tree",
        }

        yaml_str = WorkflowSpec._to_yaml_native(complex_data)
        reconstructed = WorkflowSpec._from_yaml_native(yaml_str)

        self.assertEqual(reconstructed, complex_data)
        # Explicitly verify empty collections and siblings at multiple levels
        self.assertEqual(reconstructed["empty_dict_root"], {})
        self.assertEqual(reconstructed["empty_list_root"], [])
        self.assertEqual(reconstructed["sibling_1"], "after_empty_dict")
        self.assertEqual(reconstructed["sibling_2"], "after_empty_list")
        self.assertEqual(reconstructed["level_1"]["l1_empty_dict"], {})
        self.assertEqual(reconstructed["level_1"]["l1_final_sibling"], "val_final")
        l3 = reconstructed["level_1"]["level_2"]["level_3"]
        self.assertEqual(l3[0], {})
        self.assertEqual(l3[1], [])
        self.assertEqual(l3[3], [])
        self.assertEqual(l3[4], {})
        l5 = l3[2]["level_4"]["level_5"]
        self.assertEqual(l5[0]["level_6"]["level_7"], "deep_leaf_value")
        self.assertEqual(l5[1], {})
        self.assertEqual(l5[2], [])
        self.assertEqual(l5[3]["sibling_leaf"], 42)

    def test_yaml_roundtrip_colons_exhaustive_fuzzing(self) -> None:
        """Exhaustive validation of colons in strings across list items, mapping values, and nested payloads."""
        colon_strings = [
            "url: https://apple.com:443/notes?category=work:urgent#ref:123",
            "http://127.0.0.1:8080/api/v1/resource:action",
            "tag:productivity",
            "scope:internal:admin",
            "12:30:45",
            "::1",
            ":leading_colon",
            "trailing_colon:",
            "colon:no_space:after",
            "colon: with space: and more: text",
            "key: 'quoted: value'",
            'double: "quoted: value"',
            "Content-Type: application/json; charset=utf-8",
        ]

        spec = WorkflowSpec(
            id="wf_colon_fuzz",
            name="Colon Fuzzing Workflow",
            tags=colon_strings,
            triggers={
                "canonical": "open url: https://apple.com",
                "aliases": colon_strings,
                "keywords": [f"prefix:{s}" for s in colon_strings[:4]],
            },
            parameters={f"param_{i}": val for i, val in enumerate(colon_strings)},
            steps=[
                WorkflowStep(
                    step_id="step_1",
                    order=1,
                    description="Payload with all colon strings",
                    action=ActionType.TYPE_TEXT,
                    payload={"items": colon_strings, "single_colon": "key: value"},
                )
            ],
        )

        yaml_str = spec.to_yaml()
        recon = WorkflowSpec.from_yaml(yaml_str)

        self.assertEqual(recon.tags, colon_strings)
        self.assertEqual(recon.triggers["aliases"], colon_strings)
        for i, val in enumerate(colon_strings):
            self.assertEqual(recon.parameters[f"param_{i}"], val)
        self.assertEqual(recon.steps[0].payload["items"], colon_strings)
        self.assertEqual(recon.steps[0].payload["single_colon"], "key: value")

    def test_yaml_roundtrip_unicode_and_emojis(self) -> None:
        """Verify preservation of diverse unicode codepoints, emojis, and non-ASCII scripts."""
        unicode_samples = [
            "Hello World",
            "🚀 🌟 🎯 🤖 💻 📝",  # Standard emojis
            "👨‍👩‍👧‍👦 👩🏽‍💻",  # ZWJ sequences and skin tone modifiers
            "Tiếng Việt: Hà Nội, Đắk Lắk, Phở bò",  # Vietnamese diacritics
            "日本語: こんにちは世界、デスクトップ自動化",  # Japanese
            "简体中文: 自动化工作流系统，测试用例",  # Chinese
            "العربية: اختبار سير العمل المستقل",  # Arabic RTL
            "Русский: автоматизация рабочих процессов",  # Cyrillic
            "Ελληνικά: αυτοματοποίηση εργασιών",  # Greek
            "Math: ∀x ∈ ℝ, ∃y : y > x ∧ ∑(i=1..n) x_i = 42 ± 0.05",  # Mathematical symbols
        ]

        data = {
            "unicode_list": unicode_samples,
            "unicode_dict": {f"key_{i}": s for i, s in enumerate(unicode_samples)},
        }

        yaml_str = WorkflowSpec._to_yaml_native(data)
        recon = WorkflowSpec._from_yaml_native(yaml_str)

        self.assertEqual(recon["unicode_list"], unicode_samples)
        for i, s in enumerate(unicode_samples):
            self.assertEqual(recon["unicode_dict"][f"key_{i}"], s)

    def test_yaml_roundtrip_multiline_text_fidelity(self) -> None:
        """Verify multiline text containing colons, bullets, and indentation roundtrips with 100% fidelity."""
        multiline_doc = (
            "# Weekly Plan\n"
            "1. Review pull requests: https://github.com/org/repo\n"
            "2. Deploy v2.0: production:us-east-1\n"
            "   - Subtask A: verify logs\n"
            "   - Subtask B: run smoke tests\n"
            "\n"
            "Notes:\n"
            "  * All systems operational: 100%\n"
            "  * Emergency contact: ops@example.com:443\n"
        )

        spec = WorkflowSpec(
            id="wf_multiline_doc",
            name="Multiline Documentation",
            description=multiline_doc,
            steps=[
                WorkflowStep(
                    step_id="step_1",
                    order=1,
                    description="Paste multiline document",
                    action=ActionType.PASTE_TEXT,
                    payload={"text": multiline_doc},
                )
            ],
        )

        yaml_str = spec.to_yaml()
        recon = WorkflowSpec.from_yaml(yaml_str)

        self.assertEqual(recon.description, multiline_doc)
        self.assertEqual(recon.steps[0].payload["text"], multiline_doc)

    def test_yaml_roundtrip_full_workflow_spec_oracle(self) -> None:
        """Comprehensive roundtrip oracle covering every single field in WorkflowSpec."""
        spec = WorkflowSpec(
            id="wf_full_oracle_1",
            name="Autonomous Benchmark Workflow",
            description="End-to-end benchmark workflow specification",
            version=5,
            author="tester_automator",
            created_at="2026-09-23T12:00:00Z",
            updated_at="2026-09-23T15:30:00Z",
            triggers={
                "canonical": "create weekly notes todo list",
                "aliases": ["make weekly todo", "new notes todo", "notes: todo list"],
                "keywords": ["notes", "todo", "weekly", "benchmark"],
            },
            parameters={
                "doc_title": "Weekly Tasks: ${CURRENT_DATE}",
                "retry_count": 3,
                "timeout_s": 15.5,
                "is_active": True,
                "tags_filter": [],
                "metadata": {},
            },
            environment={"APP_ENV": "test", "DISPLAY_ID": "0", "EMPTY_ENV": {}},
            steps=[
                WorkflowStep(
                    step_id="s1",
                    order=1,
                    description="Launch Notes",
                    action=ActionType.LAUNCH_APP,
                    payload={"bundle_id": "com.apple.Notes"},
                    timing={"pre_delay_ms": 0, "post_delay_ms": 500, "timeout_ms": 5000},
                ),
                WorkflowStep(
                    step_id="s2",
                    order=2,
                    description="New Note Hotkey",
                    action=ActionType.PRESS_HOTKEY,
                    payload={"keys": ["cmd", "n"]},
                    timing={"pre_delay_ms": 0, "post_delay_ms": 300, "timeout_ms": 5000},
                ),
                WorkflowStep(
                    step_id="s3",
                    order=3,
                    description="Click text canvas",
                    action=ActionType.CLICK,
                    target={"norm_x": 0.5, "norm_y": 0.3, "screen_x": 640, "screen_y": 360},
                    payload={"button": "left", "click_count": 1},
                ),
                WorkflowStep(
                    step_id="s4",
                    order=4,
                    description="Paste todo body",
                    action=ActionType.PASTE_TEXT,
                    payload={"text": "- [ ] Task 1: review\n- [ ] Task 2: verify: done"},
                ),
            ],
            tags=["benchmark", "notes", "system:core"],
            pre_conditions=[{"type": "app_installed", "bundle_id": "com.apple.Notes"}],
            post_conditions=[{"type": "window_exists", "title": "Notes"}],
        )

        yaml_str = spec.to_yaml()
        recon = WorkflowSpec.from_yaml(yaml_str)

        self.assertEqual(recon.id, spec.id)
        self.assertEqual(recon.name, spec.name)
        self.assertEqual(recon.description, spec.description)
        self.assertEqual(recon.version, spec.version)
        self.assertEqual(recon.author, spec.author)
        self.assertEqual(recon.created_at, spec.created_at)
        self.assertEqual(recon.updated_at, spec.updated_at)
        self.assertEqual(recon.triggers, spec.triggers)
        self.assertEqual(recon.parameters, spec.parameters)
        self.assertEqual(recon.environment, spec.environment)
        self.assertEqual(len(recon.steps), 4)
        for s_orig, s_recon in zip(spec.steps, recon.steps):
            self.assertEqual(s_recon.step_id, s_orig.step_id)
            self.assertEqual(s_recon.order, s_orig.order)
            self.assertEqual(s_recon.description, s_orig.description)
            self.assertEqual(s_recon.action, s_orig.action)
            self.assertEqual(s_recon.target, s_orig.target)
            self.assertEqual(s_recon.payload, s_orig.payload)
            self.assertEqual(s_recon.timing, s_orig.timing)
        self.assertEqual(recon.tags, spec.tags)
        self.assertEqual(recon.pre_conditions, spec.pre_conditions)
        self.assertEqual(recon.post_conditions, spec.post_conditions)


# ==============================================================================
# Challenge Area 2: Click Coalescing Boundary Values
# ==============================================================================


class TestClickCoalescingBoundariesEmpiricalChallenge(unittest.TestCase):
    """Empirical challenge suite testing strict mathematical boundaries for click coalescing."""

    def setUp(self) -> None:
        self.pipeline = WorkflowRecorderPipeline()

    def _make_down_up_pair(
        self,
        x: float,
        y: float,
        t_down: float,
        t_up: float,
        btn: Any = "left",
    ) -> List[RawEvent]:
        return [
            RawEvent(event_type=RawEventType.MOUSE_DOWN, x=float(x), y=float(y), timestamp=t_down, button=btn),
            RawEvent(event_type=RawEventType.MOUSE_UP, x=float(x), y=float(y), timestamp=t_up, button=btn),
        ]

    # --- 1. Timing Boundaries ---

    def test_timing_boundary_exact_threshold_coalesces(self) -> None:
        """delta_t == 0.400s (exact double_click_interval_s threshold) MUST coalesce into double click."""
        # Click 1: [1.000, 1.050] (t_end = 1.050)
        # Click 2: [1.450, 1.500] (t_start = 1.450 -> delta_t = 1.450 - 1.050 = 0.400)
        events = self._make_down_up_pair(200, 200, 1.000, 1.050) + self._make_down_up_pair(200, 200, 1.450, 1.500)
        spec = self.pipeline.process_raw_events(events)

        self.assertEqual(len(spec.steps), 1, "delta_t == 0.400s must coalesce to 1 step")
        self.assertEqual(spec.steps[0].action, ActionType.CLICK)
        self.assertEqual(spec.steps[0].payload.get("click_count"), 2)

    def test_timing_boundary_slightly_under_threshold_coalesces(self) -> None:
        """delta_t == 0.399s (< 0.400s) MUST coalesce into double click."""
        events = self._make_down_up_pair(200, 200, 1.000, 1.050) + self._make_down_up_pair(200, 200, 1.449, 1.499)
        spec = self.pipeline.process_raw_events(events)

        self.assertEqual(len(spec.steps), 1)
        self.assertEqual(spec.steps[0].payload.get("click_count"), 2)

    def test_timing_boundary_slightly_over_threshold_does_not_coalesce(self) -> None:
        """delta_t == 0.4001s (> 0.400s) MUST NOT coalesce (two distinct single clicks)."""
        # delta_t = 1.4501 - 1.050 = 0.4001s
        events = self._make_down_up_pair(200, 200, 1.000, 1.050) + self._make_down_up_pair(200, 200, 1.4501, 1.5001)
        spec = self.pipeline.process_raw_events(events)

        self.assertEqual(len(spec.steps), 2, "delta_t > 0.400s must not coalesce")
        self.assertEqual(spec.steps[0].payload.get("click_count", 1), 1)
        self.assertEqual(spec.steps[1].payload.get("click_count", 1), 1)

    def test_timing_boundary_well_over_threshold_does_not_coalesce(self) -> None:
        """delta_t == 0.450s (> 0.400s) MUST NOT coalesce."""
        events = self._make_down_up_pair(200, 200, 1.000, 1.050) + self._make_down_up_pair(200, 200, 1.500, 1.550)
        spec = self.pipeline.process_raw_events(events)

        self.assertEqual(len(spec.steps), 2)
        self.assertEqual(spec.steps[0].payload.get("click_count", 1), 1)
        self.assertEqual(spec.steps[1].payload.get("click_count", 1), 1)

    # --- 2. Distance Boundaries ---

    def test_distance_boundary_exact_5px_coalesces(self) -> None:
        """Euclidean distance == 5.0px (exact boundary) MUST coalesce."""
        # 1. dx=5, dy=0 -> dist=5.0
        ev1 = self._make_down_up_pair(100, 100, 1.0, 1.05) + self._make_down_up_pair(105, 100, 1.10, 1.15)
        spec1 = self.pipeline.process_raw_events(ev1)
        self.assertEqual(len(spec1.steps), 1, "dx=5, dy=0 must coalesce")
        self.assertEqual(spec1.steps[0].payload.get("click_count"), 2)

        # 2. dx=0, dy=5 -> dist=5.0
        ev2 = self._make_down_up_pair(100, 100, 2.0, 2.05) + self._make_down_up_pair(100, 105, 2.10, 2.15)
        spec2 = self.pipeline.process_raw_events(ev2)
        self.assertEqual(len(spec2.steps), 1, "dx=0, dy=5 must coalesce")
        self.assertEqual(spec2.steps[0].payload.get("click_count"), 2)

        # 3. dx=3, dy=4 -> dist=sqrt(9+16)=5.0
        ev3 = self._make_down_up_pair(100, 100, 3.0, 3.05) + self._make_down_up_pair(103, 104, 3.10, 3.15)
        spec3 = self.pipeline.process_raw_events(ev3)
        self.assertEqual(len(spec3.steps), 1, "dx=3, dy=4 must coalesce")
        self.assertEqual(spec3.steps[0].payload.get("click_count"), 2)

        # 4. dx=-3, dy=-4 -> dist=5.0
        ev4 = self._make_down_up_pair(100, 100, 4.0, 4.05) + self._make_down_up_pair(97, 96, 4.10, 4.15)
        spec4 = self.pipeline.process_raw_events(ev4)
        self.assertEqual(len(spec4.steps), 1, "dx=-3, dy=-4 must coalesce")
        self.assertEqual(spec4.steps[0].payload.get("click_count"), 2)

    def test_distance_boundary_over_5px_does_not_coalesce(self) -> None:
        """Euclidean distance > 5.0px MUST NOT coalesce."""
        # 1. dx=6, dy=0 -> dist=6.0 > 5.0
        ev1 = self._make_down_up_pair(100, 100, 1.0, 1.05) + self._make_down_up_pair(106, 100, 1.10, 1.15)
        spec1 = self.pipeline.process_raw_events(ev1)
        self.assertEqual(len(spec1.steps), 2, "dx=6, dy=0 must not coalesce")

        # 2. dx=4, dy=4 -> dist=sqrt(32)=5.657 > 5.0
        ev2 = self._make_down_up_pair(100, 100, 2.0, 2.05) + self._make_down_up_pair(104, 104, 2.10, 2.15)
        spec2 = self.pipeline.process_raw_events(ev2)
        self.assertEqual(len(spec2.steps), 2, "dx=4, dy=4 must not coalesce")

        # 3. dx=0, dy=6 -> dist=6.0 > 5.0
        ev3 = self._make_down_up_pair(100, 100, 3.0, 3.05) + self._make_down_up_pair(100, 106, 3.10, 3.15)
        spec3 = self.pipeline.process_raw_events(ev3)
        self.assertEqual(len(spec3.steps), 2, "dx=0, dy=6 must not coalesce")

    # --- 3. Button Matching Across All Permutations ---

    def test_button_matching_permutations(self) -> None:
        """Exhaustively verify matching vs non-matching buttons at same coordinates and within timing."""
        all_buttons = ["left", "right", "middle"]

        for b1 in all_buttons:
            for b2 in all_buttons:
                events = self._make_down_up_pair(300, 300, 1.0, 1.05, btn=b1) + self._make_down_up_pair(
                    300, 300, 1.10, 1.15, btn=b2
                )
                spec = self.pipeline.process_raw_events(events)

                if b1 == b2:
                    self.assertEqual(
                        len(spec.steps),
                        1,
                        f"Matching buttons {b1}=={b2} failed to coalesce into double click",
                    )
                    self.assertEqual(spec.steps[0].payload.get("click_count"), 2)
                    actual_btn = str(getattr(spec.steps[0].payload.get("button"), "value", spec.steps[0].payload.get("button"))).lower()
                    self.assertIn(actual_btn, [b1, "center"] if b1 == "middle" else [b1])
                else:
                    self.assertEqual(
                        len(spec.steps),
                        2,
                        f"Differing buttons {b1}!={b2} must not coalesce",
                    )
                    self.assertEqual(spec.steps[0].payload.get("click_count", 1), 1)
                    self.assertEqual(spec.steps[1].payload.get("click_count", 1), 1)

    def test_button_matching_enum_and_string_heterogeneity(self) -> None:
        """Verify seamless coalescing when button is passed as MouseButton enum vs lowercase string."""
        events = self._make_down_up_pair(400, 400, 1.0, 1.05, btn=MouseButton.LEFT) + self._make_down_up_pair(
            400, 400, 1.10, 1.15, btn="left"
        )
        spec = self.pipeline.process_raw_events(events)
        self.assertEqual(len(spec.steps), 1)
        self.assertEqual(spec.steps[0].payload.get("click_count"), 2)

    def test_direct_click_raw_events_boundary_adherence(self) -> None:
        """Verify RawEventType.CLICK direct events follow exact same boundary invariants."""
        # Exact timing boundary: t1=1.000 (t_end=1.050), t2=1.450 -> delta_t=0.400 -> coalesce
        ev_coalesce = [
            RawEvent(event_type=RawEventType.CLICK, x=100.0, y=100.0, timestamp=1.000, button="left"),
            RawEvent(event_type=RawEventType.CLICK, x=100.0, y=100.0, timestamp=1.450, button="left"),
        ]
        spec_c = self.pipeline.process_raw_events(ev_coalesce)
        self.assertEqual(len(spec_c.steps), 1)
        self.assertEqual(spec_c.steps[0].payload.get("click_count"), 2)

        # Over timing boundary: t1=1.000 (t_end=1.050), t2=1.4501 -> delta_t=0.4001 -> no coalesce
        ev_no_coalesce = [
            RawEvent(event_type=RawEventType.CLICK, x=100.0, y=100.0, timestamp=1.000, button="left"),
            RawEvent(event_type=RawEventType.CLICK, x=100.0, y=100.0, timestamp=1.4501, button="left"),
        ]
        spec_nc = self.pipeline.process_raw_events(ev_no_coalesce)
        self.assertEqual(len(spec_nc.steps), 2)


# ==============================================================================
# Challenge Area 3: Retrieval Fault Isolation
# ==============================================================================


class TestRetrievalFaultIsolationEmpiricalChallenge(unittest.TestCase):
    """Empirical challenge suite testing database corruption resilience and retrieval isolation."""

    def setUp(self) -> None:
        self.engine = TaskMemoryEngine(":memory:")
        self.retrieval = NLRetrievalEngine(self.engine, min_confidence=0.50)

    def tearDown(self) -> None:
        self.engine.close()

    def test_retrieval_fault_isolation_corrupted_and_foreign_types(self) -> None:
        """Database containing corrupt specs, non-dict payloads, missing keys, and foreign types.

        Invariants:
        1. query() MUST NEVER crash or throw an exception.
        2. Corrupt rows must be safely skipped.
        3. Valid rows must be successfully matched and retrieved.
        """
        # Save valid baseline target
        valid_spec = WorkflowSpec(
            id="wf_gold_standard",
            name="Valid Target Task",
            triggers={
                "canonical": "launch notes application",
                "aliases": ["open apple notes", "start notes app"],
                "keywords": ["notes", "apple", "launch"],
            },
            steps=[WorkflowStep(step_id="s1", order=1, description="Launch Notes", action=ActionType.LAUNCH_APP, payload={"bundle_id": "com.apple.Notes"})],
        )
        self.engine.save_workflow(valid_spec)

        # Inject 15 distinct corrupt specs directly into the database table
        corrupt_rows = [
            # Corrupt JSON syntax
            ("wf_corrupt_syntax", "Corrupt Syntax", "{invalid: json, truncated"),
            # Primitive non-dict JSON
            ("wf_corrupt_int", "Integer Payload", "123456"),
            ("wf_corrupt_str", "String Payload", '"raw string"'),
            ("wf_corrupt_list", "List Payload", '["list", "elements"]'),
            ("wf_corrupt_bool", "Bool Payload", "true"),
            ("wf_corrupt_null", "Null Payload", "null"),
            # Missing essential keys
            ("wf_missing_id", "Missing ID", '{"name": "No ID", "triggers": {"canonical": "launch notes"}}'),
            ("wf_missing_name", "Missing Name", '{"id": "wf_missing_name", "triggers": {"canonical": "launch notes"}}'),
            ("wf_empty_dict", "Empty Dict", "{}"),
            # Foreign types in triggers mapping
            ("wf_triggers_int", "Triggers Int", '{"id": "wf_trig_int", "name": "T", "triggers": 42}'),
            ("wf_triggers_str", "Triggers Str", '{"id": "wf_trig_str", "name": "T", "triggers": "launch notes application"}'),
            ("wf_triggers_list", "Triggers List", '{"id": "wf_trig_list", "name": "T", "triggers": ["launch notes"]}'),
            ("wf_triggers_none", "Triggers None", '{"id": "wf_trig_none", "name": "T", "triggers": null}'),
            # Foreign types inside canonical/aliases/keywords
            ("wf_canon_dict", "Canon Dict", '{"id": "wf_c_dict", "name": "T", "triggers": {"canonical": {"dict": 1}}}'),
            ("wf_canon_int", "Canon Int", '{"id": "wf_c_int", "name": "T", "triggers": {"canonical": 999}}'),
            ("wf_aliases_dict", "Aliases Dict", '{"id": "wf_a_dict", "name": "T", "triggers": {"canonical": "unrelated task", "aliases": {"k": "v"}}}'),
            ("wf_aliases_foreign_items", "Aliases Items", '{"id": "wf_a_items", "name": "T", "triggers": {"canonical": "unrelated task", "aliases": [null, 123, {"a": "b"}, [1, 2], true]}}'),
            ("wf_kw_foreign_items", "KW Items", '{"id": "wf_kw_items", "name": "T", "triggers": {"canonical": "unrelated task", "keywords": [false, 3.14, null]}}'),
        ]

        cur = self.engine._conn.cursor()
        for wid, wname, raw_json in corrupt_rows:
            cur.execute(
                """
                INSERT INTO workflows (id, name, description, version, active, spec_json, created_at, updated_at)
                VALUES (?, ?, 'Corrupted payload', 1, 1, ?, '2026-09-23T00:00:00Z', '2026-09-23T00:00:00Z')
                """,
                (wid, wname, raw_json),
            )
        self.engine._conn.commit()

        # Invariant: Tier 1 canonical match must isolate corrupt records and retrieve valid workflow
        t1_results = self.retrieval.query("launch notes application")
        self.assertGreaterEqual(len(t1_results), 1)
        self.assertEqual(t1_results[0].workflow_id, "wf_gold_standard")
        self.assertEqual(t1_results[0].confidence, 1.0)
        self.assertEqual(t1_results[0].tier, "tier1_canonical")

        # Invariant: Tier 2 alias match
        t2_results = self.retrieval.query("open apple notes")
        self.assertGreaterEqual(len(t2_results), 1)
        self.assertEqual(t2_results[0].workflow_id, "wf_gold_standard")
        self.assertEqual(t2_results[0].confidence, 0.95)

        # Invariant: Tier 3 substring match
        t3_results = self.retrieval.query("please launch notes application now")
        self.assertGreaterEqual(len(t3_results), 1)
        self.assertEqual(t3_results[0].workflow_id, "wf_gold_standard")
        self.assertGreaterEqual(t3_results[0].confidence, 0.85)

        # Invariant: query_best convenience method
        best = self.retrieval.query_best("launch notes application")
        self.assertIsNotNone(best)
        assert best is not None
        self.assertEqual(best.workflow_id, "wf_gold_standard")

    def test_adversarial_queries_and_fts_injection_resilience(self) -> None:
        """Adversarial query inputs: None, numbers, booleans, FTS5 operator attacks, SQL injections."""
        adversarial_queries = [
            None,
            12345,
            -99.9,
            True,
            False,
            "",
            "   ",
            "\t\n",
            # FTS5 syntax attacks
            "NOT",
            "AND",
            "OR",
            "NEAR",
            "MATCH",
            '*""*()',
            "column:value",
            "notes NOT notes",
            "AND OR NOT NEAR()",
            # SQL injection attacks
            "'; DROP TABLE workflows; --",
            "' UNION SELECT * FROM workflows WHERE '1'='1",
            '" OR 1=1 --',
        ]

        for q in adversarial_queries:
            with self.subTest(query=q):
                try:
                    res = self.retrieval.query(q)  # type: ignore[arg-type]
                    self.assertIsInstance(res, list)
                    best = self.retrieval.query_best(q)  # type: ignore[arg-type]
                    self.assertTrue(best is None or isinstance(best, MatchResult))
                except Exception as ex:
                    self.fail(f"retrieval.query({q!r}) crashed with exception: {type(ex).__name__}: {ex}")


# ==============================================================================
# Challenge Area 4: Zero Live Desktop Event Dispatch (R6 Compliance)
# ==============================================================================


class TestR6ZeroLiveDesktopEventDispatchEmpiricalChallenge(unittest.TestCase):
    """Empirical verification that 0 live OS events (mouse, keyboard) are dispatched to desktop."""

    def test_zero_live_event_dispatch_under_ctypes_interception(self) -> None:
        """Intercept CoreGraphics dynamic library bindings and assert ZERO physical events posted.

        Requirement R6:
        - Unit and milestone verification tests run strictly against virtual in-memory simulators,
          ensuring 0 physical mouse movement or keyboard event posting to the user's active session.
        """
        # Track live CoreGraphics posting attempts
        live_posts: List[str] = []

        def spy_cg_event_post(*args: Any, **kwargs: Any) -> None:
            live_posts.append(f"CGEventPost called with {args}")

        def spy_cg_event_post_to_pid(*args: Any, **kwargs: Any) -> None:
            live_posts.append(f"CGEventPostToPid called with {args}")

        # Attempt to patch native CoreGraphics actuator bindings if present
        try:
            from src.actuators.macos import CoreGraphicsBindings
            cg_patch = patch.object(CoreGraphicsBindings, "CGEventPost", side_effect=spy_cg_event_post)
        except Exception:
            cg_patch = None

        with (
            cg_patch
            if cg_patch
            else unittest.mock.MagicMock()
        ):
            # 1. Run MockActuator operations
            mock_act = MockActuator()
            mock_act.move_mouse(500, 500)
            mock_act.click(500, 500)
            mock_act.type_text("Hello Headless")
            mock_act.press_hotkey("cmd", "n")
            mock_act.paste_text("Autonomous document")

            # paste_text produces both paste_text action and synthetic Cmd+V hotkey (6 total actions)
            self.assertEqual(len(mock_act.history), 6)
            self.assertEqual(mock_act.get_mouse_position(), (500.0, 500.0))

            # 2. Run WorkflowRecorder operations
            pipeline = WorkflowRecorderPipeline()
            events = [
                RawEvent(event_type=RawEventType.MOUSE_DOWN, x=100.0, y=100.0, timestamp=1.0, button="left"),
                RawEvent(event_type=RawEventType.MOUSE_UP, x=100.0, y=100.0, timestamp=1.05, button="left"),
                RawEvent(event_type=RawEventType.KEY_CHAR, key="a", timestamp=1.1),
                RawEvent(event_type=RawEventType.KEY_CHAR, key="b", timestamp=1.2),
            ]
            spec = pipeline.process_raw_events(events)
            self.assertEqual(len(spec.steps), 2)

            # 3. Run TaskMemoryEngine and NLRetrievalEngine operations
            engine = TaskMemoryEngine(":memory:")
            engine.save_workflow(spec)
            retrieval = NLRetrievalEngine(engine)
            retrieval.query("test query")
            engine.close()

        # Invariant: 0 live CoreGraphics posts
        self.assertEqual(
            live_posts,
            [],
            f"VIOLATION OF R6: Live desktop events were posted during testing: {live_posts}",
        )


if __name__ == "__main__":
    unittest.main()
