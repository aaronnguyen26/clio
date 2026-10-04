"""Benchmark Execution Runner.

Belongs to Milestone M5 (Multi-Domain Autonomous Benchmark Suite).
Runs the end-to-end benchmark suite across Web, Desktop Notes, and Cross-Domain workflows,
validating completion, performance, and R6/R7 zero-disruption constraints.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any, Dict, List, Optional

from src.actuators.base import BaseActuator
from src.actuators.mock import MockActuator
from src.actuators.virtual_cursor import VirtualCursor
from src.benchmark.scenarios import (
    build_cross_domain_workflow,
    build_notes_productivity_workflow,
    build_web_automation_workflow,
)
from src.benchmark.verifier import BenchmarkCompletionVerifier
from src.executor.events import ExecutionEventBus
from src.executor.executor import AutonomousWorkflowExecutor, ExecutionResult
from src.memory.models import WorkflowSpec


@dataclass
class BenchmarkReport:
    """Detailed results from a single benchmark run."""

    scenario_name: str
    workflow_id: str
    success: bool
    steps_completed: int
    total_steps: int
    duration_seconds: float
    verified: bool
    r6_compliant: bool
    error: Optional[str] = None
    telemetry: Dict[str, Any] = field(default_factory=dict)


class BenchmarkRunner:
    """Automated benchmark test runner executing multi-domain evaluation flows."""

    def __init__(
        self,
        actuator: Optional[BaseActuator] = None,
        bus: Optional[ExecutionEventBus] = None,
        virtual_cursor: Optional[VirtualCursor] = None,
        zero_delay: bool = True,
    ) -> None:
        self.actuator = actuator or MockActuator()
        self.bus = bus or ExecutionEventBus()
        self.virtual_cursor = virtual_cursor or VirtualCursor(initial_x=0.0, initial_y=0.0, mock=True)
        self.zero_delay = zero_delay
        self.executor = AutonomousWorkflowExecutor(
            actuator=self.actuator,
            bus=self.bus,
            virtual_cursor=self.virtual_cursor,
            zero_delay=self.zero_delay,
        )

    def run_notes_benchmark(self) -> BenchmarkReport:
        """Executes the Apple Notes weekly to-do list benchmark."""
        spec = build_notes_productivity_workflow()
        t0 = time.perf_counter()
        result = self.executor.execute_workflow(spec)
        elapsed = time.perf_counter() - t0

        verified = BenchmarkCompletionVerifier.verify_notes(self.actuator)
        r6_compliant = BenchmarkCompletionVerifier.verify_r6_background_invariants(self.actuator)

        return BenchmarkReport(
            scenario_name="Native Desktop Productivity (Apple Notes)",
            workflow_id=spec.id,
            success=result.success,
            steps_completed=result.steps_completed,
            total_steps=result.total_steps,
            duration_seconds=elapsed,
            verified=verified,
            r6_compliant=r6_compliant,
            error=result.error,
            telemetry=result.telemetry,
        )

    def run_web_benchmark(self) -> BenchmarkReport:
        """Executes the Web Browser automation benchmark."""
        spec = build_web_automation_workflow()
        t0 = time.perf_counter()
        result = self.executor.execute_workflow(spec)
        elapsed = time.perf_counter() - t0

        verified = BenchmarkCompletionVerifier.verify_web(self.actuator)
        r6_compliant = BenchmarkCompletionVerifier.verify_r6_background_invariants(self.actuator)

        return BenchmarkReport(
            scenario_name="Web Browser Automation",
            workflow_id=spec.id,
            success=result.success,
            steps_completed=result.steps_completed,
            total_steps=result.total_steps,
            duration_seconds=elapsed,
            verified=verified,
            r6_compliant=r6_compliant,
            error=result.error,
            telemetry=result.telemetry,
        )

    def run_cross_domain_benchmark(self) -> BenchmarkReport:
        """Executes the Cross-Domain Web <-> Desktop workflow benchmark."""
        spec = build_cross_domain_workflow()
        t0 = time.perf_counter()
        result = self.executor.execute_workflow(spec)
        elapsed = time.perf_counter() - t0

        verified = BenchmarkCompletionVerifier.verify_cross_domain(self.actuator)
        r6_compliant = BenchmarkCompletionVerifier.verify_r6_background_invariants(self.actuator)

        return BenchmarkReport(
            scenario_name="Cross-Domain Web <-> Desktop Transfer",
            workflow_id=spec.id,
            success=result.success,
            steps_completed=result.steps_completed,
            total_steps=result.total_steps,
            duration_seconds=elapsed,
            verified=verified,
            r6_compliant=r6_compliant,
            error=result.error,
            telemetry=result.telemetry,
        )

    def run_all(self) -> List[BenchmarkReport]:
        """Runs all 3 benchmark scenarios in sequence and returns consolidated report."""
        return [
            self.run_notes_benchmark(),
            self.run_web_benchmark(),
            self.run_cross_domain_benchmark(),
        ]
