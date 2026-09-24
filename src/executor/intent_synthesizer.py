"""Dynamic Intent Synthesizer & Universal System Controller for Clio.

Translates arbitrary user natural language instructions into closed-loop
executable WorkflowSpecs for:
1. Opening ANY application installed on macOS (via dynamic system app discovery).
2. Opening ANY browser tab or URL (in Safari, Google Chrome, Arc, Brave, Firefox, Edge).
3. Targeted on-screen virtual cursor interactions (moves, clicks).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple
import urllib.parse
import uuid

from src.memory.models import (
    ActionType,
    CoordMode,
    TargetCoordinates,
    WorkflowSpec,
    WorkflowStep,
)

logger = logging.getLogger(__name__)


class SystemAppRegistry:
    """Discovers and caches installed macOS applications across system and user domains."""

    _cached_apps: Optional[Dict[str, Dict[str, str]]] = None

    @classmethod
    def get_installed_apps(cls, force_refresh: bool = False) -> Dict[str, Dict[str, str]]:
        """Returns mapping: normalized_name -> {'name': DisplayName, 'path': /path/to.app, 'bundle_id': id}."""
        if cls._cached_apps is not None and not force_refresh:
            return cls._cached_apps

        apps: Dict[str, Dict[str, str]] = {}
        search_dirs = [
            "/Applications",
            "/Applications/Utilities",
            "/System/Applications",
            "/System/Applications/Utilities",
            os.path.expanduser("~/Applications"),
        ]

        for sdir in search_dirs:
            p = Path(sdir)
            if not p.exists():
                continue
            for app_path in p.glob("*.app"):
                clean_name = app_path.stem
                norm_key = clean_name.lower().strip()
                
                # Extract bundle ID if Info.plist exists
                bundle_id = ""
                plist_path = app_path / "Contents" / "Info.plist"
                if plist_path.exists():
                    try:
                        # Quick bundle id extraction via defaults/plutil or text grep
                        out = subprocess.check_output(
                            ["defaults", "read", str(plist_path), "CFBundleIdentifier"],
                            text=True,
                            stderr=subprocess.DEVNULL,
                            timeout=0.3,
                        ).strip()
                        bundle_id = out
                    except Exception:
                        pass

                apps[norm_key] = {
                    "name": clean_name,
                    "path": str(app_path),
                    "bundle_id": bundle_id,
                }

        # Add well-known synonyms & common abbreviations
        synonyms = {
            "chrome": "google chrome",
            "google chrome": "google chrome",
            "vscode": "visual studio code",
            "code": "visual studio code",
            "vs code": "visual studio code",
            "calc": "calculator",
            "calculator": "calculator",
            "settings": "system settings",
            "system settings": "system settings",
            "preferences": "system settings",
            "system preferences": "system settings",
            "term": "terminal",
            "terminal": "terminal",
            "iterm": "iterm",
            "iterm2": "iterm",
            "safari": "safari",
            "notes": "notes",
            "music": "music",
            "spotify": "spotify",
            "slack": "slack",
            "messages": "messages",
            "imessage": "messages",
            "mail": "mail",
            "photos": "photos",
            "calendar": "calendar",
            "reminders": "reminders",
            "finder": "finder",
            "files": "finder",
            "cursor": "cursor",
            "xcode": "xcode",
            "preview": "preview",
            "app store": "app store",
            "activity monitor": "activity monitor",
        }

        for alias, target in synonyms.items():
            if target in apps and alias not in apps:
                apps[alias] = apps[target]

        cls._cached_apps = apps
        return apps

    @classmethod
    def resolve_app(cls, query: str) -> Optional[Dict[str, str]]:
        """Resolves an app query string to application metadata."""
        apps = cls.get_installed_apps()
        clean = query.lower().strip()
        # Remove filler words
        clean = re.sub(r"^(?:the\s+)?", "", clean)
        clean = re.sub(r"\s+app$", "", clean)

        # 1. Exact match
        if clean in apps:
            return apps[clean]

        # 2. Singular / Plural match (e.g. "note" -> "notes", "calculators" -> "calculator")
        if clean + "s" in apps:
            return apps[clean + "s"]
        if clean.endswith("s") and clean[:-1] in apps:
            return apps[clean[:-1]]

        # 3. Whole word boundary match (e.g. "chrome" in "google chrome", "code" in "visual studio code")
        for key, info in apps.items():
            if clean in key.split():
                return info

        # 4. Prefix match ONLY if query is at least 4 characters long (e.g. "calc" -> "calculator", "term" -> "terminal")
        if len(clean) >= 4:
            for key, info in apps.items():
                if key.startswith(clean):
                    return info

        return None


class DynamicIntentSynthesizer:
    """Parses natural language queries and builds on-the-fly WorkflowSpecs."""

    WEB_DESTINATIONS = {
        "yt": "https://www.youtube.com",
        "youtube": "https://www.youtube.com",
        "google": "https://www.google.com",
        "github": "https://www.github.com",
        "gh": "https://www.github.com",
        "twitter": "https://www.x.com",
        "x": "https://www.x.com",
        "reddit": "https://www.reddit.com",
        "gmail": "https://mail.google.com",
        "netflix": "https://www.netflix.com",
        "amazon": "https://www.amazon.com",
        "linkedin": "https://www.linkedin.com",
        "chatgpt": "https://chatgpt.com",
        "claude": "https://claude.ai",
        "perplexity": "https://www.perplexity.ai",
    }

    @classmethod
    def is_browser_tab_intent(cls, query: str) -> bool:
        """Determines if query represents opening a browser tab or URL."""
        q = re.sub(r"\s+", " ", query.strip().lower())
        if re.search(r"^(?:open|new|create)\s+(?:a\s+)?(?:new\s+)?tab", q):
            return True
        if re.search(r"^(?:open|go to|navigate to)\s+(?:https?://|[a-zA-Z0-9\-]+\.[a-zA-Z]{2,})", q):
            return True
        if re.search(r"(?:in\s+(?:a\s+)?(?:new\s+)?tab)$", q):
            return True
        # Match web destination keywords like "open yt", "open youtube", "open github"
        m = re.match(r"^(?:open|go to|navigate to)\s+([a-zA-Z0-9\-]+)$", q)
        if m and m.group(1) in cls.WEB_DESTINATIONS:
            return True
        return False

    @staticmethod
    def is_app_open_intent(query: str) -> bool:
        """Determines if query is requesting to open/launch an application."""
        q = query.strip().lower()
        return bool(re.match(r"^(?:open|launch|start|focus|switch to)\s+", q))

    @classmethod
    def synthesize_browser_tab_workflow(cls, query: str) -> WorkflowSpec:
        """Builds a WorkflowSpec to open any browser tab or navigate to a URL."""
        q = re.sub(r"\s+", " ", query.strip())
        q_lower = q.lower()

        # Extract URL or destination if present
        target_url = ""
        # Match "open tab <url>", "open <url>", "new tab <url>"
        url_match = re.search(r"(?:open|go to|navigate to|tab\s+(?:to\s+)?)\s*(https?://\S+|[a-zA-Z0-9\-]+\.[a-zA-Z]{2,}[/\S]*)", q, re.IGNORECASE)
        if url_match:
            raw_url = url_match.group(1).strip()
            if not raw_url.startswith("http://") and not raw_url.startswith("https://"):
                target_url = "https://" + raw_url
            else:
                target_url = raw_url
        else:
            # Check web destination shortcuts
            m = re.match(r"^(?:open|go to|navigate to)\s+([a-zA-Z0-9\-]+)$", q_lower)
            if m and m.group(1) in cls.WEB_DESTINATIONS:
                target_url = cls.WEB_DESTINATIONS[m.group(1)]
            else:
                for k, dest_url in cls.WEB_DESTINATIONS.items():
                    if k in q_lower.split():
                        target_url = dest_url
                        break

        spec_id = f"dynamic_tab_{uuid.uuid4().hex[:8]}"
        steps: List[WorkflowStep] = []

        # Detect preferred browser (check Chrome, Arc, Safari)
        browser_app = "Safari"
        apps = SystemAppRegistry.get_installed_apps()
        if "google chrome" in apps:
            browser_app = "Google Chrome"
        elif "company.thebrowser.browser" in [v.get("bundle_id") for v in apps.values()]:
            browser_app = "Arc"

        # Step 1: Move Clio's virtual cursor gracefully to screen upper-center
        steps.append(
            WorkflowStep(
                step_id="step_cursor_motion",
                order=1,
                description="Move Clio virtual cursor into position",
                action=ActionType.MOVE_MOUSE,
                coordinates=TargetCoordinates(mode=CoordMode.SCREEN_ABSOLUTE, abs_x=640, abs_y=80),
                timing={"pre_delay_ms": 50, "post_delay_ms": 100},
            )
        )

        if target_url:
            # Step 2: Open target URL directly in a new tab
            steps.append(
                WorkflowStep(
                    step_id="step_open_url_tab",
                    order=2,
                    description=f"Open tab at {target_url}",
                    action=ActionType.OPEN_URL,
                    payload={"url": target_url},
                    timing={"pre_delay_ms": 100, "post_delay_ms": 300},
                )
            )
            name = f"Open Tab: {urllib.parse.urlparse(target_url).netloc or target_url}"
            desc = f"Opens a new browser tab navigating directly to {target_url}."
        else:
            # Step 2: Focus browser
            steps.append(
                WorkflowStep(
                    step_id="step_focus_browser",
                    order=2,
                    description=f"Focus browser ({browser_app})",
                    action=ActionType.FOCUS_APP,
                    target={"app_name": browser_app},
                    timing={"pre_delay_ms": 50, "post_delay_ms": 200},
                )
            )
            # Step 3: Press Cmd+T to open new tab
            steps.append(
                WorkflowStep(
                    step_id="step_new_tab_hotkey",
                    order=3,
                    description="Open new tab via Cmd+T",
                    action=ActionType.PRESS_HOTKEY,
                    payload={"keys": ["cmd", "t"]},
                    timing={"pre_delay_ms": 50, "post_delay_ms": 200},
                )
            )
            name = "Open New Browser Tab"
            desc = "Opens a blank new tab in your default browser."

        # Final Step: Virtual cursor click/hover in tab space to confirm focus
        steps.append(
            WorkflowStep(
                step_id="step_cursor_confirm",
                order=len(steps) + 1,
                description="Clio confirms new tab focus",
                action=ActionType.CLICK,
                coordinates=TargetCoordinates(mode=CoordMode.SCREEN_ABSOLUTE, abs_x=640, abs_y=180),
                timing={"pre_delay_ms": 150, "post_delay_ms": 100},
            )
        )

        return WorkflowSpec(
            id=spec_id,
            name=name,
            description=desc,
            triggers={"canonical": query.lower(), "aliases": [q_lower]},
            target_app={"app_name": browser_app},
            steps=steps,
        )

    @staticmethod
    def synthesize_app_open_workflow(query: str) -> Optional[WorkflowSpec]:
        """Builds a WorkflowSpec to open and focus ANY application on user's Mac."""
        q = query.strip()
        m = re.match(r"^(?:open|launch|start|focus|switch to)\s+(?:the\s+)?(.+?)(?:\s+app)?$", q, re.IGNORECASE)
        if not m:
            return None

        app_name_query = m.group(1).strip()
        resolved = SystemAppRegistry.resolve_app(app_name_query)
        if not resolved:
            return None

        display_name = resolved["name"]
        bundle_id = resolved.get("bundle_id", "")
        spec_id = f"dynamic_app_{uuid.uuid4().hex[:8]}"

        steps: List[WorkflowStep] = [
            # Step 1: Move virtual cursor towards dock / center
            WorkflowStep(
                step_id="step_cursor_approach",
                order=1,
                description=f"Clio approaches {display_name}",
                action=ActionType.MOVE_MOUSE,
                coordinates=TargetCoordinates(mode=CoordMode.SCREEN_ABSOLUTE, abs_x=400, abs_y=400),
                timing={"pre_delay_ms": 50, "post_delay_ms": 150},
            ),
            # Step 2: Launch or focus the application
            WorkflowStep(
                step_id="step_launch_target_app",
                order=2,
                description=f"Launch and focus {display_name}",
                action=ActionType.LAUNCH_APP,
                payload={"app": display_name, "bundle_id": bundle_id},
                target={"app_name": display_name, "bundle_id": bundle_id},
                timing={"pre_delay_ms": 100, "post_delay_ms": 400},
            ),
            # Step 3: Ensure frontmost focus
            WorkflowStep(
                step_id="step_focus_target_app",
                order=3,
                description=f"Activate window for {display_name}",
                action=ActionType.FOCUS_APP,
                target={"app_name": display_name, "bundle_id": bundle_id},
                timing={"pre_delay_ms": 50, "post_delay_ms": 200},
            ),
            # Step 4: Virtual cursor settles on application window to confirm
            WorkflowStep(
                step_id="step_cursor_settle",
                order=4,
                description=f"Clio ready inside {display_name}",
                action=ActionType.CLICK,
                coordinates=TargetCoordinates(mode=CoordMode.SCREEN_ABSOLUTE, abs_x=500, abs_y=350),
                timing={"pre_delay_ms": 150, "post_delay_ms": 50},
            ),
        ]

        return WorkflowSpec(
            id=spec_id,
            name=f"Open {display_name}",
            description=f"Launches and brings {display_name} to the front using Clio's virtual cursor.",
            triggers={"canonical": f"open {display_name.lower()}", "aliases": [q.lower()]},
            target_app={"app_name": display_name, "bundle_id": bundle_id},
            steps=steps,
        )

    @classmethod
    def parse_intent(cls, query: str) -> Optional[WorkflowSpec]:
        """Main entry point: translates query into an on-the-fly executable WorkflowSpec."""
        if not query or not query.strip():
            return None

        # 1. Browser tab intent
        if cls.is_browser_tab_intent(query):
            return cls.synthesize_browser_tab_workflow(query)

        # 2. App open intent
        if cls.is_app_open_intent(query):
            return cls.synthesize_app_open_workflow(query)

        # 3. Direct app name (e.g. user just types "Safari", "Chrome", "Notes", "Calculator")
        resolved = SystemAppRegistry.resolve_app(query)
        if resolved:
            return cls.synthesize_app_open_workflow(f"open {resolved['name']}")

        return None
