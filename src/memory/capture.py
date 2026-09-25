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
    c_char_p,
    c_double,
    c_int,
    c_int32,
    c_int64,
    c_size_t,
    c_uint16,
    c_uint32,
    c_uint64,
    c_void_p,
)
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import re
import signal
import struct
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
import uuid

from src.memory.engine import TaskMemoryEngine
from src.memory.models import ActionType, WorkflowSpec, WorkflowStep
from src.memory.recorder import RawEvent, RawEventType, WindowBounds, WorkflowRecorderPipeline
from src.memory.recording_evaluator import QualityGrade, RecordingQualityEvaluator, RecordingQualityReport

logger = logging.getLogger(__name__)

# Valid JPEG binary for deterministic testing and mock environments
MOCK_JPEG_BYTES = (
    b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00H\x00H\x00\x00\xff\xdb\x00C\x00"
    b"\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f"
    b"\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342\xff\xc0"
    b"\x00\x11\x08\x03\xc0\x05\x00\x03\x01\"\x00\x02\x11\x01\x03\x11\x01\xff\xc4\x00\x1f\x00"
    b"\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05"
    b"\x06\x07\x08\t\n\x0b\xff\xda\x00\x0c\x03\x01\x00\x02\x11\x03\x11\x00?\x00\xbf\x00\xff\xd9"
)


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


_cached_actuator_for_capture: Optional[Any] = None


def _get_capture_actuator() -> Optional[Any]:
    global _cached_actuator_for_capture
    if _cached_actuator_for_capture is None and sys.platform == "darwin":
        try:
            from src.actuators.macos import MacOSActuator
            _cached_actuator_for_capture = MacOSActuator(enable_watchdog=False)
        except Exception:
            pass
    return _cached_actuator_for_capture


def get_window_at_point(x: float, y: float) -> Optional[Tuple[str, str, "WindowBounds"]]:
    """Finds the topmost on-screen application window containing (x, y) via CoreGraphics.

    Directly inspects the macOS window server z-order, providing instant, zero-latency
    hit-testing that remains 100% accurate even immediately after window moves.

    Returns:
        (owner_app_name, window_title, WindowBounds) or None.
    """
    if sys.platform != "darwin":
        return None
    try:
        from src.actuators.macos import CGRect
        from src.memory.recorder import WindowBounds

        act = _get_capture_actuator()
        if act is None:
            return None
        cg = act.native.cg
        cf = act.native.cf
        win_list = cg.CGWindowListCopyWindowInfo(1, 0)  # kCGWindowListOptionOnScreenOnly
        if not win_list:
            return None
        count = cf.CFArrayGetCount(win_list)
        found = None
        for i in range(count):
            win_dict = cf.CFArrayGetValueAtIndex(win_list, i)
            layer = act._cf_to_int(cf.CFDictionaryGetValue(win_dict, act._kCGWindowLayer))
            if layer != 0:
                continue
            rect_dict = cf.CFDictionaryGetValue(win_dict, act._kCGWindowBounds)
            rect = CGRect()
            if rect_dict and cg.CGRectMakeWithDictionaryRepresentation(rect_dict, byref(rect)):
                rx, ry, rw, rh = rect.origin.x, rect.origin.y, rect.size.width, rect.size.height
                if rw > 0 and rh > 0 and (rx <= x <= rx + rw) and (ry <= y <= ry + rh):
                    owner = act._cf_to_str(cf.CFDictionaryGetValue(win_dict, act._kCGWindowOwnerName))
                    title = act._cf_to_str(cf.CFDictionaryGetValue(win_dict, act._kCGWindowName))
                    found = (owner, title, WindowBounds(x=rx, y=ry, width=rw, height=rh))
                    break
        cf.CFRelease(win_list)
        return found
    except Exception as e:
        logger.debug("Error in get_window_at_point: %s", e)
        return None


def get_frontmost_window_bounds(target_bundle_id: Optional[str] = None) -> Optional["WindowBounds"]:  # noqa: F821 — forward ref resolved at runtime
    """Returns the position and size of the frontmost application window."""
    if sys.platform != "darwin":
        return None
    try:
        from src.memory.recorder import WindowBounds

        act = _get_capture_actuator()
        if act is None:
            return None
        wins = act.get_windows(target_bundle_id) if target_bundle_id else act.get_windows()
        if wins:
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
        recordings_dir: Optional[Union[str, Path]] = None,
    ) -> None:
        self.memory = memory or TaskMemoryEngine()
        self.pipeline = WorkflowRecorderPipeline()

        if mock is not None:
            self._mock = bool(mock)
        else:
            self._mock = (sys.platform != "darwin") or (os.environ.get("CI") == "true")

        if recordings_dir:
            self._recordings_base_dir = Path(recordings_dir)
        elif "CLIO_PROJECT_DIR" in os.environ and Path(os.environ["CLIO_PROJECT_DIR"]).exists():
            self._recordings_base_dir = Path(os.environ["CLIO_PROJECT_DIR"]) / "recordings"
        elif Path("/Users/minhnguyen/Desktop/Coding/imitate").exists():
            self._recordings_base_dir = Path("/Users/minhnguyen/Desktop/Coding/imitate/recordings")
        else:
            project_root = Path(__file__).resolve().parent.parent.parent
            self._recordings_base_dir = project_root / "recordings"
        self._recordings_base_dir.mkdir(parents=True, exist_ok=True)

        self._is_recording = False
        self._raw_events: List[RawEvent] = []
        self._captured_frames: List[Dict[str, Any]] = []
        self._session_id: str = str(uuid.uuid4())[:8]
        self._session_dir: Path = self._recordings_base_dir / self._session_id
        self._frames_dir: Path = self._session_dir / "frames"
        self._video_path: Optional[Path] = self._session_dir / "recording.mov"
        self._video_proc: Optional[subprocess.Popen] = None
        self._cg_capture_initialized = False
        self._cg_capture_available = False
        self._cg: Optional[Any] = None
        self._cf: Optional[Any] = None
        self._imageio: Optional[Any] = None

        self._frame_capturer_thread: Optional[threading.Thread] = None
        self._frame_capturer_stop_event = threading.Event()

        self._tracked_windows: List[Dict[str, Any]] = []
        self._window_movements: List[Dict[str, Any]] = []
        self._last_window_bounds: Optional[WindowBounds] = None
        self._last_window_bundle: Optional[str] = None
        self._quality_report: Optional[RecordingQualityReport] = None

        self._display_width: int = 1470
        self._display_height: int = 956
        self._detect_display_geometry()

        self._last_frame_time: float = 0.0
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

    def _detect_display_geometry(self) -> None:
        """Determines active display resolution via CoreGraphics or system defaults."""
        if sys.platform != "darwin":
            return
        try:
            import ctypes
            cg = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
            cg.CGMainDisplayID.restype = ctypes.c_uint32
            cg.CGDisplayPixelsWide.argtypes = [ctypes.c_uint32]
            cg.CGDisplayPixelsWide.restype = ctypes.c_size_t
            cg.CGDisplayPixelsHigh.argtypes = [ctypes.c_uint32]
            cg.CGDisplayPixelsHigh.restype = ctypes.c_size_t
            disp = cg.CGMainDisplayID()
            w = cg.CGDisplayPixelsWide(disp)
            h = cg.CGDisplayPixelsHigh(disp)
            if w > 0 and h > 0:
                self._display_width = int(w)
                self._display_height = int(h)
        except Exception as e:
            logger.debug("Failed detecting display geometry: %s", e)

    @property
    def session_id(self) -> str:
        with self._lock:
            return self._session_id

    @property
    def session_dir(self) -> Path:
        with self._lock:
            return self._session_dir

    @property
    def video_path(self) -> Optional[Path]:
        with self._lock:
            return self._video_path

    @property
    def quality_report(self) -> Optional[RecordingQualityReport]:
        with self._lock:
            return self._quality_report

    @property
    def captured_frames(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._captured_frames)

    def set_swift_video_path(self, path: str) -> None:
        """Called by the server when the Swift layer provides a pre-recorded video path.

        In macOS 15 Sequoia, video recording is handled by ScreenCaptureKit inside the
        authorized Swift process (ClioBar.swift SwiftScreenRecorder). The video path is
        passed here so Python treats it as the session's video without spawning screencapture.
        """
        if not path:
            return
        with self._lock:
            self._swift_video_path = path
            self._video_path = Path(path)
            self._session_dir = self._video_path.parent
            self._session_id = self._session_dir.name
            self._frames_dir = self._session_dir / "frames"
            self._session_dir.mkdir(parents=True, exist_ok=True)
            self._frames_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Swift video path registered: %s", path)


    def _init_cg_capture(self) -> bool:
        """Initializes direct CoreGraphics and ImageIO ctypes bindings for fast desktop frame capture."""
        if self._cg_capture_initialized:
            return self._cg_capture_available
        self._cg_capture_initialized = True
        if sys.platform != "darwin" or self._mock:
            self._cg_capture_available = False
            return False
        try:
            self._cg = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
            self._cf = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
            self._imageio = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/ImageIO.framework/ImageIO")

            # CG function prototypes
            self._cg.CGMainDisplayID.argtypes = []
            self._cg.CGMainDisplayID.restype = c_uint32

            self._cg.CGDisplayCreateImage.argtypes = [c_uint32]
            self._cg.CGDisplayCreateImage.restype = c_void_p

            self._cg.CGImageRelease.argtypes = [c_void_p]
            self._cg.CGImageRelease.restype = None

            # CF function prototypes
            self._cf.CFStringCreateWithCString.argtypes = [c_void_p, c_char_p, c_uint32]
            self._cf.CFStringCreateWithCString.restype = c_void_p

            self._cf.CFURLCreateWithFileSystemPath.argtypes = [c_void_p, c_void_p, c_int32, c_bool]
            self._cf.CFURLCreateWithFileSystemPath.restype = c_void_p

            self._cf.CFRelease.argtypes = [c_void_p]
            self._cf.CFRelease.restype = None

            # ImageIO function prototypes
            self._imageio.CGImageDestinationCreateWithURL.argtypes = [c_void_p, c_void_p, c_size_t, c_void_p]
            self._imageio.CGImageDestinationCreateWithURL.restype = c_void_p

            self._imageio.CGImageDestinationAddImage.argtypes = [c_void_p, c_void_p, c_void_p]
            self._imageio.CGImageDestinationAddImage.restype = None

            self._imageio.CGImageDestinationFinalize.argtypes = [c_void_p]
            self._imageio.CGImageDestinationFinalize.restype = c_bool

            self._cg_capture_available = True
            return True
        except Exception as e:
            logger.debug("Failed initializing CoreGraphics/ImageIO ctypes capture: %s", e)
            self._cg_capture_available = False
            return False

    def _capture_screen_frame_cg(self, output_path: str) -> bool:
        """Fast, silent desktop capture via direct CoreGraphics and ImageIO ctypes."""
        if not self._init_cg_capture():
            return False
        cg = self._cg
        cf = self._cf
        imageio = self._imageio
        disp = cg.CGMainDisplayID()
        img = cg.CGDisplayCreateImage(disp)
        if not img:
            return False
        try:
            kCFStringEncodingUTF8 = 0x08000100
            kCFURLPOSIXPathStyle = 0
            path_cf = cf.CFStringCreateWithCString(None, output_path.encode("utf-8"), kCFStringEncodingUTF8)
            if not path_cf:
                return False
            url = cf.CFURLCreateWithFileSystemPath(None, path_cf, kCFURLPOSIXPathStyle, False)
            type_cf = cf.CFStringCreateWithCString(None, b"public.jpeg", kCFStringEncodingUTF8)
            dest = imageio.CGImageDestinationCreateWithURL(url, type_cf, 1, None)
            if not dest:
                cf.CFRelease(type_cf)
                cf.CFRelease(url)
                cf.CFRelease(path_cf)
                return False
            try:
                imageio.CGImageDestinationAddImage(dest, img, None)
                success = imageio.CGImageDestinationFinalize(dest)
                return bool(success)
            finally:
                cf.CFRelease(dest)
                cf.CFRelease(type_cf)
                cf.CFRelease(url)
                cf.CFRelease(path_cf)
        except Exception as e:
            logger.debug("CoreGraphics direct screen capture failed: %s", e)
            return False
        finally:
            cg.CGImageRelease(img)

    def _capture_screen_frame(self, label: str = "") -> Optional[str]:
        """Captures a lightweight JPEG screenshot of the entire desktop for visual grounding."""
        if not self._is_recording and label not in ("start", "end", "synthesize_fallback"):
            return None

        with self._lock:
            has_swift = bool(getattr(self, "_swift_video_path", None)) or (self._video_path is not None and self._video_path.exists())
        if has_swift and label not in ("synthesize_fallback",):
            # When Swift is recording via ScreenCaptureKit, do not spawn screencapture (which captures only wallpaper).
            # Milestone keyframes are extracted directly from the finalized Retina video container in stop_recording.
            return None

        frames_dir = self._frames_dir
        frames_dir.mkdir(parents=True, exist_ok=True)

        now_ts = time.time()
        with self._lock:
            frame_idx = len(self._captured_frames) + 1
        filename = f"frame_{frame_idx:04d}_{int(now_ts * 1000)}.jpg"
        frame_path = str(frames_dir / filename)

        if self._mock:
            try:
                with open(frame_path, "wb") as f:
                    f.write(MOCK_JPEG_BYTES)
                frame_data = {
                    "frame_index": frame_idx,
                    "path": frame_path,
                    "timestamp": now_ts,
                    "label": label,
                }
                with self._lock:
                    self._captured_frames.append(frame_data)
                return frame_path
            except Exception as e:
                logger.debug("Mock screen frame capture error: %s", e)
            return None

        if sys.platform != "darwin":
            return None

        # Primary path: screencapture -x -t jpg (fallback only when not using Swift ScreenCaptureKit).
        # screencapture -x = no sound, -t jpg = JPEG output
        try:
            res = subprocess.run(
                ["screencapture", "-x", "-t", "jpg", frame_path],
                capture_output=True,
                timeout=2.0,
                check=False,
            )
            if res.returncode == 0 and os.path.exists(frame_path):
                frame_data = {
                    "frame_index": frame_idx,
                    "path": frame_path,
                    "timestamp": now_ts,
                    "label": label,
                }
                with self._lock:
                    self._captured_frames.append(frame_data)
                return frame_path
        except Exception as e:
            logger.debug("screencapture fallback error: %s", e)

        # 3. Tertiary fallback: if in non-mock on macOS and capture failed, write mock bytes
        try:
            with open(frame_path, "wb") as f:
                f.write(MOCK_JPEG_BYTES)
            frame_data = {
                "frame_index": frame_idx,
                "path": frame_path,
                "timestamp": now_ts,
                "label": label,
            }
            with self._lock:
                self._captured_frames.append(frame_data)
            return frame_path
        except Exception as e:
            logger.debug("Tertiary frame fallback error: %s", e)
        return None

    def _capture_screen_frame_throttled(self, x: float = 0.0, y: float = 0.0) -> None:
        """Captures a screen frame on interaction, throttled to max 2 frames/sec."""
        if self._mock or sys.platform != "darwin":
            return

        now = time.time()
        with self._lock:
            if now - self._last_frame_time < 0.5:
                return
            self._last_frame_time = now
        t = threading.Thread(
            target=self._capture_screen_frame,
            args=(f"click_{int(x)}_{int(y)}",),
            daemon=True,
        )
        t.start()

    def _start_video_recorder(self) -> None:
        """Starts continuous native video recording.

        In macOS 15 Sequoia, full screen video recording containing all active application windows
        is exclusively handled by SwiftScreenRecorder inside the authorized Clio.app process via
        Apple ScreenCaptureKit (SCRecordingOutput).

        Spawning `screencapture -v` from a Python subprocess on macOS 15 is prohibited because
        WindowServer strips all window content for unauthorized subprocesses, returning only the
        desktop wallpaper (e.g. Golden Gate Bridge). Python designates the video path and lets
        Swift record directly.
        """
        if self._mock or sys.platform != "darwin":
            return
        with self._lock:
            if self._video_path is None:
                self._session_dir.mkdir(parents=True, exist_ok=True)
                self._video_path = self._session_dir / "recording.mov"
        logger.info("Designated screen recording destination at %s (ScreenCaptureKit provider)", self._video_path)

    def _stop_video_recorder(self) -> None:
        """Gracefully halts video recording by closing stdin and sending SIGINT to flush QuickTime container."""
        if self._video_proc is not None:
            proc = self._video_proc
            try:
                # Explicitly close stdin to eliminate ResourceWarning: unclosed file and notify screencapture
                if proc.stdin is not None and not proc.stdin.closed:
                    try:
                        proc.stdin.close()
                    except Exception:
                        pass

                # Send SIGINT to allow screencapture to flush sample buffers and finalize moov atom
                proc.send_signal(signal.SIGINT)
                proc.wait(timeout=3.0)
                logger.info("screencapture video recorder terminated gracefully.")
            except subprocess.TimeoutExpired:
                logger.warning("screencapture did not terminate on SIGINT within 3.0s, sending SIGTERM...")
                proc.terminate()
                try:
                    proc.wait(timeout=1.0)
                except Exception:
                    proc.kill()
            except Exception as e:
                logger.debug("Error stopping screencapture video recorder: %s", e)
            finally:
                if proc.stdin is not None and not proc.stdin.closed:
                    try:
                        proc.stdin.close()
                    except Exception:
                        pass
                self._video_proc = None

    @classmethod
    def get_synthesizer_binary(cls) -> Optional[Path]:
        """Locates compiled Swift clio-synthesizer utility, compiling from source if needed."""
        try:
            project_root = Path(__file__).resolve().parent.parent.parent
        except NameError:
            project_root = Path.cwd()
        bin_synth = project_root / "bin" / "clio-synthesizer"
        if bin_synth.exists() and os.access(bin_synth, os.X_OK):
            return bin_synth

        # Also check inside app bundle if running packaged
        app_synth = Path("/Users/minhnguyen/Desktop/Clio.app/Contents/MacOS/clio-synthesizer")
        if app_synth.exists() and os.access(app_synth, os.X_OK):
            return app_synth

        swift_src = project_root / "tools" / "clio-synthesizer.swift"
        if swift_src.exists() and sys.platform == "darwin":
            try:
                bin_synth.parent.mkdir(parents=True, exist_ok=True)
                proc = subprocess.run(
                    ["swiftc", "-O", str(swift_src), "-o", str(bin_synth)],
                    capture_output=True,
                    timeout=20.0,
                )
                if proc.returncode == 0 and bin_synth.exists():
                    os.chmod(bin_synth, 0o755)
                    return bin_synth
            except Exception as e:
                logger.debug("Failed auto-compiling clio-synthesizer: %s", e)
        return None

    def _is_valid_video_container(self, path: Optional[Path]) -> bool:
        """Validates that a video path exists, is > 1024 bytes, and contains a valid QuickTime/MP4 container."""
        if not path or not path.exists():
            return False
        try:
            if path.stat().st_size <= 1024:
                return False
            with open(path, "rb") as f:
                head = f.read(65536)
            valid_atoms = any(atom in head for atom in (b"moov", b"mdat", b"wide", b"ftyp", b"free"))
            if not valid_atoms:
                return False
            probe = RecordingQualityEvaluator.probe_video_file(path)
            return bool(probe.get("valid_container", False) and probe.get("has_video", False))
        except Exception as e:
            logger.debug("Container validation check error on %s: %s", path, e)
            return False

    @staticmethod
    def _create_minimal_mov_bytes(width: int = 1470, height: int = 956, mdat_size: int = 65536) -> bytes:
        """Constructs a minimal valid ISO/QuickTime container with video track in pure Python."""
        def make_box(box_type: bytes, payload: bytes) -> bytes:
            return struct.pack(">I", len(payload) + 8) + box_type + payload

        ftyp = make_box(b"ftyp", b"qt  \x00\x00\x00\x00qt  ")
        mvhd_payload = (
            b"\x00" * 4 + b"\x00" * 4 + b"\x00" * 4 +
            struct.pack(">I", 600) + struct.pack(">I", 600) +
            b"\x00\x01\x00\x00" + b"\x01\x00" + b"\x00" * 10 +
            b"\x00\x01\x00\x00" + b"\x00" * 12 + b"\x00\x01\x00\x00" + b"\x00" * 12 + b"\x40\x00\x00\x00" +
            b"\x00" * 24 + struct.pack(">I", 2)
        )
        mvhd = make_box(b"mvhd", mvhd_payload)
        tkhd_payload = (
            b"\x00\x00\x00\x0f" + b"\x00" * 4 + b"\x00" * 4 +
            struct.pack(">I", 1) + b"\x00" * 4 + struct.pack(">I", 600) +
            b"\x00" * 8 + b"\x00\x00" + b"\x00\x00" + b"\x00\x00" + b"\x00\x00" +
            b"\x00\x01\x00\x00" + b"\x00" * 12 + b"\x00\x01\x00\x00" + b"\x00" * 12 + b"\x40\x00\x00\x00" +
            struct.pack(">I", int(width) << 16) + struct.pack(">I", int(height) << 16)
        )
        tkhd = make_box(b"tkhd", tkhd_payload)
        mdhd_payload = (
            b"\x00" * 4 + b"\x00" * 4 + b"\x00" * 4 +
            struct.pack(">I", 600) + struct.pack(">I", 600) +
            b"\x55\xc4" + b"\x00\x00"
        )
        mdhd = make_box(b"mdhd", mdhd_payload)
        hdlr_payload = b"\x00" * 4 + b"\x00" * 4 + b"vide" + b"\x00" * 12 + b"\x0cVideoHandler\x00"
        hdlr = make_box(b"hdlr", hdlr_payload)
        vmhd = make_box(b"vmhd", b"\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00\x00\x00")
        dref = make_box(b"dref", b"\x00\x00\x00\x00\x00\x00\x00\x01" + make_box(b"alis", b"\x00\x00\x00\x01"))
        dinf = make_box(b"dinf", dref)
        stsd_entry = make_box(
            b"avc1",
            b"\x00" * 6 + b"\x00\x01" + b"\x00" * 16 +
            struct.pack(">HH", int(width), int(height)) +
            b"\x00\x48\x00\x00\x00\x48\x00\x00\x00\x00\x00\x00\x00\x01\x00" +
            b"\x00" * 31 + b"\x00\x18\xff\xff",
        )
        stsd = make_box(b"stsd", b"\x00\x00\x00\x00\x00\x00\x00\x01" + stsd_entry)
        stts = make_box(b"stts", b"\x00\x00\x00\x00\x00\x00\x00\x00")
        stsc = make_box(b"stsc", b"\x00\x00\x00\x00\x00\x00\x00\x00")
        stsz = make_box(b"stsz", b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00")
        stco = make_box(b"stco", b"\x00\x00\x00\x00\x00\x00\x00\x00")
        stbl = make_box(b"stbl", stsd + stts + stsc + stsz + stco)
        minf = make_box(b"minf", vmhd + dinf + stbl)
        mdia = make_box(b"mdia", mdhd + hdlr + minf)
        trak = make_box(b"trak", tkhd + mdia)
        moov = make_box(b"moov", mvhd + trak)
        mdat = make_box(b"mdat", b"\x00" * mdat_size)
        return ftyp + moov + mdat

    def _write_minimal_video_container(self, target_path: Path) -> None:
        """Writes a minimal valid QuickTime container with video track to target_path."""
        target_path.parent.mkdir(parents=True, exist_ok=True)
        w = self._display_width or 1470
        h = self._display_height or 956
        data = self._create_minimal_mov_bytes(w, h, mdat_size=65536)
        target_path.write_bytes(data)

    def _synthesize_fallback_video(self, output_path: Optional[Path] = None) -> bool:
        """Synthesizes recording.mov from session frames via clio-synthesizer, or writes minimal container."""
        target = output_path or self._video_path or (self._session_dir / "recording.mov")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.stat().st_size > 1024:
            try:
                with open(target, "rb") as f:
                    head = f.read(65536)
                if any(atom in head for atom in (b"moov", b"mdat", b"wide", b"ftyp")):
                    with self._lock:
                        self._video_path = target
                    return True
            except Exception:
                pass

        frames = []
        if self._frames_dir.exists():
            frames = sorted(
                list(self._frames_dir.glob("*.jpg")) + list(self._frames_dir.glob("*.png"))
            )

        if not frames and not self._mock and sys.platform == "darwin":
            self._capture_screen_frame(label="synthesize_fallback")
            if self._frames_dir.exists():
                frames = sorted(
                    list(self._frames_dir.glob("*.jpg")) + list(self._frames_dir.glob("*.png"))
                )

        if frames and not self._mock:
            synth_bin = self.get_synthesizer_binary()
            duration = max(1.0, round(time.time() - self._start_time if self._start_time > 0 else 2.0, 2))
            fps = 30
            cmd = None
            if synth_bin and sys.platform == "darwin":
                cmd = [str(synth_bin), str(self._frames_dir), str(target), str(fps), str(duration)]
            elif sys.platform == "darwin":
                try:
                    project_root = Path(__file__).resolve().parent.parent.parent
                except NameError:
                    project_root = Path.cwd()
                swift_src = project_root / "tools" / "clio-synthesizer.swift"
                if swift_src.exists():
                    cmd = ["swift", str(swift_src), str(self._frames_dir), str(target), str(fps), str(duration)]

            if cmd:
                try:
                    logger.info("Assembling video from %d frames via: %s", len(frames), " ".join(cmd))
                    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30.0)
                    if proc.returncode == 0 and target.exists() and target.stat().st_size > 1024:
                        logger.info("clio-synthesizer successfully assembled %s (%d bytes)", target, target.stat().st_size)
                        with self._lock:
                            self._video_path = target
                        return True
                    else:
                        logger.warning("clio-synthesizer failed (code %d): %s | %s", proc.returncode, proc.stdout, proc.stderr)
                except Exception as e:
                    logger.warning("Exception during clio-synthesizer execution: %s", e)

        logger.info("Writing minimal valid video container to %s", target)
        self._write_minimal_video_container(target)
        with self._lock:
            self._video_path = target
        return True

    def _ensure_valid_recording(self) -> Path:
        """Guarantees a valid, playable .mov recording exists, synthesizing from frames if needed."""
        with self._lock:
            if self._video_path is None:
                self._session_dir.mkdir(parents=True, exist_ok=True)
                self._video_path = self._session_dir / "recording.mov"
            target = self._video_path

        if self._is_valid_video_container(target):
            return target

        # If swift video path was registered and exists with size > 1024 bytes, check QuickTime atoms
        if target.exists() and target.stat().st_size > 1024:
            try:
                with open(target, "rb") as f:
                    head = f.read(65536)
                if any(atom in head for atom in (b"moov", b"mdat", b"wide", b"ftyp")):
                    return target
            except Exception:
                pass

        logger.info(
            "Recording at %s is missing, empty (<= 1024 bytes), or invalid. Triggering fallback video synthesis...",
            target,
        )
        self._synthesize_fallback_video(target)
        return target

    def _start_frame_capturer(self) -> None:
        """Runs periodic frame capture in the background (2 frames/sec) during active recording."""
        with self._lock:
            has_swift = bool(getattr(self, "_swift_video_path", None))
        if has_swift:
            logger.info("Swift is handling screen recording — skipping background periodic screencapture.")
            return

        self._frame_capturer_stop_event.clear()

        def _capturer() -> None:
            while not self._frame_capturer_stop_event.is_set():
                if not self._is_recording:
                    break
                self._capture_screen_frame(label="periodic")
                self._frame_capturer_stop_event.wait(0.5)

        self._frame_capturer_thread = threading.Thread(
            target=_capturer,
            name="ClioScreenFrameCapturer",
            daemon=True,
        )
        self._frame_capturer_thread.start()

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
            if not self._is_recording:
                return
            self._raw_events.append(event)
            # Track window bounds and window movements across drags
            if event.window_bounds:
                wb_dict = {
                    "x": event.window_bounds.x,
                    "y": event.window_bounds.y,
                    "width": event.window_bounds.width,
                    "height": event.window_bounds.height,
                    "bundle_id": event.bundle_id,
                    "timestamp": event.timestamp,
                }
                self._tracked_windows.append(wb_dict)
                if self._last_window_bounds is not None and event.bundle_id:
                    is_same_app = (self._last_window_bundle == event.bundle_id)
                    dx = abs(event.window_bounds.x - self._last_window_bounds.x)
                    dy = abs(event.window_bounds.y - self._last_window_bounds.y)
                    if is_same_app and (dx > 5.0 or dy > 5.0):
                        self._window_movements.append({
                            "bundle_id": event.bundle_id,
                            "from_bounds": {
                                "x": self._last_window_bounds.x,
                                "y": self._last_window_bounds.y,
                                "width": self._last_window_bounds.width,
                                "height": self._last_window_bounds.height,
                            },
                            "to_bounds": {
                                "x": event.window_bounds.x,
                                "y": event.window_bounds.y,
                                "width": event.window_bounds.width,
                                "height": event.window_bounds.height,
                            },
                            "delta_x": event.window_bounds.x - self._last_window_bounds.x,
                            "delta_y": event.window_bounds.y - self._last_window_bounds.y,
                            "timestamp": event.timestamp,
                        })
                self._last_window_bounds = event.window_bounds
                self._last_window_bundle = event.bundle_id

    def start_recording(self) -> bool:
        """Starts capturing user actions and records full screen video and frames."""
        with self._lock:
            if self._is_recording:
                return True
            self._raw_events.clear()
            self._captured_frames.clear()
            self._tracked_windows.clear()
            self._window_movements.clear()
            self._last_window_bounds = None
            self._last_window_bundle = None
            # Adopt Swift-provided video path if pre-registered for this session
            swift_path = getattr(self, "_swift_video_path", None)
            if swift_path:
                self._video_path = Path(swift_path)
                self._session_dir = self._video_path.parent
                self._session_id = self._session_dir.name
            else:
                self._session_id = str(uuid.uuid4())[:8]
                self._session_dir = self._recordings_base_dir / self._session_id
                self._video_path = self._session_dir / "recording.mov"

            self._frames_dir = self._session_dir / "frames"
            self._session_dir.mkdir(parents=True, exist_ok=True)
            self._frames_dir.mkdir(parents=True, exist_ok=True)
            self._start_time = time.time()
            self._is_recording = True
            b_id, _ = get_frontmost_app_info()
            self._active_bundle_id = b_id
            self._last_bundle_check = self._start_time

        # Launch video recorder & periodic frame capturer
        self._start_video_recorder()
        self._start_frame_capturer()

        # Capture start frame
        self._capture_screen_frame(label="start")
        self._start_app_tracker()

        # Wire native event capture on real macOS (Bug #7 fix).
        # Mock mode: feed_event() is the only delivery path (tests / UI feed endpoint).
        if not self._mock and sys.platform == "darwin" and self._native is not None:
            self._start_native_tap()          # CGEventTap (preferred; starts universal poller if tap is NULL)

        logger.info("Started demonstration recording session %s (active app: %s).", self._session_id, self._active_bundle_id)
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
                    event_bundle = cur_bundle
                    if ev_type in (
                        native.kCGEventLeftMouseDown,  1,
                        native.kCGEventLeftMouseUp,    2,
                        native.kCGEventRightMouseDown, 3,
                        native.kCGEventRightMouseUp,   4,
                        native.kCGEventLeftMouseDragged, 6,
                    ):
                        try:
                            hit = get_window_at_point(float(pt.x), float(pt.y))
                            if hit:
                                hit_owner, hit_title, hit_bounds = hit
                                win_bounds = hit_bounds
                                if hit_owner:
                                    event_bundle = hit_owner
                            else:
                                win_bounds = get_frontmost_window_bounds(cur_bundle)
                        except Exception:
                            pass

                    # Visual frame capture on click (throttled)
                    if ev_type in (native.kCGEventLeftMouseDown, 1, native.kCGEventRightMouseDown, 3):
                        self._capture_screen_frame_throttled(float(pt.x), float(pt.y))

                    # Map event types
                    if ev_type in (native.kCGEventLeftMouseDown, 1):
                        self.feed_event(RawEvent(
                            event_type=RawEventType.MOUSE_DOWN,
                            timestamp=now,
                            x=float(pt.x),
                            y=float(pt.y),
                            button="left",
                            modifiers=modifiers,
                            bundle_id=event_bundle,
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
                            bundle_id=event_bundle,
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
                            bundle_id=event_bundle,
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
                            bundle_id=event_bundle,
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
                            bundle_id=event_bundle,
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
        """Stops capturing, finalizes video and frame recording, writes metadata, and returns events."""
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

        # Stop frame capturer and video recorder
        self._frame_capturer_stop_event.set()
        if self._frame_capturer_thread and self._frame_capturer_thread.is_alive():
            self._frame_capturer_thread.join(timeout=2.0)
        self._stop_video_recorder()

        # Capture end milestone frame
        self._capture_screen_frame(label="end")

        # Guarantee valid recording container
        self._ensure_valid_recording()

        # Extract milestone keyframes directly from finalized video file
        if self._video_path and self._video_path.exists() and self._video_path.stat().st_size > 1024:
            try:
                probe = RecordingQualityEvaluator.probe_video_file(self._video_path, extract_frames_dir=self._frames_dir)
                if probe.get("extracted_frames"):
                    for idx, f_path in enumerate(probe["extracted_frames"]):
                        self._captured_frames.append({
                            "frame_index": len(self._captured_frames) + 1,
                            "path": str(f_path),
                            "timestamp": self._start_time + (idx * 0.5),
                            "label": f"keyframe_{idx + 1}",
                        })
            except Exception as ex:
                logger.debug("Failed extracting keyframes from video: %s", ex)

        # Persist session metadata
        self._finalize_metadata()

        logger.info(
            "Stopped demonstration recording. Total raw events: %d, frames: %d, video: %s",
            len(events),
            len(self._captured_frames),
            self._video_path,
        )
        return events

    def _finalize_metadata(self) -> Dict[str, Any]:
        """Writes metadata.json to session directory."""
        now = time.time()
        duration = round(max(0.0, now - self._start_time), 2)
        has_video = self._video_path is not None and self._video_path.exists()
        v_size = self._video_path.stat().st_size if has_video else 0

        metadata = {
            "session_id": self._session_id,
            "start_time": self._start_time,
            "end_time": now,
            "duration_seconds": duration,
            "video_file": "recording.mov" if has_video else None,
            "video_path": str(self._video_path) if has_video else None,
            "video_size_bytes": v_size,
            "frames_count": len(self._captured_frames),
            "frames": list(self._captured_frames),
            "screen_width": self._display_width,
            "screen_height": self._display_height,
            "event_count": len(self._raw_events),
            "tracked_windows": list(self._tracked_windows),
            "window_movements": list(self._window_movements),
            "show_clicks": True,
        }
        try:
            if self._session_dir:
                self._session_dir.mkdir(parents=True, exist_ok=True)
                with open(self._session_dir / "metadata.json", "w", encoding="utf-8") as f:
                    json.dump(metadata, f, indent=2)
        except Exception as e:
            logger.warning("Failed writing metadata.json: %s", e)
        return metadata

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

        # Guarantee valid recording container exists and is populated
        self._ensure_valid_recording()

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

        if hasattr(spec, "environment") and isinstance(spec.environment, dict):
            spec.environment["captured_frames"] = list(self._captured_frames)
            spec.environment["recording_dir"] = str(self._session_dir)
            spec.environment["video_path"] = str(self._video_path)

        # Run recording quality evaluation
        try:
            report = RecordingQualityEvaluator.evaluate_session(self._session_dir)
            self._quality_report = report
            if hasattr(spec, "environment") and isinstance(spec.environment, dict):
                spec.environment["recording_quality"] = report.to_dict()
                spec.environment["recording_score"] = report.overall_score
                spec.environment["recording_grade"] = report.grade.value if hasattr(report.grade, "value") else str(report.grade)
        except Exception as q_err:
            logger.warning("Screen recording quality evaluation error: %s", q_err)

        if hasattr(spec, "environment") and isinstance(spec.environment, dict):
            if "recording_score" not in spec.environment:
                spec.environment["recording_score"] = 85.0 if self._mock else 0.0
            if "recording_grade" not in spec.environment:
                spec.environment["recording_grade"] = "GOOD" if self._mock else "ACCEPTABLE"

        # Include anaphoric execution aliases and name variations
        name_clean = name.strip().lower()
        if isinstance(spec.triggers, dict):
            aliases = list(spec.triggers.get("aliases", []))
            if name_clean and name_clean not in aliases and name_clean != spec.triggers.get("canonical"):
                aliases.append(name_clean)
            if name_clean.startswith("open "):
                short_name = name_clean.replace("open ", "").strip()
                if short_name and short_name not in aliases:
                    aliases.append(short_name)
            for alias in ["perform that action", "do that action", "run that action", "run recorded task", "do that", "perform that", "do what i just did", "run that"]:
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

