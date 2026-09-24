"""Unit tests for FEAT-EXEC-01 (src/executor/events.py).

Comprehensive unit and boundary tests covering:
- EventType enum definitions and completeness
- ExecutionEvent serialization, deserialization, formatting, and defaults
- ExecutionEventBus exact, wildcard, and unsubscription token workflows
- Error isolation across faulty subscribers
- Asynchronous dispatch, queue worker, and flush synchronization
- Thread-safe concurrent publishing under multi-threaded contention
"""

from __future__ import annotations

import json
import threading
import time
from typing import List
import pytest

from src.executor.events import EventType, ExecutionEvent, ExecutionEventBus


class TestExecutionEvents:
    """Tests for EventType and ExecutionEvent dataclass."""

    def test_event_type_enum_members(self) -> None:
        """Verifies all mandatory lifecycle and micro event types exist."""
        required_types = [
            "task_started",
            "app_launch_init",
            "app_launched",
            "window_waiting",
            "window_ready",
            "action_starting",
            "action_completed",
            "text_typing_progress",
            "emergency_stop",
            "task_completed",
            "task_failed",
            "step_failed",
            "retry_attempt",
        ]
        enum_values = [e.value for e in EventType]
        for req in required_types:
            assert req in enum_values, f"Missing required EventType: {req}"

    def test_execution_event_initialization_defaults(self) -> None:
        """Verifies default values and field assignment."""
        ev = ExecutionEvent(
            event_type=EventType.TASK_STARTED,
            task_id="task_123",
            message="Task initialized",
        )
        assert ev.event_type == EventType.TASK_STARTED
        assert ev.task_id == "task_123"
        assert ev.message == "Task initialized"
        assert ev.target_app is None
        assert ev.step_index is None
        assert ev.total_steps is None
        assert isinstance(ev.payload, dict)
        assert isinstance(ev.timestamp, float)
        assert ev.timestamp > 0

    def test_execution_event_string_event_type_coercion(self) -> None:
        """Verifies that passing a string event_type auto-coerces to EventType enum."""
        ev = ExecutionEvent(
            event_type="app_launched",  # type: ignore
            task_id="t1",
            target_app="Notes",
        )
        assert isinstance(ev.event_type, EventType)
        assert ev.event_type == EventType.APP_LAUNCHED

    def test_execution_event_serialization_roundtrip(self) -> None:
        """Verifies to_dict, from_dict, to_json, and from_json fidelity."""
        original = ExecutionEvent(
            event_type=EventType.ACTION_STARTING,
            task_id="task_roundtrip",
            message="Clicking button",
            target_app="com.apple.Notes",
            step_index=2,
            total_steps=5,
            payload={"norm_x": 0.5, "norm_y": 0.25, "button": "left"},
        )
        d = original.to_dict()
        assert d["event_type"] == "action_starting"
        assert d["step_index"] == 2
        assert d["payload"]["norm_x"] == 0.5

        restored_from_dict = ExecutionEvent.from_dict(d)
        assert restored_from_dict.event_type == EventType.ACTION_STARTING
        assert restored_from_dict.task_id == original.task_id
        assert restored_from_dict.payload == original.payload

        json_str = original.to_json()
        restored_from_json = ExecutionEvent.from_json(json_str)
        assert restored_from_json.event_type == original.event_type
        assert restored_from_json.step_index == 2
        assert restored_from_json.payload["button"] == "left"

    def test_execution_event_string_representation(self) -> None:
        """Verifies human-readable string formatting for logging and CLI output."""
        ev = ExecutionEvent(
            event_type=EventType.ACTION_COMPLETED,
            task_id="wf_notes",
            message="Note created successfully",
            target_app="Notes",
            step_index=3,
            total_steps=5,
        )
        s = str(ev)
        assert "<wf_notes>" in s
        assert "[ACTION_COMPLETED]" in s
        assert "[3/5]" in s
        assert "(Notes)" in s
        assert "Note created successfully" in s


class TestExecutionEventBus:
    """Tests for ExecutionEventBus pub/sub, concurrency, and error isolation."""

    def test_exact_subscription(self) -> None:
        """Verifies that exact subscribers only receive matching event types."""
        bus = ExecutionEventBus()
        received_window: List[ExecutionEvent] = []
        received_action: List[ExecutionEvent] = []

        bus.subscribe(EventType.WINDOW_READY, lambda e: received_window.append(e))
        bus.subscribe(EventType.ACTION_STARTING, lambda e: received_action.append(e))

        ev_win = ExecutionEvent(event_type=EventType.WINDOW_READY, task_id="t1")
        ev_act = ExecutionEvent(event_type=EventType.ACTION_STARTING, task_id="t1")
        ev_other = ExecutionEvent(event_type=EventType.TASK_COMPLETED, task_id="t1")

        bus.publish(ev_win)
        bus.publish(ev_act)
        bus.publish(ev_other)

        assert len(received_window) == 1
        assert received_window[0] == ev_win
        assert len(received_action) == 1
        assert received_action[0] == ev_act

    def test_wildcard_subscription(self) -> None:
        """Verifies that wildcard subscribers receive all emitted event types."""
        bus = ExecutionEventBus()
        all_events: List[ExecutionEvent] = []

        # Form 1: single argument callback
        bus.subscribe(lambda e: all_events.append(e))

        # Form 2: explicit "*" string
        wildcard_events: List[ExecutionEvent] = []
        bus.subscribe("*", lambda e: wildcard_events.append(e))

        bus.publish(ExecutionEvent(event_type=EventType.TASK_STARTED, task_id="t1"))
        bus.publish(ExecutionEvent(event_type=EventType.ACTION_COMPLETED, task_id="t1"))

        assert len(all_events) == 2
        assert len(wildcard_events) == 2
        assert all_events[0].event_type == EventType.TASK_STARTED
        assert all_events[1].event_type == EventType.ACTION_COMPLETED

    def test_unsubscription_token(self) -> None:
        """Verifies that the callable token returned by subscribe removes the listener."""
        bus = ExecutionEventBus()
        received: List[ExecutionEvent] = []

        unsub = bus.subscribe(EventType.STEP_FAILED, lambda e: received.append(e))
        assert bus.listener_count(EventType.STEP_FAILED) == 1

        ev = ExecutionEvent(event_type=EventType.STEP_FAILED, task_id="t1")
        bus.publish(ev)
        assert len(received) == 1

        # Call unsubscription token
        unsub()
        assert bus.listener_count(EventType.STEP_FAILED) == 0

        bus.publish(ev)
        assert len(received) == 1

    def test_unsubscribe_method(self) -> None:
        """Verifies explicit bus.unsubscribe by callback and event_type."""
        bus = ExecutionEventBus()
        counter = 0

        def callback(e: ExecutionEvent) -> None:
            nonlocal counter
            counter += 1

        bus.subscribe(EventType.ACTION_STARTING, callback)
        bus.subscribe(EventType.ACTION_COMPLETED, callback)
        bus.subscribe("*", callback)

        # Total registrations = 1 exact + 1 exact + 1 wildcard = 3
        assert bus.listener_count() == 3

        # Unsubscribe only from ACTION_STARTING
        removed = bus.unsubscribe(callback, EventType.ACTION_STARTING)
        assert removed is True
        assert bus.listener_count() == 2

        # Unsubscribe completely from everything
        bus.unsubscribe(callback)
        assert bus.listener_count() == 0

    def test_error_isolation(self) -> None:
        """Verifies that an exception in one listener does NOT disrupt others or caller."""
        bus = ExecutionEventBus()
        called_before = False
        called_after = False

        def listener_good1(e: ExecutionEvent) -> None:
            nonlocal called_before
            called_before = True

        def listener_bad(e: ExecutionEvent) -> None:
            raise RuntimeError("Listener disaster!")

        def listener_good2(e: ExecutionEvent) -> None:
            nonlocal called_after
            called_after = True

        bus.subscribe(EventType.TASK_STARTED, listener_good1)
        bus.subscribe(EventType.TASK_STARTED, listener_bad)
        bus.subscribe(EventType.TASK_STARTED, listener_good2)

        # Publishing should NOT raise exception
        bus.publish(ExecutionEvent(event_type=EventType.TASK_STARTED, task_id="t1"))

        assert called_before is True
        assert called_after is True

        # Check recorded errors
        errors = bus.get_errors()
        assert len(errors) == 1
        fn, ev, exc = errors[0]
        assert fn == listener_bad
        assert isinstance(exc, RuntimeError)
        assert str(exc) == "Listener disaster!"

    def test_custom_error_callback(self) -> None:
        """Verifies on_error callback is invoked when a subscriber fails."""
        captured_error = None

        def handle_error(fn, ev, exc) -> None:
            nonlocal captured_error
            captured_error = (fn, ev.task_id, str(exc))

        bus = ExecutionEventBus(on_error=handle_error)
        bus.subscribe(EventType.TASK_FAILED, lambda e: 1 / 0)

        bus.publish(ExecutionEvent(event_type=EventType.TASK_FAILED, task_id="task_div_zero"))
        assert captured_error is not None
        assert captured_error[1] == "task_div_zero"
        assert "division by zero" in captured_error[2]

    def test_history_recording_and_bounded_capacity(self) -> None:
        """Verifies event history buffer and maximum capacity bounds."""
        bus = ExecutionEventBus(record_history=True, max_history=5)
        for i in range(10):
            bus.publish(
                ExecutionEvent(
                    event_type=EventType.TEXT_TYPING_PROGRESS if i % 2 == 0 else EventType.ACTION_STARTING,
                    task_id=f"t_{i}",
                )
            )

        history = bus.get_history()
        assert len(history) == 5
        # Oldest events should have been dropped, leaving t_5 through t_9
        assert history[0].task_id == "t_5"
        assert history[-1].task_id == "t_9"

        # Filter history by event type
        typing_events = bus.get_history(EventType.TEXT_TYPING_PROGRESS)
        assert all(e.event_type == EventType.TEXT_TYPING_PROGRESS for e in typing_events)

        bus.clear_history()
        assert len(bus.get_history()) == 0

    def test_asynchronous_dispatch_and_flush(self) -> None:
        """Verifies publish_async offloads event to background worker thread."""
        bus = ExecutionEventBus()
        received_async: List[ExecutionEvent] = []
        worker_threads: List[int] = []
        main_thread_id = threading.get_ident()

        def async_listener(ev: ExecutionEvent) -> None:
            worker_threads.append(threading.get_ident())
            received_async.append(ev)

        bus.subscribe(EventType.ACTION_STARTING, async_listener)

        # Publish asynchronously
        for i in range(5):
            bus.publish_async(ExecutionEvent(event_type=EventType.ACTION_STARTING, task_id=f"async_{i}"))

        # Block until queue drained
        success = bus.flush(timeout=3.0)
        assert success is True
        assert len(received_async) == 5

        # Verify listeners executed on background worker thread
        assert all(tid != main_thread_id for tid in worker_threads)

        bus.close()

    def test_concurrent_multithreaded_publishing(self) -> None:
        """Stress tests thread-safe concurrency under 20 publisher threads."""
        bus = ExecutionEventBus(record_history=True, max_history=1000)
        counter = 0
        lock = threading.Lock()

        def listener(ev: ExecutionEvent) -> None:
            nonlocal counter
            with lock:
                counter += 1

        bus.subscribe("*", listener)

        threads = []
        events_per_thread = 25
        num_threads = 20

        for t_idx in range(num_threads):
            t = threading.Thread(
                target=lambda idx: [
                    bus.publish(
                        ExecutionEvent(
                            event_type=EventType.TEXT_TYPING_PROGRESS,
                            task_id=f"thread_{idx}_{step}",
                        )
                    )
                    for step in range(events_per_thread)
                ],
                args=(t_idx,),
            )
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        assert counter == num_threads * events_per_thread
        assert len(bus.get_history()) == num_threads * events_per_thread
