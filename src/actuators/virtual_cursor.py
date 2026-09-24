"""FEAT-ACT-11: Independent Companion Virtual Cursor Subsystem.

Path: src/actuators/virtual_cursor.py
Belongs to Milestone M3 (Autonomous Closed-Loop Executor & Virtual Cursor)
and Requirement R7 (Independent Companion Virtual Cursor).

Key Architecture & Invariants:
1. Zero Physical Cursor Displacement (Requirement R7):
   The virtual cursor operates completely independently of the user's hardware mouse.
   It NEVER calls CGWarpMouseCursorPosition, nor does it post to kCGHIDEventTap.
   All targeted OS interactions dispatch directly to specific target process IDs
   via CGEventPostToPid (macOS) or are recorded by the in-memory mock collector.
2. Coordinates & State Tracking:
   Maintains floating-point coordinates (vx, vy), VirtualCursorState enum,
   instantaneous velocity vector (vx_vel, vy_vel), and scalar speed.
3. Simulation & Mocking:
   When mock=True or running in CI/headless environments, executes deterministically
   without requiring live display hardware or macOS window server access.
4. Telemetry & Observers:
   Emits typed VirtualCursorEvent records to subscribed listeners (e.g. companion HUD,
   commentary engine, execution event bus, test assertions).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import logging
import math
import os
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from src.actuators.types import (
    ActuatorError,
    InputSynthesisError,
    MouseButton,
    WindowInfo,
    normalize_mouse_button,
)

logger = logging.getLogger(__name__)


# =============================================================================
# 1. Virtual Cursor State Machine Enum
# =============================================================================

class VirtualCursorState(str, Enum):
    """Lifecycle states of the independent companion virtual cursor."""
    IDLE = "IDLE"
    MOVING = "MOVING"
    HOVERING = "HOVERING"
    CLICKING = "CLICKING"
    DRAGGING = "DRAGGING"


# =============================================================================
# 2. Virtual Cursor Event Telemetry Model
# =============================================================================

@dataclass
class VirtualCursorEvent:
    """Represents a virtual cursor action, state change, or event dispatch."""
    event_type: str  # "move", "mouse_down", "mouse_up", "click", "drag", "hover", "scroll"
    x: float
    y: float
    timestamp: float = field(default_factory=time.time)
    previous_position: Optional[Tuple[float, float]] = None
    state: VirtualCursorState = VirtualCursorState.IDLE
    button: Optional[str] = None
    click_count: int = 0
    target_pid: Optional[int] = None
    target_window_id: Optional[int] = None
    duration: float = 0.0
    smooth: bool = False
    speed: float = 0.0
    velocity: Tuple[float, float] = (0.0, 0.0)
    extra: Dict[str, Any] = field(default_factory=dict)


# =============================================================================
# 3. macOS Native Bindings for Targeted Event Dispatch (CGEventPostToPid)
# =============================================================================

class _VirtualCursorNativeBindings:
    """Minimal, robust ctypes bindings for macOS CoreGraphics targeted dispatch.

    STRICT INVARIANT:
    This class NEVER loads or calls CGWarpMouseCursorPosition or CGEventPost(kCGHIDEventTap).
    It ONLY uses CGEventPostToPid for non-displacing, process-targeted dispatch.
    """

    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise RuntimeError(f"MacOS native bindings not supported on {sys.platform}")

        import ctypes
        from ctypes import (
            POINTER,
            Structure,
            byref,
            c_bool,
            c_char_p,
            c_double,
            c_int,
            c_int32,
            c_int64,
            c_uint16,
            c_uint32,
            c_uint64,
            c_void_p,
        )

        self.ctypes = ctypes

        class CGPoint(Structure):
            _fields_ = [("x", c_double), ("y", c_double)]

        class CGSize(Structure):
            _fields_ = [("width", c_double), ("height", c_double)]

        class CGRect(Structure):
            _fields_ = [("origin", CGPoint), ("size", CGSize)]

        self.CGPoint = CGPoint
        self.CGSize = CGSize
        self.CGRect = CGRect

        try:
            self.cg = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
            self.cf = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        except OSError as e:
            raise RuntimeError(f"Failed to load macOS system frameworks: {e}") from e

        # CoreGraphics function prototypes
        cg = self.cg
        cg.CGEventCreateMouseEvent.argtypes = [c_void_p, c_uint32, CGPoint, c_uint32]
        cg.CGEventCreateMouseEvent.restype = c_void_p

        cg.CGEventCreateScrollWheelEvent2.argtypes = [c_void_p, c_uint32, c_uint32, c_int32, c_int32, c_int32]
        cg.CGEventCreateScrollWheelEvent2.restype = c_void_p

        cg.CGEventSetIntegerValueField.argtypes = [c_void_p, c_uint32, c_int64]
        cg.CGEventSetIntegerValueField.restype = None

        # Targeted keyboard and modifier prototypes
        cg.CGEventCreateKeyboardEvent.argtypes = [c_void_p, c_uint16, c_bool]
        cg.CGEventCreateKeyboardEvent.restype = c_void_p

        cg.CGEventKeyboardSetUnicodeString.argtypes = [c_void_p, c_uint32, POINTER(c_uint16)]
        cg.CGEventKeyboardSetUnicodeString.restype = None

        cg.CGEventSetFlags.argtypes = [c_void_p, c_uint64]
        cg.CGEventSetFlags.restype = None

        # CRITICAL: Targeted process dispatch only!
        cg.CGEventPostToPid.argtypes = [c_int32, c_void_p]
        cg.CGEventPostToPid.restype = None

        # Window info inspection (for PID resolution from window ID)
        cg.CGWindowListCopyWindowInfo.argtypes = [c_uint32, c_uint32]
        cg.CGWindowListCopyWindowInfo.restype = c_void_p

        # CoreFoundation function prototypes
        cf = self.cf
        cf.CFRelease.argtypes = [c_void_p]
        cf.CFRelease.restype = None

        cf.CFArrayGetCount.argtypes = [c_void_p]
        cf.CFArrayGetCount.restype = c_int

        cf.CFArrayGetValueAtIndex.argtypes = [c_void_p, c_int]
        cf.CFArrayGetValueAtIndex.restype = c_void_p

        cf.CFDictionaryGetValue.argtypes = [c_void_p, c_void_p]
        cf.CFDictionaryGetValue.restype = c_void_p

        cf.CFStringCreateWithCString.argtypes = [c_void_p, c_char_p, c_uint32]
        cf.CFStringCreateWithCString.restype = c_void_p

        cf.CFNumberGetValue.argtypes = [c_void_p, c_int, c_void_p]
        cf.CFNumberGetValue.restype = c_bool

        # Cached CFString keys
        kCFStringEncodingUTF8 = 0x08000100
        self.kCFNumberSInt64Type = 4
        self.key_pid = cf.CFStringCreateWithCString(None, b"kCGWindowOwnerPID", kCFStringEncodingUTF8)
        self.key_num = cf.CFStringCreateWithCString(None, b"kCGWindowNumber", kCFStringEncodingUTF8)

        # CGEvent event type constants
        self.kCGEventLeftMouseDown = 1
        self.kCGEventLeftMouseUp = 2
        self.kCGEventRightMouseDown = 3
        self.kCGEventRightMouseUp = 4
        self.kCGEventMouseMoved = 5
        self.kCGEventLeftMouseDragged = 6
        self.kCGEventRightMouseDragged = 7
        self.kCGEventOtherMouseDown = 25
        self.kCGEventOtherMouseUp = 26
        self.kCGEventOtherMouseDragged = 27
        self.kCGMouseEventClickState = 1

        self.kCGMouseButtonLeft = 0
        self.kCGMouseButtonRight = 1
        self.kCGMouseButtonCenter = 2

        self.kCGEventFlagMaskShift = 0x00020000
        self.kCGEventFlagMaskControl = 0x00040000
        self.kCGEventFlagMaskAlternate = 0x00080000
        self.kCGEventFlagMaskCommand = 0x00100000

    def resolve_pid_from_bundle_id(self, bundle_id: str) -> Optional[int]:
        """Resolves target process PID from a macOS application bundle ID or name."""
        import subprocess
        clean = bundle_id.split(".")[-1] if "." in bundle_id else bundle_id
        try:
            res = subprocess.run(["pgrep", "-x", clean], capture_output=True, text=True, check=False)
            if res.returncode == 0 and res.stdout.strip():
                pids = [int(p) for p in res.stdout.strip().splitlines() if p.isdigit()]
                if pids:
                    return pids[0]
            # Fallback to general pgrep
            res = subprocess.run(["pgrep", "-f", clean], capture_output=True, text=True, check=False)
            if res.returncode == 0 and res.stdout.strip():
                pids = [int(p) for p in res.stdout.strip().splitlines() if p.isdigit()]
                if pids:
                    return pids[0]
        except Exception as e:
            logger.debug("pgrep pid resolution failed: %s", e)
        return None

    def resolve_frontmost_pid(self) -> Optional[int]:
        """Resolves the PID of the current frontmost application on macOS."""
        import re
        import subprocess
        try:
            out = subprocess.check_output(
                "lsappinfo info -bundleid $(lsappinfo front)",
                shell=True,
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=1.0,
            )
            m = re.search(r'\bpid\s*=\s*(\d+)', out)
            if m:
                return int(m.group(1))
        except Exception:
            pass
        return None


    def resolve_pid_from_window_id(self, window_id: int) -> Optional[int]:
        """Resolves target process PID from a macOS window ID."""
        from ctypes import byref, c_int64
        wlist = self.cg.CGWindowListCopyWindowInfo(1, 0)
        if not wlist:
            return None
        try:
            count = self.cf.CFArrayGetCount(wlist)
            for i in range(count):
                d = self.cf.CFArrayGetValueAtIndex(wlist, i)
                num_ref = self.cf.CFDictionaryGetValue(d, self.key_num)
                if num_ref:
                    wnum = c_int64(0)
                    self.cf.CFNumberGetValue(num_ref, self.kCFNumberSInt64Type, byref(wnum))
                    if wnum.value == window_id:
                        pid_ref = self.cf.CFDictionaryGetValue(d, self.key_pid)
                        if pid_ref:
                            pid_val = c_int64(0)
                            self.cf.CFNumberGetValue(pid_ref, self.kCFNumberSInt64Type, byref(pid_val))
                            return int(pid_val.value)
            return None
        finally:
            self.cf.CFRelease(wlist)


# Lazy singleton instance of native bindings
_NATIVE_BINDINGS: Optional[_VirtualCursorNativeBindings] = None
_NATIVE_INIT_ATTEMPTED = False


def _get_native_bindings() -> Optional[_VirtualCursorNativeBindings]:
    global _NATIVE_BINDINGS, _NATIVE_INIT_ATTEMPTED
    if not _NATIVE_INIT_ATTEMPTED:
        _NATIVE_INIT_ATTEMPTED = True
        if sys.platform == "darwin":
            try:
                _NATIVE_BINDINGS = _VirtualCursorNativeBindings()
            except Exception as exc:
                logger.warning(f"Failed to initialize macOS native virtual cursor bindings: {exc}")
                _NATIVE_BINDINGS = None
    return _NATIVE_BINDINGS


# =============================================================================
# 4. VirtualCursor Main Class
# =============================================================================

class VirtualCursor:
    """Independent companion virtual cursor with process-targeted event dispatch.

    Complies strictly with Requirement R7:
    Zero physical cursor displacement. Never displaces hardware mouse.
    """

    def __init__(
        self,
        initial_x: float = 0.0,
        initial_y: float = 0.0,
        target_pid: Optional[int] = None,
        target_window_id: Optional[int] = None,
        mock: Optional[bool] = None,
        name: str = "companion_cursor",
    ) -> None:
        """Initializes the virtual cursor.

        Args:
            initial_x: Starting horizontal coordinate (points).
            initial_y: Starting vertical coordinate (points).
            target_pid: Target process ID for directed event dispatch.
            target_window_id: Target window ID.
            mock: If True, operates in simulation mode without macOS window server.
                  If None, auto-detected: True in CI or non-macOS or when bindings fail.
            name: Identifier for this virtual cursor instance.
        """
        self.name: str = name
        self._vx: float = float(initial_x)
        self._vy: float = float(initial_y)
        self._state: VirtualCursorState = VirtualCursorState.IDLE
        self._velocity: Tuple[float, float] = (0.0, 0.0)
        self._speed: float = 0.0
        self.target_pid: Optional[int] = target_pid
        self.target_window_id: Optional[int] = target_window_id
        self.background_mode: bool = False

        # Determine mock mode
        if mock is not None:
            self._mock: bool = bool(mock)
        else:
            ci_env = os.environ.get("CI", "").strip().lower() in ("true", "1", "yes")
            native = _get_native_bindings() if sys.platform == "darwin" else None
            self._mock = ci_env or (sys.platform != "darwin") or (native is None)

        self._history: List[VirtualCursorEvent] = []
        self._trajectory: List[Tuple[float, float, float]] = [(self._vx, self._vy, time.time())]
        self._listeners: List[Callable[[VirtualCursorEvent], None]] = []
        self._lock = threading.RLock()

    # -------------------------------------------------------------------------
    # Properties
    # -------------------------------------------------------------------------

    @property
    def vx(self) -> float:
        """Current virtual cursor X coordinate."""
        with self._lock:
            return self._vx

    @vx.setter
    def vx(self, val: float) -> None:
        with self._lock:
            self._vx = float(val)

    @property
    def vy(self) -> float:
        """Current virtual cursor Y coordinate."""
        with self._lock:
            return self._vy

    @vy.setter
    def vy(self, val: float) -> None:
        with self._lock:
            self._vy = float(val)

    @property
    def position(self) -> Tuple[float, float]:
        """Current (vx, vy) coordinate tuple."""
        with self._lock:
            return (self._vx, self._vy)

    @property
    def state(self) -> VirtualCursorState:
        """Current VirtualCursorState."""
        with self._lock:
            return self._state

    @property
    def velocity(self) -> Tuple[float, float]:
        """Instantaneous velocity vector (vx_vel, vy_vel) in points/sec."""
        with self._lock:
            return self._velocity

    @property
    def speed(self) -> float:
        """Current scalar speed in points/sec."""
        with self._lock:
            return self._speed

    @property
    def mock(self) -> bool:
        """Whether cursor runs in pure in-memory mock mode."""
        return self._mock

    @property
    def history(self) -> List[VirtualCursorEvent]:
        """List of all recorded events."""
        with self._lock:
            return list(self._history)

    @property
    def trajectory(self) -> List[Tuple[float, float, float]]:
        """List of (x, y, timestamp) trajectory points recorded during motion."""
        with self._lock:
            return list(self._trajectory)

    # -------------------------------------------------------------------------
    # Target Configuration
    # -------------------------------------------------------------------------

    def set_target(self, pid: Optional[int] = None, window_id: Optional[int] = None) -> None:
        """Sets target process and/or window ID for directed event dispatch.

        Args:
            pid: Process ID of the target application.
            window_id: Window ID of the target window.
        """
        with self._lock:
            if pid is not None:
                self.target_pid = int(pid)
            if window_id is not None:
                self.target_window_id = int(window_id)
                if self.target_pid is None and not self._mock:
                    native = _get_native_bindings()
                    if native:
                        resolved_pid = native.resolve_pid_from_window_id(int(window_id))
                        if resolved_pid:
                            self.target_pid = resolved_pid

    def set_target_window(self, window: WindowInfo) -> None:
        """Sets target window metadata and resolves target PID if available."""
        self.set_target(window_id=window.window_id)

    # -------------------------------------------------------------------------
    # Core Cursor Motion & Interaction Methods
    # -------------------------------------------------------------------------

    def move_to(
        self,
        x: float,
        y: float,
        duration: float = 0.0,
        smooth: bool = False,
        steps: Optional[int] = None,
    ) -> None:
        """Moves virtual cursor to specified coordinates without displacing physical mouse.

        Args:
            x: Target horizontal coordinate.
            y: Target vertical coordinate.
            duration: Time in seconds for movement.
            smooth: If True, uses smoothstep interpolation along trajectory.
            steps: Optional explicit number of interpolation steps.

        Raises:
            InputSynthesisError: If coordinates are non-finite.
        """
        if not (math.isfinite(x) and math.isfinite(y)):
            raise InputSynthesisError(f"Invalid non-finite virtual cursor coordinates: ({x}, {y})")

        with self._lock:
            start_x, start_y = self._vx, self._vy
            prev_pos = (start_x, start_y)
            self._state = VirtualCursorState.MOVING

            dist = math.hypot(x - start_x, y - start_y)

            # Instantaneous movement
            if duration <= 0.0 and not smooth:
                self._vx = float(x)
                self._vy = float(y)
                self._velocity = (0.0, 0.0)
                self._speed = 0.0
                now = time.time()
                self._trajectory.append((self._vx, self._vy, now))

                self._dispatch_native_mouse_event(self._vx, self._vy, event_type="move")
                self._state = VirtualCursorState.IDLE

                event = VirtualCursorEvent(
                    event_type="move",
                    x=self._vx,
                    y=self._vy,
                    timestamp=now,
                    previous_position=prev_pos,
                    state=self._state,
                    target_pid=self.target_pid,
                    target_window_id=self.target_window_id,
                    duration=0.0,
                    smooth=False,
                    speed=0.0,
                    velocity=(0.0, 0.0),
                )
                self._record_event(event)
                return

            # Smooth / Timed movement
            actual_duration = max(0.001, duration)
            step_count = steps if steps is not None else max(5, int(actual_duration * 60))
            step_delay = actual_duration / step_count

            # Average velocity across the entire move
            vx_vel = (x - start_x) / actual_duration
            vy_vel = (y - start_y) / actual_duration
            self._velocity = (vx_vel, vy_vel)
            self._speed = dist / actual_duration

            last_notify = 0.0
            for i in range(1, step_count + 1):
                t = i / step_count
                if smooth:
                    # Hermite smoothstep: 3*t^2 - 2*t^3
                    smooth_t = 3 * (t ** 2) - 2 * (t ** 3)
                else:
                    smooth_t = t

                cx = start_x + (x - start_x) * smooth_t
                cy = start_y + (y - start_y) * smooth_t

                self._vx = float(cx)
                self._vy = float(cy)
                now = time.time()
                self._trajectory.append((self._vx, self._vy, now))

                self._dispatch_native_mouse_event(self._vx, self._vy, event_type="move")

                # Real-time position broadcast at ~40Hz for fluid on-screen cursor motion
                if now - last_notify >= 0.025:
                    last_notify = now
                    inter_event = VirtualCursorEvent(
                        event_type="move",
                        x=self._vx,
                        y=self._vy,
                        timestamp=now,
                        previous_position=prev_pos,
                        state=VirtualCursorState.MOVING,
                        target_pid=self.target_pid,
                        target_window_id=self.target_window_id,
                        duration=actual_duration,
                        smooth=smooth,
                        speed=dist / actual_duration,
                        velocity=(vx_vel, vy_vel),
                    )
                    self._record_event(inter_event)

                if not self._mock and step_delay > 0:
                    time.sleep(step_delay)

            # Ensure final position is exact
            self._vx = float(x)
            self._vy = float(y)
            self._velocity = (0.0, 0.0)
            self._speed = 0.0
            self._state = VirtualCursorState.IDLE

            event = VirtualCursorEvent(
                event_type="move",
                x=self._vx,
                y=self._vy,
                timestamp=time.time(),
                previous_position=prev_pos,
                state=self._state,
                target_pid=self.target_pid,
                target_window_id=self.target_window_id,
                duration=actual_duration,
                smooth=smooth,
                speed=dist / actual_duration,
                velocity=(vx_vel, vy_vel),
            )
            self._record_event(event)

    def click(
        self,
        x: Optional[float] = None,
        y: Optional[float] = None,
        button: Union[str, MouseButton] = "left",
        click_count: int = 1,
        interval: float = 0.02,
    ) -> None:
        """Performs a targeted mouse click without displacing hardware mouse.

        Args:
            x: Optional horizontal coordinate. If omitted, clicks at current vx.
            y: Optional vertical coordinate. If omitted, clicks at current vy.
            button: MouseButton or string ('left', 'right', 'center').
            click_count: Number of clicks (1 = single, 2 = double).
            interval: Delay between down and up states in seconds.

        Raises:
            InputSynthesisError: If button is unsupported or coordinates non-finite.
        """
        btn_str = normalize_mouse_button(button)
        if btn_str not in ("left", "right", "center"):
            raise InputSynthesisError(f"Unsupported mouse button: {button}")

        if x is not None and y is not None:
            dist = math.hypot(x - self._vx, y - self._vy)
            duration = min(0.35, max(0.12, dist / 2000.0)) if dist > 20 else 0.0
            self.move_to(x, y, duration=duration, smooth=True)

        # Capture state under lock, release before ctypes dispatch
        with self._lock:
            cur_x, cur_y = self._vx, self._vy
            self._state = VirtualCursorState.CLICKING
            snap_pid = self.target_pid
            snap_win = self.target_window_id

        for i in range(1, click_count + 1):
            # Mouse Down — outside lock
            self._dispatch_native_button_event(
                cur_x, cur_y, button=btn_str, is_down=True, click_state=i
            )
            down_event = VirtualCursorEvent(
                event_type="mouse_down",
                x=cur_x,
                y=cur_y,
                state=VirtualCursorState.CLICKING,
                button=btn_str,
                click_count=i,
                target_pid=snap_pid,
                target_window_id=snap_win,
            )
            self._record_event(down_event)  # non-blocking

            if not self._mock and interval > 0:
                time.sleep(interval)

            # Mouse Up
            self._dispatch_native_button_event(
                cur_x, cur_y, button=btn_str, is_down=False, click_state=i
            )
            up_event = VirtualCursorEvent(
                event_type="mouse_up",
                x=cur_x,
                y=cur_y,
                state=VirtualCursorState.CLICKING,
                button=btn_str,
                click_count=i,
                target_pid=snap_pid,
                target_window_id=snap_win,
            )
            self._record_event(up_event)  # non-blocking

            if i < click_count and not self._mock:
                time.sleep(0.05)

        with self._lock:
            self._state = VirtualCursorState.IDLE

        click_event = VirtualCursorEvent(
            event_type="click",
            x=cur_x,
            y=cur_y,
            state=VirtualCursorState.IDLE,
            button=btn_str,
            click_count=click_count,
            target_pid=snap_pid,
            target_window_id=snap_win,
        )
        self._record_event(click_event)  # non-blocking


    def double_click(
        self,
        x: Optional[float] = None,
        y: Optional[float] = None,
        button: Union[str, MouseButton] = "left",
    ) -> None:
        """Convenience method for double clicking."""
        self.click(x=x, y=y, button=button, click_count=2)

    def right_click(
        self,
        x: Optional[float] = None,
        y: Optional[float] = None,
    ) -> None:
        """Convenience method for right clicking."""
        self.click(x=x, y=y, button="right", click_count=1)

    def drag_to(
        self,
        x: float,
        y: float,
        duration: float = 0.0,
        button: Union[str, MouseButton] = "left",
        steps: Optional[int] = None,
    ) -> None:
        """Performs a drag gesture from current (vx, vy) to (x, y).

        Args:
            x: Target horizontal coordinate.
            y: Target vertical coordinate.
            duration: Time in seconds for drag operation.
            button: MouseButton or string ('left', 'right', 'center').
            steps: Optional explicit interpolation steps.

        Raises:
            InputSynthesisError: If coordinates are non-finite.
        """
        if not (math.isfinite(x) and math.isfinite(y)):
            raise InputSynthesisError(f"Invalid non-finite drag coordinates: ({x}, {y})")

        btn_str = normalize_mouse_button(button)
        if btn_str not in ("left", "right", "center"):
            raise InputSynthesisError(f"Unsupported mouse button for drag: {button}")

        with self._lock:
            start_x, start_y = self._vx, self._vy
            self._state = VirtualCursorState.DRAGGING
            dist = math.hypot(x - start_x, y - start_y)

            # Start drag: Mouse Down
            self._dispatch_native_button_event(start_x, start_y, button=btn_str, is_down=True, click_state=1)
            drag_start_event = VirtualCursorEvent(
                event_type="drag_start",
                x=start_x,
                y=start_y,
                state=self._state,
                button=btn_str,
                target_pid=self.target_pid,
                target_window_id=self.target_window_id,
            )
            self._record_event(drag_start_event)

            if not self._mock:
                time.sleep(0.02)

            actual_duration = max(0.001, duration)
            step_count = steps if steps is not None else max(5, int(actual_duration * 60))
            step_delay = actual_duration / step_count

            vx_vel = (x - start_x) / actual_duration
            vy_vel = (y - start_y) / actual_duration
            self._velocity = (vx_vel, vy_vel)
            self._speed = dist / actual_duration

            for i in range(1, step_count + 1):
                t = i / step_count
                cx = start_x + (x - start_x) * t
                cy = start_y + (y - start_y) * t

                self._vx = float(cx)
                self._vy = float(cy)
                now = time.time()
                self._trajectory.append((self._vx, self._vy, now))

                self._dispatch_native_drag_event(self._vx, self._vy, button=btn_str)

                if not self._mock and step_delay > 0:
                    time.sleep(step_delay)

            # End drag: Mouse Up
            self._vx = float(x)
            self._vy = float(y)
            self._dispatch_native_button_event(self._vx, self._vy, button=btn_str, is_down=False, click_state=1)

            self._velocity = (0.0, 0.0)
            self._speed = 0.0
            self._state = VirtualCursorState.IDLE

            drag_event = VirtualCursorEvent(
                event_type="drag",
                x=self._vx,
                y=self._vy,
                state=self._state,
                button=btn_str,
                previous_position=(start_x, start_y),
                duration=actual_duration,
                speed=dist / actual_duration,
                velocity=(vx_vel, vy_vel),
                target_pid=self.target_pid,
                target_window_id=self.target_window_id,
            )
            self._record_event(drag_event)

    def hover(self, x: float, y: float, duration: float = 0.0) -> None:
        """Moves cursor to (x, y) and maintains a hover state.

        Args:
            x: Target horizontal coordinate.
            y: Target vertical coordinate.
            duration: Time in seconds to dwell at target.
        """
        if not (math.isfinite(x) and math.isfinite(y)):
            raise InputSynthesisError(f"Invalid non-finite hover coordinates: ({x}, {y})")

        if (self._vx != x) or (self._vy != y):
            self.move_to(x, y)

        with self._lock:
            self._state = VirtualCursorState.HOVERING
            self._velocity = (0.0, 0.0)
            self._speed = 0.0

            hover_event = VirtualCursorEvent(
                event_type="hover",
                x=self._vx,
                y=self._vy,
                duration=duration,
                state=self._state,
                target_pid=self.target_pid,
                target_window_id=self.target_window_id,
            )
            self._record_event(hover_event)

            if not self._mock and duration > 0:
                time.sleep(duration)

            self._state = VirtualCursorState.IDLE

    def scroll(self, dx: int = 0, dy: int = 0) -> None:
        """Synthesizes targeted scroll wheel events.

        Args:
            dx: Horizontal scroll delta.
            dy: Vertical scroll delta.
        """
        with self._lock:
            if not self._mock and self.target_pid is not None:
                native = _get_native_bindings()
                if native:
                    ev = native.cg.CGEventCreateScrollWheelEvent2(None, 1, 1, dy, dx, 0)
                    if ev:
                        native.cg.CGEventPostToPid(self.target_pid, ev)
                        native.cf.CFRelease(ev)

            scroll_event = VirtualCursorEvent(
                event_type="scroll",
                x=self._vx,
                y=self._vy,
                state=self._state,
                target_pid=self.target_pid,
                target_window_id=self.target_window_id,
                extra={"dx": dx, "dy": dy},
            )
            self._record_event(scroll_event)

    # -------------------------------------------------------------------------
    # Window-Relative Ratio Helpers
    # -------------------------------------------------------------------------

    def move_to_ratio(
        self,
        window: WindowInfo,
        norm_x: float,
        norm_y: float,
        duration: float = 0.0,
        smooth: bool = False,
    ) -> None:
        """Moves cursor to a window-relative ratio coordinate [0.0, 1.0]."""
        self.set_target_window(window)
        screen_x, screen_y = window.ratio_to_screen(norm_x, norm_y)
        self.move_to(screen_x, screen_y, duration=duration, smooth=smooth)

    def click_ratio(
        self,
        window: WindowInfo,
        norm_x: float,
        norm_y: float,
        button: Union[str, MouseButton] = "left",
        click_count: int = 1,
    ) -> None:
        """Clicks at a window-relative ratio coordinate [0.0, 1.0]."""
        self.set_target_window(window)
        screen_x, screen_y = window.ratio_to_screen(norm_x, norm_y)
        self.click(x=screen_x, y=screen_y, button=button, click_count=click_count)

    def drag_to_ratio(
        self,
        window: WindowInfo,
        norm_x: float,
        norm_y: float,
        duration: float = 0.0,
        button: Union[str, MouseButton] = "left",
    ) -> None:
        """Drags to a window-relative ratio coordinate [0.0, 1.0]."""
        self.set_target_window(window)
        screen_x, screen_y = window.ratio_to_screen(norm_x, norm_y)
        self.drag_to(screen_x, screen_y, duration=duration, button=button)

    # -------------------------------------------------------------------------
    # Targeted Background Keyboard & Process Methods (Requirement 2)
    # -------------------------------------------------------------------------

    def set_target_pid(self, pid: Optional[int]) -> None:
        """Sets target process ID for directed background input dispatch."""
        with self._lock:
            self.target_pid = pid

    def set_target_bundle_id(self, bundle_id: str) -> bool:
        """Resolves and targets a process ID from a bundle identifier."""
        native = _get_native_bindings()
        if native:
            pid = native.resolve_pid_from_bundle_id(bundle_id)
            if pid:
                self.set_target_pid(pid)
                return True
        return False

    def _ensure_target_pid(self) -> Optional[int]:
        """Ensures a valid target PID is set. Falls back to frontmost application if unassigned."""
        if self._mock:
            return None
        if self.target_pid is not None:
            return self.target_pid

        native = _get_native_bindings()
        if native:
            pid = native.resolve_frontmost_pid()
            if pid:
                self.target_pid = pid
                return pid
        return None

    def type_text(self, text: str, interval: float = 0.01) -> None:
        """Types text directly into target process without stealing user's active keyboard focus."""
        if not text:
            return

        # Capture state under lock, then release before dispatch
        with self._lock:
            pid = self._ensure_target_pid()
            snap_x, snap_y = self._vx, self._vy
            snap_state = self._state

        type_event = VirtualCursorEvent(
            event_type="type_text",
            x=snap_x,
            y=snap_y,
            state=snap_state,
            target_pid=pid,
            target_window_id=self.target_window_id,
            extra={"text": text, "length": len(text)},
        )
        self._record_event(type_event)  # fires listeners in daemon thread, non-blocking

        # Dispatch ctypes events OUTSIDE the lock (can hold lock for seconds otherwise)
        if not self._mock and pid is not None:
            native = _get_native_bindings()
            if native:
                from ctypes import c_uint16
                for ch in text:
                    if ch in ("\n", "\r"):
                        ev_d = native.cg.CGEventCreateKeyboardEvent(None, 36, True)
                        ev_u = native.cg.CGEventCreateKeyboardEvent(None, 36, False)
                        if ev_d:
                            native.cg.CGEventPostToPid(pid, ev_d)
                            native.cf.CFRelease(ev_d)
                        if ev_u:
                            native.cg.CGEventPostToPid(pid, ev_u)
                            native.cf.CFRelease(ev_u)
                    else:
                        utf16_bytes = ch.encode("utf-16-le")
                        code_units_count = len(utf16_bytes) // 2
                        code_units = (c_uint16 * code_units_count).from_buffer_copy(utf16_bytes)

                        ev_d = native.cg.CGEventCreateKeyboardEvent(None, 0, True)
                        if ev_d:
                            native.cg.CGEventKeyboardSetUnicodeString(ev_d, code_units_count, code_units)
                            native.cg.CGEventPostToPid(pid, ev_d)
                            native.cf.CFRelease(ev_d)

                        ev_u = native.cg.CGEventCreateKeyboardEvent(None, 0, False)
                        if ev_u:
                            native.cg.CGEventKeyboardSetUnicodeString(ev_u, code_units_count, code_units)
                            native.cg.CGEventPostToPid(pid, ev_u)
                            native.cf.CFRelease(ev_u)

                    if interval > 0:
                        time.sleep(interval)


    def press_hotkey(self, *keys: str) -> None:
        """Synthesizes hotkey combination directly to target process PID."""
        if not keys:
            return

        # Capture state under lock, release before ctypes dispatch
        with self._lock:
            pid = self._ensure_target_pid()
            snap_x, snap_y = self._vx, self._vy
            snap_state = self._state

        hotkey_event = VirtualCursorEvent(
            event_type="hotkey",
            x=snap_x,
            y=snap_y,
            state=snap_state,
            target_pid=pid,
            target_window_id=self.target_window_id,
            extra={"keys": list(keys)},
        )
        self._record_event(hotkey_event)  # non-blocking daemon thread

        if not self._mock:
            native = _get_native_bindings()
            if native:
                VK_MAP = {
                    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7,
                    "c": 8, "v": 9, "b": 11, "q": 12, "w": 13, "e": 14, "r": 15,
                    "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22,
                    "5": 23, "=": 24, "9": 25, "7": 26, "-": 27, "8": 28, "0": 29,
                    "]": 30, "o": 31, "u": 32, "[": 33, "i": 34, "p": 35, "l": 37,
                    "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44,
                    "n": 45, "m": 46, ".": 47, "`": 50,
                    "return": 36, "enter": 36, "\n": 36, "\r": 36,
                    "tab": 48, "\t": 48,
                    "space": 49, " ": 49,
                    "backspace": 51, "delete": 51,
                    "escape": 53, "esc": 53,
                    "left": 123, "right": 124, "down": 125, "up": 126,
                    "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
                    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96,
                    "f6": 97, "f7": 98, "f8": 100, "f9": 101, "f10": 109,
                    "f11": 103, "f12": 111,
                }
                flags = 0
                primary_code = 0
                for k in keys:
                    k_lower = k.lower().strip()
                    if k_lower in ("cmd", "command"):
                        flags |= native.kCGEventFlagMaskCommand
                    elif k_lower in ("shift",):
                        flags |= native.kCGEventFlagMaskShift
                    elif k_lower in ("alt", "option"):
                        flags |= native.kCGEventFlagMaskAlternate
                    elif k_lower in ("ctrl", "control"):
                        flags |= native.kCGEventFlagMaskControl
                    else:
                        primary_code = VK_MAP.get(k_lower, 0)

                # Dispatch to PID if available
                if pid is not None:
                    ev_d = native.cg.CGEventCreateKeyboardEvent(None, primary_code, True)
                    if ev_d:
                        if flags:
                            native.cg.CGEventSetFlags(ev_d, flags)
                        native.cg.CGEventPostToPid(pid, ev_d)
                        native.cf.CFRelease(ev_d)

                    ev_u = native.cg.CGEventCreateKeyboardEvent(None, primary_code, False)
                    if ev_u:
                        if flags:
                            native.cg.CGEventSetFlags(ev_u, flags)
                        native.cg.CGEventPostToPid(pid, ev_u)
                        native.cf.CFRelease(ev_u)

                # Only dispatch via HID if not in background mode and (target PID is frontmost or no PID specified)
                front_pid = native.resolve_frontmost_pid()
                if not self.background_mode and (pid is None or front_pid == pid) and sys.platform == "darwin":
                    try:
                        hid_d = native.cg.CGEventCreateKeyboardEvent(None, primary_code, True)
                        if hid_d:
                            if flags:
                                native.cg.CGEventSetFlags(hid_d, flags)
                            native.cg.CGEventPost(0, hid_d)
                            native.cf.CFRelease(hid_d)
                        time.sleep(0.01)
                        hid_u = native.cg.CGEventCreateKeyboardEvent(None, primary_code, False)
                        if hid_u:
                            if flags:
                                native.cg.CGEventSetFlags(hid_u, flags)
                            native.cg.CGEventPost(0, hid_u)
                            native.cf.CFRelease(hid_u)
                    except Exception as e:
                        logger.debug("HID hotkey post error: %s", e)

    def paste_text(self, text: str) -> None:
        """Injects text into target process without taking foreground focus."""
        self.type_text(text, interval=0.0)

    # -------------------------------------------------------------------------
    # Native Dispatch Helpers (STRICT R7 Invariant Enforcement)
    # -------------------------------------------------------------------------

    def _dispatch_native_mouse_event(self, x: float, y: float, event_type: str = "move") -> None:
        """Dispatches mouse move event directly to target process PID."""
        if self._mock:
            return
        pid = self._ensure_target_pid()
        if pid is None:
            # Enforce R7: never dispatch un-targeted events to kCGHIDEventTap!
            logger.debug("VirtualCursor: No target_pid set; live event skipped to preserve physical mouse.")
            return

        native = _get_native_bindings()
        if not native:
            return

        ev = native.cg.CGEventCreateMouseEvent(
            None,
            native.kCGEventMouseMoved,
            native.CGPoint(x, y),
            native.kCGMouseButtonLeft,
        )
        if ev:
            native.cg.CGEventPostToPid(pid, ev)
            native.cf.CFRelease(ev)

    def _dispatch_native_button_event(
        self,
        x: float,
        y: float,
        button: str,
        is_down: bool,
        click_state: int = 1,
    ) -> None:
        """Dispatches mouse down/up event directly to target process PID."""
        if self._mock:
            return
        pid = self._ensure_target_pid()
        if pid is None:
            native_tmp = _get_native_bindings()
            if native_tmp:
                pid = native_tmp.resolve_frontmost_pid()
        if pid is None:
            try:
                import subprocess
                res = subprocess.run(["pgrep", "-x", "Finder"], capture_output=True, text=True, check=False)
                if res.returncode == 0 and res.stdout.strip():
                    pid = int(res.stdout.strip().splitlines()[0])
            except Exception:
                pass
        if pid is None:
            logger.debug("VirtualCursor: No target_pid set; live click skipped to preserve physical mouse.")
            return

        native = _get_native_bindings()
        if not native:
            return

        if button == "left":
            cg_type = native.kCGEventLeftMouseDown if is_down else native.kCGEventLeftMouseUp
            cg_btn = native.kCGMouseButtonLeft
        elif button == "right":
            cg_type = native.kCGEventRightMouseDown if is_down else native.kCGEventRightMouseUp
            cg_btn = native.kCGMouseButtonRight
        else:
            cg_type = native.kCGEventOtherMouseDown if is_down else native.kCGEventOtherMouseUp
            cg_btn = native.kCGMouseButtonCenter

        ev = native.cg.CGEventCreateMouseEvent(None, cg_type, native.CGPoint(x, y), cg_btn)
        if ev:
            native.cg.CGEventSetIntegerValueField(ev, native.kCGMouseEventClickState, click_state)
            native.cg.CGEventPostToPid(pid, ev)
            native.cf.CFRelease(ev)

        # In live macOS desktop mode: only dispatch via HID if not in background mode and target process is frontmost
        front_pid = native.resolve_frontmost_pid()
        if not self.background_mode and (pid is None or front_pid == pid) and sys.platform == "darwin":
            try:
                loc_ev = native.cg.CGEventCreate(None)
                if loc_ev:
                    pt = native.cg.CGEventGetLocation(loc_ev)
                    orig_x, orig_y = float(pt.x), float(pt.y)
                    native.cf.CFRelease(loc_ev)

                    hid_ev = native.cg.CGEventCreateMouseEvent(None, cg_type, native.CGPoint(x, y), cg_btn)
                    if hid_ev:
                        native.cg.CGEventSetIntegerValueField(hid_ev, native.kCGMouseEventClickState, click_state)
                        native.cg.CGEventPost(0, hid_ev)  # kCGHIDEventTap
                        native.cf.CFRelease(hid_ev)

                    # Instantly restore hardware mouse position
                    native.cg.CGWarpMouseCursorPosition(native.CGPoint(orig_x, orig_y))
            except Exception as e:
                logger.debug("Live click dispatch error: %s", e)

    def _dispatch_native_drag_event(self, x: float, y: float, button: str) -> None:
        """Dispatches mouse dragged event directly to target process PID."""
        if self._mock:
            return
        pid = self._ensure_target_pid()
        if pid is None:
            return

        native = _get_native_bindings()
        if not native:
            return

        if button == "left":
            cg_type = native.kCGEventLeftMouseDragged
            cg_btn = native.kCGMouseButtonLeft
        elif button == "right":
            cg_type = native.kCGEventRightMouseDragged
            cg_btn = native.kCGMouseButtonRight
        else:
            cg_type = native.kCGEventOtherMouseDragged
            cg_btn = native.kCGMouseButtonCenter

        ev = native.cg.CGEventCreateMouseEvent(None, cg_type, native.CGPoint(x, y), cg_btn)
        if ev:
            native.cg.CGEventPostToPid(pid, ev)
            native.cf.CFRelease(ev)

    # -------------------------------------------------------------------------
    # Telemetry, Observers & Assertion Helpers
    # -------------------------------------------------------------------------

    def _record_event(self, event: VirtualCursorEvent) -> None:
        """Records event to history and notifies subscribed listeners (non-blocking, off-lock)."""
        self._history.append(event)
        listeners = list(self._listeners)
        if listeners:
            # Fire listeners in a daemon thread so we NEVER hold self._lock while
            # a listener tries to acquire another lock (e.g. ClioServer._lock → deadlock).
            import threading
            def _dispatch():
                for listener in listeners:
                    try:
                        listener(event)
                    except Exception as e:
                        logger.error("Error in virtual cursor listener callback: %s", e)
            t = threading.Thread(target=_dispatch, daemon=True)
            t.start()

    def add_listener(self, callback: Callable[[VirtualCursorEvent], None]) -> None:
        """Subscribes an observer callback to receive virtual cursor events."""
        with self._lock:
            if callback not in self._listeners:
                self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[VirtualCursorEvent], None]) -> None:
        """Unsubscribes an observer callback."""
        with self._lock:
            if callback in self._listeners:
                self._listeners.remove(callback)

    def get_history(self, event_type: Optional[str] = None) -> List[VirtualCursorEvent]:
        """Returns filtered event history."""
        with self._lock:
            if event_type is None:
                return list(self._history)
            return [e for e in self._history if e.event_type == event_type]

    def clear_history(self) -> None:
        """Clears recorded event history and trajectory."""
        with self._lock:
            self._history.clear()
            self._trajectory = [(self._vx, self._vy, time.time())]

    def assert_position(self, x: float, y: float, tolerance: float = 0.001) -> None:
        """Asserts that the virtual cursor is within tolerance of (x, y)."""
        cur_x, cur_y = self.position
        if math.hypot(cur_x - x, cur_y - y) > tolerance:
            raise AssertionError(f"Expected cursor at ({x}, {y}), got ({cur_x}, {cur_y}) [tol={tolerance}]")

    def assert_state(self, expected_state: VirtualCursorState) -> None:
        """Asserts that the current virtual cursor state matches expected_state."""
        if self.state != expected_state:
            raise AssertionError(f"Expected virtual cursor state {expected_state}, got {self.state}")

    def assert_clicked_at(
        self,
        x: float,
        y: float,
        button: str = "left",
        count: int = 1,
        tolerance: float = 1.0,
    ) -> None:
        """Asserts that a click event was recorded at (x, y) with specified button and count."""
        clicks = [
            e for e in self.get_history("click")
            if e.button == button and e.click_count == count and math.hypot(e.x - x, e.y - y) <= tolerance
        ]
        if not clicks:
            raise AssertionError(
                f"No click found at ({x}, {y}) with button={button!r}, count={count} (tolerance={tolerance})"
            )

    def assert_dragged(
        self,
        start: Tuple[float, float],
        end: Tuple[float, float],
        tolerance: float = 1.0,
    ) -> None:
        """Asserts that a drag gesture was recorded from start to end."""
        drags = [
            e for e in self.get_history("drag")
            if e.previous_position is not None
            and math.hypot(e.previous_position[0] - start[0], e.previous_position[1] - start[1]) <= tolerance
            and math.hypot(e.x - end[0], e.y - end[1]) <= tolerance
        ]
        if not drags:
            raise AssertionError(f"No drag found from {start} to {end} within tolerance {tolerance}")

    def assert_hovered_at(self, x: float, y: float, tolerance: float = 1.0) -> None:
        """Asserts that a hover event was recorded at (x, y)."""
        hovers = [
            e for e in self.get_history("hover")
            if math.hypot(e.x - x, e.y - y) <= tolerance
        ]
        if not hovers:
            raise AssertionError(f"No hover found at ({x}, {y}) within tolerance {tolerance}")
