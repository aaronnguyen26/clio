"""Tier 4: Real-World Application Scenarios.

Validates end-to-end user workflows:
1. Scenario 1: Cold-start hands-free Apple Notes benchmark (R5 primary).
2. Scenario 2: Warm-start Apple Notes benchmark with Notes already open.
3. Scenario 3: Human emergency intervention (corner flick abort).
4. Scenario 4: Natural language conversational delegation flow.
5. Scenario 5: Complete Teach-Mode demonstration, persistence, and replay.
"""

import time
import pytest
from tests.harness import (
    MockActuator,
    TaskMemoryEngine,
    ExecutionEventBus,
    CommentaryEngine,
    CompanionDialogueEngine,
    AutonomousWorkflowExecutor,
    BenchmarkCompletionVerifier,
    DialogueState,
    WorkflowSpec,
    WorkflowStep,
    ActionType,
    create_notes_benchmark_workflow,
)


@pytest.mark.tier4
@pytest.mark.mock
class TestTier4Scenarios:
    """Tier 4 end-to-end real-world user scenario tests."""

    def test_scenario_1_cold_start_notes_benchmark(
        self,
        mock_actuator: MockActuator,
        event_bus: ExecutionEventBus,
        notes_benchmark_wf: WorkflowSpec,
    ):
        """Scenario 1: Cold-start benchmark opens Notes, writes weekly to-do, verifies completion."""
        # Notes is NOT open initially
        assert mock_actuator.get_frontmost_app() != "com.apple.Notes"

        commentary = CommentaryEngine(bus=event_bus, tone="vibrant", throttle_ms=0.0)
        executor = AutonomousWorkflowExecutor(actuator=mock_actuator, bus=event_bus)

        res = executor.execute_workflow(notes_benchmark_wf)
        assert res.success is True
        assert res.steps_completed == 4

        # Verify application state
        assert mock_actuator.get_frontmost_app() == "com.apple.Notes"
        assert BenchmarkCompletionVerifier.verify(mock_actuator) is True

        # Verify commentary output celebrated the success
        assert any("Ta-da!" in msg or "finished" in msg for msg in commentary.commentary_log)

    def test_scenario_2_warm_start_notes_benchmark(
        self,
        mock_actuator: MockActuator,
        event_bus: ExecutionEventBus,
        notes_benchmark_wf: WorkflowSpec,
    ):
        """Scenario 2: Warm-start benchmark with Notes already running."""
        # Pre-launch Notes
        mock_actuator.launch_app("com.apple.Notes")
        initial_history_len = len(mock_actuator.history)

        executor = AutonomousWorkflowExecutor(actuator=mock_actuator, bus=event_bus)
        res = executor.execute_workflow(notes_benchmark_wf)

        assert res.success is True
        assert BenchmarkCompletionVerifier.verify(mock_actuator) is True
        assert len(mock_actuator.history) > initial_history_len

    def test_scenario_3_emergency_corner_intervention(
        self,
        mock_actuator: MockActuator,
        event_bus: ExecutionEventBus,
        notes_benchmark_wf: WorkflowSpec,
    ):
        """Scenario 3: Human jerks mouse to corner (0, 0); halts automation instantly."""
        commentary = CommentaryEngine(bus=event_bus, tone="vibrant", throttle_ms=0.0)

        # Trigger corner failsafe
        mock_actuator.trigger_failsafe()

        executor = AutonomousWorkflowExecutor(actuator=mock_actuator, bus=event_bus)
        res = executor.execute_workflow(notes_benchmark_wf)

        assert res.success is False
        assert "Emergency stop" in str(res.error)
        mock_actuator.assert_modifiers_released()

        # Commentary must report abort
        assert any("Emergency stop" in msg or "🛑" in msg for msg in commentary.commentary_log)

    def test_scenario_4_conversational_task_delegation_flow(
        self,
        memory_engine: TaskMemoryEngine,
        mock_actuator: MockActuator,
        event_bus: ExecutionEventBus,
        notes_benchmark_wf: WorkflowSpec,
    ):
        """Scenario 4: User converses with companion to initiate autonomous task."""
        dialogue = CompanionDialogueEngine(memory=memory_engine, tone="vibrant")

        # Step 1: User says "Hey Clio, write my weekly to-do list"
        reply, state = dialogue.handle_user_message("Hey Clio, write my weekly to-do list")
        assert state == DialogueState.EXECUTING
        assert "Create Weekly To-Do in Apple Notes" in reply

        # Step 2: System executes the matched workflow
        executor = AutonomousWorkflowExecutor(actuator=mock_actuator, bus=event_bus)
        res = executor.execute_workflow(notes_benchmark_wf)
        assert res.success is True
        assert BenchmarkCompletionVerifier.verify(mock_actuator) is True

    def test_scenario_5_teach_mode_record_persist_and_replay(
        self,
        empty_memory_engine: TaskMemoryEngine,
        mock_actuator: MockActuator,
        event_bus: ExecutionEventBus,
    ):
        """Scenario 5: Complete Teach-Mode cycle: learn task, persist, and replay."""
        # 1. User demonstrates task: open Safari and type URL
        learned_spec = WorkflowSpec(
            id="wf_open_github",
            name="Open GitHub in Browser",
            description="Opens Safari and navigates to GitHub",
            triggers={
                "canonical": "open github",
                "aliases": ["browse github", "go to github"],
                "keywords": ["github", "safari", "browser"],
            },
            steps=[
                WorkflowStep(
                    step_id="s1",
                    order=1,
                    description="Launch Safari",
                    action=ActionType.LAUNCH_APP,
                    target={"bundle_id": "com.apple.Safari"},
                ),
                WorkflowStep(
                    step_id="s2",
                    order=2,
                    description="Navigate to GitHub",
                    action=ActionType.TYPE_TEXT,
                    payload={"text": "https://github.com\n"},
                ),
            ],
        )

        # 2. Persist to database
        wf_id = empty_memory_engine.save_workflow(learned_spec, change_summary="Recorded via teach mode")
        assert wf_id == "wf_open_github"

        # 3. Retrieve via NL trigger
        dialogue = CompanionDialogueEngine(memory=empty_memory_engine)
        reply, state = dialogue.handle_user_message("browse github")
        assert state == DialogueState.EXECUTING
        assert "Open GitHub in Browser" in reply

        # 4. Replay execution
        wf_to_replay = empty_memory_engine.get_workflow(wf_id)
        assert wf_to_replay is not None

        executor = AutonomousWorkflowExecutor(actuator=mock_actuator, bus=event_bus)
        result = executor.execute_workflow(wf_to_replay)
        assert result.success is True
        assert result.steps_completed == 2

        mock_actuator.assert_app_launched("com.apple.Safari")
        mock_actuator.assert_text_typed("https://github.com\n")
