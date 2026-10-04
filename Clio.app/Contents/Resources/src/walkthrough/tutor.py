"""Pedagogical Walkthrough Tutor & State Machine Controller.

Path: src/walkthrough/tutor.py
Orchestrates on-screen desktop walkthroughs, managing step transitions,
virtual cursor choreography, element highlighting, and user interaction feedback.
"""

from __future__ import annotations

from enum import Enum
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.actuators.virtual_cursor import VirtualCursor, VirtualCursorState
from src.executor.events import EventType, ExecutionBus, ExecutionEvent
from src.walkthrough.grounder import ScreenGrounder
from src.walkthrough.models import (
    TeachingAction,
    WalkthroughMode,
    WalkthroughPlan,
    WalkthroughStep,
)

logger = logging.getLogger(__name__)


class WalkthroughStatus(str, Enum):
    """Lifecycle states of the walkthrough tutor."""
    IDLE = "IDLE"
    READY = "READY"
    NAVIGATING = "NAVIGATING"
    HIGHLIGHTING = "HIGHLIGHTING"
    DEMONSTRATING = "DEMONSTRATING"
    WAITING_FOR_USER = "WAITING_FOR_USER"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    ERROR = "ERROR"


class WalkthroughTutor:
    """Manages the step-by-step on-screen walkthrough execution loop."""

    def __init__(
        self,
        virtual_cursor: Optional[VirtualCursor] = None,
        grounder: Optional[ScreenGrounder] = None,
        bus: Optional[ExecutionBus] = None,
        mock: bool = False,
        auto_advance: bool = True,
        auto_advance_delay: float = 1.8,
        assist_timeout: float = 4.0,
    ) -> None:
        self.mock = mock
        self.virtual_cursor = virtual_cursor or VirtualCursor(initial_x=640.0, initial_y=400.0, mock=mock)
        self.grounder = grounder or ScreenGrounder(mock=mock)
        self.bus = bus or ExecutionBus()
        self.auto_advance = auto_advance
        self.auto_advance_delay = auto_advance_delay
        self.assist_timeout = assist_timeout

        self._plan: Optional[WalkthroughPlan] = None
        self._current_step_index: int = 0  # 1-indexed
        self._status: WalkthroughStatus = WalkthroughStatus.IDLE
        self._lock = threading.RLock()
        self._listeners: List[Callable[[Dict[str, Any]], None]] = []
        self._active_target_coords: Tuple[float, float] = (640.0, 400.0)
        self._active_spotlight_bounds: Optional[Tuple[float, float, float, float]] = None
        self._pause_event = threading.Event()
        self._pause_event.set()  # Unpaused by default
        self._auto_advance_timer: Optional[threading.Timer] = None
        self._assist_timer: Optional[threading.Timer] = None

    def _cancel_timers(self) -> None:
        """Cancels all active background timers (auto-advance and assist watchdog)."""
        with self._lock:
            if self._auto_advance_timer is not None:
                self._auto_advance_timer.cancel()
                self._auto_advance_timer = None
            if self._assist_timer is not None:
                self._assist_timer.cancel()
                self._assist_timer = None

    def _cancel_auto_advance(self) -> None:
        """Cancels any pending auto-advance timer."""
        self._cancel_timers()

    def _schedule_assist_watchdog(self, delay: Optional[float] = None) -> None:
        """Schedules a safety watchdog assist hint if a step is stalled or element is ungrounded."""
        with self._lock:
            if self._assist_timer is not None:
                self._assist_timer.cancel()
                self._assist_timer = None
            if self._status in (
                WalkthroughStatus.PAUSED,
                WalkthroughStatus.COMPLETED,
                WalkthroughStatus.CANCELLED,
                WalkthroughStatus.ERROR,
            ):
                return
            wait_time = delay if delay is not None else self.assist_timeout
            timer = threading.Timer(wait_time, self._on_assist_timeout)
            timer.daemon = True
            self._assist_timer = timer
            timer.start()

    def _on_assist_timeout(self) -> None:
        """Dispatches an assist telemetry hint when a step needs guidance or target is stalled."""
        with self._lock:
            if self._status in (
                WalkthroughStatus.WAITING_FOR_USER,
                WalkthroughStatus.HIGHLIGHTING,
                WalkthroughStatus.DEMONSTRATING,
            ):
                self._broadcast_telemetry(
                    "ASSIST",
                    message="Need help? Click the highlighted beacon or press 'Next' to continue.",
                )

    def _schedule_auto_advance(self, delay: Optional[float] = None) -> None:
        """Schedules auto-advancement to the next step after delay."""
        with self._lock:
            self._cancel_auto_advance()
            if not self.auto_advance:
                return
            if not self._plan or self._plan.mode == WalkthroughMode.INTERACTIVE_TUTOR:
                return
            if self._status in (
                WalkthroughStatus.PAUSED,
                WalkthroughStatus.COMPLETED,
                WalkthroughStatus.CANCELLED,
                WalkthroughStatus.ERROR,
            ):
                return

            wait_time = delay if delay is not None else self.auto_advance_delay
            timer = threading.Timer(wait_time, self._on_auto_advance)
            timer.daemon = True
            self._auto_advance_timer = timer
            timer.start()

    def _on_auto_advance(self) -> None:
        """Timer callback to advance to the next step automatically."""
        with self._lock:
            if not self.auto_advance:
                return
            if self._status in (WalkthroughStatus.DEMONSTRATING, WalkthroughStatus.HIGHLIGHTING):
                self.step_forward()

    @property
    def status(self) -> WalkthroughStatus:
        with self._lock:
            return self._status

    @property
    def current_step_index(self) -> int:
        with self._lock:
            return self._current_step_index

    @property
    def plan(self) -> Optional[WalkthroughPlan]:
        with self._lock:
            return self._plan

    def add_listener(self, listener: Callable[[Dict[str, Any]], None]) -> None:
        """Registers an observer for walkthrough state transitions."""
        with self._lock:
            if listener not in self._listeners:
                self._listeners.append(listener)

    def remove_listener(self, listener: Callable[[Dict[str, Any]], None]) -> None:
        """Removes a registered observer."""
        with self._lock:
            if listener in self._listeners:
                self._listeners.remove(listener)

    def load_plan(self, plan: WalkthroughPlan) -> None:
        """Loads a WalkthroughPlan into the tutor and sets status to READY."""
        with self._lock:
            self._cancel_auto_advance()
            self._plan = plan
            self._current_step_index = 0
            self._status = WalkthroughStatus.READY
            self._broadcast_telemetry("LOADED", message=f"Loaded walkthrough: '{plan.goal}'")

    def start(self) -> bool:
        """Begins execution from step 1."""
        with self._lock:
            self._cancel_auto_advance()
            if not self._plan or not self._plan.steps:
                self._status = WalkthroughStatus.ERROR
                return False
            self._current_step_index = 1
            self._execute_active_step()
            return True

    def step_forward(self) -> bool:
        """Navigates to the next instructional step."""
        with self._lock:
            self._cancel_auto_advance()
            if not self._plan:
                return False
            if self._current_step_index >= len(self._plan.steps):
                self._status = WalkthroughStatus.COMPLETED
                self._active_target_coords = (-200.0, -200.0)
                self._active_spotlight_bounds = None
                self._broadcast_telemetry("COMPLETED", message="Walkthrough completed! 🎉")
                # Park virtual cursor safely off-screen so the cursor disappears
                try:
                    self.virtual_cursor.hover(-200.0, -200.0)
                except Exception:
                    pass
                return False
            self._current_step_index += 1
            self._execute_active_step()
            return True

    def step_backward(self) -> bool:
        """Navigates to the previous instructional step."""
        with self._lock:
            self._cancel_auto_advance()
            if not self._plan:
                return False
            if self._current_step_index <= 1:
                return False
            self._current_step_index -= 1
            self._execute_active_step()
            return True

    def retry_step(self) -> None:
        """Re-demonstrates the current active step."""
        with self._lock:
            self._cancel_auto_advance()
            if self._plan and self._current_step_index >= 1:
                self._execute_active_step()

    def pause(self) -> None:
        """Pauses the walkthrough tutor."""
        with self._lock:
            self._cancel_auto_advance()
            if self._status != WalkthroughStatus.PAUSED:
                self._status = WalkthroughStatus.PAUSED
                self._pause_event.clear()
                self._broadcast_telemetry("PAUSED", message="Walkthrough paused.")

    def resume(self) -> None:
        """Resumes the walkthrough tutor from paused state."""
        with self._lock:
            if self._status == WalkthroughStatus.PAUSED:
                self._pause_event.set()
                self._execute_active_step()

    def stop(self) -> None:
        """Cancels and resets the walkthrough session."""
        with self._lock:
            self._cancel_auto_advance()
            self._status = WalkthroughStatus.CANCELLED
            self._pause_event.set()
            self._active_target_coords = (-200.0, -200.0)
            self._active_spotlight_bounds = None
            self._broadcast_telemetry("CANCELLED", message="Walkthrough stopped.")
            # Park virtual cursor safely off-screen
            try:
                self.virtual_cursor.hover(-200.0, -200.0)
            except Exception:
                pass

    def simulate_user_action(self) -> bool:
        """Simulates user completing the current step in interactive mode."""
        with self._lock:
            if self._status == WalkthroughStatus.WAITING_FOR_USER:
                return self.step_forward()
            return False

    def _execute_active_step(self) -> None:
        """Executes the visual and pedagogical choreography for the current step."""
        if not self._plan or self._current_step_index < 1 or self._current_step_index > len(self._plan.steps):
            return

        step = self._plan.steps[self._current_step_index - 1]

        # 1. Resolve screen coordinates and bounding spotlight
        target_x, target_y, bounds = self.grounder.resolve_step_coordinates(step)
        self._active_target_coords = (target_x, target_y)
        self._active_spotlight_bounds = bounds

        # 2. State: NAVIGATING (Glides Virtual Cursor to target)
        self._status = WalkthroughStatus.NAVIGATING
        self._broadcast_telemetry("NAVIGATING", message=f"Moving to step {step.step_index}: {step.title}")

        # Humanized ease-in-out movement duration (0.2s in mock, 0.7s in live)
        move_duration = 0.05 if self.mock else 0.7
        self.virtual_cursor.move_to(target_x, target_y, duration=move_duration, smooth=True)

        # 3. State: HIGHLIGHTING (Wiggle and pulse)
        self._status = WalkthroughStatus.HIGHLIGHTING
        self._broadcast_telemetry("HIGHLIGHTING", message=step.instruction)

        if not self.mock:
            self.virtual_cursor.wiggle_at(target_x, target_y, amplitude=6.0, oscillations=2)
            self.virtual_cursor.pulse_at(target_x, target_y, duration=0.3)

        # 4. State: Action Dispatch
        if self._plan.mode == WalkthroughMode.INTERACTIVE_TUTOR and step.action_type in (
            TeachingAction.WAIT_FOR_USER_CLICK,
            TeachingAction.DEMONSTRATE_CLICK,
        ):
            self._status = WalkthroughStatus.WAITING_FOR_USER
            self._broadcast_telemetry("WAITING_FOR_USER", message=f"Your turn: {step.instruction}")
            self._schedule_assist_watchdog()
        else:
            self._status = WalkthroughStatus.DEMONSTRATING
            self._broadcast_telemetry("DEMONSTRATING", message=f"Demonstrating: {step.instruction}")

            if step.action_type == TeachingAction.DEMONSTRATE_CLICK:
                self.virtual_cursor.click(target_x, target_y)
            elif step.action_type == TeachingAction.PULSE_BEACON:
                self.virtual_cursor.pulse_at(target_x, target_y, duration=0.4)
            elif step.action_type == TeachingAction.DEMONSTRATE_TYPE and step.text_to_type:
                self.virtual_cursor.type_text(step.text_to_type)
            elif step.action_type == TeachingAction.DEMONSTRATE_HOTKEY and step.hotkey_combo:
                self.virtual_cursor.press_hotkey(*step.hotkey_combo)

            # Auto-advance to next step in guided demo mode
            if self.auto_advance and self._plan.mode != WalkthroughMode.INTERACTIVE_TUTOR:
                self._schedule_auto_advance()

    def get_telemetry(self) -> Dict[str, Any]:
        """Produces serialized telemetry snapshot for API responses and SSE broadcasting."""
        with self._lock:
            cur_step = None
            total_steps = 0
            goal = ""
            mode = "guided_demo"
            wt_id = ""

            if self._plan:
                wt_id = self._plan.walkthrough_id
                goal = self._plan.goal
                mode = self._plan.mode.value
                total_steps = len(self._plan.steps)
                if 1 <= self._current_step_index <= total_steps:
                    cur_step = self._plan.steps[self._current_step_index - 1].to_dict()

            spotlight_dict = None
            if self._active_spotlight_bounds and len(self._active_spotlight_bounds) == 4:
                spotlight_dict = {
                    "x": self._active_spotlight_bounds[0],
                    "y": self._active_spotlight_bounds[1],
                    "width": self._active_spotlight_bounds[2],
                    "height": self._active_spotlight_bounds[3],
                }

            return {
                "type": "walkthrough",
                "walkthrough_id": wt_id,
                "goal": goal,
                "mode": mode,
                "status": self._status.value,
                "current_step_index": self._current_step_index,
                "total_steps": total_steps,
                "step": cur_step,
                "steps": [s.to_dict() for s in self._plan.steps] if self._plan else [],
                "auto_advance": self.auto_advance,
                "cursor_x": self._active_target_coords[0],
                "cursor_y": self._active_target_coords[1],
                "spotlight": spotlight_dict,
                "timestamp": time.time(),
            }

    def _broadcast_telemetry(self, event_tag: str, message: str = "") -> None:
        """Broadcasts telemetry to registered subscribers and ExecutionBus."""
        telemetry = self.get_telemetry()
        telemetry["tag"] = event_tag
        telemetry["message"] = message

        for listener in list(self._listeners):
            try:
                listener(telemetry)
            except Exception as e:
                logger.debug("Tutor listener error: %s", e)

        # Publish to general Clio execution bus
        try:
            self.bus.publish(
                ExecutionEvent(
                    event_type=EventType.STATUS_UPDATE,
                    task_id=self._plan.walkthrough_id if self._plan else "wt",
                    message=f"[Walkthrough] {message or event_tag}",
                    step_index=self._current_step_index,
                    total_steps=len(self._plan.steps) if self._plan else 0,
                    payload=telemetry,
                )
            )
        except Exception:
            pass
