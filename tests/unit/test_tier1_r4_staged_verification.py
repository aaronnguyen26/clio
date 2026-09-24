"""Tier 1: Feature Coverage for R4 — Staged Verification & Progressive Test Suite.

Tests validate:
- ActuatorFactory instantiating MockActuator in test/CI mode
- WindowReadinessPoller resolving without brittle sleeps
- AutonomousWorkflowExecutor sequentially executing multi-step workflows
- ExecutionEventBus pub/sub dispatch and payload preservation
- Closed-loop step execution and lifecycle event emissions
"""

import pytest
from tests.harness import (
    ActuatorFactory,
    MockActuator,
    WindowReadinessPoller,
    AutonomousWorkflowExecutor,
    ExecutionEventBus,
    ExecutionEvent,
    EventType,
    WorkflowSpec,
    WorkflowStep,
    ActionType,
    WindowInfo,
)


@pytest.mark.tier1
@pytest.mark.mock
class TestTier1StagedVerification:
    """Tier 1 tests for R4 staged verification and execution engine."""

    def test_actuator_factory_mock_in_ci(self):
        """TEST-T1-R4-01: ActuatorFactory returns MockActuator in mock mode."""
        actuator = ActuatorFactory.create(mode="mock")
        assert isinstance(actuator, MockActuator)
        assert actuator.get_frontmost_app() == "com.apple.finder"

    def test_window_readiness_poller_mock_success(self, mock_actuator: MockActuator):
        """TEST-T1-R4-02: WindowReadinessPoller resolves immediately when window is present."""
        mock_actuator.add_virtual_window(
            WindowInfo(
                window_id=42,
                owner_name="com.apple.Notes",
                title="Quick Note",
                x=100.0,
                y=100.0,
                width=600.0,
                height=400.0,
            )
        )

        window = WindowReadinessPoller.wait_for_window(
            actuator=mock_actuator,
            app_name="Notes",
            timeout=1.0,
            poll_interval=0.01,
        )
        assert window.window_id == 42
        assert window.owner_name == "com.apple.Notes"

    def test_executor_executes_multi_step_workflow(
        self, mock_actuator: MockActuator, event_bus: ExecutionEventBus
    ):
        """TEST-T1-R4-03: AutonomousWorkflowExecutor executes all steps sequentially."""
        spec = WorkflowSpec(
            id="wf_multi_step",
            name="Multi Step Automation",
            description="Launches app, types, and saves",
            steps=[
                WorkflowStep(
                    step_id="step_1",
                    order=1,
                    description="Launch TextEdit",
                    action=ActionType.LAUNCH_APP,
                    target={"bundle_id": "com.apple.TextEdit"},
                ),
                WorkflowStep(
                    step_id="step_2",
                    order=2,
                    description="Type note text",
                    action=ActionType.TYPE_TEXT,
                    payload={"text": "Progressive verification complete."},
                ),
                WorkflowStep(
                    step_id="step_3",
                    order=3,
                    description="Save file via Cmd+S",
                    action=ActionType.PRESS_HOTKEY,
                    payload={"keys": ["cmd", "s"]},
                ),
            ],
        )

        executor = AutonomousWorkflowExecutor(actuator=mock_actuator, bus=event_bus)
        result = executor.execute_workflow(spec)

        assert result.success is True
        assert result.steps_completed == 3
        assert result.total_steps == 3
        assert result.error is None

        # Verify actuator actions
        mock_actuator.assert_app_launched("com.apple.TextEdit")
        mock_actuator.assert_text_typed("Progressive verification complete.")
        mock_actuator.assert_hotkey_pressed("cmd", "s")

    def test_execution_event_bus_pub_sub(self, event_bus: ExecutionEventBus):
        """TEST-T1-R4-04: Pub/sub event bus notifies subscribers with typed events."""
        received_events = []

        def on_event(ev: ExecutionEvent):
            received_events.append(ev)

        event_bus.subscribe(on_event)

        ev = ExecutionEvent(
            event_type=EventType.TASK_STARTED,
            task_id="wf_test_bus",
            message="Test bus dispatch",
            total_steps=1,
        )
        event_bus.publish(ev)

        assert len(received_events) == 1
        assert received_events[0].event_type == EventType.TASK_STARTED
        assert received_events[0].task_id == "wf_test_bus"

        # Unsubscribe and verify no more events received
        event_bus.unsubscribe(on_event)
        event_bus.publish(ev)
        assert len(received_events) == 1

    def test_executor_step_retry_recovery(
        self, mock_actuator: MockActuator, event_bus: ExecutionEventBus
    ):
        """TEST-T1-R4-05: Emits lifecycle events across start, action, and completion."""
        lifecycle_events = []
        event_bus.subscribe(lambda e: lifecycle_events.append(e.event_type))

        spec = WorkflowSpec(
            id="wf_lifecycle",
            name="Lifecycle Test",
            description="Testing event flow",
            steps=[
                WorkflowStep(
                    step_id="s1",
                    order=1,
                    description="Single step",
                    action=ActionType.TYPE_TEXT,
                    payload={"text": "hello"},
                )
            ],
        )

        executor = AutonomousWorkflowExecutor(actuator=mock_actuator, bus=event_bus)
        res = executor.execute_workflow(spec)
        assert res.success is True

        assert EventType.TASK_STARTED in lifecycle_events
        assert EventType.ACTION_STARTING in lifecycle_events
        assert EventType.ACTION_COMPLETED in lifecycle_events
        assert EventType.TASK_COMPLETED in lifecycle_events
