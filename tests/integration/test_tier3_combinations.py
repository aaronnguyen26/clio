"""Tier 3: Cross-Feature Combinations (Pairwise Component Interactions).

Tests validate:
1. R1 + R2: Actuator execution streaming into Companion Commentary Engine.
2. R1 + R3: TaskMemoryEngine workflow converted to screen coordinates and actuated.
3. R2 + R3: Conversational memory query resolving to workflow retrieval.
4. R1 + R5: Benchmark execution verified against mock actuator audit trail.
5. R2 + R4: Complete conversational dialogue loop tested in mock staged verification.
"""

import pytest
from tests.harness import (
    MockActuator,
    TaskMemoryEngine,
    ExecutionEventBus,
    ExecutionEvent,
    EventType,
    CommentaryEngine,
    CompanionDialogueEngine,
    DialogueState,
    AutonomousWorkflowExecutor,
    CoordinateAdapter,
    BenchmarkCompletionVerifier,
    WorkflowSpec,
    WorkflowStep,
    ActionType,
    WindowInfo,
    create_notes_benchmark_workflow,
)


@pytest.mark.tier3
@pytest.mark.mock
class TestTier3Combinations:
    """Tier 3 pairwise cross-feature integration tests."""

    def test_r1_actuator_plus_r2_commentary_stream(
        self,
        mock_actuator: MockActuator,
        event_bus: ExecutionEventBus,
        notes_benchmark_wf: WorkflowSpec,
    ):
        """Pairwise R1+R2: As actuator executes steps, commentary engine emits formatted updates."""
        commentary = CommentaryEngine(bus=event_bus, tone="vibrant", throttle_ms=0.0)
        executor = AutonomousWorkflowExecutor(actuator=mock_actuator, bus=event_bus)

        result = executor.execute_workflow(notes_benchmark_wf)
        assert result.success is True

        # Commentary log must contain start, actions, and completion
        assert len(commentary.commentary_log) >= 3
        full_commentary = " ".join(commentary.commentary_log)
        assert "Alright" in full_commentary or "hands-free" in full_commentary
        assert "Ta-da!" in full_commentary or "finished" in full_commentary

    def test_r1_actuator_plus_r3_coordinate_projection_workflow(
        self,
        mock_actuator: MockActuator,
        empty_memory_engine: TaskMemoryEngine,
        event_bus: ExecutionEventBus,
    ):
        """Pairwise R1+R3: Memory-stored workflow with ratio coordinates executes via actuator."""
        window = WindowInfo(
            window_id=15,
            owner_name="com.apple.Notes",
            title="Notes",
            x=200.0,
            y=150.0,
            width=800.0,
            height=600.0,
        )
        mock_actuator.add_virtual_window(window)

        # Convert ratio (0.5, 0.5) to screen coordinates
        screen_x, screen_y = CoordinateAdapter.to_screen_coordinates(0.5, 0.5, window)
        assert screen_x == 600
        assert screen_y == 450

        spec = WorkflowSpec(
            id="wf_ratio_click",
            name="Ratio Click Task",
            description="Clicks at center of window",
            steps=[
                WorkflowStep(
                    step_id="s1",
                    order=1,
                    description="Click center",
                    action=ActionType.CLICK,
                    payload={"coordinates": {"x": screen_x, "y": screen_y}},
                )
            ],
        )
        empty_memory_engine.save_workflow(spec)

        executor = AutonomousWorkflowExecutor(actuator=mock_actuator, bus=event_bus)
        res = executor.execute_workflow(spec)
        assert res.success is True

        mock_actuator.assert_action_called("click", x=600.0, y=450.0)

    def test_r2_companion_plus_r3_memory_retrieval(
        self,
        memory_engine: TaskMemoryEngine,
    ):
        """Pairwise R2+R3: Conversational input queries memory and identifies target workflow."""
        dialogue = CompanionDialogueEngine(memory=memory_engine)

        reply, state = dialogue.handle_user_message("plan my week in notes")
        assert state == DialogueState.EXECUTING
        assert "Create Weekly To-Do in Apple Notes" in reply

    def test_r1_actuator_plus_r5_benchmark_safety_abort(
        self,
        mock_actuator: MockActuator,
        notes_benchmark_wf: WorkflowSpec,
        event_bus: ExecutionEventBus,
    ):
        """Pairwise R1+R5: Failsafe triggered during benchmark halts actuator cleanly."""
        # Arm failsafe
        mock_actuator.trigger_failsafe()

        executor = AutonomousWorkflowExecutor(actuator=mock_actuator, bus=event_bus)
        res = executor.execute_workflow(notes_benchmark_wf)

        assert res.success is False
        assert "Emergency stop" in str(res.error)
        mock_actuator.assert_modifiers_released()

    def test_r2_companion_plus_r4_staged_mock_session(
        self,
        memory_engine: TaskMemoryEngine,
        mock_actuator: MockActuator,
        event_bus: ExecutionEventBus,
    ):
        """Pairwise R2+R4: Complete multi-turn conversational session operates in mock mode."""
        dialogue = CompanionDialogueEngine(memory=memory_engine, tone="developer")

        # Turn 1: Greet
        r1, s1 = dialogue.handle_user_message("Hello Clio")
        assert s1 == DialogueState.IDLE

        # Turn 2: Query memory
        r2, s2 = dialogue.handle_user_message("What tasks do you remember?")
        assert s2 == DialogueState.IDLE
        assert "Create Weekly To-Do in Apple Notes" in r2

        # Turn 3: Trigger task
        r3, s3 = dialogue.handle_user_message("write my weekly to-do list")
        assert s3 == DialogueState.EXECUTING
