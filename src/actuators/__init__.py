"""Actuators subsystem: OS automation primitives, failsafe watchdog, and mock testing.

Belongs to Milestone M1 (Actuator Primitives & OS Control).
"""

from __future__ import annotations

from src.actuators.base import BaseActuator
from src.actuators.factory import ActuatorFactory, get_actuator
from src.actuators.failsafe import FailsafeCornerWatchdog, FailsafeWatchdog
from src.actuators.mock import MockActuator
from src.actuators.types import (
    ActuatedAction,
    ActuatorError,
    ActuatorMode,
    ApplicationLaunchError,
    CornerLocation,
    FailsafeEmergencyStop,
    FailsafeTriggerInfo,
    InputSynthesisError,
    MouseButton,
    WindowInfo,
    WindowNotFoundError,
    get_screen_corner,
    is_screen_corner,
)
from src.actuators.virtual_cursor import (
    VirtualCursor,
    VirtualCursorEvent,
    VirtualCursorState,
)

# Conditionally import MacOSActuator to prevent errors on non-Darwin platforms
try:
    from src.actuators.macos import MacOSActuator
except (ImportError, OSError):
    MacOSActuator = None  # type: ignore

__all__ = [
    "BaseActuator",
    "MacOSActuator",
    "MockActuator",
    "FailsafeWatchdog",
    "FailsafeCornerWatchdog",
    "ActuatorFactory",
    "get_actuator",
    "WindowInfo",
    "MouseButton",
    "ActuatedAction",
    "CornerLocation",
    "ActuatorMode",
    "FailsafeTriggerInfo",
    "ActuatorError",
    "FailsafeEmergencyStop",
    "ApplicationLaunchError",
    "WindowNotFoundError",
    "InputSynthesisError",
    "is_screen_corner",
    "get_screen_corner",
    "VirtualCursor",
    "VirtualCursorState",
    "VirtualCursorEvent",
]

