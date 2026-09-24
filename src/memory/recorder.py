"""Workflow demonstration recorder pipeline.

Belongs to FEAT-MEM-05 (WorkflowRecorderPipeline).
4-Stage Pipeline:
  Stage 1: Event Capture (ingests raw user actions with high-precision timestamps & context)
  Stage 2: Noise Reduction / Micro-Jitter Filtering (suppresses spatial micro-jitter < 4px and sub-10ms noise while preserving click anchors)
  Stage 3: Event Coalescing (aggregates sequential typing keystrokes into type_text/paste_text, clicks into click/double-click, hotkeys)
  Stage 4: Pause Clamping (clamps idle wait gaps > 2.0s to realistic playback delays)

Zero third-party dependencies (pure standard library).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import datetime
from enum import Enum
import logging
import math
import re
import time
from typing import Any, Dict, List, Optional, Tuple, Union
import uuid

from src.memory.models import (
    ActionType,
    CoordMode,
    TargetCoordinates,
    WorkflowSpec,
    WorkflowStep,
)

logger = logging.getLogger(__name__)


class RawEventType(str, Enum):
    MOUSE_MOVE = "mouse_move"
    MOUSE_DOWN = "mouse_down"
    MOUSE_UP = "mouse_up"
    MOUSE_DRAG = "mouse_drag"
    CLICK = "click"
    KEY_DOWN = "key_down"
    KEY_UP = "key_up"
    KEY_CHAR = "key_char"
    APP_ACTIVATE = "app_activate"
    WINDOW_FOCUS = "window_focus"


@dataclass
class WindowBounds:
    x: float
    y: float
    width: float
    height: float


@dataclass
class RawEvent:
    """Represents a raw captured input or OS event during demonstration."""
    event_type: Union[RawEventType, str]
    timestamp: float
    x: float = 0.0
    y: float = 0.0
    button: str = "left"  # "left", "right", "center"
    key: str = ""  # Character or key identifier ("a", "Return", "BackSpace")
    modifiers: List[str] = field(default_factory=list)  # ["cmd", "shift", "alt", "ctrl"]
    bundle_id: Optional[str] = None
    window_bounds: Optional[WindowBounds] = None
    is_dock_item: bool = False
    dock_item_title: str = ""


# Alias for backward-compatibility with test suites
RawInputEvent = RawEvent


@dataclass
class RecorderConfig:
    """Configuration knobs for the 4-stage recorder pipeline."""
    jitter_distance_px: float = 4.0  # Euclidean pixel threshold for micro-jitter
    jitter_time_delta_s: float = 0.010  # Max temporal window for jitter drops (10ms)
    click_max_duration_s: float = 0.350  # Max down-to-up interval for a click
    double_click_interval_s: float = 0.400  # Max inter-click interval for double-click
    max_pause_seconds: float = 2.0  # Threshold above which pauses are clamped
    clamped_pause_ms: int = 500  # Realistic playback delay for clamped pauses
    default_post_delay_ms: int = 50  # Standard inter-step delay
    paste_threshold_chars: int = 60  # Strings longer than this coalesce into paste_text


# -----------------------------------------------------------------------------
# Stage 1: Event Capture
# -----------------------------------------------------------------------------

class EventCaptureStage:
    """Stage 1: Thread-safe raw input event capture."""

    def __init__(self) -> None:
        self._events: List[RawEvent] = []
        self._is_recording: bool = False
        self._start_time: float = 0.0

    def start(self) -> None:
        self._events.clear()
        self._start_time = time.time()
        self._is_recording = True

    def stop(self) -> List[RawEvent]:
        self._is_recording = False
        return list(self._events)

    @property
    def is_recording(self) -> bool:
        return self._is_recording

    def feed_event(self, event: RawEvent) -> None:
        if self._is_recording:
            self._events.append(event)


# -----------------------------------------------------------------------------
# Stage 2: Noise Reduction / Micro-Jitter Filtering
# -----------------------------------------------------------------------------

class NoiseFilterStage:
    """Stage 2: Filters sub-pixel mouse trembling, duplicate hovers, and key bounces.

    Preserves critical motion anchors (e.g. mouse positions immediately preceding clicks).
    """

    def __init__(self, config: RecorderConfig) -> None:
        self.config = config

    def filter(self, raw_events: List[RawEvent]) -> List[RawEvent]:
        if not raw_events:
            return []

        filtered: List[RawEvent] = []
        n = len(raw_events)

        for i, ev in enumerate(raw_events):
            ev_type = ev.event_type.value if isinstance(ev.event_type, RawEventType) else str(ev.event_type).lower()
            next_ev = raw_events[i + 1] if i + 1 < n else None
            next_type = (
                (next_ev.event_type.value if isinstance(next_ev.event_type, RawEventType) else str(next_ev.event_type).lower())
                if next_ev
                else ""
            )

            # Critical Anchor Rule: Never drop mouse move immediately preceding a down/click/hotkey
            is_anchor = next_ev is not None and next_type in (
                "mouse_down",
                "mousedown",
                "mouse_up",
                "mouseup",
                "click",
                "key_down",
                "keydown",
            )

            if ev_type in ("mouse_move", "mousemove"):
                if is_anchor:
                    filtered.append(ev)
                    continue

                if filtered:
                    last_type = (
                        filtered[-1].event_type.value
                        if isinstance(filtered[-1].event_type, RawEventType)
                        else str(filtered[-1].event_type).lower()
                    )
                    if last_type in ("mouse_move", "mousemove"):
                        last_ev = filtered[-1]
                        dist = math.hypot(ev.x - last_ev.x, ev.y - last_ev.y)
                        dt = ev.timestamp - last_ev.timestamp

                        # Filter micro-jitter (< jitter_dist in short temporal window) or stationary drift (< 1.5px)
                        if (dist < self.config.jitter_distance_px and dt < self.config.jitter_time_delta_s) or (dist < 1.5):
                            continue

            elif ev_type in ("key_down", "keydown"):
                # Key bounce suppression: duplicate key down within 3ms
                if filtered:
                    last_type = (
                        filtered[-1].event_type.value
                        if isinstance(filtered[-1].event_type, RawEventType)
                        else str(filtered[-1].event_type).lower()
                    )
                    if last_type in ("key_down", "keydown"):
                        last_ev = filtered[-1]
                        if last_ev.key == ev.key and (ev.timestamp - last_ev.timestamp < 0.003):
                            continue

            filtered.append(ev)

        return filtered


# -----------------------------------------------------------------------------
# Stage 3: Event Coalescing
# -----------------------------------------------------------------------------

class EventCoalescingStage:
    """Stage 3: Coalesces low-level events into semantic high-level workflow steps.

    - Consecutive typing events -> ActionType.TYPE_TEXT (or PASTE_TEXT for large blocks)
    - Modifier + key combinations -> ActionType.PRESS_HOTKEY
    - Down + Up at same coordinates -> ActionType.CLICK (click_count: 1 or 2)
    - Translates screen coordinates to window-relative ratio coordinates
    """

    def __init__(self, config: RecorderConfig) -> None:
        self.config = config

    def _flush_typing(
        self,
        text_buffer: List[str],
        start_time: float,
        end_time: float,
        steps: List[WorkflowStep],
        step_timestamps: List[Tuple[float, float]],
        target_bundle: Optional[str] = None,
    ) -> None:
        if not text_buffer:
            return
        full_text = "".join(text_buffer)
        text_buffer.clear()

        # Decide whether to use paste_text or type_text based on length
        action = ActionType.PASTE_TEXT if len(full_text) >= self.config.paste_threshold_chars else ActionType.TYPE_TEXT
        desc = (
            f"Paste text ({len(full_text)} chars)"
            if action == ActionType.PASTE_TEXT
            else f"Type text: '{full_text[:30]}...'" if len(full_text) > 30 else f"Type text: '{full_text}'"
        )

        target_dict: Dict[str, Any] = {}
        if target_bundle:
            target_dict["bundle_id"] = target_bundle
            target_dict["app_name"] = target_bundle.split(".")[-1]

        step_idx = len(steps) + 1
        steps.append(
            WorkflowStep(
                step_id=f"step_{step_idx}",
                order=step_idx,
                description=desc,
                action=action,
                payload={"text": full_text},
                target=target_dict,
            )
        )
        step_timestamps.append((start_time, end_time))

    def coalesce(
        self, events: List[RawEvent]
    ) -> Tuple[List[WorkflowStep], List[Tuple[float, float]]]:
        if not events:
            return [], []

        steps: List[WorkflowStep] = []
        step_timestamps: List[Tuple[float, float]] = []

        typing_buffer: List[str] = []
        typing_start_t = 0.0
        typing_last_t = 0.0

        current_active_bundle: Optional[str] = None
        clio_bundles = {"com.apple.loginwindow", "com.apple.dock", "com.clio.desktop", "clio-bar", "clio", "Clio"}

        i = 0
        n = len(events)

        while i < n:
            ev = events[i]
            ev_type = ev.event_type.value if isinstance(ev.event_type, RawEventType) else str(ev.event_type).lower()

            # Automatic app focus tracking across multi-app workflows
            if ev.bundle_id and ev.bundle_id not in clio_bundles:
                if current_active_bundle is not None and ev.bundle_id != current_active_bundle:
                    self._flush_typing(typing_buffer, typing_start_t, typing_last_t, steps, step_timestamps, current_active_bundle)
                    step_idx = len(steps) + 1
                    steps.append(
                        WorkflowStep(
                            step_id=f"step_{step_idx}",
                            order=step_idx,
                            description=f"Focus application {ev.bundle_id}",
                            action=ActionType.FOCUS_APP,
                            target={"bundle_id": ev.bundle_id, "app_name": ev.bundle_id.split(".")[-1]},
                        )
                    )
                    step_timestamps.append((ev.timestamp, ev.timestamp + 0.05))
                current_active_bundle = ev.bundle_id

            # Key down handling (hotkeys, special control keys, typing)
            if ev_type in ("key_down", "keydown", "key_char", "keychar"):
                key_clean = (ev.key or "").strip().lower()

                # Check hotkey combination (Cmd, Alt, Ctrl modifiers or compound key like 'cmd+l')
                combo_parts = [p.strip().lower() for p in key_clean.split("+")] if "+" in key_clean else []
                has_combo = bool(set(combo_parts).intersection({"cmd", "command", "alt", "option", "ctrl", "control", "shift", "fn"}))
                has_cmd_ctrl = has_combo or bool(set(ev.modifiers).intersection({"cmd", "command", "alt", "option", "ctrl", "control"}))
                if has_cmd_ctrl:
                    self._flush_typing(typing_buffer, typing_start_t, typing_last_t, steps, step_timestamps, current_active_bundle)
                    if has_combo:
                        hotkey_keys = combo_parts
                    else:
                        hotkey_keys = [m.lower() for m in ev.modifiers]
                        if ev.key and ev.key.lower() not in hotkey_keys:
                            hotkey_keys.append(ev.key.lower())

                    step_idx = len(steps) + 1
                    target_dict = {}
                    if current_active_bundle:
                        target_dict["bundle_id"] = current_active_bundle
                        target_dict["app_name"] = current_active_bundle.split(".")[-1]
                    steps.append(
                        WorkflowStep(
                            step_id=f"step_{step_idx}",
                            order=step_idx,
                            description=f"Press hotkey: {'+'.join(hotkey_keys)}",
                            action=ActionType.PRESS_HOTKEY,
                            payload={"keys": hotkey_keys},
                            target=target_dict,
                        )
                    )
                    step_timestamps.append((ev.timestamp, ev.timestamp + 0.05))
                    i += 1
                    continue

                # Check special control & navigation keys
                if key_clean in ("return", "enter", "\r", "\n", "k_36"):
                    self._flush_typing(typing_buffer, typing_start_t, typing_last_t, steps, step_timestamps, current_active_bundle)
                    step_idx = len(steps) + 1
                    target_dict = {}
                    if current_active_bundle:
                        target_dict["bundle_id"] = current_active_bundle
                        target_dict["app_name"] = current_active_bundle.split(".")[-1]
                    steps.append(
                        WorkflowStep(
                            step_id=f"step_{step_idx}",
                            order=step_idx,
                            description="Press Return",
                            action=ActionType.PRESS_HOTKEY,
                            payload={"keys": ["return"]},
                            target=target_dict,
                        )
                    )
                    step_timestamps.append((ev.timestamp, ev.timestamp + 0.05))
                    i += 1
                    continue

                if key_clean in ("tab", "\t", "k_48"):
                    self._flush_typing(typing_buffer, typing_start_t, typing_last_t, steps, step_timestamps, current_active_bundle)
                    step_idx = len(steps) + 1
                    target_dict = {}
                    if current_active_bundle:
                        target_dict["bundle_id"] = current_active_bundle
                        target_dict["app_name"] = current_active_bundle.split(".")[-1]
                    steps.append(
                        WorkflowStep(
                            step_id=f"step_{step_idx}",
                            order=step_idx,
                            description="Press Tab",
                            action=ActionType.PRESS_HOTKEY,
                            payload={"keys": ["tab"]},
                            target=target_dict,
                        )
                    )
                    step_timestamps.append((ev.timestamp, ev.timestamp + 0.05))
                    i += 1
                    continue

                if key_clean in ("escape", "esc", "k_53"):
                    self._flush_typing(typing_buffer, typing_start_t, typing_last_t, steps, step_timestamps, current_active_bundle)
                    step_idx = len(steps) + 1
                    target_dict = {}
                    if current_active_bundle:
                        target_dict["bundle_id"] = current_active_bundle
                        target_dict["app_name"] = current_active_bundle.split(".")[-1]
                    steps.append(
                        WorkflowStep(
                            step_id=f"step_{step_idx}",
                            order=step_idx,
                            description="Press Escape",
                            action=ActionType.PRESS_HOTKEY,
                            payload={"keys": ["escape"]},
                            target=target_dict,
                        )
                    )
                    step_timestamps.append((ev.timestamp, ev.timestamp + 0.05))
                    i += 1
                    continue

                if key_clean in ("backspace", "delete", "\b", "\x7f", "k_51"):
                    if typing_buffer:
                        typing_buffer.pop()
                    else:
                        self._flush_typing(typing_buffer, typing_start_t, typing_last_t, steps, step_timestamps, current_active_bundle)
                        step_idx = len(steps) + 1
                        target_dict = {}
                        if current_active_bundle:
                            target_dict["bundle_id"] = current_active_bundle
                            target_dict["app_name"] = current_active_bundle.split(".")[-1]
                        steps.append(
                            WorkflowStep(
                                step_id=f"step_{step_idx}",
                                order=step_idx,
                                description="Press Backspace",
                                action=ActionType.PRESS_HOTKEY,
                                payload={"keys": ["backspace"]},
                                target=target_dict,
                            )
                        )
                        step_timestamps.append((ev.timestamp, ev.timestamp + 0.05))
                    i += 1
                    continue

                if key_clean in ("up", "down", "left", "right", "k_123", "k_124", "k_125", "k_126"):
                    k_name = {"k_123": "left", "k_124": "right", "k_125": "down", "k_126": "up"}.get(key_clean, key_clean)
                    self._flush_typing(typing_buffer, typing_start_t, typing_last_t, steps, step_timestamps, current_active_bundle)
                    step_idx = len(steps) + 1
                    target_dict = {}
                    if current_active_bundle:
                        target_dict["bundle_id"] = current_active_bundle
                        target_dict["app_name"] = current_active_bundle.split(".")[-1]
                    steps.append(
                        WorkflowStep(
                            step_id=f"step_{step_idx}",
                            order=step_idx,
                            description=f"Press {k_name.capitalize()}",
                            action=ActionType.PRESS_HOTKEY,
                            payload={"keys": [k_name]},
                            target=target_dict,
                        )
                    )
                    step_timestamps.append((ev.timestamp, ev.timestamp + 0.05))
                    i += 1
                    continue

                # Printable character typing
                is_printable = (
                    ev_type in ("key_char", "keychar")
                    or len(ev.key) == 1
                    or key_clean == "space"
                )
                if is_printable:
                    char = " " if key_clean == "space" else ev.key
                    if not typing_buffer:
                        typing_start_t = ev.timestamp
                    typing_last_t = ev.timestamp
                    typing_buffer.append(char)
                    i += 1
                    continue

            # Non-typing event encountered: flush any accumulated typing
            self._flush_typing(typing_buffer, typing_start_t, typing_last_t, steps, step_timestamps, current_active_bundle)

            # 3. Direct CLICK or Mouse Down (+ optional Mouse Up)
            if ev_type in ("click", "mouse_down", "mousedown"):
                click_start_t = ev.timestamp
                click_x = ev.x
                click_y = ev.y
                button = ev.button

                # Check if this click targeted an application icon in the macOS Dock
                dock_app = ev.dock_item_title if ev.is_dock_item else None
                if not dock_app:
                    try:
                        from src.executor.executor import AutonomousWorkflowExecutor
                        dock_app = AutonomousWorkflowExecutor._detect_dock_app_at(click_x, click_y)
                    except Exception:
                        pass

                if dock_app:
                    step_idx = len(steps) + 1
                    steps.append(
                        WorkflowStep(
                            step_id=f"step_{step_idx}",
                            order=step_idx,
                            description=f"Launch and focus {dock_app}",
                            action=ActionType.LAUNCH_APP,
                            payload={"app": dock_app},
                            target={"app_name": dock_app, "screen_x": int(click_x), "screen_y": int(click_y)},
                            coordinates=TargetCoordinates(mode=CoordMode.SCREEN_ABSOLUTE, abs_x=int(click_x), abs_y=int(click_y)),
                        )
                    )
                    step_timestamps.append((click_start_t, click_start_t + 0.1))
                    i += 1
                    continue

                if ev_type in ("mouse_down", "mousedown"):
                    up_ev: Optional[RawEvent] = None
                    if i + 1 < n:
                        next_t = (
                            events[i + 1].event_type.value
                            if isinstance(events[i + 1].event_type, RawEventType)
                            else str(events[i + 1].event_type).lower()
                        )
                        if next_t in ("mouse_up", "mouseup"):
                            up_ev = events[i + 1]
                            i += 1  # Consume mouse_up
                    click_end_t = up_ev.timestamp if up_ev else click_start_t + 0.05
                else:
                    click_end_t = click_start_t + 0.05

                target_dict = {"screen_x": int(click_x), "screen_y": int(click_y)}
                if ev.bundle_id and ev.bundle_id not in clio_bundles:
                    target_dict["bundle_id"] = ev.bundle_id
                    target_dict["app_name"] = ev.bundle_id.split(".")[-1]
                elif current_active_bundle:
                    target_dict["bundle_id"] = current_active_bundle
                    target_dict["app_name"] = current_active_bundle.split(".")[-1]

                if ev.window_bounds and ev.window_bounds.width > 0 and ev.window_bounds.height > 0:
                    norm_x = (click_x - ev.window_bounds.x) / ev.window_bounds.width
                    norm_y = (click_y - ev.window_bounds.y) / ev.window_bounds.height
                    target_dict["norm_x"] = round(max(0.0, min(1.0, norm_x)), 4)
                    target_dict["norm_y"] = round(max(0.0, min(1.0, norm_y)), 4)

                # Check if this qualifies as double-click (2) or triple-click (3)
                is_coalesced = False
                if steps and steps[-1].action == ActionType.CLICK:
                    prev_target = steps[-1].target
                    prev_t_end = step_timestamps[-1][1]
                    dist_to_prev = math.hypot(
                        target_dict["screen_x"] - prev_target.get("screen_x", 0),
                        target_dict["screen_y"] - prev_target.get("screen_y", 0),
                    )
                    prev_btn = str(
                        getattr(
                            steps[-1].payload.get("button", "left"),
                            "value",
                            steps[-1].payload.get("button", "left"),
                        )
                    ).lower()
                    cur_btn = (
                        str(getattr(button, "value", button)).lower()
                        if button is not None
                        else "left"
                    )
                    if (
                        dist_to_prev <= 5.0
                        and (click_start_t - prev_t_end <= self.config.double_click_interval_s)
                        and prev_btn == cur_btn
                    ):
                        prev_count = steps[-1].payload.get("click_count", 1)
                        if prev_count == 1:
                            steps[-1].payload["click_count"] = 2
                            steps[-1].description = f"Double click at ({click_x:.0f}, {click_y:.0f})"
                            step_timestamps[-1] = (step_timestamps[-1][0], click_end_t)
                            is_coalesced = True
                        elif prev_count == 2:
                            steps[-1].payload["click_count"] = 3
                            steps[-1].description = f"Triple click at ({click_x:.0f}, {click_y:.0f})"
                            step_timestamps[-1] = (step_timestamps[-1][0], click_end_t)
                            is_coalesced = True

                if not is_coalesced:
                    step_idx = len(steps) + 1
                    steps.append(
                        WorkflowStep(
                            step_id=f"step_{step_idx}",
                            order=step_idx,
                            description=f"Click {button} button at ({click_x:.0f}, {click_y:.0f})",
                            action=ActionType.CLICK,
                            target=target_dict,
                            payload={"button": button, "click_count": 1},
                        )
                    )
                    step_timestamps.append((click_start_t, click_end_t))

                i += 1
                continue

            # 4. App activation or window switch
            if ev_type in ("app_activate", "appactivate", "window_focus", "windowfocus"):
                bundle_id = ev.bundle_id or "com.apple.Notes"
                step_idx = len(steps) + 1
                steps.append(
                    WorkflowStep(
                        step_id=f"step_{step_idx}",
                        order=step_idx,
                        description=f"Focus application {bundle_id}",
                        action=ActionType.FOCUS_APP,
                        target={"bundle_id": bundle_id, "app_name": bundle_id.split(".")[-1]},
                    )
                )
                step_timestamps.append((ev.timestamp, ev.timestamp + 0.1))
                current_active_bundle = bundle_id
                i += 1
                continue

            i += 1

        # Final flush if recording ended on typing
        self._flush_typing(typing_buffer, typing_start_t, typing_last_t, steps, step_timestamps, current_active_bundle)

        return steps, step_timestamps


# -----------------------------------------------------------------------------
# Stage 4: Pause Clamping
# -----------------------------------------------------------------------------

class PauseClampingStage:
    """Stage 4: Clamps demonstration pauses > 2.0s down to realistic playback delays.

    Prevents user hesitation or idle gaps during recording from inflating execution time.
    """

    def __init__(self, config: RecorderConfig) -> None:
        self.config = config

    def clamp(
        self, steps: List[WorkflowStep], step_timestamps: List[Tuple[float, float]]
    ) -> List[WorkflowStep]:
        if not steps:
            return []

        n = len(steps)
        for i in range(n):
            steps[i].order = i + 1
            steps[i].step_id = f"step_{i + 1}"

            if i < n - 1 and i < len(step_timestamps) - 1:
                cur_end_t = step_timestamps[i][1]
                next_start_t = step_timestamps[i + 1][0]
                idle_gap_s = max(0.0, next_start_t - cur_end_t)

                if idle_gap_s > self.config.max_pause_seconds:
                    # User hesitation clamped to realistic delay (e.g. 500ms)
                    post_delay_ms = self.config.clamped_pause_ms
                    steps[i].timing = {
                        "pre_delay_ms": 0,
                        "post_delay_ms": post_delay_ms,
                        "timeout_ms": 5000,
                        "clamped_from_s": round(idle_gap_s, 2),
                    }
                    if i + 1 < n:
                        steps[i + 1].timing["pre_delay_ms"] = min(post_delay_ms, 500)
                elif idle_gap_s < 0.050:
                    post_delay_ms = self.config.default_post_delay_ms
                    steps[i].timing = {
                        "pre_delay_ms": 0,
                        "post_delay_ms": post_delay_ms,
                        "timeout_ms": 5000,
                    }
                else:
                    post_delay_ms = int(idle_gap_s * 1000)
                    steps[i].timing = {
                        "pre_delay_ms": 0,
                        "post_delay_ms": post_delay_ms,
                        "timeout_ms": 5000,
                    }
            else:
                steps[i].timing = {
                    "pre_delay_ms": 0,
                    "post_delay_ms": self.config.default_post_delay_ms,
                    "timeout_ms": 5000,
                }

        return steps


# -----------------------------------------------------------------------------
# End-to-End Orchestrator: WorkflowRecorderPipeline
# -----------------------------------------------------------------------------

class WorkflowRecorderPipeline:
    """4-stage pipeline for recording demonstrated user tasks into WorkflowSpec."""

    def __init__(
        self,
        config: Optional[RecorderConfig] = None,
        jitter_pixel_threshold: Optional[float] = None,
        max_pause_ms: Optional[int] = None,
    ) -> None:
        self.config = config or RecorderConfig()
        if jitter_pixel_threshold is not None:
            self.config.jitter_distance_px = float(jitter_pixel_threshold)
        if max_pause_ms is not None:
            self.config.clamped_pause_ms = int(max_pause_ms)
            self.config.max_pause_seconds = float(max_pause_ms) / 1000.0

        self.capture_stage = EventCaptureStage()
        self.noise_stage = NoiseFilterStage(self.config)
        self.coalescing_stage = EventCoalescingStage(self.config)
        self.clamping_stage = PauseClampingStage(self.config)

    def start_recording(self) -> None:
        """Start capturing demonstration events."""
        self.capture_stage.start()

    def feed_event(self, event: RawEvent) -> None:
        """Feed a raw event directly into the capture stage."""
        self.capture_stage.feed_event(event)

    def _normalize_raw_events(self, raw_events: List[Any]) -> List[RawEvent]:
        """Ensures all events are instances of RawEvent."""
        normalized: List[RawEvent] = []
        for ev in raw_events:
            if isinstance(ev, RawEvent):
                normalized.append(ev)
            elif isinstance(ev, dict):
                normalized.append(RawEvent(**ev))
            else:
                # Duck-typed object
                normalized.append(
                    RawEvent(
                        event_type=getattr(ev, "event_type"),
                        timestamp=getattr(ev, "timestamp", 0.0),
                        x=getattr(ev, "x", 0.0),
                        y=getattr(ev, "y", 0.0),
                        button=getattr(ev, "button", "left"),
                        key=getattr(ev, "key", ""),
                        modifiers=getattr(ev, "modifiers", []),
                        bundle_id=getattr(ev, "bundle_id", None),
                        window_bounds=getattr(ev, "window_bounds", None),
                    )
                )
        return normalized

    def process_raw_events(
        self,
        raw_events: List[Any],
        name: str = "Recorded Workflow",
        canonical_trigger: str = "",
        description: str = "",
        target_bundle_id: str = "",
    ) -> WorkflowSpec:
        """Process an explicit list of raw events through stages 2 -> 3 -> 4 into WorkflowSpec.

        Ideal for headless testing, deterministic simulation, and offline ingestion.
        """
        normalized_events = self._normalize_raw_events(raw_events)

        # Stage 2: Noise reduction & micro-jitter filtering
        filtered_events = self.noise_stage.filter(normalized_events)

        # Stage 3: Event coalescing into high-level steps
        coalesced_steps, step_timestamps = self.coalescing_stage.coalesce(filtered_events)

        # Stage 4: Pause clamping
        final_steps = self.clamping_stage.clamp(coalesced_steps, step_timestamps)

        # Extract primary bundle_id if not explicitly provided
        if not target_bundle_id:
            for ev in normalized_events:
                if ev.bundle_id:
                    target_bundle_id = ev.bundle_id
                    break

        canon = canonical_trigger.strip() or name.strip().lower()
        aliases: List[str] = []
        keywords = [w.lower() for w in re.findall(r"\w+", canon) if len(w) > 3]

        spec_id = f"wf_rec_{uuid.uuid4().hex[:8]}"
        desc = description or f"Demonstrated workflow with {len(final_steps)} recorded steps."

        return WorkflowSpec(
            id=spec_id,
            name=name,
            description=desc,
            triggers={"canonical": canon, "aliases": aliases, "keywords": keywords},
            target_app={"bundle_id": target_bundle_id} if target_bundle_id else {},
            steps=final_steps,
        )

    def stop_recording(
        self,
        name: str = "Recorded Workflow",
        canonical_trigger: str = "",
        description: str = "",
        target_bundle_id: str = "",
    ) -> WorkflowSpec:
        """Stop capturing and process all events through the pipeline."""
        raw_events = self.capture_stage.stop()
        return self.process_raw_events(
            raw_events=raw_events,
            name=name,
            canonical_trigger=canonical_trigger,
            description=description,
            target_bundle_id=target_bundle_id,
        )

    def compile_workflow(
        self,
        workflow_id: str,
        name: str,
        events: List[Any],
        description: str = "",
        target_bundle_id: str = "",
    ) -> WorkflowSpec:
        """Compile a list of events into a WorkflowSpec with specified ID."""
        spec = self.process_raw_events(
            raw_events=events,
            name=name,
            description=description,
            target_bundle_id=target_bundle_id,
        )
        spec.id = workflow_id
        return spec

    def coalesce_mouse_events(self, events: List[Any]) -> List[RawEvent]:
        """Convenience helper to run mouse events through noise reduction filter."""
        normalized = self._normalize_raw_events(events)
        return self.noise_stage.filter(normalized)
