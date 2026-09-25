"""Abstract Base Class for OS Actuators.

Belongs to FEAT-ACT-01 (BaseActuatorInterface).
Unified contract for Live macOS Actuator (FEAT-ACT-02) and Mock Actuator (FEAT-ACT-09).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional, Tuple

from src.actuators.types import (
    ActuatorError,
    FailsafeEmergencyStop,
    MouseButton,
    WindowInfo,
)


class BaseActuator(ABC):
    """Abstract interface defining the desktop automation contract."""

    def __enter__(self) -> BaseActuator:
        """Context manager support ensuring clean stop and resource cleanup."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Stops actuator and releases synthetic states upon context exit."""
        self.stop()

    @abstractmethod
    def launch_app(self, app_name_or_bundle: str, timeout: float = 5.0, background: bool = False) -> bool:
        """Launches an application and waits for it to become frontmost/ready.

        Args:
            app_name_or_bundle: Bundle identifier (e.g. 'com.apple.Notes') or app name (e.g. 'Notes').
            timeout: Maximum seconds to wait for launch and window visibility.
            background: If True, launches app in background without bringing it to the front.

        Returns:
            True if application launched and is ready.

        Raises:
            ApplicationLaunchError: If launch fails or exceeds timeout.
            FailsafeEmergencyStop: If emergency stop is triggered.
        """
        pass

    @abstractmethod
    def focus_app(self, app_name_or_bundle: str) -> bool:
        """Brings an already running application to the foreground.

        Args:
            app_name_or_bundle: Bundle identifier or app name.

        Returns:
            True if focus succeeded.

        Raises:
            ApplicationLaunchError: If app is not running or cannot be focused.
            FailsafeEmergencyStop: If emergency stop is triggered.
        """
        pass

    def open_url(self, url: str) -> bool:
        """Opens a URL in a browser or frontmost application."""
        return True

    @abstractmethod
    def get_frontmost_app(self) -> str:
        """Returns the bundle ID or localized name of the currently active application.

        Returns:
            String representing frontmost app bundle ID or localized name.
        """
        pass

    @abstractmethod
    def get_windows(self, app_name: Optional[str] = None) -> List[WindowInfo]:
        """Returns on-screen visible windows, optionally filtered by application name.

        Args:
            app_name: Optional application name or bundle ID filter.

        Returns:
            List of WindowInfo objects with coordinates and dimensions.
        """
        pass

    @abstractmethod
    def get_screen_size(self) -> Tuple[float, float]:
        """Returns (width, height) of the primary display in points/pixels."""
        pass

    @abstractmethod
    def get_mouse_position(self) -> Tuple[float, float]:
        """Returns the current (x, y) coordinates of the mouse cursor."""
        pass

    @abstractmethod
    def move_mouse(
        self,
        x: float,
        y: float,
        smooth: bool = False,
        duration: float = 0.0,
    ) -> None:
        """Moves mouse cursor to target coordinates.

        Args:
            x: Target horizontal coordinate.
            y: Target vertical coordinate.
            smooth: If True, interpolates movement smoothly over duration.
            duration: Time in seconds for smooth movement.

        Raises:
            FailsafeEmergencyStop: If cursor enters screen corner failsafe.
        """
        pass

    @abstractmethod
    def click(
        self,
        x: Optional[float] = None,
        y: Optional[float] = None,
        button: MouseButton = MouseButton.LEFT,
        click_count: int = 1,
    ) -> None:
        """Performs a mouse click at specified coordinates or current cursor position.

        Args:
            x: Optional horizontal coordinate. If omitted, clicks at current position.
            y: Optional vertical coordinate. If omitted, clicks at current position.
            button: MouseButton enum value (LEFT, RIGHT, CENTER).
            click_count: Number of clicks (1 = single, 2 = double, 3 = triple).

        Raises:
            FailsafeEmergencyStop: If emergency stop is triggered.
        """
        pass

    def double_click(
        self,
        x: Optional[float] = None,
        y: Optional[float] = None,
        button: MouseButton = MouseButton.LEFT,
    ) -> None:
        """Convenience method for double-clicking."""
        self.click(x=x, y=y, button=button, click_count=2)

    def right_click(
        self,
        x: Optional[float] = None,
        y: Optional[float] = None,
    ) -> None:
        """Convenience method for right-clicking."""
        self.click(x=x, y=y, button=MouseButton.RIGHT, click_count=1)

    @abstractmethod
    def drag(
        self,
        start_x: float,
        start_y: float,
        end_x: float,
        end_y: float,
        duration: float = 0.3,
    ) -> None:
        """Drags mouse from start coordinates to end coordinates.

        Args:
            start_x: Starting horizontal coordinate.
            start_y: Starting vertical coordinate.
            end_x: Ending horizontal coordinate.
            end_y: Ending vertical coordinate.
            duration: Total duration of drag movement in seconds.

        Raises:
            FailsafeEmergencyStop: If emergency stop is triggered.
        """
        pass

    @abstractmethod
    def scroll(self, dx: int = 0, dy: int = 0) -> None:
        """Scrolls the mouse wheel horizontally or vertically.

        Args:
            dx: Horizontal scroll delta (positive = right, negative = left).
            dy: Vertical scroll delta (positive = up / backward, negative = down / forward).

        Raises:
            FailsafeEmergencyStop: If emergency stop is triggered.
        """
        pass

    @abstractmethod
    def type_text(self, text: str, interval: float = 0.02) -> None:
        """Synthesizes keyboard entry for a text string with character delay.

        Args:
            text: Text to type (supports full Unicode, accents, emojis).
            interval: Delay in seconds between successive keystrokes.

        Raises:
            FailsafeEmergencyStop: If emergency stop is triggered.
        """
        pass

    @abstractmethod
    def paste_text(self, text: str) -> None:
        """Instantly injects multi-line text via clipboard paste accelerator.

        Sets system clipboard content and synthesizes Cmd+V.

        Args:
            text: Arbitrary string content to paste.

        Raises:
            FailsafeEmergencyStop: If emergency stop is triggered.
        """
        pass

    @abstractmethod
    def press_hotkey(self, *keys: str) -> None:
        """Synthesizes key combination with modifiers (e.g. 'cmd', 'n' or 'shift', 'cmd', '4').

        Args:
            *keys: Key names or characters, case-insensitive (e.g. 'cmd', 'n', 'enter').

        Raises:
            FailsafeEmergencyStop: If emergency stop is triggered.
        """
        pass

    def click_window_ratio(
        self,
        window: WindowInfo,
        norm_x: float,
        norm_y: float,
        button: MouseButton = MouseButton.LEFT,
        click_count: int = 1,
    ) -> None:
        """Clicks at a normalized ratio coordinate relative to a given window.

        Enables resolution-independent task playback across different window sizes.

        Args:
            window: Target WindowInfo metadata.
            norm_x: Horizontal ratio in range [0.0, 1.0].
            norm_y: Vertical ratio in range [0.0, 1.0].
            button: MouseButton to click with.
            click_count: Number of clicks.
        """
        screen_x, screen_y = window.ratio_to_screen(norm_x, norm_y)
        self.click(x=screen_x, y=screen_y, button=button, click_count=click_count)

    @abstractmethod
    def is_failsafe_triggered(self) -> bool:
        """Checks if the failsafe condition is currently met.

        Returns:
            True if emergency stop is triggered (mouse in corner or flag set).
        """
        pass

    @abstractmethod
    def check_failsafe(self) -> None:
        """Checks failsafe condition and raises FailsafeEmergencyStop if triggered.

        Raises:
            FailsafeEmergencyStop: If failsafe condition is active.
        """
        pass

    @abstractmethod
    def stop(self) -> None:
        """Halts all synthetic actions, releases held modifier keys, and cleans up resources."""
        pass
