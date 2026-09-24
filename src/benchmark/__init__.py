"""Multi-Domain Autonomous Benchmark Subsystem.

Belongs to Milestone M5 (Multi-Domain Autonomous Benchmark Suite).
"""

from __future__ import annotations

from src.benchmark.scenarios import (
    build_cross_domain_workflow,
    build_notes_productivity_workflow,
    build_web_automation_workflow,
)
from src.benchmark.verifier import BenchmarkCompletionVerifier
from src.benchmark.runner import BenchmarkReport, BenchmarkRunner

__all__ = [
    "build_notes_productivity_workflow",
    "build_web_automation_workflow",
    "build_cross_domain_workflow",
    "BenchmarkCompletionVerifier",
    "BenchmarkReport",
    "BenchmarkRunner",
]
