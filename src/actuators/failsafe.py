"""FEAT-ACT-08: Failsafe Watchdog & Screen Corner Detection.

Multi-layer 50Hz watchdog daemon and inline screen-corner detector raising emergency stop.
Includes synthetic modifier key release and POSIX signal trapping.
Zero external dependencies: pure Python standard library with lazy ctypes for macOS.
"""

from __future__ import annotations

import ctypes
from ctypes import Structure, byref, c_bool, c_double, c_uint32, c_uint64, c_void_p
import inspect
import logging
import math
import os
import signal
import sys
import threading
import time
from typing import Callable, List, Optional, Tuple

from src.actuators.types import (
    CornerLocation,
    FailsafeEmergencyStop,
    FailsafeTriggerInfo,
    get_screen_corner,
    is_screen_corner,
)

logger = logging.getLogger(__name__)

# CoreGraphics Structures
class CGPoint(Structure):
    _fields_ = [("x", c_double), ("y", c_double)]

class CGSize(Structure):
    _fields_ = [("width", c_double), ("height", c_double)]

class CGRect(Structure):
    _fields_ = [("origin", CGPoint), ("size", CGSize)]

# macOS Virtual Keycodes for Modifiers
KEY_COMMAND = 55
KEY_COMMAND_RIGHT = 54
KEY_SHIFT = 56
KEY_SHIFT_RIGHT = 60
KEY_OPTION = 58
KEY_OPTION_RIGHT = 61
KEY_CONTROL = 59
KEY_CONTROL_RIGHT = 62

# CoreGraphics Event Constants
kCGEventLeftMouseUp = 2
kCGEventRightMouseUp = 4
kCGEventOtherMouseUp = 7
kCGMouseButtonLeft = 0
kCGMouseButtonRight = 1
kCGMouseButtonCenter = 2
kCGHIDEventTap = 0


class _NativeCoreGraphicsBindings:
    """Lazy loader for macOS CoreGraphics and CoreFoundation ctypes bindings."""

    def __init__(self) -> None:
        self.available = False
        if sys.platform != "darwin":
            return

        try:
            self.cg = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
            self.cf = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")

            # Mouse location
            self.cg.CGEventCreate.argtypes = [c_void_p]
            self.cg.CGEventCreate.restype = c_void_p
            self.cg.CGEventGetLocation.argtypes = [c_void_p]
            self.cg.CGEventGetLocation.restype = CGPoint
            self.cf.CFRelease.argtypes = [c_void_p]

            # Display geometry
            self.cg.CGMainDisplayID.restype = c_uint32
            self.cg.CGDisplayBounds.argtypes = [c_uint32]
            self.cg.CGDisplayBounds.restype = CGRect
            self.cg.CGGetActiveDisplayList.argtypes = [c_uint32, ctypes.POINTER(c_uint32), ctypes.POINTER(c_uint32)]

            # Event synthesis (Modifier / Mouse release)
            self.cg.CGEventCreateKeyboardEvent.argtypes = [c_void_p, c_uint32, c_bool]
            self.cg.CGEventCreateKeyboardEvent.restype = c_void_p
            self.cg.CGEventCreateMouseEvent.argtypes = [c_void_p, c_uint32, CGPoint, c_uint32]
            self.cg.CGEventCreateMouseEvent.restype = c_void_p
            self.cg.CGEventSetFlags.argtypes = [c_void_p, c_uint64]
            self.cg.CGEventPost.argtypes = [c_uint32, c_void_p]

            self.available = True
        except Exception as e:
            logger.warning("Failed to initialize CoreGraphics ctypes bindings: %s", e)
            self.available = False

    def get_mouse_position(self) -> Tuple[float, float]:
        if not self.available:
            return (500.0, 500.0)
        ev = self.cg.CGEventCreate(None)
        if not ev:
            return (500.0, 500.0)
        try:
            loc = self.cg.CGEventGetLocation(ev)
            return (float(loc.x), float(loc.y))
        finally:
            self.cf.CFRelease(ev)

    def get_display_bounds(self) -> List[Tuple[float, float, float, float]]:
        """Returns list of (origin_x, origin_y, width, height) for all active displays."""
        if not self.available:
            return [(0.0, 0.0, 1920.0, 1080.0)]

        max_displays = 16
        displays = (c_uint32 * max_displays)()
        count = c_uint32(0)
        self.cg.CGGetActiveDisplayList(max_displays, displays, byref(count))

        results = []
        for i in range(count.value):
            b = self.cg.CGDisplayBounds(displays[i])
            results.append((float(b.origin.x), float(b.origin.y), float(b.size.width), float(b.size.height)))

        if not results:
            b = self.cg.CGDisplayBounds(self.cg.CGMainDisplayID())
            results.append((float(b.origin.x), float(b.origin.y), float(b.size.width), float(b.size.height)))
        return results

    def release_all_modifiers(self, current_pos: Optional[Tuple[float, float]] = None) -> None:
        """Posts key-up events for Command, Shift, Option, Control and mouse-up events."""
        if not self.available:
            return

        # 1. Release modifier keys
        modifier_keys = [
            KEY_COMMAND, KEY_COMMAND_RIGHT,
            KEY_SHIFT, KEY_SHIFT_RIGHT,
            KEY_OPTION, KEY_OPTION_RIGHT,
            KEY_CONTROL, KEY_CONTROL_RIGHT,
        ]
        for keycode in modifier_keys:
            try:
                ev = self.cg.CGEventCreateKeyboardEvent(None, keycode, False)
                if ev:
                    self.cg.CGEventSetFlags(ev, 0)
                    self.cg.CGEventPost(kCGHIDEventTap, ev)
                    self.cf.CFRelease(ev)
            except Exception as e:
                logger.debug("Error releasing modifier %s: %s", keycode, e)

        # 2. Release mouse buttons
        pos_x, pos_y = current_pos if current_pos else self.get_mouse_position()
        pt = CGPoint(pos_x, pos_y)
        mouse_ups = [
            (kCGEventLeftMouseUp, kCGMouseButtonLeft),
            (kCGEventRightMouseUp, kCGMouseButtonRight),
            (kCGEventOtherMouseUp, kCGMouseButtonCenter),
        ]
        for ev_type, btn in mouse_ups:
            try:
                ev = self.cg.CGEventCreateMouseEvent(None, ev_type, pt, btn)
                if ev:
                    self.cg.CGEventPost(kCGHIDEventTap, ev)
                    self.cf.CFRelease(ev)
            except Exception as e:
                logger.debug("Error releasing mouse button %s: %s", btn, e)


_cg_bindings = _NativeCoreGraphicsBindings()


class FailsafeWatchdog:
    """High-frequency background watchdog and inline checker detecting mouse in screen corners.

    Provides:
      - 50Hz background thread polling cursor position.
      - Inline pre-action cursor checking with zero latency.
      - Synthetic modifier and mouse button release upon abort.
      - POSIX signal trapping (SIGINT, SIGTERM) with clean cleanup.
      - Injectable hooks for 100% deterministic testability.
    """

    def __init__(
        self,
        margin: float = 5.0,
        frequency: float = 50.0,
        position_getter: Optional[Callable[[], Tuple[float, float]]] = None,
        bounds_getter: Optional[Callable[[], List[Tuple[float, float, float, float]]]] = None,
        modifier_releaser: Optional[Callable[..., None]] = None,
        auto_release_modifiers: bool = True,
        trap_signals: bool = False,
        poll_frequency_hz: Optional[float] = None,
        on_emergency: Optional[Callable[[FailsafeTriggerInfo], None]] = None,
        install_signals: Optional[bool] = None,
    ) -> None:
        """Initializes the watchdog.

        Args:
            margin: Pixel threshold from screen corners (default: 5.0).
            frequency: Polling frequency in Hz (default: 50.0 -> 20ms interval).
            position_getter: Optional hook returning current (x, y) coordinates.
            bounds_getter: Optional hook returning list of (ox, oy, w, h) display bounds.
            modifier_releaser: Optional hook for releasing modifier keys on emergency stop.
            auto_release_modifiers: Whether to automatically release modifiers when triggered.
            trap_signals: Whether to register POSIX signal handlers on start.
            poll_frequency_hz: Alias for frequency parameter.
            on_emergency: Optional initial callback to invoke when failsafe triggers.
            install_signals: Alias for trap_signals parameter.
        """
        self.margin = float(margin)
        hz = poll_frequency_hz if poll_frequency_hz is not None else frequency
        self.frequency = float(hz)
        self.interval = 1.0 / self.frequency if self.frequency > 0 else 0.02
        self.auto_release_modifiers = auto_release_modifiers
        self.should_trap_signals = install_signals if install_signals is not None else trap_signals

        # Injectable hooks
        self._position_getter = position_getter or _cg_bindings.get_mouse_position
        self._bounds_getter = bounds_getter or _cg_bindings.get_display_bounds
        self._modifier_releaser = modifier_releaser or _cg_bindings.release_all_modifiers

        # Synchronization & State
        self._lock = threading.RLock()
        self._triggered = threading.Event()
        self._stop_requested = threading.Event()
        self._reset_event = threading.Event()
        self._trigger_info: Optional[FailsafeTriggerInfo] = None
        self._thread: Optional[threading.Thread] = None
        self._started = False
        self._callbacks: List[Callable[[FailsafeTriggerInfo], None]] = []
        if on_emergency is not None:
            self._callbacks.append(on_emergency)

        # Signal handling state
        self._original_sigint = None
        self._original_sigterm = None
        self._signals_installed = False
        self._armed = True

    @property
    def is_armed(self) -> bool:
        """Returns True if the watchdog is currently armed to detect emergency stops."""
        with self._lock:
            return self._armed

    def arm(self) -> None:
        """Arms the failsafe watchdog during automated execution."""
        with self._lock:
            self._triggered.clear()
            self._trigger_info = None
            self._armed = True

    def disarm(self) -> None:
        """Disarms the failsafe watchdog during user demonstration recording and idle periods."""
        with self._lock:
            self._armed = False
            self._triggered.clear()
            self._trigger_info = None

    @property
    def is_running(self) -> bool:
        """Returns True if the background watchdog thread is currently running."""
        return self._thread is not None and self._thread.is_alive()

    @property
    def is_triggered(self) -> bool:
        """Returns True if the failsafe emergency stop condition has been met."""
        return self._triggered.is_set()

    @property
    def trigger_info(self) -> Optional[FailsafeTriggerInfo]:
        """Returns details about the emergency stop trigger, or None if not triggered."""
        with self._lock:
            return self._trigger_info

    def add_callback(self, callback: Callable[[FailsafeTriggerInfo], None]) -> None:
        """Registers a callback to execute immediately when failsafe triggers."""
        with self._lock:
            if callback not in self._callbacks:
                self._callbacks.append(callback)

    def remove_callback(self, callback: Callable[[FailsafeTriggerInfo], None]) -> None:
        """Unregisters an emergency callback."""
        with self._lock:
            if callback in self._callbacks:
                self._callbacks.remove(callback)

    def get_mouse_position(self) -> Tuple[float, float]:
        """Returns current mouse coordinates via hook or CoreGraphics."""
        return self._position_getter()

    def get_display_bounds(self) -> List[Tuple[float, float, float, float]]:
        """Returns list of active display bounds."""
        return self._bounds_getter()

    def check_corner(
        self, x: float, y: float
    ) -> Optional[Tuple[str, int, Tuple[float, float, float, float]]]:
        """Checks if the given (x, y) coordinates fall within any screen corner.

        Returns:
            Tuple of (corner_name, display_index, display_bounds) or None.
            corner_name is one of 'top-left', 'top-right', 'bottom-left', 'bottom-right'.
        """
        if not (math.isfinite(x) and math.isfinite(y)):
            return None


        displays = self.get_display_bounds()
        m = self.margin

        # 1. If point is inside a display's bounding box, evaluate only that display
        for idx, bounds in enumerate(displays):
            ox, oy, w, h = bounds
            if ox <= x <= (ox + w) and oy <= y <= (oy + h):
                corner = get_screen_corner(x - ox, y - oy, w, h, m)
                if corner:
                    return (corner.value.replace("_", "-"), idx, bounds)
                return None

        # 2. Point is outside all displays (overshoot / off-screen panic gesture)
        matches = []
        for idx, bounds in enumerate(displays):
            ox, oy, w, h = bounds
            corner = get_screen_corner(x - ox, y - oy, w, h, m)
            if corner:
                cx = ox if "left" in corner.value else (ox + w)
                cy = oy if "top" in corner.value else (oy + h)
                dist_sq = (x - cx) ** 2 + (y - cy) ** 2
                matches.append((dist_sq, corner.value.replace("_", "-"), idx, bounds))

        if matches:
            matches.sort(key=lambda item: item[0])
            _, corner_name, idx, bounds = matches[0]
            return (corner_name, idx, bounds)

        return None

    def trigger(
        self,
        position: Optional[Tuple[float, float]] = None,
        corner: Optional[str] = None,
        display_index: int = 0,
        display_bounds: Optional[Tuple[float, float, float, float]] = None,
        reason: str = "Emergency stop triggered",
    ) -> None:
        """Thread-safe invocation to trip the emergency stop."""
        callbacks_to_invoke = []
        with self._lock:
            if self._triggered.is_set():
                return  # Already triggered

            pos = position if position is not None else self.get_mouse_position()
            cor = corner or "manual"
            bnds = display_bounds or (0.0, 0.0, 0.0, 0.0)

            info = FailsafeTriggerInfo(
                corner=cor,
                position=pos,
                display_index=display_index,
                display_bounds=bnds,
                timestamp=time.time(),
                reason=reason,
            )
            self._trigger_info = info
            self._triggered.set()
            callbacks_to_invoke = list(self._callbacks)

        logger.warning("Failsafe emergency stop triggered: %s at %s (%s)", cor, pos, reason)

        # Release modifiers immediately
        if self.auto_release_modifiers:
            self.release_modifiers(current_pos=pos)

        # Execute registered callbacks with exception isolation
        for cb in callbacks_to_invoke:
            try:
                cb(info)
            except Exception as e:
                logger.error("Error in failsafe callback %s: %s", cb, e, exc_info=True)

    def release_modifiers(self, current_pos: Optional[Tuple[float, float]] = None) -> None:
        """Releases stuck modifier keys (Cmd, Shift, Opt, Ctrl) and mouse buttons."""
        try:
            sig = inspect.signature(self._modifier_releaser)
            if "current_pos" in sig.parameters:
                self._modifier_releaser(current_pos=current_pos)
            else:
                self._modifier_releaser()
        except Exception as e:
            logger.error("Failed to execute modifier releaser hook: %s", e)

    def check_failsafe(self) -> None:
        """Inline pre-action check.

        Must be called before every actuator action.
        Raises:
            FailsafeEmergencyStop: If already triggered or if cursor is currently in a corner.
        """
        # 1. Quick atomic flag check
        if self._triggered.is_set():
            info = self.trigger_info
            raise FailsafeEmergencyStop(
                message=info.reason if info else "Emergency stop is active",
                position=info.position if info else None,
                corner=info.corner if info else None,
                display_index=info.display_index if info else None,
            )

        # 2. Instant inline cursor check
        pos = self.get_mouse_position()
        corner_match = self.check_corner(pos[0], pos[1])
        if corner_match:
            corner_name, idx, bounds = corner_match
            self.trigger(
                position=pos,
                corner=corner_name,
                display_index=idx,
                display_bounds=bounds,
                reason=f"Inline check detected mouse in {corner_name} corner",
            )
            raise FailsafeEmergencyStop(
                message=f"Emergency stop: cursor in {corner_name} corner",
                position=pos,
                corner=corner_name,
                display_index=idx,
            )

    def reset(self) -> None:
        """Resets the triggered state, allowing automation to resume."""
        with self._lock:
            self._triggered.clear()
            self._trigger_info = None
            self._reset_event.set()
            if self._started and not self.is_running and not self._stop_requested.is_set():
                self._start_thread_locked()

    def _watchdog_loop(self) -> None:
        """50Hz background thread loop polling mouse position."""
        while not self._stop_requested.is_set():
            loop_start = time.perf_counter()

            if not self._armed:
                time.sleep(self.interval)
                continue

            if self._triggered.is_set():
                self._reset_event.wait(timeout=self.interval)
                self._reset_event.clear()
                continue

            pos = self.get_mouse_position()
            corner_match = self.check_corner(pos[0], pos[1])
            if corner_match:
                corner_name, idx, bounds = corner_match
                self.trigger(
                    position=pos,
                    corner=corner_name,
                    display_index=idx,
                    display_bounds=bounds,
                    reason=f"50Hz watchdog detected mouse in {corner_name} corner",
                )
                continue

            elapsed = time.perf_counter() - loop_start
            sleep_duration = max(0.0, self.interval - elapsed)
            if self._stop_requested.wait(timeout=sleep_duration):
                break

    def _start_thread_locked(self) -> None:
        """Starts the watchdog thread while holding self._lock."""
        if self._thread is not None and self._thread.is_alive():
            if not self._stop_requested.is_set():
                logger.debug("FailsafeWatchdog is already running.")
                return
            if threading.current_thread() != self._thread:
                self._thread.join(timeout=0.5)

        self._started = True
        self._stop_requested.clear()
        self._reset_event.clear()
        self._thread = threading.Thread(
            target=self._watchdog_loop,
            name="FailsafeWatchdogThread",
            daemon=True,
        )
        self._thread.start()

    def start(self) -> None:
        """Starts the 50Hz background watchdog thread."""
        with self._lock:
            self._start_thread_locked()

        if self.should_trap_signals:
            self.install_signal_handlers()

    def stop(self, timeout: float = 1.0) -> None:
        """Stops the watchdog thread and restores signal handlers."""
        with self._lock:
            self._started = False
            self._stop_requested.set()
            self._reset_event.set()
            thread = self._thread
            self._thread = None
            if thread and thread.is_alive() and threading.current_thread() != thread:
                thread.join(timeout=timeout)

        if self._signals_installed:
            self.restore_signal_handlers()

    def install_signal_handlers(self) -> bool:
        """Traps POSIX signals (SIGINT, SIGTERM) to ensure clean modifier release on interrupt.

        Returns True if handlers were installed, False if skipped (e.g. not in main thread).
        """
        if threading.current_thread() is not threading.main_thread():
            logger.debug("Skipping POSIX signal handler installation: not on main thread.")
            return False

        def _signal_handler(signum, frame):
            sig_name = signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
            logger.warning("Failsafe intercepted POSIX signal %s", sig_name)
            self.trigger(
                corner="signal",
                reason=f"Process intercepted {sig_name}",
            )
            raise FailsafeEmergencyStop(
                message=f"Process interrupted by {sig_name}",
                corner="signal",
            )

        try:
            self._original_sigint = signal.signal(signal.SIGINT, _signal_handler)
            self._original_sigterm = signal.signal(signal.SIGTERM, _signal_handler)
            self._signals_installed = True
            return True
        except Exception as e:
            logger.warning("Failed to install signal handlers: %s", e)
            return False

    def restore_signal_handlers(self) -> None:
        """Restores original POSIX signal handlers."""
        if not self._signals_installed:
            return
        if threading.current_thread() is not threading.main_thread():
            return

        try:
            if self._original_sigint is not None:
                signal.signal(signal.SIGINT, self._original_sigint)
            if self._original_sigterm is not None:
                signal.signal(signal.SIGTERM, self._original_sigterm)
        except Exception as e:
            logger.warning("Error restoring signal handlers: %s", e)
        finally:
            self._signals_installed = False

    def __enter__(self) -> FailsafeWatchdog:
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.stop()


# Alias for backward compatibility across Explorer handoffs
FailsafeCornerWatchdog = FailsafeWatchdog
