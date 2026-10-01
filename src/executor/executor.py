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
import threading
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
        if virtual_cursor is None and use_virtual_cursor:
            is_mock = getattr(actuator, "mode", None) != "macos"
            self.virtual_cursor = VirtualCursor(initial_x=550.0, initial_y=350.0, mock=is_mock)
        else:
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
        self._cancel_requested = threading.Event()

    def cancel(self) -> None:
        """Signals running workflow execution to halt immediately."""
        self._cancel_requested.set()
        if self.virtual_cursor is not None and hasattr(self.virtual_cursor, "_cancel_event"):
            self.virtual_cursor._cancel_event.set()

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
        if self.virtual_cursor is not None:
            cur_x = getattr(self.virtual_cursor, "_vx", 550.0)
            cur_y = getattr(self.virtual_cursor, "_vy", 350.0)
            if cur_x <= 0.0 or cur_y <= 0.0:
                cur_x, cur_y = 550.0, 350.0
            self.virtual_cursor.move_to(cur_x, cur_y, duration=0.15, smooth=True)

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

        self._cancel_requested.clear()
        if self.virtual_cursor is not None and hasattr(self.virtual_cursor, "_cancel_event"):
            self.virtual_cursor._cancel_event.clear()

        try:
            # Enforce failsafe check before starting any steps
            self.actuator.check_failsafe()

            for idx, step in enumerate(steps, 1):
                if self._cancel_requested.is_set():
                    raise ActuatorError("Workflow execution cancelled by user request.")
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
        import threading
        if threading.current_thread() is not threading.main_thread():
            # AXUIElementCopyElementAtPosition causes SIGSEGV on macOS worker threads
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
            if not sys_elem:
                return None
            elem = c_void_p()
            rc = hiservices.AXUIElementCopyElementAtPosition(sys_elem, c_float(x), c_float(y), byref(elem))
            cf.CFRelease(sys_elem)
            if rc != 0 or not elem.value:
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
            # PHYSICAL CURSOR EXECUTION:
            # First, check if the app exists in the macOS Dock
            dock_info = self._find_dock_item(app_id)
            coords = self._resolve_optional_screen_coordinates(step, spec, effective_params)

            if dock_info:
                dock_x, dock_y, dock_title = dock_info
                logger.info(
                    "CURSOR LAUNCH: Moving cursor to Dock icon '%s' at (%.1f, %.1f) and clicking",
                    dock_title,
                    dock_x,
                    dock_y,
                )
                if step_use_vc and self.virtual_cursor is not None:
                    self.virtual_cursor.move_to(dock_x, dock_y, duration=0.45, smooth=True)
                    self.virtual_cursor.click(x=dock_x, y=dock_y, button="left", click_count=1)
                elif not background_mode:
                    self.actuator.move_mouse(dock_x, dock_y, smooth=True, duration=0.35)
                    self.actuator.click(x=dock_x, y=dock_y, button=MouseButton.LEFT, click_count=1)

                # Launch target application (in background if background_mode is active)
                try:
                    self.actuator.launch_app(app_id, timeout=timeout_s, background=background_mode)
                except TypeError:
                    self.actuator.launch_app(app_id, timeout=timeout_s)

            elif coords is not None:
                # App was recorded at specific screen coordinates (e.g. Dock or Desktop icon)
                sx, sy = coords
                logger.info(
                    "CURSOR LAUNCH: Moving cursor to recorded coordinates (%.1f, %.1f) and clicking to launch",
                    sx,
                    sy,
                )
                if step_use_vc and self.virtual_cursor is not None:
                    self.virtual_cursor.move_to(sx, sy, duration=0.45, smooth=True)
                    self.virtual_cursor.click(x=sx, y=sy, button="left", click_count=1)
                elif not background_mode:
                    self.actuator.move_mouse(sx, sy, smooth=True, duration=0.35)
                    self.actuator.click(x=sx, y=sy, button=MouseButton.LEFT, click_count=1)
                try:
                    self.actuator.launch_app(app_id, timeout=timeout_s, background=background_mode)
                except TypeError:
                    self.actuator.launch_app(app_id, timeout=timeout_s)

            else:
                # App not in Dock: use Spotlight only if NOT in background mode and VC is used
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
                elif background_mode and self.virtual_cursor is not None and step_use_vc:
                    self.virtual_cursor.move_to(550.0, 350.0, duration=0.3, smooth=True)

                try:
                    self.actuator.launch_app(app_id, timeout=timeout_s, background=background_mode)
                except TypeError:
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

            # Move cursor smoothly into the launched window
            if not background_mode:
                win = None
                for _ in range(15):
                    windows = self.actuator.get_windows(app_id)
                    if windows:
                        win = windows[0]
                        break
                    time.sleep(0.1)

                if win:
                    target_x = max(100.0, win.x + min(250.0, win.width / 2))
                    target_y = max(80.0, win.y + min(120.0, win.height / 3))
                    if step_use_vc and self.virtual_cursor is not None:
                        if hasattr(self.virtual_cursor, "set_target_bundle_id"):
                            self.virtual_cursor.set_target_bundle_id(app_id)
                        if hasattr(self.virtual_cursor, "set_target_window"):
                            self.virtual_cursor.set_target_window(win)
                        self.virtual_cursor.move_to(target_x, target_y, duration=0.35, smooth=True)
                    else:
                        self.actuator.move_mouse(target_x, target_y, smooth=True, duration=0.30)

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

            # Target specific window if multi-window context exists (Intra-App Window Switching)
            target_win_bounds = target.get("window_bounds") or payload.get("window_bounds")
            windows = self.actuator.get_windows(app_id)
            target_win = None
            if windows:
                if target_win_bounds:
                    for w in windows:
                        if abs(w.x - target_win_bounds.get("x", 0)) < 120 and abs(w.y - target_win_bounds.get("y", 0)) < 120:
                            target_win = w
                            break
                if not target_win:
                    target_win = windows[0]

            if target_win and not background_mode:
                target_x = max(100.0, target_win.x + min(250.0, target_win.width / 2))
                target_y = max(60.0, target_win.y + min(25.0, target_win.height / 10))
                if step_use_vc and self.virtual_cursor is not None:
                    if hasattr(self.virtual_cursor, "set_target_bundle_id"):
                        self.virtual_cursor.set_target_bundle_id(app_id)
                    if hasattr(self.virtual_cursor, "set_target_window"):
                        self.virtual_cursor.set_target_window(target_win)
                    self.virtual_cursor.move_to(target_x, target_y, duration=0.3, smooth=True)
                else:
                    self.actuator.move_mouse(target_x, target_y, smooth=True, duration=0.25)
                    self.actuator.click(target_x, target_y, button=MouseButton.LEFT, click_count=1)

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

            dwell_s = float(payload.get("dwell_s", 0.0))
            if dwell_s > 0:
                self._interruptible_sleep(dwell_s)

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

            # 1. Check if this click targets an item in the macOS Dock
            dock_app = self._detect_dock_app_at(sx, sy)
            if not dock_app and (target.get("launch_method") == "dock" or target.get("bundle_id") == "com.apple.dock"):
                dock_app = target.get("app_name") or payload.get("app")

            if dock_app:
                dock_item = self._find_dock_item(dock_app)
                if dock_item:
                    sx, sy, _ = dock_item
                logger.info("Click at (%.1f, %.1f) identified as Dock icon for '%s' — launching app", sx, sy, dock_app)
                if step_use_vc and self.virtual_cursor is not None:
                    self.virtual_cursor.move_to(sx, sy, duration=0.35, smooth=True)
                    self.virtual_cursor.click(x=sx, y=sy, button=btn, click_count=click_count)
                else:
                    self.actuator.move_mouse(sx, sy, smooth=True, duration=0.30)
                    self.actuator.click(x=sx, y=sy, button=MouseButton(btn), click_count=click_count)

                # Launch clicked application (honoring background_mode)
                try:
                    self.actuator.launch_app(dock_app, background=background_mode)
                except TypeError:
                    self.actuator.launch_app(dock_app)
                if not background_mode:
                    self.actuator.focus_app(dock_app)
                if step_use_vc and self.virtual_cursor is not None:
                    self.virtual_cursor.set_target_bundle_id(dock_app)
                    windows = self.actuator.get_windows(dock_app)
                    if windows:
                        win = windows[0]
                        self.virtual_cursor.move_to(win.x + win.width / 2, win.y + win.height / 3, duration=0.35, smooth=True)
                return

            # 2. Standard Click (Virtual Cursor vs Physical Cursor)
            if step_use_vc and self.virtual_cursor is not None:
                target_bundle = target.get("bundle_id") or payload.get("bundle_id")
                if target_bundle and hasattr(self.virtual_cursor, "set_target_bundle_id"):
                    self.virtual_cursor.set_target_bundle_id(target_bundle)
                self.virtual_cursor.move_to(sx, sy, duration=0.25, smooth=True)
                self.virtual_cursor.click(x=sx, y=sy, button=btn, click_count=click_count)
            else:
                self.actuator.move_mouse(sx, sy, smooth=True, duration=0.20)
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
                sx, sy = 400.0, 300.0

            if step_use_vc and self.virtual_cursor is not None:
                self.virtual_cursor.move_to(sx, sy, duration=0.25, smooth=True)
                self.virtual_cursor.click(x=sx, y=sy, button=btn, click_count=2)
            else:
                self.actuator.move_mouse(sx, sy, smooth=True, duration=0.20)
                self.actuator.click(x=sx, y=sy, button=MouseButton(btn), click_count=2)

        # ---------------------------------------------------------------------
        # 6. RIGHT_CLICK
        # ---------------------------------------------------------------------
        elif action_name == "right_click":
            coords = self._resolve_optional_screen_coordinates(step, spec, effective_params)
            if coords is not None:
                sx, sy = coords
            else:
                sx, sy = 400.0, 300.0

            if step_use_vc and self.virtual_cursor is not None:
                self.virtual_cursor.move_to(sx, sy, duration=0.25, smooth=True)
                self.virtual_cursor.click(x=sx, y=sy, button="right", click_count=1)
            else:
                self.actuator.move_mouse(sx, sy, smooth=True, duration=0.20)
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
                sx = float(payload.get("start_x", 0.0) or target.get("start_x", 0.0))
                sy = float(payload.get("start_y", 0.0) or target.get("start_y", 0.0))
                ex = float(payload.get("end_x", sx) or target.get("screen_x", sx))
                ey = float(payload.get("end_y", sy) or target.get("screen_y", sy))

            # Adapt start and end drag coordinates if target window moved or resized
            rec_wb = (
                target.get("window_bounds") if isinstance(target, dict)
                else (payload.get("window_bounds") if isinstance(payload, dict) else None)
            )
            if rec_wb and isinstance(rec_wb, dict):
                app_name = self._resolve_target_app(step, spec)
                if app_name:
                    windows = self.actuator.get_windows(app_name)
                    if windows:
                        matching_win = self._find_best_matching_window(windows, rec_wb)
                        if matching_win:
                            sx, sy = self._adapt_coordinates_to_window(
                                sx, sy, rec_wb, matching_win, {"norm_x": target.get("start_norm_x"), "norm_y": target.get("start_norm_y")}
                            )
                            ex, ey = self._adapt_coordinates_to_window(
                                ex, ey, rec_wb, matching_win, {"norm_x": target.get("norm_x"), "norm_y": target.get("norm_y")}
                            )

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

            target_bundle = (
                target.get("bundle_id")
                or target.get("app_name")
                or payload.get("bundle_id")
                or payload.get("app")
                or (spec.target_app.get("bundle_id") if isinstance(getattr(spec, "target_app", None), dict) else "")
            )
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
            target_bundle = (
                target.get("bundle_id")
                or target.get("app_name")
                or payload.get("bundle_id")
                or payload.get("app")
                or (spec.target_app.get("bundle_id") if isinstance(getattr(spec, "target_app", None), dict) else "")
            )
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
                raw_key = payload.get("key") or payload.get("hotkey", "")
                if "+" in raw_key:
                    keys = [k.strip() for k in raw_key.split("+")]
                elif raw_key:
                    keys = [raw_key]
                else:
                    keys = []

            target_bundle = (
                target.get("bundle_id")
                or target.get("app_name")
                or payload.get("bundle_id")
                or payload.get("app")
                or (spec.target_app.get("bundle_id") if isinstance(getattr(spec, "target_app", None), dict) else "")
            )
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

        # 0. Check for explicit recorded screen coordinates (100% pixel-perfect fidelity)
        recorded_sx: Optional[float] = None
        recorded_sy: Optional[float] = None
        if isinstance(target, dict):
            if "screen_x" in target and "screen_y" in target:
                recorded_sx, recorded_sy = float(target["screen_x"]), float(target["screen_y"])
            elif "x" in target and "y" in target:
                recorded_sx, recorded_sy = float(target["x"]), float(target["y"])
        if recorded_sx is None and isinstance(payload, dict):
            if "screen_x" in payload and "screen_y" in payload:
                recorded_sx, recorded_sy = float(payload["screen_x"]), float(payload["screen_y"])
            elif "x" in payload and "y" in payload:
                recorded_sx, recorded_sy = float(payload["x"]), float(payload["y"])
        if recorded_sx is None and step_coords is not None:
            if hasattr(step_coords, "abs_x") and step_coords.abs_x is not None and step_coords.abs_y is not None:
                recorded_sx, recorded_sy = float(step_coords.abs_x), float(step_coords.abs_y)

        if recorded_sx is not None and recorded_sy is not None:
            # If window bounds were recorded, check if the target window has moved or resized
            rec_wb = (
                target.get("window_bounds") if isinstance(target, dict)
                else (payload.get("window_bounds") if isinstance(payload, dict) else None)
            )
            if rec_wb and isinstance(rec_wb, dict):
                app_name = self._resolve_target_app(step, spec)
                if app_name:
                    windows = self.actuator.get_windows(app_name)
                    if windows:
                        matching_win = self._find_best_matching_window(windows, rec_wb)
                        if matching_win:
                            return self._adapt_coordinates_to_window(
                                recorded_sx, recorded_sy, rec_wb, matching_win, target if isinstance(target, dict) else {}
                            )
            # Window is in the same position or action is screen-level (Dock, desktop, etc.): exact coordinate match
            return recorded_sx, recorded_sy

        # 1. Tri-Factor Target Anchor Resolution
        tri_factor = target.get("tri_factor_anchor") if isinstance(target, dict) else None
        if tri_factor and isinstance(tri_factor, dict):
            # Factor 1: Accessibility Query (AXRole + AXTitle + bundle_id)
            f1 = tri_factor.get("factor_1_ax", {})
            ax_role = f1.get("ax_role")
            ax_title = f1.get("ax_title")
            ax_bundle = f1.get("bundle_id") or self._resolve_target_app(step, spec)
            if ax_bundle and (ax_role or ax_title):
                ax_elem = self._query_accessibility_element(ax_bundle, ax_role, ax_title)
                if ax_elem is not None:
                    ax_x, ax_y, ax_w, ax_h = ax_elem
                    logger.info("Tri-Factor Anchor: Resolved Factor 1 (AX Query) for '%s' ('%s') at (%.1f, %.1f)", ax_title, ax_role, ax_x + ax_w / 2.0, ax_y + ax_h / 2.0)
                    return ax_x + ax_w / 2.0, ax_y + ax_h / 2.0

            # Factor 2: Window-Relative Ratio (norm_x, norm_y)
            f2 = tri_factor.get("factor_2_ratio", {})
            f2_nx = f2.get("norm_x")
            f2_ny = f2.get("norm_y")
            if f2_nx is not None and f2_ny is not None:
                logger.info("Tri-Factor Anchor: Resolved Factor 2 (Window Ratio) norm=(%.4f, %.4f)", float(f2_nx), float(f2_ny))
                return self._project_norm_to_screen(float(f2_nx), float(f2_ny), step, spec)

            # Factor 3: Clean Visual Crop
            f3 = tri_factor.get("factor_3_visual", {})
            crop_path = f3.get("crop_path")
            if crop_path and os.path.exists(crop_path):
                matched = self._match_visual_crop(crop_path, step, spec)
                if matched is not None:
                    logger.info("Tri-Factor Anchor: Resolved Factor 3 (Visual Crop) at (%.1f, %.1f)", matched[0], matched[1])
                    return matched

        # 2. Check step.target for norm_x / norm_y
        if isinstance(target, dict):
            if "norm_x" in target and "norm_y" in target:
                return self._project_norm_to_screen(float(target["norm_x"]), float(target["norm_y"]), step, spec)

        # 3. Coordinates embedded in payload dict: payload["coordinates"]
        raw_coords = payload.get("coordinates")
        if isinstance(raw_coords, dict):
            if "norm_x" in raw_coords and "norm_y" in raw_coords:
                return self._project_norm_to_screen(
                    float(raw_coords["norm_x"]),
                    float(raw_coords["norm_y"]),
                    step,
                    spec,
                )
            if "x" in raw_coords and "y" in raw_coords:
                return float(raw_coords["x"]), float(raw_coords["y"])
            if "abs_x" in raw_coords and "abs_y" in raw_coords:
                return float(raw_coords["abs_x"]), float(raw_coords["abs_y"])

        # 4. TargetCoordinates instance on step: step.coordinates
        if step_coords is not None:
            if hasattr(step_coords, "norm_x") and step_coords.norm_x is not None and step_coords.norm_y is not None:
                return self._project_norm_to_screen(
                    float(step_coords.norm_x),
                    float(step_coords.norm_y),
                    step,
                    spec,
                )

        # 5. Check norm_x and norm_y directly in payload
        if "norm_x" in payload and "norm_y" in payload:
            return self._project_norm_to_screen(
                float(payload["norm_x"]),
                float(payload["norm_y"]),
                step,
                spec,
            )

        return None

    def _find_best_matching_window(
        self,
        windows: List[WindowInfo],
        rec_wb: Dict[str, Any],
    ) -> Optional[WindowInfo]:
        """Finds the window in `windows` that best corresponds to `rec_wb`.

        Prevents false positive window shifts from popups, menus, inspectors, or unrelated windows.
        """
        if not windows or not isinstance(rec_wb, dict):
            return None

        rec_x = float(rec_wb.get("x", 0))
        rec_y = float(rec_wb.get("y", 0))
        rec_w = float(rec_wb.get("width", 0))
        rec_h = float(rec_wb.get("height", 0))

        if rec_w <= 10 or rec_h <= 10:
            return None

        # 1. Exact or near-identical position check: window has not moved
        for w in windows:
            if (
                abs(w.x - rec_x) <= 15
                and abs(w.y - rec_y) <= 15
                and abs(w.width - rec_w) <= 25
                and abs(w.height - rec_h) <= 25
            ):
                return w

        # 2. Window moved: find a candidate with compatible geometry
        best_candidate: Optional[WindowInfo] = None
        best_score = float("inf")

        for w in windows:
            # Reject windows that are vastly different in size (e.g. 150px popup vs 1400px window)
            w_diff = abs(w.width - rec_w)
            h_diff = abs(w.height - rec_h)
            max_w_allowed = max(150.0, 0.40 * rec_w)
            max_h_allowed = max(150.0, 0.40 * rec_h)

            if w_diff > max_w_allowed or h_diff > max_h_allowed:
                continue

            pos_diff = abs(w.x - rec_x) + abs(w.y - rec_y)
            size_diff = w_diff + h_diff
            score = (size_diff * 2.0) + pos_diff

            if score < best_score:
                best_score = score
                best_candidate = w

        return best_candidate

    def _adapt_coordinates_to_window(
        self,
        recorded_sx: float,
        recorded_sy: float,
        rec_wb: Dict[str, Any],
        best_win: WindowInfo,
        target: Dict[str, Any],
    ) -> Tuple[float, float]:
        """Adapts recorded screen coordinates when the target window has moved or resized."""
        rec_x = float(rec_wb.get("x", 0))
        rec_y = float(rec_wb.get("y", 0))
        rec_w = float(rec_wb.get("width", 0))
        rec_h = float(rec_wb.get("height", 0))

        dx = best_win.x - rec_x
        dy = best_win.y - rec_y
        dw = abs(best_win.width - rec_w)
        dh = abs(best_win.height - rec_h)

        # If window barely moved or resized (< 5px), keep exact recorded coordinates
        if abs(dx) <= 5 and abs(dy) <= 5 and dw <= 8 and dh <= 8:
            return recorded_sx, recorded_sy

        # If window only moved without significant resize, translate exact pixel offset
        if dw <= 20 and dh <= 20:
            return round(recorded_sx + dx, 1), round(recorded_sy + dy, 1)

        # Window was resized: handle fixed macOS window regions vs scaling content
        rel_x = recorded_sx - rec_x
        rel_y = recorded_sy - rec_y

        # Top toolbar / title bar (fixed height ~65px)
        if rel_y < 65:
            new_y = best_win.y + rel_y
        # Bottom status bar (fixed height ~30px)
        elif rel_y > rec_h - 30:
            new_y = best_win.y + best_win.height - (rec_h - rel_y)
        else:
            # Scaled vertically within content area
            norm_y = target.get("norm_y") if isinstance(target, dict) else None
            if norm_y is not None:
                new_y = best_win.y + (float(norm_y) * best_win.height)
            else:
                new_y = best_win.y + (rel_y / max(1.0, rec_h)) * best_win.height

        # Left sidebar (fixed width ~200px)
        if rel_x < 200:
            new_x = best_win.x + rel_x
        # Right inspector rail (fixed width ~200px)
        elif rel_x > rec_w - 200:
            new_x = best_win.x + best_win.width - (rec_w - rel_x)
        else:
            # Scaled horizontally within content area
            norm_x = target.get("norm_x") if isinstance(target, dict) else None
            if norm_x is not None:
                new_x = best_win.x + (float(norm_x) * best_win.width)
            else:
                new_x = best_win.x + (rel_x / max(1.0, rec_w)) * best_win.width

        return round(new_x, 1), round(new_y, 1)

    def _project_norm_to_screen(
        self,
        norm_x: float,
        norm_y: float,
        step: WorkflowStep,
        spec: WorkflowSpec,
    ) -> Tuple[float, float]:
        """Projects normalized (norm_x, norm_y) to absolute screen coordinates using target window or screen."""
        target = getattr(step, "target", {}) or {}
        payload = getattr(step, "payload", {}) or {}

        # Fallback 0: if explicit recorded screen_x and screen_y are present in target or payload, use them!
        if isinstance(target, dict) and "screen_x" in target and "screen_y" in target:
            return float(target["screen_x"]), float(target["screen_y"])
        if isinstance(payload, dict) and "screen_x" in payload and "screen_y" in payload:
            return float(payload["screen_x"]), float(payload["screen_y"])

        app_name = self._resolve_target_app(step, spec)
        target_win: Optional[WindowInfo] = None

        if app_name:
            windows = self.actuator.get_windows(app_name)
            if windows:
                rec_wb = (
                    target.get("window_bounds") if isinstance(target, dict)
                    else (payload.get("window_bounds") if isinstance(payload, dict) else None)
                )
                if rec_wb and isinstance(rec_wb, dict):
                    target_win = self._find_best_matching_window(windows, rec_wb)
                if target_win is None:
                    target_win = windows[0]

        if target_win is not None:
            # Use CoordinateAdapter projection against window geometry
            sx, sy = self.coordinate_adapter.to_screen_coordinates(norm_x, norm_y, target_win)
            return float(sx), float(sy)

        # Fallback 2: scale across primary screen size
        sw, sh = self.actuator.get_screen_size()
        clamped_x = max(0.0, min(1.0, norm_x))
        clamped_y = max(0.0, min(1.0, norm_y))
        return float(round(clamped_x * sw)), float(round(clamped_y * sh))

    def _query_accessibility_element(
        self,
        app_name_or_bundle: str,
        ax_role: Optional[str] = None,
        ax_title: Optional[str] = None,
    ) -> Optional[Tuple[float, float, float, float]]:
        """Queries on-screen accessibility element using actuator support or Darwin Accessibility."""
        # 1. Check if actuator has find_accessibility_element implemented (e.g. MockActuator)
        if hasattr(self.actuator, "find_accessibility_element"):
            elem = self.actuator.find_accessibility_element(app_name_or_bundle, ax_role, ax_title)
            if elem is not None:
                return elem

        # 2. Live macOS accessibility query via ctypes if on Darwin
        if sys.platform == "darwin" and threading.current_thread() is threading.main_thread():
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

                # Resolve PID
                pids_out = subprocess.check_output(["pgrep", "-f", app_name_or_bundle]).decode().strip().splitlines()
                if not pids_out:
                    return None
                pid = int(pids_out[0].strip())
                app_elem = hiservices.AXUIElementCreateApplication(pid)
                if not app_elem:
                    return None

                cf_windows = cf.CFStringCreateWithCString(None, b"AXWindows", 0x08000100)
                cf_children = cf.CFStringCreateWithCString(None, b"AXChildren", 0x08000100)
                cf_title = cf.CFStringCreateWithCString(None, b"AXTitle", 0x08000100)
                cf_role = cf.CFStringCreateWithCString(None, b"AXRole", 0x08000100)
                cf_pos = cf.CFStringCreateWithCString(None, b"AXPosition", 0x08000100)
                cf_size = cf.CFStringCreateWithCString(None, b"AXSize", 0x08000100)

                target_title_clean = (ax_title or "").lower().strip()
                target_role_clean = (ax_role or "").lower().strip()

                def _inspect_node(node: Any, depth: int = 0) -> Optional[Tuple[float, float, float, float]]:
                    if depth > 4:
                        return None
                    t_val = c_void_p()
                    t_str = ""
                    if hiservices.AXUIElementCopyAttributeValue(node, cf_title, byref(t_val)) == 0 and t_val.value:
                        buf = ctypes.create_string_buffer(256)
                        if cf.CFStringGetCString(t_val, buf, 256, 0x08000100):
                            t_str = buf.value.decode("utf-8").lower().strip()
                        cf.CFRelease(t_val)

                    r_val = c_void_p()
                    r_str = ""
                    if hiservices.AXUIElementCopyAttributeValue(node, cf_role, byref(r_val)) == 0 and r_val.value:
                        buf2 = ctypes.create_string_buffer(256)
                        if cf.CFStringGetCString(r_val, buf2, 256, 0x08000100):
                            r_str = buf2.value.decode("utf-8").lower().strip()
                        cf.CFRelease(r_val)

                    matches_title = (not target_title_clean) or (target_title_clean == t_str) or (target_title_clean in t_str)
                    matches_role = (not target_role_clean) or (target_role_clean == r_str)
                    if matches_title and matches_role and (t_str or r_str):
                        pos_v = c_void_p()
                        pt = CGPoint()
                        if hiservices.AXUIElementCopyAttributeValue(node, cf_pos, byref(pos_v)) == 0 and pos_v.value:
                            hiservices.AXValueGetValue(pos_v, 1, byref(pt))
                            cf.CFRelease(pos_v)

                        sz_v = c_void_p()
                        sz = CGSize()
                        if hiservices.AXUIElementCopyAttributeValue(node, cf_size, byref(sz_v)) == 0 and sz_v.value:
                            hiservices.AXValueGetValue(sz_v, 2, byref(sz))
                            cf.CFRelease(sz_v)

                        if sz.width > 0 and sz.height > 0:
                            return (pt.x, pt.y, sz.width, sz.height)

                    # Recurse children
                    c_val = c_void_p()
                    if hiservices.AXUIElementCopyAttributeValue(node, cf_children, byref(c_val)) == 0 and c_val.value:
                        cnt = cf.CFArrayGetCount(c_val)
                        res = None
                        for k in range(cnt):
                            child = cf.CFArrayGetValueAtIndex(c_val, k)
                            res = _inspect_node(child, depth + 1)
                            if res:
                                break
                        cf.CFRelease(c_val)
                        return res
                    return None

                result = _inspect_node(app_elem)
                cf.CFRelease(cf_windows)
                cf.CFRelease(cf_children)
                cf.CFRelease(cf_title)
                cf.CFRelease(cf_role)
                cf.CFRelease(cf_pos)
                cf.CFRelease(cf_size)
                cf.CFRelease(app_elem)
                return result
            except Exception as e:
                logger.debug("Live AX query error: %s", e)
        return None

    def _match_visual_crop(
        self,
        crop_path: str,
        step: WorkflowStep,
        spec: WorkflowSpec,
    ) -> Optional[Tuple[float, float]]:
        """Optional Factor 3 visual template matching fallback."""
        return None

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
