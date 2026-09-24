"""Tier 1: Feature Coverage for R2 — Conversational Companion with Personality.

Tests validate:
- Natural language greeting parsing and state transitions
- Commentary tone profile switching (vibrant, concise, zen, developer)
- Direct intent resolution to executable workflows
- Event commentary generation and throttling
- Conversational task listing from memory
- Ambiguity detection and clarification prompting
"""

import time
import pytest
from tests.harness import (
    CompanionDialogueEngine,
    CommentaryEngine,
    DialogueState,
    ExecutionEventBus,
    ExecutionEvent,
    EventType,
    TaskMemoryEngine,
    WorkflowSpec,
)


@pytest.mark.tier1
@pytest.mark.mock
class TestTier1Companion:
    """Tier 1 tests for R2 conversational companion persona."""

    def test_companion_greeting_intent(self, dialogue_engine: CompanionDialogueEngine):
        """TEST-T1-R2-01: Processing casual greetings returns friendly intro."""
        reply, state = dialogue_engine.handle_user_message("Hello Clio!")
        assert state == DialogueState.IDLE
        assert "Autonomous Desktop Companion" in reply or "Hey" in reply
        assert "weekly to-do" in reply.lower()

    def test_companion_tone_profile_switching(self, event_bus: ExecutionEventBus):
        """TEST-T1-R2-02: Switching commentary tone changes generated phrasing."""
        commentary = CommentaryEngine(bus=event_bus, tone="vibrant", throttle_ms=0.0)

        event = ExecutionEvent(
            event_type=EventType.TASK_STARTED,
            task_id="wf_notes_weekly_todo",
            message="Starting task",
            total_steps=4,
        )

        # Vibrant tone
        vibrant_msg = commentary.handle_event(event)
        assert "Alright" in vibrant_msg or "hands-free" in vibrant_msg

        # Concise tone
        commentary.set_tone("concise")
        concise_msg = commentary.handle_event(event)
        assert "Starting task: wf_notes_weekly_todo" in concise_msg

        # Zen tone
        commentary.set_tone("zen")
        zen_msg = commentary.handle_event(event)
        assert "Beginning" in zen_msg or "relax" in zen_msg

        # Developer tone
        commentary.set_tone("developer")
        dev_msg = commentary.handle_event(event)
        assert "[TASK_START]" in dev_msg

    def test_companion_intent_direct_execution(self, dialogue_engine: CompanionDialogueEngine):
        """TEST-T1-R2-03: Direct workflow trigger transitions to EXECUTING."""
        reply, state = dialogue_engine.handle_user_message("write my weekly to-do list")
        assert state == DialogueState.EXECUTING
        assert "Create Weekly To-Do in Apple Notes" in reply or "Starting" in reply

    def test_companion_commentary_throttling(self, event_bus: ExecutionEventBus):
        """TEST-T1-R2-04: Throttling debounces rapid low-priority events."""
        commentary = CommentaryEngine(bus=event_bus, tone="vibrant", throttle_ms=800.0)

        event1 = ExecutionEvent(
            event_type=EventType.ACTION_STARTING,
            task_id="wf_test",
            message="Step 1 in progress",
            step_index=1,
            total_steps=5,
        )
        msg1 = commentary.handle_event(event1)
        assert msg1 is not None

        # Immediate second low-priority event should be throttled
        event2 = ExecutionEvent(
            event_type=EventType.ACTION_STARTING,
            task_id="wf_test",
            message="Step 2 in progress",
            step_index=2,
            total_steps=5,
        )
        msg2 = commentary.handle_event(event2)
        assert msg2 is None, "Expected rapid event to be throttled"

        # High priority events (EMERGENCY_STOP) must NOT be throttled
        emergency_event = ExecutionEvent(
            event_type=EventType.EMERGENCY_STOP,
            task_id="wf_test",
            message="Aborting immediately",
        )
        msg3 = commentary.handle_event(emergency_event)
        assert msg3 is not None
        assert "Emergency stop" in msg3 or "🛑" in msg3

    def test_companion_conversational_task_listing(self, dialogue_engine: CompanionDialogueEngine):
        """TEST-T1-R2-05: Querying learned workflows returns list of tasks."""
        reply, state = dialogue_engine.handle_user_message("What tasks do you remember?")
        assert state == DialogueState.IDLE
        assert "Create Weekly To-Do in Apple Notes" in reply

    def test_companion_clarification_on_ambiguity(
        self, empty_memory_engine: TaskMemoryEngine
    ):
        """TEST-T1-R2-06: Ambiguous trigger prompt prompts user for clarification."""
        # Add two workflows that share an ambiguous trigger alias "open notes"
        wf1 = WorkflowSpec(
            id="wf_notes_weekly",
            name="Weekly Planning in Apple Notes",
            description="Opens Notes for weekly planning",
            triggers={"canonical": "open weekly notes", "aliases": ["open notes"]},
        )
        wf2 = WorkflowSpec(
            id="wf_notes_scratchpad",
            name="Scratchpad in Apple Notes",
            description="Opens Notes for quick scratchpad",
            triggers={"canonical": "open scratchpad notes", "aliases": ["open notes"]},
        )
        empty_memory_engine.save_workflow(wf1)
        empty_memory_engine.save_workflow(wf2)

        dialogue = CompanionDialogueEngine(memory=empty_memory_engine)
        reply, state = dialogue.handle_user_message("open notes")
        assert state == DialogueState.CLARIFYING
        assert "Did you mean one of these" in reply
        assert "Weekly Planning in Apple Notes" in reply
        assert "Scratchpad in Apple Notes" in reply
