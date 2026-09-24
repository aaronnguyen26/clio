"""Deterministic In-Memory Mock Actuator Suite.

Belongs to FEAT-ACT-09 (MockActuatorSuite).
Provides deterministic simulation of OS automation for 100% automated test coverage in CI/CD.
Features full state tracking, in-memory action audit log, and comprehensive assertion helpers.
Zero external dependencies: pure Python standard library.
"""

from __future__ import annotations

import math
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from src.actuators.base import BaseActuator
from src.actuators.types import (
    ActuatedAction,
    ApplicationLaunchError,
    FailsafeEmergencyStop,
    InputSynthesisError,
    MouseButton,
    WindowInfo,
    get_screen_corner,
    is_screen_corner,
    normalize_mouse_button,
)


class MockActuator(BaseActuator):
    """Deterministic in-memory actuator tracking state and action history."""

    def __init__(
        self,
        screen_size: Tuple[float, float] = (1920.0, 1080.0),
        initial_pos: Tuple[float, float] = (500.0, 500.0),
        failsafe_margin: float = 5.0,
        enforce_corner_failsafe: bool = True,
    ) -> None:
        """Initializes mock actuator with virtual display and state.

        Args:
            screen_size: Dimensions of virtual primary display (width, height).
            initial_pos: Initial cursor (x, y) coordinates.
            failsafe_margin: Distance from display corners in points to trigger failsafe.
            enforce_corner_failsafe: If True, moving cursor to corner triggers failsafe.
        """
        self._screen_size: Tuple[float, float] = (float(screen_size[0]), float(screen_size[1]))
        self._cursor_pos: Tuple[float, float] = (float(initial_pos[0]), float(initial_pos[1]))
        self._failsafe_margin: float = float(failsafe_margin)
        self.enforce_corner_failsafe: bool = enforce_corner_failsafe

        # Virtual OS state
        self._frontmost_app: str = "com.apple.finder"
        self.running_apps: Set[str] = set()
        self._windows: List[WindowInfo] = []
        self._clipboard: str = ""
        self._active_modifiers: Set[str] = set()
        self._typed_buffer: List[str] = []
        self._failsafe_triggered: bool = False

        # Audit log and test configuration
        self._history: List[ActuatedAction] = []
        self._app_launch_should_fail: Set[str] = set()
        self.raise_on_action: Optional[Exception] = None

    # =========================================================================
    # Properties for attribute compatibility
    # =========================================================================

    @property
    def screen_size(self) -> Tuple[float, float]:
        return self._screen_size

    @screen_size.setter
    def screen_size(self, val: Tuple[float, float]) -> None:
        self._screen_size = (float(val[0]), float(val[1]))

    @property
    def cursor_pos(self) -> Tuple[float, float]:
        return self._cursor_pos

    @cursor_pos.setter
    def cursor_pos(self, val: Tuple[float, float]) -> None:
        self._cursor_pos = (float(val[0]), float(val[1]))

    @property
    def failsafe_margin(self) -> float:
        return self._failsafe_margin

    @failsafe_margin.setter
    def failsafe_margin(self, val: float) -> None:
        self._failsafe_margin = float(val)

    @property
    def frontmost_app(self) -> str:
        return self._frontmost_app

    @frontmost_app.setter
    def frontmost_app(self, val: str) -> None:
        self._frontmost_app = val

    @property
    def windows(self) -> List[WindowInfo]:
        return self._windows

    @windows.setter
    def windows(self, val: List[WindowInfo]) -> None:
        self._windows = list(val)

    @property
    def clipboard(self) -> str:
        return self._clipboard

    @clipboard.setter
    def clipboard(self, val: str) -> None:
        self._clipboard = val

    @property
    def failsafe_triggered(self) -> bool:
        return self._failsafe_triggered

    @failsafe_triggered.setter
    def failsafe_triggered(self, val: bool) -> None:
        self._failsafe_triggered = val

    @property
    def action_history(self) -> List[ActuatedAction]:
        return self._history

    @action_history.setter
    def action_history(self, val: List[ActuatedAction]) -> None:
        self._history = val

    @property
    def history(self) -> List[ActuatedAction]:
        return list(self._history)

    @property
    def typed_text(self) -> str:
        return "".join(self._typed_buffer)

    @property
    def launch_failures(self) -> Set[str]:
        return self._app_launch_should_fail

    @launch_failures.setter
    def launch_failures(self, val: Set[str]) -> None:
        self._app_launch_should_fail = set(val)

    # =========================================================================
    # Failsafe Protocol
    # =========================================================================

    def is_failsafe_triggered(self) -> bool:
        """Evaluates if failsafe is currently active."""
        if self._failsafe_triggered:
            return True
        if self.enforce_corner_failsafe:
            return is_screen_corner(
                self._cursor_pos[0],
                self._cursor_pos[1],
                self._screen_size[0],
                self._screen_size[1],
                self._failsafe_margin,
            )
        return False

    def check_failsafe(self) -> None:
        """Raises FailsafeEmergencyStop if failsafe is active."""
        if self.is_failsafe_triggered():
            self._active_modifiers.clear()
            corner = get_screen_corner(
                self._cursor_pos[0],
                self._cursor_pos[1],
                self._screen_size[0],
                self._screen_size[1],
                self._failsafe_margin,
            )
            reason = f"mouse in {corner.value} corner" if corner else "manual trigger"
            raise FailsafeEmergencyStop(
                message=f"Emergency stop triggered: {reason}",
                position=self._cursor_pos,
                trigger_reason=reason,
            )

    def trigger_failsafe(self, position: Optional[Tuple[float, float]] = None) -> None:
        """Simulates triggering emergency stop programmatically.

        Args:
            position: Optional corner position to place virtual cursor at.
        """
        self._failsafe_triggered = True
        if position is not None:
            self._cursor_pos = (float(position[0]), float(position[1]))

    def reset_failsafe(self) -> None:
        """Resets failsafe triggered flag."""
        self._failsafe_triggered = False

    # =========================================================================
    # Application Lifecycle Primitives
    # =========================================================================

    def launch_app(self, app_name_or_bundle: str, timeout: float = 5.0) -> bool:
        """Simulates launching an application."""
        self.check_failsafe()

        if self.raise_on_action:
            raise self.raise_on_action

        if app_name_or_bundle in self._app_launch_should_fail:
            action = ActuatedAction(
                action_type="launch_app",
                parameters={
                    "app": app_name_or_bundle,
                    "bundle_id": app_name_or_bundle,
                    "timeout": timeout,
                },
                success=False,
                error=f"Configured launch failure for {app_name_or_bundle}",
            )
            self._history.append(action)
            raise ApplicationLaunchError(f"Failed to launch application: {app_name_or_bundle}")

        self._frontmost_app = app_name_or_bundle
        self.running_apps.add(app_name_or_bundle)

        # Ensure at least one virtual window exists for launched app
        if not any(w.owner_name.lower() == app_name_or_bundle.lower() or app_name_or_bundle.lower() in w.owner_name.lower() for w in self._windows):
            title = app_name_or_bundle.split(".")[-1] if "." in app_name_or_bundle else app_name_or_bundle
            new_win = WindowInfo(
                window_id=len(self._windows) + 100,
                owner_name=app_name_or_bundle,
                title=f"{title} Window",
                x=100.0,
                y=100.0,
                width=800.0,
                height=600.0,
                layer=0,
                is_on_screen=True,
            )
            self._windows.append(new_win)

        self._history.append(
            ActuatedAction(
                action_type="launch_app",
                parameters={
                    "app": app_name_or_bundle,
                    "bundle_id": app_name_or_bundle,
                    "timeout": timeout,
                },
                success=True,
            )
        )
        return True

    def focus_app(self, app_name_or_bundle: str) -> bool:
        """Simulates bringing application to the foreground."""
        self.check_failsafe()

        if self.raise_on_action:
            raise self.raise_on_action

        self._frontmost_app = app_name_or_bundle
        self._history.append(
            ActuatedAction(
                action_type="focus_app",
                parameters={
                    "app": app_name_or_bundle,
                    "bundle_id": app_name_or_bundle,
                },
                success=True,
            )
        )
        return True

    def open_url(self, url: str) -> bool:
        """Simulates opening a URL in browser."""
        self.check_failsafe()
        if self.raise_on_action:
            raise self.raise_on_action

        self._history.append(
            ActuatedAction(
                action_type="open_url",
                parameters={"url": url},
                success=True,
            )
        )
        return True

    def get_frontmost_app(self) -> str:
        """Returns currently active frontmost application."""
        return self._frontmost_app

    def get_windows(self, app_name: Optional[str] = None) -> List[WindowInfo]:
        """Returns virtual windows matching filter."""
        if app_name is None:
            return list(self._windows)
        app_lower = app_name.lower()
        return [
            w for w in self._windows
            if app_lower in w.owner_name.lower() or app_lower in w.title.lower()
        ]

    def get_screen_size(self) -> Tuple[float, float]:
        """Returns virtual screen size."""
        return self._screen_size

    def get_mouse_position(self) -> Tuple[float, float]:
        """Returns current virtual cursor position."""
        return self._cursor_pos

    # =========================================================================
    # Mouse & Input Synthesis Primitives
    # =========================================================================

    def move_mouse(
        self,
        x: float,
        y: float,
        smooth: bool = False,
        duration: float = 0.0,
    ) -> None:
        """Updates virtual mouse cursor position and records action."""
        self.check_failsafe()

        if not (math.isfinite(x) and math.isfinite(y)):
            raise InputSynthesisError(f"Invalid non-finite mouse coordinates: ({x}, {y})")

        if self.raise_on_action:
            raise self.raise_on_action

        self._cursor_pos = (float(x), float(y))

        # Check if moving into corner triggers failsafe
        if self.enforce_corner_failsafe and is_screen_corner(
            x, y, self._screen_size[0], self._screen_size[1], self._failsafe_margin
        ):
            self._failsafe_triggered = True

        self._history.append(
            ActuatedAction(
                action_type="move_mouse",
                parameters={"x": float(x), "y": float(y), "raw_x": x, "raw_y": y, "smooth": smooth, "duration": duration},
                success=True,
            )
        )

    def click(
        self,
        x: Optional[float] = None,
        y: Optional[float] = None,
        button: MouseButton = MouseButton.LEFT,
        click_count: int = 1,
    ) -> None:
        """Simulates mouse click at coordinates."""
        self.check_failsafe()

        if (x is not None and not math.isfinite(x)) or (y is not None and not math.isfinite(y)):
            raise InputSynthesisError(f"Invalid non-finite mouse coordinates: ({x}, {y})")

        if self.raise_on_action:
            raise self.raise_on_action

        if x is not None and y is not None:
            self._cursor_pos = (float(x), float(y))
            if self.enforce_corner_failsafe and is_screen_corner(
                float(x), float(y), self._screen_size[0], self._screen_size[1], self._failsafe_margin
            ):
                self._failsafe_triggered = True

        btn_str = normalize_mouse_button(button)
        self._history.append(
            ActuatedAction(
                action_type="click",
                parameters={
                    "x": self._cursor_pos[0],
                    "y": self._cursor_pos[1],
                    "button": btn_str,
                    "click_count": click_count,
                },
                success=True,
            )
        )

    def drag(
        self,
        start_x: float,
        start_y: float,
        end_x: float,
        end_y: float,
        duration: float = 0.3,
    ) -> None:
        """Simulates dragging from start to end coordinates."""
        self.check_failsafe()

        if not (math.isfinite(start_x) and math.isfinite(start_y) and math.isfinite(end_x) and math.isfinite(end_y)):
            raise InputSynthesisError(
                f"Invalid non-finite drag coordinates: ({start_x}, {start_y}) -> ({end_x}, {end_y})"
            )

        if self.raise_on_action:
            raise self.raise_on_action

        self._cursor_pos = (float(end_x), float(end_y))
        if self.enforce_corner_failsafe and is_screen_corner(
            float(end_x), float(end_y), self._screen_size[0], self._screen_size[1], self._failsafe_margin
        ):
            self._failsafe_triggered = True

        self._history.append(
            ActuatedAction(
                action_type="drag",
                parameters={
                    "start": (float(start_x), float(start_y)),
                    "end": (float(end_x), float(end_y)),
                    "start_x": float(start_x),
                    "start_y": float(start_y),
                    "end_x": float(end_x),
                    "end_y": float(end_y),
                    "duration": duration,
                },
                success=True,
            )
        )

    def scroll(self, dx: int = 0, dy: int = 0) -> None:
        """Simulates mouse wheel scroll."""
        self.check_failsafe()

        if self.raise_on_action:
            raise self.raise_on_action

        self._history.append(
            ActuatedAction(
                action_type="scroll",
                parameters={"dx": dx, "dy": dy},
                success=True,
            )
        )

    def type_text(self, text: str, interval: float = 0.02) -> None:
        """Simulates keyboard character entry."""
        self.check_failsafe()

        if self.raise_on_action:
            raise self.raise_on_action

        if text:
            self._typed_buffer.append(text)
        self._history.append(
            ActuatedAction(
                action_type="type_text",
                parameters={"text": text, "interval": interval},
                success=True,
            )
        )

    def paste_text(self, text: str) -> None:
        """Simulates instant clipboard text paste and synthetic Cmd+V."""
        self.check_failsafe()

        if self.raise_on_action:
            raise self.raise_on_action

        self._clipboard = text
        if text:
            self._typed_buffer.append(text)
        self._history.append(
            ActuatedAction(
                action_type="paste_text",
                parameters={"text": text},
                success=True,
            )
        )
        self.press_hotkey("cmd", "v")

    def press_hotkey(self, *keys: str) -> None:
        """Simulates pressing and releasing hotkey combination."""
        self.check_failsafe()

        if self.raise_on_action:
            raise self.raise_on_action

        lowered = [k.lower().strip() for k in keys]
        for k in lowered:
            if k in ("cmd", "command", "shift", "alt", "option", "ctrl", "control"):
                self._active_modifiers.add(k)

        self._history.append(
            ActuatedAction(
                action_type="press_hotkey",
                parameters={"keys": tuple(lowered), "raw_keys": list(keys), "normalized_keys": tuple(lowered)},
                success=True,
            )
        )
        # Release synthetic modifiers
        self._active_modifiers.clear()

    def stop(self) -> None:
        """Halts mock actuator and records stop event."""
        self._active_modifiers.clear()
        self._history.append(
            ActuatedAction(
                action_type="stop",
                parameters={},
                success=True,
            )
        )

    # =========================================================================
    # Mock State Configuration Helpers
    # =========================================================================

    def add_window(self, window: WindowInfo) -> None:
        """Registers a virtual window."""
        self._windows.append(window)

    def add_virtual_window(self, window: WindowInfo) -> None:
        """Alias for add_window."""
        self.add_window(window)

    def clear_windows(self) -> None:
        """Removes all virtual windows."""
        self._windows.clear()

    def clear_virtual_windows(self) -> None:
        """Alias for clear_windows."""
        self.clear_windows()

    def set_screen_size(self, width: float, height: float) -> None:
        """Updates virtual display dimensions."""
        self._screen_size = (float(width), float(height))

    def configure_launch_failure(self, bundle_or_name: str) -> None:
        """Configures specified application to fail when launched."""
        self._app_launch_should_fail.add(bundle_or_name)

    def set_app_launch_failure(self, bundle_id: str, should_fail: bool = True) -> None:
        """Alias for configure_launch_failure."""
        if should_fail:
            self._app_launch_should_fail.add(bundle_id)
        else:
            self._app_launch_should_fail.discard(bundle_id)

    def clear_history(self) -> None:
        """Clears action audit history log and typed buffer."""
        self._history.clear()
        self._typed_buffer.clear()

    def get_history(self, action_type: Optional[str] = None) -> List[ActuatedAction]:
        """Returns action history, optionally filtered by action_type."""
        if action_type is None:
            return list(self._history)
        return [a for a in self._history if a.action_type == action_type]

    # =========================================================================
    # Assertion Helpers
    # =========================================================================

    def assert_action_called(
        self,
        action_type: str,
        count: Optional[int] = None,
        **kwargs: Any,
    ) -> List[ActuatedAction]:
        """Asserts that an action of specified type occurred with matching parameters.

        Args:
            action_type: Action name (e.g. 'click', 'launch_app').
            count: If specified, exact expected number of occurrences.
            **kwargs: Subset of parameters to match.

        Returns:
            List of matching ActuatedAction records.

        Raises:
            AssertionError: If conditions are not satisfied.
        """
        matches = [a for a in self._history if a.action_type == action_type]

        if kwargs:
            filtered = []
            for action in matches:
                match = True
                for k, v in kwargs.items():
                    if action.parameters.get(k) != v:
                        match = False
                        break
                if match:
                    filtered.append(action)
            matches = filtered

        if count is not None:
            assert len(matches) == count, (
                f"Expected exactly {count} '{action_type}' actions matching {kwargs}, "
                f"found {len(matches)}. History: {self._history}"
            )
        else:
            assert len(matches) > 0, (
                f"Expected at least one '{action_type}' action matching {kwargs}, "
                f"found none. History: {self._history}"
            )
        return matches

    def assert_app_launched(self, bundle_or_name: str, count: Optional[int] = None) -> None:
        """Asserts that specified application was launched."""
        matches = [
            a for a in self._history
            if a.action_type == "launch_app"
            and (a.parameters.get("app") == bundle_or_name or a.parameters.get("bundle_id") == bundle_or_name)
        ]
        if count is not None:
            assert len(matches) == count, (
                f"Expected {count} launches for {bundle_or_name}, found {len(matches)}."
            )
        else:
            assert len(matches) > 0, f"App {bundle_or_name} was never launched."

    def assert_app_focused(self, bundle_or_name: str, count: Optional[int] = None) -> None:
        """Asserts that specified application was focused."""
        matches = [
            a for a in self._history
            if a.action_type == "focus_app"
            and (a.parameters.get("app") == bundle_or_name or a.parameters.get("bundle_id") == bundle_or_name)
        ]
        if count is not None:
            assert len(matches) == count, (
                f"Expected {count} focuses for {bundle_or_name}, found {len(matches)}."
            )
        else:
            assert len(matches) > 0, f"App {bundle_or_name} was never focused."

    def assert_hotkey_pressed(self, *keys: str, count: Optional[int] = None) -> None:
        """Asserts that specified hotkey combination was pressed."""
        expected_norm = tuple(k.lower().strip() for k in keys)
        expected_list = list(keys)
        matches = []
        for a in self._history:
            if a.action_type == "press_hotkey":
                recorded_keys = a.parameters.get("keys", [])
                norm_keys = a.parameters.get("normalized_keys")
                if norm_keys == expected_norm or recorded_keys == expected_list:
                    matches.append(a)
                elif [k.lower().strip() for k in recorded_keys] == list(expected_norm):
                    matches.append(a)

        if count is not None:
            assert len(matches) == count, (
                f"Expected hotkey {keys} pressed {count} times, found {len(matches)}."
            )
        else:
            assert len(matches) > 0, (
                f"Hotkey {keys} was never pressed. Recorded hotkeys: {[a.parameters for a in self._history if a.action_type == 'press_hotkey']}"
            )

    def assert_text_typed(
        self,
        substring: str,
        exact: bool = False,
        count: Optional[int] = None,
    ) -> None:
        """Asserts that text matching substring was typed."""
        matches = [a for a in self._history if a.action_type == "type_text"]
        if exact:
            matched = [a for a in matches if a.parameters.get("text") == substring]
        else:
            # Check both individual action text and total typed buffer
            matched = [a for a in matches if substring in a.parameters.get("text", "")]
            if not matched and substring in self.typed_text:
                matched = matches

        if count is not None:
            assert len(matched) == count, (
                f"Expected {count} typed text actions matching {substring!r} (exact={exact}), "
                f"found {len(matched)}."
            )
        else:
            assert len(matched) > 0 or substring in self.typed_text, (
                f"Expected typed text matching {substring!r} (exact={exact}), found none. Total typed: {self.typed_text!r}"
            )

    def assert_text_pasted(
        self,
        substring: str,
        exact: bool = False,
        count: Optional[int] = None,
    ) -> None:
        """Asserts that text matching substring was injected via clipboard."""
        matches = [a for a in self._history if a.action_type == "paste_text"]
        if exact:
            matched = [a for a in matches if a.parameters.get("text") == substring]
        else:
            matched = [a for a in matches if substring in a.parameters.get("text", "")]

        if count is not None:
            assert len(matched) == count, (
                f"Expected {count} pasted text actions matching {substring!r} (exact={exact}), "
                f"found {len(matched)}."
            )
        else:
            assert len(matched) > 0, (
                f"Expected pasted text matching {substring!r} (exact={exact}), found none."
            )

    def assert_clicked_at(
        self,
        x: float,
        y: float,
        tolerance: float = 1.0,
        button: Optional[MouseButton] = None,
        count: Optional[int] = None,
    ) -> None:
        """Asserts that a mouse click occurred near (x, y) within tolerance."""
        matches = [a for a in self._history if a.action_type == "click"]
        matched = []
        for a in matches:
            cx = a.parameters.get("x", -9999.0)
            cy = a.parameters.get("y", -9999.0)
            if abs(cx - x) <= tolerance and abs(cy - y) <= tolerance:
                btn_val = normalize_mouse_button(button) if button is not None else None
                if btn_val is None or a.parameters.get("button") == btn_val:
                    matched.append(a)

        btn_str = f" with button={button}" if button else ""
        if count is not None:
            assert len(matched) == count, (
                f"Expected {count} clicks at ({x}, {y}) +/- {tolerance}{btn_str}, found {len(matched)}."
            )
        else:
            assert len(matched) > 0, (
                f"Expected click at ({x}, {y}) +/- {tolerance}{btn_str}, found none."
            )

    def assert_modifiers_released(self) -> None:
        """Asserts that no sticky synthetic modifier keys remain held down."""
        assert len(self._active_modifiers) == 0, f"Sticky modifiers detected: {self._active_modifiers}"

    def assert_no_errors(self) -> None:
        """Asserts that every executed action completed successfully."""
        errors = [a for a in self._history if not a.success]
        assert len(errors) == 0, f"Found failed actions in history: {errors}"
