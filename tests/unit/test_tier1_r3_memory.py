"""Tier 1: Feature Coverage for R3 — Teach-Mode Workflow Memory & Retrieval.

Tests validate:
- WorkflowSpec persistence and JSON schema round-trip
- Version history increment and audit trail
- Natural language exact, alias, and fuzzy trigger matching
- Parameter token interpolation (${var}, ${CURRENT_DATE})
- Resolution-independent ratio coordinate projection
- Execution telemetry logging
"""

import datetime
from typing import Generator
import pytest

from src.actuators.types import WindowInfo
from src.memory.coordinates import CoordinateAdapter
from src.memory.engine import TaskMemoryEngine
from src.memory.models import ActionType, WorkflowSpec, WorkflowStep
from src.memory.parameters import ParameterEngine
from src.memory.retrieval import NLRetrievalEngine


@pytest.fixture
def empty_memory_engine() -> Generator[TaskMemoryEngine, None, None]:
    """Provides an isolated, empty in-memory SQLite workflow store."""
    engine = TaskMemoryEngine(":memory:")
    yield engine
    engine.close()


@pytest.fixture
def memory_engine(empty_memory_engine: TaskMemoryEngine) -> TaskMemoryEngine:
    """Provides an in-memory workflow store pre-seeded with the Notes benchmark."""
    todo_content = (
        "# 📋 Weekly Action Plan & Priorities\n\n"
        "[ ] Monday: Team sync, triage critical backlog, and align sprint goals\n"
        "[ ] Tuesday: Deep work on core architecture & refactoring\n"
        "[ ] Wednesday: Code reviews, unit testing, and integration verification\n"
        "[ ] Thursday: Performance profiling, benchmark validation, and bug hardening\n"
        "[ ] Friday: Weekly retrospective, release sign-off, and celebrate wins\n\n"
        "---\nGenerated autonomously by Autonomous Desktop Companion\n"
    )

    steps = [
        WorkflowStep(
            step_id="s1_launch",
            order=1,
            description="Launch Apple Notes application",
            action=ActionType.LAUNCH_APP,
            target={"bundle_id": "com.apple.Notes", "app_name": "Notes"},
        ),
        WorkflowStep(
            step_id="s2_focus",
            order=2,
            description="Focus Apple Notes window",
            action=ActionType.FOCUS_APP,
            target={"bundle_id": "com.apple.Notes"},
        ),
        WorkflowStep(
            step_id="s3_new_note",
            order=3,
            description="Create new note via Cmd+N shortcut",
            action=ActionType.PRESS_HOTKEY,
            payload={"keys": ["cmd", "n"]},
        ),
        WorkflowStep(
            step_id="s4_type_content",
            order=4,
            description="Inject formatted weekly to-do list",
            action=ActionType.PASTE_TEXT,
            payload={"text": todo_content},
        ),
    ]

    spec = WorkflowSpec(
        id="wf_notes_weekly_todo",
        name="Create Weekly To-Do in Apple Notes",
        description="Opens Apple Notes, creates a new note, and types weekly to-do list hands-free.",
        triggers={
            "canonical": "write my weekly to-do list",
            "aliases": [
                "plan my week in notes",
                "create weekly to-do list in notes",
                "new weekly todo note",
            ],
            "keywords": ["notes", "weekly", "todo", "plan"],
        },
        target_app={"bundle_id": "com.apple.Notes", "app_name": "Notes"},
        steps=steps,
    )
    empty_memory_engine.save_workflow(spec)
    return empty_memory_engine


@pytest.mark.tier1
@pytest.mark.mock
class TestTier1Memory:
    """Tier 1 tests for R3 task memory and retrieval engine."""

    def test_memory_save_and_retrieve_workflow(self, empty_memory_engine: TaskMemoryEngine):
        """TEST-T1-R3-01: Saving and retrieving workflow spec preserves structure."""
        spec = WorkflowSpec(
            id="wf_custom_task",
            name="Custom User Task",
            description="Performs automated typing",
            steps=[
                WorkflowStep(
                    step_id="step_1",
                    order=1,
                    description="Type greeting",
                    action=ActionType.TYPE_TEXT,
                    payload={"text": "Hello world"},
                )
            ],
        )

        wf_id = empty_memory_engine.save_workflow(spec)
        assert wf_id == "wf_custom_task"

        retrieved = empty_memory_engine.get_workflow("wf_custom_task")
        assert retrieved is not None
        assert retrieved.name == "Custom User Task"
        assert len(retrieved.steps) == 1
        assert retrieved.steps[0].action == ActionType.TYPE_TEXT
        assert retrieved.steps[0].payload["text"] == "Hello world"

    def test_memory_version_history_increment(self, empty_memory_engine: TaskMemoryEngine):
        """TEST-T1-R3-02: Updating workflow increments version number and preserves history."""
        spec = WorkflowSpec(
            id="wf_version_test",
            name="Version 1 Task",
            description="Initial description",
            version=1,
        )
        empty_memory_engine.save_workflow(spec, change_summary="Initial commit")

        # Retrieve v1
        v1 = empty_memory_engine.get_workflow("wf_version_test")
        assert v1.version == 1

        # Update spec to v2
        spec.name = "Version 2 Task"
        spec.description = "Updated description"
        empty_memory_engine.save_workflow(spec, change_summary="Updated title and desc")

        # Current active should be v2
        current = empty_memory_engine.get_workflow("wf_version_test")
        assert current.version == 2
        assert current.name == "Version 2 Task"

        # Explicitly fetching v1 from history
        old_v1 = empty_memory_engine.get_workflow("wf_version_test", version=1)
        assert old_v1 is not None
        assert old_v1.version == 1
        assert old_v1.name == "Version 1 Task"

    def test_memory_nl_exact_and_alias_match(self, memory_engine: TaskMemoryEngine):
        """TEST-T1-R3-03: Natural language retrieval matches canonical and alias triggers."""
        retrieval = NLRetrievalEngine(memory_engine)

        # 1. Exact canonical
        matches_canonical = retrieval.query("write my weekly to-do list")
        assert len(matches_canonical) >= 1
        assert matches_canonical[0].workflow_id == "wf_notes_weekly_todo"
        assert matches_canonical[0].confidence == 1.0

        # 2. Alias match
        matches_alias = retrieval.query("plan my week in notes")
        assert len(matches_alias) >= 1
        assert matches_alias[0].workflow_id == "wf_notes_weekly_todo"
        assert matches_alias[0].confidence >= 0.90

        # 3. Substring / partial match
        matches_partial = retrieval.query("weekly to-do list")
        assert len(matches_partial) >= 1
        assert matches_partial[0].confidence >= 0.80

    def test_memory_parameter_interpolation(self):
        """TEST-T1-R3-04: Variable tokens are replaced with runtime parameters."""
        template = "Meeting Agenda: ${meeting_title}\nDate: ${CURRENT_DATE}\nItems:\n${action_items}"
        params = {
            "meeting_title": "Sprint Planning",
            "action_items": ["Review Backlog", "Assign Stories", "Estimate Points"],
        }

        output = ParameterEngine.interpolate(template, params)
        assert "Meeting Agenda: Sprint Planning" in output
        assert datetime.date.today().strftime("%A") in output
        assert "- Review Backlog" in output
        assert "- Assign Stories" in output

    def test_memory_coordinate_ratio_projection(self):
        """TEST-T1-R3-05: Translates normalized ratio coordinates to screen pixels."""
        window = WindowInfo(
            window_id=10,
            owner_name="com.apple.Notes",
            title="Notes",
            x=200.0,
            y=100.0,
            width=800.0,
            height=600.0,
        )

        # Center of window (0.5, 0.5)
        # Expected screen X: 200 + 400 = 600
        # Expected screen Y: 100 + 300 = 400
        screen_x, screen_y = CoordinateAdapter.to_screen_coordinates(0.5, 0.5, window)
        assert screen_x == 600
        assert screen_y == 400

        # Top-left corner (0.0, 0.0)
        top_left_x, top_left_y = CoordinateAdapter.to_screen_coordinates(0.0, 0.0, window)
        assert top_left_x == 200
        assert top_left_y == 100

        # Bottom-right corner (1.0, 1.0)
        br_x, br_y = CoordinateAdapter.to_screen_coordinates(1.0, 1.0, window)
        assert br_x == 1000
        assert br_y == 700

    def test_memory_execution_telemetry_logging(self, empty_memory_engine: TaskMemoryEngine):
        """TEST-T1-R3-06: Telemetry writes execution records and returns row id."""
        row_id = empty_memory_engine.record_execution(
            workflow_id="wf_notes_weekly_todo",
            status="success",
            duration_ms=450,
            steps_completed=4,
        )
        assert row_id is not None
        assert row_id >= 1
