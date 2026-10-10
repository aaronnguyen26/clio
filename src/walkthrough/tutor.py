"""Pedagogical Walkthrough Tutor & State Machine Controller.

Path: src/walkthrough/tutor.py
Orchestrates on-screen desktop walkthroughs, managing step transitions,
virtual cursor choreography, element highlighting, and user interaction feedback.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import logging
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.actuators.virtual_cursor import VirtualCursor, VirtualCursorState
from src.executor.events import EventType, ExecutionBus, ExecutionEvent
from src.walkthrough.grounder import ScreenGrounder
from src.walkthrough.models import (
    TeachingAction,
    WalkthroughMode,
    WalkthroughPlan,
    WalkthroughStep,
)

logger = logging.getLogger(__name__)


class WalkthroughStatus(str, Enum):
    """Lifecycle states of the walkthrough tutor."""
    IDLE = "IDLE"
    READY = "READY"
    NAVIGATING = "NAVIGATING"
    HIGHLIGHTING = "HIGHLIGHTING"
    DEMONSTRATING = "DEMONSTRATING"
    WAITING_FOR_USER = "WAITING_FOR_USER"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    ERROR = "ERROR"


@dataclass
class StepEvaluationResult:
    """Result of evaluating whether a walkthrough step's UI effect completed."""
    completed: bool
    attempts: int = 1
    verification_method: str = "default"
    detail: str = ""


class StepCompletionEvaluator:
    """Evaluates and drives walkthrough step completion on live macOS UI before advancing."""

    def __init__(
        self,
        grounder: Optional[ScreenGrounder] = None,
        mock: bool = False,
        max_retries: int = 3,
        retry_delay: float = 0.18,
        custom_verifier: Optional[Callable[[WalkthroughStep, int], bool]] = None,
        require_completion_before_advance: bool = False,
    ) -> None:
        self.grounder = grounder
        self.mock = mock
        self.max_retries = max(1, int(max_retries))
        self.retry_delay = float(retry_delay)
        self.custom_verifier = custom_verifier
        self.require_completion_before_advance = require_completion_before_advance
        self._pid_cache: Dict[str, Tuple[int, float]] = {}
        self._last_switched_pid: Optional[int] = None

    @staticmethod
    def _norm(s: Optional[str]) -> str:
        if not s:
            return ""
        return (
            s.replace("\u2026", "...")
            .replace("\u2011", "-")
            .replace("\u2013", "-")
            .replace("\u2014", "-")
            .strip()
            .lower()
        )

    def _get_ax_bindings(self) -> Optional[Tuple[Any, Any]]:
        if sys.platform != "darwin":
            return None
        try:
            import ctypes
            from ctypes import c_bool, c_char_p, c_float, c_int, c_void_p

            hiservices = ctypes.cdll.LoadLibrary(
                "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
            )
            cf = ctypes.cdll.LoadLibrary(
                "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
            )
            hiservices.AXUIElementCreateSystemWide.argtypes = []
            hiservices.AXUIElementCreateSystemWide.restype = c_void_p
            hiservices.AXUIElementCopyElementAtPosition.argtypes = [
                c_void_p,
                c_float,
                c_float,
                ctypes.POINTER(c_void_p),
            ]
            hiservices.AXUIElementCopyElementAtPosition.restype = c_int
            hiservices.AXUIElementCreateApplication.argtypes = [c_int]
            hiservices.AXUIElementCreateApplication.restype = c_void_p
            hiservices.AXUIElementCopyAttributeValue.argtypes = [
                c_void_p,
                c_void_p,
                ctypes.POINTER(c_void_p),
            ]
            hiservices.AXUIElementCopyAttributeValue.restype = c_int
            hiservices.AXUIElementPerformAction.argtypes = [c_void_p, c_void_p]
            hiservices.AXUIElementPerformAction.restype = c_int

            cf.CFStringCreateWithCString.argtypes = [c_void_p, c_char_p, c_int]
            cf.CFStringCreateWithCString.restype = c_void_p
            cf.CFStringGetCString.argtypes = [c_void_p, c_char_p, c_int, c_int]
            cf.CFStringGetCString.restype = c_bool
            cf.CFGetTypeID.argtypes = [c_void_p]
            cf.CFGetTypeID.restype = ctypes.c_ulong
            cf.CFStringGetTypeID.argtypes = []
            cf.CFStringGetTypeID.restype = ctypes.c_ulong
            cf.CFArrayGetCount.argtypes = [c_void_p]
            cf.CFArrayGetCount.restype = c_int
            cf.CFArrayGetValueAtIndex.argtypes = [c_void_p, c_int]
            cf.CFArrayGetValueAtIndex.restype = c_void_p
            cf.CFBooleanGetValue.argtypes = [c_void_p]
            cf.CFBooleanGetValue.restype = c_bool
            cf.CFRelease.argtypes = [c_void_p]
            return hiservices, cf
        except Exception as e:
            logger.debug("StepCompletionEvaluator AX binding error: %s", e)
            return None

    def _resolve_pid(self, app_name: str) -> Optional[int]:
        if not app_name or sys.platform != "darwin":
            return None
        key = app_name.strip().lower()
        now = time.time()
        cached = self._pid_cache.get(key)
        if cached and (now - cached[1]) < 0.5:
            return cached[0]
        for flag in ["-x", "-xi", "-f", "-fi"]:
            try:
                out = (
                    subprocess.check_output(["pgrep", flag, app_name], stderr=subprocess.DEVNULL)
                    .decode()
                    .strip()
                    .splitlines()
                )
                if out:
                    pid = int(out[0].strip())
                    self._pid_cache[key] = (pid, now)
                    return pid
            except Exception:
                pass
        return None

    def _get_apple_menubar_elem(self) -> Optional[int]:
        """Returns a retained AXUIElementRef (as int) for the top-left Apple menu bar item."""
        bindings = self._get_ax_bindings()
        if not bindings:
            return None
        hiservices, cf = bindings
        import ctypes
        from ctypes import byref, c_void_p

        cf_role = cf.CFStringCreateWithCString(None, b"AXRole", 0x08000100)
        str_tid = cf.CFStringGetTypeID()
        sys_elem = hiservices.AXUIElementCreateSystemWide()
        if sys_elem:
            for px, py in ((14.0, 8.0), (18.0, 12.0)):
                apple_elem = c_void_p()
                if hiservices.AXUIElementCopyElementAtPosition(sys_elem, px, py, byref(apple_elem)) == 0 and apple_elem.value:
                    r_val = c_void_p()
                    r_str = ""
                    if hiservices.AXUIElementCopyAttributeValue(apple_elem.value, cf_role, byref(r_val)) == 0 and r_val.value:
                        if cf.CFGetTypeID(r_val) == str_tid:
                            buf = ctypes.create_string_buffer(128)
                            if cf.CFStringGetCString(r_val, buf, 128, 0x08000100):
                                r_str = buf.value.decode("utf-8", errors="ignore").strip().lower()
                        cf.CFRelease(r_val)
                    if r_str == "axmenubaritem":
                        cf.CFRelease(cf_role)
                        cf.CFRelease(sys_elem)
                        return int(apple_elem.value)
                    cf.CFRelease(apple_elem)
            cf.CFRelease(sys_elem)
        cf.CFRelease(cf_role)

        finder_pid = self._resolve_pid("Finder")
        if finder_pid:
            app_elem = hiservices.AXUIElementCreateApplication(finder_pid)
            if app_elem:
                cf_mb = cf.CFStringCreateWithCString(None, b"AXMenuBar", 0x08000100)
                cf_ch = cf.CFStringCreateWithCString(None, b"AXChildren", 0x08000100)
                mb_val = c_void_p()
                res_ptr = None
                if hiservices.AXUIElementCopyAttributeValue(app_elem, cf_mb, byref(mb_val)) == 0 and mb_val.value:
                    ch_val = c_void_p()
                    if hiservices.AXUIElementCopyAttributeValue(mb_val.value, cf_ch, byref(ch_val)) == 0 and ch_val.value:
                        if cf.CFArrayGetCount(ch_val.value) > 0:
                            item0 = cf.CFArrayGetValueAtIndex(ch_val.value, 0)
                            cf.CFRetain.argtypes = [c_void_p]
                            cf.CFRetain.restype = c_void_p
                            cf.CFRetain(item0)
                            res_ptr = int(item0)
                        cf.CFRelease(ch_val)
                    cf.CFRelease(mb_val)
                cf.CFRelease(cf_ch)
                cf.CFRelease(cf_mb)
                cf.CFRelease(app_elem)
                if res_ptr:
                    return res_ptr
        return None

    def is_apple_menu_open(self) -> bool:
        """Checks whether the top-left macOS Apple menu dropdown is currently open."""
        bindings = self._get_ax_bindings()
        if not bindings:
            return False
        hiservices, cf = bindings
        from ctypes import byref, c_void_p

        apple_ptr = self._get_apple_menubar_elem()
        if not apple_ptr:
            return False
        cf_sel = cf.CFStringCreateWithCString(None, b"AXSelected", 0x08000100)
        sel_val = c_void_p()
        is_open = False
        if hiservices.AXUIElementCopyAttributeValue(c_void_p(apple_ptr), cf_sel, byref(sel_val)) == 0 and sel_val.value:
            is_open = bool(cf.CFBooleanGetValue(sel_val.value))
            cf.CFRelease(sel_val)
        cf.CFRelease(cf_sel)
        cf.CFRelease(c_void_p(apple_ptr))
        return is_open

    def press_apple_menu(self) -> bool:
        """Opens the top-left macOS Apple menu visibly on screen via native AXPress."""
        if self.is_apple_menu_open():
            return True
        bindings = self._get_ax_bindings()
        ok = False
        if bindings:
            hiservices, cf = bindings
            from ctypes import c_void_p

            apple_ptr = self._get_apple_menubar_elem()
            if apple_ptr:
                cf_press = cf.CFStringCreateWithCString(None, b"AXPress", 0x08000100)
                rc = hiservices.AXUIElementPerformAction(c_void_p(apple_ptr), cf_press)
                ok = rc == 0
                cf.CFRelease(cf_press)
                cf.CFRelease(c_void_p(apple_ptr))
                time.sleep(0.06)

            # If Clio.app (windowless accessory) owned focus, activate Finder so the menu bar dropdown visibly renders
            if not self.is_apple_menu_open() and sys.platform == "darwin":
                try:
                    subprocess.run(
                        ["osascript", "-e", 'tell application "Finder" to activate'],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=0.35,
                    )
                    time.sleep(0.08)
                except Exception:
                    pass
                apple_ptr2 = self._get_apple_menubar_elem()
                if apple_ptr2:
                    cf_press2 = cf.CFStringCreateWithCString(None, b"AXPress", 0x08000100)
                    rc2 = hiservices.AXUIElementPerformAction(c_void_p(apple_ptr2), cf_press2)
                    ok = ok or (rc2 == 0)
                    cf.CFRelease(cf_press2)
                    cf.CFRelease(c_void_p(apple_ptr2))
                    time.sleep(0.06)
        if not ok and sys.platform == "darwin":
            try:
                as_cmd = 'tell application "System Events" to tell process "Finder" to click menu bar item 1 of menu bar 1'
                res = subprocess.run(["osascript", "-e", as_cmd], capture_output=True, timeout=0.4)
                ok = res.returncode == 0
            except Exception:
                pass
        return ok

    def ensure_window_on_primary_display(self, app_name: str) -> bool:
        """Moves the primary window of app_name onto the primary display if it is off-screen or on a secondary monitor."""
        pid = self._resolve_pid(app_name)
        if not pid:
            return False
        bindings = self._get_ax_bindings()
        if not bindings:
            return False
        hiservices, cf = bindings
        import ctypes
        from ctypes import Structure, byref, c_double, c_void_p

        class CGPoint(Structure):
            _fields_ = [("x", c_double), ("y", c_double)]

        class CGSize(Structure):
            _fields_ = [("width", c_double), ("height", c_double)]

        try:
            hiservices.AXValueGetValue.argtypes = [c_void_p, ctypes.c_int, c_void_p]
            hiservices.AXValueGetValue.restype = ctypes.c_bool
            hiservices.AXValueCreate.argtypes = [ctypes.c_int, c_void_p]
            hiservices.AXValueCreate.restype = c_void_p
            hiservices.AXUIElementSetAttributeValue.argtypes = [c_void_p, c_void_p, c_void_p]
            hiservices.AXUIElementSetAttributeValue.restype = ctypes.c_int
        except Exception:
            return False

        app_elem = hiservices.AXUIElementCreateApplication(pid)
        if not app_elem:
            return False
        cf_wins = cf.CFStringCreateWithCString(None, b"AXWindows", 0x08000100)
        cf_pos = cf.CFStringCreateWithCString(None, b"AXPosition", 0x08000100)
        cf_size = cf.CFStringCreateWithCString(None, b"AXSize", 0x08000100)
        wv = c_void_p()
        moved = False
        if hiservices.AXUIElementCopyAttributeValue(app_elem, cf_wins, byref(wv)) == 0 and wv.value:
            cnt = cf.CFArrayGetCount(wv.value)
            for i in range(cnt):
                win = cf.CFArrayGetValueAtIndex(wv.value, i)
                pv, sv = c_void_p(), c_void_p()
                pt, sz = CGPoint(), CGSize()
                has_p, has_s = False, False
                if hiservices.AXUIElementCopyAttributeValue(win, cf_pos, byref(pv)) == 0 and pv.value:
                    has_p = bool(hiservices.AXValueGetValue(pv, 1, byref(pt)))
                    cf.CFRelease(pv)
                if hiservices.AXUIElementCopyAttributeValue(win, cf_size, byref(sv)) == 0 and sv.value:
                    has_s = bool(hiservices.AXValueGetValue(sv, 2, byref(sz)))
                    cf.CFRelease(sv)
                if not (has_p and has_s):
                    continue
                if sz.width >= 1400.0 and sz.height >= 900.0:
                    continue
                if pt.x < 0.0 or pt.y < 24.0 or pt.x > 1200.0 or pt.y > 800.0:
                    if app_name.strip().lower() == "spotlight":
                        new_pt = CGPoint(435.0, 220.0)
                    else:
                        nx = max(60.0, min(float(pt.x), 450.0)) if pt.x >= 0.0 else 180.0
                        ny = max(60.0, min(float(pt.y), 420.0)) if pt.y >= 24.0 else 120.0
                        new_pt = CGPoint(nx, ny)
                    pos_val = hiservices.AXValueCreate(1, byref(new_pt))
                    if pos_val:
                        moved = hiservices.AXUIElementSetAttributeValue(win, cf_pos, pos_val) == 0
                        cf.CFRelease(pos_val)
                break
            cf.CFRelease(wv)
        for ref in (cf_size, cf_pos, cf_wins, app_elem):
            cf.CFRelease(ref)
        return moved

    def is_spotlight_open(self) -> bool:
        """Checks whether the macOS Spotlight search bar window is currently open on screen."""
        pid = self._resolve_pid("Spotlight")
        if not pid:
            return False
        bindings = self._get_ax_bindings()
        if not bindings:
            return False
        hiservices, cf = bindings
        from ctypes import byref, c_void_p

        app_elem = hiservices.AXUIElementCreateApplication(pid)
        if not app_elem:
            return False
        cf_wins = cf.CFStringCreateWithCString(None, b"AXWindows", 0x08000100)
        w_val = c_void_p()
        is_open = False
        if hiservices.AXUIElementCopyAttributeValue(app_elem, cf_wins, byref(w_val)) == 0 and w_val.value:
            is_open = cf.CFArrayGetCount(w_val.value) > 0
            cf.CFRelease(w_val)
        cf.CFRelease(cf_wins)
        cf.CFRelease(app_elem)
        return is_open

    def open_spotlight(self) -> bool:
        """Opens the macOS Spotlight search bar on the primary display."""
        if not self.is_spotlight_open():
            try:
                subprocess.run(
                    ["osascript", "-e", 'tell application "System Events" to keystroke space using {command down}'],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=0.5,
                )
                time.sleep(0.25)
            except Exception:
                pass
        if self.is_spotlight_open():
            self.ensure_window_on_primary_display("Spotlight")
            return True
        return False

    def close_spotlight(self) -> bool:
        """Closes the macOS Spotlight search bar if it is currently open."""
        if not self.is_spotlight_open():
            return True
        try:
            subprocess.run(
                ["osascript", "-e", 'tell application "System Events" to keystroke space using {command down}'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=0.5,
            )
            time.sleep(0.15)
        except Exception:
            pass
        return not self.is_spotlight_open()

    def get_spotlight_text(self) -> str:
        """Returns the current text inside the open Spotlight search field."""
        pid = self._resolve_pid("Spotlight")
        if not pid:
            return ""
        bindings = self._get_ax_bindings()
        if not bindings:
            return ""
        hiservices, cf = bindings
        import ctypes
        from ctypes import byref, c_void_p

        app_elem = hiservices.AXUIElementCreateApplication(pid)
        if not app_elem:
            return ""
        cf_wins = cf.CFStringCreateWithCString(None, b"AXWindows", 0x08000100)
        cf_ch = cf.CFStringCreateWithCString(None, b"AXChildren", 0x08000100)
        cf_role = cf.CFStringCreateWithCString(None, b"AXRole", 0x08000100)
        cf_val = cf.CFStringCreateWithCString(None, b"AXValue", 0x08000100)
        str_tid = cf.CFStringGetTypeID()
        text_val = ""

        def _get_s(el: Any, attr: Any) -> str:
            v = c_void_p()
            res = ""
            if hiservices.AXUIElementCopyAttributeValue(el, attr, byref(v)) == 0 and v.value:
                if cf.CFGetTypeID(v) == str_tid:
                    buf = ctypes.create_string_buffer(512)
                    if cf.CFStringGetCString(v, buf, 512, 0x08000100):
                        res = buf.value.decode("utf-8", errors="ignore")
                cf.CFRelease(v)
            return res

        wv = c_void_p()
        if hiservices.AXUIElementCopyAttributeValue(app_elem, cf_wins, byref(wv)) == 0 and wv.value:
            if cf.CFArrayGetCount(wv.value) > 0:
                win = cf.CFArrayGetValueAtIndex(wv.value, 0)
                cv = c_void_p()
                if hiservices.AXUIElementCopyAttributeValue(win, cf_ch, byref(cv)) == 0 and cv.value:
                    for i in range(cf.CFArrayGetCount(cv.value)):
                        c = cf.CFArrayGetValueAtIndex(cv.value, i)
                        if _get_s(c, cf_role) in ("AXTextField", "AXSearchField"):
                            text_val = _get_s(c, cf_val)
                            break
                    cf.CFRelease(cv)
            cf.CFRelease(wv)
        for ref in (cf_val, cf_role, cf_ch, cf_wins, app_elem):
            cf.CFRelease(ref)
        return text_val

    def type_into_spotlight(self, text: str) -> bool:
        """Opens Spotlight if needed and types text into the Spotlight search field."""
        clean_text = (text or "").rstrip("\r\n")
        if not clean_text:
            return self.open_spotlight()
        if not self.open_spotlight():
            return False
        current = self.get_spotlight_text()
        if current.strip().lower() == clean_text.strip().lower():
            return True
        escaped = clean_text.replace("\\", "\\\\").replace('"', '\\"')
        as_cmd = (
            'tell application "System Events"\n'
            'keystroke "a" using {command down}\n'
            f'keystroke "{escaped}"\n'
            "end tell"
        )
        try:
            subprocess.run(["osascript", "-e", as_cmd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=0.6)
            time.sleep(0.18)
        except Exception:
            pass
        return clean_text.strip().lower() in self.get_spotlight_text().strip().lower()

    def close_apple_menu(self) -> bool:
        """Closes the top-left macOS Apple menu if it is currently open."""
        if not self.is_apple_menu_open():
            return True
        bindings = self._get_ax_bindings()
        if not bindings:
            return False
        hiservices, cf = bindings
        from ctypes import byref, c_void_p

        apple_ptr = self._get_apple_menubar_elem()
        if not apple_ptr:
            return False
        cf_ch = cf.CFStringCreateWithCString(None, b"AXChildren", 0x08000100)
        ch_val = c_void_p()
        closed = False
        if hiservices.AXUIElementCopyAttributeValue(c_void_p(apple_ptr), cf_ch, byref(ch_val)) == 0 and ch_val.value:
            if cf.CFArrayGetCount(ch_val.value) > 0:
                menu = cf.CFArrayGetValueAtIndex(ch_val.value, 0)
                cf_cancel = cf.CFStringCreateWithCString(None, b"AXCancel", 0x08000100)
                closed = hiservices.AXUIElementPerformAction(menu, cf_cancel) == 0
                cf.CFRelease(cf_cancel)
            cf.CFRelease(ch_val)
        cf.CFRelease(cf_ch)
        cf.CFRelease(c_void_p(apple_ptr))
        return closed

    def press_apple_menu_item(self, item_title_substr: str) -> bool:
        """Clicks a menu item (e.g., 'System Settings...' or 'About This Mac') inside the Apple menu dropdown."""
        bindings = self._get_ax_bindings()
        if not bindings:
            return False
        hiservices, cf = bindings
        import ctypes
        from ctypes import byref, c_void_p

        target_norm = self._norm(item_title_substr).rstrip(".")
        cf_ch = cf.CFStringCreateWithCString(None, b"AXChildren", 0x08000100)
        cf_title = cf.CFStringCreateWithCString(None, b"AXTitle", 0x08000100)
        cf_press = cf.CFStringCreateWithCString(None, b"AXPress", 0x08000100)
        str_tid = cf.CFStringGetTypeID()

        def _press_in_apple_elem(apple_ptr_val: int) -> bool:
            ch_val = c_void_p()
            did_press = False
            if hiservices.AXUIElementCopyAttributeValue(c_void_p(apple_ptr_val), cf_ch, byref(ch_val)) == 0 and ch_val.value:
                if cf.CFArrayGetCount(ch_val.value) > 0:
                    menu_node = cf.CFArrayGetValueAtIndex(ch_val.value, 0)
                    m_ch = c_void_p()
                    if hiservices.AXUIElementCopyAttributeValue(menu_node, cf_ch, byref(m_ch)) == 0 and m_ch.value:
                        cnt = cf.CFArrayGetCount(m_ch.value)
                        for i in range(cnt):
                            mi = cf.CFArrayGetValueAtIndex(m_ch.value, i)
                            t_val = c_void_p()
                            t_str = ""
                            if hiservices.AXUIElementCopyAttributeValue(mi, cf_title, byref(t_val)) == 0 and t_val.value:
                                if cf.CFGetTypeID(t_val) == str_tid:
                                    buf = ctypes.create_string_buffer(512)
                                    if cf.CFStringGetCString(t_val, buf, 512, 0x08000100):
                                        t_str = self._norm(buf.value.decode("utf-8", errors="ignore"))
                                cf.CFRelease(t_val)
                            if t_str and target_norm in t_str:
                                did_press = hiservices.AXUIElementPerformAction(mi, cf_press) == 0
                                break
                        cf.CFRelease(m_ch)
                cf.CFRelease(ch_val)
            return did_press

        pressed = False
        apple_ptr = self._get_apple_menubar_elem()
        if apple_ptr:
            pressed = _press_in_apple_elem(apple_ptr)
            cf.CFRelease(c_void_p(apple_ptr))

        # Also invoke via Finder's AXMenuBar so About This Mac / System Settings works even if Clio was frontmost
        finder_pid = self._resolve_pid("Finder")
        if finder_pid:
            f_app = hiservices.AXUIElementCreateApplication(finder_pid)
            if f_app:
                cf_mb = cf.CFStringCreateWithCString(None, b"AXMenuBar", 0x08000100)
                mb_val = c_void_p()
                if hiservices.AXUIElementCopyAttributeValue(f_app, cf_mb, byref(mb_val)) == 0 and mb_val.value:
                    mb_ch = c_void_p()
                    if hiservices.AXUIElementCopyAttributeValue(mb_val.value, cf_ch, byref(mb_ch)) == 0 and mb_ch.value:
                        if cf.CFArrayGetCount(mb_ch.value) > 0:
                            f_apple = cf.CFArrayGetValueAtIndex(mb_ch.value, 0)
                            if _press_in_apple_elem(int(f_apple)):
                                pressed = True
                        cf.CFRelease(mb_ch)
                    cf.CFRelease(mb_val)
                cf.CFRelease(cf_mb)
                cf.CFRelease(f_app)

        for ref in (cf_press, cf_title, cf_ch):
            cf.CFRelease(ref)
        return pressed

    def is_app_window_open(self, app_name: str) -> bool:
        """Verifies that the target application is running and has an open window on screen."""
        if app_name and app_name.strip().lower() == "spotlight":
            return self.is_spotlight_open()
        if self.grounder:
            bounds = self.grounder.get_window_bounds(app_name)
            if bounds and len(bounds) == 4 and bounds[2] > 80 and bounds[3] > 80:
                return True
        pid = self._resolve_pid(app_name)
        if not pid:
            return False
        bindings = self._get_ax_bindings()
        if not bindings:
            return False
        hiservices, cf = bindings
        from ctypes import byref, c_void_p

        app_elem = hiservices.AXUIElementCreateApplication(pid)
        if not app_elem:
            return False
        cf_wins = cf.CFStringCreateWithCString(None, b"AXWindows", 0x08000100)
        w_val = c_void_p()
        has_win = False
        if hiservices.AXUIElementCopyAttributeValue(app_elem, cf_wins, byref(w_val)) == 0 and w_val.value:
            has_win = cf.CFArrayGetCount(w_val.value) > 0
            cf.CFRelease(w_val)
        cf.CFRelease(cf_wins)
        cf.CFRelease(app_elem)
        return has_win

    def get_app_window_title(self, app_name: str) -> str:
        """Returns the normalized AXTitle of the primary window of app_name."""
        pid = self._resolve_pid(app_name)
        if not pid:
            return ""
        bindings = self._get_ax_bindings()
        if not bindings:
            return ""
        hiservices, cf = bindings
        import ctypes
        from ctypes import byref, c_void_p

        app_elem = hiservices.AXUIElementCreateApplication(pid)
        if not app_elem:
            return ""
        cf_wins = cf.CFStringCreateWithCString(None, b"AXWindows", 0x08000100)
        cf_title = cf.CFStringCreateWithCString(None, b"AXTitle", 0x08000100)
        str_tid = cf.CFStringGetTypeID()
        w_val = c_void_p()
        win_title = ""
        if hiservices.AXUIElementCopyAttributeValue(app_elem, cf_wins, byref(w_val)) == 0 and w_val.value:
            if cf.CFArrayGetCount(w_val.value) > 0:
                win = cf.CFArrayGetValueAtIndex(w_val.value, 0)
                t_val = c_void_p()
                if hiservices.AXUIElementCopyAttributeValue(win, cf_title, byref(t_val)) == 0 and t_val.value:
                    if cf.CFGetTypeID(t_val) == str_tid:
                        buf = ctypes.create_string_buffer(512)
                        if cf.CFStringGetCString(t_val, buf, 512, 0x08000100):
                            win_title = self._norm(buf.value.decode("utf-8", errors="ignore"))
                    cf.CFRelease(t_val)
            cf.CFRelease(w_val)
        cf.CFRelease(cf_title)
        cf.CFRelease(cf_wins)
        cf.CFRelease(app_elem)
        return win_title

    def has_open_sheet(self, app_name: str) -> bool:
        """Checks whether the primary window of app_name currently has an open AXSheet."""
        pid = self._resolve_pid(app_name)
        if not pid:
            return False
        bindings = self._get_ax_bindings()
        if not bindings:
            return False
        hiservices, cf = bindings
        import ctypes
        from ctypes import byref, c_void_p

        app_elem = hiservices.AXUIElementCreateApplication(pid)
        if not app_elem:
            return False
        cf_wins = cf.CFStringCreateWithCString(None, b"AXWindows", 0x08000100)
        cf_ch = cf.CFStringCreateWithCString(None, b"AXChildren", 0x08000100)
        cf_role = cf.CFStringCreateWithCString(None, b"AXRole", 0x08000100)
        str_tid = cf.CFStringGetTypeID()
        w_val = c_void_p()
        found_sheet = False
        if hiservices.AXUIElementCopyAttributeValue(app_elem, cf_wins, byref(w_val)) == 0 and w_val.value:
            if cf.CFArrayGetCount(w_val.value) > 0:
                win = cf.CFArrayGetValueAtIndex(w_val.value, 0)
                ch_val = c_void_p()
                if hiservices.AXUIElementCopyAttributeValue(win, cf_ch, byref(ch_val)) == 0 and ch_val.value:
                    cnt = cf.CFArrayGetCount(ch_val.value)
                    for i in range(cnt):
                        child = cf.CFArrayGetValueAtIndex(ch_val.value, i)
                        r_val = c_void_p()
                        if hiservices.AXUIElementCopyAttributeValue(child, cf_role, byref(r_val)) == 0 and r_val.value:
                            if cf.CFGetTypeID(r_val) == str_tid:
                                buf = ctypes.create_string_buffer(128)
                                if cf.CFStringGetCString(r_val, buf, 128, 0x08000100):
                                    if buf.value.decode("utf-8", errors="ignore") == "AXSheet":
                                        found_sheet = True
                            cf.CFRelease(r_val)
                        if found_sheet:
                            break
                    cf.CFRelease(ch_val)
            cf.CFRelease(w_val)
        cf.CFRelease(cf_role)
        cf.CFRelease(cf_ch)
        cf.CFRelease(cf_wins)
        cf.CFRelease(app_elem)
        return found_sheet

    def dismiss_open_sheet(self, app_name: str) -> bool:
        """If app_name has an open AXSheet, dismisses it via 'Done', 'Cancel', or 'OK' button."""
        if not self.has_open_sheet(app_name):
            return False
        for btn_label in ("Done", "Cancel", "OK"):
            if self.press_window_element(app_name, "AXButton", btn_label):
                time.sleep(0.15)
                if not self.has_open_sheet(app_name):
                    return True
        return False

    def press_app_menu_bar_item(self, app_name: str, menu_title: str) -> Tuple[bool, bool]:
        """Presses an AXMenuBarItem (e.g., 'File', 'Finder', 'Bookmarks') and returns (pressed, is_open)."""
        pid = self._resolve_pid(app_name)
        if not pid:
            return False, False
        bindings = self._get_ax_bindings()
        if not bindings:
            return False, False
        hiservices, cf = bindings
        import ctypes
        from ctypes import byref, c_void_p

        target_norm = self._norm(menu_title)
        app_elem = hiservices.AXUIElementCreateApplication(pid)
        if not app_elem:
            return False, False
        cf_mb = cf.CFStringCreateWithCString(None, b"AXMenuBar", 0x08000100)
        cf_ch = cf.CFStringCreateWithCString(None, b"AXChildren", 0x08000100)
        cf_title = cf.CFStringCreateWithCString(None, b"AXTitle", 0x08000100)
        cf_sel = cf.CFStringCreateWithCString(None, b"AXSelected", 0x08000100)
        cf_press = cf.CFStringCreateWithCString(None, b"AXPress", 0x08000100)
        str_tid = cf.CFStringGetTypeID()

        mb_val = c_void_p()
        pressed = False
        is_open = False
        if hiservices.AXUIElementCopyAttributeValue(app_elem, cf_mb, byref(mb_val)) == 0 and mb_val.value:
            ch_val = c_void_p()
            if hiservices.AXUIElementCopyAttributeValue(mb_val.value, cf_ch, byref(ch_val)) == 0 and ch_val.value:
                cnt = cf.CFArrayGetCount(ch_val.value)
                for i in range(cnt):
                    mbi = cf.CFArrayGetValueAtIndex(ch_val.value, i)
                    t_val = c_void_p()
                    t_str = ""
                    if hiservices.AXUIElementCopyAttributeValue(mbi, cf_title, byref(t_val)) == 0 and t_val.value:
                        if cf.CFGetTypeID(t_val) == str_tid:
                            buf = ctypes.create_string_buffer(256)
                            if cf.CFStringGetCString(t_val, buf, 256, 0x08000100):
                                t_str = self._norm(buf.value.decode("utf-8", errors="ignore"))
                        cf.CFRelease(t_val)
                    if t_str and (t_str == target_norm or target_norm in t_str):
                        pressed = hiservices.AXUIElementPerformAction(mbi, cf_press) == 0
                        time.sleep(0.08)
                        sel_val = c_void_p()
                        if hiservices.AXUIElementCopyAttributeValue(mbi, cf_sel, byref(sel_val)) == 0 and sel_val.value:
                            is_open = bool(cf.CFBooleanGetValue(sel_val.value))
                            cf.CFRelease(sel_val)
                        break
                cf.CFRelease(ch_val)
            cf.CFRelease(mb_val)

        for ref in (cf_press, cf_sel, cf_title, cf_ch, cf_mb, app_elem):
            cf.CFRelease(ref)
        return pressed, is_open

    def press_app_menu_item(self, app_name: str, item_title: str) -> bool:
        """Finds and presses an AXMenuItem inside app_name's menu bar (e.g., View > Appearance)."""
        pid = self._resolve_pid(app_name)
        if not pid:
            return False
        bindings = self._get_ax_bindings()
        if not bindings:
            return False
        hiservices, cf = bindings
        import ctypes
        from ctypes import byref, c_void_p

        target_norm = self._norm(item_title)
        app_elem = hiservices.AXUIElementCreateApplication(pid)
        if not app_elem:
            return False
        cf_mb = cf.CFStringCreateWithCString(None, b"AXMenuBar", 0x08000100)
        cf_ch = cf.CFStringCreateWithCString(None, b"AXChildren", 0x08000100)
        cf_title = cf.CFStringCreateWithCString(None, b"AXTitle", 0x08000100)
        cf_role = cf.CFStringCreateWithCString(None, b"AXRole", 0x08000100)
        cf_press = cf.CFStringCreateWithCString(None, b"AXPress", 0x08000100)
        str_tid = cf.CFStringGetTypeID()

        def _search_menu(node: Any, depth: int = 0) -> bool:
            if depth > 6:
                return False
            r_val = c_void_p()
            r_str = ""
            if hiservices.AXUIElementCopyAttributeValue(node, cf_role, byref(r_val)) == 0 and r_val.value:
                if cf.CFGetTypeID(r_val) == str_tid:
                    buf = ctypes.create_string_buffer(128)
                    if cf.CFStringGetCString(r_val, buf, 128, 0x08000100):
                        r_str = self._norm(buf.value.decode("utf-8", errors="ignore"))
                cf.CFRelease(r_val)

            if r_str == "axmenuitem":
                t_val = c_void_p()
                t_str = ""
                if hiservices.AXUIElementCopyAttributeValue(node, cf_title, byref(t_val)) == 0 and t_val.value:
                    if cf.CFGetTypeID(t_val) == str_tid:
                        buf2 = ctypes.create_string_buffer(256)
                        if cf.CFStringGetCString(t_val, buf2, 256, 0x08000100):
                            t_str = self._norm(buf2.value.decode("utf-8", errors="ignore"))
                    cf.CFRelease(t_val)
                if t_str and (t_str == target_norm or t_str.rstrip(".") == target_norm.rstrip(".")):
                    if hiservices.AXUIElementPerformAction(node, cf_press) == 0:
                        return True

            ch_val = c_void_p()
            if hiservices.AXUIElementCopyAttributeValue(node, cf_ch, byref(ch_val)) == 0 and ch_val.value:
                cnt = cf.CFArrayGetCount(ch_val.value)
                for i in range(cnt):
                    child = cf.CFArrayGetValueAtIndex(ch_val.value, i)
                    if _search_menu(child, depth + 1):
                        cf.CFRelease(ch_val)
                        return True
                cf.CFRelease(ch_val)
            return False

        mb_val = c_void_p()
        pressed = False
        if hiservices.AXUIElementCopyAttributeValue(app_elem, cf_mb, byref(mb_val)) == 0 and mb_val.value:
            pressed = _search_menu(mb_val.value, 0)
            cf.CFRelease(mb_val)

        for ref in (cf_press, cf_role, cf_title, cf_ch, cf_mb, app_elem):
            cf.CFRelease(ref)
        return pressed

    def press_window_element(self, app_name: str, ax_role: str, ax_title: str) -> bool:
        """Finds a matching control in app_name's windows, scrolls it into view if needed, and executes AXPress."""
        pid = self._resolve_pid(app_name)
        if not pid:
            return False
        bindings = self._get_ax_bindings()
        if not bindings:
            return False
        hiservices, cf = bindings
        import ctypes
        from ctypes import byref, c_double, c_void_p

        target_title = self._norm(ax_title)
        target_role = self._norm(ax_role)
        if not target_title:
            return False

        try:
            hiservices.AXUIElementSetAttributeValue.argtypes = [c_void_p, c_void_p, c_void_p]
            hiservices.AXUIElementSetAttributeValue.restype = ctypes.c_int
            cf.CFNumberCreate.argtypes = [c_void_p, ctypes.c_int, c_void_p]
            cf.CFNumberCreate.restype = c_void_p
        except Exception:
            pass

        app_elem = hiservices.AXUIElementCreateApplication(pid)
        if not app_elem:
            return False
        cf_wins = cf.CFStringCreateWithCString(None, b"AXWindows", 0x08000100)
        cf_ch = cf.CFStringCreateWithCString(None, b"AXChildren", 0x08000100)
        cf_title = cf.CFStringCreateWithCString(None, b"AXTitle", 0x08000100)
        cf_desc = cf.CFStringCreateWithCString(None, b"AXDescription", 0x08000100)
        cf_val = cf.CFStringCreateWithCString(None, b"AXValue", 0x08000100)
        cf_role = cf.CFStringCreateWithCString(None, b"AXRole", 0x08000100)
        cf_scroll = cf.CFStringCreateWithCString(None, b"AXScrollToVisible", 0x08000100)
        cf_vsb = cf.CFStringCreateWithCString(None, b"AXVerticalScrollBar", 0x08000100)
        cf_press = cf.CFStringCreateWithCString(None, b"AXPress", 0x08000100)
        str_tid = cf.CFStringGetTypeID()
        visited = [0]
        cur_scroll_area: List[Any] = [None]

        def _scroll_area_to_bottom(sa: Any) -> None:
            if not sa:
                return
            try:
                one_val = c_double(1.0)
                num_one = cf.CFNumberCreate(None, 6, byref(one_val))
                if num_one:
                    vsb = c_void_p()
                    if hiservices.AXUIElementCopyAttributeValue(sa, cf_vsb, byref(vsb)) == 0 and vsb.value:
                        hiservices.AXUIElementSetAttributeValue(vsb.value, cf_val, num_one)
                        cf.CFRelease(vsb)
                    cf.CFRelease(num_one)
            except Exception:
                pass

        def _find_and_press(node: Any, depth: int = 0) -> bool:
            if depth > 15 or visited[0] > 1500:
                return False
            visited[0] += 1

            r_val = c_void_p()
            r_str = ""
            if hiservices.AXUIElementCopyAttributeValue(node, cf_role, byref(r_val)) == 0 and r_val.value:
                if cf.CFGetTypeID(r_val) == str_tid:
                    buf = ctypes.create_string_buffer(128)
                    if cf.CFStringGetCString(r_val, buf, 128, 0x08000100):
                        r_str = self._norm(buf.value.decode("utf-8", errors="ignore"))
                cf.CFRelease(r_val)

            prev_sa = cur_scroll_area[0]
            if r_str == "axscrollarea":
                cur_scroll_area[0] = node

            t_str = ""
            for attr in (cf_title, cf_desc, cf_val):
                t_val = c_void_p()
                if hiservices.AXUIElementCopyAttributeValue(node, attr, byref(t_val)) == 0 and t_val.value:
                    if cf.CFGetTypeID(t_val) == str_tid:
                        buf2 = ctypes.create_string_buffer(512)
                        if cf.CFStringGetCString(t_val, buf2, 512, 0x08000100):
                            t_str = self._norm(buf2.value.decode("utf-8", errors="ignore"))
                    cf.CFRelease(t_val)
                    if t_str:
                        break

            role_ok = (not target_role) or (target_role == r_str) or (
                target_role in ("axbutton", "axradiobutton", "axcheckbox")
                and r_str in ("axbutton", "axradiobutton", "axcheckbox", "axswitch", "axpopupbutton")
            )
            if role_ok and t_str and (t_str == target_title or t_str.rstrip(".") == target_title.rstrip(".")):
                hiservices.AXUIElementPerformAction(node, cf_scroll)
                if cur_scroll_area[0] is not None and any(k in target_title for k in ("system report", "hot corners")):
                    _scroll_area_to_bottom(cur_scroll_area[0])
                    time.sleep(0.06)
                if hiservices.AXUIElementPerformAction(node, cf_press) == 0:
                    cur_scroll_area[0] = prev_sa
                    return True

            ch_val = c_void_p()
            if hiservices.AXUIElementCopyAttributeValue(node, cf_ch, byref(ch_val)) == 0 and ch_val.value:
                cnt = cf.CFArrayGetCount(ch_val.value)
                for i in range(cnt):
                    child = cf.CFArrayGetValueAtIndex(ch_val.value, i)
                    if _find_and_press(child, depth + 1):
                        cf.CFRelease(ch_val)
                        cur_scroll_area[0] = prev_sa
                        return True
                cf.CFRelease(ch_val)
            cur_scroll_area[0] = prev_sa
            return False

        w_val = c_void_p()
        pressed = False
        if hiservices.AXUIElementCopyAttributeValue(app_elem, cf_wins, byref(w_val)) == 0 and w_val.value:
            w_cnt = cf.CFArrayGetCount(w_val.value)
            for k in range(w_cnt):
                win = cf.CFArrayGetValueAtIndex(w_val.value, k)
                if _find_and_press(win, 1):
                    pressed = True
                    break
            cf.CFRelease(w_val)

        for ref in (cf_press, cf_vsb, cf_scroll, cf_role, cf_val, cf_desc, cf_title, cf_ch, cf_wins, app_elem):
            cf.CFRelease(ref)
        return pressed

    def switch_visible_window(
        self,
        cycle_same_app: bool = False,
        activate_app_fn: Optional[Callable[[str], None]] = None,
    ) -> Optional[Tuple[str, Tuple[float, float, float, float]]]:
        """Visibly switches/moves between open application windows on the primary display without HID modifier lag."""
        if sys.platform != "darwin" or self.mock:
            return None
        bindings = self._get_ax_bindings()
        if not bindings:
            return None
        hiservices, cf = bindings
        import ctypes
        from ctypes import Structure, byref, c_double, c_int64, c_uint32, c_void_p

        class CGPoint(Structure):
            _fields_ = [("x", c_double), ("y", c_double)]

        class CGSize(Structure):
            _fields_ = [("width", c_double), ("height", c_double)]

        try:
            cg = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
            cg.CGWindowListCopyWindowInfo.argtypes = [c_uint32, c_uint32]
            cg.CGWindowListCopyWindowInfo.restype = c_void_p
            cf.CFDictionaryGetValue.argtypes = [c_void_p, c_void_p]
            cf.CFDictionaryGetValue.restype = c_void_p
            cf.CFNumberGetValue.argtypes = [c_void_p, ctypes.c_int, c_void_p]
            cf.CFNumberGetValue.restype = ctypes.c_bool
            hiservices.AXValueGetValue.argtypes = [c_void_p, ctypes.c_int, c_void_p]
            hiservices.AXValueGetValue.restype = ctypes.c_bool
        except Exception:
            return None

        key_owner = cf.CFStringCreateWithCString(None, b"kCGWindowOwnerName", 0x08000100)
        key_pid = cf.CFStringCreateWithCString(None, b"kCGWindowOwnerPID", 0x08000100)
        key_layer = cf.CFStringCreateWithCString(None, b"kCGWindowLayer", 0x08000100)
        str_tid = cf.CFStringGetTypeID()
        excluded = {
            "clio", "windowserver", "control center", "controlcenter",
            "dock", "systemuiserver", "notification center", " notificationcenter", "spotlight",
        }

        visible_apps: List[Tuple[str, int]] = []
        wlist = cg.CGWindowListCopyWindowInfo(17, 0)
        if wlist:
            try:
                cnt = cf.CFArrayGetCount(wlist)
                for i in range(cnt):
                    d = cf.CFArrayGetValueAtIndex(wlist, i)
                    l_ref = cf.CFDictionaryGetValue(d, key_layer)
                    if l_ref:
                        lv = c_int64(0)
                        cf.CFNumberGetValue(l_ref, 4, byref(lv))
                        if lv.value != 0:
                            continue
                    p_ref = cf.CFDictionaryGetValue(d, key_pid)
                    pid_val = c_int64(0)
                    if p_ref:
                        cf.CFNumberGetValue(p_ref, 4, byref(pid_val))
                    o_ref = cf.CFDictionaryGetValue(d, key_owner)
                    owner = ""
                    if o_ref and cf.CFGetTypeID(o_ref) == str_tid:
                        buf = ctypes.create_string_buffer(256)
                        if cf.CFStringGetCString(o_ref, buf, 256, 0x08000100):
                            owner = buf.value.decode("utf-8", errors="ignore").strip()
                    if owner and owner.lower() not in excluded and pid_val.value > 0:
                        if not any(p == int(pid_val.value) for _, p in visible_apps):
                            visible_apps.append((owner, int(pid_val.value)))
            finally:
                cf.CFRelease(wlist)

        for ref in (key_layer, key_pid, key_owner):
            cf.CFRelease(ref)

        def _get_ax_win_bounds(win_el: Any, cf_pos: Any, cf_size: Any) -> Optional[Tuple[float, float, float, float]]:
            pv, sv = c_void_p(), c_void_p()
            pt, sz = CGPoint(), CGSize()
            hp, hs = False, False
            if hiservices.AXUIElementCopyAttributeValue(win_el, cf_pos, byref(pv)) == 0 and pv.value:
                hp = bool(hiservices.AXValueGetValue(pv, 1, byref(pt)))
                cf.CFRelease(pv)
            if hiservices.AXUIElementCopyAttributeValue(win_el, cf_size, byref(sv)) == 0 and sv.value:
                hs = bool(hiservices.AXValueGetValue(sv, 2, byref(sz)))
                cf.CFRelease(sv)
            if hp and hs and sz.width >= 100.0 and sz.height >= 80.0:
                return (float(pt.x), float(pt.y), float(sz.width), float(sz.height))
            return None

        cf_wins = cf.CFStringCreateWithCString(None, b"AXWindows", 0x08000100)
        cf_pos = cf.CFStringCreateWithCString(None, b"AXPosition", 0x08000100)
        cf_size = cf.CFStringCreateWithCString(None, b"AXSize", 0x08000100)
        cf_raise = cf.CFStringCreateWithCString(None, b"AXRaise", 0x08000100)

        # 1. If cycling windows of the active app and it has >= 2 AXWindows, raise the last window to front
        if cycle_same_app and visible_apps:
            front_name, front_pid = visible_apps[0]
            app_el = hiservices.AXUIElementCreateApplication(front_pid)
            if app_el:
                wv = c_void_p()
                if hiservices.AXUIElementCopyAttributeValue(app_el, cf_wins, byref(wv)) == 0 and wv.value:
                    w_cnt = cf.CFArrayGetCount(wv.value)
                    if w_cnt >= 2:
                        target_w = cf.CFArrayGetValueAtIndex(wv.value, w_cnt - 1)
                        hiservices.AXUIElementPerformAction(target_w, cf_raise)
                        b = _get_ax_win_bounds(target_w, cf_pos, cf_size)
                        cf.CFRelease(wv)
                        cf.CFRelease(app_el)
                        for r in (cf_raise, cf_size, cf_pos, cf_wins):
                            cf.CFRelease(r)
                        if b:
                            return (front_name, b)
                    cf.CFRelease(wv)
                cf.CFRelease(app_el)

        # 2. Switch between distinct visible application windows
        chosen_name: Optional[str] = None
        chosen_pid: Optional[int] = None
        for idx, (app_n, app_p) in enumerate(visible_apps):
            if idx == 0 and len(visible_apps) > 1:
                continue
            if self._last_switched_pid is not None and app_p == self._last_switched_pid and len(visible_apps) > 2:
                continue
            chosen_name, chosen_pid = app_n, app_p
            break

        if not chosen_name and visible_apps:
            chosen_name, chosen_pid = visible_apps[-1]

        if chosen_name:
            self._last_switched_pid = chosen_pid
            if activate_app_fn:
                activate_app_fn(chosen_name)
            time.sleep(0.12)
            self.ensure_window_on_primary_display(chosen_name)
            if chosen_pid:
                app_el = hiservices.AXUIElementCreateApplication(chosen_pid)
                if app_el:
                    wv = c_void_p()
                    res_b = None
                    if hiservices.AXUIElementCopyAttributeValue(app_el, cf_wins, byref(wv)) == 0 and wv.value:
                        if cf.CFArrayGetCount(wv.value) > 0:
                            w0 = cf.CFArrayGetValueAtIndex(wv.value, 0)
                            hiservices.AXUIElementPerformAction(w0, cf_raise)
                            res_b = _get_ax_win_bounds(w0, cf_pos, cf_size)
                        cf.CFRelease(wv)
                    cf.CFRelease(app_el)
                    if res_b:
                        for r in (cf_raise, cf_size, cf_pos, cf_wins):
                            cf.CFRelease(r)
                        return (chosen_name, res_b)

        for r in (cf_raise, cf_size, cf_pos, cf_wins):
            cf.CFRelease(r)
        return None

    def evaluate_and_ensure_step_completion(
        self,
        step: WalkthroughStep,
        eff_action: TeachingAction,
        target_x: float,
        target_y: float,
        activate_app_fn: Optional[Callable[[str], None]] = None,
        retry_cursor_action_fn: Optional[Callable[[], None]] = None,
    ) -> StepEvaluationResult:
        """Evaluates whether a step's UI effect occurred and drives completion before advancing."""
        # 1. Custom verifier hook (for unit tests or custom step assertions)
        if self.custom_verifier is not None:
            for attempt in range(1, self.max_retries + 1):
                if self.custom_verifier(step, attempt):
                    return StepEvaluationResult(
                        completed=True,
                        attempts=attempt,
                        verification_method="custom_verifier",
                        detail=f"Verified on attempt {attempt}",
                    )
                if attempt < self.max_retries:
                    if retry_cursor_action_fn is not None:
                        retry_cursor_action_fn()
                    if self.retry_delay > 0:
                        time.sleep(self.retry_delay)
            return StepEvaluationResult(
                completed=False,
                attempts=self.max_retries,
                verification_method="custom_verifier",
                detail="Failed verification after max retries",
            )

        # 2. In mock or non-darwin environments, mark completed immediately
        if self.mock or (self.grounder and self.grounder.mock) or sys.platform != "darwin":
            return StepEvaluationResult(completed=True, attempts=1, verification_method="mock")

        query = step.target_element_query or {}
        role = self._norm(query.get("ax_role") or query.get("role") or "")
        title = self._norm(query.get("ax_title") or query.get("title") or "")
        app = (step.target_app or "").strip()
        app_lower = app.lower()
        inst_lower = self._norm(step.instruction)
        step_title_lower = self._norm(step.title)
        combined = f"{inst_lower} {step_title_lower}"
        hotkey_list = [k.lower() for k in (step.hotkey_combo or [])]

        # Case 1: Opening the Apple Menu ()
        if (
            role in ("axmenubaritem", "axmenubutton", "axbutton")
            and title in ("apple", "", "apple menu", "apple icon")
        ) or (
            target_x <= 45.0
            and target_y <= 30.0
            and "apple" in combined
            and not any(k in step_title_lower for k in ("system settings", "about this mac", "system information"))
        ):
            for attempt in range(1, self.max_retries + 1):
                if self.is_apple_menu_open():
                    return StepEvaluationResult(
                        completed=True,
                        attempts=attempt,
                        verification_method="apple_menu_open",
                        detail="Apple menu dropdown verified open",
                    )
                self.press_apple_menu()
                if self.retry_delay > 0:
                    time.sleep(self.retry_delay)
                if self.is_apple_menu_open():
                    return StepEvaluationResult(
                        completed=True,
                        attempts=attempt,
                        verification_method="apple_menu_open",
                        detail="Apple menu dropdown verified open",
                    )
            return StepEvaluationResult(
                completed=False,
                attempts=self.max_retries,
                verification_method="apple_menu_open",
                detail="Apple menu did not open",
            )

        # Case 2a: Selecting 'System Settings...' from Apple Menu or Launching System Settings
        if (role == "axmenuitem" and "system settings" in title) or (
            app_lower == "system settings"
            and step.step_index == 1
            and any(k in combined for k in ("open system settings", "launch system settings"))
        ):
            for attempt in range(1, self.max_retries + 1):
                if role != "axmenuitem" and attempt == 1 and target_x <= 45.0 and target_y <= 30.0:
                    self.press_apple_menu()
                    time.sleep(0.22)
                self.press_apple_menu_item("system settings")
                if activate_app_fn:
                    activate_app_fn("System Settings")
                self.close_apple_menu()
                time.sleep(self.retry_delay)
                if self.is_app_window_open("System Settings"):
                    self.ensure_window_on_primary_display("System Settings")
                    self.dismiss_open_sheet("System Settings")
                    return StepEvaluationResult(
                        completed=True,
                        attempts=attempt,
                        verification_method="app_window_ready",
                        detail="System Settings window verified open",
                    )
            return StepEvaluationResult(
                completed=False,
                attempts=self.max_retries,
                verification_method="app_window_ready",
                detail="System Settings window not detected",
            )

        # Case 2b: Selecting 'About This Mac' or 'System Information' from Apple Menu
        if (role == "axmenuitem" and ("about this mac" in title or "system information" in title)) or (
            target_x <= 280.0
            and target_y <= 150.0
            and ("about this mac" in combined or ("system information" in combined and "apple" in combined))
        ):
            item_key = "about this mac" if "about this mac" in (title or combined) else "system information"
            for attempt in range(1, self.max_retries + 1):
                self.press_apple_menu_item(item_key)
                self.close_apple_menu()
                if activate_app_fn:
                    activate_app_fn("System Information")
                for _ in range(4):
                    time.sleep(max(0.15, self.retry_delay))
                    if self.is_app_window_open("System Information"):
                        if activate_app_fn:
                            activate_app_fn("System Information")
                        self.ensure_window_on_primary_display("System Information")
                        return StepEvaluationResult(
                            completed=True,
                            attempts=attempt,
                            verification_method="app_window_ready",
                            detail=f"'{item_key}' window verified open",
                        )
            return StepEvaluationResult(
                completed=False,
                attempts=self.max_retries,
                verification_method="app_window_ready",
                detail=f"'{item_key}' window not detected",
            )

        # Case 3a: Opening Spotlight / Command Bar (⌘ + Space)
        if (
            eff_action == TeachingAction.DEMONSTRATE_HOTKEY
            and "space" in hotkey_list
            and any(m in hotkey_list for m in ("cmd", "command", "⌘"))
        ) or (
            any(k in combined for k in ("spotlight", "command bar"))
            and eff_action != TeachingAction.DEMONSTRATE_TYPE
            and not any(w in combined for w in ("press return", "press enter", "top result", "hit return"))
            and any(w in combined for w in ("open", "press", "command", "cmd", "⌘", "invoke", "search"))
        ):
            for attempt in range(1, self.max_retries + 1):
                if self.open_spotlight():
                    if app and app_lower not in (
                        "", "spotlight", "systemuiserver", "system utilities",
                        "controlcenter", "windowserver", "loginwindow", "macos",
                    ):
                        # Single-step app launch via Spotlight (e.g. "Press ⌘ + Space, type 'Calculator', and press Return")
                        self.type_into_spotlight(app)
                        if any(w in combined for w in ("return", "enter", "launch", "open")):
                            time.sleep(0.45)
                            try:
                                subprocess.run(
                                    ["osascript", "-e", 'tell application "System Events" to key code 36'],
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL,
                                    timeout=0.4,
                                )
                            except Exception:
                                pass
                            time.sleep(0.25)
                            self.close_spotlight()
                            if activate_app_fn:
                                activate_app_fn(app)
                            time.sleep(0.18)
                            self.ensure_window_on_primary_display(app)
                            return StepEvaluationResult(
                                completed=True,
                                attempts=attempt,
                                verification_method="app_window_ready",
                                detail=f"Launched '{app}' via Spotlight command bar",
                            )
                    return StepEvaluationResult(
                        completed=True,
                        attempts=attempt,
                        verification_method="spotlight_open",
                        detail="Spotlight search bar verified open on primary display",
                    )
                if self.retry_delay > 0:
                    time.sleep(self.retry_delay)
            return StepEvaluationResult(
                completed=False,
                attempts=self.max_retries,
                verification_method="spotlight_open",
                detail="Spotlight search bar did not open",
            )

        # Case 3b: Typing into Spotlight / Command Bar
        if eff_action == TeachingAction.DEMONSTRATE_TYPE and (
            app_lower == "spotlight"
            or any(k in combined for k in ("spotlight", "command bar", "search bar", "search field"))
            or self._norm(query.get("ax_title") or "") == "spotlight search field"
        ):
            query_text = step.text_to_type or ""
            if not query_text:
                import re
                quoted = re.findall(r"['\"]([^'\"]+)['\"]", step.instruction or "")
                if quoted:
                    query_text = quoted[0]
            for attempt in range(1, self.max_retries + 1):
                if self.type_into_spotlight(query_text):
                    return StepEvaluationResult(
                        completed=True,
                        attempts=attempt,
                        verification_method="spotlight_typed",
                        detail=f"Typed '{query_text.strip()}' into Spotlight",
                    )
                if self.retry_delay > 0:
                    time.sleep(self.retry_delay)
            return StepEvaluationResult(
                completed=False,
                attempts=self.max_retries,
                verification_method="spotlight_typed",
                detail="Could not verify text typed into Spotlight",
            )

        # Case 3c: Submitting / Pressing Return in Spotlight to launch top result
        if (app_lower == "spotlight" or "spotlight" in combined or self.is_spotlight_open()) and any(
            w in combined for w in ("press return", "press enter", "top result", "hit return")
        ):
            typed_query = self.get_spotlight_text().strip()
            try:
                subprocess.run(
                    ["osascript", "-e", 'tell application "System Events" to key code 36'],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=0.4,
                )
                time.sleep(0.25)
            except Exception:
                pass
            self.close_spotlight()
            launch_candidate = (
                app
                if (app and app_lower not in ("spotlight", "systemuiserver", "system utilities", "macos"))
                else typed_query
            )
            if launch_candidate and activate_app_fn:
                activate_app_fn(launch_candidate)
                time.sleep(0.18)
                self.ensure_window_on_primary_display(launch_candidate)
            return StepEvaluationResult(
                completed=True,
                attempts=1,
                verification_method="spotlight_submitted",
                detail=f"Spotlight top result launched ({launch_candidate or 'top result'})",
            )

        # Case 4: Navigating to a System Settings Sidebar Pane (e.g., Appearance, Desktop & Dock, Wallpaper, Bluetooth)
        if app_lower == "system settings" and role == "axrow" and title:
            for attempt in range(1, self.max_retries + 1):
                self.dismiss_open_sheet("System Settings")
                pressed = self.press_app_menu_item("System Settings", title)
                time.sleep(self.retry_delay)
                win_title = self.get_app_window_title("System Settings")
                if (win_title and (title in win_title or win_title in title)) or (not win_title and pressed):
                    return StepEvaluationResult(
                        completed=True,
                        attempts=attempt,
                        verification_method="settings_pane_active",
                        detail=f"System Settings pane '{title}' active (window='{win_title}')",
                    )
            return StepEvaluationResult(
                completed=False,
                attempts=self.max_retries,
                verification_method="settings_pane_active",
                detail=f"Could not verify System Settings pane '{title}'",
            )

        # Case 5: Clicking an App Menu Bar Item (e.g., File, Finder, Bookmarks)
        if role == "axmenubaritem" and title and title not in ("apple", ""):
            for attempt in range(1, self.max_retries + 1):
                pressed, is_open = self.press_app_menu_bar_item(app, title)
                if is_open or pressed:
                    return StepEvaluationResult(
                        completed=True,
                        attempts=attempt,
                        verification_method="menubar_item_open",
                        detail=f"App menu '{title}' opened",
                    )
                time.sleep(self.retry_delay)
            return StepEvaluationResult(
                completed=False,
                attempts=self.max_retries,
                verification_method="menubar_item_open",
                detail=f"App menu '{title}' did not open",
            )

        # Case 6: Clicking an App Menu Item (e.g., New Folder, Empty Trash..., Add Bookmark...)
        if role == "axmenuitem" and title and "system settings" not in title:
            for attempt in range(1, self.max_retries + 1):
                if self.press_app_menu_item(app, title):
                    return StepEvaluationResult(
                        completed=True,
                        attempts=attempt,
                        verification_method="menu_item_pressed",
                        detail=f"Menu item '{title}' pressed",
                    )
                time.sleep(self.retry_delay)
            return StepEvaluationResult(
                completed=False,
                attempts=self.max_retries,
                verification_method="menu_item_pressed",
                detail=f"Menu item '{title}' could not be pressed",
            )

        # Case 7: Clicking a Window Button / RadioButton / CheckBox / Sheet Trigger (e.g., More Info..., System Report..., Hot Corners..., Dark)
        if title and role in ("axbutton", "axradiobutton", "axcheckbox", "axswitch", "axpopupbutton"):
            should_press = eff_action == TeachingAction.DEMONSTRATE_CLICK or (
                eff_action == TeachingAction.PULSE_BEACON
                and any(w in inst_lower for w in ("click", "select", "toggle", "switch"))
            )
            if should_press:
                for attempt in range(1, self.max_retries + 1):
                    pressed = self.press_window_element(app, role, title)
                    if "more info" in title:
                        for _ in range(6):
                            time.sleep(max(0.2, self.retry_delay))
                            if self.is_app_window_open("System Settings"):
                                if activate_app_fn:
                                    activate_app_fn("System Settings")
                                self.ensure_window_on_primary_display("System Settings")
                                return StepEvaluationResult(
                                    completed=True,
                                    attempts=attempt,
                                    verification_method="app_window_ready",
                                    detail="System Settings About pane opened via More Info",
                                )
                    elif "system report" in title:
                        for _ in range(5):
                            time.sleep(max(0.18, self.retry_delay))
                            if self.is_app_window_open("System Information"):
                                if activate_app_fn:
                                    activate_app_fn("System Information")
                                self.ensure_window_on_primary_display("System Information")
                                return StepEvaluationResult(
                                    completed=True,
                                    attempts=attempt,
                                    verification_method="app_window_ready",
                                    detail="System Information report window verified open",
                                )
                    elif "hot corners" in title:
                        time.sleep(self.retry_delay)
                        if self.has_open_sheet(app):
                            return StepEvaluationResult(
                                completed=True,
                                attempts=attempt,
                                verification_method="sheet_opened",
                                detail="Hot Corners sheet verified open",
                            )
                    elif pressed:
                        return StepEvaluationResult(
                            completed=True,
                            attempts=attempt,
                            verification_method="element_pressed",
                            detail=f"Control '{title}' pressed via AXPress",
                        )
                    time.sleep(self.retry_delay)

        # Case 8: Moving / Switching / Cycling Between Windows
        if any(
            k in combined
            for k in (
                "cycle windows",
                "switch between open windows",
                "switch between applications",
                "move between windows",
                "switch between windows",
                "command + `",
                "command + tab",
            )
        ) or (
            eff_action == TeachingAction.DEMONSTRATE_HOTKEY
            and any(k in hotkey_list for k in ("`", "tab"))
            and any(m in hotkey_list for m in ("cmd", "command", "⌘"))
        ):
            cycle_same = "`" in hotkey_list or "cycle windows" in combined
            switched = self.switch_visible_window(
                cycle_same_app=cycle_same,
                activate_app_fn=activate_app_fn,
            )
            if switched:
                sw_app, _ = switched
                return StepEvaluationResult(
                    completed=True,
                    attempts=1,
                    verification_method="window_switched",
                    detail=f"Switched focus to '{sw_app}' window",
                )

        return StepEvaluationResult(completed=True, attempts=1, verification_method="cursor_dispatched")


class WalkthroughTutor:
    """Manages the step-by-step on-screen walkthrough execution loop."""

    def __init__(
        self,
        virtual_cursor: Optional[VirtualCursor] = None,
        grounder: Optional[ScreenGrounder] = None,
        bus: Optional[ExecutionBus] = None,
        mock: bool = False,
        auto_advance: bool = True,
        auto_advance_delay: float = 1.8,
        assist_timeout: float = 4.0,
        evaluator: Optional[StepCompletionEvaluator] = None,
    ) -> None:
        self.mock = mock
        self.virtual_cursor = virtual_cursor or VirtualCursor(initial_x=640.0, initial_y=400.0, mock=mock)
        self.grounder = grounder or ScreenGrounder(mock=mock)
        self.bus = bus or ExecutionBus()
        self.auto_advance = auto_advance
        self.auto_advance_delay = auto_advance_delay
        self.assist_timeout = assist_timeout
        self.evaluator = evaluator or StepCompletionEvaluator(grounder=self.grounder, mock=mock)
        self._last_evaluation: Optional[StepEvaluationResult] = None

        self._plan: Optional[WalkthroughPlan] = None
        self._current_step_index: int = 0  # 1-indexed
        self._status: WalkthroughStatus = WalkthroughStatus.IDLE
        self._lock = threading.RLock()
        self._listeners: List[Callable[[Dict[str, Any]], None]] = []
        self._active_target_coords: Tuple[float, float] = (640.0, 400.0)
        self._active_spotlight_bounds: Optional[Tuple[float, float, float, float]] = None
        self._pause_event = threading.Event()
        self._pause_event.set()  # Unpaused by default
        self._auto_advance_timer: Optional[threading.Timer] = None
        self._assist_timer: Optional[threading.Timer] = None

    def _cancel_timers(self) -> None:
        """Cancels all active background timers (auto-advance and assist watchdog)."""
        with self._lock:
            if self._auto_advance_timer is not None:
                self._auto_advance_timer.cancel()
                self._auto_advance_timer = None
            if self._assist_timer is not None:
                self._assist_timer.cancel()
                self._assist_timer = None

    def _cancel_auto_advance(self) -> None:
        """Cancels any pending auto-advance timer."""
        self._cancel_timers()

    def _schedule_assist_watchdog(self, delay: Optional[float] = None) -> None:
        """Schedules a safety watchdog assist hint if a step is stalled or element is ungrounded."""
        with self._lock:
            if self._assist_timer is not None:
                self._assist_timer.cancel()
                self._assist_timer = None
            if self._status in (
                WalkthroughStatus.PAUSED,
                WalkthroughStatus.COMPLETED,
                WalkthroughStatus.CANCELLED,
                WalkthroughStatus.ERROR,
            ):
                return
            wait_time = delay if delay is not None else self.assist_timeout
            timer = threading.Timer(wait_time, self._on_assist_timeout)
            timer.daemon = True
            self._assist_timer = timer
            timer.start()

    def _on_assist_timeout(self) -> None:
        """Dispatches an assist telemetry hint when a step needs guidance or target is stalled."""
        with self._lock:
            if self._status in (
                WalkthroughStatus.WAITING_FOR_USER,
                WalkthroughStatus.HIGHLIGHTING,
                WalkthroughStatus.DEMONSTRATING,
            ):
                self._broadcast_telemetry(
                    "ASSIST",
                    message="Need help? Click the highlighted beacon or press 'Next' to continue.",
                )

    def _schedule_auto_advance(self, delay: Optional[float] = None) -> None:
        """Schedules auto-advancement to the next step after delay."""
        with self._lock:
            self._cancel_auto_advance()
            if not self.auto_advance:
                return
            if not self._plan or self._plan.mode == WalkthroughMode.INTERACTIVE_TUTOR:
                return
            if self._status in (
                WalkthroughStatus.PAUSED,
                WalkthroughStatus.COMPLETED,
                WalkthroughStatus.CANCELLED,
                WalkthroughStatus.ERROR,
            ):
                return

            wait_time = delay if delay is not None else self.auto_advance_delay
            timer = threading.Timer(wait_time, self._on_auto_advance)
            timer.daemon = True
            self._auto_advance_timer = timer
            timer.start()

    def _on_auto_advance(self) -> None:
        """Timer callback to advance to the next step automatically."""
        with self._lock:
            if not self.auto_advance:
                return
            if self._status in (WalkthroughStatus.DEMONSTRATING, WalkthroughStatus.HIGHLIGHTING):
                self.step_forward()

    @property
    def status(self) -> WalkthroughStatus:
        with self._lock:
            return self._status

    @property
    def current_step_index(self) -> int:
        with self._lock:
            return self._current_step_index

    @property
    def plan(self) -> Optional[WalkthroughPlan]:
        with self._lock:
            return self._plan

    def add_listener(self, listener: Callable[[Dict[str, Any]], None]) -> None:
        """Registers an observer for walkthrough state transitions."""
        with self._lock:
            if listener not in self._listeners:
                self._listeners.append(listener)

    def remove_listener(self, listener: Callable[[Dict[str, Any]], None]) -> None:
        """Removes a registered observer."""
        with self._lock:
            if listener in self._listeners:
                self._listeners.remove(listener)

    def load_plan(self, plan: WalkthroughPlan) -> None:
        """Loads a WalkthroughPlan into the tutor and sets status to READY."""
        with self._lock:
            self._cancel_auto_advance()
            self._plan = plan
            self._current_step_index = 0
            self._status = WalkthroughStatus.READY
            self._broadcast_telemetry("LOADED", message=f"Loaded walkthrough: '{plan.goal}'")

    def start(self) -> bool:
        """Begins execution from step 1 synchronously."""
        with self._lock:
            self._cancel_auto_advance()
            if not self._plan or not self._plan.steps:
                self._status = WalkthroughStatus.ERROR
                return False
            if not self.mock and (self.virtual_cursor.vx <= 5.0 or self.virtual_cursor.vy <= 5.0):
                self.virtual_cursor.vx = 640.0
                self.virtual_cursor.vy = 400.0
            self._current_step_index = 1
            self._execute_active_step()
            return True

    def start_async(self) -> bool:
        """Begins execution from step 1 on a background daemon thread so HTTP callers receive Step 1 telemetry immediately."""
        with self._lock:
            self._cancel_auto_advance()
            if not self._plan or not self._plan.steps:
                self._status = WalkthroughStatus.ERROR
                return False
            if not self.mock and (self.virtual_cursor.vx <= 5.0 or self.virtual_cursor.vy <= 5.0):
                self.virtual_cursor.vx = 640.0
                self.virtual_cursor.vy = 400.0
            self._current_step_index = 1
            first_step = self._plan.steps[0]
            if first_step.fallback_screen_coords and len(first_step.fallback_screen_coords) == 2:
                self._active_target_coords = (
                    float(first_step.fallback_screen_coords[0]),
                    float(first_step.fallback_screen_coords[1]),
                )
            if first_step.spotlight_bounds and len(first_step.spotlight_bounds) == 4:
                self._active_spotlight_bounds = first_step.spotlight_bounds
            self._status = WalkthroughStatus.NAVIGATING
            self._broadcast_telemetry("NAVIGATING", message=f"Moving to step 1: {first_step.title}")

        def _runner() -> None:
            with self._lock:
                if self._status != WalkthroughStatus.CANCELLED and self._current_step_index == 1:
                    self._execute_active_step()

        t = threading.Thread(target=_runner, name="ClioWalkthroughStep1", daemon=True)
        t.start()
        return True

    def step_forward(self) -> bool:
        """Navigates to the next instructional step."""
        with self._lock:
            self._cancel_auto_advance()
            if not self._plan:
                return False
            if self._current_step_index >= len(self._plan.steps):
                if not self.mock:
                    try:
                        self.evaluator.close_apple_menu()
                    except Exception:
                        pass
                self._status = WalkthroughStatus.COMPLETED
                self._active_target_coords = (-200.0, -200.0)
                self._active_spotlight_bounds = None
                self._broadcast_telemetry("COMPLETED", message="Walkthrough completed! 🎉")
                # Park virtual cursor safely off-screen so the cursor disappears
                try:
                    self.virtual_cursor.hover(-200.0, -200.0)
                except Exception:
                    pass
                return False
            self._current_step_index += 1
            self._execute_active_step()
            return True

    def step_backward(self) -> bool:
        """Navigates to the previous instructional step."""
        with self._lock:
            self._cancel_auto_advance()
            if not self._plan:
                return False
            if self._current_step_index <= 1:
                return False
            self._current_step_index -= 1
            self._execute_active_step()
            return True

    def retry_step(self) -> None:
        """Re-demonstrates the current active step."""
        with self._lock:
            self._cancel_auto_advance()
            if self._plan and self._current_step_index >= 1:
                self._execute_active_step()

    def pause(self) -> None:
        """Pauses the walkthrough tutor."""
        with self._lock:
            self._cancel_auto_advance()
            if self._status != WalkthroughStatus.PAUSED:
                self._status = WalkthroughStatus.PAUSED
                self._pause_event.clear()
                self._broadcast_telemetry("PAUSED", message="Walkthrough paused.")

    def resume(self) -> None:
        """Resumes the walkthrough tutor from paused state."""
        with self._lock:
            if self._status == WalkthroughStatus.PAUSED:
                self._pause_event.set()
                self._execute_active_step()

    def stop(self) -> None:
        """Cancels and resets the walkthrough session."""
        with self._lock:
            self._cancel_auto_advance()
            if not self.mock:
                try:
                    self.evaluator.close_apple_menu()
                    self.evaluator.close_spotlight()
                except Exception:
                    pass
            self._status = WalkthroughStatus.CANCELLED
            self._pause_event.set()
            self._active_target_coords = (-200.0, -200.0)
            self._active_spotlight_bounds = None
            self._broadcast_telemetry("CANCELLED", message="Walkthrough stopped.")
            # Park virtual cursor safely off-screen
            try:
                self.virtual_cursor.hover(-200.0, -200.0)
            except Exception:
                pass

    def simulate_user_action(self) -> bool:
        """Simulates user completing the current step in interactive mode."""
        with self._lock:
            if self._status == WalkthroughStatus.WAITING_FOR_USER:
                return self.step_forward()
            return False

    @property
    def last_evaluation(self) -> Optional[StepEvaluationResult]:
        """Returns the evaluation result of the most recently executed step."""
        with self._lock:
            return self._last_evaluation

    def _should_skip_pre_activation(self, step: WalkthroughStep) -> bool:
        """Determines if pre-step app activation should be deferred (e.g. Apple menu, open dropdowns, or Spotlight)."""
        query = step.target_element_query or {}
        role = (query.get("ax_role") or query.get("role") or "").strip().lower()
        title = (query.get("ax_title") or query.get("title") or "").strip().lower()
        app_lower = (step.target_app or "").strip().lower()
        combined = f"{(step.title or '').lower()} {(step.instruction or '').lower()}"
        hotkey_list = [k.lower() for k in (step.hotkey_combo or [])]

        if role in ("axmenubaritem", "axmenubutton", "axbutton") and title in ("apple", "", "apple menu", "apple icon"):
            return True
        if role == "axmenuitem" or "dropdown" in combined or "about this mac" in combined:
            return True
        if app_lower == "spotlight" or "spotlight" in combined or "space" in hotkey_list:
            return True
        if any(k in combined for k in ("cycle windows", "switch between", "move between windows")):
            return True
        if step.fallback_screen_coords and len(step.fallback_screen_coords) == 2:
            fx, fy = step.fallback_screen_coords
            if fx <= 45.0 and fy <= 30.0 and "apple" in combined:
                return True
        return False

    def _activate_target_app(self, app_name: str) -> None:
        """Brings the target application frontmost during live demonstration."""
        if not app_name or self.mock or sys.platform != "darwin":
            return
        clean = app_name.strip()
        lower = clean.lower()
        if lower in ("system", "controlcenter", "menubar", "dock", "spotlight"):
            return
        try:
            as_code = f'tell application "{clean}" to activate'
            res = subprocess.run(["osascript", "-e", as_code], capture_output=True, timeout=0.4)
            if res.returncode != 0:
                subprocess.run(["open", "-a", clean], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=0.4)
        except Exception as e:
            logger.debug("Failed to activate target application %s: %s", clean, e)

    def _execute_active_step(self) -> None:
        """Executes the visual and pedagogical choreography for the current step."""
        if not self._plan or self._current_step_index < 1 or self._current_step_index > len(self._plan.steps):
            return

        step = self._plan.steps[self._current_step_index - 1]
        inst_lower = (step.instruction or "").lower()
        title_lower = (step.title or "").lower()
        combined_text = f"{inst_lower} {title_lower}"
        query_dict = step.target_element_query or {}
        ax_role_lower = (query_dict.get("ax_role") or query_dict.get("role") or "").strip().lower()
        ax_title_lower = (query_dict.get("ax_title") or query_dict.get("title") or "").strip().lower()
        hotkey_list_lower = [k.lower() for k in (step.hotkey_combo or [])]

        # 0. Ensure target application is frontmost on live desktop (unless clicking Apple menu or Spotlight)
        if step.target_app and not self._should_skip_pre_activation(step):
            if not self.mock and self.evaluator.is_spotlight_open():
                self.evaluator.close_spotlight()
            self._activate_target_app(step.target_app)
            if not self.mock:
                self.evaluator.ensure_window_on_primary_display(step.target_app)

        # 1. Pre-step container UI readiness so intermediate UI is visibly on screen during the step
        is_opening_spotlight = (
            "space" in hotkey_list_lower and any(m in hotkey_list_lower for m in ("cmd", "command", "⌘"))
        ) or (
            any(k in combined_text for k in ("spotlight", "command bar"))
            and step.action_type != TeachingAction.DEMONSTRATE_TYPE
            and not any(w in combined_text for w in ("press return", "press enter", "top result", "hit return"))
            and any(w in combined_text for w in ("open", "press", "command", "cmd", "⌘", "invoke"))
        )
        if not self.mock and is_opening_spotlight:
            self.evaluator.open_spotlight()

        if (
            not self.mock
            and ax_role_lower == "axmenuitem"
            and (step.target_app or "").strip().lower() in ("controlcenter", "control center", "systemuiserver")
        ):
            if not self.evaluator.is_apple_menu_open():
                self.evaluator.press_apple_menu()
                time.sleep(0.18)

        target_x, target_y, bounds = self.grounder.resolve_step_coordinates(step)
        if not self.mock and self.evaluator.is_spotlight_open() and (
            "spotlight" in combined_text or "command bar" in combined_text or (step.target_app or "").strip().lower() == "spotlight" or is_opening_spotlight
        ):
            sp_bounds = self.grounder.resolve_element_bounds("Spotlight", ax_role="AXTextField", ax_title="")
            if sp_bounds and len(sp_bounds) == 4:
                bx, by, bw, bh = sp_bounds
                target_x = bx + (bw / 2.0)
                target_y = by + (bh / 2.0)
                if any(w in combined_text for w in ("press return", "press enter", "top result", "hit return")):
                    target_y += 45.0
                    bounds = (bx, by + 36.0, bw, 36.0)
                else:
                    bounds = sp_bounds

        self._active_target_coords = (target_x, target_y)
        self._active_spotlight_bounds = bounds

        # 2. State: NAVIGATING (Glides Virtual Cursor to target)
        self._status = WalkthroughStatus.NAVIGATING
        self._broadcast_telemetry("NAVIGATING", message=f"Moving to step {step.step_index}: {step.title}")

        # Humanized ease-in-out movement duration (0.05s in mock, 0.7s in live)
        move_duration = 0.05 if self.mock else 0.7
        self.virtual_cursor.move_to(target_x, target_y, duration=move_duration, smooth=True)

        # If this step opens the Apple menu (), open the dropdown immediately as the cursor arrives
        # so the user visibly sees the Apple menu dropdown open on screen during the step's highlight & dwell!
        is_apple_icon_step = (
            ax_role_lower in ("axmenubaritem", "axmenubutton", "axbutton")
            and ax_title_lower in ("apple", "", "apple menu", "apple icon")
        ) or (
            target_x <= 45.0
            and target_y <= 30.0
            and "apple" in combined_text
            and not any(k in title_lower for k in ("system settings", "about this mac", "system information"))
        )
        if not self.mock and is_apple_icon_step:
            self.evaluator.press_apple_menu()

        # 3. State: HIGHLIGHTING (Wiggle and pulse)
        self._status = WalkthroughStatus.HIGHLIGHTING
        self._broadcast_telemetry("HIGHLIGHTING", message=step.instruction)

        if not self.mock:
            self.virtual_cursor.wiggle_at(target_x, target_y, amplitude=6.0, oscillations=2)
            self.virtual_cursor.pulse_at(target_x, target_y, duration=0.3)

        # 4. State: Action Dispatch
        if self._plan.mode == WalkthroughMode.INTERACTIVE_TUTOR and step.action_type in (
            TeachingAction.WAIT_FOR_USER_CLICK,
            TeachingAction.DEMONSTRATE_CLICK,
        ):
            self._status = WalkthroughStatus.WAITING_FOR_USER
            self._broadcast_telemetry("WAITING_FOR_USER", message=f"Your turn: {step.instruction}")
            self._schedule_assist_watchdog()
        else:
            self._status = WalkthroughStatus.DEMONSTRATING
            self._broadcast_telemetry("DEMONSTRATING", message=f"Demonstrating: {step.instruction}")

            # Resolve effective action to carry out based on step.action_type and step instruction/context
            action_type = step.action_type

            # If action_type is generic or ambiguous, infer specific gesture from instruction
            eff_action = action_type
            hotkey_to_press = list(step.hotkey_combo) if step.hotkey_combo else []
            text_to_type = step.text_to_type

            if any(w in combined_text for w in ("double click", "double-click", "click twice", "rapidly click")):
                eff_action = TeachingAction.DEMONSTRATE_DOUBLE_CLICK
            elif any(w in combined_text for w in ("right click", "right-click", "context menu", "contextual menu", "secondary click", "two fingers")):
                eff_action = TeachingAction.DEMONSTRATE_RIGHT_CLICK
            elif any(w in combined_text for w in ("drag", "drag and drop", "pull down", "drag to", "swipe")):
                eff_action = TeachingAction.DEMONSTRATE_DRAG
            elif any(w in combined_text for w in ("scroll", "scroll down", "scroll up", "scroll wheel")) and not (
                action_type == TeachingAction.DEMONSTRATE_CLICK and "click" in combined_text
            ):
                eff_action = TeachingAction.DEMONSTRATE_SCROLL
            elif any(w in combined_text for w in ("press return", "press enter", "hit enter", "hit return")):
                eff_action = TeachingAction.DEMONSTRATE_HOTKEY
                if not hotkey_to_press:
                    hotkey_to_press = ["return"]
            elif any(w in combined_text for w in ("type ", "enter text", "search for", "input text")):
                eff_action = TeachingAction.DEMONSTRATE_TYPE
                if not text_to_type:
                    import re
                    quoted = re.findall(r"['\"]([^'\"]+)['\"]", step.instruction or "")
                    if quoted:
                        text_to_type = quoted[0]
            elif any(w in combined_text for w in ("press ", "shortcut", "hotkey", "command +", "control +", "option +", "cmd +", "ctrl +", "⌘")):
                eff_action = TeachingAction.DEMONSTRATE_HOTKEY
                if not hotkey_to_press:
                    import re
                    combo = []
                    if any(m in combined_text for m in ("cmd", "command", "⌘")):
                        combo.append("cmd")
                    if any(m in combined_text for m in ("shift", "⇧")):
                        combo.append("shift")
                    if any(m in combined_text for m in ("option", "alt", "⌥")):
                        combo.append("option")
                    if any(m in combined_text for m in ("ctrl", "control", "⌃")):
                        combo.append("ctrl")
                    key_match = re.search(r"\+\s*([a-zA-Z0-9`\-\=\[\]\\])", combined_text)
                    if key_match:
                        combo.append(key_match.group(1).lower())
                    elif "tab" in combined_text:
                        combo.append("tab")
                    elif "space" in combined_text:
                        combo.append("space")
                    elif "return" in combined_text or "enter" in combined_text:
                        combo.append("return")
                    if combo:
                        hotkey_to_press = combo
            elif eff_action == TeachingAction.MOVE_AND_HOVER and any(
                w in combined_text for w in (
                    "click", "select", "tap", "choose", "toggle", "turn on", "turn off", "switch to", "check", "uncheck", "enable", "disable", "open"
                )
            ):
                eff_action = TeachingAction.DEMONSTRATE_CLICK

            # If typing into Spotlight and there is a subsequent step, do not send trailing newline yet
            if (
                not self.mock
                and eff_action == TeachingAction.DEMONSTRATE_TYPE
                and text_to_type
                and ("spotlight" in combined_text or (step.target_app or "").strip().lower() == "spotlight")
                and self._current_step_index < len(self._plan.steps)
            ):
                text_to_type = text_to_type.rstrip("\r\n")

            def _dispatch_cursor_action() -> None:
                if eff_action == TeachingAction.DEMONSTRATE_DOUBLE_CLICK:
                    self.virtual_cursor.double_click(target_x, target_y)
                elif eff_action == TeachingAction.DEMONSTRATE_RIGHT_CLICK:
                    self.virtual_cursor.right_click(target_x, target_y)
                elif eff_action == TeachingAction.DEMONSTRATE_DRAG:
                    drag_x, drag_y = step.drag_target_coords if step.drag_target_coords else (target_x, target_y + 120.0)
                    drag_duration = 0.1 if self.mock else 0.6
                    self.virtual_cursor.drag_to(drag_x, drag_y, duration=drag_duration)
                elif eff_action == TeachingAction.DEMONSTRATE_SCROLL:
                    scroll_dx, scroll_dy = step.scroll_delta if step.scroll_delta else (0, -5)
                    self.virtual_cursor.scroll(dx=scroll_dx, dy=scroll_dy)
                elif eff_action == TeachingAction.DEMONSTRATE_CLICK:
                    self.virtual_cursor.click(target_x, target_y)
                elif eff_action == TeachingAction.PULSE_BEACON:
                    self.virtual_cursor.pulse_at(target_x, target_y, duration=0.4)
                elif eff_action == TeachingAction.DEMONSTRATE_TYPE and text_to_type:
                    self.virtual_cursor.type_text(text_to_type)
                elif eff_action == TeachingAction.DEMONSTRATE_HOTKEY and hotkey_to_press:
                    self.virtual_cursor.press_hotkey(*hotkey_to_press)
                elif eff_action == TeachingAction.MOVE_AND_HOVER:
                    hover_dur = 0.05 if self.mock else 0.3
                    self.virtual_cursor.hover(target_x, target_y, duration=hover_dur)
                else:
                    hover_dur = 0.05 if self.mock else 0.3
                    self.virtual_cursor.hover(target_x, target_y, duration=hover_dur)

            # Dispatch physical action through the Virtual Cursor
            _dispatch_cursor_action()

            # 5. Evaluate and ensure step completion before advancing to the next step
            eval_res = self.evaluator.evaluate_and_ensure_step_completion(
                step=step,
                eff_action=eff_action,
                target_x=target_x,
                target_y=target_y,
                activate_app_fn=self._activate_target_app,
                retry_cursor_action_fn=_dispatch_cursor_action,
            )
            self._last_evaluation = eval_res

            # Visually guide the Clio cursor onto the newly opened/switched UI container within the step
            if not self.mock:
                if eval_res.verification_method in ("spotlight_open", "spotlight_typed"):
                    sp_bounds = self.grounder.resolve_element_bounds("Spotlight", ax_role="AXTextField", ax_title="")
                    if sp_bounds and len(sp_bounds) == 4:
                        bx, by, bw, bh = sp_bounds
                        target_x = bx + (bw / 2.0)
                        target_y = by + (bh / 2.0)
                        self._active_target_coords = (target_x, target_y)
                        self._active_spotlight_bounds = sp_bounds
                        self.virtual_cursor.move_to(target_x, target_y, duration=0.22, smooth=True)
                        self._broadcast_telemetry("DEMONSTRATING", message=f"Demonstrating: {step.instruction}")
                elif eval_res.verification_method in ("app_window_ready", "window_switched"):
                    opened_app = step.target_app
                    if "about this mac" in combined_text or "system report" in combined_text:
                        opened_app = "System Information"
                    elif "more info" in combined_text or "system settings" in combined_text:
                        opened_app = "System Settings"
                    elif eval_res.verification_method == "window_switched" and "'" in eval_res.detail:
                        parts = eval_res.detail.split("'")
                        if len(parts) >= 2:
                            opened_app = parts[1]
                    win_b = self.grounder.get_window_bounds(opened_app) if opened_app else None
                    if win_b and len(win_b) == 4:
                        wx, wy, ww, wh = win_b
                        target_x = wx + (ww / 2.0)
                        target_y = wy + min(120.0, wh * 0.35)
                        self._active_target_coords = (target_x, target_y)
                        self._active_spotlight_bounds = win_b
                        self.virtual_cursor.move_to(target_x, target_y, duration=0.25, smooth=True)
                        self._broadcast_telemetry("DEMONSTRATING", message=f"Demonstrating: {step.instruction}")

            if self.evaluator.require_completion_before_advance and not eval_res.completed:
                self._schedule_assist_watchdog()
                return

            # Auto-advance to next step in guided demo mode once step evaluation passes
            if self.auto_advance and self._plan.mode != WalkthroughMode.INTERACTIVE_TUTOR:
                step_delay = (
                    self.auto_advance_delay
                    if self.mock
                    else max(self.auto_advance_delay, float(getattr(step, "post_delay_seconds", None) or 0.0))
                )
                self._schedule_auto_advance(delay=step_delay)

    def get_telemetry(self) -> Dict[str, Any]:
        """Produces serialized telemetry snapshot for API responses and SSE broadcasting."""
        plan = self._plan
        step_idx = self._current_step_index
        status = self._status
        coords = self._active_target_coords
        sp_bounds = self._active_spotlight_bounds
        last_eval = self._last_evaluation

        cur_step = None
        total_steps = 0
        goal = ""
        mode = "guided_demo"
        wt_id = ""

        if plan:
            wt_id = plan.walkthrough_id
            goal = plan.goal
            mode = plan.mode.value
            total_steps = len(plan.steps)
            if 1 <= step_idx <= total_steps:
                cur_step = plan.steps[step_idx - 1].to_dict()

        spotlight_dict = None
        if sp_bounds and len(sp_bounds) == 4:
            spotlight_dict = {
                "x": sp_bounds[0],
                "y": sp_bounds[1],
                "width": sp_bounds[2],
                "height": sp_bounds[3],
            }

        return {
            "type": "walkthrough",
            "walkthrough_id": wt_id,
            "goal": goal,
            "mode": mode,
            "status": status.value,
            "current_step_index": step_idx,
            "total_steps": total_steps,
            "step": cur_step,
            "steps": [s.to_dict() for s in plan.steps] if plan else [],
            "auto_advance": self.auto_advance,
            "step_verified": last_eval.completed if last_eval else True,
            "cursor_x": coords[0],
            "cursor_y": coords[1],
            "spotlight": spotlight_dict,
            "timestamp": time.time(),
        }

    def _broadcast_telemetry(self, event_tag: str, message: str = "") -> None:
        """Broadcasts telemetry to registered subscribers and ExecutionBus."""
        telemetry = self.get_telemetry()
        telemetry["tag"] = event_tag
        telemetry["message"] = message

        for listener in list(self._listeners):
            try:
                listener(telemetry)
            except Exception as e:
                logger.debug("Tutor listener error: %s", e)

        # Publish to general Clio execution bus
        try:
            self.bus.publish(
                ExecutionEvent(
                    event_type=EventType.STATUS_UPDATE,
                    task_id=self._plan.walkthrough_id if self._plan else "wt",
                    message=f"[Walkthrough] {message or event_tag}",
                    step_index=self._current_step_index,
                    total_steps=len(self._plan.steps) if self._plan else 0,
                    payload=telemetry,
                )
            )
        except Exception:
            pass
