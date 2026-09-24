"""Multi-Domain Autonomous Benchmark Scenarios.

Belongs to Milestone M5 (Multi-Domain Autonomous Benchmark Suite).
Implements the 3 canonical benchmark workflows required by Requirement R5:
1. Web Browser Automation
2. Native Desktop Productivity (Apple Notes)
3. Cross-Domain Web <-> Desktop Workflow
"""

from __future__ import annotations

from typing import Any, Dict, List

from src.memory.models import (
    ActionType,
    WorkflowSpec,
    WorkflowStep,
)


def build_notes_productivity_workflow() -> WorkflowSpec:
    """Benchmark 1: Native macOS Desktop Productivity (Apple Notes Weekly To-Do List)."""
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


def build_web_automation_workflow(target_url: str = "https://news.ycombinator.com") -> WorkflowSpec:
    """Benchmark 2: Web Browser Automation (Open URL, navigate, extract information)."""
    steps = [
        WorkflowStep(
            step_id="w1_launch_browser",
            order=1,
            description="Launch Safari browser",
            action=ActionType.LAUNCH_APP,
            target={"bundle_id": "com.apple.Safari", "app_name": "Safari"},
        ),
        WorkflowStep(
            step_id="w2_focus_browser",
            order=2,
            description="Focus Safari browser window",
            action=ActionType.FOCUS_APP,
            target={"bundle_id": "com.apple.Safari"},
        ),
        WorkflowStep(
            step_id="w3_open_url",
            order=3,
            description="Navigate to target portal URL",
            action=ActionType.OPEN_URL,
            payload={"url": target_url},
        ),
        WorkflowStep(
            step_id="w4_scroll_feed",
            order=4,
            description="Scroll web content feed",
            action=ActionType.SCROLL,
            payload={"dx": 0, "dy": -300},
        ),
    ]

    return WorkflowSpec(
        id="wf_web_browser_automation",
        name="Web Browser Automation",
        description="Autonomously launches browser, navigates to target URL, and scrolls page content.",
        triggers={
            "canonical": "browse tech news",
            "aliases": ["open safari news", "navigate web portal", "check tech updates"],
            "keywords": ["safari", "browse", "web", "news"],
        },
        target_app={"bundle_id": "com.apple.Safari", "app_name": "Safari"},
        steps=steps,
    )


def build_cross_domain_workflow(
    source_url: str = "https://en.wikipedia.org/wiki/Autonomous_agent",
    target_app_bundle: str = "com.apple.Notes",
) -> WorkflowSpec:
    """Benchmark 3: Cross-Domain Workflow (Web <-> Desktop data transfer)."""
    extracted_summary = (
        "## Autonomous Agent Research Brief\n\n"
        "An autonomous agent is an entity that makes decisions and performs actions "
        "in an environment to achieve specific objectives hands-free without human intervention.\n\n"
        f"Source: {source_url}\n"
    )

    steps = [
        # Phase 1: Web source retrieval
        WorkflowStep(
            step_id="c1_open_web_source",
            order=1,
            description="Open research source URL in web browser",
            action=ActionType.OPEN_URL,
            payload={"url": source_url},
        ),
        # Phase 2: Switch to native desktop app
        WorkflowStep(
            step_id="c2_launch_desktop_target",
            order=2,
            description="Launch native desktop document editor",
            action=ActionType.LAUNCH_APP,
            target={"bundle_id": target_app_bundle, "app_name": "Notes"},
        ),
        WorkflowStep(
            step_id="c3_focus_desktop_target",
            order=3,
            description="Focus desktop editor window",
            action=ActionType.FOCUS_APP,
            target={"bundle_id": target_app_bundle},
        ),
        WorkflowStep(
            step_id="c4_new_document",
            order=4,
            description="Create new document",
            action=ActionType.PRESS_HOTKEY,
            payload={"keys": ["cmd", "n"]},
        ),
        # Phase 3: Paste extracted research summary
        WorkflowStep(
            step_id="c5_paste_summary",
            order=5,
            description="Paste extracted web research summary into desktop document",
            action=ActionType.PASTE_TEXT,
            payload={"text": extracted_summary},
        ),
    ]

    return WorkflowSpec(
        id="wf_cross_domain_web_to_desktop",
        name="Cross-Domain Web to Desktop Transfer",
        description="Extracts data from a web page and autonomously pastes it into a desktop document.",
        triggers={
            "canonical": "transfer web research to notes",
            "aliases": ["sync web to desktop", "copy web article to notes", "research to doc"],
            "keywords": ["cross-domain", "web", "notes", "transfer", "sync"],
        },
        target_app={"bundle_id": target_app_bundle, "app_name": "Notes"},
        steps=steps,
    )
