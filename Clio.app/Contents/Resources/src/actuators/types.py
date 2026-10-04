"""Type definitions, enums, data structures, and exceptions for actuators.

Belongs to FEAT-ACT-01 (BaseActuatorInterface).
Zero external dependencies: pure Python standard library.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import time
from typing import Any, Dict, List, Optional, Tuple


class ActuatorError(Exception):
    """Base exception for all actuator errors."""
    pass


class FailsafeEmergencyStop(ActuatorError):
    """Raised when the failsafe emergency stop is triggered.

    Triggered when the mouse moves to any screen corner or via an emergency hotkey/trigger.
    """

    def __init__(
        self,
        message: str = "Emergency stop triggered: mouse moved to screen corner",
        position: Optional[Tuple[float, float]] = None,
        trigger_reason: str = "mouse_corner",
        timestamp: Optional[float] = None,
        corner: Optional[str] = None,
        display_index: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.position = position
        self.trigger_reason = trigger_reason
        self.corner = corner
        self.display_index = display_index
        self.timestamp = timestamp if timestamp is not None else time.time()

    def __str__(self) -> str:
        pos_str = f" at {self.position}" if self.position is not None else ""
        corner_str = f" [{self.corner}]" if self.corner else ""
        return f"{self.message}{corner_str}{pos_str} (reason: {self.trigger_reason})"


class ApplicationLaunchError(ActuatorError):
    """Raised when an application fails to launch or focus within timeout."""
    pass


class WindowNotFoundError(ActuatorError):
    """Raised when a target application window is not found or visible."""
    pass


class InputSynthesisError(ActuatorError):
    """Raised when synthesizing an input event fails at the OS level."""
    pass


class MouseButton(str, Enum):
    """Supported mouse buttons."""
    LEFT = "left"
    RIGHT = "right"
    CENTER = "center"
    MIDDLE = "center"  # Alias for CENTER


class CornerLocation(str, Enum):
    """Screen corner locations for failsafe triggering."""
    TOP_LEFT = "top_left"
    TOP_RIGHT = "top_right"
    BOTTOM_LEFT = "bottom_left"
    BOTTOM_RIGHT = "bottom_right"


class ActuatorMode(str, Enum):
    """Operating modes for ActuatorFactory."""
    AUTO = "auto"
    MOCK = "mock"
    MACOS = "macos"


@dataclass(frozen=True)
class WindowInfo:
    """Represents geometry and metadata of an on-screen application window.

    All coordinates and dimensions are in display points / pixels.
    """
    window_id: int
    owner_name: str
    title: str
    x: float
    y: float
    width: float
    height: float
    layer: int = 0
    is_on_screen: bool = True

    @property
    def bounds(self) -> Tuple[float, float, float, float]:
        """Returns (x, y, width, height) tuple."""
        return (self.x, self.y, self.width, self.height)

    @property
    def center(self) -> Tuple[float, float]:
        """Returns the (center_x, center_y) coordinates of the window."""
        return (self.x + self.width / 2.0, self.y + self.height / 2.0)

    def contains_point(self, px: float, py: float) -> bool:
        """Checks if the given point is inside the window bounds."""
        return (self.x <= px <= self.x + self.width) and (self.y <= py <= self.y + self.height)

    def ratio_to_screen(self, norm_x: float, norm_y: float) -> Tuple[float, float]:
        """Converts normalized ratios (0.0 to 1.0) into absolute screen coordinates.

        Args:
            norm_x: Normalized horizontal ratio (0.0 = left edge, 1.0 = right edge).
            norm_y: Normalized vertical ratio (0.0 = top edge, 1.0 = bottom edge).

        Returns:
            Absolute screen coordinates (screen_x, screen_y).
        """
        screen_x = self.x + (norm_x * self.width)
        screen_y = self.y + (norm_y * self.height)
        return (screen_x, screen_y)


@dataclass
class ActuatedAction:
    """Represents an executed or simulated action in the actuator audit history."""
    action_type: str
    parameters: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    success: bool = True
    error: Optional[str] = None


@dataclass(frozen=True)
class FailsafeTriggerInfo:
    """Immutable payload delivered to failsafe emergency callbacks."""
    corner: str
    position: Tuple[float, float]
    display_index: int = 0
    display_bounds: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    timestamp: float = field(default_factory=time.time)
    reason: str = "Screen corner detected"


def is_screen_corner(
    x: float,
    y: float,
    screen_width: float,
    screen_height: float,
    margin: float = 5.0,
) -> bool:
    """Evaluates whether cursor position (x, y) is within margin pixels of any screen corner.

    Handles coordinates on primary or relative display bounds.

    Args:
        x: Cursor horizontal position.
        y: Cursor vertical position.
        screen_width: Width of display in points.
        screen_height: Height of display in points.
        margin: Corner threshold margin in points (default: 5.0).

    Returns:
        True if cursor is in any corner, False otherwise.
    """
    in_left = x <= margin
    in_right = x >= (screen_width - margin)
    in_top = y <= margin
    in_bottom = y >= (screen_height - margin)
    return (in_left or in_right) and (in_top or in_bottom)


def get_screen_corner(
    x: float,
    y: float,
    screen_width: float,
    screen_height: float,
    margin: float = 5.0,
) -> Optional[CornerLocation]:
    """Identifies the specific screen corner for a given cursor position.

    Args:
        x: Cursor horizontal position.
        y: Cursor vertical position.
        screen_width: Width of display in points.
        screen_height: Height of display in points.
        margin: Corner threshold margin in points (default: 5.0).

    Returns:
        CornerLocation enum if in a corner, None otherwise.
    """
    in_left = x <= margin
    in_right = x >= (screen_width - margin)
    in_top = y <= margin
    in_bottom = y >= (screen_height - margin)

    if in_left and in_top:
        return CornerLocation.TOP_LEFT
    if in_right and in_top:
        return CornerLocation.TOP_RIGHT
    if in_left and in_bottom:
        return CornerLocation.BOTTOM_LEFT
    if in_right and in_bottom:
        return CornerLocation.BOTTOM_RIGHT
    return None


def normalize_mouse_button(button: Any) -> str:
    """Normalizes any MouseButton enum, foreign enum, or string to 'left', 'right', or 'center'."""
    val = getattr(button, "value", button)
    val_str = str(val).lower().strip()
    if "." in val_str:
        val_str = val_str.split(".")[-1]
    if val_str in ("center", "middle"):
        return "center"
    return val_str

