"""Pytest fixtures and configuration for Autonomous Desktop Companion tests.

Provides isolated mock actuators, in-memory databases, event buses,
and workflow recipes for fast, deterministic, opaque-box testing.
"""

from typing import Generator
import pytest

from tests.harness import (
    MockActuator,
    TaskMemoryEngine,
    ExecutionEventBus,
    CommentaryEngine,
    AutonomousWorkflowExecutor,
    CompanionDialogueEngine,
    WorkflowSpec,
    WindowInfo,
    create_notes_benchmark_workflow,
)


@pytest.fixture
def mock_actuator() -> Generator[MockActuator, None, None]:
    """Provides a clean, isolated MockActuator with standard virtual desktop state."""
    actuator = MockActuator(screen_size=(1920.0, 1080.0))
    # Seed initial virtual window for Finder/Desktop
    actuator.add_virtual_window(
        WindowInfo(
            window_id=1,
            owner_name="com.apple.finder",
            title="Desktop",
            x=0.0,
            y=0.0,
            width=1920.0,
            height=1080.0,
        )
    )
    yield actuator
    # Teardown safety invariant: verify all modifiers released
    actuator.stop()
    actuator.assert_modifiers_released()


@pytest.fixture
def empty_memory_engine() -> Generator[TaskMemoryEngine, None, None]:
    """Provides an isolated, empty in-memory SQLite workflow store."""
    engine = TaskMemoryEngine(":memory:")
    yield engine
    engine.close()


@pytest.fixture
def memory_engine(empty_memory_engine: TaskMemoryEngine) -> TaskMemoryEngine:
    """Provides an in-memory workflow store pre-seeded with the Notes benchmark."""
    spec = create_notes_benchmark_workflow()
    empty_memory_engine.save_workflow(spec)
    return empty_memory_engine


@pytest.fixture
def event_bus() -> ExecutionEventBus:
    """Provides a fresh pub/sub execution event bus."""
    return ExecutionEventBus()


@pytest.fixture
def commentary_engine(event_bus: ExecutionEventBus) -> CommentaryEngine:
    """Provides a CommentaryEngine subscribed to the event bus with vibrant tone."""
    return CommentaryEngine(bus=event_bus, tone="vibrant", throttle_ms=0.0)


@pytest.fixture
def executor(
    mock_actuator: MockActuator, event_bus: ExecutionEventBus
) -> AutonomousWorkflowExecutor:
    """Provides an AutonomousWorkflowExecutor wired to mock actuator and event bus."""
    return AutonomousWorkflowExecutor(actuator=mock_actuator, bus=event_bus)


@pytest.fixture
def dialogue_engine(memory_engine: TaskMemoryEngine) -> CompanionDialogueEngine:
    """Provides a CompanionDialogueEngine connected to memory with default vibrant tone."""
    return CompanionDialogueEngine(memory=memory_engine, tone="vibrant")


@pytest.fixture
def notes_benchmark_wf() -> WorkflowSpec:
    """Returns the canonical Apple Notes weekly to-do list workflow specification."""
    return create_notes_benchmark_workflow()
