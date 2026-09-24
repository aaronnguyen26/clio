"""Conversational Companion Dialogue Subsystem.

Belongs to Milestone M4 (Conversational Companion Persona & Intent Routing).
Manages conversational state transitions, greeting intents, memory retrieval,
and interactive workflow confirmations.
"""

from __future__ import annotations

from enum import Enum
import re
from typing import Any, Dict, List, Optional, Tuple

from src.memory.engine import TaskMemoryEngine
from src.memory.models import MatchResult, WorkflowSpec
from src.memory.retrieval import NLRetrievalEngine


class DialogueState(str, Enum):
    """Lifecycle states of the companion dialogue state machine."""

    IDLE = "idle"
    INTENT_PARSING = "intent_parsing"
    CLARIFYING = "clarifying"
    CONFIRMING = "confirming"
    EXECUTING = "executing"
    COMPLETED = "completed"


class CompanionDialogueEngine:
    """Conversational persona managing states, greetings, memory retrieval, and intent."""

    def __init__(
        self,
        memory: Optional[TaskMemoryEngine] = None,
        tone: str = "vibrant",
    ) -> None:
        self.memory = memory or TaskMemoryEngine()
        self.retrieval = NLRetrievalEngine(self.memory)
        self.state = DialogueState.IDLE
        self.tone = tone
        self.pending_workflow: Optional[WorkflowSpec] = None

    def handle_user_message(self, message: str) -> Tuple[str, DialogueState]:
        """Processes a natural language user utterance and returns (reply_text, new_state)."""
        text = message.strip()
        if not text:
            return "Hey there! I didn't catch that. How can I help you today? ✨", self.state

        # Check for conversational prefix like "Hey Clio, " or "Please "
        cleaned_intent = re.sub(
            r"^(hey|hello|hi)(\s+clio)?[,!]?\s*", "", text, flags=re.IGNORECASE
        ).strip()
        cleaned_intent = re.sub(r"^please\s+", "", cleaned_intent, flags=re.IGNORECASE).strip()

        # Pure Greetings (no subsequent action intent)
        if not cleaned_intent and any(
            w in text.lower() for w in ["hello", "hi", "hey", "who are you"]
        ):
            self.state = DialogueState.IDLE
            return (
                "Hey! I'm your Autonomous Desktop Companion! 🌟 I can observe tasks, remember workflows, "
                "or operate your desktop hands-free. Try saying 'write my weekly to-do list'!",
                self.state,
            )

        # Task listing query
        if "what tasks do you remember" in text.lower() or "list tasks" in text.lower():
            workflows = self.memory.list_workflows()
            if not workflows:
                return (
                    "I don't have any learned workflows yet! You can teach me one anytime.",
                    self.state,
                )
            items = ", ".join(f"'{w['name']}'" for w in workflows)
            return f"Here are the workflows I remember: {items}.", self.state

        # Confirmation handling
        if self.state == DialogueState.CONFIRMING:
            text_lower = text.lower()
            if any(affirm in text_lower for affirm in ("yes", "y", "sure", "proceed", "do it", "ok", "yep", "go ahead")):
                self.state = DialogueState.EXECUTING
                wf = self.pending_workflow
                self.pending_workflow = None
                return (
                    f"Got it! Executing '{wf.name if wf else 'task'}' now.",
                    self.state,
                )
            else:
                self.state = DialogueState.IDLE
                self.pending_workflow = None
                return "Cancelled! What would you like to do instead?", self.state

        # Intent search on both original text and cleaned intent
        matches: List[MatchResult] = self.retrieval.query(
            cleaned_intent if cleaned_intent else text
        )
        if not matches and cleaned_intent != text:
            matches = self.retrieval.query(text)

        if not matches:
            if any(w in text.lower() for w in ["hello", "hi", "hey"]):
                self.state = DialogueState.IDLE
                return "Hey! What workflow would you like to run today? 🌟", self.state
            return (
                f"I'm not sure how to '{text}' yet. Would you like to teach me this workflow?",
                self.state,
            )

        top_match = matches[0]

        # Ambiguity check: if 2+ matches and the second match has very close confidence
        if len(matches) > 1 and (matches[0].confidence - matches[1].confidence < 0.15):
            self.state = DialogueState.CLARIFYING
            candidates = [m.workflow_name for m in matches[:3]]
            return (
                f"Did you mean one of these: {', '.join(candidates)}?",
                self.state,
            )

        if top_match.confidence >= 0.85:
            self.state = DialogueState.EXECUTING
            return f"Starting '{top_match.workflow_name}' right away! 🚀", self.state
        elif top_match.confidence >= 0.5:
            self.state = DialogueState.CLARIFYING
            candidates = [m.workflow_name for m in matches[:3]]
            return (
                f"Did you mean one of these: {', '.join(candidates)}?",
                self.state,
            )
        else:
            return (
                f"I found '{top_match.workflow_name}', but I'm not totally sure. Would you like me to run it?",
                DialogueState.CONFIRMING,
            )
