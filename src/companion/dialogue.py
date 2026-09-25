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
        self.pending_workflow: Optional[Any] = None
        self.last_matched_workflow: Optional[Any] = None
        self.last_recorded_workflow_id: Optional[str] = None

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

        # Anaphoric / Contextual Reference Handling ("perform that action", "do that action", "run that", "do what I just did")
        anaphoric_pattern = r"^(perform|do|run|execute|replay)\s+(that|the|this|my|last|recorded)?\s*(action|task|workflow|demonstration|what i just did)?$"
        normalized_cmd = re.sub(r"[^\w\s]", "", cleaned_intent.lower()).strip()
        is_anaphoric = bool(re.match(anaphoric_pattern, normalized_cmd)) or normalized_cmd in (
            "do that", "perform that", "run that", "execute that", "do it", "run it",
            "perform that action", "do that action", "run that action", "execute that action",
            "perform the action", "do the action", "run the action", "execute the action",
            "run recorded task", "do recorded task", "perform recorded task",
            "run the recorded action", "perform the recorded action", "do what i just did",
            "run last task", "perform last task", "do last task",
        )

        if is_anaphoric:
            target_wf = None
            if self.last_recorded_workflow_id:
                target_wf = self.memory.get_workflow(self.last_recorded_workflow_id)
            if not target_wf:
                # Fallback to the latest saved workflow in task memory
                all_wfs = self.memory.list_workflows()
                if all_wfs:
                    latest_id = all_wfs[-1]["id"]
                    target_wf = self.memory.get_workflow(latest_id)

            if target_wf:
                self.state = DialogueState.EXECUTING
                self.last_matched_workflow = MatchResult(
                    workflow_id=target_wf.id,
                    workflow_name=target_wf.name,
                    confidence=1.0,
                    tier="tier1_contextual_anaphora",
                    matched_trigger=normalized_cmd,
                    spec=target_wf,
                )
                return f"Got it! Running '{target_wf.name}' now! 🚀", self.state
            else:
                return (
                    "I don't have any recorded actions in memory yet. Click 'Record Task' to teach me one!",
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

        # Clarifying state follow-up handling
        if self.state == DialogueState.CLARIFYING:
            text_lower = text.lower()
            candidates = getattr(self, "clarifying_candidates", []) or []
            if any(neg in text_lower for neg in ("no", "neither", "none", "cancel", "stop", "nevermind")):
                self.state = DialogueState.IDLE
                self.clarifying_candidates = []
                self.pending_workflow = None
                return "Cancelled! What would you like to do instead?", self.state

            chosen = None
            if ("1" in text_lower or "first" in text_lower) and len(candidates) >= 1:
                chosen = candidates[0]
            elif ("2" in text_lower or "second" in text_lower) and len(candidates) >= 2:
                chosen = candidates[1]
            elif ("3" in text_lower or "third" in text_lower) and len(candidates) >= 3:
                chosen = candidates[2]
            elif any(affirm in text_lower for affirm in ("yes", "y", "sure", "proceed", "do it", "ok", "yep", "go ahead")) and candidates:
                chosen = candidates[0]
            else:
                for c in candidates:
                    c_name = getattr(c, "workflow_name", getattr(c, "name", "")).lower()
                    if c_name and (c_name in text_lower or text_lower in c_name):
                        chosen = c
                        break

            if chosen:
                self.state = DialogueState.EXECUTING
                self.last_matched_workflow = chosen
                self.clarifying_candidates = []
                self.pending_workflow = None
                chosen_name = getattr(chosen, "workflow_name", getattr(chosen, "name", "task"))
                return f"Got it! Starting '{chosen_name}' now! 🚀", self.state

        # Confirmation handling
        if self.state == DialogueState.CONFIRMING:
            text_lower = text.lower()
            if any(affirm in text_lower for affirm in ("yes", "y", "sure", "proceed", "do it", "ok", "yep", "go ahead")):
                self.state = DialogueState.EXECUTING
                wf = self.pending_workflow
                self.last_matched_workflow = wf
                self.pending_workflow = None
                wf_name = getattr(wf, "workflow_name", getattr(wf, "name", "task"))
                return (
                    f"Got it! Executing '{wf_name}' now.",
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
            self.clarifying_candidates = matches[:3]
            self.pending_workflow = matches[0]
            candidates = [m.workflow_name for m in matches[:3]]
            return (
                f"Did you mean one of these: {', '.join(candidates)}?",
                self.state,
            )

        if top_match.confidence >= 0.70:
            self.state = DialogueState.EXECUTING
            self.last_matched_workflow = top_match
            return f"Starting '{top_match.workflow_name}' right away! 🚀", self.state
        else:
            self.state = DialogueState.CONFIRMING
            self.pending_workflow = top_match
            return (
                f"I found '{top_match.workflow_name}', but I'm not totally sure. Would you like me to run it?",
                self.state,
            )
