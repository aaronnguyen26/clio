"""Zero-Brittle-Sleep Dynamic Window Readiness Poller.

Belongs to FEAT-EXEC-03 (WindowReadinessPoller).
Zero external dependencies: pure Python standard library.
Features:
- Dynamic state polling: replaces arbitrary sleeps with condition verification.
- Checks target window visibility via BaseActuator.get_windows(app_name).
- App frontmost verification via BaseActuator.get_frontmost_app().
- Geometry stability check: ensures window bounds remain identical across
  consecutive samples (min_stable_samples >= 2) before allowing clicks or inputs.
- Flexible title matching: supports substring, exact, or regular expression matching.
- Immediate corner failsafe detection: checks actuator.check_failsafe() on each tick.
- Event bus integration: broadcasts WINDOW_WAITING and WINDOW_READY lifecycle events.
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar, Union

from src.actuators.base import BaseActuator
from src.actuators.types import (
    ActuatorError,
    ApplicationLaunchError,
    FailsafeEmergencyStop,
    WindowInfo,
    WindowNotFoundError,
)
from src.executor.events import EventType, ExecutionEvent, ExecutionEventBus

T = TypeVar("T")


class _hybridmethod:
    """Descriptor enabling a method to be called cleanly on either an instance or class."""

    def __init__(self, func: Any) -> None:
        self.func = func

    def __get__(self, instance: Any, owner: Any) -> Any:
        if instance is None:
            def _class_bound(*args: Any, **kwargs: Any) -> Any:
                return self.func(owner, *args, **kwargs)
            return _class_bound
        def _instance_bound(*args: Any, **kwargs: Any) -> Any:
            return self.func(instance, *args, **kwargs)
        return _instance_bound


class WindowReadinessPoller:
    """Zero-brittle-sleep dynamic state poller waiting for target app and window visibility."""

    def __init__(
        self,
        actuator: Optional[BaseActuator] = None,
        event_bus: Optional[ExecutionEventBus] = None,
        default_timeout: float = 5.0,
        default_poll_interval: float = 0.1,
        min_stable_samples: int = 2,
    ) -> None:
        """Initializes poller with optional defaults and dependency injection.

        Args:
            actuator: Optional default BaseActuator instance.
            event_bus: Optional default ExecutionEventBus instance.
            default_timeout: Default seconds to wait before timeout (default: 5.0).
            default_poll_interval: Default polling interval in seconds (default: 0.1).
            min_stable_samples: Consecutive stable samples required (default: 2).
        """
        self.actuator = actuator
        self.event_bus = event_bus
        self.default_timeout = default_timeout
        self.default_poll_interval = default_poll_interval
        self.min_stable_samples = min_stable_samples

    # =========================================================================
    # Window Readiness Polling
    # =========================================================================

    @_hybridmethod
    def wait_for_window(
        self_or_cls: Any,
        *args: Any,
        app_name: Optional[str] = None,
        title: Optional[str] = None,
        title_match_mode: str = "substring",
        timeout: Optional[float] = None,
        poll_interval: Optional[float] = None,
        min_width: float = 10.0,
        min_height: float = 10.0,
        require_frontmost: bool = False,
        min_stable_samples: Optional[int] = None,
        raise_on_timeout: bool = True,
        actuator: Optional[BaseActuator] = None,
        event_bus: Optional[ExecutionEventBus] = None,
        task_id: str = "window_poller",
        **kwargs: Any,
    ) -> Optional[WindowInfo]:
        """Polls dynamically until a target application window is visible and geometrically stable.

        Supports both instance invocation:
            poller.wait_for_window(app_name="Notes")
        and static/class invocation:
            WindowReadinessPoller.wait_for_window(actuator=mock_actuator, app_name="Notes")

        Args:
            app_name: Application bundle ID (e.g. 'com.apple.Notes') or name (e.g. 'Notes').
            title: Window title or pattern to match.
            title_match_mode: Matching strategy: 'substring', 'exact', or 'regex'.
            timeout: Maximum seconds to wait (defaults to instance setting or 5.0).
            poll_interval: Polling sleep interval in seconds (defaults to instance setting or 0.1).
            min_width: Minimum acceptable window width in points (default: 10.0).
            min_height: Minimum acceptable window height in points (default: 10.0).
            require_frontmost: If True, target app must also be frontmost in OS.
            min_stable_samples: Number of consecutive samples with identical bounds
                                required to certify window is not mid-animation (default: 2).
            raise_on_timeout: If True, raises WindowNotFoundError on timeout; if False, returns None.
            actuator: Explicit BaseActuator (overrides instance default if provided).
            event_bus: Explicit ExecutionEventBus (overrides instance default if provided).
            task_id: Task identifier for emitted lifecycle events.

        Returns:
            WindowInfo object of the ready window, or None if timed out with raise_on_timeout=False.

        Raises:
            WindowNotFoundError: If timeout is reached and raise_on_timeout=True.
            FailsafeEmergencyStop: If cursor enters screen corner failsafe during polling.
            ValueError: If title_match_mode is unknown or regex pattern is invalid.
        """
        # Parse positional args if passed to class call
        if args:
            if isinstance(args[0], BaseActuator):
                actuator = args[0]
                if len(args) > 1 and isinstance(args[1], str):
                    app_name = args[1]
            elif isinstance(args[0], str) and app_name is None:
                app_name = args[0]

        # Resolve self vs actuator
        if isinstance(self_or_cls, WindowReadinessPoller):
            poller_instance = self_or_cls
            active_actuator = actuator or poller_instance.actuator
            active_bus = event_bus or poller_instance.event_bus
            to = timeout if timeout is not None else poller_instance.default_timeout
            interval = poll_interval if poll_interval is not None else poller_instance.default_poll_interval
            stable_req = min_stable_samples if min_stable_samples is not None else poller_instance.min_stable_samples
        else:
            active_actuator = actuator
            active_bus = event_bus
            to = timeout if timeout is not None else 5.0
            interval = poll_interval if poll_interval is not None else 0.1
            stable_req = min_stable_samples if min_stable_samples is not None else 2

        if active_actuator is None:
            raise ValueError("No BaseActuator provided to WindowReadinessPoller.")

        # Compile regex if regex match mode requested
        compiled_regex = None
        if title and title_match_mode.lower() == "regex":
            try:
                compiled_regex = re.compile(title, re.IGNORECASE)
            except re.error as e:
                raise ValueError(f"Invalid regex pattern for window title '{title}': {e}") from e

        # Broadcast WINDOW_WAITING event if event bus attached
        if active_bus:
            active_bus.publish(
                ExecutionEvent(
                    event_type=EventType.WINDOW_WAITING,
                    task_id=task_id,
                    message=f"Waiting for window: app='{app_name or '*'}' title='{title or '*'}' (mode: {title_match_mode})",
                    target_app=app_name,
                    payload={
                        "app_name": app_name,
                        "title": title,
                        "title_match_mode": title_match_mode,
                        "timeout": to,
                        "poll_interval": interval,
                    },
                )
            )

        deadline = time.monotonic() + to
        stable_bounds: Dict[int, Tuple[float, float, float, float]] = {}
        stable_counts: Dict[int, int] = {}

        while time.monotonic() < deadline:
            # Check screen corner failsafe on every loop tick
            active_actuator.check_failsafe()

            # If frontmost application check is requested
            if require_frontmost and app_name:
                frontmost = active_actuator.get_frontmost_app().lower()
                target_lower = app_name.lower()
                if (target_lower not in frontmost) and (frontmost not in target_lower):
                    time.sleep(interval)
                    continue

            # Query windows
            windows = active_actuator.get_windows(app_name)

            for w in windows:
                # 1. Dimension validation
                if w.width < min_width or w.height < min_height:
                    continue

                # 2. Title matching
                if title:
                    matched = False
                    mode_clean = title_match_mode.lower().strip()
                    if mode_clean == "substring":
                        matched = title.lower() in w.title.lower()
                    elif mode_clean == "exact":
                        matched = title.strip().lower() == w.title.strip().lower()
                    elif mode_clean == "regex" and compiled_regex:
                        matched = bool(compiled_regex.search(w.title))
                    else:
                        raise ValueError(f"Unknown title_match_mode: '{title_match_mode}'")

                    if not matched:
                        continue

                # 3. Geometry stability check across consecutive samples
                cur_bounds = w.bounds
                if stable_req <= 1:
                    # Single sample sufficient
                    if active_bus:
                        active_bus.publish(
                            ExecutionEvent(
                                event_type=EventType.WINDOW_READY,
                                task_id=task_id,
                                message=f"Window '{w.title}' is ready (bounds: {w.bounds})",
                                target_app=w.owner_name,
                                payload={
                                    "window_id": w.window_id,
                                    "title": w.title,
                                    "bounds": cur_bounds,
                                    "owner_name": w.owner_name,
                                },
                            )
                        )
                    return w

                # Check if bounds match previous sample for this window_id
                prev_bounds = stable_bounds.get(w.window_id)
                if prev_bounds == cur_bounds:
                    count = stable_counts.get(w.window_id, 0) + 1
                    stable_counts[w.window_id] = count
                    if count >= stable_req:
                        # Certified stable and ready
                        if active_bus:
                            active_bus.publish(
                                ExecutionEvent(
                                    event_type=EventType.WINDOW_READY,
                                    task_id=task_id,
                                    message=f"Window '{w.title}' is ready and stable (bounds: {w.bounds})",
                                    target_app=w.owner_name,
                                    payload={
                                        "window_id": w.window_id,
                                        "title": w.title,
                                        "bounds": cur_bounds,
                                        "owner_name": w.owner_name,
                                        "stable_samples": count,
                                    },
                                )
                            )
                        return w
                else:
                    stable_bounds[w.window_id] = cur_bounds
                    stable_counts[w.window_id] = 1

            time.sleep(interval)

        # Timeout reached
        if raise_on_timeout:
            desc = f"app='{app_name}'" if app_name else "any app"
            if title:
                desc += f", title='{title}' ({title_match_mode})"
            raise WindowNotFoundError(f"Timed out after {to:.2f}s waiting for window: {desc}")
        return None

    def assert_window_ready(
        self,
        app_name: Optional[str] = None,
        title: Optional[str] = None,
        title_match_mode: str = "substring",
        timeout: Optional[float] = None,
        poll_interval: Optional[float] = None,
        min_width: float = 10.0,
        min_height: float = 10.0,
        require_frontmost: bool = False,
        min_stable_samples: Optional[int] = None,
    ) -> WindowInfo:
        """Strict window readiness assertion; raises WindowNotFoundError if not found."""
        win = self.wait_for_window(
            app_name=app_name,
            title=title,
            title_match_mode=title_match_mode,
            timeout=timeout,
            poll_interval=poll_interval,
            min_width=min_width,
            min_height=min_height,
            require_frontmost=require_frontmost,
            min_stable_samples=min_stable_samples,
            raise_on_timeout=True,
        )
        assert win is not None
        return win

    # =========================================================================
    # Application & Window Lifecycle Helpers
    # =========================================================================

    def wait_for_app_frontmost(
        self,
        bundle_or_name: str,
        timeout: Optional[float] = None,
        poll_interval: Optional[float] = None,
        raise_on_timeout: bool = True,
    ) -> bool:
        """Polls until the specified application becomes the active frontmost app."""
        if not self.actuator:
            raise ValueError("No BaseActuator provided to WindowReadinessPoller.")

        to = timeout if timeout is not None else self.default_timeout
        interval = poll_interval if poll_interval is not None else self.default_poll_interval
        deadline = time.monotonic() + to
        target = bundle_or_name.lower().strip()

        while time.monotonic() < deadline:
            self.actuator.check_failsafe()
            front = self.actuator.get_frontmost_app().lower().strip()
            if target in front or front in target:
                return True
            time.sleep(interval)

        if raise_on_timeout:
            raise ApplicationLaunchError(
                f"Timed out after {to:.2f}s waiting for app '{bundle_or_name}' to become frontmost."
            )
        return False

    def wait_for_window_close(
        self,
        window_id: int,
        timeout: Optional[float] = None,
        poll_interval: Optional[float] = None,
        raise_on_timeout: bool = True,
    ) -> bool:
        """Polls until a specific window ID is closed and no longer visible on screen."""
        if not self.actuator:
            raise ValueError("No BaseActuator provided to WindowReadinessPoller.")

        to = timeout if timeout is not None else self.default_timeout
        interval = poll_interval if poll_interval is not None else self.default_poll_interval
        deadline = time.monotonic() + to

        while time.monotonic() < deadline:
            self.actuator.check_failsafe()
            all_windows = self.actuator.get_windows()
            if not any(w.window_id == window_id for w in all_windows):
                return True
            time.sleep(interval)

        if raise_on_timeout:
            raise WindowNotFoundError(
                f"Timed out after {to:.2f}s waiting for window ID {window_id} to close."
            )
        return False

    # =========================================================================
    # Generic Dynamic Condition Poller
    # =========================================================================

    def poll_until(
        self,
        predicate: Callable[[], Optional[T]],
        timeout: Optional[float] = None,
        poll_interval: Optional[float] = None,
        error_message: str = "Condition not met within timeout",
    ) -> T:
        """Generic dynamic condition poller with inline failsafe checking.

        Args:
            predicate: Callable returning truthy value or None.
            timeout: Maximum seconds to poll.
            poll_interval: Sleep interval between checks.
            error_message: Error text if deadline expires.

        Returns:
            The truthy return value of predicate.

        Raises:
            TimeoutError: If condition is not met within timeout.
            FailsafeEmergencyStop: If emergency stop is triggered during polling.
        """
        to = timeout if timeout is not None else self.default_timeout
        interval = poll_interval if poll_interval is not None else self.default_poll_interval
        deadline = time.monotonic() + to

        while time.monotonic() < deadline:
            if self.actuator:
                self.actuator.check_failsafe()
            res = predicate()
            if res is not None and bool(res):
                return res
            time.sleep(interval)

        raise TimeoutError(f"{error_message} (timeout: {to:.2f}s)")
