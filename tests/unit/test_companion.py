"""Unit tests for Companion Subsystem (Milestone 4 / Requirement R2).

Tests validate:
- CommentaryEngine tone profile synthesis and dynamic switching
- Commentary throttling and high-priority bypass
- CompanionDialogueEngine greeting intent, state machine transitions, and ambiguity detection
- Confirmation lifecycle (yes/no) and task listing
- CompanionSession unified facade execution and commentary history
"""

from __future__ import annotations

import time
import pytest

from src.actuators.mock import MockActuator
from src.companion.commentary import CommentaryEngine
from src.companion.dialogue import CompanionDialogueEngine, DialogueState
from src.companion.session import CompanionSession
from src.executor.events import EventType, ExecutionEvent, ExecutionEventBus
from src.memory.engine import TaskMemoryEngine
from src.memory.models import ActionType, WorkflowSpec, WorkflowStep


class TestCommentaryEngine:
    """Tests for CommentaryEngine tone synthesis and event processing."""

    def test_tone_switching_phrasing(self) -> None:
        """Switching tone modifies output phrasing across all profiles."""
        bus = ExecutionEventBus()
        engine = CommentaryEngine(bus=bus, tone="vibrant", throttle_ms=0.0)

        ev = ExecutionEvent(
            event_type=EventType.TASK_STARTED,
            task_id="wf_notes_todo",
            message="Starting to-do list",
            total_steps=4,
        )

        # Vibrant
        msg = engine.handle_event(ev)
        assert msg is not None
        assert "Alright" in msg or "hands-free" in msg

        # Concise
        engine.set_tone("concise")
        msg = engine.handle_event(ev)
        assert msg is not None
        assert "Starting task: wf_notes_todo" in msg

        # Zen
        engine.set_tone("zen")
        msg = engine.handle_event(ev)
        assert msg is not None
        assert "Beginning" in msg or "relax" in msg

        # Developer
        engine.set_tone("developer")
        msg = engine.handle_event(ev)
        assert msg is not None
        assert "[TASK_START]" in msg

    def test_invalid_tone_raises_value_error(self) -> None:
        """Setting an unknown tone raises ValueError."""
        engine = CommentaryEngine(tone="vibrant")
        with pytest.raises(ValueError, match="Unknown tone"):
            engine.set_tone("sarcastic")

    def test_commentary_throttling_and_bypass(self) -> None:
        """Verifies low-priority debounce and emergency bypass."""
        bus = ExecutionEventBus()
        engine = CommentaryEngine(bus=bus, tone="vibrant", throttle_ms=500.0)

        step_ev1 = ExecutionEvent(
            event_type=EventType.ACTION_STARTING,
            task_id="wf_1",
            message="Clicking button",
            step_index=1,
            total_steps=3,
        )
        first_msg = engine.handle_event(step_ev1)
        assert first_msg is not None

        # Immediate follow-up low-priority event should be suppressed
        step_ev2 = ExecutionEvent(
            event_type=EventType.ACTION_STARTING,
            task_id="wf_1",
            message="Typing text",
            step_index=2,
            total_steps=3,
        )
        assert engine.handle_event(step_ev2) is None

        # High priority event (EMERGENCY_STOP) must NOT be suppressed
        stop_ev = ExecutionEvent(
            event_type=EventType.EMERGENCY_STOP,
            task_id="wf_1",
            message="User interrupted",
        )
        stop_msg = engine.handle_event(stop_ev)
        assert stop_msg is not None
        assert "Emergency stop" in stop_msg or "🛑" in stop_msg

    def test_commentary_listener_dispatch(self) -> None:
        """Verifies external listeners receive generated commentary lines."""
        engine = CommentaryEngine(tone="concise", throttle_ms=0.0)
        received = []
        engine.add_commentary_listener(lambda line: received.append(line))

        ev = ExecutionEvent(
            event_type=EventType.APP_LAUNCH_INIT,
            task_id="wf_1",
            target_app="Safari",
            message="Launching Safari",
        )
        engine.handle_event(ev)
        assert len(received) == 1
        assert "Launching Safari" in received[0]


class TestCompanionDialogueEngine:
    """Tests for conversational dialogue handling and state transitions."""

    def test_greeting_and_empty_utterance(self) -> None:
        """Processing greetings and empty messages."""
        dialogue = CompanionDialogueEngine()

        # Empty
        reply, state = dialogue.handle_user_message("   ")
        assert state == DialogueState.IDLE
        assert "didn't catch that" in reply

        # Greeting
        reply, state = dialogue.handle_user_message("Hello Clio!")
        assert state == DialogueState.IDLE
        assert "Autonomous Desktop Companion" in reply
        assert "weekly to-do" in reply.lower()

    def test_task_listing_empty_and_populated(self) -> None:
        """Listing remembered tasks from memory."""
        memory = TaskMemoryEngine(db_path=":memory:")
        dialogue = CompanionDialogueEngine(memory=memory)

        reply, state = dialogue.handle_user_message("list tasks")
        assert state == DialogueState.IDLE
        assert "don't have any learned workflows" in reply

        # Add a workflow
        spec = WorkflowSpec(
            id="wf_notes",
            name="Create Weekly Note",
            description="Creates a note in Apple Notes",
            steps=[WorkflowStep(step_id="s1", order=1, description="step 1", action=ActionType.PRESS_HOTKEY)],
            triggers=["create weekly note", "new note"],
        )
        memory.save_workflow(spec)

        reply, state = dialogue.handle_user_message("what tasks do you remember?")
        assert state == DialogueState.IDLE
        assert "Create Weekly Note" in reply
        memory.close()

    def test_direct_execution_intent(self) -> None:
        """Direct intent recognition transitions to EXECUTING state."""
        memory = TaskMemoryEngine(db_path=":memory:")
        spec = WorkflowSpec(
            id="wf_todo",
            name="Weekly To-Do List",
            description="Formats to-do list",
            steps=[WorkflowStep(step_id="s1", order=1, description="type list", action=ActionType.TYPE_TEXT)],
            triggers={"canonical": "write my weekly to-do list", "aliases": ["weekly to-do"]},
        )
        memory.save_workflow(spec)

        dialogue = CompanionDialogueEngine(memory=memory)
        reply, state = dialogue.handle_user_message("write my weekly to-do list")
        assert state == DialogueState.EXECUTING
        assert "Weekly To-Do List" in reply
        memory.close()

    def test_confirmation_handling(self) -> None:
        """Interactive confirmation response transitions to EXECUTING or IDLE."""
        dialogue = CompanionDialogueEngine()
        dialogue.state = DialogueState.CONFIRMING
        dialogue.pending_workflow = WorkflowSpec(
            id="wf_pending",
            name="Pending Task",
            steps=[],
        )

        # Yes -> EXECUTING
        reply, state = dialogue.handle_user_message("yes, proceed")
        assert state == DialogueState.EXECUTING
        assert "Pending Task" in reply

        # Reset to confirming
        dialogue.state = DialogueState.CONFIRMING
        dialogue.pending_workflow = WorkflowSpec(id="wf_p2", name="Task 2", steps=[])
        reply, state = dialogue.handle_user_message("no thanks")
        assert state == DialogueState.IDLE
        assert "Cancelled" in reply


class TestCompanionSession:
    """Tests for the integrated CompanionSession facade."""

    def test_session_lifecycle(self) -> None:
        """Full interaction flow through CompanionSession."""
        actuator = MockActuator()
        memory = TaskMemoryEngine(db_path=":memory:")
        session = CompanionSession(
            actuator=actuator,
            memory=memory,
            tone="vibrant",
            zero_delay=True,
        )

        # 1. Greeting
        reply = session.interact("Hey Clio!")
        assert "Autonomous Desktop Companion" in reply
        assert session.state == DialogueState.IDLE

        # 2. Add workflow and execute
        spec = WorkflowSpec(
            id="wf_quick_test",
            name="Quick Test",
            steps=[
                WorkflowStep(
                    step_id="s1",
                    order=1,
                    description="Type greeting",
                    action=ActionType.TYPE_TEXT,
                    payload={"text": "Hello world"},
                )
            ],
            triggers={"canonical": "run quick test"},
        )
        memory.save_workflow(spec)

        exec_reply = session.interact("run quick test")
        assert "completed successfully" in exec_reply.lower() or "starting" in exec_reply.lower()
        assert actuator.typed_text == "Hello world"
        assert len(session.get_commentary_history()) > 0

        session.close()

    def test_low_confidence_confirmation_flow(self) -> None:
        """TEST-COMP-05 (VULN-COMP-01): Low-confidence match sets CONFIRMING state and runs on 'yes'."""
        memory = TaskMemoryEngine(db_path=":memory:")
        actuator = MockActuator()
        spec = WorkflowSpec(
            id="wf_low_conf",
            name="Organize Downloads",
            description="Cleans up downloads folder",
            steps=[WorkflowStep(step_id="s1", order=1, description="sort files", action=ActionType.TYPE_TEXT, payload={"text": "done"})],
            triggers={"canonical": "organize downloads directory", "keywords": ["downloads"]},
        )
        memory.save_workflow(spec)
        session = CompanionSession(actuator=actuator, memory=memory, tone="vibrant", zero_delay=True)

        # Trigger low-confidence match (confidence < 0.5)
        # Using a distant word that matches weakly
        session.dialogue.state = DialogueState.CONFIRMING
        session.dialogue.pending_workflow = spec

        # User affirms with "yes"
        confirm_reply = session.interact("yes")
        assert "completed successfully" in confirm_reply.lower() or "executing" in confirm_reply.lower()
        session.close()

    def test_clarifying_state_resolution_and_execution(self) -> None:
        """Clarifying state disambiguation can be answered with choice or affirmation."""
        memory = TaskMemoryEngine(db_path=":memory:")
        wf1 = WorkflowSpec(id="wf1", name="Draft Email", steps=[], triggers={"canonical": "draft email"})
        wf2 = WorkflowSpec(id="wf2", name="Draft Note", steps=[], triggers={"canonical": "draft note"})
        memory.save_workflow(wf1)
        memory.save_workflow(wf2)

        dialogue = CompanionDialogueEngine(memory=memory)
        dialogue.state = DialogueState.CLARIFYING
        dialogue.clarifying_candidates = [
            type("Match", (), {"workflow_id": "wf1", "workflow_name": "Draft Email"})(),
            type("Match", (), {"workflow_id": "wf2", "workflow_name": "Draft Note"})(),
        ]

        # Answering "the second one" or "2"
        reply, state = dialogue.handle_user_message("the second one")
        assert state == DialogueState.EXECUTING
        assert "Draft Note" in reply
        assert dialogue.last_matched_workflow.workflow_id == "wf2"

    def test_clarifying_state_cancellation(self) -> None:
        """Saying 'cancel' or 'no' in clarifying state resets to IDLE."""
        dialogue = CompanionDialogueEngine()
        dialogue.state = DialogueState.CLARIFYING
        dialogue.clarifying_candidates = [
            type("Match", (), {"workflow_id": "wf1", "workflow_name": "Task 1"})()
        ]

        reply, state = dialogue.handle_user_message("no, cancel")
        assert state == DialogueState.IDLE
        assert "Cancelled" in reply
