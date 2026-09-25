"""Live Demonstration Capture Subsystem.

Path: src/memory/capture.py
Belongs to Teach-Mode Demonstration and Dissection Pipeline.

Captures live user demonstration actions (mouse clicks, drags, keystrokes, active window context)
using macOS CGEventTap (passive listen-only mode) and feeds them into WorkflowRecorderPipeline
to automatically dissect human actions into reusable, parameterized WorkflowSpec objects.

Supports:
1. Live macOS Event Tap via CoreGraphics / ApplicationServices ctypes.
2. Graceful headless / mock fallback for testing and CI.
3. Frontmost application context & window bounds resolution at the instant of action.
"""

from __future__ import annotations

import ctypes
from ctypes import (
    CFUNCTYPE,
    POINTER,
    Structure,
    byref,
    c_bool,
    c_double,
    c_int,
    c_int32,
    c_int64,
    c_uint16,
    c_uint32,
    c_uint64,
    c_void_p,
)
from dataclasses import dataclass
import logging
import os
import re
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
import uuid

from src.memory.engine import TaskMemoryEngine
from src.memory.models import ActionType, WorkflowSpec, WorkflowStep
from src.memory.recorder import RawEvent, RawEventType, WindowBounds, WorkflowRecorderPipeline

logger = logging.getLogger(__name__)


def get_frontmost_app_info() -> Tuple[Optional[str], Optional[int]]:
    """Resolves the current frontmost application's bundle ID and PID on macOS."""
    if sys.platform != "darwin":
        return None, None
    try:
        out = subprocess.check_output(
            "lsappinfo info -bundleid $(lsappinfo front)",
            shell=True,
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=1.0,
        )
        b_match = re.search(r'bundleID="([^"]+)"', out)
        p_match = re.search(r'\bpid\s*=\s*(\d+)', out)
        bundle_id = b_match.group(1) if b_match else None
        pid = int(p_match.group(1)) if p_match else None
        return bundle_id, pid
    except Exception:
        return None, None


def get_frontmost_window_bounds(target_bundle_id: Optional[str] = None) -> Optional["WindowBounds"]:  # noqa: F821 — forward ref resolved at runtime
    """Returns the position and size of the frontmost application window.

    Uses CoreGraphics CGWindowListCopyWindowInfo via MacOSActuator (thread-safe,
    requires no special AX threading context, and never segfaults across background
    worker threads).

    Used by the native CGEventTap callback to attach window_bounds to raw mouse events
    so the recorder pipeline can compute normalised (norm_x, norm_y) coordinates for
    resolution-independent replay (Bug #4 fix).
    """
    if sys.platform != "darwin":
        return None
    try:
        from src.actuators.macos import MacOSActuator
        from src.memory.recorder import WindowBounds

        act = MacOSActuator()
        wins = act.get_windows(target_bundle_id) if target_bundle_id else act.get_windows()
        if wins:
            # First window in list is frontmost in z-order
            w = wins[0]
            if w.width > 0 and w.height > 0:
                return WindowBounds(x=w.x, y=w.y, width=w.width, height=w.height)
    except Exception:
        pass
    return None





# -----------------------------------------------------------------------------
# macOS Native Event Tap Types & Bindings
# -----------------------------------------------------------------------------

class _NativeEventTap:
    """ctypes bindings to macOS CGEventTap for passive listen-only input capture."""

    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise RuntimeError(f"macOS required for native event tap (current: {sys.platform})")

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
            self.appkit = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/AppKit.framework/AppKit")
        except OSError as e:
            raise RuntimeError(f"Could not load macOS system libraries for EventTap: {e}") from e

        self.kCFRunLoopCommonModes = c_void_p.in_dll(self.cf, "kCFRunLoopCommonModes")


        cg = self.cg
        cf = self.cf

        # Constants
        self.kCGHIDEventTap = 0
        self.kCGHeadInsertEventTap = 0
        self.kCGEventTapOptionListenOnly = 1

        # Event masks
        self.kCGEventLeftMouseDown = 1
        self.kCGEventLeftMouseUp = 2
        self.kCGEventRightMouseDown = 3
        self.kCGEventRightMouseUp = 4
        self.kCGEventMouseMoved = 5
        self.kCGEventLeftMouseDragged = 6
        self.kCGEventRightMouseDragged = 7
        self.kCGEventKeyDown = 10
        self.kCGEventKeyUp = 11
        self.kCGEventFlagsChanged = 12

        self.kCGEventFlagMaskShift = 0x00020000
        self.kCGEventFlagMaskControl = 0x00040000
        self.kCGEventFlagMaskAlternate = 0x00080000
        self.kCGEventFlagMaskCommand = 0x00100000

        # Function Prototypes
        cg.CGEventGetLocation.argtypes = [c_void_p]
        cg.CGEventGetLocation.restype = CGPoint

        cg.CGEventGetFlags.argtypes = [c_void_p]
        cg.CGEventGetFlags.restype = c_uint64

        cg.CGEventGetIntegerValueField.argtypes = [c_void_p, c_uint32]
        cg.CGEventGetIntegerValueField.restype = c_int64

        self.kCGKeyboardEventKeycode = 9

        cf.CFRunLoopGetCurrent.argtypes = []
        cf.CFRunLoopGetCurrent.restype = c_void_p

        cf.CFRunLoopRun.argtypes = []
        cf.CFRunLoopRun.restype = None

        cf.CFRunLoopStop.argtypes = [c_void_p]
        cf.CFRunLoopStop.restype = None

        cf.CFMachPortCreateRunLoopSource.argtypes = [c_void_p, c_void_p, c_int32]
        cf.CFMachPortCreateRunLoopSource.restype = c_void_p

        cf.CFRunLoopAddSource.argtypes = [c_void_p, c_void_p, c_void_p]
        cf.CFRunLoopAddSource.restype = None

        cf.CFRelease.argtypes = [c_void_p]
        cf.CFRelease.restype = None

        # Callback signature: CGEventRef callback(CGEventTapProxy proxy, CGEventType type, CGEventRef event, void *refcon)
        self.CALLBACK_TYPE = CFUNCTYPE(c_void_p, c_void_p, c_uint32, c_void_p, c_void_p)

        cg.CGEventTapCreate.argtypes = [
            c_uint32,  # tap
            c_uint32,  # place
            c_uint32,  # options
            c_uint64,  # eventsOfInterest
            self.CALLBACK_TYPE,
            c_void_p,
        ]
        cg.CGEventTapCreate.restype = c_void_p

        cg.CGEventTapEnable.argtypes = [c_void_p, c_bool]
        cg.CGEventTapEnable.restype = None


# -----------------------------------------------------------------------------
# Demonstration Capture Manager
# -----------------------------------------------------------------------------

class LiveDemonstrationCapture:
    """Manages recording human demonstration sessions into clean WorkflowSpecs."""

    # Keycode mapping table for common QWERTY keys
    KEYCODE_MAP = {
        0: "a", 1: "s", 2: "d", 3: "f", 4: "h", 5: "g", 6: "z", 7: "x",
        8: "c", 9: "v", 11: "b", 12: "q", 13: "w", 14: "e", 15: "r",
        16: "y", 17: "t", 31: "o", 32: "u", 34: "i", 35: "p", 37: "l",
        38: "j", 40: "k", 45: "n", 46: "m",
        18: "1", 19: "2", 20: "3", 21: "4", 23: "5", 22: "6", 26: "7",
        28: "8", 25: "9", 29: "0",
        36: "Return", 48: "Tab", 49: " ", 51: "BackSpace", 53: "Escape",
    }

    def __init__(
        self,
        memory: Optional[TaskMemoryEngine] = None,
        mock: Optional[bool] = None,
    ) -> None:
        self.memory = memory or TaskMemoryEngine()
        self.pipeline = WorkflowRecorderPipeline()

        if mock is not None:
            self._mock = bool(mock)
        else:
            self._mock = (sys.platform != "darwin") or (os.environ.get("CI") == "true")

        self._is_recording = False
        self._raw_events: List[RawEvent] = []
        self._start_time: float = 0.0
        self._lock = threading.RLock()

        self._active_bundle_id: Optional[str] = None
        self._last_bundle_check: float = 0.0

        self._tap_thread: Optional[threading.Thread] = None
        self._run_loop: Optional[Any] = None
        self._mach_port: Optional[Any] = None
        self._c_callback: Optional[Any] = None
        self._native: Optional[_NativeEventTap] = None

        if not self._mock and sys.platform == "darwin":
            try:
                self._native = _NativeEventTap()
            except Exception as e:
                logger.warning("Native event tap unavailable: %s. Falling back to mock capture.", e)
                self._mock = True

    @property
    def is_recording(self) -> bool:
        with self._lock:
            return self._is_recording

    @property
    def event_count(self) -> int:
        with self._lock:
            return len(self._raw_events)

    @property
    def elapsed_seconds(self) -> float:
        with self._lock:
            if not self._is_recording:
                return 0.0
            return max(0.0, time.time() - self._start_time)

    def feed_event(self, event: RawEvent) -> None:
        """Manually or synthetically appends a raw event (used in tests / mock mode)."""
        with self._lock:
            if self._is_recording:
                self._raw_events.append(event)

    def start_recording(self) -> bool:
        """Starts capturing user actions.

        On real macOS (non-mock), this launches the CGEventTap listener and the
        60 Hz universal mouse poller as a belt-and-suspenders fallback.  When the
        CGEventTap returns NULL (Accessibility permission not granted), the poller
        ensures at least button-state transitions are captured.

        In mock / CI mode neither background tap is started; events are delivered
        exclusively through :meth:`feed_event` (used by tests and
        ``/api/record/feed``).
        """
        with self._lock:
            if self._is_recording:
                return True
            self._raw_events.clear()
            self._start_time = time.time()
            self._is_recording = True
            b_id, _ = get_frontmost_app_info()
            self._active_bundle_id = b_id
            self._last_bundle_check = self._start_time

        self._start_app_tracker()

        # Wire native event capture on real macOS (Bug #7 fix).
        # Mock mode: feed_event() is the only delivery path (tests / UI feed endpoint).
        if not self._mock and sys.platform == "darwin" and self._native is not None:
            self._start_native_tap()          # CGEventTap (preferred; starts universal poller if tap is NULL)

        logger.info("Started demonstration recording (active app: %s).", self._active_bundle_id)
        return True

    def _start_app_tracker(self) -> None:
        """Runs a lightweight background thread updating active bundle ID every 200ms."""
        def _tracker() -> None:
            while self._is_recording:
                try:
                    b_id, _ = get_frontmost_app_info()
                    if b_id:
                        with self._lock:
                            self._active_bundle_id = b_id
                except Exception:
                    pass
                time.sleep(0.2)

        t = threading.Thread(target=_tracker, name="ClioAppTracker", daemon=True)
        t.start()

    def _start_universal_poller(self) -> None:
        """Universal 60Hz mouse poller using CoreGraphics button state (requires zero permissions)."""
        def _poller() -> None:
            native = self._native
            if not native:
                return

            last_left = False
            last_right = False

            while self._is_recording:
                try:
                    ev = native.cg.CGEventCreate(None)
                    if ev:
                        pt = native.cg.CGEventGetLocation(ev)
                        cur_x = float(pt.x)
                        cur_y = float(pt.y)
                        native.cf.CFRelease(ev)
                    else:
                        time.sleep(0.016)
                        continue

                    # Check button states
                    cur_left = bool(native.cg.CGEventSourceButtonState(0, 0))
                    cur_right = bool(native.cg.CGEventSourceButtonState(0, 1))
                    now = time.time()
                    bundle = self._active_bundle_id

                    # Left button transitions
                    if cur_left and not last_left:
                        self.feed_event(RawEvent(
                            event_type=RawEventType.MOUSE_DOWN,
                            timestamp=now,
                            x=cur_x,
                            y=cur_y,
                            button="left",
                            bundle_id=bundle,
                        ))
                    elif not cur_left and last_left:
                        self.feed_event(RawEvent(
                            event_type=RawEventType.MOUSE_UP,
                            timestamp=now,
                            x=cur_x,
                            y=cur_y,
                            button="left",
                            bundle_id=bundle,
                        ))

                    # Right button transitions
                    if cur_right and not last_right:
                        self.feed_event(RawEvent(
                            event_type=RawEventType.MOUSE_DOWN,
                            timestamp=now,
                            x=cur_x,
                            y=cur_y,
                            button="right",
                            bundle_id=bundle,
                        ))
                    elif not cur_right and last_right:
                        self.feed_event(RawEvent(
                            event_type=RawEventType.MOUSE_UP,
                            timestamp=now,
                            x=cur_x,
                            y=cur_y,
                            button="right",
                            bundle_id=bundle,
                        ))

                    last_left = cur_left
                    last_right = cur_right
                except Exception as ex:
                    logger.debug("Error in universal mouse poller: %s", ex)

                time.sleep(0.016)  # ~60Hz polling

        t = threading.Thread(target=_poller, name="ClioUniversalMousePoller", daemon=True)
        t.start()

    def _start_native_tap(self) -> None:
        """Launches macOS event tap listener loop on background thread."""
        def _tap_worker() -> None:
            native = self._native
            if not native:
                return

            def _callback(proxy: Any, ev_type: int, event_ref: Any, refcon: Any) -> Any:
                if not self._is_recording:
                    return event_ref

                # Auto-reenable if macOS disabled the tap by timeout
                if ev_type in (0xFFFFFFFE, 0xFFFFFFFF, 4294967294, 4294967295):
                    logger.warning("CGEventTap disabled by macOS timeout, auto-reenabling...")
                    if self._mach_port:
                        native.cg.CGEventTapEnable(self._mach_port, True)
                    return event_ref

                try:
                    now = time.time()
                    cur_bundle = self._active_bundle_id
                    pt = native.cg.CGEventGetLocation(event_ref)
                    flags = native.cg.CGEventGetFlags(event_ref)

                    modifiers: List[str] = []
                    if flags & native.kCGEventFlagMaskCommand:
                        modifiers.append("cmd")
                    if flags & native.kCGEventFlagMaskShift:
                        modifiers.append("shift")
                    if flags & native.kCGEventFlagMaskAlternate:
                        modifiers.append("alt")
                    if flags & native.kCGEventFlagMaskControl:
                        modifiers.append("ctrl")

                    # Fetch window bounds once per mouse event so norm_x/y can be
                    # computed during coalescing (Bug #4 fix).  Keyboard events do
                    # not need coordinates, so bounds is only fetched for mouse types.
                    win_bounds = None
                    if ev_type in (
                        native.kCGEventLeftMouseDown,  1,
                        native.kCGEventLeftMouseUp,    2,
                        native.kCGEventRightMouseDown, 3,
                        native.kCGEventRightMouseUp,   4,
                        native.kCGEventLeftMouseDragged, 6,
                    ):
                        try:
                            win_bounds = get_frontmost_window_bounds(cur_bundle)
                        except Exception:
                            pass

                    # Map event types
                    if ev_type in (native.kCGEventLeftMouseDown, 1):
                        self.feed_event(RawEvent(
                            event_type=RawEventType.MOUSE_DOWN,
                            timestamp=now,
                            x=float(pt.x),
                            y=float(pt.y),
                            button="left",
                            modifiers=modifiers,
                            bundle_id=cur_bundle,
                            window_bounds=win_bounds,
                        ))
                    elif ev_type in (native.kCGEventLeftMouseUp, 2):
                        self.feed_event(RawEvent(
                            event_type=RawEventType.MOUSE_UP,
                            timestamp=now,
                            x=float(pt.x),
                            y=float(pt.y),
                            button="left",
                            modifiers=modifiers,
                            bundle_id=cur_bundle,
                            window_bounds=win_bounds,
                        ))
                    elif ev_type in (native.kCGEventRightMouseDown, 3):
                        self.feed_event(RawEvent(
                            event_type=RawEventType.MOUSE_DOWN,
                            timestamp=now,
                            x=float(pt.x),
                            y=float(pt.y),
                            button="right",
                            modifiers=modifiers,
                            bundle_id=cur_bundle,
                            window_bounds=win_bounds,
                        ))
                    elif ev_type in (native.kCGEventRightMouseUp, 4):
                        self.feed_event(RawEvent(
                            event_type=RawEventType.MOUSE_UP,
                            timestamp=now,
                            x=float(pt.x),
                            y=float(pt.y),
                            button="right",
                            modifiers=modifiers,
                            bundle_id=cur_bundle,
                            window_bounds=win_bounds,
                        ))
                    elif ev_type in (native.kCGEventLeftMouseDragged, 6):
                        self.feed_event(RawEvent(
                            event_type=RawEventType.MOUSE_DRAG,
                            timestamp=now,
                            x=float(pt.x),
                            y=float(pt.y),
                            button="left",
                            modifiers=modifiers,
                            bundle_id=cur_bundle,
                            window_bounds=win_bounds,
                        ))
                    elif ev_type in (native.kCGEventKeyDown, 10):
                        keycode = int(native.cg.CGEventGetIntegerValueField(event_ref, native.kCGKeyboardEventKeycode))
                        key_str = self.KEYCODE_MAP.get(keycode, f"k_{keycode}")
                        self.feed_event(RawEvent(
                            event_type=RawEventType.KEY_DOWN,
                            timestamp=now,
                            key=key_str,
                            modifiers=modifiers,
                            bundle_id=cur_bundle,
                        ))
                except Exception as ex:
                    logger.debug("Error in CGEventTap callback: %s", ex)

                return event_ref

            self._c_callback = native.CALLBACK_TYPE(_callback)

            mask = (
                (1 << native.kCGEventLeftMouseDown)
                | (1 << native.kCGEventLeftMouseUp)
                | (1 << native.kCGEventRightMouseDown)
                | (1 << native.kCGEventRightMouseUp)
                | (1 << native.kCGEventLeftMouseDragged)
                | (1 << native.kCGEventKeyDown)
            )

            port = native.cg.CGEventTapCreate(
                native.kCGHIDEventTap,
                native.kCGHeadInsertEventTap,
                native.kCGEventTapOptionListenOnly,
                mask,
                self._c_callback,
                None,
            )

            if not port:
                logger.warning("CGEventTapCreate returned NULL. Universal poller active as fallback.")
                self._start_universal_poller()
                return

            self._mach_port = port
            source = native.cf.CFMachPortCreateRunLoopSource(None, port, 0)
            self._run_loop = native.cf.CFRunLoopGetCurrent()
            native.cf.CFRunLoopAddSource(self._run_loop, source, native.kCFRunLoopCommonModes)
            native.cg.CGEventTapEnable(port, True)

            native.cf.CFRunLoopRun()

        self._tap_thread = threading.Thread(target=_tap_worker, name="ClioDemonstrationTap", daemon=True)
        self._tap_thread.start()

    def stop_recording(self) -> List[RawEvent]:
        """Stops capturing and returns all raw recorded events."""
        with self._lock:
            if not self._is_recording:
                return list(self._raw_events)
            self._is_recording = False
            events = list(self._raw_events)

        if self._native and self._mach_port:
            try:
                self._native.cg.CGEventTapEnable(self._mach_port, False)
            except Exception:
                pass
            self._mach_port = None
        if self._native and self._run_loop:
            try:
                self._native.cf.CFRunLoopStop(self._run_loop)
            except Exception:
                pass
            self._run_loop = None

        logger.info("Stopped demonstration recording. Total raw events: %d", len(events))
        return events

    def dissect_and_save(
        self,
        name: str = "Demonstrated Task",
        canonical_trigger: str = "",
        description: str = "",
        target_bundle_id: str = "",
        save: bool = True,
    ) -> WorkflowSpec:
        """Stops recording, passes events through the 4-stage dissection pipeline, and saves to memory."""
        raw_events = self.stop_recording()

        # Only target an application if events explicitly occurred in that application.
        # Do NOT fall back to _active_bundle_id (which could be the user's background editor/IDE).
        clio_bundles = {"com.apple.loginwindow", "com.apple.dock", "com.clio.desktop", "clio-bar", "clio", "Clio"}
        if not target_bundle_id:
            for ev in raw_events:
                if ev.bundle_id and ev.bundle_id not in clio_bundles:
                    target_bundle_id = ev.bundle_id
                    break


        # Process through 4-stage pipeline
        spec = self.pipeline.process_raw_events(
            raw_events=raw_events,
            name=name,
            canonical_trigger=canonical_trigger or name.lower(),
            description=description or f"Demonstrated task '{name}' with {len(raw_events)} events.",
            target_bundle_id=target_bundle_id,
        )

        # Include anaphoric execution aliases so that saying "perform that action", "do that action", "run that"
        # can also directly match via NL retrieval
        if isinstance(spec.triggers, dict):
            aliases = list(spec.triggers.get("aliases", []))
            for alias in ["perform that action", "do that action", "run that action", "run recorded task", "do that", "perform that"]:
                if alias not in aliases and alias != spec.triggers.get("canonical"):
                    aliases.append(alias)
            spec.triggers["aliases"] = aliases

        # Prepend a focus step if targeting an application and not already present
        if target_bundle_id and spec.steps:
            first_action = spec.steps[0].action
            first_action_str = first_action.value if hasattr(first_action, "value") else str(first_action)
            if first_action_str not in ("focus_app", "launch_app"):
                focus_step = WorkflowStep(
                    step_id=f"step_{uuid.uuid4().hex[:6]}_focus",
                    order=1,
                    description=f"Focus target application ({target_bundle_id})",
                    action=ActionType.FOCUS_APP,
                    target={"bundle_id": target_bundle_id},
                )
                for s in spec.steps:
                    s.order += 1
                spec.steps.insert(0, focus_step)

        if save and self.memory is not None:
            try:
                self.memory.save_workflow(spec)
                logger.info(
                    "Successfully dissected and saved workflow '%s' (ID: %s, steps: %d)",
                    spec.name,
                    spec.id,
                    len(spec.steps),
                )
            except Exception as save_err:
                import traceback
                logger.error(
                    "Failed to persist workflow '%s' to memory: %s\n%s",
                    spec.name,
                    save_err,
                    traceback.format_exc(),
                )
                raise RuntimeError(
                    f"Workflow dissection succeeded but database save failed: {save_err}"
                ) from save_err

        return spec

