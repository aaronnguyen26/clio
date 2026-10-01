"""Non-AI User Input & Trajectory Tracking Subsystem.

Path: src/memory/input_tracker.py
Belongs to Teach-Mode Demonstration and Dissection Pipeline.

Provides deterministic, zero-AI, OS-level ground-truth tracking for:
1. Continuous mouse movements, spatial decimation, velocity, and hover/dwell detection.
2. Clicks (left, right, middle, double, triple), drags, and scroll wheel accumulation.
3. Keyboard tracking with native macOS Unicode string extraction (via CGEventKeyboardGetUnicodeString),
   modifier tracking (Cmd, Shift, Alt, Ctrl), hotkey classification, and typing buffer coalescing.
4. Window context resolution, coordinate normalization, and window movement tracking.
5. Deterministic action dissection and ultra-compact AI payload compilation (minimal token consumption).

Zero external dependencies (pure Python standard library + macOS ctypes).
"""

from __future__ import annotations

import ctypes
from ctypes import (
    POINTER,
    byref,
    c_int,
    c_uint16,
    c_uint32,
    c_void_p,
)
from dataclasses import dataclass, field
from enum import Enum
import logging
import math
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple, Union

from src.memory.engine import redact_sensitive_text
from src.memory.models import (
    ActionType,
    CoordMode,
    TargetCoordinates,
    WorkflowSpec,
    WorkflowStep,
)
from src.memory.recorder import RawEvent, RawEventType, WindowBounds

logger = logging.getLogger(__name__)


# =============================================================================
# 1. Comprehensive macOS Keycode Mapping (Fallback for non-printable keys)
# =============================================================================

MACOS_VIRTUAL_KEYCODES: Dict[int, str] = {
    # Letters
    0: "a", 1: "s", 2: "d", 3: "f", 4: "h", 5: "g", 6: "z", 7: "x",
    8: "c", 9: "v", 11: "b", 12: "q", 13: "w", 14: "e", 15: "r",
    16: "y", 17: "t", 31: "o", 32: "u", 34: "i", 35: "p", 37: "l",
    38: "j", 40: "k", 45: "n", 46: "m",
    # Numbers
    18: "1", 19: "2", 20: "3", 21: "4", 23: "5", 22: "6", 26: "7",
    28: "8", 25: "9", 29: "0",
    # Punctuation & Symbols
    27: "-", 24: "=", 33: "[", 30: "]", 42: "\\", 41: ";", 39: "'",
    43: ",", 47: ".", 44: "/", 50: "`",
    # Editing & Navigation
    36: "Return", 48: "Tab", 49: "Space", 51: "BackSpace", 53: "Escape",
    117: "Delete", 115: "Home", 119: "End", 116: "PageUp", 121: "PageDown",
    123: "Left", 124: "Right", 125: "Down", 126: "Up",
    # Function Keys
    122: "F1", 120: "F2", 99: "F3", 118: "F4", 96: "F5", 97: "F6",
    98: "F7", 100: "F8", 101: "F9", 109: "F10", 103: "F11", 111: "F12",
    # Keypad
    65: "KeypadDecimal", 67: "KeypadMultiply", 69: "KeypadPlus", 71: "KeypadClear",
    75: "KeypadDivide", 76: "KeypadEnter", 78: "KeypadMinus", 81: "KeypadEquals",
    82: "Keypad0", 83: "Keypad1", 84: "Keypad2", 85: "Keypad3", 86: "Keypad4",
    87: "Keypad5", 88: "Keypad6", 89: "Keypad7", 91: "Keypad8", 92: "Keypad9",
}


# =============================================================================
# 2. Mouse Trajectory Tracking & Hover/Dwell Detection
# =============================================================================

@dataclass
class MouseTrajectoryPoint:
    """Represents a discrete cursor position sample with kinetic metrics."""
    x: float
    y: float
    timestamp: float
    velocity_px_s: float = 0.0
    is_dwell: bool = False
    window_bounds: Optional[WindowBounds] = None
    bundle_id: Optional[str] = None


@dataclass
class MouseDwellSegment:
    """Represents a period where the user paused/hovered the mouse over an area."""
    start_time: float
    end_time: float
    x: float
    y: float
    duration_s: float
    bundle_id: Optional[str] = None
    window_bounds: Optional[WindowBounds] = None


class MouseTrajectoryTracker:
    """Tracks continuous mouse movements, filters jitter, computes kinetics, and identifies dwells."""

    def __init__(
        self,
        min_move_distance_px: float = 3.0,
        min_sampling_interval_s: float = 0.016,  # ~60 Hz
        dwell_velocity_threshold: float = 15.0,  # px/second
        dwell_radius_px: float = 10.0,
        dwell_min_duration_s: float = 0.180,    # 180 ms threshold for deliberate dwell
    ) -> None:
        self.min_move_dist = min_move_distance_px
        self.min_interval = min_sampling_interval_s
        self.dwell_velocity_thresh = dwell_velocity_threshold
        self.dwell_radius = dwell_radius_px
        self.dwell_min_duration = dwell_min_duration_s

        self._points: List[MouseTrajectoryPoint] = []
        self._dwells: List[MouseDwellSegment] = []

        # Current dwell tracking state
        self._potential_dwell_start: Optional[float] = None
        self._potential_dwell_x: float = 0.0
        self._potential_dwell_y: float = 0.0
        self._potential_dwell_bundle: Optional[str] = None
        self._potential_dwell_bounds: Optional[WindowBounds] = None

    def reset(self) -> None:
        self._points.clear()
        self._dwells.clear()
        self._potential_dwell_start = None

    def add_point(
        self,
        x: float,
        y: float,
        timestamp: Optional[float] = None,
        bundle_id: Optional[str] = None,
        window_bounds: Optional[WindowBounds] = None,
    ) -> Optional[MouseTrajectoryPoint]:
        """Ingests a cursor coordinate sample, applies spatial/temporal decimation, and updates kinetics."""
        t = timestamp if timestamp is not None else time.time()

        if self._points:
            last = self._points[-1]
            dt = t - last.timestamp
            dist = math.hypot(x - last.x, y - last.y)

            # Spatial decimation: if time is too brief and distance is negligible, ignore micro-jitter
            if dt < self.min_interval and dist < self.min_move_dist:
                return None

            velocity = (dist / dt) if dt > 0.0001 else 0.0
        else:
            velocity = 0.0
            dist = 0.0

        is_dwell = self._update_dwell_detector(x, y, t, velocity, bundle_id, window_bounds)

        pt = MouseTrajectoryPoint(
            x=x,
            y=y,
            timestamp=t,
            velocity_px_s=velocity,
            is_dwell=is_dwell,
            window_bounds=window_bounds,
            bundle_id=bundle_id,
        )
        self._points.append(pt)
        return pt

    def _update_dwell_detector(
        self,
        x: float,
        y: float,
        t: float,
        velocity: float,
        bundle_id: Optional[str],
        window_bounds: Optional[WindowBounds],
    ) -> bool:
        """Determines if the mouse has settled into a deliberate hover/dwell zone."""
        if self._potential_dwell_start is None:
            # Begin tracking candidate dwell
            if velocity < self.dwell_velocity_thresh:
                self._potential_dwell_start = t
                self._potential_dwell_x = x
                self._potential_dwell_y = y
                self._potential_dwell_bundle = bundle_id
                self._potential_dwell_bounds = window_bounds
            return False

        # Existing candidate dwell active: check if mouse stayed within radius
        dist_from_anchor = math.hypot(x - self._potential_dwell_x, y - self._potential_dwell_y)
        if dist_from_anchor <= self.dwell_radius and velocity <= self.dwell_velocity_thresh * 1.5:
            duration = t - self._potential_dwell_start
            if duration >= self.dwell_min_duration:
                # Confirmed dwell
                return True
        else:
            # Mouse broke out of dwell zone: record segment if duration was significant
            duration = t - self._potential_dwell_start
            if duration >= self.dwell_min_duration:
                self._dwells.append(
                    MouseDwellSegment(
                        start_time=self._potential_dwell_start,
                        end_time=t,
                        x=self._potential_dwell_x,
                        y=self._potential_dwell_y,
                        duration_s=round(duration, 3),
                        bundle_id=self._potential_dwell_bundle,
                        window_bounds=self._potential_dwell_bounds,
                    )
                )
            self._potential_dwell_start = None
            if velocity < self.dwell_velocity_thresh:
                self._potential_dwell_start = t
                self._potential_dwell_x = x
                self._potential_dwell_y = y
                self._potential_dwell_bundle = bundle_id
                self._potential_dwell_bounds = window_bounds

        return False

    def get_dwell_preceding(self, x: float, y: float, timestamp: float, max_lookback_s: float = 0.6) -> Optional[float]:
        """Calculates how long the user hovered within proximity of (x, y) immediately prior to an action."""
        if not self._points:
            return None

        # Check in confirmed dwells first
        for dwell in reversed(self._dwells):
            if abs(dwell.end_time - timestamp) <= max_lookback_s:
                if math.hypot(dwell.x - x, dwell.y - y) <= self.dwell_radius * 2.5:
                    return dwell.duration_s

        # Also inspect points in the active window
        relevant_points = [
            p for p in self._points
            if 0 <= (timestamp - p.timestamp) <= max_lookback_s
            and math.hypot(p.x - x, p.y - y) <= self.dwell_radius * 2.5
        ]
        if len(relevant_points) >= 2:
            dwell_duration = relevant_points[-1].timestamp - relevant_points[0].timestamp
            if dwell_duration >= 0.1:
                return round(dwell_duration, 3)

        return None

    @property
    def points(self) -> List[MouseTrajectoryPoint]:
        return list(self._points)

    @property
    def dwells(self) -> List[MouseDwellSegment]:
        return list(self._dwells)

    def finalize(self, end_timestamp: Optional[float] = None) -> None:
        """Flushes any active candidate dwell at recording termination."""
        if self._potential_dwell_start is not None:
            t = end_timestamp if end_timestamp is not None else (self._points[-1].timestamp if self._points else time.time())
            duration = t - self._potential_dwell_start
            if duration >= self.dwell_min_duration:
                self._dwells.append(
                    MouseDwellSegment(
                        start_time=self._potential_dwell_start,
                        end_time=t,
                        x=self._potential_dwell_x,
                        y=self._potential_dwell_y,
                        duration_s=round(duration, 3),
                        bundle_id=self._potential_dwell_bundle,
                        window_bounds=self._potential_dwell_bounds,
                    )
                )
            self._potential_dwell_start = None

    def extract_trajectory_waypoints(
        self,
        epsilon_px: float = 8.0,
        max_waypoints: int = 24,
    ) -> List[Dict[str, Any]]:
        """Extracts significant trajectory waypoints using Ramer-Douglas-Peucker simplification
        and dwell/hover clustering.
        """
        self.finalize()
        if not self._points:
            return []

        if len(self._points) <= 2:
            pts = self._points
            res = []
            for idx, p in enumerate(pts):
                dur = (pts[idx].timestamp - pts[idx - 1].timestamp) if idx > 0 else 0.25
                res.append({
                    "x": p.x,
                    "y": p.y,
                    "timestamp": p.timestamp,
                    "duration_s": max(0.15, min(2.0, dur)),
                    "is_hover": p.is_dwell,
                    "dwell_s": 0.0,
                    "bundle_id": p.bundle_id,
                    "window_bounds": p.window_bounds,
                })
            return res

        # 1. RDP simplification on points
        def _rdp_indices(start_idx: int, end_idx: int) -> List[int]:
            if end_idx <= start_idx + 1:
                return [start_idx, end_idx]
            p_start = self._points[start_idx]
            p_end = self._points[end_idx]
            x1, y1 = p_start.x, p_start.y
            x2, y2 = p_end.x, p_end.y
            line_len = math.hypot(x2 - x1, y2 - y1)

            max_dist = 0.0
            max_idx = start_idx
            for k in range(start_idx + 1, end_idx):
                pk = self._points[k]
                if line_len > 1e-5:
                    dist = abs((y2 - y1) * pk.x - (x2 - x1) * pk.y + x2 * y1 - y2 * x1) / line_len
                else:
                    dist = math.hypot(pk.x - x1, pk.y - y1)
                if dist > max_dist:
                    max_dist = dist
                    max_idx = k

            if max_dist > epsilon_px:
                left = _rdp_indices(start_idx, max_idx)
                right = _rdp_indices(max_idx, end_idx)
                return left[:-1] + right
            return [start_idx, end_idx]

        selected_indices = set(_rdp_indices(0, len(self._points) - 1))

        # 2. Ensure every confirmed dwell is included in selected indices
        for dwell in self._dwells:
            best_idx = 0
            best_dt = float("inf")
            for idx, p in enumerate(self._points):
                dt = abs(p.timestamp - dwell.start_time)
                if dt < best_dt:
                    best_dt = dt
                    best_idx = idx
            selected_indices.add(best_idx)

        sorted_indices = sorted(selected_indices)

        # 3. Build waypoints list with kinetic durations and hover states
        waypoints: List[Dict[str, Any]] = []
        for i, idx in enumerate(sorted_indices):
            p = self._points[idx]
            dwell_match = next((d for d in self._dwells if math.hypot(d.x - p.x, d.y - p.y) <= self.dwell_radius * 2.0 and d.start_time <= p.timestamp <= d.end_time + 0.1), None)
            is_hover = dwell_match is not None or p.is_dwell
            dwell_time = dwell_match.duration_s if dwell_match else 0.0

            if waypoints:
                prev_p = waypoints[-1]
                dt = p.timestamp - prev_p["timestamp"]
                # Filter redundant points too close in space (< 15px)
                if math.hypot(p.x - prev_p["x"], p.y - prev_p["y"]) < 15.0:
                    if prev_p.get("is_hover") and is_hover:
                        prev_p["dwell_s"] = round(prev_p.get("dwell_s", 0.0) + dwell_time, 2)
                        prev_p["duration_s"] = round(max(0.15, min(2.0, p.timestamp - prev_p["timestamp"])), 2)
                    continue
                seg_dur = max(0.15, min(2.0, dt))
            else:
                seg_dur = 0.25

            waypoints.append({
                "x": p.x,
                "y": p.y,
                "timestamp": p.timestamp,
                "duration_s": round(seg_dur, 2),
                "is_hover": is_hover,
                "dwell_s": round(dwell_time, 2),
                "bundle_id": p.bundle_id,
                "window_bounds": p.window_bounds,
            })

        # Cap at max_waypoints if needed
        if len(waypoints) > max_waypoints:
            thinned = [waypoints[0]]
            for w in waypoints[1:-1]:
                if w["is_hover"] or len(thinned) < max_waypoints - 1:
                    thinned.append(w)
            thinned.append(waypoints[-1])
            waypoints = thinned

        return waypoints


# =============================================================================
# 3. Scroll Wheel Accumulator
# =============================================================================

@dataclass
class AccumulatedScroll:
    """Represents a coherent user scroll gesture combining continuous wheel ticks."""
    start_time: float
    end_time: float
    x: float
    y: float
    delta_x: float
    delta_y: float
    bundle_id: Optional[str] = None
    window_bounds: Optional[WindowBounds] = None


class ScrollWheelAccumulator:
    """Accumulates high-frequency scroll wheel ticks into discrete semantic scroll steps."""

    def __init__(self, max_idle_gap_s: float = 0.250) -> None:
        self.max_idle_gap = max_idle_gap_s
        self._current: Optional[AccumulatedScroll] = None
        self._completed: List[AccumulatedScroll] = []

    def feed_scroll(
        self,
        x: float,
        y: float,
        delta_x: float,
        delta_y: float,
        timestamp: Optional[float] = None,
        bundle_id: Optional[str] = None,
        window_bounds: Optional[WindowBounds] = None,
    ) -> Optional[AccumulatedScroll]:
        t = timestamp if timestamp is not None else time.time()

        if self._current is None:
            self._current = AccumulatedScroll(
                start_time=t,
                end_time=t,
                x=x,
                y=y,
                delta_x=delta_x,
                delta_y=delta_y,
                bundle_id=bundle_id,
                window_bounds=window_bounds,
            )
            return None

        # Check if continuous with active scroll
        dt = t - self._current.end_time
        dist = math.hypot(x - self._current.x, y - self._current.y)
        if dt <= self.max_idle_gap and dist <= 30.0:
            self._current.end_time = t
            self._current.delta_x += delta_x
            self._current.delta_y += delta_y
            return None
        else:
            # Previous scroll finished, flush it and begin new one
            finished = self._current
            self._completed.append(finished)
            self._current = AccumulatedScroll(
                start_time=t,
                end_time=t,
                x=x,
                y=y,
                delta_x=delta_x,
                delta_y=delta_y,
                bundle_id=bundle_id,
                window_bounds=window_bounds,
            )
            return finished

    def flush(self) -> Optional[AccumulatedScroll]:
        if self._current is not None:
            finished = self._current
            self._completed.append(finished)
            self._current = None
            return finished
        return None

    @property
    def completed_scrolls(self) -> List[AccumulatedScroll]:
        return list(self._completed)


# =============================================================================
# 4. Native Keyboard Extraction & Typing Stream Coalescer
# =============================================================================

@dataclass
class KeyboardEventData:
    """Rich keystroke representation with extracted Unicode character and modifier state."""
    keycode: int
    key_name: str
    unicode_char: str
    is_down: bool
    modifiers: List[str]
    timestamp: float
    bundle_id: Optional[str] = None


class KeyboardStreamTracker:
    """Processes low-level keystroke events, extracts true Unicode text, detects hotkeys, and buffers typing."""

    def __init__(self, pause_flush_threshold_s: float = 1.2) -> None:
        self.pause_threshold = pause_flush_threshold_s
        self._typing_buffer: List[str] = []
        self._typing_start_t: float = 0.0
        self._typing_last_t: float = 0.0
        self._typing_bundle: Optional[str] = None
        self._typing_bounds: Optional[WindowBounds] = None

        self._cg_lib: Optional[Any] = None
        self._init_cg_bindings()

    def _init_cg_bindings(self) -> None:
        if sys.platform != "darwin":
            return
        try:
            cg = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
            cg.CGEventKeyboardGetUnicodeString.argtypes = [c_void_p, c_uint32, POINTER(c_uint32), POINTER(c_uint16)]
            cg.CGEventKeyboardGetUnicodeString.restype = None
            self._cg_lib = cg
        except Exception as e:
            logger.debug("CoreGraphics Unicode extraction binding unavailable: %s", e)

    def extract_unicode_from_event_ref(self, event_ref: Any) -> str:
        """Extracts native Unicode string from macOS CGEventRef with zero loss of character fidelity."""
        if not event_ref or not self._cg_lib:
            return ""
        try:
            buf = (c_uint16 * 16)()
            actual_len = c_uint32(0)
            self._cg_lib.CGEventKeyboardGetUnicodeString(event_ref, 16, byref(actual_len), buf)
            if actual_len.value > 0:
                chars = [chr(buf[i]) for i in range(actual_len.value)]
                return "".join(chars)
        except Exception as e:
            logger.debug("Failed extracting Unicode from CGEventRef: %s", e)
        return ""

    @staticmethod
    def resolve_key_name(keycode: int, unicode_char: str = "") -> str:
        """Resolves a canonical key identifier."""
        if keycode in MACOS_VIRTUAL_KEYCODES:
            return MACOS_VIRTUAL_KEYCODES[keycode]
        if unicode_char and unicode_char.isprintable():
            return unicode_char
        return f"k_{keycode}"

    @staticmethod
    def extract_modifiers_from_flags(flags: int) -> List[str]:
        """Translates CoreGraphics CGEventFlags bitmask into a list of modifier names."""
        mods = []
        if flags & 0x00100000:  # kCGEventFlagMaskCommand
            mods.append("cmd")
        if flags & 0x00020000:  # kCGEventFlagMaskShift
            mods.append("shift")
        if flags & 0x00080000:  # kCGEventFlagMaskAlternate
            mods.append("alt")
        if flags & 0x00040000:  # kCGEventFlagMaskControl
            mods.append("ctrl")
        if flags & 0x00800000:  # kCGEventFlagMaskSecondaryFn
            mods.append("fn")
        return mods

    def is_hotkey(self, key_name: str, modifiers: List[str]) -> bool:
        """Determines if the keystroke represents a command hotkey vs typing text."""
        mods_lower = {m.lower() for m in modifiers}
        has_cmd_or_ctrl = bool(mods_lower.intersection({"cmd", "command", "ctrl", "control", "alt", "option"}))
        if has_cmd_or_ctrl:
            return True

        # Shift + navigation/control keys count as hotkeys (e.g. Shift+Tab, Shift+Return)
        is_nav_or_ctrl = key_name.lower() in (
            "tab", "return", "enter", "escape", "esc", "backspace", "delete",
            "up", "down", "left", "right", "home", "end", "pageup", "pagedown",
        )
        if "shift" in mods_lower and is_nav_or_ctrl:
            return True

        # Function keys (F1-F12) are always hotkeys
        if key_name.upper().startswith("F") and len(key_name) in (2, 3) and key_name[1:].isdigit():
            return True

        return False

    def process_key_event(
        self,
        event_data: KeyboardEventData,
        window_bounds: Optional[WindowBounds] = None,
    ) -> List[Tuple[WorkflowStep, Tuple[float, float]]]:
        """Processes a single keystroke event, buffering typing or producing discrete steps."""
        if not event_data.is_down:
            return []

        produced: List[Tuple[WorkflowStep, Tuple[float, float]]] = []
        key_name = event_data.key_name
        mods = event_data.modifiers
        unicode_str = event_data.unicode_char
        now = event_data.timestamp
        bundle = event_data.bundle_id

        # 1. Check if this is a command hotkey
        if self.is_hotkey(key_name, mods):
            flushed_step, flushed_ts = self.flush_typing()
            if flushed_step and flushed_ts:
                produced.append((flushed_step, flushed_ts))

            hotkey_keys = [m.lower() for m in mods]
            k_lower = key_name.lower()
            if k_lower not in hotkey_keys:
                hotkey_keys.append(k_lower)

            target: Dict[str, Any] = {}
            app_clean = ""
            if bundle:
                app_clean = bundle.split(".")[-1].capitalize()
                target["bundle_id"] = bundle
                target["app_name"] = app_clean
            if window_bounds and window_bounds.width > 0:
                target["window_bounds"] = {
                    "x": window_bounds.x,
                    "y": window_bounds.y,
                    "width": window_bounds.width,
                    "height": window_bounds.height,
                }

            canonical_combo = "+".join(k.capitalize() for k in hotkey_keys)
            semantic_hotkeys = {
                ("cmd", "c"): "Copy selection",
                ("cmd", "v"): "Paste",
                ("cmd", "s"): "Save",
                ("cmd", "a"): "Select all",
                ("cmd", "f"): "Find",
                ("cmd", "z"): "Undo",
                ("cmd", "shift", "z"): "Redo",
                ("cmd", "w"): "Close window",
                ("cmd", "q"): "Quit",
                ("cmd", "t"): "New tab",
                ("cmd", "n"): "New document",
                ("cmd", "space"): "Open Spotlight",
            }
            combo_tuple = tuple(hotkey_keys)
            app_suffix = f" in {app_clean}" if app_clean else ""
            if combo_tuple in semantic_hotkeys:
                desc = f"{semantic_hotkeys[combo_tuple]} ({canonical_combo}){app_suffix}"
            else:
                desc = f"Press {canonical_combo}{app_suffix}"

            step = WorkflowStep(
                step_id=f"step_{now:.3f}_hotkey",
                order=1,
                description=desc,
                action=ActionType.PRESS_HOTKEY,
                payload={
                    "keys": hotkey_keys,
                    "modifiers": [m for m in hotkey_keys if m in ("cmd", "shift", "alt", "ctrl", "fn")],
                },
                target=target,
            )
            produced.append((step, (now, now + 0.05)))
            return produced

        # 2. Check special non-printable control keys (Return, Tab, Escape, Navigation)
        k_lower = key_name.lower()
        if k_lower in ("return", "enter", "tab", "escape", "esc", "up", "down", "left", "right"):
            flushed_step, flushed_ts = self.flush_typing()
            if flushed_step and flushed_ts:
                produced.append((flushed_step, flushed_ts))

            target = {}
            app_clean = ""
            if bundle:
                app_clean = bundle.split(".")[-1].capitalize()
                target["bundle_id"] = bundle
                target["app_name"] = app_clean
            if window_bounds and window_bounds.width > 0:
                target["window_bounds"] = {
                    "x": window_bounds.x,
                    "y": window_bounds.y,
                    "width": window_bounds.width,
                    "height": window_bounds.height,
                }

            ctrl_semantic = {
                "return": "Submit (Return)",
                "enter": "Submit (Enter)",
                "tab": "Next field (Tab)",
                "escape": "Cancel / Dismiss (Escape)",
                "esc": "Cancel / Dismiss (Escape)",
                "up": "Navigate Up",
                "down": "Navigate Down",
                "left": "Navigate Left",
                "right": "Navigate Right",
            }
            app_suffix = f" in {app_clean}" if app_clean else ""
            desc = f"{ctrl_semantic.get(k_lower, f'Press {key_name.capitalize()}')}{app_suffix}"

            step = WorkflowStep(
                step_id=f"step_{now:.3f}_hotkey",
                order=1,
                description=desc,
                action=ActionType.PRESS_HOTKEY,
                payload={"keys": [k_lower]},
                target=target,
            )
            produced.append((step, (now, now + 0.05)))
            return produced

        # 3. Handle Backspace in typing buffer
        if k_lower in ("backspace", "delete"):
            if self._typing_buffer:
                self._typing_buffer.pop()
                self._typing_last_t = now
                return []
            else:
                target = {}
                app_clean = ""
                if bundle:
                    app_clean = bundle.split(".")[-1].capitalize()
                    target["bundle_id"] = bundle
                    target["app_name"] = app_clean
                if window_bounds and window_bounds.width > 0:
                    target["window_bounds"] = {
                        "x": window_bounds.x,
                        "y": window_bounds.y,
                        "width": window_bounds.width,
                        "height": window_bounds.height,
                    }
                app_suffix = f" in {app_clean}" if app_clean else ""
                step = WorkflowStep(
                    step_id=f"step_{now:.3f}_hotkey",
                    order=1,
                    description=f"Delete selection (BackSpace){app_suffix}",
                    action=ActionType.PRESS_HOTKEY,
                    payload={"keys": ["backspace"]},
                    target=target,
                )
                produced.append((step, (now, now + 0.05)))
                return produced

        # 4. Printable character typing
        char_to_add = unicode_str if unicode_str and unicode_str.isprintable() else (
            " " if k_lower == "space" else (key_name if len(key_name) == 1 else "")
        )

        if char_to_add:
            if not self._typing_buffer:
                self._typing_start_t = now
                self._typing_bundle = bundle
                self._typing_bounds = window_bounds
            elif (now - self._typing_last_t) > self.pause_threshold or (bundle != self._typing_bundle):
                # Idle gap or app switch: flush prior typing chunk and start new one
                flushed_step, flushed_ts = self.flush_typing()
                if flushed_step and flushed_ts:
                    produced.append((flushed_step, flushed_ts))
                self._typing_start_t = now
                self._typing_bundle = bundle
                self._typing_bounds = window_bounds

            self._typing_buffer.append(char_to_add)
            self._typing_last_t = now

        return produced

    def flush_typing(self) -> Tuple[Optional[WorkflowStep], Optional[Tuple[float, float]]]:
        """Flushes accumulated keystrokes into a concrete TYPE_TEXT or PASTE_TEXT WorkflowStep."""
        if not self._typing_buffer:
            return None, None

        full_text = "".join(self._typing_buffer)
        self._typing_buffer.clear()

        # Sensitive credential redaction
        clean_text = redact_sensitive_text(full_text)
        action = ActionType.PASTE_TEXT if len(clean_text) >= 60 else ActionType.TYPE_TEXT

        bundle = self._typing_bundle
        app_name = bundle.split(".")[-1].capitalize() if bundle else ""
        app_suffix = f" in {app_name}" if app_name else ""

        if action == ActionType.PASTE_TEXT:
            desc = f"Paste text ({len(clean_text)} chars){app_suffix}"
        else:
            preview = clean_text.strip()
            if not preview:
                desc = f"Enter text{app_suffix}"
            elif len(preview) > 25:
                desc = f"Type '{preview[:22]}...'{app_suffix}"
            else:
                desc = f"Type '{preview}'{app_suffix}"

        target = {}
        if bundle:
            target["bundle_id"] = bundle
            target["app_name"] = app_name
        if self._typing_bounds and self._typing_bounds.width > 0:
            target["window_bounds"] = {
                "x": self._typing_bounds.x,
                "y": self._typing_bounds.y,
                "width": self._typing_bounds.width,
                "height": self._typing_bounds.height,
            }

        step = WorkflowStep(
            step_id=f"step_{self._typing_start_t:.3f}_text",
            order=1,
            description=desc,
            action=action,
            payload={"text": clean_text, "char_count": len(clean_text)},
            target=target,
        )
        ts_range = (self._typing_start_t, self._typing_last_t)
        return step, ts_range


# =============================================================================
# 5. Non-AI Ground-Truth Dissection Engine & AI Payload Compiler
# =============================================================================

class DeterministicActionDissector:
    """Compiles tracked mouse movements, clicks, drags, scrolls, and keystrokes

    into a complete, noise-free WorkflowSpec without invoking external AI models.
    Also produces the exact ultra-minimal JSON payload ready for optional semantic AI naming.
    """

    def __init__(self) -> None:
        self.mouse_tracker = MouseTrajectoryTracker()
        self.scroll_accumulator = ScrollWheelAccumulator()
        self.keyboard_tracker = KeyboardStreamTracker()

    def dissect_raw_events(
        self,
        raw_events: List[RawEvent],
        session_id: str = "",
        name: str = "Demonstrated Task",
        canonical_trigger: str = "",
        target_bundle_id: str = "",
    ) -> Tuple[WorkflowSpec, Dict[str, Any]]:
        """Processes raw events into a 100% deterministic WorkflowSpec and compiles the ultra-minimal AI payload.

        Returns:
            (workflow_spec, minimal_ai_payload_dict)
        """
        # Reset tracker states for complete idempotency across sessions
        self.mouse_tracker = MouseTrajectoryTracker()
        self.keyboard_tracker = KeyboardStreamTracker()
        self.scroll_accumulator = ScrollWheelAccumulator()

        # Sort chronologically by timestamp
        raw_events = sorted(raw_events, key=lambda e: getattr(e, "timestamp", 0.0) or 0.0)

        # Deduplicate redundant events from overlapping capture channels (CGEventTap + NSEvent global monitor + stop payload buffer)
        deduped_events: List[RawEvent] = []
        for ev in raw_events:
            ev_type_str = ev.event_type.value if hasattr(ev.event_type, "value") else str(ev.event_type).lower()
            is_dup = False
            for prev in reversed(deduped_events[-8:]):
                prev_type_str = prev.event_type.value if hasattr(prev.event_type, "value") else str(prev.event_type).lower()
                dt = abs(ev.timestamp - prev.timestamp)
                if ev_type_str == prev_type_str:
                    if ev_type_str in ("mouse_move", "mousemove", "mouse_down", "mousedown", "mouse_up", "mouseup", "click", "mouse_drag", "mousedrag"):
                        if dt <= 0.035 and math.hypot(ev.x - prev.x, ev.y - prev.y) <= 4.0 and getattr(ev, "button", "left") == getattr(prev, "button", "left"):
                            is_dup = True
                            break
                    elif ev_type_str in ("key_down", "keydown"):
                        if dt <= 0.025 and getattr(ev, "key", "") == getattr(prev, "key", ""):
                            is_dup = True
                            break
                if dt > 0.050:
                    break
            if not is_dup:
                deduped_events.append(ev)
        raw_events = deduped_events

        steps: List[WorkflowStep] = []
        step_timestamps: List[Tuple[float, float]] = []

        current_app: Optional[str] = target_bundle_id or None
        current_window_bounds: Optional[WindowBounds] = None
        i = 0
        n = len(raw_events)

        def _emit_scroll_step(scroll_item: AccumulatedScroll) -> None:
            nonlocal steps, step_timestamps, current_app
            step_order = len(steps) + 1
            app_clean = (scroll_item.bundle_id or current_app or "").split(".")[-1].capitalize()
            target_dict: Dict[str, Any] = {"screen_x": int(scroll_item.x), "screen_y": int(scroll_item.y)}
            if scroll_item.bundle_id:
                target_dict["bundle_id"] = scroll_item.bundle_id
            if app_clean:
                target_dict["app_name"] = app_clean
            if scroll_item.window_bounds and scroll_item.window_bounds.width > 0:
                target_dict["window_bounds"] = {
                    "x": scroll_item.window_bounds.x,
                    "y": scroll_item.window_bounds.y,
                    "width": scroll_item.window_bounds.width,
                    "height": scroll_item.window_bounds.height,
                }
                target_dict["norm_x"] = round((scroll_item.x - scroll_item.window_bounds.x) / scroll_item.window_bounds.width, 4)
                target_dict["norm_y"] = round((scroll_item.y - scroll_item.window_bounds.y) / scroll_item.window_bounds.height, 4)

            direction = "down" if scroll_item.delta_y < 0 else "up"
            if abs(scroll_item.delta_x) > abs(scroll_item.delta_y):
                direction = "right" if scroll_item.delta_x > 0 else "left"
            app_suffix = f" in {app_clean}" if app_clean else ""
            desc = f"Scroll {direction}{app_suffix}"

            steps.append(
                WorkflowStep(
                    step_id=f"step_{step_order}_scroll",
                    order=step_order,
                    description=desc,
                    action=ActionType.SCROLL,
                    payload={
                        "delta_x": scroll_item.delta_x,
                        "delta_y": scroll_item.delta_y,
                        "direction": direction,
                        "ticks": max(1, int(abs(scroll_item.delta_y or scroll_item.delta_x) / 10)),
                    },
                    target=target_dict,
                    coordinates=TargetCoordinates(
                        mode=CoordMode.WINDOW_RELATIVE_RATIO if "norm_x" in target_dict else CoordMode.SCREEN_ABSOLUTE,
                        norm_x=target_dict.get("norm_x"),
                        norm_y=target_dict.get("norm_y"),
                        abs_x=int(scroll_item.x),
                        abs_y=int(scroll_item.y),
                    ),
                )
            )
            step_timestamps.append((scroll_item.start_time, scroll_item.end_time))

        while i < n:
            ev = raw_events[i]
            ev_type = ev.event_type.value if hasattr(ev.event_type, "value") else str(ev.event_type).lower()

            # 1. Update application context
            if ev.bundle_id and ev.bundle_id not in ("com.apple.dock", "com.clio.desktop", "clio-bar"):
                if current_app is not None and ev.bundle_id != current_app:
                    # Flush pending scroll
                    flushed_scroll = self.scroll_accumulator.flush()
                    if flushed_scroll:
                        _emit_scroll_step(flushed_scroll)

                    # Flush pending typing
                    flushed_s, flushed_t = self.keyboard_tracker.flush_typing()
                    if flushed_s:
                        flushed_s.order = len(steps) + 1
                        steps.append(flushed_s)
                        step_timestamps.append(flushed_t or (ev.timestamp, ev.timestamp))

                    app_clean = ev.bundle_id.split(".")[-1].capitalize()
                    step_order = len(steps) + 1
                    steps.append(
                        WorkflowStep(
                            step_id=f"step_{step_order}_focus",
                            order=step_order,
                            description=f"Switch to {app_clean}",
                            action=ActionType.FOCUS_APP,
                            target={"bundle_id": ev.bundle_id, "app_name": app_clean},
                        )
                    )
                    step_timestamps.append((ev.timestamp, ev.timestamp + 0.05))
                current_app = ev.bundle_id

            # 2. Mouse Move
            if ev_type in ("mouse_move", "mousemove"):
                if self.scroll_accumulator._current:
                    dt = ev.timestamp - self.scroll_accumulator._current.end_time
                    dist = math.hypot(ev.x - self.scroll_accumulator._current.x, ev.y - self.scroll_accumulator._current.y)
                    if dt > self.scroll_accumulator.max_idle_gap or dist > 50.0:
                        flushed_scroll = self.scroll_accumulator.flush()
                        if flushed_scroll:
                            _emit_scroll_step(flushed_scroll)

                self.mouse_tracker.add_point(
                    x=ev.x,
                    y=ev.y,
                    timestamp=ev.timestamp,
                    bundle_id=ev.bundle_id or current_app,
                    window_bounds=ev.window_bounds,
                )
                i += 1
                continue

            # 3. Scroll Wheel
            if ev_type in ("mouse_scroll", "scroll", "scroll_wheel"):
                # Flush pending typing
                flushed_s, flushed_t = self.keyboard_tracker.flush_typing()
                if flushed_s:
                    flushed_s.order = len(steps) + 1
                    steps.append(flushed_s)
                    step_timestamps.append(flushed_t or (ev.timestamp, ev.timestamp))

                dx = getattr(ev, "delta_x", 0.0)
                dy = getattr(ev, "delta_y", 0.0)
                flushed = self.scroll_accumulator.feed_scroll(
                    x=ev.x,
                    y=ev.y,
                    delta_x=dx,
                    delta_y=dy,
                    timestamp=ev.timestamp,
                    bundle_id=ev.bundle_id or current_app,
                    window_bounds=ev.window_bounds,
                )
                if flushed:
                    _emit_scroll_step(flushed)
                i += 1
                continue

            # 4. Keyboard Events
            if ev_type in ("key_down", "keydown", "key_char", "keychar"):
                flushed_scroll = self.scroll_accumulator.flush()
                if flushed_scroll:
                    _emit_scroll_step(flushed_scroll)
                raw_key = ev.key or ""
                mods = ev.modifiers or []
                unicode_c = getattr(ev, "unicode_char", "") or (raw_key if len(raw_key) == 1 else "")
                keycode = getattr(ev, "keycode", 0)

                ev_data = KeyboardEventData(
                    keycode=keycode,
                    key_name=raw_key,
                    unicode_char=unicode_c,
                    is_down=True,
                    modifiers=mods,
                    timestamp=ev.timestamp,
                    bundle_id=ev.bundle_id or current_app,
                )
                produced_steps = self.keyboard_tracker.process_key_event(ev_data, ev.window_bounds)
                for k_step, k_ts in produced_steps:
                    k_step.order = len(steps) + 1
                    steps.append(k_step)
                    step_timestamps.append(k_ts or (ev.timestamp, ev.timestamp))
                i += 1
                continue

            # 5. Mouse Down / Click / Drag
            if ev_type in ("mouse_down", "mousedown", "click"):
                # Flush pending scroll
                flushed_scroll = self.scroll_accumulator.flush()
                if flushed_scroll:
                    _emit_scroll_step(flushed_scroll)

                # Flush pending typing
                flushed_s, flushed_t = self.keyboard_tracker.flush_typing()
                if flushed_s:
                    flushed_s.order = len(steps) + 1
                    steps.append(flushed_s)
                    step_timestamps.append(flushed_t or (ev.timestamp, ev.timestamp))

                click_x = ev.x
                click_y = ev.y
                click_start_t = ev.timestamp
                button = ev.button or "left"

                # Check preceding dwell
                dwell_duration = self.mouse_tracker.get_dwell_preceding(click_x, click_y, click_start_t)

                # Look ahead for mouse_up / drag
                up_ev: Optional[RawEvent] = None
                last_drag: Optional[RawEvent] = None
                j = i + 1
                while j < n:
                    nxt = raw_events[j]
                    nxt_type = nxt.event_type.value if hasattr(nxt.event_type, "value") else str(nxt.event_type).lower()
                    if nxt_type in ("mouse_up", "mouseup"):
                        up_ev = nxt
                        i = j
                        break
                    elif nxt_type in ("mouse_drag", "mousedrag", "mouse_move", "mousemove"):
                        last_drag = nxt
                        j += 1
                    else:
                        break

                click_end_t = up_ev.timestamp if up_ev else (last_drag.timestamp if last_drag else click_start_t + 0.05)
                end_x = up_ev.x if up_ev else (last_drag.x if last_drag else click_x)
                end_y = up_ev.y if up_ev else (last_drag.y if last_drag else click_y)

                drag_dist = math.hypot(end_x - click_x, end_y - click_y)
                if drag_dist > 8.0:
                    # Coalesced Drag
                    step_order = len(steps) + 1
                    app_clean = (ev.bundle_id or current_app or "").split(".")[-1].capitalize()
                    app_suffix = f" in {app_clean}" if app_clean else ""
                    src_ocr = getattr(ev, "recognized_text", "").strip()
                    dst_ocr = getattr(up_ev, "recognized_text", "").strip() if up_ev else ""

                    if src_ocr and dst_ocr:
                        desc = f"Drag '{src_ocr}' to '{dst_ocr}'{app_suffix}"
                    elif src_ocr:
                        desc = f"Drag '{src_ocr}' to ({int(end_x)}, {int(end_y)}){app_suffix}"
                    else:
                        desc = f"Drag from ({int(click_x)}, {int(click_y)}) to ({int(end_x)}, {int(end_y)}){app_suffix}"

                    target_dict = {
                        "screen_x": int(end_x),
                        "screen_y": int(end_y),
                        "start_x": int(click_x),
                        "start_y": int(click_y),
                    }
                    if ev.bundle_id or current_app:
                        target_dict["bundle_id"] = ev.bundle_id or current_app
                        target_dict["app_name"] = app_clean
                    if src_ocr:
                        target_dict["source_text"] = src_ocr
                    if dst_ocr:
                        target_dict["destination_text"] = dst_ocr
                    if ev.window_bounds and ev.window_bounds.width > 0:
                        target_dict["window_bounds"] = {
                            "x": ev.window_bounds.x,
                            "y": ev.window_bounds.y,
                            "width": ev.window_bounds.width,
                            "height": ev.window_bounds.height,
                        }
                        target_dict["norm_start_x"] = round((click_x - ev.window_bounds.x) / ev.window_bounds.width, 4)
                        target_dict["norm_start_y"] = round((click_y - ev.window_bounds.y) / ev.window_bounds.height, 4)
                        target_dict["norm_x"] = round((end_x - ev.window_bounds.x) / ev.window_bounds.width, 4)
                        target_dict["norm_y"] = round((end_y - ev.window_bounds.y) / ev.window_bounds.height, 4)

                    payload_dict: Dict[str, Any] = {
                        "start_x": int(click_x),
                        "start_y": int(click_y),
                        "end_x": int(end_x),
                        "end_y": int(end_y),
                        "duration": max(0.1, round(click_end_t - click_start_t, 2)),
                        "drag_distance_px": round(drag_dist, 1),
                    }
                    v_delta = float(getattr(up_ev, "visual_delta", 0.0) if up_ev else getattr(ev, "visual_delta", 0.0))
                    if v_delta > 0:
                        payload_dict["visual_delta"] = round(v_delta, 4)

                    verif_dict: Dict[str, Any] = {}
                    if v_delta > 0:
                        verif_dict["visual_delta"] = round(v_delta, 4)
                        verif_dict["visual_confirmed"] = bool(v_delta > 0.015)
                        verif_dict["condition"] = "visual_change"

                    steps.append(
                        WorkflowStep(
                            step_id=f"step_{step_order}_drag",
                            order=step_order,
                            description=desc,
                            action=ActionType.DRAG,
                            payload=payload_dict,
                            target=target_dict,
                            verification=verif_dict,
                            coordinates=TargetCoordinates(
                                mode=CoordMode.WINDOW_RELATIVE_RATIO if "norm_x" in target_dict else CoordMode.SCREEN_ABSOLUTE,
                                norm_x=target_dict.get("norm_x"),
                                norm_y=target_dict.get("norm_y"),
                                abs_x=int(end_x),
                                abs_y=int(end_y),
                            ),
                        )
                    )
                    step_timestamps.append((click_start_t, click_end_t))
                    i += 1
                    continue

                # Coalesced Click
                target_dict = {
                    "screen_x": int(click_x),
                    "screen_y": int(click_y),
                    "x": int(click_x),
                    "y": int(click_y),
                }
                app_clean = (ev.bundle_id or current_app or "").split(".")[-1].capitalize()
                if ev.bundle_id or current_app:
                    target_dict["bundle_id"] = ev.bundle_id or current_app
                    target_dict["app_name"] = app_clean

                if ev.window_bounds and ev.window_bounds.width > 0 and ev.window_bounds.height > 0:
                    norm_x = (click_x - ev.window_bounds.x) / ev.window_bounds.width
                    norm_y = (click_y - ev.window_bounds.y) / ev.window_bounds.height
                    target_dict["norm_x"] = round(max(0.0, min(1.0, norm_x)), 4)
                    target_dict["norm_y"] = round(max(0.0, min(1.0, norm_y)), 4)
                    target_dict["window_bounds"] = {
                        "x": ev.window_bounds.x,
                        "y": ev.window_bounds.y,
                        "width": ev.window_bounds.width,
                        "height": ev.window_bounds.height,
                    }

                ocr_text = (getattr(ev, "recognized_text", "") or (getattr(up_ev, "recognized_text", "") if up_ev else "")).strip()
                if ocr_text:
                    target_dict["element_text"] = ocr_text

                if dwell_duration:
                    target_dict["hover_dwell_s"] = dwell_duration

                v_delta = float(getattr(ev, "visual_delta", 0.0) or (getattr(up_ev, "visual_delta", 0.0) if up_ev else 0.0))

                # Tri-Factor Target Anchor
                target_dict["tri_factor_anchor"] = {
                    "factor_1_ax": {"ax_title": ocr_text, "bundle_id": ev.bundle_id or current_app or ""},
                    "factor_2_ratio": {"norm_x": target_dict.get("norm_x"), "norm_y": target_dict.get("norm_y")},
                    "factor_3_visual": {"visual_delta": v_delta, "recognized_text": ocr_text},
                }

                # Approach trajectory metrics from MouseTrajectoryTracker
                payload_dict: Dict[str, Any] = {
                    "button": button,
                    "click_count": 1,
                    "x": int(click_x),
                    "y": int(click_y),
                    "screen_x": int(click_x),
                    "screen_y": int(click_y),
                }
                if dwell_duration:
                    payload_dict["dwell_s"] = dwell_duration
                    payload_dict["hover"] = bool(dwell_duration >= 0.18)
                recent_pts = [p for p in self.mouse_tracker.points if 0 < (click_start_t - p.timestamp) <= 0.35]
                if recent_pts:
                    dist_appr = math.hypot(click_x - recent_pts[0].x, click_y - recent_pts[0].y)
                    dt_appr = click_start_t - recent_pts[0].timestamp
                    if dt_appr > 0.01:
                        payload_dict["approach_speed_px_s"] = round(dist_appr / dt_appr, 1)
                        payload_dict["approach_distance_px"] = round(dist_appr, 1)
                if v_delta > 0:
                    payload_dict["visual_delta"] = round(v_delta, 4)

                verif_dict: Dict[str, Any] = {}
                if ocr_text:
                    verif_dict["expected_text"] = ocr_text
                if v_delta > 0:
                    verif_dict["visual_delta"] = round(v_delta, 4)
                    verif_dict["visual_confirmed"] = bool(v_delta > 0.015)
                if ocr_text or v_delta > 0.015:
                    verif_dict["condition"] = "visual_or_element"

                # Check if double click
                is_double = False
                if steps and steps[-1].action == ActionType.CLICK:
                    prev_target = steps[-1].target
                    prev_t_end = step_timestamps[-1][1]
                    dist_to_prev = math.hypot(
                        target_dict["screen_x"] - prev_target.get("screen_x", 0),
                        target_dict["screen_y"] - prev_target.get("screen_y", 0),
                    )
                    if dist_to_prev <= 6.0 and (click_start_t - prev_t_end <= 0.400):
                        steps[-1].action = ActionType.DOUBLE_CLICK
                        app_ctx = f" in {app_clean}" if app_clean else ""
                        if ocr_text:
                            steps[-1].description = f"Double click '{ocr_text}' at ({int(click_x)}, {int(click_y)}){app_ctx}"
                        else:
                            steps[-1].description = f"Double click target at ({int(click_x)}, {int(click_y)}){app_ctx}"
                        steps[-1].payload["click_count"] = 2
                        is_double = True

                if not is_double:
                    step_order = len(steps) + 1
                    action_type = ActionType.RIGHT_CLICK if button == "right" else ActionType.CLICK
                    action_verb = "Right-click" if button == "right" else "Click"
                    app_ctx = f" in {app_clean}" if app_clean else ""

                    # 1. Detect Dock App Launch
                    is_dock_click = (
                        (ev.bundle_id == "com.apple.dock")
                        or (app_clean.lower() == "dock")
                        or (click_y > 850 and not ev.window_bounds)
                    )
                    if is_dock_click and button != "right":
                        target_app_name = ocr_text
                        target_bundle = ""
                        if not target_app_name:
                            # Look ahead for which application launched or became frontmost
                            for k in range(i + 1, min(n, i + 15)):
                                nxt_ev = raw_events[k]
                                if nxt_ev.bundle_id and nxt_ev.bundle_id not in ("com.apple.dock", "com.clio.desktop", "clio-bar"):
                                    target_bundle = nxt_ev.bundle_id
                                    target_app_name = target_bundle.split(".")[-1].capitalize()
                                    break
                        if target_app_name:
                            desc = f"Open {target_app_name} from Dock"
                            action_type = ActionType.LAUNCH_APP
                            target_dict["app_name"] = target_app_name
                            target_dict["bundle_id"] = target_bundle or f"com.apple.{target_app_name}"
                            target_dict["launch_method"] = "dock"
                            current_app = target_bundle or target_app_name
                        else:
                            desc = f"Click Dock at ({int(click_x)}, {int(click_y)})"

                    # 2. Detect Tab Opening via '+' Click
                    elif ocr_text in ("+", "new tab", "New Tab") and button != "right":
                        desc = f"Open new tab (+) at ({int(click_x)}, {int(click_y)}){app_ctx}"
                        target_dict["intent"] = "open_tab"
                        payload_dict["intent"] = "open_tab"

                    # 3. Detect Intra-App Window Switch Click
                    elif (
                        current_window_bounds is not None
                        and ev.window_bounds is not None
                        and ev.bundle_id == current_app
                        and (
                            abs(ev.window_bounds.x - current_window_bounds.x) > 30
                            or abs(ev.window_bounds.y - current_window_bounds.y) > 30
                            or abs(ev.window_bounds.width - current_window_bounds.width) > 30
                            or abs(ev.window_bounds.height - current_window_bounds.height) > 30
                        )
                        and (target_dict.get("norm_y", 1.0) < 0.12 or (click_y - ev.window_bounds.y) < 35)
                    ):
                        desc = f"Switch to window at ({int(click_x)}, {int(click_y)}){app_ctx}"
                        action_type = ActionType.FOCUS_APP
                        target_dict["window_switch"] = True
                        target_dict["window_bounds"] = {
                            "x": ev.window_bounds.x,
                            "y": ev.window_bounds.y,
                            "width": ev.window_bounds.width,
                            "height": ev.window_bounds.height,
                        }

                    elif ocr_text:
                        desc = f"{action_verb} '{ocr_text}' at ({int(click_x)}, {int(click_y)}){app_ctx}"
                    else:
                        desc = f"{action_verb} target at ({int(click_x)}, {int(click_y)}){app_ctx}"

                    if ev.window_bounds:
                        current_window_bounds = ev.window_bounds

                    steps.append(
                        WorkflowStep(
                            step_id=f"step_{step_order}_click",
                            order=step_order,
                            description=desc,
                            action=action_type,
                            payload=payload_dict,
                            target=target_dict,
                            verification=verif_dict,
                            coordinates=TargetCoordinates(
                                mode=CoordMode.WINDOW_RELATIVE_RATIO if "norm_x" in target_dict else CoordMode.SCREEN_ABSOLUTE,
                                norm_x=target_dict.get("norm_x"),
                                norm_y=target_dict.get("norm_y"),
                                abs_x=int(click_x),
                                abs_y=int(click_y),
                            ),
                        )
                    )
                    step_timestamps.append((click_start_t, click_end_t))

                i += 1
                continue

            i += 1

        # Final flush for keyboard buffer
        final_k_step, final_k_ts = self.keyboard_tracker.flush_typing()
        if final_k_step:
            final_k_step.order = len(steps) + 1
            steps.append(final_k_step)
            step_timestamps.append(final_k_ts or (time.time(), time.time()))

        # Flush final scroll if any
        final_scroll = self.scroll_accumulator.flush()
        if final_scroll:
            _emit_scroll_step(final_scroll)

        # Check if steps contains any interactive actions (clicks, drags, typing, hotkeys, scrolls)
        has_interactive_action = any(
            s.action in (
                ActionType.CLICK,
                ActionType.DOUBLE_CLICK,
                ActionType.RIGHT_CLICK,
                ActionType.DRAG,
                ActionType.SCROLL,
                ActionType.TYPE_TEXT,
                ActionType.PASTE_TEXT,
                ActionType.PRESS_HOTKEY,
            )
            for s in steps
        )

        # If there are no interactive actions, extract user mouse trajectory as move_mouse and hover steps
        if not has_interactive_action and len(self.mouse_tracker.points) >= 2:
            movement_waypoints = self.mouse_tracker.extract_trajectory_waypoints(epsilon_px=8.0, max_waypoints=24)
            for wp in movement_waypoints:
                step_order = len(steps) + 1
                is_hover = wp["is_hover"] or (wp["dwell_s"] >= 0.18)
                action_type = ActionType.MOVE_MOUSE
                action_desc = f"Hover cursor at ({int(wp['x'])}, {int(wp['y'])})" if is_hover else f"Move cursor to ({int(wp['x'])}, {int(wp['y'])})"

                target_dict: Dict[str, Any] = {
                    "screen_x": int(wp["x"]),
                    "screen_y": int(wp["y"]),
                    "x": int(wp["x"]),
                    "y": int(wp["y"]),
                }
                if wp.get("bundle_id"):
                    target_dict["bundle_id"] = wp["bundle_id"]
                wb = wp.get("window_bounds")
                if wb and wb.width > 0 and wb.height > 0:
                    nx = max(0.0, min(1.0, (wp["x"] - wb.x) / wb.width))
                    ny = max(0.0, min(1.0, (wp["y"] - wb.y) / wb.height))
                    target_dict["norm_x"] = round(nx, 4)
                    target_dict["norm_y"] = round(ny, 4)

                payload_dict: Dict[str, Any] = {
                    "smooth": True,
                    "duration": wp["duration_s"],
                    "x": int(wp["x"]),
                    "y": int(wp["y"]),
                    "screen_x": int(wp["x"]),
                    "screen_y": int(wp["y"]),
                }
                if is_hover and wp["dwell_s"] > 0:
                    payload_dict["hover"] = True
                    payload_dict["dwell_s"] = wp["dwell_s"]

                steps.append(
                    WorkflowStep(
                        step_id=f"step_{step_order}_move",
                        order=step_order,
                        description=action_desc,
                        action=action_type,
                        payload=payload_dict,
                        target=target_dict,
                        coordinates=TargetCoordinates(
                            mode=CoordMode.WINDOW_RELATIVE_RATIO if "norm_x" in target_dict else CoordMode.SCREEN_ABSOLUTE,
                            norm_x=target_dict.get("norm_x"),
                            norm_y=target_dict.get("norm_y"),
                            abs_x=int(wp["x"]),
                            abs_y=int(wp["y"]),
                        ),
                    )
                )

        spec = WorkflowSpec(
            id=session_id or f"wf_{int(time.time())}",
            name=name,
            description=f"Demonstrated workflow with {len(steps)} ground-truth action steps.",
            triggers={"canonical": canonical_trigger or name.lower(), "aliases": [name.lower()]},
            target_app={"bundle_id": current_app or target_bundle_id},
            steps=steps,
        )

        ai_payload = self.compile_minimal_ai_payload(spec)
        return spec, ai_payload

    @staticmethod
    def compile_minimal_ai_payload(spec: WorkflowSpec) -> Dict[str, Any]:
        """Compiles the minimal JSON payload designed to be fed into the AI system.

        Key metric: As little token usage as possible (< 350 tokens).
        Zero images. Pure structured textual action representation.
        """
        compact_steps = []
        for s in spec.steps:
            item: Dict[str, Any] = {
                "order": s.order,
                "action": s.action.value if hasattr(s.action, "value") else str(s.action),
            }
            if s.payload.get("text"):
                item["text"] = s.payload["text"]
            if s.payload.get("keys"):
                item["keys"] = s.payload["keys"]
            if s.payload.get("button") and s.payload["button"] != "left":
                item["button"] = s.payload["button"]
            if s.target.get("element_text"):
                item["target"] = s.target["element_text"]
            elif s.target.get("source_text"):
                item["source"] = s.target["source_text"]
                if s.target.get("destination_text"):
                    item["destination"] = s.target["destination_text"]
            if s.payload.get("direction"):
                item["direction"] = s.payload["direction"]
            if s.action in (ActionType.MOVE_MOUSE, "move_mouse", "move"):
                if s.target.get("screen_x") is not None:
                    item["x"] = s.target["screen_x"]
                    item["y"] = s.target["screen_y"]
                if s.payload.get("hover"):
                    item["hover"] = True
                if s.payload.get("duration"):
                    item["duration"] = s.payload["duration"]
            if s.target.get("app_name"):
                item["app"] = s.target["app_name"]
            elif s.target.get("bundle_id"):
                item["app"] = s.target["bundle_id"].split(".")[-1]
            compact_steps.append(item)

        return {
            "demonstrated_name": spec.name,
            "target_app": spec.target_app.get("bundle_id") if isinstance(spec.target_app, dict) else "",
            "step_count": len(spec.steps),
            "action_sequence": compact_steps,
        }
