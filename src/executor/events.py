"""Typed Execution Event Bus Subsystem.

Belongs to FEAT-EXEC-01 (ExecutionEventBus).
Zero external dependencies: pure Python standard library.
Features:
- Complete EventType enum covering lifecycle milestones and micro execution events.
- Serializable ExecutionEvent dataclass with dictionary/JSON export and formatters.
- Thread-safe ExecutionEventBus supporting exact and wildcard '*' subscriptions.
- Synchronous and asynchronous queue-backed background dispatch.
- Robust error isolation (failing subscriber never disrupts other listeners or caller).
- Audit history recording with configurable capacity and filtering.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
import json
import queue
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union


class EventType(str, Enum):
    """Supported execution lifecycle event types."""
    TASK_STARTED = "task_started"
    APP_LAUNCH_INIT = "app_launch_init"
    APP_LAUNCHED = "app_launched"
    WINDOW_WAITING = "window_waiting"
    WINDOW_READY = "window_ready"
    ACTION_STARTING = "action_starting"
    ACTION_COMPLETED = "action_completed"
    TEXT_TYPING_PROGRESS = "text_typing_progress"
    EMERGENCY_STOP = "emergency_stop"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    STEP_FAILED = "step_failed"
    RETRY_ATTEMPT = "retry_attempt"

    # Compatibility / Stalled Step Events
    TASK_STALLED = "task_stalled"
    RECOVERY_ATTEMPT = "recovery_attempt"


@dataclass
class ExecutionEvent:
    """Represents an execution lifecycle event delivered across the system.

    Attributes:
        event_type: Enum member or string representing event nature.
        task_id: Unique identifier of the task/workflow being executed.
        timestamp: Unix timestamp when event occurred (defaults to time.time()).
        message: Human-readable commentary or status text.
        target_app: Optional target application bundle or name.
        step_index: Current 1-based step index, or None if task-level event.
        total_steps: Total step count in workflow, or None if unknown.
        payload: Arbitrary contextual metadata dictionary.
    """
    event_type: EventType
    task_id: str = ""
    timestamp: float = field(default_factory=time.time)
    message: str = ""
    target_app: Optional[str] = None
    step_index: Optional[int] = None
    total_steps: Optional[int] = None
    payload: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.event_type, str):
            try:
                self.event_type = EventType(self.event_type)
            except ValueError:
                pass
        if self.payload is None:
            self.payload = {}

    def to_dict(self) -> Dict[str, Any]:
        """Serializes event to a pure dictionary."""
        return {
            "event_type": self.event_type.value if isinstance(self.event_type, EventType) else str(self.event_type),
            "task_id": self.task_id,
            "timestamp": self.timestamp,
            "message": self.message,
            "target_app": self.target_app,
            "step_index": self.step_index,
            "total_steps": self.total_steps,
            "payload": dict(self.payload) if self.payload else {},
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ExecutionEvent:
        """Constructs an ExecutionEvent from dictionary."""
        d = dict(data)
        if "event_type" in d and isinstance(d["event_type"], str):
            try:
                d["event_type"] = EventType(d["event_type"])
            except ValueError:
                pass
        return cls(**d)

    def to_json(self, indent: Optional[int] = None) -> str:
        """Serializes event to a JSON string."""
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @classmethod
    def from_json(cls, json_str: str) -> ExecutionEvent:
        """Deserializes event from a JSON string."""
        return cls.from_dict(json.loads(json_str))

    def __str__(self) -> str:
        evt_name = self.event_type.value.upper() if isinstance(self.event_type, EventType) else str(self.event_type).upper()
        step_str = f" [{self.step_index}/{self.total_steps}]" if self.step_index is not None and self.total_steps else ""
        app_str = f" ({self.target_app})" if self.target_app else ""
        task_str = f"<{self.task_id}> " if self.task_id else ""
        return f"{task_str}[{evt_name}]{step_str}{app_str}: {self.message}".strip()


class ExecutionEventBus:
    """Thread-safe decoupled pub/sub event bus with error isolation.

    Supports:
    - Exact event type subscription (e.g. EventType.WINDOW_READY)
    - Wildcard subscription ('*' or calling subscribe with single callback argument)
    - Synchronous publish (publish) and background asynchronous publish (publish_async)
    - Error isolation: listener exceptions are caught and never crash the bus or publisher
    - Replay / audit history buffer with bounded capacity
    """

    def __init__(
        self,
        record_history: bool = True,
        max_history: int = 1000,
        on_error: Optional[Callable[[Callable, ExecutionEvent, Exception], None]] = None,
    ) -> None:
        self._lock = threading.RLock()
        self._exact_subscribers: Dict[str, List[Callable[[ExecutionEvent], None]]] = {}
        self._wildcard_subscribers: List[Callable[[ExecutionEvent], None]] = []
        self._history: List[ExecutionEvent] = []
        self._errors: List[Tuple[Callable, ExecutionEvent, Exception]] = []

        self.record_history: bool = record_history
        self.max_history: int = max_history
        self._on_error: Optional[Callable[[Callable, ExecutionEvent, Exception], None]] = on_error

        # Asynchronous worker infrastructure (lazy initialized on first publish_async)
        self._async_queue: Optional[queue.Queue[Optional[ExecutionEvent]]] = None
        self._worker_thread: Optional[threading.Thread] = None
        self._shutdown_event = threading.Event()

    def __enter__(self) -> ExecutionEventBus:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    # =========================================================================
    # Subscription Management
    # =========================================================================

    def subscribe(
        self,
        event_type_or_callback: Union[EventType, str, Callable[[ExecutionEvent], None]],
        callback: Optional[Callable[[ExecutionEvent], None]] = None,
    ) -> Callable[[], None]:
        """Subscribes a listener callback to events.

        Supports two invocation forms:
        1. bus.subscribe(callback): Wildcard subscription matching all events.
        2. bus.subscribe(event_type, callback): Exact subscription (or '*' for all).

        Args:
            event_type_or_callback: EventType, string, or callable listener.
            callback: Callable listener if event type is passed as first argument.

        Returns:
            An unsubscription callable that removes this listener when called.
        """
        with self._lock:
            if callback is None and callable(event_type_or_callback):
                # Wildcard subscription with single argument
                fn = event_type_or_callback
                if fn not in self._wildcard_subscribers:
                    self._wildcard_subscribers.append(fn)

                def _unsub() -> None:
                    self.unsubscribe(fn)

                return _unsub

            if callback is not None:
                fn = callback
                target = event_type_or_callback
                if target == "*":
                    if fn not in self._wildcard_subscribers:
                        self._wildcard_subscribers.append(fn)

                    def _unsub_all() -> None:
                        self.unsubscribe(fn, "*")

                    return _unsub_all
                else:
                    key = target.value if isinstance(target, EventType) else str(target).lower()
                    if key not in self._exact_subscribers:
                        self._exact_subscribers[key] = []
                    if fn not in self._exact_subscribers[key]:
                        self._exact_subscribers[key].append(fn)

                    def _unsub_exact() -> None:
                        self.unsubscribe(fn, target)

                    return _unsub_exact

            raise ValueError("Invalid subscription signature: must provide a callable listener.")

    def subscribe_all(self, callback: Callable[[ExecutionEvent], None]) -> Callable[[], None]:
        """Convenience method subscribing to all events (wildcard)."""
        return self.subscribe(callback)

    def subscribe_exact(
        self, event_type: Union[EventType, str], callback: Callable[[ExecutionEvent], None]
    ) -> Callable[[], None]:
        """Convenience method subscribing to a specific EventType."""
        return self.subscribe(event_type, callback)

    def unsubscribe(
        self,
        callback: Callable[[ExecutionEvent], None],
        event_type: Optional[Union[EventType, str]] = None,
    ) -> bool:
        """Removes a previously subscribed listener.

        Args:
            callback: The subscriber callable to remove.
            event_type: Specific event type to unsubscribe from, or None to remove
                        from wildcard and all exact subscriber lists.

        Returns:
            True if listener was found and removed from at least one list, False otherwise.
        """
        removed = False
        with self._lock:
            if event_type is None:
                # Remove from wildcard
                if callback in self._wildcard_subscribers:
                    self._wildcard_subscribers.remove(callback)
                    removed = True
                # Remove from all exact lists
                for subs in self._exact_subscribers.values():
                    while callback in subs:
                        subs.remove(callback)
                        removed = True
            elif event_type == "*":
                if callback in self._wildcard_subscribers:
                    self._wildcard_subscribers.remove(callback)
                    removed = True
            else:
                key = event_type.value if isinstance(event_type, EventType) else str(event_type).lower()
                if key in self._exact_subscribers:
                    subs = self._exact_subscribers[key]
                    while callback in subs:
                        subs.remove(callback)
                        removed = True
        return removed

    # =========================================================================
    # Event Publishing & Dispatch
    # =========================================================================

    def publish(self, event: ExecutionEvent) -> None:
        """Synchronously dispatches an event to matching subscribers.

        Ensures full error isolation: exceptions raised by listeners are caught,
        recorded, and never crash the publisher or prevent other listeners from running.

        Args:
            event: ExecutionEvent instance to broadcast.
        """
        key = event.event_type.value if isinstance(event.event_type, EventType) else str(event.event_type).lower()

        # Snapshot listeners under lock to prevent concurrent modification deadlocks
        with self._lock:
            exact_listeners = list(self._exact_subscribers.get(key, []))
            wildcard_listeners = list(self._wildcard_subscribers)

            if self.record_history:
                self._history.append(event)
                if len(self._history) > self.max_history:
                    self._history.pop(0)

        # Dispatch outside lock so listener execution cannot block registration/unregistration
        all_listeners = exact_listeners + wildcard_listeners
        for listener in all_listeners:
            try:
                listener(event)
            except Exception as ex:
                with self._lock:
                    self._errors.append((listener, event, ex))
                if self._on_error:
                    try:
                        self._on_error(listener, event, ex)
                    except Exception:
                        pass

    def publish_async(self, event: ExecutionEvent) -> None:
        """Asynchronously dispatches an event via a dedicated background queue.

        Decouples the publisher thread (e.g. high-speed action loops) from
        slow listeners (e.g. text-to-speech, heavy logging).

        Args:
            event: ExecutionEvent to enqueue for background dispatch.
        """
        self._ensure_worker()
        if self._async_queue is not None:
            self._async_queue.put(event)

    def _ensure_worker(self) -> None:
        """Lazily starts background worker thread on first asynchronous publish."""
        with self._lock:
            if self._worker_thread is None or not self._worker_thread.is_alive():
                self._async_queue = queue.Queue()
                self._shutdown_event.clear()
                self._worker_thread = threading.Thread(
                    target=self._worker_loop,
                    name="ExecutionEventBusWorker",
                    daemon=True,
                )
                self._worker_thread.start()

    def _worker_loop(self) -> None:
        """Background thread worker loop consuming events from async queue."""
        assert self._async_queue is not None
        while not self._shutdown_event.is_set():
            try:
                event = self._async_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            if event is None:  # Shutdown sentinel
                self._async_queue.task_done()
                break

            try:
                self.publish(event)
            finally:
                self._async_queue.task_done()

    def flush(self, timeout: float = 5.0) -> bool:
        """Blocks until all queued asynchronous events have been processed.

        Args:
            timeout: Maximum seconds to wait.

        Returns:
            True if queue was drained within timeout, False otherwise.
        """
        if self._async_queue is None or self._worker_thread is None:
            return True
        deadline = time.monotonic() + timeout
        while not self._async_queue.empty():
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)
        return True

    def close(self, timeout: float = 2.0) -> None:
        """Gracefully shuts down background worker thread and cleans up resources."""
        with self._lock:
            if self._worker_thread is not None and self._worker_thread.is_alive():
                self._shutdown_event.set()
                if self._async_queue is not None:
                    self._async_queue.put(None)
                self._worker_thread.join(timeout=timeout)
                self._worker_thread = None
                self._async_queue = None

    # =========================================================================
    # Introspection & Audit Inspection
    # =========================================================================

    def get_history(
        self, event_type: Optional[Union[EventType, str]] = None
    ) -> List[ExecutionEvent]:
        """Returns recorded execution events, optionally filtered by event type."""
        with self._lock:
            if event_type is None:
                return list(self._history)
            key = event_type.value if isinstance(event_type, EventType) else str(event_type).lower()
            return [
                e for e in self._history
                if (e.event_type.value if isinstance(e.event_type, EventType) else str(e.event_type).lower()) == key
            ]

    def clear_history(self) -> None:
        """Clears all recorded event history."""
        with self._lock:
            self._history.clear()

    def listener_count(self, event_type: Optional[Union[EventType, str]] = None) -> int:
        """Returns total count of registered listeners.

        Args:
            event_type: If provided, count of exact subscribers + wildcard subscribers.
                        If None, total unique registrations across all lists.
        """
        with self._lock:
            if event_type is None:
                total_exact = sum(len(subs) for subs in self._exact_subscribers.values())
                return total_exact + len(self._wildcard_subscribers)
            if event_type == "*":
                return len(self._wildcard_subscribers)
            key = event_type.value if isinstance(event_type, EventType) else str(event_type).lower()
            return len(self._exact_subscribers.get(key, [])) + len(self._wildcard_subscribers)

    def get_errors(self) -> List[Tuple[Callable, ExecutionEvent, Exception]]:
        """Returns list of captured subscriber errors."""
        with self._lock:
            return list(self._errors)

    def clear_errors(self) -> None:
        """Clears captured error history."""
        with self._lock:
            self._errors.clear()

    def clear(self) -> None:
        """Resets event bus: removes all listeners, clears history and errors."""
        with self._lock:
            self._exact_subscribers.clear()
            self._wildcard_subscribers.clear()
            self._history.clear()
            self._errors.clear()
