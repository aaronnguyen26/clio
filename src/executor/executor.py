"""Closed-Loop Autonomous Workflow Executor Subsystem.

Belongs to FEAT-EXEC-02 (AutonomousWorkflowExecutor).
Zero external dependencies: pure Python standard library.

Key Features & Invariants:
1. Closed-Loop Execution Loop:
   - Iterates through spec.steps sorted by order.
   - Enforces failsafe checks before, during, and after each step (actuator.check_failsafe()).
   - Broadcasts typed lifecycle events (TASK_STARTED, ACTION_STARTING, ACTION_COMPLETED,
     TEXT_TYPING_PROGRESS, RETRY_ATTEMPT, STEP_FAILED, EMERGENCY_STOP, TASK_COMPLETED, TASK_FAILED).
2. Dynamic Parameter Interpolation:
   - Resolves variable tokens (${doc_title}, ${CURRENT_DATE}) in payloads and targets via ParameterEngine.
3. Resolution-Independent Coordinate Projection:
   - Projects normalized window ratios (norm_x, norm_y) to absolute screen coordinates using
     CoordinateAdapter and window geometry from WindowInfo.
4. Comprehensive Action Dispatch:
   - Dispatches LAUNCH_APP, ACTIVATE_APP/FOCUS_APP, MOVE, CLICK, DOUBLE_CLICK, RIGHT_CLICK,
     DRAG, TYPE_TEXT, PASTE_TEXT, HOTKEY, WAIT, WAIT_WINDOW, SCROLL.
5. Independent Virtual Cursor Routing (Requirement R7):
   - Supports targeted virtual cursor dispatch (VirtualCursor) maintaining zero physical
     cursor displacement when use_virtual_cursor=True.
6. Per-Step Retry & Error Recovery:
   - Configurable max_retries and exponential backoff.
   - Error strategies: retry, abort, skip, and fallback.
7. Clean Failsafe Interception & Shutdown:
   - Immediately intercepts FailsafeEmergencyStop, halts execution, releases synthetic
     modifiers via actuator.stop(), resets virtual cursor, and emits EMERGENCY_STOP.
8. Telemetry & Memory Logging:
   - Returns typed ExecutionResult and records ExecutionRecord in TaskMemoryEngine if provided.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import datetime
import logging
import math
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Type, Union

from src.actuators.base import BaseActuator
from src.actuators.types import (
    ActuatorError,
    ApplicationLaunchError,
    FailsafeEmergencyStop,
    InputSynthesisError,
    MouseButton,
    WindowInfo,
    WindowNotFoundError,
    normalize_mouse_button,
)
from src.memory.coordinates import CoordinateAdapter
from src.memory.models import (
    ActionType,
    ExecutionRecord,
    TargetCoordinates,
    WorkflowSpec,
    WorkflowStep,
)
from src.memory.parameters import ParameterEngine

from src.actuators.virtual_cursor import VirtualCursor
from src.executor.events import EventType, ExecutionEvent, ExecutionEventBus
from src.executor.poller import WindowReadinessPoller
from src.memory.engine import TaskMemoryEngine

logger = logging.getLogger(__name__)


# =============================================================================
# Execution Result
# =============================================================================

@dataclass
class ExecutionResult:
    """Typed result delivered upon completion, failure, or emergency stop of a workflow.

    Maintains full backward compatibility with tests expecting duration_s or positional parameters.
    """
    success: bool
    task_id: str
    steps_completed: int = 0
    total_steps: int = 0
    error: Optional[str] = None
    duration: float = 0.0
    workflow_id: str = ""
    telemetry: Dict[str, Any] = field(default_factory=dict)
    duration_s: Optional[float] = None

    def __post_init__(self) -> None:
        if not self.workflow_id:
            self.workflow_id = self.task_id
        if self.duration_s is not None and self.duration == 0.0:
            self.duration = float(self.duration_s)
        elif self.duration_s is None:
            self.duration_s = self.duration

    @property
    def elapsed_seconds(self) -> float:
        """Alias for duration."""
        return self.duration

    @property
    def error_message(self) -> Optional[str]:
        """Alias for error string."""
        return self.error


# =============================================================================
# Autonomous Workflow Executor
# =============================================================================

class AutonomousWorkflowExecutor:
    """Closed-loop autonomous step executor with retry handling, delays, coordinate projection,
    targeted virtual cursor routing, and failsafe emergency stop interception.
    """

    def __init__(
        self,
        actuator: BaseActuator,
        bus: Optional[ExecutionEventBus] = None,
        virtual_cursor: Optional[VirtualCursor] = None,
        poller: Optional[WindowReadinessPoller] = None,
        parameter_engine: Optional[Type[ParameterEngine]] = None,
        coordinate_adapter: Optional[Type[CoordinateAdapter]] = None,
        memory_engine: Optional[TaskMemoryEngine] = None,
        use_virtual_cursor: bool = False,
        default_timeout_ms: int = 5000,
        default_poll_interval: float = 0.05,
        default_backoff_factor: float = 0.1,
        zero_delay: bool = False,
        background_mode: bool = False,
    ) -> None:
        """Initializes the AutonomousWorkflowExecutor.

        Args:
            actuator: BaseActuator implementation (Live MacOSActuator or MockActuator).
            bus: Pub/sub ExecutionEventBus. Defaults to fresh ExecutionEventBus.
            virtual_cursor: Optional VirtualCursor instance for non-displacing cursor actions.
            poller: Optional WindowReadinessPoller for dynamic window synchronization.
            parameter_engine: Parameter token interpolation engine. Defaults to ParameterEngine.
            coordinate_adapter: Window ratio projection adapter. Defaults to CoordinateAdapter.
            memory_engine: Optional TaskMemoryEngine for persisting execution telemetry.
            use_virtual_cursor: If True, routes mouse actions through VirtualCursor by default.
            default_timeout_ms: Default action timeout in milliseconds.
            default_poll_interval: Default polling tick interval in seconds.
            default_backoff_factor: Base multiplier for exponential backoff on retries.
            zero_delay: If True, skips execution delays (used for ultra-fast CI test runs).
            background_mode: If True, operates in the background with zero hardware mouse/keyboard stealing.
        """
        self.actuator = actuator
        self.bus = bus if bus is not None else ExecutionEventBus()
        self.virtual_cursor = virtual_cursor
        self.poller = poller if poller is not None else WindowReadinessPoller()
        self.parameter_engine = parameter_engine if parameter_engine is not None else ParameterEngine
        self.coordinate_adapter = coordinate_adapter if coordinate_adapter is not None else CoordinateAdapter
        self.memory_engine = memory_engine
        self.use_virtual_cursor = use_virtual_cursor
        self.default_timeout_ms = default_timeout_ms
        self.default_poll_interval = default_poll_interval
        self.default_backoff_factor = default_backoff_factor
        self.zero_delay = zero_delay
        self.background_mode = background_mode

    # =========================================================================
    # Main Workflow Execution Entry Point
    # =========================================================================

    def execute_workflow(
        self,
        spec: WorkflowSpec,
        runtime_params: Optional[Dict[str, Any]] = None,
        memory_engine: Optional[TaskMemoryEngine] = None,
        use_virtual_cursor: Optional[bool] = None,
        task_id: Optional[str] = None,
        background_mode: Optional[bool] = None,
    ) -> ExecutionResult:
        """Executes a complete workflow specification sequentially with closed-loop verification.

        Args:
            spec: WorkflowSpec describing the task, target app, and sequential steps.
            runtime_params: Runtime dictionary of parameter values overriding spec defaults.
            memory_engine: Optional override for TaskMemoryEngine telemetry recording.
            use_virtual_cursor: Optional override for virtual cursor routing flag.
            task_id: Optional unique task execution ID (defaults to spec.id).
            background_mode: Optional override to run hands-free in the background without stealing focus.

        Returns:
            ExecutionResult containing execution status, steps completed, error, and telemetry.
        """
        start_time = time.time()
        active_task_id = task_id or spec.id
        effective_memory = memory_engine or self.memory_engine
        active_bg = background_mode if background_mode is not None else getattr(self, "background_mode", False)
        active_use_vc = True if active_bg else (use_virtual_cursor if use_virtual_cursor is not None else self.use_virtual_cursor)
        if self.virtual_cursor is not None:
            self.virtual_cursor.background_mode = bool(active_bg)

        # Prepare parameters: merge spec.parameters with runtime_params
        effective_params: Dict[str, Any] = {}
        if hasattr(spec, "parameters") and isinstance(spec.parameters, dict):
            effective_params.update(spec.parameters)
        if runtime_params:
            effective_params.update(runtime_params)

        # Sort steps by order
        steps: List[WorkflowStep] = sorted(spec.steps, key=lambda s: getattr(s, "order", 0))
        total_steps = len(steps)
        steps_completed = 0
        step_records: List[Dict[str, Any]] = []

        target_bundle_or_app = ""
        if hasattr(spec, "target_app") and isinstance(spec.target_app, dict):
            target_bundle_or_app = spec.target_app.get("bundle_id") or spec.target_app.get("app_name", "")

        # Target virtual cursor to the workflow application if specified
        if target_bundle_or_app and self.virtual_cursor is not None:
            if hasattr(self.virtual_cursor, "set_target_bundle_id"):
                self.virtual_cursor.set_target_bundle_id(target_bundle_or_app)

        # Make virtual cursor visible on screen right at the start of execution
        if self.virtual_cursor is not None and getattr(self.virtual_cursor, "_vx", 0.0) <= 0.0:
            self.virtual_cursor.move_to(550.0, 350.0, duration=0.2, smooth=True)

        # Emit TASK_STARTED event
        self.bus.publish(
            ExecutionEvent(
                event_type=EventType.TASK_STARTED,
                task_id=active_task_id,
                message=f"Starting workflow: {spec.name}",
                target_app=target_bundle_or_app,
                step_index=0,
                total_steps=total_steps,
                payload={"workflow_id": spec.id, "workflow_name": spec.name, "total_steps": total_steps},
            )
        )

        try:
            # Enforce failsafe check before starting any steps
            self.actuator.check_failsafe()

            for idx, step in enumerate(steps, 1):
                # Execute step with retry/recovery logic
                step_record = self._execute_step_with_retries(
                    step=step,
                    step_index=idx,
                    total_steps=total_steps,
                    task_id=active_task_id,
                    spec=spec,
                    effective_params=effective_params,
                    use_virtual_cursor=active_use_vc,
                    background_mode=active_bg,
                )
                step_records.append(step_record)

                if step_record.get("skipped", False):
                    continue

                steps_completed += 1

            # Workflow successfully completed
            duration = time.time() - start_time
            self.bus.publish(
                ExecutionEvent(
                    event_type=EventType.TASK_COMPLETED,
                    task_id=active_task_id,
                    message=f"Workflow '{spec.name}' completed successfully ({steps_completed}/{total_steps} steps)",
                    target_app=target_bundle_or_app,
                    step_index=steps_completed,
                    total_steps=total_steps,
                    payload={"duration": duration, "steps_completed": steps_completed},
                )
            )

            # Record in memory engine
            self._record_telemetry_safe(
                memory_engine=effective_memory,
                workflow_id=spec.id,
                status="success",
                duration=duration,
                steps_completed=steps_completed,
                error_message=None,
                effective_params=effective_params,
            )

            return ExecutionResult(
                success=True,
                task_id=active_task_id,
                workflow_id=spec.id,
                steps_completed=steps_completed,
                total_steps=total_steps,
                error=None,
                duration=duration,
                telemetry={
                    "status": "success",
                    "duration": duration,
                    "step_records": step_records,
                    "parameters_used": effective_params,
                },
            )

        except FailsafeEmergencyStop as fes:
            duration = time.time() - start_time
            # Immediate emergency cleanup: halt actuator and release modifier keys
            try:
                self.actuator.stop()
            except Exception:
                pass

            if self.virtual_cursor is not None and hasattr(self.virtual_cursor, "reset"):
                try:
                    self.virtual_cursor.reset()
                except Exception:
                    pass

            self.bus.publish(
                ExecutionEvent(
                    event_type=EventType.EMERGENCY_STOP,
                    task_id=active_task_id,
                    message=f"Emergency stop halted execution: {fes}",
                    target_app=target_bundle_or_app,
                    step_index=steps_completed,
                    total_steps=total_steps,
                    payload={"reason": str(fes), "corner": getattr(fes, "corner", None)},
                )
            )

            self._record_telemetry_safe(
                memory_engine=effective_memory,
                workflow_id=spec.id,
                status="emergency_stop",
                duration=duration,
                steps_completed=steps_completed,
                error_message=str(fes),
                effective_params=effective_params,
            )

            return ExecutionResult(
                success=False,
                task_id=active_task_id,
                workflow_id=spec.id,
                steps_completed=steps_completed,
                total_steps=total_steps,
                error=str(fes),
                duration=duration,
                telemetry={
                    "status": "emergency_stop",
                    "stopped_at_step": steps_completed + 1,
                    "duration": duration,
                    "step_records": step_records,
                },
            )

        except Exception as exc:
            duration = time.time() - start_time
            # Halt actuator on unhandled error
            try:
                self.actuator.stop()
            except Exception:
                pass

            self.bus.publish(
                ExecutionEvent(
                    event_type=EventType.TASK_FAILED,
                    task_id=active_task_id,
                    message=f"Workflow failed at step {steps_completed + 1}: {exc}",
                    target_app=target_bundle_or_app,
                    step_index=steps_completed,
                    total_steps=total_steps,
                    payload={"error": str(exc)},
                )
            )

            self._record_telemetry_safe(
                memory_engine=effective_memory,
                workflow_id=spec.id,
                status="failed",
                duration=duration,
                steps_completed=steps_completed,
                error_message=str(exc),
                effective_params=effective_params,
            )

            return ExecutionResult(
                success=False,
                task_id=active_task_id,
                workflow_id=spec.id,
                steps_completed=steps_completed,
                total_steps=total_steps,
                error=str(exc),
                duration=duration,
                telemetry={
                    "status": "failed",
                    "failed_at_step": steps_completed + 1,
                    "duration": duration,
                    "step_records": step_records,
                },
            )

    # Alias for convenience
    execute = execute_workflow

    # =========================================================================
    # Step Execution & Retry Loop
    # =========================================================================

    def _execute_step_with_retries(
        self,
        step: WorkflowStep,
        step_index: int,
        total_steps: int,
        task_id: str,
        spec: WorkflowSpec,
        effective_params: Dict[str, Any],
        use_virtual_cursor: bool,
        background_mode: bool = False,
    ) -> Dict[str, Any]:
        """Executes a single workflow step, applying per-step retry backoff and error recovery."""
        error_handling = getattr(step, "error_handling", {}) or {}
        on_failure = error_handling.get("on_failure", "retry")
        max_retries = int(error_handling.get("max_retries", 1))
        backoff_s = float(error_handling.get("backoff_s", error_handling.get("backoff_factor", self.default_backoff_factor)))

        retry_count = 0
        step_start_time = time.time()
        last_exception: Optional[Exception] = None

        while True:
            # Always check failsafe before each attempt
            self.actuator.check_failsafe()

            # Emit ACTION_STARTING event
            action_name = self._normalize_action_name(step.action)
            target_app_name = self._resolve_target_app(step, spec)

            self.bus.publish(
                ExecutionEvent(
                    event_type=EventType.ACTION_STARTING,
                    task_id=task_id,
                    message=step.description or f"Executing step {step_index}: {action_name}",
                    target_app=target_app_name,
                    step_index=step_index,
                    total_steps=total_steps,
                    payload={
                        "step_id": step.step_id,
                        "action": action_name,
                        "retry_attempt": retry_count,
                    },
                )
            )

            try:
                # Pre-delay
                pre_delay_ms = self._get_timing_val(step, "pre_delay_ms", 0)
                if pre_delay_ms > 0:
                    self._interruptible_sleep(pre_delay_ms / 1000.0)

                # Execute action dispatch
                self._dispatch_action(
                    step=step,
                    spec=spec,
                    effective_params=effective_params,
                    use_virtual_cursor=use_virtual_cursor,
                    task_id=task_id,
                    step_index=step_index,
                    total_steps=total_steps,
                    background_mode=background_mode,
                )

                # Post-delay
                post_delay_ms = self._get_timing_val(step, "post_delay_ms", 50)
                if post_delay_ms > 0:
                    self._interruptible_sleep(post_delay_ms / 1000.0)

                # Check failsafe after action completion
                self.actuator.check_failsafe()

                # Emit ACTION_COMPLETED event
                self.bus.publish(
                    ExecutionEvent(
                        event_type=EventType.ACTION_COMPLETED,
                        task_id=task_id,
                        message=f"Step {step_index} completed: {step.description or action_name}",
                        target_app=target_app_name,
                        step_index=step_index,
                        total_steps=total_steps,
                        payload={
                            "step_id": step.step_id,
                            "action": action_name,
                            "duration_s": time.time() - step_start_time,
                            "retries": retry_count,
                        },
                    )
                )

                return {
                    "step_id": step.step_id,
                    "order": step.order,
                    "action": action_name,
                    "status": "success",
                    "retries": retry_count,
                    "duration_s": time.time() - step_start_time,
                }

            except FailsafeEmergencyStop:
                # Emergency stop must NEVER be caught or retried by step error handling!
                raise

            except Exception as exc:
                last_exception = exc
                logger.warning(
                    "Step %s ('%s') failed on attempt %d: %s",
                    step.step_id,
                    action_name,
                    retry_count + 1,
                    exc,
                )

                # Check retry capability
                if on_failure == "retry" and retry_count < max_retries:
                    retry_count += 1
                    sleep_time = backoff_s * (2 ** (retry_count - 1))

                    self.bus.publish(
                        ExecutionEvent(
                            event_type=EventType.RETRY_ATTEMPT,
                            task_id=task_id,
                            message=f"Retrying step {step_index} ({retry_count}/{max_retries}) after {sleep_time:.2f}s: {exc}",
                            target_app=target_app_name,
                            step_index=step_index,
                            total_steps=total_steps,
                            payload={
                                "step_id": step.step_id,
                                "retry_count": retry_count,
                                "max_retries": max_retries,
                                "backoff_s": sleep_time,
                                "error": str(exc),
                            },
                        )
                    )

                    self._interruptible_sleep(sleep_time)
                    continue  # Retry step

                # Retries exhausted or alternate error strategy
                self.bus.publish(
                    ExecutionEvent(
                        event_type=EventType.STEP_FAILED,
                        task_id=task_id,
                        message=f"Step {step_index} failed permanently: {exc}",
                        target_app=target_app_name,
                        step_index=step_index,
                        total_steps=total_steps,
                        payload={
                            "step_id": step.step_id,
                            "error": str(exc),
                            "on_failure": on_failure,
                        },
                    )
                )

                if on_failure == "skip":
                    logger.info("Skipping failed step %s per on_failure='skip' policy", step.step_id)
                    return {
                        "step_id": step.step_id,
                        "order": step.order,
                        "action": action_name,
                        "status": "skipped",
                        "skipped": True,
                        "error": str(exc),
                    }
                elif on_failure == "fallback":
                    fallback_id = error_handling.get("fallback_step_id")
                    if fallback_id:
                        fallback_step = next((s for s in spec.steps if s.step_id == fallback_id), None)
                        if fallback_step:
                            logger.info("Executing fallback step %s for failed step %s", fallback_id, step.step_id)
                            return self._execute_step_with_retries(
                                step=fallback_step,
                                step_index=step_index,
                                total_steps=total_steps,
                                task_id=task_id,
                                spec=spec,
                                effective_params=effective_params,
                                use_virtual_cursor=use_virtual_cursor,
                            )
                    raise last_exception or exc
                else:
                    # Default: on_failure == "abort" or retries exhausted
                    raise last_exception or exc

    @staticmethod
    def _detect_dock_app_at(x: float, y: float) -> Optional[str]:
        """Detects if (x, y) corresponds to an application icon in the macOS Dock via Accessibility."""
        if sys.platform != "darwin":
            return None
        try:
            import ctypes
            from ctypes import c_void_p, c_float, byref, c_int, c_char_p, c_bool
            hiservices = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
            cf = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")

            hiservices.AXUIElementCreateSystemWide.restype = c_void_p
            hiservices.AXUIElementCopyElementAtPosition.argtypes = [c_void_p, c_float, c_float, ctypes.POINTER(c_void_p)]
            hiservices.AXUIElementCopyElementAtPosition.restype = c_int
            hiservices.AXUIElementCopyAttributeValue.argtypes = [c_void_p, c_void_p, ctypes.POINTER(c_void_p)]
            hiservices.AXUIElementCopyAttributeValue.restype = c_int

            cf.CFStringCreateWithCString.argtypes = [c_void_p, c_char_p, c_int]
            cf.CFStringCreateWithCString.restype = c_void_p
            cf.CFStringGetCString.argtypes = [c_void_p, c_char_p, c_int, c_int]
            cf.CFStringGetCString.restype = c_bool
            cf.CFRelease.argtypes = [c_void_p]

            sys_elem = hiservices.AXUIElementCreateSystemWide()
            elem = c_void_p()
            if hiservices.AXUIElementCopyElementAtPosition(sys_elem, c_float(x), c_float(y), byref(elem)) != 0 or not elem.value:
                return None

            cf_role = cf.CFStringCreateWithCString(None, b"AXRole", 0x08000100)
            role_val = c_void_p()
            role_str = ""
            if hiservices.AXUIElementCopyAttributeValue(elem, cf_role, byref(role_val)) == 0 and role_val.value:
                buf = ctypes.create_string_buffer(256)
                if cf.CFStringGetCString(role_val, buf, 256, 0x08000100):
                    role_str = buf.value.decode("utf-8")
                cf.CFRelease(role_val)
            cf.CFRelease(cf_role)

            title_str = None
            if role_str == "AXDockItem":
                cf_title = cf.CFStringCreateWithCString(None, b"AXTitle", 0x08000100)
                title_val = c_void_p()
                if hiservices.AXUIElementCopyAttributeValue(elem, cf_title, byref(title_val)) == 0 and title_val.value:
                    buf2 = ctypes.create_string_buffer(256)
                    if cf.CFStringGetCString(title_val, buf2, 256, 0x08000100):
                        title_str = buf2.value.decode("utf-8")
                    cf.CFRelease(title_val)
                cf.CFRelease(cf_title)

            cf.CFRelease(elem)
            return title_str
        except Exception:
            pass
        return None

    @staticmethod
    def _find_dock_item(query_name: str) -> Optional[Tuple[float, float, str, Any]]:
        """Finds screen coordinates (center_x, center_y), title, and AX element of an app in the macOS Dock."""
        if sys.platform != "darwin" or not query_name:
            return None
        try:
            import ctypes
            import subprocess
            from ctypes import c_void_p, c_int, c_char_p, c_bool, byref, c_double, Structure

            class CGPoint(Structure):
                _fields_ = [("x", c_double), ("y", c_double)]

            class CGSize(Structure):
                _fields_ = [("width", c_double), ("height", c_double)]

            hiservices = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
            cf = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")

            hiservices.AXUIElementCreateApplication.argtypes = [c_int]
            hiservices.AXUIElementCreateApplication.restype = c_void_p
            hiservices.AXUIElementCopyAttributeValue.argtypes = [c_void_p, c_void_p, ctypes.POINTER(c_void_p)]
            hiservices.AXUIElementCopyAttributeValue.restype = c_int
            hiservices.AXUIElementPerformAction.argtypes = [c_void_p, c_void_p]
            hiservices.AXUIElementPerformAction.restype = c_int
            hiservices.AXValueGetValue.argtypes = [c_void_p, c_int, c_void_p]
            hiservices.AXValueGetValue.restype = c_bool

            cf.CFStringCreateWithCString.argtypes = [c_void_p, c_char_p, c_int]
            cf.CFStringCreateWithCString.restype = c_void_p
            cf.CFStringGetCString.argtypes = [c_void_p, c_char_p, c_int, c_int]
            cf.CFStringGetCString.restype = c_bool
            cf.CFArrayGetCount.argtypes = [c_void_p]
            cf.CFArrayGetCount.restype = c_int
            cf.CFArrayGetValueAtIndex.argtypes = [c_void_p, c_int]
            cf.CFArrayGetValueAtIndex.restype = c_void_p
            cf.CFRelease.argtypes = [c_void_p]

            clean_q = query_name.lower().strip()
            if clean_q.endswith(".app"):
                clean_q = clean_q[:-4]
            if clean_q.startswith("com.apple."):
                clean_q = clean_q[len("com.apple."):]
            synonyms = {
                "ical": "calendar",
                "chrome": "google chrome",
                "code": "visual studio code",
                "term": "terminal",
                "prefs": "system settings",
                "preferences": "system settings",
            }
            clean_q = synonyms.get(clean_q, clean_q)

            dock_pid = int(subprocess.check_output(["pgrep", "-x", "Dock"]).decode().strip())
            dock_app = hiservices.AXUIElementCreateApplication(dock_pid)

            cf_children = cf.CFStringCreateWithCString(None, b"AXChildren", 0x08000100)
            cf_title = cf.CFStringCreateWithCString(None, b"AXTitle", 0x08000100)
            cf_pos = cf.CFStringCreateWithCString(None, b"AXPosition", 0x08000100)
            cf_size = cf.CFStringCreateWithCString(None, b"AXSize", 0x08000100)

            result = None
            children_val = c_void_p()
            if hiservices.AXUIElementCopyAttributeValue(dock_app, cf_children, byref(children_val)) == 0 and children_val.value:
                cnt = cf.CFArrayGetCount(children_val)
                for i in range(cnt):
                    child = cf.CFArrayGetValueAtIndex(children_val, i)
                    sub_val = c_void_p()
                    if hiservices.AXUIElementCopyAttributeValue(child, cf_children, byref(sub_val)) == 0 and sub_val.value:
                        sub_cnt = cf.CFArrayGetCount(sub_val)
                        for j in range(sub_cnt):
                            item = cf.CFArrayGetValueAtIndex(sub_val, j)
                            t_val = c_void_p()
                            title_str = ""
                            if hiservices.AXUIElementCopyAttributeValue(item, cf_title, byref(t_val)) == 0 and t_val.value:
                                buf = ctypes.create_string_buffer(256)
                                if cf.CFStringGetCString(t_val, buf, 256, 0x08000100):
                                    title_str = buf.value.decode("utf-8")
                                cf.CFRelease(t_val)

                            t_lower = title_str.lower().strip()
                            if t_lower and (clean_q == t_lower or (len(clean_q) >= 3 and clean_q in t_lower)):
                                pos_val = c_void_p()
                                pt = CGPoint()
                                if hiservices.AXUIElementCopyAttributeValue(item, cf_pos, byref(pos_val)) == 0 and pos_val.value:
                                    hiservices.AXValueGetValue(pos_val, 1, byref(pt))
                                    cf.CFRelease(pos_val)

                                size_val = c_void_p()
                                sz = CGSize()
                                if hiservices.AXUIElementCopyAttributeValue(item, cf_size, byref(size_val)) == 0 and size_val.value:
                                    hiservices.AXValueGetValue(size_val, 2, byref(sz))
                                    cf.CFRelease(size_val)

                                cx = pt.x + sz.width / 2.0
                                cy = pt.y + sz.height / 2.0
                                result = (cx, cy, title_str)
                                break
                        cf.CFRelease(sub_val)
                    if result:
                        break
                cf.CFRelease(children_val)

            cf.CFRelease(cf_children)
            cf.CFRelease(cf_title)
            cf.CFRelease(cf_pos)
            cf.CFRelease(cf_size)
            cf.CFRelease(dock_app)
            return result
        except Exception as e:
            logger.debug("Failed querying Dock for item '%s': %s", query_name, e)
            return None

    # =========================================================================
    # Action Dispatcher
    # =========================================================================

    def _dispatch_action(
        self,
        step: WorkflowStep,
        spec: WorkflowSpec,
        effective_params: Dict[str, Any],
        use_virtual_cursor: bool,
        task_id: str,
        step_index: int,
        total_steps: int,
        background_mode: bool = False,
    ) -> None:
        """Dispatches an individual workflow step action to actuator or virtual cursor."""
        action_name = self._normalize_action_name(step.action)
        payload = getattr(step, "payload", {}) or {}
        target = getattr(step, "target", {}) or {}
        timeout_ms = self._get_timing_val(step, "timeout_ms", self.default_timeout_ms)
        timeout_s = timeout_ms / 1000.0

        # Step-level virtual cursor override
        step_use_vc = use_virtual_cursor or background_mode
        if target.get("use_virtual_cursor") is not None:
            step_use_vc = bool(target["use_virtual_cursor"])
        elif payload.get("use_virtual_cursor") is not None:
            step_use_vc = bool(payload["use_virtual_cursor"])

        # ---------------------------------------------------------------------
        # 1. LAUNCH_APP
        # ---------------------------------------------------------------------
        if action_name == "launch_app":
            app_id = self._interpolate_str(
                target.get("bundle_id") or target.get("app_name") or payload.get("app") or payload.get("bundle_id", ""),
                effective_params,
            )
            if not app_id:
                raise ApplicationLaunchError(f"No application bundle_id or name specified in step {step.step_id}")

            self.bus.publish(
                ExecutionEvent(
                    event_type=EventType.APP_LAUNCH_INIT,
                    task_id=task_id,
                    message=f"Launching application '{app_id}'",
                    target_app=app_id,
                    step_index=step_index,
                    total_steps=total_steps,
                    payload={"app": app_id, "timeout_s": timeout_s},
                )
            )

            # PHYSICAL CURSOR EXECUTION:
            # First, check if the app exists in the macOS Dock
            dock_info = self._find_dock_item(app_id)
            if dock_info and self.virtual_cursor is not None and step_use_vc:
                dock_x, dock_y, dock_title = dock_info
                logger.info(
                    "PHYSICAL LAUNCH: Moving virtual cursor to Dock icon '%s' at (%.1f, %.1f) and clicking",
                    dock_title,
                    dock_x,
                    dock_y,
                )
                self.virtual_cursor.move_to(dock_x, dock_y, duration=0.45, smooth=True)
                if not background_mode:
                    self.virtual_cursor.click(x=dock_x, y=dock_y, button="left", click_count=1)

                # Launch target application
                self.actuator.launch_app(app_id, timeout=timeout_s)

            elif target.get("screen_x") and target.get("screen_y") and self.virtual_cursor is not None and step_use_vc:
                # App was recorded at specific screen coordinates
                sx = float(target["screen_x"])
                sy = float(target["screen_y"])
                self.virtual_cursor.move_to(sx, sy, duration=0.45, smooth=True)
                if not background_mode:
                    self.virtual_cursor.click(x=sx, y=sy, button="left", click_count=1)
                self.actuator.launch_app(app_id, timeout=timeout_s)

            else:
                # App not in Dock: use Spotlight only if NOT in background mode
                if not background_mode and self.virtual_cursor is not None and step_use_vc:
                    try:
                        self.actuator.press_hotkey("cmd", "space")
                        self._interruptible_sleep(0.15)
                        self.virtual_cursor.move_to(700.0, 220.0, duration=0.35, smooth=True)
                        if hasattr(self.virtual_cursor, "type_text"):
                            self.virtual_cursor.type_text(app_id, interval=0.03)
                        self._interruptible_sleep(0.15)
                        self.actuator.press_hotkey("return")
                    except Exception:
                        pass
                self.actuator.launch_app(app_id, timeout=timeout_s)

            self.bus.publish(
                ExecutionEvent(
                    event_type=EventType.APP_LAUNCHED,
                    task_id=task_id,
                    message=f"Application '{app_id}' launched successfully",
                    target_app=app_id,
                    step_index=step_index,
                    total_steps=total_steps,
                    payload={"app": app_id},
                )
            )

            # If virtual cursor is used, configure target window/pid and glide into it
            if step_use_vc and self.virtual_cursor is not None:
                if hasattr(self.virtual_cursor, "set_target_bundle_id"):
                    self.virtual_cursor.set_target_bundle_id(app_id)
                
                win = None
                for _ in range(15):
                    windows = self.actuator.get_windows(app_id)
                    if windows:
                        win = windows[0]
                        break
                    time.sleep(0.1)

                if win:
                    if hasattr(self.virtual_cursor, "set_target_window"):
                        self.virtual_cursor.set_target_window(win)
                    target_x = max(100.0, win.x + min(250.0, win.width / 2))
                    target_y = max(80.0, win.y + min(120.0, win.height / 3))
                    self.virtual_cursor.move_to(target_x, target_y, duration=0.35, smooth=True)

        # ---------------------------------------------------------------------
        # 2. ACTIVATE_APP / FOCUS_APP
        # ---------------------------------------------------------------------
        elif action_name in ("activate_app", "focus_app"):
            app_id = self._interpolate_str(
                target.get("bundle_id") or target.get("app_name") or payload.get("app") or payload.get("bundle_id", ""),
                effective_params,
            )
            if not app_id:
                raise ApplicationLaunchError(f"No application bundle_id or name specified in step {step.step_id}")
            if not background_mode:
                self.actuator.focus_app(app_id)
            if self.virtual_cursor is not None:
                if hasattr(self.virtual_cursor, "set_target_bundle_id"):
                    self.virtual_cursor.set_target_bundle_id(app_id)
                windows = self.actuator.get_windows(app_id)
                if windows:
                    win = windows[0]
                    if hasattr(self.virtual_cursor, "set_target_window"):
                        self.virtual_cursor.set_target_window(win)
                    target_x = max(100.0, win.x + min(250.0, win.width / 2))
                    target_y = max(80.0, win.y + min(120.0, win.height / 3))
                    self.virtual_cursor.move_to(target_x, target_y, duration=0.3, smooth=True)

        # ---------------------------------------------------------------------
        # 3. MOVE / MOVE_MOUSE
        # ---------------------------------------------------------------------
        elif action_name in ("move", "move_mouse"):
            sx, sy = self._resolve_screen_coordinates(step, spec, effective_params)
            duration = float(payload.get("duration", 0.0))
            smooth = bool(payload.get("smooth", False))

            if step_use_vc and self.virtual_cursor is not None:
                self.virtual_cursor.move_to(sx, sy, duration=duration, smooth=smooth)
            else:
                self.actuator.move_mouse(sx, sy, smooth=smooth, duration=duration)

        # ---------------------------------------------------------------------
        # 4. CLICK
        # ---------------------------------------------------------------------
        elif action_name == "click":
            coords = self._resolve_optional_screen_coordinates(step, spec, effective_params)
            btn = normalize_mouse_button(payload.get("button", MouseButton.LEFT))
            click_count = int(payload.get("click_count", 1))

            if coords is not None:
                sx, sy = coords
            else:
                win = getattr(self.virtual_cursor, "target_window", None) if self.virtual_cursor else None
                if win:
                    sx = max(50.0, win.x + 200.0)
                    sy = max(50.0, win.y + 150.0)
                else:
                    sx, sy = 400.0, 300.0

            # Check if this click targets an item in the macOS Dock
            dock_app = self._detect_dock_app_at(sx, sy)
            if dock_app:
                logger.info("Click at (%s, %s) identified as Dock icon for '%s' — launching app", sx, sy, dock_app)
                if step_use_vc and self.virtual_cursor is not None:
                    self.virtual_cursor.move_to(sx, sy, duration=0.35, smooth=True)
                    self.virtual_cursor.click(x=sx, y=sy, button=btn, click_count=click_count)
                else:
                    self.actuator.click(x=sx, y=sy, button=MouseButton(btn), click_count=click_count)

                # Launch and activate the clicked application
                self.actuator.launch_app(dock_app)
                self.actuator.focus_app(dock_app)
                if step_use_vc and self.virtual_cursor is not None:
                    self.virtual_cursor.set_target_bundle_id(dock_app)
                    windows = self.actuator.get_windows(dock_app)
                    if windows:
                        win = windows[0]
                        self.virtual_cursor.move_to(win.x + win.width / 2, win.y + win.height / 3, duration=0.35, smooth=True)
            elif step_use_vc and self.virtual_cursor is not None:
                target_bundle = target.get("bundle_id") or payload.get("bundle_id")
                if target_bundle and hasattr(self.virtual_cursor, "set_target_bundle_id"):
                    self.virtual_cursor.set_target_bundle_id(target_bundle)
                self.virtual_cursor.move_to(sx, sy, duration=0.25, smooth=True)
                self.virtual_cursor.click(x=sx, y=sy, button=btn, click_count=click_count)
            else:
                # Fallback: direct hardware actuator click
                self.actuator.click(x=sx, y=sy, button=MouseButton(btn), click_count=click_count)

        # ---------------------------------------------------------------------
        # 5. DOUBLE_CLICK
        # ---------------------------------------------------------------------
        elif action_name == "double_click":
            coords = self._resolve_optional_screen_coordinates(step, spec, effective_params)
            btn = normalize_mouse_button(payload.get("button", MouseButton.LEFT))

            if coords is not None:
                sx, sy = coords
            else:
                win = getattr(self.virtual_cursor, "target_window", None) if self.virtual_cursor else None
                if win:
                    sx = max(50.0, win.x + 200.0)
                    sy = max(50.0, win.y + 150.0)
                else:
                    sx, sy = 400.0, 300.0

            if step_use_vc and self.virtual_cursor is not None:
                self.virtual_cursor.move_to(sx, sy, duration=0.25, smooth=True)
                self.virtual_cursor.click(x=sx, y=sy, button=btn, click_count=2)
            else:
                self.actuator.click(x=sx, y=sy, button=MouseButton(btn), click_count=2)

        # ---------------------------------------------------------------------
        # 6. RIGHT_CLICK
        # ---------------------------------------------------------------------
        elif action_name == "right_click":
            coords = self._resolve_optional_screen_coordinates(step, spec, effective_params)
            if coords is not None:
                sx, sy = coords
            else:
                win = getattr(self.virtual_cursor, "target_window", None) if self.virtual_cursor else None
                if win:
                    sx = max(50.0, win.x + 200.0)
                    sy = max(50.0, win.y + 150.0)
                else:
                    sx, sy = 400.0, 300.0

            if step_use_vc and self.virtual_cursor is not None:
                self.virtual_cursor.move_to(sx, sy, duration=0.25, smooth=True)
                self.virtual_cursor.click(x=sx, y=sy, button="right", click_count=1)
            else:
                self.actuator.click(x=sx, y=sy, button=MouseButton.RIGHT, click_count=1)

        # ---------------------------------------------------------------------
        # 7. DRAG
        # ---------------------------------------------------------------------
        elif action_name == "drag":
            duration = float(payload.get("duration", 0.3))
            start_coords = payload.get("start") or payload.get("from")
            end_coords = payload.get("end") or payload.get("to")

            if start_coords and end_coords:
                sx, sy = float(start_coords[0]), float(start_coords[1])
                ex, ey = float(end_coords[0]), float(end_coords[1])
            else:
                sx = float(payload.get("start_x", 0.0))
                sy = float(payload.get("start_y", 0.0))
                ex = float(payload.get("end_x", sx))
                ey = float(payload.get("end_y", sy))

            if step_use_vc and self.virtual_cursor is not None:
                self.virtual_cursor.move_to(sx, sy)
                self.virtual_cursor.drag_to(ex, ey, duration=duration)
            else:
                self.actuator.drag(sx, sy, ex, ey, duration=duration)

        # ---------------------------------------------------------------------
        # 8. TYPE_TEXT
        # ---------------------------------------------------------------------
        elif action_name == "type_text":
            raw_text = payload.get("text", "")
            interpolated_text = self._interpolate_str(raw_text, effective_params)
            interval = float(payload.get("interval", 0.02))

            target_bundle = target.get("bundle_id") or payload.get("bundle_id")
            if target_bundle:
                if not background_mode:
                    self.actuator.focus_app(target_bundle)
                if self.virtual_cursor is not None and hasattr(self.virtual_cursor, "set_target_bundle_id"):
                    self.virtual_cursor.set_target_bundle_id(target_bundle)

            self.bus.publish(
                ExecutionEvent(
                    event_type=EventType.TEXT_TYPING_PROGRESS,
                    task_id=task_id,
                    message=f"Typing {len(interpolated_text)} characters",
                    target_app=self._resolve_target_app(step, spec),
                    step_index=step_index,
                    total_steps=total_steps,
                    payload={"length": len(interpolated_text)},
                )
            )

            if step_use_vc and self.virtual_cursor is not None and hasattr(self.virtual_cursor, "type_text"):
                # Virtual cursor handles typing via CGEventPostToPid to target PID
                self.virtual_cursor.type_text(interpolated_text, interval=interval)
            else:
                self.actuator.type_text(interpolated_text, interval=interval)

        # ---------------------------------------------------------------------
        # 9. PASTE_TEXT
        # ---------------------------------------------------------------------
        elif action_name == "paste_text":
            raw_text = payload.get("text", "")
            interpolated_text = self._interpolate_str(raw_text, effective_params)
            target_bundle = target.get("bundle_id") or payload.get("bundle_id")
            if target_bundle:
                if not background_mode:
                    self.actuator.focus_app(target_bundle)
                if self.virtual_cursor is not None and hasattr(self.virtual_cursor, "set_target_bundle_id"):
                    self.virtual_cursor.set_target_bundle_id(target_bundle)

            if step_use_vc and self.virtual_cursor is not None and hasattr(self.virtual_cursor, "paste_text"):
                self.virtual_cursor.paste_text(interpolated_text)
            else:
                self.actuator.paste_text(interpolated_text)

        # ---------------------------------------------------------------------
        # 10. HOTKEY / PRESS_HOTKEY
        # ---------------------------------------------------------------------
        elif action_name in ("hotkey", "press_hotkey"):
            keys = payload.get("keys")
            if keys is None:
                raw_key = payload.get("key", "")
                if "+" in raw_key:
                    keys = [k.strip() for k in raw_key.split("+")]
                elif raw_key:
                    keys = [raw_key]
                else:
                    keys = []

            target_bundle = target.get("bundle_id") or payload.get("bundle_id")
            if target_bundle:
                if not background_mode:
                    self.actuator.focus_app(target_bundle)
                if self.virtual_cursor is not None and hasattr(self.virtual_cursor, "set_target_bundle_id"):
                    self.virtual_cursor.set_target_bundle_id(target_bundle)

            # Interpolate any key tokens if string
            interpolated_keys = [
                self._interpolate_str(str(k), effective_params) for k in keys
            ]
            if step_use_vc and self.virtual_cursor is not None and hasattr(self.virtual_cursor, "press_hotkey"):
                self.virtual_cursor.press_hotkey(*interpolated_keys)
            else:
                self.actuator.press_hotkey(*interpolated_keys)

        # ---------------------------------------------------------------------
        # 11. WAIT
        # ---------------------------------------------------------------------
        elif action_name == "wait":
            sec = float(payload.get("duration", payload.get("seconds", payload.get("duration_s", 0.0))))
            if sec == 0.0 and "duration_ms" in payload:
                sec = float(payload["duration_ms"]) / 1000.0
            self._interruptible_sleep(sec)

        # ---------------------------------------------------------------------
        # 12. WAIT_WINDOW
        # ---------------------------------------------------------------------
        elif action_name == "wait_window":
            app_id = self._interpolate_str(
                target.get("app_name") or target.get("bundle_id") or payload.get("app") or "",
                effective_params,
            )
            title = payload.get("title") or target.get("title")

            self.poller.wait_for_window(
                self.actuator,
                app_name=app_id or None,
                title=title,
                timeout=timeout_s,
                poll_interval=self.default_poll_interval,
            )

        # ---------------------------------------------------------------------
        # 13. SCROLL
        # ---------------------------------------------------------------------
        elif action_name == "scroll":
            dx = int(payload.get("dx", 0))
            dy = int(payload.get("dy", 0))
            if step_use_vc and self.virtual_cursor is not None:
                self.virtual_cursor.scroll(dx=dx, dy=dy)
            else:
                self.actuator.scroll(dx=dx, dy=dy)

        # ---------------------------------------------------------------------
        # 14. OPEN_URL
        # ---------------------------------------------------------------------
        elif action_name == "open_url":
            url = self._interpolate_str(payload.get("url", ""), effective_params)
            if hasattr(self.actuator, "open_url"):
                self.actuator.open_url(url)
            else:
                self.actuator.press_hotkey("cmd", "l")
                self.actuator.paste_text(url)
                self.actuator.press_hotkey("return")

        else:
            raise ActuatorError(f"Unsupported action type '{action_name}' in step {step.step_id}")

    # =========================================================================
    # Coordinate Resolution & Projection Helpers
    # =========================================================================

    def _resolve_screen_coordinates(
        self,
        step: WorkflowStep,
        spec: WorkflowSpec,
        effective_params: Dict[str, Any],
    ) -> Tuple[float, float]:
        """Resolves screen coordinates for a step, raising an error if coordinates cannot be determined."""
        coords = self._resolve_optional_screen_coordinates(step, spec, effective_params)
        if coords is None:
            raise InputSynthesisError(f"Step {step.step_id} requires coordinates but none could be determined.")
        return coords

    def _resolve_optional_screen_coordinates(
        self,
        step: WorkflowStep,
        spec: WorkflowSpec,
        effective_params: Dict[str, Any],
    ) -> Optional[Tuple[float, float]]:
        """Resolves screen coordinates from step coordinates metadata or payload."""
        payload = getattr(step, "payload", {}) or {}
        target = getattr(step, "target", {}) or {}
        step_coords = getattr(step, "coordinates", None)

        # 0. Check step.target for screen_x / screen_y or x / y or norm_x / norm_y
        if isinstance(target, dict):
            if "screen_x" in target and "screen_y" in target:
                return float(target["screen_x"]), float(target["screen_y"])
            if "x" in target and "y" in target:
                return float(target["x"]), float(target["y"])
            if "norm_x" in target and "norm_y" in target:
                return self._project_norm_to_screen(float(target["norm_x"]), float(target["norm_y"]), step, spec)

        # 1. Direct explicit (x, y) coordinates in payload
        if "x" in payload and "y" in payload:
            return float(payload["x"]), float(payload["y"])
        if "screen_x" in payload and "screen_y" in payload:
            return float(payload["screen_x"]), float(payload["screen_y"])

        # 2. Coordinates embedded in payload dict: payload["coordinates"]
        raw_coords = payload.get("coordinates")
        if isinstance(raw_coords, dict):
            if "x" in raw_coords and "y" in raw_coords:
                return float(raw_coords["x"]), float(raw_coords["y"])
            if "abs_x" in raw_coords and "abs_y" in raw_coords:
                return float(raw_coords["abs_x"]), float(raw_coords["abs_y"])
            if "norm_x" in raw_coords and "norm_y" in raw_coords:
                return self._project_norm_to_screen(
                    float(raw_coords["norm_x"]),
                    float(raw_coords["norm_y"]),
                    step,
                    spec,
                )

        # 3. TargetCoordinates instance on step: step.coordinates
        if step_coords is not None:
            if hasattr(step_coords, "abs_x") and step_coords.abs_x is not None and step_coords.abs_y is not None:
                return float(step_coords.abs_x), float(step_coords.abs_y)
            if hasattr(step_coords, "norm_x") and step_coords.norm_x is not None and step_coords.norm_y is not None:
                return self._project_norm_to_screen(
                    float(step_coords.norm_x),
                    float(step_coords.norm_y),
                    step,
                    spec,
                )

        # 4. Check norm_x and norm_y directly in payload
        if "norm_x" in payload and "norm_y" in payload:
            return self._project_norm_to_screen(
                float(payload["norm_x"]),
                float(payload["norm_y"]),
                step,
                spec,
            )

        return None

    def _project_norm_to_screen(
        self,
        norm_x: float,
        norm_y: float,
        step: WorkflowStep,
        spec: WorkflowSpec,
    ) -> Tuple[float, float]:
        """Projects normalized (norm_x, norm_y) to absolute screen coordinates using target window or screen."""
        app_name = self._resolve_target_app(step, spec)
        target_win: Optional[WindowInfo] = None

        if app_name:
            windows = self.actuator.get_windows(app_name)
            if windows:
                target_win = windows[0]

        if target_win is not None:
            # Use CoordinateAdapter projection against window geometry
            sx, sy = self.coordinate_adapter.to_screen_coordinates(norm_x, norm_y, target_win)
            return float(sx), float(sy)

        # Fallback to primary screen size
        sw, sh = self.actuator.get_screen_size()
        clamped_x = max(0.0, min(1.0, norm_x))
        clamped_y = max(0.0, min(1.0, norm_y))
        return float(math.floor(clamped_x * sw)), float(math.floor(clamped_y * sh))

    # =========================================================================
    # Helpers & Interruptible Delays
    # =========================================================================

    def _interruptible_sleep(self, duration_s: float, tick_s: float = 0.02) -> None:
        """Sleeps for duration_s in small tick slices, checking failsafe on each tick."""
        if duration_s <= 0.0 or self.zero_delay:
            self.actuator.check_failsafe()
            return

        deadline = time.time() + duration_s
        while time.time() < deadline:
            self.actuator.check_failsafe()
            remaining = deadline - time.time()
            time.sleep(min(tick_s, max(0.0, remaining)))
        self.actuator.check_failsafe()

    def _interpolate_str(self, text: str, params: Dict[str, Any]) -> str:
        """Applies ParameterEngine interpolation if text is a non-empty string."""
        if not text or not isinstance(text, str):
            return str(text) if text is not None else ""
        return self.parameter_engine.interpolate(text, runtime_params=params)

    @staticmethod
    def _normalize_action_name(action: Any) -> str:
        """Normalizes an ActionType enum, foreign enum, or string to lowercase action identifier."""
        val = getattr(action, "value", action)
        val_str = str(val).lower().strip()
        if "." in val_str:
            val_str = val_str.split(".")[-1]
        return val_str

    @staticmethod
    def _resolve_target_app(step: WorkflowStep, spec: WorkflowSpec) -> Optional[str]:
        """Extracts target application name or bundle ID from step or spec metadata."""
        target = getattr(step, "target", {}) or {}
        app = target.get("bundle_id") or target.get("app_name") or target.get("app")
        if app:
            return str(app)
        spec_target = getattr(spec, "target_app", {}) or {}
        spec_app = spec_target.get("bundle_id") or spec_target.get("app_name")
        return str(spec_app) if spec_app else None

    @staticmethod
    def _get_timing_val(step: WorkflowStep, key: str, default: int) -> int:
        """Extracts timing parameter from step.timing dictionary."""
        timing = getattr(step, "timing", {}) or {}
        if isinstance(timing, dict):
            return int(timing.get(key, default))
        if hasattr(timing, key):
            return int(getattr(timing, key))
        return default

    @staticmethod
    def _record_telemetry_safe(
        memory_engine: Optional[TaskMemoryEngine],
        workflow_id: str,
        status: str,
        duration: float,
        steps_completed: int,
        error_message: Optional[str],
        effective_params: Dict[str, Any],
    ) -> None:
        """Safely invokes TaskMemoryEngine.record_execution without letting DB errors disrupt caller."""
        if memory_engine is None or not hasattr(memory_engine, "record_execution"):
            return
        try:
            record = ExecutionRecord(
                workflow_id=workflow_id,
                status=status,
                duration_ms=int(duration * 1000),
                steps_completed=steps_completed,
                error_message=error_message,
                parameters_used=effective_params,
            )
            memory_engine.record_execution(record)
        except Exception as e:
            logger.warning("Could not record execution telemetry to TaskMemoryEngine: %s", e)
