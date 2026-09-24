"""Unit tests for FEAT-EXEC-03 (src/executor/poller.py).

Comprehensive unit and boundary tests covering:
- WindowReadinessPoller immediate resolution
- Geometry stability detection across animated/resizing windows
- Title matching modes: substring, exact, and regular expressions
- Dimension filtering (ignoring 0x0 or sub-threshold phantom windows)
- Frontmost application prerequisites
- Timeout exhaustion and error propagation
- Screen corner failsafe emergency interception during polling
- Event bus integration (WINDOW_WAITING and WINDOW_READY emissions)
- Application frontmost and window closure lifecycle polling
- Dual-use invocation (instance vs static/class method)
"""

from __future__ import annotations

import re
import time
from typing import List, Optional
import pytest

from src.actuators.mock import MockActuator
from src.actuators.types import (
    ApplicationLaunchError,
    FailsafeEmergencyStop,
    WindowInfo,
    WindowNotFoundError,
)
from src.executor.events import EventType, ExecutionEvent, ExecutionEventBus
from src.executor.poller import WindowReadinessPoller


@pytest.fixture
def mock_actuator() -> MockActuator:
    actuator = MockActuator(screen_size=(1920.0, 1080.0), initial_pos=(500.0, 500.0))
    # Finder desktop window
    actuator.add_virtual_window(
        WindowInfo(
            window_id=1,
            owner_name="com.apple.finder",
            title="Desktop",
            x=0.0,
            y=0.0,
            width=1920.0,
            height=1080.0,
        )
    )
    return actuator


@pytest.fixture
def event_bus() -> ExecutionEventBus:
    return ExecutionEventBus()


class TestWindowReadinessPoller:
    """Unit tests for WindowReadinessPoller."""

    def test_poller_immediate_success(self, mock_actuator: MockActuator) -> None:
        """Verifies poller returns existing stable window immediately."""
        notes_win = WindowInfo(
            window_id=101,
            owner_name="com.apple.Notes",
            title="Quick Notes",
            x=150.0,
            y=120.0,
            width=700.0,
            height=500.0,
        )
        mock_actuator.add_virtual_window(notes_win)

        poller = WindowReadinessPoller(actuator=mock_actuator, min_stable_samples=1)
        win = poller.wait_for_window(app_name="Notes", timeout=1.0, poll_interval=0.01)

        assert win is not None
        assert win.window_id == 101
        assert win.owner_name == "com.apple.Notes"

    def test_poller_geometry_stability_check(self, mock_actuator: MockActuator) -> None:
        """Verifies poller waits until window geometry stops changing across samples."""
        poller = WindowReadinessPoller(actuator=mock_actuator, min_stable_samples=2)

        # Window starts small and expands (simulating window zoom/open animation)
        w_anim1 = WindowInfo(window_id=202, owner_name="Notes", title="Notes", x=100, y=100, width=200, height=150)
        w_anim2 = WindowInfo(window_id=202, owner_name="Notes", title="Notes", x=100, y=100, width=500, height=350)
        w_stable = WindowInfo(window_id=202, owner_name="Notes", title="Notes", x=100, y=100, width=800, height=600)

        # Simulate dynamic window updating
        samples = [w_anim1, w_anim2, w_stable, w_stable]
        idx = 0

        def dynamic_get_windows(app_name=None):
            nonlocal idx
            w = samples[min(idx, len(samples) - 1)]
            idx += 1
            return [w]

        mock_actuator.get_windows = dynamic_get_windows  # type: ignore

        win = poller.wait_for_window(app_name="Notes", timeout=2.0, poll_interval=0.01)
        assert win is not None
        assert win.bounds == (100.0, 100.0, 800.0, 600.0)
        assert idx >= 3  # Must have polled through animated frames to reach 2 stable samples

    def test_title_matching_substring(self, mock_actuator: MockActuator) -> None:
        """Verifies substring title matching (case-insensitive default)."""
        mock_actuator.add_virtual_window(
            WindowInfo(window_id=301, owner_name="Safari", title="Apple Inc. — Start Page", x=50, y=50, width=800, height=600)
        )

        poller = WindowReadinessPoller(actuator=mock_actuator)
        win = poller.wait_for_window(app_name="Safari", title="start page", title_match_mode="substring", timeout=0.5, poll_interval=0.01)
        assert win is not None
        assert win.window_id == 301

    def test_title_matching_exact(self, mock_actuator: MockActuator) -> None:
        """Verifies exact title matching rejects partial matches and accepts exact match."""
        mock_actuator.add_virtual_window(
            WindowInfo(window_id=401, owner_name="Notes", title="Notes - Weekly List", x=50, y=50, width=800, height=600)
        )

        poller = WindowReadinessPoller(actuator=mock_actuator)

        # Partial match should fail exact match and timeout
        win_partial = poller.wait_for_window(
            app_name="Notes", title="Notes", title_match_mode="exact", timeout=0.05, poll_interval=0.01, raise_on_timeout=False
        )
        assert win_partial is None

        # Full exact title should succeed
        win_exact = poller.wait_for_window(
            app_name="Notes", title="Notes - Weekly List", title_match_mode="exact", timeout=0.5, poll_interval=0.01
        )
        assert win_exact is not None
        assert win_exact.window_id == 401

    def test_title_matching_regex(self, mock_actuator: MockActuator) -> None:
        """Verifies regular expression title matching."""
        mock_actuator.add_virtual_window(
            WindowInfo(window_id=501, owner_name="TextEdit", title="Untitled 42.txt", x=50, y=50, width=800, height=600)
        )

        poller = WindowReadinessPoller(actuator=mock_actuator)
        win = poller.wait_for_window(
            app_name="TextEdit",
            title=r"Untitled\s+\d+\.txt",
            title_match_mode="regex",
            timeout=0.5,
            poll_interval=0.01,
        )
        assert win is not None
        assert win.window_id == 501

    def test_title_matching_invalid_regex(self, mock_actuator: MockActuator) -> None:
        """Verifies invalid regex pattern raises ValueError."""
        poller = WindowReadinessPoller(actuator=mock_actuator)
        with pytest.raises(ValueError, match="Invalid regex pattern"):
            poller.wait_for_window(
                app_name="TextEdit",
                title=r"[unclosed-regex",
                title_match_mode="regex",
                timeout=0.1,
            )

    def test_dimension_filtering(self, mock_actuator: MockActuator) -> None:
        """Verifies that invisible or phantom 0x0 windows are rejected."""
        # 0x0 phantom window
        mock_actuator.add_virtual_window(
            WindowInfo(window_id=601, owner_name="Notes", title="Notes", x=0, y=0, width=0, height=0)
        )
        poller = WindowReadinessPoller(actuator=mock_actuator)

        with pytest.raises(WindowNotFoundError):
            poller.wait_for_window(app_name="Notes", min_width=100.0, min_height=100.0, timeout=0.05, poll_interval=0.01)

    def test_require_frontmost(self, mock_actuator: MockActuator) -> None:
        """Verifies require_frontmost waits until target application is active."""
        mock_actuator.add_virtual_window(
            WindowInfo(window_id=701, owner_name="Notes", title="Notes", x=100, y=100, width=800, height=600)
        )
        mock_actuator.frontmost_app = "com.apple.finder"

        poller = WindowReadinessPoller(actuator=mock_actuator)

        # Should timeout while frontmost is Finder
        win = poller.wait_for_window(
            app_name="Notes", require_frontmost=True, timeout=0.05, poll_interval=0.01, raise_on_timeout=False
        )
        assert win is None

        # Now activate Notes as frontmost
        mock_actuator.frontmost_app = "com.apple.Notes"
        win = poller.wait_for_window(
            app_name="Notes", require_frontmost=True, timeout=0.5, poll_interval=0.01
        )
        assert win is not None
        assert win.window_id == 701

    def test_timeout_exhaustion_raise_vs_no_raise(self, mock_actuator: MockActuator) -> None:
        """Verifies raise_on_timeout behavior."""
        poller = WindowReadinessPoller(actuator=mock_actuator)

        # raise_on_timeout=True (default) -> WindowNotFoundError
        with pytest.raises(WindowNotFoundError, match="Timed out"):
            poller.wait_for_window(app_name="NonExistentApp", timeout=0.05, poll_interval=0.01)

        # assert_window_ready -> WindowNotFoundError
        with pytest.raises(WindowNotFoundError):
            poller.assert_window_ready(app_name="NonExistentApp", timeout=0.05, poll_interval=0.01)

        # raise_on_timeout=False -> returns None
        result = poller.wait_for_window(app_name="NonExistentApp", timeout=0.05, poll_interval=0.01, raise_on_timeout=False)
        assert result is None

    def test_failsafe_immediate_interception(self, mock_actuator: MockActuator) -> None:
        """Verifies that mouse moved to screen corner triggers FailsafeEmergencyStop immediately."""
        poller = WindowReadinessPoller(actuator=mock_actuator)

        # Place cursor in top-left corner
        mock_actuator.trigger_failsafe(position=(0.0, 0.0))

        # Poller must raise FailsafeEmergencyStop on first tick, not wait 5 seconds!
        start_t = time.monotonic()
        with pytest.raises(FailsafeEmergencyStop):
            poller.wait_for_window(app_name="Notes", timeout=5.0, poll_interval=0.01)
        duration = time.monotonic() - start_t

        assert duration < 0.2, f"Failsafe check was delayed ({duration:.2f}s) instead of immediate!"

    def test_event_bus_lifecycle_emissions(self, mock_actuator: MockActuator, event_bus: ExecutionEventBus) -> None:
        """Verifies WINDOW_WAITING and WINDOW_READY events are broadcast to event bus."""
        emitted_events: List[ExecutionEvent] = []
        event_bus.subscribe("*", lambda e: emitted_events.append(e))

        notes_win = WindowInfo(window_id=801, owner_name="com.apple.Notes", title="Notes", x=100, y=100, width=800, height=600)
        mock_actuator.add_virtual_window(notes_win)

        poller = WindowReadinessPoller(actuator=mock_actuator, event_bus=event_bus, min_stable_samples=1)
        win = poller.wait_for_window(app_name="Notes", timeout=1.0, poll_interval=0.01, task_id="wf_benchmark")

        assert win is not None
        assert len(emitted_events) == 2
        assert emitted_events[0].event_type == EventType.WINDOW_WAITING
        assert emitted_events[0].task_id == "wf_benchmark"
        assert emitted_events[1].event_type == EventType.WINDOW_READY
        assert emitted_events[1].payload["window_id"] == 801

    def test_wait_for_app_frontmost(self, mock_actuator: MockActuator) -> None:
        """Verifies wait_for_app_frontmost polling."""
        mock_actuator.frontmost_app = "com.apple.finder"
        poller = WindowReadinessPoller(actuator=mock_actuator)

        # Timeout when app is not frontmost
        with pytest.raises(ApplicationLaunchError):
            poller.wait_for_app_frontmost(bundle_or_name="com.apple.Notes", timeout=0.05, poll_interval=0.01)

        # Succeeded when frontmost is switched
        mock_actuator.frontmost_app = "com.apple.Notes"
        assert poller.wait_for_app_frontmost(bundle_or_name="Notes", timeout=0.5, poll_interval=0.01) is True

    def test_wait_for_window_close(self, mock_actuator: MockActuator) -> None:
        """Verifies wait_for_window_close polling."""
        win = WindowInfo(window_id=999, owner_name="Notes", title="Closing Window", x=100, y=100, width=400, height=300)
        mock_actuator.add_virtual_window(win)

        poller = WindowReadinessPoller(actuator=mock_actuator)

        # Still open -> timeout
        with pytest.raises(WindowNotFoundError):
            poller.wait_for_window_close(window_id=999, timeout=0.05, poll_interval=0.01)

        # Window closed -> success
        mock_actuator.clear_virtual_windows()
        assert poller.wait_for_window_close(window_id=999, timeout=0.5, poll_interval=0.01) is True

    def test_static_and_instance_dual_use(self, mock_actuator: MockActuator) -> None:
        """Verifies WindowReadinessPoller works as both an instance and static class method."""
        win = WindowInfo(window_id=123, owner_name="Calculator", title="Calculator", x=50, y=50, width=300, height=400)
        mock_actuator.add_virtual_window(win)

        # 1. Static invocation
        res_static = WindowReadinessPoller.wait_for_window(
            actuator=mock_actuator, app_name="Calculator", timeout=0.5, poll_interval=0.01, min_stable_samples=1
        )
        assert res_static is not None
        assert res_static.window_id == 123

        # 2. Instance invocation
        poller = WindowReadinessPoller(actuator=mock_actuator, min_stable_samples=1)
        res_instance = poller.wait_for_window(app_name="Calculator", timeout=0.5, poll_interval=0.01)
        assert res_instance is not None
        assert res_instance.window_id == 123
