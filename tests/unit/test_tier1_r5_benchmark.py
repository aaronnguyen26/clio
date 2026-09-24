"""Tier 1: Feature Coverage for R5 — End-to-End Autonomous Benchmark Task.

Tests validate:
- NotesBenchmarkRecipe 5-phase structure
- Execution of benchmark through mock actuator
- Content formatting and markdown checklist in weekly to-do list
- BenchmarkCompletionVerifier logic
- Event stream capture during benchmark run
"""

import pytest
from tests.harness import (
    MockActuator,
    AutonomousWorkflowExecutor,
    ExecutionEventBus,
    ExecutionEvent,
    EventType,
    BenchmarkCompletionVerifier,
    WorkflowSpec,
    ActionType,
)


@pytest.mark.tier1
@pytest.mark.mock
class TestTier1Benchmark:
    """Tier 1 tests for R5 autonomous benchmark task."""

    def test_benchmark_recipe_step_structure(self, notes_benchmark_wf: WorkflowSpec):
        """TEST-T1-R5-01: Recipe specifies required phases: launch, focus, new note, paste."""
        assert notes_benchmark_wf.id == "wf_notes_weekly_todo"
        assert len(notes_benchmark_wf.steps) >= 4

        actions = [s.action for s in notes_benchmark_wf.steps]
        assert ActionType.LAUNCH_APP in actions
        assert ActionType.FOCUS_APP in actions
        assert ActionType.PRESS_HOTKEY in actions
        assert ActionType.PASTE_TEXT in actions

    def test_benchmark_mock_execution_completes(
        self,
        mock_actuator: MockActuator,
        executor: AutonomousWorkflowExecutor,
        notes_benchmark_wf: WorkflowSpec,
    ):
        """TEST-T1-R5-02: Executing benchmark via MockActuator completes with 100% success."""
        result = executor.execute_workflow(notes_benchmark_wf)

        assert result.success is True
        assert result.steps_completed == len(notes_benchmark_wf.steps)
        assert result.error is None

        # Verify actuator calls
        mock_actuator.assert_app_launched("com.apple.Notes")
        mock_actuator.assert_hotkey_pressed("cmd", "n")
        mock_actuator.assert_hotkey_pressed("cmd", "v")
        mock_actuator.assert_text_typed("Weekly Action Plan")

    def test_benchmark_todo_content_verification(self, notes_benchmark_wf: WorkflowSpec):
        """TEST-T1-R5-03: Weekly to-do list content has formatted checklist markers."""
        paste_step = next(s for s in notes_benchmark_wf.steps if s.action == ActionType.PASTE_TEXT)
        text = paste_step.payload["text"]

        assert "[ ] Monday:" in text
        assert "[ ] Tuesday:" in text
        assert "[ ] Wednesday:" in text
        assert "[ ] Thursday:" in text
        assert "[ ] Friday:" in text
        assert "Autonomous Desktop Companion" in text

    def test_benchmark_completion_verifier_logic(
        self,
        mock_actuator: MockActuator,
        executor: AutonomousWorkflowExecutor,
        notes_benchmark_wf: WorkflowSpec,
    ):
        """TEST-T1-R5-04: Verifier confirms successful note generation after execution."""
        # Before execution: should fail verification
        assert BenchmarkCompletionVerifier.verify(mock_actuator) is False

        # Execute
        result = executor.execute_workflow(notes_benchmark_wf)
        assert result.success is True

        # After execution: should pass verification
        assert BenchmarkCompletionVerifier.verify(mock_actuator) is True

    def test_benchmark_telemetry_event_stream(
        self,
        executor: AutonomousWorkflowExecutor,
        event_bus: ExecutionEventBus,
        notes_benchmark_wf: WorkflowSpec,
    ):
        """TEST-T1-R5-05: Emits all required lifecycle events during benchmark run."""
        emitted_types = []
        event_bus.subscribe(lambda e: emitted_types.append(e.event_type))

        result = executor.execute_workflow(notes_benchmark_wf)
        assert result.success is True

        assert EventType.TASK_STARTED in emitted_types
        assert EventType.ACTION_STARTING in emitted_types
        assert EventType.ACTION_COMPLETED in emitted_types
        assert EventType.TASK_COMPLETED in emitted_types
