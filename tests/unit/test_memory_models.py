"""Unit tests for Workflow Schema Models (FEAT-MEM-01).

Tests validate:
- ActionType enum members and string serialization
- CoordMode enum members
- TargetCoordinates validation and dict conversion
- WorkflowStep dataclass creation, defaults, to_dict/from_dict round-trip
- WorkflowSpec dataclass creation, defaults, to_dict/from_dict round-trip
- JSON and YAML serialization/deserialization consistency
- MatchResult and ExecutionRecord serialization and validation
"""

import json
import unittest

from src.memory.models import (
    ActionType,
    CoordMode,
    ExecutionRecord,
    MatchResult,
    SerializationError,
    StepErrorHandling,
    StepTiming,
    StepVerification,
    TargetCoordinates,
    ValidationError,
    WorkflowSpec,
    WorkflowStep,
)


class TestMemoryModels(unittest.TestCase):
    """Test suite for memory schema models."""

    def test_action_type_members(self):
        """TEST-MODEL-01: ActionType provides all required OS automation actions."""
        expected = {
            "launch_app",
            "focus_app",
            "move_mouse",
            "click",
            "double_click",
            "right_click",
            "drag",
            "scroll",
            "type_text",
            "paste_text",
            "press_hotkey",
            "wait_window",
            "wait",
        }
        actual = {a.value for a in ActionType}
        self.assertTrue(expected.issubset(actual))

    def test_target_coordinates_validation(self):
        """TEST-MODEL-02: TargetCoordinates validates norm and abs coordinate constraints."""
        valid_ratio = TargetCoordinates(mode=CoordMode.WINDOW_RELATIVE_RATIO, norm_x=0.5, norm_y=0.8)
        valid_ratio.validate()

        invalid_ratio = TargetCoordinates(mode=CoordMode.WINDOW_RELATIVE_RATIO, norm_x=1.5, norm_y=0.5)
        with self.assertRaises(ValidationError):
            invalid_ratio.validate()

        invalid_neg = TargetCoordinates(mode=CoordMode.WINDOW_RELATIVE_RATIO, norm_x=-0.1, norm_y=0.5)
        with self.assertRaises(ValidationError):
            invalid_neg.validate()

        valid_abs = TargetCoordinates(mode=CoordMode.SCREEN_ABSOLUTE, abs_x=100, abs_y=200)
        valid_abs.validate()

        invalid_abs = TargetCoordinates(mode=CoordMode.SCREEN_ABSOLUTE, abs_x=-10, abs_y=200)
        with self.assertRaises(ValidationError):
            invalid_abs.validate()

    def test_target_coordinates_dict_roundtrip(self):
        """TEST-MODEL-03: TargetCoordinates roundtrips to and from dict."""
        tc = TargetCoordinates(mode=CoordMode.WINDOW_RELATIVE_RATIO, norm_x=0.25, norm_y=0.75, offset_x=10)
        d = tc.to_dict()
        self.assertEqual(d["mode"], "window_relative_ratio")
        self.assertEqual(d["norm_x"], 0.25)
        reconstructed = TargetCoordinates.from_dict(d)
        self.assertEqual(reconstructed.norm_x, 0.25)
        self.assertEqual(reconstructed.offset_x, 10)

    def test_workflow_step_roundtrip(self):
        """TEST-MODEL-04: WorkflowStep serializes to dict and reconstructs identically."""
        step = WorkflowStep(
            step_id="step_click_1",
            order=1,
            description="Click primary button",
            action=ActionType.CLICK,
            target={"app_name": "Notes"},
            payload={"button": "left"},
            timing={"pre_delay_ms": 100, "post_delay_ms": 50, "timeout_ms": 5000},
            coordinates=TargetCoordinates(norm_x=0.5, norm_y=0.5),
        )
        d = step.to_dict()
        self.assertEqual(d["action"], "click")
        self.assertEqual(d["order"], 1)

        reconstructed = WorkflowStep.from_dict(d)
        self.assertEqual(reconstructed.step_id, step.step_id)
        self.assertEqual(reconstructed.action, ActionType.CLICK)
        self.assertEqual(reconstructed.payload, step.payload)
        self.assertIsNotNone(reconstructed.coordinates)
        self.assertEqual(reconstructed.coordinates.norm_x, 0.5)

    def test_workflow_step_validation(self):
        """TEST-MODEL-05: WorkflowStep validates step_id, order, description."""
        invalid_id = WorkflowStep(step_id="", order=1, description="desc", action=ActionType.CLICK)
        with self.assertRaises(ValidationError):
            invalid_id.validate()

        invalid_order = WorkflowStep(step_id="s1", order=0, description="desc", action=ActionType.CLICK)
        with self.assertRaises(ValidationError):
            invalid_order.validate()

    def test_workflow_spec_roundtrip_and_json(self):
        """TEST-MODEL-06: WorkflowSpec round-trips through dictionary and JSON."""
        step1 = WorkflowStep(
            step_id="s1",
            order=1,
            description="Launch",
            action=ActionType.LAUNCH_APP,
            target={"bundle_id": "com.apple.Notes"},
        )
        spec = WorkflowSpec(
            id="wf_notes_demo",
            name="Notes Demo",
            description="Launch Notes",
            triggers={"canonical": "open notes", "aliases": ["launch notes"], "keywords": ["notes"]},
            steps=[step1],
        )

        d = spec.to_dict()
        json_str = spec.to_json()
        reconstructed = WorkflowSpec.from_json(json_str)

        self.assertEqual(reconstructed.id, "wf_notes_demo")
        self.assertEqual(len(reconstructed.steps), 1)
        self.assertEqual(reconstructed.steps[0].action, ActionType.LAUNCH_APP)

    def test_workflow_spec_validation(self):
        """TEST-MODEL-07: WorkflowSpec validates ID format, name, and steps."""
        invalid_id = WorkflowSpec(id="invalid id with spaces!", name="Valid Name")
        with self.assertRaises(ValidationError):
            invalid_id.validate()

        invalid_name = WorkflowSpec(id="wf_valid_id", name="")
        with self.assertRaises(ValidationError):
            invalid_name.validate()

        valid_spec = WorkflowSpec(id="wf_valid_id-123", name="Valid Name")
        valid_spec.validate()

    def test_workflow_spec_yaml_roundtrip(self):
        """TEST-MODEL-08: WorkflowSpec serializes to YAML and deserializes correctly."""
        step = WorkflowStep(
            step_id="s1",
            order=1,
            description="Type greeting",
            action=ActionType.TYPE_TEXT,
            payload={"text": "Hello YAML"},
        )
        spec = WorkflowSpec(
            id="wf_yaml_test",
            name="YAML Test",
            description="Testing YAML roundtrip",
            steps=[step],
        )
        yaml_out = spec.to_yaml()
        self.assertIn("wf_yaml_test", yaml_out)
        self.assertIn("Hello YAML", yaml_out)

        # Deserialize directly from YAML output (genuine YAML roundtrip)
        reconstructed = WorkflowSpec.from_yaml(yaml_out)
        self.assertEqual(reconstructed.id, spec.id)
        self.assertEqual(reconstructed.name, spec.name)
        self.assertEqual(reconstructed.description, spec.description)
        self.assertEqual(len(reconstructed.steps), len(spec.steps))
        self.assertEqual(reconstructed.steps[0].step_id, spec.steps[0].step_id)
        self.assertEqual(reconstructed.steps[0].action, spec.steps[0].action)
        self.assertEqual(reconstructed.steps[0].payload, spec.steps[0].payload)

    def test_match_result_and_execution_record(self):
        """TEST-MODEL-09: MatchResult and ExecutionRecord serialize and validate."""
        mr = MatchResult(
            workflow_id="wf_test",
            workflow_name="Test",
            confidence=0.95,
            matched_trigger="run test",
            tier="tier2_alias",
        )
        d_mr = mr.to_dict()
        self.assertEqual(d_mr["confidence"], 0.95)
        rec_mr = MatchResult.from_dict(d_mr)
        self.assertEqual(rec_mr.workflow_id, "wf_test")

        er = ExecutionRecord(
            workflow_id="wf_test",
            status="success",
            duration_ms=120,
            steps_completed=3,
        )
        er.validate()
        d_er = er.to_dict()
        rec_er = ExecutionRecord.from_dict(d_er)
        self.assertEqual(rec_er.duration_ms, 120)

        invalid_er = ExecutionRecord(
            workflow_id="",
            status="failed",
            duration_ms=-1,
            steps_completed=0,
        )
        with self.assertRaises(ValidationError):
            invalid_er.validate()
