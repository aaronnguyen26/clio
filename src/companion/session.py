"""Unified Companion Session Subsystem.

Belongs to Milestone M4 (Conversational Companion Persona).
Orchestrates memory, actuators, event bus, autonomous executor, virtual cursor,
dialogue state machine, and commentary into a cohesive interactive session.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from src.actuators.base import BaseActuator
from src.actuators.types import ActuatorMode
from src.actuators.factory import get_actuator
from src.actuators.virtual_cursor import VirtualCursor
from src.companion.commentary import CommentaryEngine
from src.companion.dialogue import CompanionDialogueEngine, DialogueState
from src.executor.events import EventType, ExecutionEvent, ExecutionEventBus
from src.executor.executor import AutonomousWorkflowExecutor, ExecutionResult
from src.memory.engine import TaskMemoryEngine
from src.memory.models import WorkflowSpec

logger = logging.getLogger(__name__)


class CompanionSession:
    """Unified companion session facade providing interactive dialogue and hands-free execution."""

    def __init__(
        self,
        actuator: Optional[BaseActuator] = None,
        memory: Optional[TaskMemoryEngine] = None,
        bus: Optional[ExecutionEventBus] = None,
        tone: str = "vibrant",
        throttle_ms: float = 800.0,
        zero_delay: bool = False,
    ) -> None:
        self.bus = bus or ExecutionEventBus()
        self.memory = memory or TaskMemoryEngine()
        self.actuator = actuator or get_actuator(mode=ActuatorMode.MOCK)

        self.virtual_cursor = VirtualCursor(
            initial_x=0.0,
            initial_y=0.0,
            mock=getattr(self.actuator, "mode", None) != "macos",
        )

        self.executor = AutonomousWorkflowExecutor(
            actuator=self.actuator,
            bus=self.bus,
            memory_engine=self.memory,
            virtual_cursor=self.virtual_cursor,
            zero_delay=zero_delay,
        )

        self.dialogue = CompanionDialogueEngine(
            memory=self.memory,
            tone=tone,
        )

        self.commentary = CommentaryEngine(
            bus=self.bus,
            tone=tone,
            throttle_ms=throttle_ms,
        )

    @property
    def state(self) -> DialogueState:
        """Current dialogue state."""
        return self.dialogue.state

    def set_tone(self, tone: str) -> None:
        """Updates tone profile across dialogue and commentary subsystems."""
        self.dialogue.tone = tone
        self.commentary.set_tone(tone)

    def interact(self, message: str) -> str:
        """Sends a natural language user utterance to the companion and returns the conversational reply."""
        reply, state = self.dialogue.handle_user_message(message)

        if state == DialogueState.EXECUTING:
            # If dialogue matched an immediate workflow, execute it
            cleaned = message.strip()
            matches = self.dialogue.retrieval.query(cleaned)
            if matches and matches[0].confidence >= 0.5:
                wf_obj = self.memory.get_workflow(matches[0].workflow_id)
                if wf_obj:
                    spec = wf_obj if isinstance(wf_obj, WorkflowSpec) else WorkflowSpec.from_dict(wf_obj)
                    result: ExecutionResult = self.executor.execute_workflow(spec)
                    if result.success:
                        self.dialogue.state = DialogueState.COMPLETED
                        return f"{reply}\n\nTask '{spec.name}' completed successfully in {result.elapsed_seconds:.2f}s! 🎉"
                    else:
                        self.dialogue.state = DialogueState.IDLE
                        return f"{reply}\n\nTask '{spec.name}' failed: {result.error_message}."

        return reply

    def execute_workflow_by_id(
        self, workflow_id: str, runtime_params: Optional[Dict[str, Any]] = None
    ) -> ExecutionResult:
        """Direct programmatic execution of a recorded workflow by ID."""
        wf_obj = self.memory.get_workflow(workflow_id)
        if not wf_obj:
            raise KeyError(f"Workflow '{workflow_id}' not found in task memory.")
        spec = wf_obj if isinstance(wf_obj, WorkflowSpec) else WorkflowSpec.from_dict(wf_obj)
        return self.executor.execute_workflow(spec, runtime_params=runtime_params)

    def get_commentary_history(self) -> List[str]:
        """Returns the chronological log of all synthesized narrative commentary."""
        return list(self.commentary.commentary_log)

    def close(self) -> None:
        """Cleans up buses, workers, and background threads."""
        if hasattr(self.bus, "close"):
            self.bus.close()
        if hasattr(self.memory, "close"):
            self.memory.close()
