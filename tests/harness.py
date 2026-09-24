"""Autonomous Desktop Companion — Progressive Test Harness & Contract Reference.

Provides deterministic, in-memory reference implementations for:
- BaseActuator, MockActuator, ActuatorFactory, WindowInfo, MouseButton, Exceptions
- WorkflowSpec, WorkflowStep, ActionType, TaskMemoryEngine, NLRetrievalEngine
- ParameterEngine, CoordinateAdapter
- ExecutionEvent, EventType, ExecutionEventBus, AutonomousWorkflowExecutor
- WindowReadinessPoller
- CommentaryEngine, CompanionDialogueEngine, DialogueState
- NotesBenchmarkRecipe, BenchmarkCompletionVerifier
"""

from __future__ import annotations

import datetime
import json
import math
import re
import sqlite3
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set, Tuple


# ============================================================================
# 1. ACTUATOR SUBSYSTEM (M1 / R1)
# ============================================================================

class ActuatorError(Exception):
    """Base exception for actuator errors."""
    pass


class FailsafeEmergencyStop(ActuatorError):
    """Raised when emergency stop is triggered (mouse corner or hotkey)."""
    def __init__(
        self,
        message: str = "Emergency stop triggered: mouse moved to screen corner",
        position: Optional[Tuple[float, float]] = None,
    ):
        super().__init__(message)
        self.position = position


class ApplicationLaunchError(ActuatorError):
    """Raised when an application fails to launch or focus."""
    pass


class WindowNotFoundError(ActuatorError):
    """Raised when target window is not visible on screen."""
    pass


class MouseButton(str, Enum):
    LEFT = "left"
    RIGHT = "right"
    CENTER = "center"


@dataclass(frozen=True)
class WindowInfo:
    window_id: int
    owner_name: str
    title: str
    x: float
    y: float
    width: float
    height: float
    layer: int = 0


@dataclass
class ActuatedAction:
    action_type: str
    parameters: Dict[str, Any]
    timestamp: float = field(default_factory=time.time)
    success: bool = True


class BaseActuator(ABC):
    """Abstract interface for desktop actuators."""

    @abstractmethod
    def launch_app(self, bundle_id: str, timeout: float = 5.0) -> bool:
        pass

    @abstractmethod
    def focus_app(self, bundle_id: str) -> bool:
        pass

    @abstractmethod
    def get_frontmost_app(self) -> str:
        pass

    @abstractmethod
    def get_windows(self, app_name: Optional[str] = None) -> List[WindowInfo]:
        pass

    @abstractmethod
    def get_screen_size(self) -> Tuple[float, float]:
        pass

    @abstractmethod
    def get_mouse_position(self) -> Tuple[float, float]:
        pass

    @abstractmethod
    def move_mouse(self, x: float, y: float, smooth: bool = False, duration: float = 0.0) -> None:
        pass

    @abstractmethod
    def click(
        self,
        x: Optional[float] = None,
        y: Optional[float] = None,
        button: MouseButton = MouseButton.LEFT,
        click_count: int = 1,
    ) -> None:
        pass

    @abstractmethod
    def drag(
        self, start_x: float, start_y: float, end_x: float, end_y: float, duration: float = 0.3
    ) -> None:
        pass

    @abstractmethod
    def scroll(self, dx: int = 0, dy: int = 0) -> None:
        pass

    @abstractmethod
    def type_text(self, text: str, interval: float = 0.02) -> None:
        pass

    @abstractmethod
    def paste_text(self, text: str) -> None:
        pass

    @abstractmethod
    def press_hotkey(self, *keys: str) -> None:
        pass

    @abstractmethod
    def is_failsafe_triggered(self) -> bool:
        pass

    @abstractmethod
    def check_failsafe(self) -> None:
        pass

    @abstractmethod
    def stop(self) -> None:
        pass


class MockActuator(BaseActuator):
    """Deterministic in-memory actuator tracking state, history log, and assertions."""

    def __init__(self, screen_size: Tuple[float, float] = (1920.0, 1080.0)):
        self._screen_size = screen_size
        self._cursor_pos: Tuple[float, float] = (500.0, 500.0)
        self._frontmost_app: str = "com.apple.finder"
        self._windows: List[WindowInfo] = []
        self._clipboard: str = ""
        self._active_modifiers: Set[str] = set()
        self._failsafe_triggered: bool = False
        self._history: List[ActuatedAction] = []
        self._typed_buffer: List[str] = []
        self._app_launch_should_fail: Set[str] = set()
        self._corner_threshold: float = 5.0

    @property
    def history(self) -> List[ActuatedAction]:
        return list(self._history)

    @property
    def clipboard(self) -> str:
        return self._clipboard

    @property
    def typed_text(self) -> str:
        return "".join(self._typed_buffer)

    def set_app_launch_failure(self, bundle_id: str, should_fail: bool = True) -> None:
        if should_fail:
            self._app_launch_should_fail.add(bundle_id)
        else:
            self._app_launch_should_fail.discard(bundle_id)

    def add_virtual_window(self, window: WindowInfo) -> None:
        self._windows.append(window)

    def clear_virtual_windows(self) -> None:
        self._windows.clear()

    def launch_app(self, bundle_id: str, timeout: float = 5.0) -> bool:
        self.check_failsafe()
        if bundle_id in self._app_launch_should_fail:
            self._history.append(ActuatedAction("launch_app", {"bundle_id": bundle_id, "timeout": timeout}, success=False))
            raise ApplicationLaunchError(f"Failed to launch application with bundle id '{bundle_id}'")
        self._frontmost_app = bundle_id
        # Automatically register a virtual window if none exists for this app
        if not any(w.owner_name == bundle_id or bundle_id in w.owner_name for w in self._windows):
            self._windows.append(
                WindowInfo(
                    window_id=len(self._windows) + 100,
                    owner_name=bundle_id,
                    title=bundle_id.split(".")[-1],
                    x=200.0,
                    y=150.0,
                    width=960.0,
                    height=720.0,
                )
            )
        self._history.append(ActuatedAction("launch_app", {"bundle_id": bundle_id, "timeout": timeout}, success=True))
        return True

    def focus_app(self, bundle_id: str) -> bool:
        self.check_failsafe()
        self._frontmost_app = bundle_id
        self._history.append(ActuatedAction("focus_app", {"bundle_id": bundle_id}, success=True))
        return True

    def get_frontmost_app(self) -> str:
        return self._frontmost_app

    def get_windows(self, app_name: Optional[str] = None) -> List[WindowInfo]:
        if not app_name:
            return list(self._windows)
        return [w for w in self._windows if app_name.lower() in w.owner_name.lower() or app_name.lower() in w.title.lower()]

    def get_screen_size(self) -> Tuple[float, float]:
        return self._screen_size

    def get_mouse_position(self) -> Tuple[float, float]:
        return self._cursor_pos

    def move_mouse(self, x: float, y: float, smooth: bool = False, duration: float = 0.0) -> None:
        self.check_failsafe()
        # Clamp coordinates to screen boundaries defensively
        clamped_x = max(0.0, min(float(x), self._screen_size[0]))
        clamped_y = max(0.0, min(float(y), self._screen_size[1]))
        self._cursor_pos = (clamped_x, clamped_y)
        self._history.append(
            ActuatedAction("move_mouse", {"x": clamped_x, "y": clamped_y, "raw_x": x, "raw_y": y, "smooth": smooth})
        )
        self.check_failsafe()

    def click(
        self,
        x: Optional[float] = None,
        y: Optional[float] = None,
        button: MouseButton = MouseButton.LEFT,
        click_count: int = 1,
    ) -> None:
        self.check_failsafe()
        if x is not None and y is not None:
            self.move_mouse(x, y)
        self._history.append(
            ActuatedAction("click", {
                "x": self._cursor_pos[0],
                "y": self._cursor_pos[1],
                "button": button.value if isinstance(button, MouseButton) else button,
                "click_count": click_count,
            })
        )

    def drag(
        self, start_x: float, start_y: float, end_x: float, end_y: float, duration: float = 0.3
    ) -> None:
        self.check_failsafe()
        self.move_mouse(start_x, start_y)
        self.move_mouse(end_x, end_y)
        self._history.append(
            ActuatedAction("drag", {
                "start": (start_x, start_y),
                "end": (end_x, end_y),
                "duration": duration,
            })
        )

    def scroll(self, dx: int = 0, dy: int = 0) -> None:
        self.check_failsafe()
        self._history.append(ActuatedAction("scroll", {"dx": dx, "dy": dy}))

    def type_text(self, text: str, interval: float = 0.02) -> None:
        self.check_failsafe()
        if not text:
            self._history.append(ActuatedAction("type_text", {"text": "", "interval": interval}))
            return
        self._typed_buffer.append(text)
        self._history.append(ActuatedAction("type_text", {"text": text, "interval": interval}))

    def paste_text(self, text: str) -> None:
        self.check_failsafe()
        self._clipboard = text
        self._typed_buffer.append(text)
        self._history.append(ActuatedAction("paste_text", {"text": text}))
        self.press_hotkey("cmd", "v")

    def press_hotkey(self, *keys: str) -> None:
        self.check_failsafe()
        lowered = [k.lower() for k in keys]
        for k in lowered:
            if k in ("cmd", "command", "shift", "alt", "option", "ctrl", "control"):
                self._active_modifiers.add(k)
        self._history.append(ActuatedAction("press_hotkey", {"keys": list(keys)}))
        # Release synthetic modifiers
        self._active_modifiers.clear()

    def trigger_failsafe(self) -> None:
        self._failsafe_triggered = True

    def is_failsafe_triggered(self) -> bool:
        if self._failsafe_triggered:
            return True
        x, y = self._cursor_pos
        # Corner threshold check (top-left, top-right, bottom-left, bottom-right)
        w, h = self._screen_size
        in_top_left = (x <= self._corner_threshold and y <= self._corner_threshold)
        in_top_right = (x >= w - self._corner_threshold and y <= self._corner_threshold)
        in_bottom_left = (x <= self._corner_threshold and y >= h - self._corner_threshold)
        in_bottom_right = (x >= w - self._corner_threshold and y >= h - self._corner_threshold)
        return in_top_left or in_top_right or in_bottom_left or in_bottom_right

    def check_failsafe(self) -> None:
        if self.is_failsafe_triggered():
            self._active_modifiers.clear()
            raise FailsafeEmergencyStop(
                "Emergency stop triggered: cursor in corner or failsafe activated",
                position=self._cursor_pos,
            )

    def stop(self) -> None:
        self._active_modifiers.clear()
        self._history.append(ActuatedAction("stop", {}))

    # --- Assertion Helpers ---
    def assert_action_called(self, action_type: str, **params) -> None:
        matches = [a for a in self._history if a.action_type == action_type]
        assert matches, f"Action '{action_type}' was never called. Total history: {len(self._history)}"
        if params:
            found = False
            for match in matches:
                if all(match.parameters.get(k) == v for k, v in params.items()):
                    found = True
                    break
            assert found, f"Action '{action_type}' was called but none matched params {params}. Calls: {matches}"

    def assert_app_launched(self, bundle_id: str) -> None:
        self.assert_action_called("launch_app", bundle_id=bundle_id)

    def assert_hotkey_pressed(self, *keys: str) -> None:
        expected = list(keys)
        matches = [a for a in self._history if a.action_type == "press_hotkey"]
        found = any(a.parameters.get("keys") == expected for a in matches)
        assert found, f"Hotkey {expected} was never pressed. Found hotkeys: {[a.parameters for a in matches]}"

    def assert_text_typed(self, substring: str) -> None:
        full_text = self.typed_text
        assert substring in full_text, f"Expected substring '{substring}' not found in typed buffer: '{full_text}'"

    def assert_modifiers_released(self) -> None:
        assert len(self._active_modifiers) == 0, f"Sticky modifiers detected: {self._active_modifiers}"


class ActuatorFactory:
    """Factory creating actuators based on mode or environment."""

    @staticmethod
    def create(mode: str = "mock", **kwargs) -> BaseActuator:
        if mode == "mock":
            return MockActuator(**kwargs)
        elif mode == "live":
            try:
                from src.actuators.macos import MacOSActuator  # type: ignore
                return MacOSActuator(**kwargs)
            except ImportError:
                raise ActuatorError("MacOSActuator not available; fallback to mock.")
        else:
            raise ValueError(f"Unknown actuator mode: '{mode}'")


# ============================================================================
# 2. WORKFLOW SCHEMA & MEMORY SUBSYSTEM (M2 / R3)
# ============================================================================

class ActionType(str, Enum):
    LAUNCH_APP = "launch_app"
    FOCUS_APP = "focus_app"
    MOVE_MOUSE = "move_mouse"
    CLICK = "click"
    TYPE_TEXT = "type_text"
    PASTE_TEXT = "paste_text"
    PRESS_HOTKEY = "press_hotkey"
    WAIT_WINDOW = "wait_window"


@dataclass
class WorkflowStep:
    step_id: str
    order: int
    description: str
    action: ActionType
    target: Dict[str, Any] = field(default_factory=dict)
    payload: Dict[str, Any] = field(default_factory=dict)
    timing: Dict[str, Any] = field(default_factory=lambda: {"pre_delay_ms": 0, "post_delay_ms": 50, "timeout_ms": 5000})
    verification: Dict[str, Any] = field(default_factory=dict)
    error_handling: Dict[str, Any] = field(default_factory=lambda: {"on_failure": "retry", "max_retries": 1})

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["action"] = self.action.value if isinstance(self.action, ActionType) else str(self.action)
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> WorkflowStep:
        d = dict(data)
        d["action"] = ActionType(d["action"])
        return cls(**d)


@dataclass
class WorkflowSpec:
    id: str
    name: str
    description: str
    schema_version: str = "1.0.0"
    version: int = 1
    author: str = "user"
    created_at: str = field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat())
    tags: List[str] = field(default_factory=list)
    triggers: Dict[str, Any] = field(default_factory=lambda: {"canonical": "", "aliases": [], "keywords": []})
    target_app: Dict[str, Any] = field(default_factory=dict)
    environment: Dict[str, Any] = field(default_factory=dict)
    parameters: Dict[str, Any] = field(default_factory=dict)
    pre_conditions: List[Dict[str, Any]] = field(default_factory=list)
    steps: List[WorkflowStep] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["steps"] = [s.to_dict() for s in self.steps]
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> WorkflowSpec:
        d = dict(data)
        steps_raw = d.pop("steps", [])
        steps = [WorkflowStep.from_dict(s) for s in steps_raw]
        return cls(steps=steps, **d)


class TaskMemoryEngine:
    """Embedded SQLite workflow store with WAL mode, versioning, and FTS5 search."""

    def __init__(self, db_path: str = ":memory:"):
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        cur = self._conn.cursor()
        cur.executescript("""
            CREATE TABLE IF NOT EXISTS workflows (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT,
                version INTEGER NOT NULL DEFAULT 1,
                active INTEGER NOT NULL DEFAULT 1,
                spec_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS workflow_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workflow_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                change_summary TEXT,
                spec_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(workflow_id) REFERENCES workflows(id)
            );

            CREATE TABLE IF NOT EXISTS executions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workflow_id TEXT NOT NULL,
                status TEXT NOT NULL,
                duration_ms INTEGER NOT NULL,
                steps_completed INTEGER NOT NULL,
                error_message TEXT,
                timestamp TEXT NOT NULL
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS workflow_fts USING fts5(
                workflow_id,
                name,
                description,
                triggers,
                content='workflows',
                content_rowid='rowid'
            );
        """)
        self._conn.commit()

    def save_workflow(self, spec: WorkflowSpec, change_summary: str = "Initial recording") -> str:
        cur = self._conn.cursor()
        cur.execute("SELECT version FROM workflows WHERE id = ?", (spec.id,))
        row = cur.fetchone()

        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        if row:
            new_version = row["version"] + 1
            spec.version = new_version
            spec.updated_at = now
            spec_json = json.dumps(spec.to_dict())
            cur.execute("""
                UPDATE workflows
                SET name = ?, description = ?, version = ?, spec_json = ?, updated_at = ?
                WHERE id = ?
            """, (spec.name, spec.description, new_version, spec_json, now, spec.id))
        else:
            new_version = spec.version
            spec.updated_at = now
            spec_json = json.dumps(spec.to_dict())
            cur.execute("""
                INSERT INTO workflows (id, name, description, version, active, spec_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, 1, ?, ?, ?)
            """, (spec.id, spec.name, spec.description, new_version, spec_json, spec.created_at, now))

        # Record version history
        cur.execute("""
            INSERT INTO workflow_versions (workflow_id, version, change_summary, spec_json, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (spec.id, new_version, change_summary, spec_json, now))

        self._conn.commit()
        return spec.id

    def get_workflow(self, workflow_id: str, version: Optional[int] = None) -> Optional[WorkflowSpec]:
        cur = self._conn.cursor()
        if version is not None:
            cur.execute("SELECT spec_json FROM workflow_versions WHERE workflow_id = ? AND version = ?", (workflow_id, version))
        else:
            cur.execute("SELECT spec_json FROM workflows WHERE id = ? AND active = 1", (workflow_id,))
        row = cur.fetchone()
        if not row:
            return None
        return WorkflowSpec.from_dict(json.loads(row["spec_json"]))

    def list_workflows(self, active_only: bool = True) -> List[Dict[str, Any]]:
        cur = self._conn.cursor()
        query = "SELECT id, name, description, version, updated_at FROM workflows"
        if active_only:
            query += " WHERE active = 1"
        query += " ORDER BY updated_at DESC"
        cur.execute(query)
        return [dict(row) for row in cur.fetchall()]

    def record_execution(
        self, workflow_id: str, status: str, duration_ms: int, steps_completed: int, error_message: Optional[str] = None
    ) -> int:
        cur = self._conn.cursor()
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        cur.execute("""
            INSERT INTO executions (workflow_id, status, duration_ms, steps_completed, error_message, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (workflow_id, status, duration_ms, steps_completed, error_message, now))
        self._conn.commit()
        return cur.lastrowid

    def close(self) -> None:
        self._conn.close()


@dataclass
class MatchResult:
    workflow_id: str
    workflow_name: str
    confidence: float
    matched_trigger: str
    spec: WorkflowSpec


class NLRetrievalEngine:
    """Hybrid Natural Language Retrieval Engine (Canonical -> Aliases -> Keywords -> Levenshtein)."""

    def __init__(self, memory_engine: TaskMemoryEngine):
        self.memory = memory_engine

    def query(self, utterance: str) -> List[MatchResult]:
        cleaned = utterance.strip().lower()
        if not cleaned:
            return []

        workflows = self.memory.list_workflows()
        results: List[MatchResult] = []

        for wf_meta in workflows:
            spec = self.memory.get_workflow(wf_meta["id"])
            if not spec:
                continue

            canonical = spec.triggers.get("canonical", "").strip().lower()
            aliases = [a.strip().lower() for a in spec.triggers.get("aliases", [])]
            keywords = [k.strip().lower() for k in spec.triggers.get("keywords", [])]

            # 1. Exact canonical match
            if cleaned == canonical:
                results.append(MatchResult(spec.id, spec.name, 1.0, canonical, spec))
                continue

            # 2. Exact alias match
            if cleaned in aliases:
                results.append(MatchResult(spec.id, spec.name, 0.95, cleaned, spec))
                continue

            # 3. Substring match
            if canonical and (canonical in cleaned or cleaned in canonical):
                results.append(MatchResult(spec.id, spec.name, 0.88, canonical, spec))
                continue

            # 4. Token overlap (Jaccard similarity)
            clean_tokens = set(re.findall(r"\w+", cleaned))
            canon_tokens = set(re.findall(r"\w+", canonical))
            if clean_tokens and canon_tokens:
                intersection = clean_tokens.intersection(canon_tokens)
                union = clean_tokens.union(canon_tokens)
                jaccard = len(intersection) / len(union)
                if jaccard > 0.4:
                    score = min(0.85, 0.5 + (jaccard * 0.4))
                    results.append(MatchResult(spec.id, spec.name, score, canonical, spec))
                    continue

            # 5. Keyword scoring
            matching_kw = [k for k in keywords if k in cleaned]
            if matching_kw:
                kw_score = min(0.75, 0.3 + (len(matching_kw) * 0.15))
                results.append(MatchResult(spec.id, spec.name, kw_score, f"keywords:{','.join(matching_kw)}", spec))

        results.sort(key=lambda r: r.confidence, reverse=True)
        return results


class ParameterEngine:
    """Variable token interpolation engine (${var}, ${CURRENT_DATE})."""

    @staticmethod
    def interpolate(template: str, runtime_params: Optional[Dict[str, Any]] = None) -> str:
        if runtime_params is None:
            runtime_params = {}

        result = template
        # Built-in dynamic tokens
        today_str = datetime.date.today().strftime("%A, %B %d, %Y")
        result = result.replace("${CURRENT_DATE}", today_str)

        for key, value in runtime_params.items():
            token = f"${{{key}}}"
            if isinstance(value, list):
                val_str = "\n".join(f"- {item}" for item in value)
            else:
                val_str = str(value)
            result = result.replace(token, val_str)

        return result


class CoordinateAdapter:
    """Resolution-independent window-relative ratio coordinate projection."""

    @staticmethod
    def to_screen_coordinates(norm_x: float, norm_y: float, window: WindowInfo) -> Tuple[int, int]:
        clamped_x = max(0.0, min(1.0, float(norm_x)))
        clamped_y = max(0.0, min(1.0, float(norm_y)))
        screen_x = int(math.floor(window.x + (clamped_x * window.width)))
        screen_y = int(math.floor(window.y + (clamped_y * window.height)))
        return screen_x, screen_y


# ============================================================================
# 3. EXECUTOR & EVENT BUS SUBSYSTEM (M3 / R4)
# ============================================================================

class EventType(str, Enum):
    TASK_STARTED = "task_started"
    APP_LAUNCH_INIT = "app_launch_init"
    WINDOW_WAITING = "window_waiting"
    WINDOW_READY = "window_ready"
    ACTION_STARTING = "action_starting"
    ACTION_COMPLETED = "action_completed"
    TEXT_TYPING_PROGRESS = "text_typing_progress"
    TASK_STALLED = "task_stalled"
    RECOVERY_ATTEMPT = "recovery_attempt"
    EMERGENCY_STOP = "emergency_stop"
    TASK_FAILED = "task_failed"
    TASK_COMPLETED = "task_completed"


@dataclass(frozen=True)
class ExecutionEvent:
    event_type: EventType
    task_id: str
    timestamp: float = field(default_factory=time.time)
    message: str = ""
    target_app: Optional[str] = None
    step_index: int = 0
    total_steps: int = 0
    payload: Optional[Dict[str, Any]] = None


class ExecutionEventBus:
    """Decoupled pub/sub event bus with error isolation."""

    def __init__(self):
        self._subscribers: List[Callable[[ExecutionEvent], None]] = []

    def subscribe(self, callback: Callable[[ExecutionEvent], None]) -> None:
        if callback not in self._subscribers:
            self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[ExecutionEvent], None]) -> None:
        if callback in self._subscribers:
            self._subscribers.remove(callback)

    def publish(self, event: ExecutionEvent) -> None:
        for sub in list(self._subscribers):
            try:
                sub(event)
            except Exception:
                # Isolate subscriber failures
                pass

    def clear(self) -> None:
        self._subscribers.clear()


class WindowReadinessPoller:
    """Zero-brittle-sleep dynamic state poller waiting for target window."""

    @staticmethod
    def wait_for_window(
        actuator: BaseActuator, app_name: str, timeout: float = 5.0, poll_interval: float = 0.05
    ) -> WindowInfo:
        deadline = time.time() + timeout
        while time.time() < deadline:
            actuator.check_failsafe()
            windows = actuator.get_windows(app_name)
            for w in windows:
                if w.width > 50 and w.height > 50:
                    return w
            time.sleep(poll_interval)
        raise WindowNotFoundError(f"Timed out after {timeout}s waiting for window of app '{app_name}'")


@dataclass
class ExecutionResult:
    success: bool
    task_id: str
    steps_completed: int
    total_steps: int
    error: Optional[str] = None
    duration_s: float = 0.0


class AutonomousWorkflowExecutor:
    """Closed-loop step executor with retry handling, delays, and failsafe interception."""

    def __init__(self, actuator: BaseActuator, bus: Optional[ExecutionEventBus] = None):
        self.actuator = actuator
        self.bus = bus or ExecutionEventBus()

    def execute_workflow(
        self, spec: WorkflowSpec, runtime_params: Optional[Dict[str, Any]] = None
    ) -> ExecutionResult:
        start_time = time.time()
        task_id = spec.id
        total_steps = len(spec.steps)

        self.bus.publish(
            ExecutionEvent(
                event_type=EventType.TASK_STARTED,
                task_id=task_id,
                message=f"Starting workflow: {spec.name}",
                total_steps=total_steps,
            )
        )

        steps_completed = 0
        try:
            for idx, step in enumerate(spec.steps, 1):
                self.actuator.check_failsafe()
                self.bus.publish(
                    ExecutionEvent(
                        event_type=EventType.ACTION_STARTING,
                        task_id=task_id,
                        message=step.description,
                        step_index=idx,
                        total_steps=total_steps,
                        payload={"action": step.action.value},
                    )
                )

                self._execute_step(step, runtime_params)
                steps_completed += 1

                self.bus.publish(
                    ExecutionEvent(
                        event_type=EventType.ACTION_COMPLETED,
                        task_id=task_id,
                        message=f"Step {idx} completed",
                        step_index=idx,
                        total_steps=total_steps,
                    )
                )

            duration = time.time() - start_time
            self.bus.publish(
                ExecutionEvent(
                    event_type=EventType.TASK_COMPLETED,
                    task_id=task_id,
                    message=f"Workflow {spec.name} completed successfully!",
                    step_index=steps_completed,
                    total_steps=total_steps,
                )
            )
            return ExecutionResult(True, task_id, steps_completed, total_steps, duration_s=duration)

        except FailsafeEmergencyStop as fes:
            duration = time.time() - start_time
            self.bus.publish(
                ExecutionEvent(
                    event_type=EventType.EMERGENCY_STOP,
                    task_id=task_id,
                    message=f"Emergency stop halted execution: {fes}",
                    step_index=steps_completed,
                    total_steps=total_steps,
                )
            )
            return ExecutionResult(False, task_id, steps_completed, total_steps, error=str(fes), duration_s=duration)

        except Exception as exc:
            duration = time.time() - start_time
            self.bus.publish(
                ExecutionEvent(
                    event_type=EventType.TASK_FAILED,
                    task_id=task_id,
                    message=f"Workflow failed at step {steps_completed + 1}: {exc}",
                    step_index=steps_completed,
                    total_steps=total_steps,
                )
            )
            return ExecutionResult(False, task_id, steps_completed, total_steps, error=str(exc), duration_s=duration)

    def _execute_step(self, step: WorkflowStep, runtime_params: Optional[Dict[str, Any]]) -> None:
        action = step.action
        if action == ActionType.LAUNCH_APP:
            bundle_id = step.target.get("bundle_id", "")
            self.actuator.launch_app(bundle_id)
        elif action == ActionType.FOCUS_APP:
            bundle_id = step.target.get("bundle_id", "")
            self.actuator.focus_app(bundle_id)
        elif action == ActionType.PRESS_HOTKEY:
            keys = step.payload.get("keys", [])
            self.actuator.press_hotkey(*keys)
        elif action == ActionType.TYPE_TEXT:
            raw_text = step.payload.get("text", "")
            interpolated = ParameterEngine.interpolate(raw_text, runtime_params)
            self.actuator.type_text(interpolated)
        elif action == ActionType.PASTE_TEXT:
            raw_text = step.payload.get("text", "")
            interpolated = ParameterEngine.interpolate(raw_text, runtime_params)
            self.actuator.paste_text(interpolated)
        elif action == ActionType.CLICK:
            coords = step.payload.get("coordinates", {})
            self.actuator.click(x=coords.get("x"), y=coords.get("y"))
        elif action == ActionType.MOVE_MOUSE:
            coords = step.payload.get("coordinates", {})
            self.actuator.move_mouse(coords.get("x", 0.0), coords.get("y", 0.0))


# ============================================================================
# 4. COMPANION DIALOGUE & COMMENTARY (M4 / R2)
# ============================================================================

class DialogueState(str, Enum):
    IDLE = "idle"
    INTENT_PARSING = "intent_parsing"
    CLARIFYING = "clarifying"
    CONFIRMING = "confirming"
    EXECUTING = "executing"
    COMPLETED = "completed"


class CommentaryEngine:
    """Real-time narrative commentary synthesis with 800ms debounce/throttle."""

    TEMPLATES = {
        EventType.TASK_STARTED: {
            "vibrant": "Alright! Let's get your '{task_name}' done hands-free! Hands off—I've got this! ✨",
            "concise": "Starting task: {task_name}.",
            "zen": "Beginning '{task_name}'. Sit back and relax.",
            "developer": "[TASK_START] id={task_id} steps={total_steps}",
        },
        EventType.APP_LAUNCH_INIT: {
            "vibrant": "Opening {target_app} now...",
            "concise": "Launching {target_app}.",
            "zen": "Opening {target_app} quietly.",
            "developer": "[LAUNCH] target={target_app}",
        },
        EventType.ACTION_STARTING: {
            "vibrant": "Working on: {message} 🚀",
            "concise": "Executing: {message}",
            "zen": "Processing step...",
            "developer": "[ACTION] step={step_index}/{total_steps} msg={message}",
        },
        EventType.EMERGENCY_STOP: {
            "vibrant": "Whoa! Emergency stop triggered! Halting everything right now. 🛑",
            "concise": "Emergency stop triggered. Aborted.",
            "zen": "Halting smoothly upon request.",
            "developer": "[ABORT] emergency_stop triggered.",
        },
        EventType.TASK_COMPLETED: {
            "vibrant": "Ta-da! 🎉 Your task is completely finished. Have an awesome day!",
            "concise": "Task completed successfully.",
            "zen": "All finished. Everything is in order.",
            "developer": "[TASK_SUCCESS] status=0",
        },
    }

    def __init__(
        self,
        bus: Optional[ExecutionEventBus] = None,
        tone: str = "vibrant",
        throttle_ms: float = 800.0,
    ):
        self.bus = bus
        self.tone = tone if tone in ("vibrant", "concise", "zen", "developer") else "vibrant"
        self.throttle_s = throttle_ms / 1000.0
        self._last_commentary_time = 0.0
        self.commentary_log: List[str] = []

        if self.bus:
            self.bus.subscribe(self.handle_event)

    def set_tone(self, tone: str) -> None:
        if tone in ("vibrant", "concise", "zen", "developer"):
            self.tone = tone

    def handle_event(self, event: ExecutionEvent) -> Optional[str]:
        now = time.time()
        # High priority events always bypass throttle
        high_priority = event.event_type in (
            EventType.TASK_STARTED,
            EventType.EMERGENCY_STOP,
            EventType.TASK_COMPLETED,
            EventType.TASK_FAILED,
        )

        if not high_priority and (now - self._last_commentary_time < self.throttle_s):
            return None

        tone_pool = self.TEMPLATES.get(event.event_type, {})
        template = tone_pool.get(self.tone, "{message}")
        formatted = template.format(
            task_name=event.task_id,
            task_id=event.task_id,
            total_steps=event.total_steps,
            step_index=event.step_index,
            target_app=event.target_app or "application",
            message=event.message,
        )
        self._last_commentary_time = now
        self.commentary_log.append(formatted)
        return formatted


class CompanionDialogueEngine:
    """Conversational persona managing states, greetings, memory retrieval, and intent."""

    def __init__(
        self,
        memory: Optional[TaskMemoryEngine] = None,
        tone: str = "vibrant",
    ):
        self.memory = memory or TaskMemoryEngine()
        self.retrieval = NLRetrievalEngine(self.memory)
        self.state = DialogueState.IDLE
        self.tone = tone
        self.pending_workflow: Optional[WorkflowSpec] = None

    def handle_user_message(self, message: str) -> Tuple[str, DialogueState]:
        text = message.strip()
        if not text:
            return "Hey there! I didn't catch that. How can I help you today? ✨", self.state

        # Check for conversational prefix like "Hey Clio, " or "Please "
        cleaned_intent = re.sub(r"^(hey|hello|hi)(\s+clio)?[,!]?\s*", "", text, flags=re.IGNORECASE).strip()
        cleaned_intent = re.sub(r"^please\s+", "", cleaned_intent, flags=re.IGNORECASE).strip()

        # Pure Greetings (no subsequent action intent)
        if not cleaned_intent and any(w in text.lower() for w in ["hello", "hi", "hey", "who are you"]):
            self.state = DialogueState.IDLE
            return (
                "Hey! I'm your Autonomous Desktop Companion! 🌟 I can observe tasks, remember workflows, "
                "or operate your desktop hands-free. Try saying 'write my weekly to-do list'!",
                self.state,
            )

        # Task listing query
        if "what tasks do you remember" in text.lower() or "list tasks" in text.lower():
            workflows = self.memory.list_workflows()
            if not workflows:
                return "I don't have any learned workflows yet! You can teach me one anytime.", self.state
            items = ", ".join(f"'{w['name']}'" for w in workflows)
            return f"Here are the workflows I remember: {items}.", self.state

        # Confirmation handling
        if self.state == DialogueState.CONFIRMING:
            if text.lower() in ("yes", "y", "sure", "proceed"):
                self.state = DialogueState.EXECUTING
                wf = self.pending_workflow
                self.pending_workflow = None
                return f"Got it! Executing '{wf.name if wf else 'task'}' now.", self.state
            else:
                self.state = DialogueState.IDLE
                self.pending_workflow = None
                return "Cancelled! What would you like to do instead?", self.state

        # Intent search on both original text and cleaned intent
        matches = self.retrieval.query(cleaned_intent if cleaned_intent else text)
        if not matches and cleaned_intent != text:
            matches = self.retrieval.query(text)

        if not matches:
            if any(w in text.lower() for w in ["hello", "hi", "hey"]):
                self.state = DialogueState.IDLE
                return "Hey! What workflow would you like to run today? 🌟", self.state
            return f"I'm not sure how to '{text}' yet. Would you like to teach me this workflow?", self.state

        top_match = matches[0]

        # Ambiguity check: if 2+ matches and the second match has very close confidence
        if len(matches) > 1 and (matches[0].confidence - matches[1].confidence < 0.15):
            self.state = DialogueState.CLARIFYING
            candidates = [m.workflow_name for m in matches[:3]]
            return f"Did you mean one of these: {', '.join(candidates)}?", self.state

        if top_match.confidence >= 0.85:
            self.state = DialogueState.EXECUTING
            return f"Starting '{top_match.workflow_name}' right away! 🚀", self.state
        elif top_match.confidence >= 0.5:
            self.state = DialogueState.CLARIFYING
            candidates = [m.workflow_name for m in matches[:3]]
            return f"Did you mean one of these: {', '.join(candidates)}?", self.state
        else:
            return "I couldn't find a close match for that request.", self.state


# ============================================================================
# 5. BENCHMARK SUBSYSTEM (M5 / R5)
# ============================================================================

def create_notes_benchmark_workflow() -> WorkflowSpec:
    """Generates the canonical 5-phase Apple Notes weekly to-do list workflow."""
    todo_content = (
        "# 📋 Weekly Action Plan & Priorities\n\n"
        "[ ] Monday: Team sync, triage critical backlog, and align sprint goals\n"
        "[ ] Tuesday: Deep work on core architecture & refactoring\n"
        "[ ] Wednesday: Code reviews, unit testing, and integration verification\n"
        "[ ] Thursday: Performance profiling, benchmark validation, and bug hardening\n"
        "[ ] Friday: Weekly retrospective, release sign-off, and celebrate wins\n\n"
        "---\nGenerated autonomously by Autonomous Desktop Companion\n"
    )

    steps = [
        WorkflowStep(
            step_id="s1_launch",
            order=1,
            description="Launch Apple Notes application",
            action=ActionType.LAUNCH_APP,
            target={"bundle_id": "com.apple.Notes", "app_name": "Notes"},
        ),
        WorkflowStep(
            step_id="s2_focus",
            order=2,
            description="Focus Apple Notes window",
            action=ActionType.FOCUS_APP,
            target={"bundle_id": "com.apple.Notes"},
        ),
        WorkflowStep(
            step_id="s3_new_note",
            order=3,
            description="Create new note via Cmd+N shortcut",
            action=ActionType.PRESS_HOTKEY,
            payload={"keys": ["cmd", "n"]},
        ),
        WorkflowStep(
            step_id="s4_type_content",
            order=4,
            description="Inject formatted weekly to-do list",
            action=ActionType.PASTE_TEXT,
            payload={"text": todo_content},
        ),
    ]

    return WorkflowSpec(
        id="wf_notes_weekly_todo",
        name="Create Weekly To-Do in Apple Notes",
        description="Opens Apple Notes, creates a new note, and types weekly to-do list hands-free.",
        triggers={
            "canonical": "write my weekly to-do list",
            "aliases": [
                "plan my week in notes",
                "create weekly to-do list in notes",
                "new weekly todo note",
            ],
            "keywords": ["notes", "weekly", "todo", "plan"],
        },
        target_app={"bundle_id": "com.apple.Notes", "app_name": "Notes"},
        steps=steps,
    )


class BenchmarkCompletionVerifier:
    """Validates the execution results of the R5 Notes benchmark."""

    @staticmethod
    def verify(actuator: BaseActuator, expected_text_substring: str = "Weekly Action Plan") -> bool:
        if isinstance(actuator, MockActuator):
            # Check mock history for app launch, hotkey cmd+n, and typed text
            has_launch = any(
                a.action_type == "launch_app" and a.parameters.get("bundle_id") == "com.apple.Notes"
                for a in actuator.history
            )
            has_hotkey = any(
                a.action_type == "press_hotkey" and "n" in a.parameters.get("keys", [])
                for a in actuator.history
            )
            has_text = expected_text_substring in actuator.typed_text
            return has_launch and has_hotkey and has_text
        return False
