"""Unit tests for Multi-Domain Autonomous Benchmark Suite (Milestone 5 / Requirement R5).

Tests validate:
- Canonical benchmark recipes (Notes, Web Browser, Cross-Domain)
- BenchmarkCompletionVerifier verification logic for all 3 scenarios
- BenchmarkRunner automated execution and consolidated reporting
- Zero-disruption background execution (Requirement R6) and Virtual Cursor compatibility (Requirement R7)
"""

from __future__ import annotations

import pytest

from src.actuators.mock import MockActuator
from src.benchmark.runner import BenchmarkReport, BenchmarkRunner
from src.benchmark.scenarios import (
    build_cross_domain_workflow,
    build_notes_productivity_workflow,
    build_web_automation_workflow,
)
from src.benchmark.verifier import BenchmarkCompletionVerifier
from src.memory.models import ActionType


class TestBenchmarkScenarios:
    """Validates structural contracts of all 3 canonical benchmark workflows."""

    def test_notes_productivity_recipe_structure(self) -> None:
        """Notes benchmark includes app launch, focus, Cmd+N hotkey, and formatted to-do checklist."""
        spec = build_notes_productivity_workflow()
        assert spec.id == "wf_notes_weekly_todo"
        assert len(spec.steps) == 4
        actions = [s.action for s in spec.steps]
        assert actions == [
            ActionType.LAUNCH_APP,
            ActionType.FOCUS_APP,
            ActionType.PRESS_HOTKEY,
            ActionType.PASTE_TEXT,
        ]
        paste_text = spec.steps[3].payload["text"]
        assert "[ ] Monday:" in paste_text
        assert "[ ] Friday:" in paste_text
        assert "Autonomous Desktop Companion" in paste_text

    def test_web_automation_recipe_structure(self) -> None:
        """Web automation benchmark includes browser launch, focus, URL navigation, and scrolling."""
        spec = build_web_automation_workflow(target_url="https://news.ycombinator.com")
        assert spec.id == "wf_web_browser_automation"
        assert len(spec.steps) == 4
        actions = [s.action for s in spec.steps]
        assert actions == [
            ActionType.LAUNCH_APP,
            ActionType.FOCUS_APP,
            ActionType.OPEN_URL,
            ActionType.SCROLL,
        ]
        assert spec.steps[2].payload["url"] == "https://news.ycombinator.com"
        assert spec.steps[3].payload["dy"] == -300

    def test_cross_domain_recipe_structure(self) -> None:
        """Cross-domain benchmark coordinates web information extraction to desktop document."""
        spec = build_cross_domain_workflow()
        assert spec.id == "wf_cross_domain_web_to_desktop"
        assert len(spec.steps) == 5
        actions = [s.action for s in spec.steps]
        assert actions == [
            ActionType.OPEN_URL,
            ActionType.LAUNCH_APP,
            ActionType.FOCUS_APP,
            ActionType.PRESS_HOTKEY,
            ActionType.PASTE_TEXT,
        ]
        assert "wikipedia.org" in spec.steps[0].payload["url"]
        assert "Autonomous Agent" in spec.steps[4].payload["text"]


class TestBenchmarkCompletionVerifier:
    """Validates verification predicates across all scenarios."""

    def test_notes_verifier_success_and_failure(self) -> None:
        """Verifier correctly distinguishes complete vs incomplete Notes execution."""
        actuator = MockActuator()
        assert BenchmarkCompletionVerifier.verify_notes(actuator) is False

        # Simulate execution
        actuator.launch_app("com.apple.Notes")
        actuator.press_hotkey("cmd", "n")
        actuator.paste_text("Weekly Action Plan for the team")
        assert BenchmarkCompletionVerifier.verify_notes(actuator) is True

    def test_web_verifier_success_and_failure(self) -> None:
        """Verifier correctly distinguishes complete vs incomplete Web execution."""
        actuator = MockActuator()
        assert BenchmarkCompletionVerifier.verify_web(actuator) is False

        # Simulate execution
        actuator.launch_app("com.apple.Safari")
        actuator.open_url("https://news.ycombinator.com")
        actuator.scroll(0, -300)
        assert BenchmarkCompletionVerifier.verify_web(actuator) is True

    def test_cross_domain_verifier_success_and_failure(self) -> None:
        """Verifier correctly distinguishes complete vs incomplete Cross-Domain execution."""
        actuator = MockActuator()
        assert BenchmarkCompletionVerifier.verify_cross_domain(actuator) is False

        # Simulate execution
        actuator.open_url("https://en.wikipedia.org/wiki/Autonomous_agent")
        actuator.launch_app("com.apple.Notes")
        actuator.press_hotkey("cmd", "n")
        actuator.paste_text("Autonomous Agent Research Summary")
        assert BenchmarkCompletionVerifier.verify_cross_domain(actuator) is True


class TestBenchmarkRunnerExecution:
    """Executes all benchmarks end-to-end through BenchmarkRunner."""

    def test_run_notes_benchmark(self) -> None:
        """Notes benchmark executes hands-free with 100% verification."""
        runner = BenchmarkRunner(zero_delay=True)
        report = runner.run_notes_benchmark()

        assert report.success is True
        assert report.verified is True
        assert report.r6_compliant is True
        assert report.steps_completed == 4
        assert report.error is None

    def test_run_web_benchmark(self) -> None:
        """Web benchmark executes hands-free with 100% verification."""
        runner = BenchmarkRunner(zero_delay=True)
        report = runner.run_web_benchmark()

        assert report.success is True
        assert report.verified is True
        assert report.r6_compliant is True
        assert report.steps_completed == 4
        assert report.error is None

    def test_run_cross_domain_benchmark(self) -> None:
        """Cross-domain benchmark coordinates web and desktop seamlessly."""
        runner = BenchmarkRunner(zero_delay=True)
        report = runner.run_cross_domain_benchmark()

        assert report.success is True
        assert report.verified is True
        assert report.r6_compliant is True
        assert report.steps_completed == 5
        assert report.error is None

    def test_run_all_consolidated_suite(self) -> None:
        """Comprehensive execution of all 3 benchmark scenarios in sequence."""
        runner = BenchmarkRunner(zero_delay=True)
        reports = runner.run_all()

        assert len(reports) == 3
        for rep in reports:
            assert rep.success is True
            assert rep.verified is True
            assert rep.r6_compliant is True
            assert rep.error is None
            assert rep.duration_seconds >= 0.0

    def test_r6_background_invariants_rejection(self) -> None:
        """TEST-BM-01 (VULN-BM-01): verify_r6_background_invariants rejects illegal warp_mouse actions."""
        actuator = MockActuator()
        actuator.launch_app("com.apple.Notes")
        assert BenchmarkCompletionVerifier.verify_r6_background_invariants(actuator) is True

        from src.actuators.types import ActuatedAction
        actuator._history.append(ActuatedAction(action_type="warp_mouse", parameters={}, timestamp=1.0))
        assert BenchmarkCompletionVerifier.verify_r6_background_invariants(actuator) is False

    def test_cross_domain_custom_extracted_text(self) -> None:
        """TEST-BM-02 (VULN-BM-02): Dynamic research text is properly transferred in cross-domain recipe."""
        custom_text = "Custom Autonomous Research Summary"
        spec = build_cross_domain_workflow(extracted_text=custom_text)
        assert spec.steps[4].payload["text"] == custom_text
