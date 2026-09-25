"""MacOSCoreGraphicsActuator Implementation.

Path: src/actuators/macos.py

Implements FEAT-ACT-02, FEAT-ACT-03, FEAT-ACT-04, FEAT-ACT-05, FEAT-ACT-06, FEAT-ACT-07.
Zero external dependencies: 100% pure standard library ctypes bindings to macOS frameworks.
"""

from __future__ import annotations

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
    c_ulong,
    c_void_p,
    create_string_buffer,
)
import logging
import math
import os
import subprocess
import sys
import time
from typing import List, Optional, Tuple

from src.actuators.base import BaseActuator
from src.actuators.failsafe import FailsafeWatchdog
from src.actuators.types import (
    ActuatorError,
    ApplicationLaunchError,
    FailsafeEmergencyStop,
    InputSynthesisError,
    MouseButton,
    WindowInfo,
    WindowNotFoundError,
    is_screen_corner,
    normalize_mouse_button,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CoreGraphics Geometric Structures (64-bit Darwin)
# ---------------------------------------------------------------------------
class CGPoint(Structure):
    _fields_ = [("x", c_double), ("y", c_double)]

class CGSize(Structure):
    _fields_ = [("width", c_double), ("height", c_double)]

class CGRect(Structure):
    _fields_ = [("origin", CGPoint), ("size", CGSize)]

# ---------------------------------------------------------------------------
# CoreGraphics Enumerations & Constants
# ---------------------------------------------------------------------------
kCGNullWindowID = 0
kCGWindowListOptionAll = 0
kCGWindowListOptionOnScreenOnly = 1
kCGWindowListExcludeDesktopElements = 16

kCGHIDEventTap = 0
kCGSessionEventTap = 1
kCGAnnotatedSessionEventTap = 2

kCGMouseButtonLeft = 0
kCGMouseButtonRight = 1
kCGMouseButtonCenter = 2

kCGEventNull = 0
kCGEventLeftMouseDown = 1
kCGEventLeftMouseUp = 2
kCGEventRightMouseDown = 3
kCGEventRightMouseUp = 4
kCGEventMouseMoved = 5
kCGEventLeftMouseDragged = 6
kCGEventRightMouseDragged = 7
kCGEventKeyDown = 10
kCGEventKeyUp = 11
kCGEventFlagsChanged = 12
kCGEventScrollWheel = 22
kCGEventOtherMouseDown = 25
kCGEventOtherMouseUp = 26
kCGEventOtherMouseDragged = 27

kCGMouseEventClickState = 1
kCGScrollEventUnitLine = 1

kCGEventFlagMaskAlphaShift = 0x00010000     # Caps Lock
kCGEventFlagMaskShift      = 0x00020000     # Shift
kCGEventFlagMaskControl    = 0x00040000     # Control
kCGEventFlagMaskAlternate  = 0x00080000     # Option / Alt
kCGEventFlagMaskCommand    = 0x00100000     # Command
kCGEventFlagMaskSecondaryFn = 0x00800000    # Fn

kCFStringEncodingUTF8 = 0x08000100
kCFNumberSInt32Type = 3
kCFNumberSInt64Type = 4
kCFNumberIntType = 9

NSApplicationActivateAllWindows = 1 << 0
NSApplicationActivateIgnoringOtherApps = 1 << 1

# ---------------------------------------------------------------------------
# macOS Virtual Keycodes Table
# ---------------------------------------------------------------------------
VIRTUAL_KEYCODES: dict[str, int] = {
    # Letters (QWERTY layout)
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7,
    "c": 8, "v": 9, "b": 11, "q": 12, "w": 13, "e": 14, "r": 15,
    "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22,
    "5": 23, "=": 24, "9": 25, "7": 26, "-": 27, "8": 28, "0": 29,
    "]": 30, "o": 31, "u": 32, "[": 33, "i": 34, "p": 35, "l": 37,
    "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44,
    "n": 45, "m": 46, ".": 47, "`": 50,

    # Whitespace & Control Keys
    "return": 36, "enter": 36, "\n": 36, "\r": 36,
    "tab": 48, "\t": 48,
    "space": 49, " ": 49,
    "backspace": 51, "delete": 51,
    "escape": 53, "esc": 53,

    # Modifiers
    "command": 55, "cmd": 55,
    "shift": 56,
    "capslock": 57,
    "option": 58, "alt": 58, "opt": 58,
    "control": 59, "ctrl": 59,
    "right_command": 54, "right_cmd": 54,
    "right_shift": 60,
    "right_option": 61, "right_alt": 61,
    "right_control": 62, "right_ctrl": 62,

    # Navigation & Editing
    "left": 123, "right": 124, "down": 125, "up": 126,
    "home": 115, "end": 119,
    "pageup": 116, "pagedown": 121,
    "forwarddelete": 117,

    # Function Keys
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96,
    "f6": 97, "f7": 98, "f8": 100, "f9": 101, "f10": 109,
    "f11": 103, "f12": 111,
}

MODIFIER_MASKS: dict[str, int] = {
    "cmd": kCGEventFlagMaskCommand,
    "command": kCGEventFlagMaskCommand,
    "shift": kCGEventFlagMaskShift,
    "alt": kCGEventFlagMaskAlternate,
    "option": kCGEventFlagMaskAlternate,
    "opt": kCGEventFlagMaskAlternate,
    "ctrl": kCGEventFlagMaskControl,
    "control": kCGEventFlagMaskControl,
}


class MacOSNativeBindings:
    """Manages loaded dynamic libraries and explicit function prototypes."""

    def __init__(self) -> None:
        try:
            self.cg = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
            self.cf = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
            self.appkit = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/AppKit.framework/AppKit")
            self.ax = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
            self.objc = ctypes.cdll.LoadLibrary("/usr/lib/libobjc.A.dylib")
        except OSError as e:
            raise RuntimeError(f"Failed to load macOS system frameworks: {e}") from e

        self._init_coregraphics()
        self._init_corefoundation()
        self._init_objc()
        self._init_accessibility()

    def _init_coregraphics(self) -> None:
        cg = self.cg
        cg.CGMainDisplayID.argtypes = []
        cg.CGMainDisplayID.restype = c_uint32

        cg.CGDisplayBounds.argtypes = [c_uint32]
        cg.CGDisplayBounds.restype = CGRect

        cg.CGEventCreate.argtypes = [c_void_p]
        cg.CGEventCreate.restype = c_void_p

        cg.CGEventGetLocation.argtypes = [c_void_p]
        cg.CGEventGetLocation.restype = CGPoint

        cg.CGEventCreateMouseEvent.argtypes = [c_void_p, c_uint32, CGPoint, c_uint32]
        cg.CGEventCreateMouseEvent.restype = c_void_p

        cg.CGEventCreateKeyboardEvent.argtypes = [c_void_p, c_uint16, c_bool]
        cg.CGEventCreateKeyboardEvent.restype = c_void_p

        cg.CGEventCreateScrollWheelEvent2.argtypes = [c_void_p, c_uint32, c_uint32, c_int32, c_int32, c_int32]
        cg.CGEventCreateScrollWheelEvent2.restype = c_void_p

        cg.CGEventKeyboardSetUnicodeString.argtypes = [c_void_p, c_uint32, POINTER(c_uint16)]
        cg.CGEventKeyboardSetUnicodeString.restype = None

        cg.CGEventSetFlags.argtypes = [c_void_p, c_uint64]
        cg.CGEventSetFlags.restype = None

        cg.CGEventGetFlags.argtypes = [c_void_p]
        cg.CGEventGetFlags.restype = c_uint64

        cg.CGEventSetIntegerValueField.argtypes = [c_void_p, c_uint32, c_int64]
        cg.CGEventSetIntegerValueField.restype = None

        cg.CGEventPost.argtypes = [c_uint32, c_void_p]
        cg.CGEventPost.restype = None

        cg.CGWindowListCopyWindowInfo.argtypes = [c_uint32, c_uint32]
        cg.CGWindowListCopyWindowInfo.restype = c_void_p

        cg.CGRectMakeWithDictionaryRepresentation.argtypes = [c_void_p, POINTER(CGRect)]
        cg.CGRectMakeWithDictionaryRepresentation.restype = c_bool

    def _init_corefoundation(self) -> None:
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

        cf.CFStringGetLength.argtypes = [c_void_p]
        cf.CFStringGetLength.restype = c_int

        cf.CFStringGetMaximumSizeForEncoding.argtypes = [c_int, c_uint32]
        cf.CFStringGetMaximumSizeForEncoding.restype = c_int

        cf.CFStringGetCString.argtypes = [c_void_p, c_char_p, c_int, c_uint32]
        cf.CFStringGetCString.restype = c_bool

        cf.CFNumberGetValue.argtypes = [c_void_p, c_int, c_void_p]
        cf.CFNumberGetValue.restype = c_bool

    def _init_objc(self) -> None:
        objc = self.objc
        objc.objc_getClass.argtypes = [c_char_p]
        objc.objc_getClass.restype = c_void_p

        objc.sel_registerName.argtypes = [c_char_p]
        objc.sel_registerName.restype = c_void_p

        self.msgSend_obj = ctypes.cast(objc.objc_msgSend, ctypes.CFUNCTYPE(c_void_p, c_void_p, c_void_p))
        self.msgSend_str = ctypes.cast(objc.objc_msgSend, ctypes.CFUNCTYPE(c_char_p, c_void_p, c_void_p))
        self.msgSend_ulong = ctypes.cast(objc.objc_msgSend, ctypes.CFUNCTYPE(c_ulong, c_void_p, c_void_p))
        self.msgSend_long = ctypes.cast(objc.objc_msgSend, ctypes.CFUNCTYPE(c_int64, c_void_p, c_void_p))
        self.msgSend_obj_str = ctypes.cast(objc.objc_msgSend, ctypes.CFUNCTYPE(c_void_p, c_void_p, c_void_p, c_char_p))
        self.msgSend_obj_obj = ctypes.cast(objc.objc_msgSend, ctypes.CFUNCTYPE(c_void_p, c_void_p, c_void_p, c_void_p))
        self.msgSend_obj_ulong = ctypes.cast(objc.objc_msgSend, ctypes.CFUNCTYPE(c_void_p, c_void_p, c_void_p, c_ulong))
        self.msgSend_bool_ulong = ctypes.cast(objc.objc_msgSend, ctypes.CFUNCTYPE(c_bool, c_void_p, c_void_p, c_ulong))
        self.msgSend_bool_obj_obj = ctypes.cast(objc.objc_msgSend, ctypes.CFUNCTYPE(c_bool, c_void_p, c_void_p, c_void_p, c_void_p))

    def _init_accessibility(self) -> None:
        self.ax.AXIsProcessTrusted.argtypes = []
        self.ax.AXIsProcessTrusted.restype = c_bool


class MacOSActuator(BaseActuator):
    """Production macOS desktop actuator utilizing CoreGraphics and AppKit."""

    def __init__(self, enable_watchdog: bool = True, watchdog_hz: float = 50.0) -> None:
        if sys.platform != "darwin":
            raise ActuatorError(f"MacOSActuator is only supported on macOS (detected {sys.platform})")

        self.native = MacOSNativeBindings()
        self._stopped = False
        self._init_cf_keys()

        # Failsafe Watchdog Daemon (FEAT-ACT-08 integration)
        self.enable_watchdog = enable_watchdog
        self._watchdog: Optional[FailsafeWatchdog] = None
        if enable_watchdog:
            self._watchdog = FailsafeWatchdog(
                poll_frequency_hz=watchdog_hz,
                margin=5.0,
                on_emergency=None,
                install_signals=True,
            )
            self._watchdog.start()

    def _init_cf_keys(self) -> None:
        """Caches CFStringRef keys for CGWindowList inspection."""
        cf = self.native.cf
        def mk_cf_str(s: str) -> int:
            return cf.CFStringCreateWithCString(None, s.encode("utf-8"), kCFStringEncodingUTF8)
        self._kCGWindowNumber = mk_cf_str("kCGWindowNumber")
        self._kCGWindowOwnerName = mk_cf_str("kCGWindowOwnerName")
        self._kCGWindowName = mk_cf_str("kCGWindowName")
        self._kCGWindowBounds = mk_cf_str("kCGWindowBounds")
        self._kCGWindowLayer = mk_cf_str("kCGWindowLayer")

    def _cf_to_str(self, cf_str_ref: int) -> str:
        if not cf_str_ref:
            return ""
        cf = self.native.cf
        length = cf.CFStringGetLength(cf_str_ref)
        max_size = cf.CFStringGetMaximumSizeForEncoding(length, kCFStringEncodingUTF8) + 1
        buf = create_string_buffer(max_size)
        if cf.CFStringGetCString(cf_str_ref, buf, max_size, kCFStringEncodingUTF8):
            return buf.value.decode("utf-8", errors="replace")
        return ""

    def _cf_to_int(self, cf_num_ref: int) -> int:
        if not cf_num_ref:
            return 0
        cf = self.native.cf
        val = c_int64(0)
        cf.CFNumberGetValue(cf_num_ref, kCFNumberSInt64Type, byref(val))
        return val.value

    # -----------------------------------------------------------------------
    # Failsafe Methods (Contract Implementation)
    # -----------------------------------------------------------------------
    def is_failsafe_triggered(self) -> bool:
        # Only use watchdog hardware failsafe — mouse-corner check removed because
        # get_mouse_position() now uses subprocess (too slow to call at 50Hz in watchdog)
        if self._watchdog and self._watchdog.is_triggered:
            return True
        return False

    def check_failsafe(self) -> None:
        if self.is_failsafe_triggered():
            self._release_all_synthetic_modifiers()
            raise FailsafeEmergencyStop(
                message="Emergency stop: watchdog triggered",
                position=(0.0, 0.0),
                trigger_reason="watchdog",
            )

    def arm_failsafe(self) -> None:
        """Arms the failsafe watchdog during automated execution."""
        if self._watchdog and hasattr(self._watchdog, "arm"):
            self._watchdog.arm()

    def disarm_failsafe(self) -> None:
        """Disarms the failsafe watchdog during user demonstration and idle periods."""
        if self._watchdog and hasattr(self._watchdog, "disarm"):
            self._watchdog.disarm()

    def reset_failsafe(self) -> None:
        """Resets the failsafe trigger state."""
        if self._watchdog and hasattr(self._watchdog, "reset"):
            self._watchdog.reset()

    def stop(self) -> None:
        """Stops the actuator, halts watchdog, releases modifiers, and cleans up CF references.

        Guaranteed to be idempotent and memory-safe across multiple invocations.
        """
        if getattr(self, "_stopped", False):
            return
        self._stopped = True

        if self._watchdog:
            self._watchdog.stop()
            self._watchdog = None
        self._release_all_synthetic_modifiers()

        cf = self.native.cf
        cf_keys = [
            "_kCGWindowNumber",
            "_kCGWindowOwnerName",
            "_kCGWindowName",
            "_kCGWindowBounds",
            "_kCGWindowLayer",
        ]
        for attr in cf_keys:
            key = getattr(self, attr, None)
            if key:
                cf.CFRelease(key)
                setattr(self, attr, None)

    def _release_synthetic_keyboard_modifiers(self) -> None:
        """Cleans up held keyboard modifier keys (Cmd, Shift, Option, Ctrl) without touching mouse."""
        cg = self.native.cg
        cf = self.native.cf
        for code in [55, 54, 56, 60, 58, 61, 59, 62]:
            ev = cg.CGEventCreateKeyboardEvent(None, code, False)
            if ev:
                cg.CGEventSetFlags(ev, 0)
                cg.CGEventPost(kCGHIDEventTap, ev)
                cf.CFRelease(ev)

    def _release_synthetic_mouse_buttons(self) -> None:
        """Releases held synthetic mouse buttons at origin (safe no-op position)."""
        cg = self.native.cg
        cf = self.native.cf
        pt = CGPoint(0.0, 0.0)  # Safe position — avoids get_mouse_position() subprocess during shutdown
        for ev_type, btn in [
            (kCGEventLeftMouseUp, kCGMouseButtonLeft),
            (kCGEventRightMouseUp, kCGMouseButtonRight),
            (kCGEventOtherMouseUp, kCGMouseButtonCenter),
        ]:
            ev = cg.CGEventCreateMouseEvent(None, ev_type, pt, btn)
            if ev:
                cg.CGEventPost(kCGHIDEventTap, ev)
                cf.CFRelease(ev)

    def _release_all_synthetic_modifiers(self) -> None:
        """Cleans up all held modifier keys and mouse buttons (used on shutdown & failsafe)."""
        self._release_synthetic_keyboard_modifiers()
        self._release_synthetic_mouse_buttons()

    # -----------------------------------------------------------------------
    # FEAT-ACT-03: AppLauncherAndFocus
    # -----------------------------------------------------------------------
    def launch_app(self, app_name_or_bundle: str, timeout: float = 5.0, background: bool = False) -> bool:
        """Launch an app by name or bundle ID. Uses subprocess exclusively to avoid GIL deadlock."""
        self.check_failsafe()

        target_lower = app_name_or_bundle.lower()
        short_target = target_lower.split(".")[-1] if "." in target_lower else target_lower

        if not background:
            # Quick check: already frontmost? (single ctypes call, fast)
            try:
                front = self.get_frontmost_app().lower()
                if short_target in front or front in short_target:
                    return True
            except Exception:
                pass

        is_bundle = "." in app_name_or_bundle
        cmd = ["open"]
        if background:
            cmd.append("-g")  # Launches without bringing application to foreground
        cmd.extend(["-b" if is_bundle else "-a", app_name_or_bundle])
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=timeout)
            if res.returncode != 0:
                # Try alternate: open by short name
                fallback_cmd = ["open"]
                if background:
                    fallback_cmd.append("-g")
                fallback_cmd.extend(["-a", short_target.capitalize()])
                subprocess.run(fallback_cmd, capture_output=True, text=True, check=False, timeout=timeout)
        except Exception as e:
            if isinstance(e, ApplicationLaunchError):
                raise
            logger.warning("launch_app subprocess failed for '%s': %s", app_name_or_bundle, e)

        # Release GIL: sleep lets HTTP server threads run while app launches
        time.sleep(0.8)
        self.check_failsafe()
        return True

    def focus_app(self, app_name_or_bundle: str) -> bool:
        """Bring app to front via NSRunningApplication, fallback to launch_app."""
        self.check_failsafe()

        target_lower = app_name_or_bundle.lower()
        short_target = target_lower.split(".")[-1] if "." in target_lower else target_lower

        # If it's a bundle ID, try direct activation via Objective-C (fast path)
        if "." in app_name_or_bundle:
            try:
                objc = self.native.objc
                NSRunningApplication = objc.objc_getClass(b"NSRunningApplication")
                NSString = objc.objc_getClass(b"NSString")
                sel_str = objc.sel_registerName(b"stringWithUTF8String:")
                sel_apps_bundle = objc.sel_registerName(b"runningApplicationsWithBundleIdentifier:")
                sel_first = objc.sel_registerName(b"firstObject")
                sel_activate = objc.sel_registerName(b"activateWithOptions:")

                ns_str = self.native.msgSend_obj_str(NSString, sel_str, app_name_or_bundle.encode("utf-8"))
                apps = self.native.msgSend_obj_obj(NSRunningApplication, sel_apps_bundle, ns_str)
                first_app = self.native.msgSend_obj(apps, sel_first)
                if first_app:
                    self.native.msgSend_bool_ulong(first_app, sel_activate, NSApplicationActivateIgnoringOtherApps)
                    time.sleep(0.3)  # GIL-releasing: let HTTP threads run
                    return True
            except Exception as e:
                logger.debug("NSRunningApplication direct activation failed: %s", e)

        # Fallback: use subprocess 'open' — always GIL-safe
        return self.launch_app(app_name_or_bundle, timeout=3.0)

    def open_url(self, url: str) -> bool:
        """Opens a URL in a new browser tab using standard macOS launch services."""
        self.check_failsafe()
        if not url:
            return False
        clean_url = url.strip()
        lower_url = clean_url.lower()
        if lower_url.startswith("file://") or lower_url.startswith("javascript:") or lower_url.startswith("data:"):
            logger.warning("Rejected disallowed URL scheme in open_url: %s", clean_url)
            return False
        if not (clean_url.startswith("http://") or clean_url.startswith("https://")):
            clean_url = f"https://{clean_url}"
        try:
            res = subprocess.run(["open", clean_url], check=False, capture_output=True, timeout=3.0)
            return res.returncode == 0
        except Exception as e:
            logger.warning("Failed to open URL %s: %s", clean_url, e)
            return False

    def get_frontmost_app(self) -> str:
        try:
            objc = self.native.objc
            NSWorkspace = objc.objc_getClass(b"NSWorkspace")
            sel_shared = objc.sel_registerName(b"sharedWorkspace")
            sel_front = objc.sel_registerName(b"frontmostApplication")
            sel_bundle = objc.sel_registerName(b"bundleIdentifier")
            sel_loc_name = objc.sel_registerName(b"localizedName")
            sel_utf8 = objc.sel_registerName(b"UTF8String")

            ws = self.native.msgSend_obj(NSWorkspace, sel_shared)
            app = self.native.msgSend_obj(ws, sel_front)
            if app:
                bundle_ns = self.native.msgSend_obj(app, sel_bundle)
                if bundle_ns:
                    b_str = self.native.msgSend_str(bundle_ns, sel_utf8)
                    if b_str:
                        return b_str.decode("utf-8")
                name_ns = self.native.msgSend_obj(app, sel_loc_name)
                if name_ns:
                    n_str = self.native.msgSend_str(name_ns, sel_utf8)
                    if n_str:
                        return n_str.decode("utf-8")
        except Exception as e:
            logger.debug("NSWorkspace frontmostApplication error: %s", e)

        try:
            res = subprocess.run(["lsappinfo", "info", "-only", "bundleID", "front"],
                                 capture_output=True, text=True, check=False)
            if "bundleID=" in res.stdout:
                return res.stdout.split("bundleID=")[1].split('"')[1]
        except Exception:
            pass
        return ""

    # -----------------------------------------------------------------------
    # FEAT-ACT-04: WindowGeometryInspector
    # -----------------------------------------------------------------------
    def get_windows(self, app_name: Optional[str] = None) -> List[WindowInfo]:
        if getattr(self, "_stopped", False):
            return []
        cg = self.native.cg
        cf = self.native.cf

        win_list = cg.CGWindowListCopyWindowInfo(
            kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements,
            kCGNullWindowID,
        )
        if not win_list:
            return []

        count = cf.CFArrayGetCount(win_list)
        results: List[WindowInfo] = []

        try:
            for i in range(count):
                win_dict = cf.CFArrayGetValueAtIndex(win_list, i)
                if not win_dict:
                    continue

                owner = self._cf_to_str(cf.CFDictionaryGetValue(win_dict, self._kCGWindowOwnerName))
                title = self._cf_to_str(cf.CFDictionaryGetValue(win_dict, self._kCGWindowName))
                wid = self._cf_to_int(cf.CFDictionaryGetValue(win_dict, self._kCGWindowNumber))
                layer = self._cf_to_int(cf.CFDictionaryGetValue(win_dict, self._kCGWindowLayer))

                rect_dict = cf.CFDictionaryGetValue(win_dict, self._kCGWindowBounds)
                rect = CGRect()
                if rect_dict and cg.CGRectMakeWithDictionaryRepresentation(rect_dict, byref(rect)):
                    if rect.size.width <= 0 or rect.size.height <= 0:
                        continue

                    if app_name:
                        target = app_name.lower()
                        short_target = target.split(".")[-1] if "." in target else target
                        owner_l = owner.lower()
                        title_l = title.lower()
                        if (
                            target not in owner_l
                            and target not in title_l
                            and short_target not in owner_l
                            and short_target not in title_l
                        ):
                            continue

                    results.append(
                        WindowInfo(
                            window_id=wid,
                            owner_name=owner,
                            title=title,
                            x=rect.origin.x,
                            y=rect.origin.y,
                            width=rect.size.width,
                            height=rect.size.height,
                            layer=layer,
                            is_on_screen=True,
                        )
                    )
        finally:
            cf.CFRelease(win_list)

        results.sort(key=lambda w: (w.layer != 0, -(w.width * w.height)))
        return results

    def get_screen_size(self) -> Tuple[float, float]:
        cg = self.native.cg
        main_id = cg.CGMainDisplayID()
        bounds = cg.CGDisplayBounds(main_id)
        return (bounds.size.width, bounds.size.height)

    def get_mouse_position(self) -> Tuple[float, float]:
        """Returns current mouse position via fast native CoreGraphics event inspection."""
        try:
            cg = self.native.cg
            cf = self.native.cf
            ev = cg.CGEventCreate(None)
            if ev:
                pt = cg.CGEventGetLocation(ev)
                cf.CFRelease(ev)
                return (float(pt.x), float(pt.y))
        except Exception:
            pass
        try:
            # Fallback to osascript if native CoreGraphics inspection fails
            result = subprocess.run(
                ["osascript", "-e",
                 "tell application \"System Events\" to get the position of the mouse"],
                capture_output=True, text=True, timeout=1.0, check=False
            )
            if result.returncode == 0 and result.stdout.strip():
                parts = result.stdout.strip().split(",")
                if len(parts) == 2:
                    return (float(parts[0].strip()), float(parts[1].strip()))
        except Exception:
            pass
        return (0.0, 0.0)

    # -----------------------------------------------------------------------
    # FEAT-ACT-05: MouseSynthesisEngine
    # -----------------------------------------------------------------------
    def move_mouse(
        self,
        x: float,
        y: float,
        smooth: bool = False,
        duration: float = 0.0,
    ) -> None:
        if not (math.isfinite(x) and math.isfinite(y)):
            raise InputSynthesisError(f"Invalid non-finite mouse coordinates: ({x}, {y})")
        self.check_failsafe()

        cg = self.native.cg
        cf = self.native.cf

        if smooth and duration > 0:
            # Start smooth move from origin (0,0) to target — only used by actuator (non-VC) path
            start_x, start_y = 0.0, 0.0
            steps = max(5, int(duration * 60))
            step_delay = duration / steps
            for i in range(1, steps + 1):
                self.check_failsafe()
                t = i / steps
                smooth_t = 3 * (t ** 2) - 2 * (t ** 3)
                cur_x = start_x + (x - start_x) * smooth_t
                cur_y = start_y + (y - start_y) * smooth_t
                ev = cg.CGEventCreateMouseEvent(None, kCGEventMouseMoved, CGPoint(cur_x, cur_y), kCGMouseButtonLeft)
                if ev:
                    cg.CGEventPost(kCGHIDEventTap, ev)
                    cf.CFRelease(ev)
                time.sleep(step_delay)

        self.check_failsafe()
        ev = cg.CGEventCreateMouseEvent(None, kCGEventMouseMoved, CGPoint(x, y), kCGMouseButtonLeft)
        if not ev:
            raise InputSynthesisError(f"Failed to create mouse move event at ({x}, {y})")
        cg.CGEventPost(kCGHIDEventTap, ev)
        cf.CFRelease(ev)

    def click(
        self,
        x: Optional[float] = None,
        y: Optional[float] = None,
        button: MouseButton = MouseButton.LEFT,
        click_count: int = 1,
    ) -> None:
        if x is not None and y is not None:
            cur_x, cur_y = float(x), float(y)
        else:
            cur_x, cur_y = 0.0, 0.0

        btn_norm = normalize_mouse_button(button)
        if btn_norm == "left":
            down_type, up_type, cg_btn = kCGEventLeftMouseDown, kCGEventLeftMouseUp, kCGMouseButtonLeft
        elif btn_norm == "right":
            down_type, up_type, cg_btn = kCGEventRightMouseDown, kCGEventRightMouseUp, kCGMouseButtonRight
        elif btn_norm == "center":
            down_type, up_type, cg_btn = kCGEventOtherMouseDown, kCGEventOtherMouseUp, kCGMouseButtonCenter
        else:
            raise InputSynthesisError(f"Unsupported mouse button: {button}")

        cg = self.native.cg
        cf = self.native.cf

        for i in range(1, click_count + 1):
            self.check_failsafe()
            ev_down = cg.CGEventCreateMouseEvent(None, down_type, CGPoint(cur_x, cur_y), cg_btn)
            if not ev_down:
                raise InputSynthesisError("Failed to create mouse down event")
            cg.CGEventSetIntegerValueField(ev_down, kCGMouseEventClickState, i)
            cg.CGEventPost(kCGHIDEventTap, ev_down)
            cf.CFRelease(ev_down)

            time.sleep(0.02)

            self.check_failsafe()
            ev_up = cg.CGEventCreateMouseEvent(None, up_type, CGPoint(cur_x, cur_y), cg_btn)
            if not ev_up:
                raise InputSynthesisError("Failed to create mouse up event")
            cg.CGEventSetIntegerValueField(ev_up, kCGMouseEventClickState, i)
            cg.CGEventPost(kCGHIDEventTap, ev_up)
            cf.CFRelease(ev_up)

            if i < click_count:
                time.sleep(0.05)
        # Note: no cursor position restore — actuator click is only used when virtual cursor is OFF

    def drag(
        self,
        start_x: float,
        start_y: float,
        end_x: float,
        end_y: float,
        duration: float = 0.3,
    ) -> None:
        if not (math.isfinite(start_x) and math.isfinite(start_y) and math.isfinite(end_x) and math.isfinite(end_y)):
            raise InputSynthesisError(
                f"Invalid non-finite drag coordinates: ({start_x}, {start_y}) -> ({end_x}, {end_y})"
            )
        self.move_mouse(start_x, start_y)
        self.check_failsafe()

        cg = self.native.cg
        cf = self.native.cf

        ev_down = cg.CGEventCreateMouseEvent(None, kCGEventLeftMouseDown, CGPoint(start_x, start_y), kCGMouseButtonLeft)
        if not ev_down:
            raise InputSynthesisError("Failed to create drag start mouse down event")
        cg.CGEventPost(kCGHIDEventTap, ev_down)
        cf.CFRelease(ev_down)

        time.sleep(0.02)

        steps = max(5, int(duration * 60))
        step_delay = duration / steps
        for i in range(1, steps + 1):
            self.check_failsafe()
            t = i / steps
            cx = start_x + (end_x - start_x) * t
            cy = start_y + (end_y - start_y) * t
            ev_drag = cg.CGEventCreateMouseEvent(None, kCGEventLeftMouseDragged, CGPoint(cx, cy), kCGMouseButtonLeft)
            if ev_drag:
                cg.CGEventPost(kCGHIDEventTap, ev_drag)
                cf.CFRelease(ev_drag)
            time.sleep(step_delay)

        self.check_failsafe()
        ev_up = cg.CGEventCreateMouseEvent(None, kCGEventLeftMouseUp, CGPoint(end_x, end_y), kCGMouseButtonLeft)
        if not ev_up:
            raise InputSynthesisError("Failed to create drag end mouse up event")
        cg.CGEventPost(kCGHIDEventTap, ev_up)
        cf.CFRelease(ev_up)

    def scroll(self, dx: int = 0, dy: int = 0) -> None:
        self.check_failsafe()
        cg = self.native.cg
        cf = self.native.cf

        ev = cg.CGEventCreateScrollWheelEvent2(None, kCGScrollEventUnitLine, 2, dy, dx, 0)
        if not ev:
            raise InputSynthesisError("Failed to create scroll wheel event")
        cg.CGEventPost(kCGHIDEventTap, ev)
        cf.CFRelease(ev)

    # -----------------------------------------------------------------------
    # FEAT-ACT-06: KeyboardAndHotkeyEngine
    # -----------------------------------------------------------------------
    def press_hotkey(self, *keys: str) -> None:
        if not keys:
            return

        mask = 0
        primary_key: Optional[str] = None

        for k in keys:
            k_lower = k.lower().strip()
            if k_lower in MODIFIER_MASKS:
                mask |= MODIFIER_MASKS[k_lower]
            else:
                primary_key = k_lower

        if not primary_key:
            return

        if primary_key not in VIRTUAL_KEYCODES:
            raise InputSynthesisError(f"Unsupported virtual key for hotkey: '{primary_key}'")

        self.check_failsafe()

        keycode = VIRTUAL_KEYCODES[primary_key]

        cg = self.native.cg
        cf = self.native.cf

        ev_down = cg.CGEventCreateKeyboardEvent(None, keycode, True)
        if not ev_down:
            raise InputSynthesisError(f"Failed to create keyboard down event for keycode {keycode}")
        if mask != 0:
            cg.CGEventSetFlags(ev_down, mask)
        cg.CGEventPost(kCGHIDEventTap, ev_down)
        cf.CFRelease(ev_down)

        time.sleep(0.02)

        self.check_failsafe()
        ev_up = cg.CGEventCreateKeyboardEvent(None, keycode, False)
        if not ev_up:
            raise InputSynthesisError(f"Failed to create keyboard up event for keycode {keycode}")
        if mask != 0:
            cg.CGEventSetFlags(ev_up, mask)
        cg.CGEventPost(kCGHIDEventTap, ev_up)
        cf.CFRelease(ev_up)

        time.sleep(0.02)
        self._release_synthetic_keyboard_modifiers()

    def type_text(self, text: str, interval: float = 0.02) -> None:
        self.check_failsafe()
        if not text:
            return

        cg = self.native.cg
        cf = self.native.cf

        for ch in text:
            self.check_failsafe()

            if ch in ("\n", "\r"):
                keycode = 36  # Return
                ev_d = cg.CGEventCreateKeyboardEvent(None, keycode, True)
                if ev_d:
                    cg.CGEventPost(kCGHIDEventTap, ev_d)
                    cf.CFRelease(ev_d)
                time.sleep(0.005)
                ev_u = cg.CGEventCreateKeyboardEvent(None, keycode, False)
                if ev_u:
                    cg.CGEventPost(kCGHIDEventTap, ev_u)
                    cf.CFRelease(ev_u)
            elif ch == "\t":
                keycode = 48  # Tab
                ev_d = cg.CGEventCreateKeyboardEvent(None, keycode, True)
                if ev_d:
                    cg.CGEventPost(kCGHIDEventTap, ev_d)
                    cf.CFRelease(ev_d)
                time.sleep(0.005)
                ev_u = cg.CGEventCreateKeyboardEvent(None, keycode, False)
                if ev_u:
                    cg.CGEventPost(kCGHIDEventTap, ev_u)
                    cf.CFRelease(ev_u)
            else:
                utf16_bytes = ch.encode("utf-16-le")
                code_units_count = len(utf16_bytes) // 2
                code_units = (c_uint16 * code_units_count).from_buffer_copy(utf16_bytes)

                ev_d = cg.CGEventCreateKeyboardEvent(None, 0, True)
                if ev_d:
                    cg.CGEventKeyboardSetUnicodeString(ev_d, code_units_count, code_units)
                    cg.CGEventPost(kCGHIDEventTap, ev_d)
                    cf.CFRelease(ev_d)

                time.sleep(0.005)

                ev_u = cg.CGEventCreateKeyboardEvent(None, 0, False)
                if ev_u:
                    cg.CGEventKeyboardSetUnicodeString(ev_u, code_units_count, code_units)
                    cg.CGEventPost(kCGHIDEventTap, ev_u)
                    cf.CFRelease(ev_u)

            if interval > 0:
                time.sleep(interval)

    # -----------------------------------------------------------------------
    # FEAT-ACT-07: PasteboardAccelerator
    # -----------------------------------------------------------------------
    def paste_text(self, text: str) -> None:
        self.check_failsafe()
        if not text:
            return

        copied = False
        try:
            objc = self.native.objc
            NSPasteboard = objc.objc_getClass(b"NSPasteboard")
            NSString = objc.objc_getClass(b"NSString")

            sel_gen = objc.sel_registerName(b"generalPasteboard")
            sel_clear = objc.sel_registerName(b"clearContents")
            sel_set_str = objc.sel_registerName(b"setString:forType:")
            sel_utf8_str = objc.sel_registerName(b"stringWithUTF8String:")

            pb = self.native.msgSend_obj(NSPasteboard, sel_gen)
            self.native.msgSend_long(pb, sel_clear)

            ns_text = self.native.msgSend_obj_str(NSString, sel_utf8_str, text.encode("utf-8"))
            ns_type = self.native.msgSend_obj_str(NSString, sel_utf8_str, b"public.utf8-plain-text")

            copied = bool(self.native.msgSend_bool_obj_obj(pb, sel_set_str, ns_text, ns_type))
        except Exception as e:
            logger.debug("NSPasteboard copy failed: %s; falling back to pbcopy", e)
            copied = False

        if not copied:
            try:
                subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True, timeout=2.0)
                copied = True
            except Exception as e:
                raise InputSynthesisError(f"Failed to copy text to pasteboard: {e}") from e

        time.sleep(0.03)
        self.press_hotkey("cmd", "v")
        time.sleep(0.05)
