"""Commentary Engine Subsystem.

Belongs to Milestone M4 (Conversational Companion Persona & Commentary Engine).
Provides real-time narrative commentary synthesis with configurable tone profiles and event throttling.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Optional

from src.executor.events import EventType, ExecutionEvent, ExecutionEventBus

logger = logging.getLogger(__name__)


class CommentaryEngine:
    """Real-time narrative commentary synthesis with debounce/throttle."""

    TEMPLATES: Dict[EventType, Dict[str, str]] = {
        EventType.TASK_STARTED: {
            "vibrant": "Alright! Let's get your '{task_name}' done hands-free! Hands off—I've got this! ✨",
            "concise": "Starting task: {task_name}.",
            "zen": "Beginning '{task_name}'. Sit back and relax.",
            "developer": "[TASK_START] id={task_id} steps={total_steps}",
        },
        EventType.APP_LAUNCH_INIT: {
            "vibrant": "Opening {target_app} now...",
            "concise": "Launching {target_app}.",
            "zen": "Opening {target_app} quietly.",
            "developer": "[LAUNCH] target={target_app}",
        },
        EventType.APP_LAUNCHED: {
            "vibrant": "{target_app} is open and ready to roll! 🚀",
            "concise": "{target_app} launched.",
            "zen": "{target_app} is now open.",
            "developer": "[APP_READY] target={target_app}",
        },
        EventType.WINDOW_WAITING: {
            "vibrant": "Waiting for {target_app} window to settle in...",
            "concise": "Waiting for window: {target_app}.",
            "zen": "Waiting patiently for the window.",
            "developer": "[WINDOW_WAIT] target={target_app}",
        },
        EventType.WINDOW_READY: {
            "vibrant": "Window found and locked in! 🎯",
            "concise": "Window ready.",
            "zen": "Window is ready.",
            "developer": "[WINDOW_READY] target={target_app}",
        },
        EventType.ACTION_STARTING: {
            "vibrant": "Working on: {message} 🚀",
            "concise": "Executing: {message}",
            "zen": "Processing step...",
            "developer": "[ACTION] step={step_index}/{total_steps} msg={message}",
        },
        EventType.ACTION_COMPLETED: {
            "vibrant": "Done: {message} ✅",
            "concise": "Completed: {message}",
            "zen": "Step finished.",
            "developer": "[ACTION_DONE] step={step_index}/{total_steps}",
        },
        EventType.TEXT_TYPING_PROGRESS: {
            "vibrant": "Typing away smoothly...",
            "concise": "Typing text.",
            "zen": "Writing gently.",
            "developer": "[TYPE] msg={message}",
        },
        EventType.RETRY_ATTEMPT: {
            "vibrant": "Hang on, giving that step another quick try! 🔄",
            "concise": "Retrying step.",
            "zen": "Taking another gentle attempt.",
            "developer": "[RETRY] attempt msg={message}",
        },
        EventType.STEP_FAILED: {
            "vibrant": "Oops! A step hit a snag: {message} ⚠️",
            "concise": "Step failed: {message}",
            "zen": "An unexpected pause occurred: {message}",
            "developer": "[STEP_FAIL] error={message}",
        },
        EventType.EMERGENCY_STOP: {
            "vibrant": "Whoa! Emergency stop triggered! Halting everything right now. 🛑",
            "concise": "Emergency stop triggered. Aborted.",
            "zen": "Halting smoothly upon request.",
            "developer": "[ABORT] emergency_stop triggered.",
        },
        EventType.TASK_COMPLETED: {
            "vibrant": "Ta-da! 🎉 Your task is completely finished. Have an awesome day!",
            "concise": "Task completed successfully.",
            "zen": "All finished. Everything is in order.",
            "developer": "[TASK_SUCCESS] status=0",
        },
        EventType.TASK_FAILED: {
            "vibrant": "Oh no! Something went wrong while running the task: {message} 😢",
            "concise": "Task failed: {message}",
            "zen": "The task could not be completed at this time.",
            "developer": "[TASK_FAILURE] error={message}",
        },
    }

    VALID_TONES = ("vibrant", "concise", "zen", "developer")

    def __init__(
        self,
        bus: Optional[ExecutionEventBus] = None,
        tone: str = "vibrant",
        throttle_ms: float = 800.0,
    ) -> None:
        self.bus = bus
        self.tone = tone if tone in self.VALID_TONES else "vibrant"
        self.throttle_s = throttle_ms / 1000.0
        self._last_commentary_time = 0.0
        self.commentary_log: List[str] = []
        self._listeners: List[Callable[[str], None]] = []

        if self.bus is not None:
            self.bus.subscribe(self.handle_event)

    def set_tone(self, tone: str) -> None:
        """Sets the persona tone profile."""
        if tone in self.VALID_TONES:
            self.tone = tone
        else:
            raise ValueError(f"Unknown tone '{tone}'. Valid tones: {self.VALID_TONES}")

    def add_commentary_listener(self, listener: Callable[[str], None]) -> None:
        """Adds a callback to receive newly generated commentary text."""
        self._listeners.append(listener)

    def handle_event(self, event: ExecutionEvent) -> Optional[str]:
        """Processes an execution event and synthesizes personality commentary.

        High priority events (TASK_STARTED, EMERGENCY_STOP, TASK_COMPLETED,
        TASK_FAILED, STEP_FAILED) bypass throttling.
        """
        now = time.time()
        high_priority = event.event_type in (
            EventType.TASK_STARTED,
            EventType.EMERGENCY_STOP,
            EventType.TASK_COMPLETED,
            EventType.TASK_FAILED,
            EventType.STEP_FAILED,
        )

        if not high_priority and (now - self._last_commentary_time < self.throttle_s):
            return None

        tone_pool = self.TEMPLATES.get(event.event_type, {})
        template = tone_pool.get(self.tone, "{message}")
        formatted = template.format(
            task_name=event.task_id or "Workflow",
            task_id=event.task_id or "",
            total_steps=event.total_steps,
            step_index=event.step_index,
            target_app=event.target_app or "application",
            message=event.message,
        )
        self._last_commentary_time = now
        self.commentary_log.append(formatted)

        for listener in self._listeners:
            try:
                listener(formatted)
            except Exception as e:
                logger.debug("Commentary listener error: %s", e)

        return formatted
