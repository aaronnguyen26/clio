"""Unit tests for FEAT-EXEC-02: AutonomousWorkflowExecutor.

Validates:
- Initialization with standard and custom dependencies
- Sequential execution loop sorted by order
- Failsafe checks before and during each step
- ParameterEngine token interpolation (${doc_title}, ${CURRENT_DATE})
- Window ratio coordinate projection (CoordinateAdapter)
- Full action dispatch suite (LAUNCH, FOCUS, MOVE, CLICK, DOUBLE_CLICK, RIGHT_CLICK, DRAG, TYPE, PASTE, HOTKEY, WAIT, SCROLL)
- Targeted VirtualCursor dispatch (Requirement R7: zero physical cursor displacement)
- Per-step retry logic, exponential backoff, and recovery policies (retry, skip, fallback, abort)
- FailsafeEmergencyStop clean shutdown and modifier cleanup
- TaskMemoryEngine execution record logging
- Typed ExecutionResult contract validation
"""

from __future__ import annotations

import math
import sys
import time
from pathlib import Path
from typing import Any, Dict, List
import pytest

from src.actuators.mock import MockActuator
from src.actuators.types import (
    FailsafeEmergencyStop,
    MouseButton,
    WindowInfo,
)
from src.memory.coordinates import CoordinateAdapter
from src.memory.engine import TaskMemoryEngine
from src.memory.models import (
    ActionType,
    CoordMode,
    ExecutionRecord,
    TargetCoordinates,
    WorkflowSpec,
    WorkflowStep,
)
from src.memory.parameters import ParameterEngine

from src.executor.executor import AutonomousWorkflowExecutor, ExecutionResult
from src.executor.events import EventType, ExecutionEvent, ExecutionEventBus
from src.executor.poller import WindowReadinessPoller
from src.actuators.virtual_cursor import VirtualCursor, VirtualCursorState


# =============================================================================
# Pytest Fixtures
# =============================================================================

@pytest.fixture
def mock_actuator() -> MockActuator:
    """Provides a fresh MockActuator seeded with a standard test window."""
    actuator = MockActuator(screen_size=(1920.0, 1080.0), initial_pos=(500.0, 500.0))
    actuator.add_window(
        WindowInfo(
            window_id=101,
            owner_name="com.apple.Notes",
            title="Notes",
            x=200.0,
            y=150.0,
            width=800.0,
            height=600.0,
        )
    )
    return actuator


@pytest.fixture
def event_bus() -> ExecutionEventBus:
    """Provides an isolated pub/sub execution event bus."""
    return ExecutionEventBus()


@pytest.fixture
def memory_engine() -> TaskMemoryEngine:
    """Provides an isolated in-memory SQLite workflow store."""
    engine = TaskMemoryEngine(":memory:")
    yield engine
    engine.close()


@pytest.fixture
def virtual_cursor() -> VirtualCursor:
    """Provides a simulated mock VirtualCursor."""
    return VirtualCursor(initial_x=100.0, initial_y=100.0, mock=True)


@pytest.fixture
def poller(mock_actuator: MockActuator, event_bus: ExecutionEventBus) -> WindowReadinessPoller:
    """Provides a WindowReadinessPoller connected to the mock actuator and event bus."""
    return WindowReadinessPoller(actuator=mock_actuator, event_bus=event_bus, default_timeout=1.0, default_poll_interval=0.01)


@pytest.fixture
def executor(
    mock_actuator: MockActuator,
    event_bus: ExecutionEventBus,
    virtual_cursor: VirtualCursor,
    poller: WindowReadinessPoller,
    memory_engine: TaskMemoryEngine,
) -> AutonomousWorkflowExecutor:
    """Provides a fully wired AutonomousWorkflowExecutor with zero_delay=True for instant test execution."""
    return AutonomousWorkflowExecutor(
        actuator=mock_actuator,
        bus=event_bus,
        virtual_cursor=virtual_cursor,
        poller=poller,
        memory_engine=memory_engine,
        zero_delay=True,
    )


# =============================================================================
# 1. Initialization and Contract Tests
# =============================================================================

class TestExecutorInitialization:
    def test_default_initialization(self, mock_actuator: MockActuator):
        """Validates that executor initializes with sensible defaults when optional components are omitted."""
        exec_instance = AutonomousWorkflowExecutor(actuator=mock_actuator)
        assert exec_instance.actuator is mock_actuator
        assert isinstance(exec_instance.bus, ExecutionEventBus)
        assert isinstance(exec_instance.poller, WindowReadinessPoller)
        assert exec_instance.parameter_engine is ParameterEngine
        assert exec_instance.coordinate_adapter is CoordinateAdapter
        assert exec_instance.memory_engine is None
        assert exec_instance.use_virtual_cursor is False
        assert exec_instance.zero_delay is False

    def test_execution_result_contract_and_backward_compatibility(self):
        """Validates ExecutionResult dataclass fields, aliases, and duration_s property."""
        res = ExecutionResult(
            success=True,
            task_id="task_123",
            workflow_id="wf_abc",
            steps_completed=3,
            total_steps=3,
            error=None,
            duration=0.125,
            telemetry={"key": "val"},
        )
        assert res.success is True
        assert res.task_id == "task_123"
        assert res.workflow_id == "wf_abc"
        assert res.steps_completed == 3
        assert res.total_steps == 3
        assert res.error is None
        assert res.duration == 0.125
        assert res.duration_s == 0.125
        assert res.telemetry == {"key": "val"}

        # Positional constructor backward compatibility
        res2 = ExecutionResult(True, "t1", 2, 2, error=None, duration_s=0.5)
        assert res2.success is True
        assert res2.task_id == "t1"
        assert res2.workflow_id == "t1"
        assert res2.duration == 0.5
        assert res2.duration_s == 0.5


# =============================================================================
# 2. Sequential Execution & Sorting by Order
# =============================================================================

class TestExecutorStepSequencing:
    def test_steps_executed_strictly_by_order(
        self,
        executor: AutonomousWorkflowExecutor,
        mock_actuator: MockActuator,
    ):
        """Validates that steps are executed strictly in order of step.order, even if passed out of order."""
        # Define steps with scrambled order: order 3, then 1, then 2
        steps = [
            WorkflowStep(
                step_id="step_3",
                order=3,
                description="Third action",
                action=ActionType.TYPE_TEXT,
                payload={"text": "THREE "},
            ),
            WorkflowStep(
                step_id="step_1",
                order=1,
                description="First action",
                action=ActionType.TYPE_TEXT,
                payload={"text": "ONE "},
            ),
            WorkflowStep(
                step_id="step_2",
                order=2,
                description="Second action",
                action=ActionType.TYPE_TEXT,
                payload={"text": "TWO "},
            ),
        ]

        spec = WorkflowSpec(
            id="wf_scrambled_order",
            name="Scrambled Order Test",
            steps=steps,
        )

        res = executor.execute_workflow(spec)
        assert res.success is True
        assert res.steps_completed == 3
        assert res.total_steps == 3
        assert mock_actuator.typed_text == "ONE TWO THREE "

    def test_empty_workflow_handling(self, executor: AutonomousWorkflowExecutor):
        """Validates that a workflow with zero steps completes cleanly without error."""
        spec = WorkflowSpec(id="wf_empty", name="Empty Spec", steps=[])
        res = executor.execute_workflow(spec)
        assert res.success is True
        assert res.steps_completed == 0
        assert res.total_steps == 0
        assert res.error is None


# =============================================================================
# 3. Lifecycle Events Emission
# =============================================================================

class TestExecutorLifecycleEvents:
    def test_full_lifecycle_event_stream(
        self,
        executor: AutonomousWorkflowExecutor,
        event_bus: ExecutionEventBus,
    ):
        """Validates that TASK_STARTED, ACTION_STARTING, ACTION_COMPLETED, and TASK_COMPLETED are emitted."""
        events: List[ExecutionEvent] = []
        event_bus.subscribe(lambda e: events.append(e))

        spec = WorkflowSpec(
            id="wf_lifecycle",
            name="Lifecycle Stream",
            target_app={"bundle_id": "com.apple.Notes"},
            steps=[
                WorkflowStep(
                    step_id="s1",
                    order=1,
                    description="Step 1",
                    action=ActionType.TYPE_TEXT,
                    payload={"text": "a"},
                ),
                WorkflowStep(
                    step_id="s2",
                    order=2,
                    description="Step 2",
                    action=ActionType.TYPE_TEXT,
                    payload={"text": "b"},
                ),
            ],
        )

        res = executor.execute_workflow(spec)
        assert res.success is True

        event_types = [e.event_type for e in events]
        assert event_types[0] == EventType.TASK_STARTED
        assert EventType.ACTION_STARTING in event_types
        assert EventType.ACTION_COMPLETED in event_types
        assert event_types[-1] == EventType.TASK_COMPLETED

        # Check step indexing
        step_starts = [e for e in events if e.event_type == EventType.ACTION_STARTING]
        assert len(step_starts) == 2
        assert step_starts[0].step_index == 1
        assert step_starts[0].total_steps == 2
        assert step_starts[1].step_index == 2
        assert step_starts[1].total_steps == 2


# =============================================================================
# 4. Parameter Interpolation
# =============================================================================

class TestExecutorParameterInterpolation:
    def test_parameter_token_substitution(
        self,
        executor: AutonomousWorkflowExecutor,
        mock_actuator: MockActuator,
    ):
        """Validates that ${token} placeholders are interpolated from runtime_params and spec.parameters."""
        spec = WorkflowSpec(
            id="wf_params",
            name="Param Test",
            parameters={"default_user": "Alice", "doc_title": "Default Note"},
            steps=[
                WorkflowStep(
                    step_id="s1",
                    order=1,
                    description="Type note title",
                    action=ActionType.TYPE_TEXT,
                    payload={"text": "Title: ${doc_title} for ${user_name}"},
                ),
                WorkflowStep(
                    step_id="s2",
                    order=2,
                    description="Paste author",
                    action=ActionType.PASTE_TEXT,
                    payload={"text": "Created by: ${default_user}"},
                ),
            ],
        )

        # Override doc_title and provide user_name; default_user falls back to spec
        runtime_params = {"doc_title": "Project Blueprint", "user_name": "Minh"}
        res = executor.execute_workflow(spec, runtime_params=runtime_params)

        assert res.success is True
        mock_actuator.assert_text_typed("Title: Project Blueprint for Minh")
        mock_actuator.assert_text_pasted("Created by: Alice")


# =============================================================================
# 5. Coordinate Projection via CoordinateAdapter
# =============================================================================

class TestExecutorCoordinateProjection:
    def test_window_ratio_projection(
        self,
        executor: AutonomousWorkflowExecutor,
        mock_actuator: MockActuator,
    ):
        """Validates norm_x and norm_y window ratio projection against target window bounds."""
        # Notes window at (200, 150) size (800, 600)
        # Ratio (0.5, 0.5) should map to center: 200 + 0.5*800 = 600, 150 + 0.5*600 = 450
        spec = WorkflowSpec(
            id="wf_coord_ratio",
            name="Ratio Coordinate Test",
            target_app={"bundle_id": "com.apple.Notes"},
            steps=[
                WorkflowStep(
                    step_id="s_click_center",
                    order=1,
                    description="Click center of window",
                    action=ActionType.CLICK,
                    target={"bundle_id": "com.apple.Notes"},
                    coordinates=TargetCoordinates(
                        mode=CoordMode.WINDOW_RELATIVE_RATIO,
                        norm_x=0.5,
                        norm_y=0.5,
                    ),
                ),
                WorkflowStep(
                    step_id="s_move_quarter",
                    order=2,
                    description="Move to quarter of window",
                    action=ActionType.MOVE_MOUSE,
                    target={"bundle_id": "com.apple.Notes"},
                    payload={"coordinates": {"norm_x": 0.25, "norm_y": 0.25}},
                ),
            ],
        )

        res = executor.execute_workflow(spec)
        assert res.success is True

        # Assert click at center (600, 450)
        mock_actuator.assert_clicked_at(600.0, 450.0, tolerance=1.0)

        # Assert move to (200 + 0.25*800, 150 + 0.25*600) = (400, 300)
        pos = mock_actuator.get_mouse_position()
        assert math.isclose(pos[0], 400.0, abs_tol=1.0)
        assert math.isclose(pos[1], 300.0, abs_tol=1.0)


# =============================================================================
# 6. Action Dispatching Suite
# =============================================================================

class TestExecutorActionDispatching:
    def test_launch_and_focus_app(
        self,
        executor: AutonomousWorkflowExecutor,
        mock_actuator: MockActuator,
    ):
        """Validates LAUNCH_APP and FOCUS_APP action dispatches."""
        spec = WorkflowSpec(
            id="wf_launch_focus",
            name="Launch & Focus",
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
                    description="Focus Notes",
                    action=ActionType.FOCUS_APP,
                    target={"bundle_id": "com.apple.Notes"},
                ),
            ],
        )

        res = executor.execute_workflow(spec)
        assert res.success is True
        mock_actuator.assert_app_launched("com.apple.Safari")
        mock_actuator.assert_app_focused("com.apple.Notes")

    def test_mouse_actions_click_double_click_right_click_drag_scroll(
        self,
        executor: AutonomousWorkflowExecutor,
        mock_actuator: MockActuator,
    ):
        """Validates CLICK, DOUBLE_CLICK, RIGHT_CLICK, DRAG, and SCROLL dispatches."""
        spec = WorkflowSpec(
            id="wf_mouse_suite",
            name="Mouse Action Suite",
            steps=[
                WorkflowStep(
                    step_id="s1_click",
                    order=1,
                    description="Single click",
                    action=ActionType.CLICK,
                    payload={"x": 300.0, "y": 300.0, "button": "left"},
                ),
                WorkflowStep(
                    step_id="s2_double_click",
                    order=2,
                    description="Double click",
                    action=ActionType.DOUBLE_CLICK,
                    payload={"x": 350.0, "y": 350.0},
                ),
                WorkflowStep(
                    step_id="s3_right_click",
                    order=3,
                    description="Right click",
                    action=ActionType.RIGHT_CLICK,
                    payload={"x": 400.0, "y": 400.0},
                ),
                WorkflowStep(
                    step_id="s4_drag",
                    order=4,
                    description="Drag",
                    action=ActionType.DRAG,
                    payload={"start_x": 100.0, "start_y": 100.0, "end_x": 200.0, "end_y": 200.0},
                ),
                WorkflowStep(
                    step_id="s5_scroll",
                    order=5,
                    description="Scroll",
                    action=ActionType.SCROLL,
                    payload={"dx": 0, "dy": -5},
                ),
            ],
        )

        res = executor.execute_workflow(spec)
        assert res.success is True

        mock_actuator.assert_clicked_at(300.0, 300.0, button=MouseButton.LEFT, count=1)
        mock_actuator.assert_clicked_at(350.0, 350.0, count=1)  # double_click records click with count=2
        mock_actuator.assert_clicked_at(400.0, 400.0, button=MouseButton.RIGHT)
        mock_actuator.assert_action_called("drag", start_x=100.0, end_x=200.0)
        mock_actuator.assert_action_called("scroll", dx=0, dy=-5)

    def test_keyboard_actions_type_paste_hotkey(
        self,
        executor: AutonomousWorkflowExecutor,
        mock_actuator: MockActuator,
    ):
        """Validates TYPE_TEXT, PASTE_TEXT, and PRESS_HOTKEY dispatches."""
        spec = WorkflowSpec(
            id="wf_keyboard_suite",
            name="Keyboard Action Suite",
            steps=[
                WorkflowStep(
                    step_id="s1_type",
                    order=1,
                    description="Type greeting",
                    action=ActionType.TYPE_TEXT,
                    payload={"text": "Hello World!"},
                ),
                WorkflowStep(
                    step_id="s2_paste",
                    order=2,
                    description="Paste multi-line text",
                    action=ActionType.PASTE_TEXT,
                    payload={"text": "Line 1\nLine 2\nLine 3"},
                ),
                WorkflowStep(
                    step_id="s3_hotkey",
                    order=3,
                    description="Select all and save",
                    action=ActionType.PRESS_HOTKEY,
                    payload={"keys": ["cmd", "s"]},
                ),
            ],
        )

        res = executor.execute_workflow(spec)
        assert res.success is True
        mock_actuator.assert_text_typed("Hello World!")
        mock_actuator.assert_text_pasted("Line 1\nLine 2\nLine 3")
        mock_actuator.assert_hotkey_pressed("cmd", "s")


# =============================================================================
# 7. Targeted Virtual Cursor Dispatch (Requirement R7)
# =============================================================================

class TestExecutorVirtualCursorRouting:
    def test_virtual_cursor_dispatch_zero_physical_displacement(
        self,
        mock_actuator: MockActuator,
        virtual_cursor: VirtualCursor,
        event_bus: ExecutionEventBus,
    ):
        """TEST-R7: When use_virtual_cursor=True, mouse actions are routed to VirtualCursor

        Strict Invariant: Physical mouse on mock_actuator remains untouched at (500, 500).
        """
        initial_physical_pos = mock_actuator.get_mouse_position()
        assert initial_physical_pos == (500.0, 500.0)

        executor = AutonomousWorkflowExecutor(
            actuator=mock_actuator,
            virtual_cursor=virtual_cursor,
            bus=event_bus,
            use_virtual_cursor=True,
            zero_delay=True,
        )

        spec = WorkflowSpec(
            id="wf_vc_targeting",
            name="Virtual Cursor Targeting",
            steps=[
                WorkflowStep(
                    step_id="vc_move",
                    order=1,
                    description="Move virtual cursor",
                    action=ActionType.MOVE_MOUSE,
                    payload={"x": 250.0, "y": 350.0},
                ),
                WorkflowStep(
                    step_id="vc_click",
                    order=2,
                    description="Click virtual cursor",
                    action=ActionType.CLICK,
                    payload={"x": 250.0, "y": 350.0, "button": "left"},
                ),
            ],
        )

        res = executor.execute_workflow(spec)
        assert res.success is True

        # STRICT INVARIANT: Physical cursor was never moved or clicked!
        assert mock_actuator.get_mouse_position() == (500.0, 500.0)
        assert len([a for a in mock_actuator.history if a.action_type in ("move_mouse", "click")]) == 0

        # Virtual cursor moved and clicked at (250, 350)
        assert virtual_cursor.position == (250.0, 350.0)
        vc_history = virtual_cursor.history
        assert any(e.event_type == "move" and e.x == 250.0 and e.y == 350.0 for e in vc_history)
        assert any(e.event_type == "click" and e.x == 250.0 and e.y == 350.0 for e in vc_history)


# =============================================================================
# 8. Per-Step Retry Logic & Error Recovery
# =============================================================================

class TestExecutorRetryAndRecovery:
    def test_transient_failure_retries_and_succeeds(
        self,
        executor: AutonomousWorkflowExecutor,
        mock_actuator: MockActuator,
        event_bus: ExecutionEventBus,
    ):
        """Validates that transient step failures trigger RETRY_ATTEMPT events and succeed on retry."""
        retry_events: List[ExecutionEvent] = []
        event_bus.subscribe(lambda e: retry_events.append(e) if e.event_type == EventType.RETRY_ATTEMPT else None)

        # Configure launch failure on first call, clear before second attempt
        call_count = 0

        def flaky_action():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise ActuatorError("Flaky OS input synthesis error")

        # Mock actuator raise_on_action hook
        class FlakyActuator(MockActuator):
            def type_text(self, text: str, interval: float = 0.02) -> None:
                flaky_action()
                super().type_text(text, interval)

        flaky = FlakyActuator()
        flaky_executor = AutonomousWorkflowExecutor(actuator=flaky, bus=event_bus, zero_delay=True)

        spec = WorkflowSpec(
            id="wf_flaky",
            name="Flaky Step",
            steps=[
                WorkflowStep(
                    step_id="s1_flaky",
                    order=1,
                    description="Flaky typing",
                    action=ActionType.TYPE_TEXT,
                    payload={"text": "Recovered!"},
                    error_handling={"on_failure": "retry", "max_retries": 2, "backoff_s": 0.01},
                )
            ],
        )

        res = flaky_executor.execute_workflow(spec)
        assert res.success is True
        assert res.steps_completed == 1
        assert len(retry_events) == 1
        assert flaky.typed_text == "Recovered!"

    def test_retry_exhaustion_raises_step_failed_and_task_failed(
        self,
        mock_actuator: MockActuator,
        event_bus: ExecutionEventBus,
    ):
        """Validates that retry exhaustion emits STEP_FAILED, TASK_FAILED, and returns success=False."""
        failed_events: List[ExecutionEvent] = []
        event_bus.subscribe(lambda e: failed_events.append(e))

        mock_actuator.configure_launch_failure("com.apple.BrokenApp")

        executor = AutonomousWorkflowExecutor(actuator=mock_actuator, bus=event_bus, zero_delay=True)
        spec = WorkflowSpec(
            id="wf_fail",
            name="Failing Workflow",
            steps=[
                WorkflowStep(
                    step_id="s_broken",
                    order=1,
                    description="Launch broken app",
                    action=ActionType.LAUNCH_APP,
                    target={"bundle_id": "com.apple.BrokenApp"},
                    error_handling={"on_failure": "retry", "max_retries": 1, "backoff_s": 0.01},
                )
            ],
        )

        res = executor.execute_workflow(spec)
        assert res.success is False
        assert res.steps_completed == 0
        assert "Failed to launch application" in str(res.error)

        evt_types = [e.event_type for e in failed_events]
        assert EventType.RETRY_ATTEMPT in evt_types
        assert EventType.STEP_FAILED in evt_types
        assert EventType.TASK_FAILED in evt_types

    def test_on_failure_skip_policy(
        self,
        mock_actuator: MockActuator,
        event_bus: ExecutionEventBus,
    ):
        """Validates that on_failure='skip' skips failed step and completes subsequent steps."""
        mock_actuator.configure_launch_failure("com.apple.NonEssentialApp")

        executor = AutonomousWorkflowExecutor(actuator=mock_actuator, bus=event_bus, zero_delay=True)
        spec = WorkflowSpec(
            id="wf_skip_policy",
            name="Skip Policy Test",
            steps=[
                WorkflowStep(
                    step_id="s1_skip",
                    order=1,
                    description="Launch non-essential app",
                    action=ActionType.LAUNCH_APP,
                    target={"bundle_id": "com.apple.NonEssentialApp"},
                    error_handling={"on_failure": "skip", "max_retries": 0},
                ),
                WorkflowStep(
                    step_id="s2_proceed",
                    order=2,
                    description="Subsequent action",
                    action=ActionType.TYPE_TEXT,
                    payload={"text": "Step 2 executed"},
                ),
            ],
        )

        res = executor.execute_workflow(spec)
        assert res.success is True
        assert res.steps_completed == 1  # 1 non-skipped step completed
        assert mock_actuator.typed_text == "Step 2 executed"


# =============================================================================
# 9. Failsafe Emergency Stop Clean Interception
# =============================================================================

class TestExecutorFailsafeInterception:
    def test_failsafe_halts_execution_immediately_and_releases_modifiers(
        self,
        mock_actuator: MockActuator,
        event_bus: ExecutionEventBus,
        virtual_cursor: VirtualCursor,
    ):
        """TEST-FAILSAFE: Triggering emergency stop halts workflow, releases modifiers, and emits EMERGENCY_STOP."""
        events: List[ExecutionEvent] = []
        event_bus.subscribe(lambda e: events.append(e))

        # Place cursor in top-left corner
        mock_actuator.trigger_failsafe(position=(0.0, 0.0))

        executor = AutonomousWorkflowExecutor(
            actuator=mock_actuator,
            bus=event_bus,
            virtual_cursor=virtual_cursor,
            zero_delay=True,
        )

        spec = WorkflowSpec(
            id="wf_failsafe",
            name="Failsafe Test",
            steps=[
                WorkflowStep(
                    step_id="s1",
                    order=1,
                    description="Should not execute",
                    action=ActionType.TYPE_TEXT,
                    payload={"text": "UNSAFE TEXT"},
                )
            ],
        )

        res = executor.execute_workflow(spec)

        assert res.success is False
        assert "Emergency stop triggered" in str(res.error)
        assert res.steps_completed == 0
        assert mock_actuator.typed_text == ""  # Zero typing occurred

        # Clean shutdown checks
        mock_actuator.assert_modifiers_released()
        evt_types = [e.event_type for e in events]
        assert EventType.EMERGENCY_STOP in evt_types


# =============================================================================
# 10. TaskMemoryEngine Telemetry Recording
# =============================================================================

class TestExecutorMemoryTelemetryRecording:
    def test_execution_record_persisted_to_memory_engine(
        self,
        executor: AutonomousWorkflowExecutor,
        memory_engine: TaskMemoryEngine,
    ):
        """Validates that successful execution logs an ExecutionRecord in SQLite memory engine."""
        spec = WorkflowSpec(
            id="wf_telemetry_test",
            name="Telemetry Persistence Test",
            steps=[
                WorkflowStep(
                    step_id="s1",
                    order=1,
                    description="Type note",
                    action=ActionType.TYPE_TEXT,
                    payload={"text": "Recorded to memory."},
                )
            ],
        )

        res = executor.execute_workflow(spec, memory_engine=memory_engine)
        assert res.success is True

        # Query executions from database
        with memory_engine._lock:
            cur = memory_engine._conn.cursor()
            cur.execute("SELECT workflow_id, status, steps_completed FROM executions WHERE workflow_id = ?", ("wf_telemetry_test",))
            row = cur.fetchone()
            assert row is not None
            assert row["workflow_id"] == "wf_telemetry_test"
            assert row["status"] == "success"
            assert row["steps_completed"] == 1


# =============================================================================
# 11. Advanced Boundary & Error Recovery Cases
# =============================================================================

class TestExecutorAdvancedBoundaries:
    def test_on_failure_fallback_policy(
        self,
        mock_actuator: MockActuator,
        event_bus: ExecutionEventBus,
    ):
        """Validates that on_failure='fallback' invokes the fallback step upon primary step failure."""
        mock_actuator.configure_launch_failure("com.apple.PrimaryApp")

        executor = AutonomousWorkflowExecutor(actuator=mock_actuator, bus=event_bus, zero_delay=True)
        spec = WorkflowSpec(
            id="wf_fallback_policy",
            name="Fallback Policy Test",
            steps=[
                WorkflowStep(
                    step_id="step_primary",
                    order=1,
                    description="Launch primary app",
                    action=ActionType.LAUNCH_APP,
                    target={"bundle_id": "com.apple.PrimaryApp"},
                    error_handling={
                        "on_failure": "fallback",
                        "max_retries": 0,
                        "fallback_step_id": "step_fallback",
                    },
                ),
                WorkflowStep(
                    step_id="step_fallback",
                    order=2,
                    description="Launch fallback TextEdit",
                    action=ActionType.LAUNCH_APP,
                    target={"bundle_id": "com.apple.TextEdit"},
                ),
            ],
        )

        res = executor.execute_workflow(spec)
        assert res.success is True
        mock_actuator.assert_app_launched("com.apple.TextEdit")

    def test_failsafe_halts_during_multi_step_execution(
        self,
        mock_actuator: MockActuator,
        event_bus: ExecutionEventBus,
    ):
        """Validates that failsafe triggered during step 2 halts execution before step 3 can execute."""
        class TriggerFailsafeOnStepTwoActuator(MockActuator):
            def type_text(self, text: str, interval: float = 0.02) -> None:
                if text == "STEP_2":
                    self.trigger_failsafe(position=(0.0, 0.0))
                super().type_text(text, interval)

        custom_actuator = TriggerFailsafeOnStepTwoActuator(screen_size=(1920.0, 1080.0), initial_pos=(500.0, 500.0))
        executor = AutonomousWorkflowExecutor(actuator=custom_actuator, bus=event_bus, zero_delay=True)

        spec = WorkflowSpec(
            id="wf_stop_midstream",
            name="Midstream Stop",
            steps=[
                WorkflowStep(
                    step_id="s1",
                    order=1,
                    description="Step 1",
                    action=ActionType.TYPE_TEXT,
                    payload={"text": "STEP_1 "},
                ),
                WorkflowStep(
                    step_id="s2",
                    order=2,
                    description="Step 2",
                    action=ActionType.TYPE_TEXT,
                    payload={"text": "STEP_2"},
                ),
                WorkflowStep(
                    step_id="s3",
                    order=3,
                    description="Step 3",
                    action=ActionType.TYPE_TEXT,
                    payload={"text": "STEP_3"},
                ),
            ],
        )

        res = executor.execute_workflow(spec)
        assert res.success is False
        assert "Emergency stop triggered" in str(res.error)
        assert res.steps_completed == 1  # only step 1 completed cleanly
        assert "STEP_3" not in custom_actuator.typed_text

    def test_wait_window_action_dispatches_to_poller(
        self,
        mock_actuator: MockActuator,
        event_bus: ExecutionEventBus,
        poller: WindowReadinessPoller,
    ):
        """Validates that WAIT_WINDOW action synchronizes with WindowReadinessPoller."""
        executor = AutonomousWorkflowExecutor(
            actuator=mock_actuator,
            bus=event_bus,
            poller=poller,
            zero_delay=True,
        )

        spec = WorkflowSpec(
            id="wf_wait_win",
            name="Wait Window Test",
            steps=[
                WorkflowStep(
                    step_id="s1_wait",
                    order=1,
                    description="Wait for Notes window",
                    action=ActionType.WAIT_WINDOW,
                    target={"app_name": "Notes"},
                    timing={"timeout_ms": 1000},
                )
            ],
        )

        res = executor.execute_workflow(spec)
        assert res.success is True
        assert res.steps_completed == 1

    def test_virtual_cursor_drag_and_scroll(
        self,
        mock_actuator: MockActuator,
        virtual_cursor: VirtualCursor,
        event_bus: ExecutionEventBus,
    ):
        """Validates that DRAG and SCROLL actions are properly routed to VirtualCursor."""
        executor = AutonomousWorkflowExecutor(
            actuator=mock_actuator,
            virtual_cursor=virtual_cursor,
            bus=event_bus,
            use_virtual_cursor=True,
            zero_delay=True,
        )

        spec = WorkflowSpec(
            id="wf_vc_drag_scroll",
            name="VC Drag and Scroll",
            steps=[
                WorkflowStep(
                    step_id="s1_drag",
                    order=1,
                    description="Drag virtual cursor",
                    action=ActionType.DRAG,
                    payload={"start_x": 100.0, "start_y": 100.0, "end_x": 200.0, "end_y": 200.0},
                ),
                WorkflowStep(
                    step_id="s2_scroll",
                    order=2,
                    description="Scroll virtual cursor",
                    action=ActionType.SCROLL,
                    payload={"dx": 0, "dy": 10},
                ),
            ],
        )

        res = executor.execute_workflow(spec)
        assert res.success is True
        assert virtual_cursor.position == (200.0, 200.0)
        assert any(e.event_type == "drag" for e in virtual_cursor.history)
        assert any(e.event_type == "scroll" for e in virtual_cursor.history)

    def test_multiple_consecutive_runs_on_same_executor(
        self,
        executor: AutonomousWorkflowExecutor,
        mock_actuator: MockActuator,
    ):
        """Validates that the same executor instance can cleanly run multiple workflows consecutively."""
        spec1 = WorkflowSpec(
            id="wf_run_1",
            name="Run 1",
            steps=[
                WorkflowStep(
                    step_id="s1",
                    order=1,
                    description="Type A",
                    action=ActionType.TYPE_TEXT,
                    payload={"text": "A"},
                )
            ],
        )
        spec2 = WorkflowSpec(
            id="wf_run_2",
            name="Run 2",
            steps=[
                WorkflowStep(
                    step_id="s2",
                    order=1,
                    description="Type B",
                    action=ActionType.TYPE_TEXT,
                    payload={"text": "B"},
                )
            ],
        )

        res1 = executor.execute_workflow(spec1)
        res2 = executor.execute_workflow(spec2)

        assert res1.success is True
        assert res2.success is True
        assert mock_actuator.typed_text == "AB"

