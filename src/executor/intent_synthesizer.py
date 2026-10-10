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

    SEARCH_ENGINES = {
        "google": "https://www.google.com/search?q={query}",
        "youtube": "https://www.youtube.com/results?search_query={query}",
        "yt": "https://www.youtube.com/results?search_query={query}",
        "github": "https://github.com/search?q={query}",
        "gh": "https://github.com/search?q={query}",
        "reddit": "https://www.reddit.com/search/?q={query}",
        "wikipedia": "https://en.wikipedia.org/wiki/Special:Search?search={query}",
        "wiki": "https://en.wikipedia.org/wiki/Special:Search?search={query}",
        "duckduckgo": "https://duckduckgo.com/?q={query}",
        "ddg": "https://duckduckgo.com/?q={query}",
        "bing": "https://www.bing.com/search?q={query}",
        "amazon": "https://www.amazon.com/s?k={query}",
    }

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
    def is_search_intent(cls, query: str) -> bool:
        """Determines if query represents searching Google, YouTube, GitHub, or the web."""
        q = re.sub(r"\s+", " ", query.strip().lower())
        if re.search(r"^(?:search|google|look\s+up|find)\s+", q):
            return True
        if re.search(r"^(?:search\s+(?:the\s+)?(?:web|website|internet|online))\b", q):
            return True
        if re.search(r"\b(?:on\s+(?:google|youtube|github|reddit|wikipedia|the\s+web))\s*$", q):
            return True
        return False

    @classmethod
    def synthesize_search_workflow(cls, query: str) -> WorkflowSpec:
        """Builds a WorkflowSpec to search Google, YouTube, GitHub, or the web."""
        q = re.sub(r"\s+", " ", query.strip())
        q_lower = q.lower()

        # Identify target search engine
        engine = "google"
        for eng in ("youtube", "yt", "github", "gh", "reddit", "wikipedia", "wiki", "duckduckgo", "ddg", "bing", "amazon"):
            if eng in q_lower.split() or f"on {eng}" in q_lower or f"in {eng}" in q_lower:
                engine = eng
                break

        # Clean search terms
        clean_terms = q
        clean_terms = re.sub(r"^(?:please\s+)?(?:can\s+you\s+)?(?:search|look\s+up|find|google)\s+(?:the\s+web\s+|website\s+|online\s+)?(?:for\s+)?", "", clean_terms, flags=re.IGNORECASE)
        clean_terms = re.sub(r"^(?:google|youtube|github|reddit|wikipedia)\s+(?:for\s+)?", "", clean_terms, flags=re.IGNORECASE)
        clean_terms = re.sub(r"\s+(?:on|in|using)\s+(?:google|youtube|github|reddit|wikipedia|the\s+web|safari|chrome)$", "", clean_terms, flags=re.IGNORECASE).strip()
        clean_terms = clean_terms.strip("'\"")

        if not clean_terms:
            clean_terms = "Clio"

        url_template = cls.SEARCH_ENGINES.get(engine, cls.SEARCH_ENGINES["google"])
        encoded_query = urllib.parse.quote_plus(clean_terms)
        target_url = url_template.format(query=encoded_query)

        spec_id = f"search_{uuid.uuid4().hex[:8]}"
        steps: List[WorkflowStep] = [
            WorkflowStep(
                step_id="step_cursor_search",
                order=1,
                description=f"Clio prepares web search for '{clean_terms}'",
                action=ActionType.MOVE_MOUSE,
                coordinates=TargetCoordinates(mode=CoordMode.SCREEN_ABSOLUTE, abs_x=640, abs_y=80),
                timing={"pre_delay_ms": 50, "post_delay_ms": 100},
            ),
            WorkflowStep(
                step_id="step_open_search_url",
                order=2,
                description=f"Open search in browser: {clean_terms}",
                action=ActionType.OPEN_URL,
                payload={"url": target_url},
                timing={"pre_delay_ms": 100, "post_delay_ms": 300},
            ),
            WorkflowStep(
                step_id="step_confirm_search",
                order=3,
                description=f"Clio settles on search results for '{clean_terms}'",
                action=ActionType.CLICK,
                coordinates=TargetCoordinates(mode=CoordMode.SCREEN_ABSOLUTE, abs_x=640, abs_y=220),
                timing={"pre_delay_ms": 150, "post_delay_ms": 50},
            ),
        ]

        engine_title = "YouTube" if engine in ("yt", "youtube") else ("GitHub" if engine in ("gh", "github") else engine.capitalize())
        return WorkflowSpec(
            id=spec_id,
            name=f"Search {engine_title}: {clean_terms}",
            description=f"Searches {engine_title} for '{clean_terms}' and navigates directly to results.",
            triggers={"canonical": f"search {clean_terms.lower()}", "aliases": [q_lower, f"google {clean_terms.lower()}"]},
            target_app={"app_name": "Safari"},
            steps=steps,
        )

    @classmethod
    def is_write_intent(cls, query: str) -> bool:
        """Determines if query represents writing a note, taking a note, or typing text."""
        q = re.sub(r"\s+", " ", query.strip().lower())
        if re.search(r"^(?:write|type|draft|note\s+down|take\s+a\s+note|take\s+note|create\s+a\s+note|record\s+note)\b", q):
            return True
        if re.search(r"\b(?:in\s+notes|in\s+textedit|in\s+a\s+note)\s*$", q):
            return True
        return False

    @classmethod
    def synthesize_writing_workflow(cls, query: str) -> WorkflowSpec:
        """Builds a WorkflowSpec to write/type notes or text into Notes or TextEdit."""
        q = re.sub(r"\s+", " ", query.strip())
        q_lower = q.lower()

        target_app = "Notes"
        target_bundle = "com.apple.Notes"
        if "textedit" in q_lower:
            target_app = "TextEdit"
            target_bundle = "com.apple.TextEdit"

        text_to_write = q
        m = re.search(
            r"^(?:please\s+)?(?:can\s+you\s+)?(?:write\s+a\s+note\s+(?:saying|that\s+says)?|take\s+a\s+note\s+(?:saying|that\s+says)?|take\s+note\s+(?:of|that)?|note\s+down|create\s+a\s+note\s+(?:saying)?|write\s+in\s+notes|write|type|draft)\s*[:\-]?\s*(.+)$",
            text_to_write,
            re.IGNORECASE,
        )
        if m:
            text_to_write = m.group(1).strip()

        text_to_write = re.sub(r"\s+(?:in|into|on)\s+(?:notes|textedit|a\s+note|my\s+notes)$", "", text_to_write, flags=re.IGNORECASE).strip()
        text_to_write = text_to_write.strip("'\"")

        if not text_to_write:
            text_to_write = "Note recorded by Clio."

        spec_id = f"write_{uuid.uuid4().hex[:8]}"
        steps: List[WorkflowStep] = [
            WorkflowStep(
                step_id="step_cursor_approach_write",
                order=1,
                description=f"Clio approaches {target_app}",
                action=ActionType.MOVE_MOUSE,
                coordinates=TargetCoordinates(mode=CoordMode.SCREEN_ABSOLUTE, abs_x=450, abs_y=350),
                timing={"pre_delay_ms": 50, "post_delay_ms": 100},
            ),
            WorkflowStep(
                step_id="step_launch_write_app",
                order=2,
                description=f"Open {target_app}",
                action=ActionType.LAUNCH_APP,
                payload={"app": target_app, "bundle_id": target_bundle},
                target={"app_name": target_app, "bundle_id": target_bundle},
                timing={"pre_delay_ms": 100, "post_delay_ms": 300},
            ),
            WorkflowStep(
                step_id="step_focus_write_app",
                order=3,
                description=f"Activate {target_app} window",
                action=ActionType.FOCUS_APP,
                target={"app_name": target_app, "bundle_id": target_bundle},
                timing={"pre_delay_ms": 50, "post_delay_ms": 200},
            ),
            WorkflowStep(
                step_id="step_new_note_hotkey",
                order=4,
                description="Create new document via Cmd+N",
                action=ActionType.PRESS_HOTKEY,
                payload={"keys": ["cmd", "n"]},
                target={"app_name": target_app, "bundle_id": target_bundle},
                timing={"pre_delay_ms": 100, "post_delay_ms": 250},
            ),
            WorkflowStep(
                step_id="step_click_editor",
                order=5,
                description=f"Clio positions cursor inside {target_app} body",
                action=ActionType.CLICK,
                coordinates=TargetCoordinates(mode=CoordMode.SCREEN_ABSOLUTE, abs_x=550, abs_y=320),
                target={"app_name": target_app, "bundle_id": target_bundle},
                timing={"pre_delay_ms": 100, "post_delay_ms": 100},
            ),
            WorkflowStep(
                step_id="step_type_content",
                order=6,
                description=f"Type note content ({len(text_to_write)} characters)",
                action=ActionType.PASTE_TEXT if len(text_to_write) > 40 else ActionType.TYPE_TEXT,
                payload={"text": text_to_write, "interval": 0.02},
                target={"app_name": target_app, "bundle_id": target_bundle},
                timing={"pre_delay_ms": 50, "post_delay_ms": 200},
            ),
        ]

        short_preview = text_to_write if len(text_to_write) <= 25 else text_to_write[:22] + "..."
        return WorkflowSpec(
            id=spec_id,
            name=f"Write: {short_preview}",
            description=f"Writes '{short_preview}' into {target_app} using Clio's virtual cursor.",
            triggers={"canonical": f"write {short_preview.lower()}", "aliases": [q_lower]},
            target_app={"app_name": target_app, "bundle_id": target_bundle},
            steps=steps,
        )

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

        if target_url:
            parsed = urllib.parse.urlparse(target_url)
            if parsed.scheme.lower() not in ("http", "https") or target_url.startswith("-") or any(c in target_url for c in "\r\n\x00"):
                target_url = ""

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

    # -------------------------------------------------------------------------
    # Cursor Movement Intent
    # -------------------------------------------------------------------------

    @classmethod
    def is_cursor_move_intent(cls, query: str) -> bool:
        """Determines if query represents moving the mouse / virtual cursor."""
        q = query.strip().lower()
        return bool(re.search(r"\b(?:move cursor|move mouse|cursor to|mouse to|center cursor|center mouse|reset cursor|cursor|mouse pointer)\b", q))

    @classmethod
    def synthesize_cursor_move_workflow(cls, query: str) -> WorkflowSpec:
        """Builds a WorkflowSpec to smoothly navigate the cursor to coordinates or screen positions."""
        q = query.strip().lower()
        target_x, target_y = 720.0, 450.0  # Default screen center
        desc = "Move cursor to screen center"

        # Check explicit coordinates (e.g. "move cursor to 500, 300" or "(500, 300)")
        coord_match = re.search(r"(\d{1,5})\s*[,x\s]\s*(\d{1,5})", q)
        if coord_match:
            target_x = float(coord_match.group(1))
            target_y = float(coord_match.group(2))
            desc = f"Move cursor to ({int(target_x)}, {int(target_y)})"
        elif "top left" in q or "top-left" in q:
            target_x, target_y = 60.0, 60.0
            desc = "Move cursor to top-left"
        elif "top right" in q or "top-right" in q:
            target_x, target_y = 1380.0, 60.0
            desc = "Move cursor to top-right"
        elif "bottom left" in q or "bottom-left" in q:
            target_x, target_y = 60.0, 840.0
            desc = "Move cursor to bottom-left"
        elif "bottom right" in q or "bottom-right" in q:
            target_x, target_y = 1380.0, 840.0
            desc = "Move cursor to bottom-right"
        elif "top" in q:
            target_x, target_y = 720.0, 60.0
            desc = "Move cursor to top"
        elif "bottom" in q:
            target_x, target_y = 720.0, 840.0
            desc = "Move cursor to bottom"

        spec_id = f"move_cursor_{uuid.uuid4().hex[:8]}"
        steps = [
            WorkflowStep(
                step_id="step_move_cursor",
                order=1,
                description=desc,
                action=ActionType.MOVE_MOUSE,
                coordinates=TargetCoordinates(mode=CoordMode.SCREEN_ABSOLUTE, abs_x=target_x, abs_y=target_y),
                timing={"pre_delay_ms": 50, "post_delay_ms": 150},
            )
        ]
        return WorkflowSpec(
            id=spec_id,
            name=desc,
            description=f"Smoothly guides the cursor to ({int(target_x)}, {int(target_y)}).",
            triggers={"canonical": "move cursor", "aliases": [q]},
            steps=steps,
        )

    # -------------------------------------------------------------------------
    # Window Switching & Swiping Intent
    # -------------------------------------------------------------------------

    @classmethod
    def is_window_switch_intent(cls, query: str) -> bool:
        """Determines if query represents switching or swiping between windows/spaces."""
        q = query.strip().lower()
        return bool(re.search(r"\b(?:swipe between (?:the )?windows?|switch windows?|switch between windows?|cycle windows?|next window|previous window|swipe window|swipe spaces?|switch space|swipe desktops?|switch apps?|switch applications?|switch between apps?|mission control|show all windows)\b", q))

    @classmethod
    def synthesize_window_switch_workflow(cls, query: str) -> WorkflowSpec:
        """Builds a WorkflowSpec to switch or swipe between windows/spaces using native shortcuts."""
        q = query.strip().lower()
        spec_id = f"window_switch_{uuid.uuid4().hex[:8]}"

        if "mission control" in q or "show all windows" in q or "all windows" in q:
            hotkey = ["ctrl", "up"]
            desc = "Activate Mission Control"
        elif ("left" in q and ("swipe" in q or "space" in q or "desktop" in q)) or "previous space" in q:
            hotkey = ["ctrl", "left"]
            desc = "Swipe to previous desktop space"
        elif ("right" in q and ("swipe" in q or "space" in q or "desktop" in q)) or "next space" in q:
            hotkey = ["ctrl", "right"]
            desc = "Swipe to next desktop space"
        elif "switch app" in q or "switch application" in q or "switch between apps" in q or "app" in q or "cmd tab" in q:
            hotkey = ["cmd", "tab"]
            desc = "Switch to next application via Cmd+Tab"
        elif "previous window" in q or "back" in q:
            hotkey = ["cmd", "shift", "`"]
            desc = "Switch to previous window"
        else:
            # Default "swipe between the window" / "switch window"
            hotkey = ["cmd", "`"]
            desc = "Switch between windows via Cmd+`"

        steps = [
            WorkflowStep(
                step_id="step_window_switch_hotkey",
                order=1,
                description=desc,
                action=ActionType.PRESS_HOTKEY,
                payload={"keys": hotkey},
                timing={"pre_delay_ms": 100, "post_delay_ms": 300},
            )
        ]
        return WorkflowSpec(
            id=spec_id,
            name=desc,
            description=f"Performs macOS window switching using {hotkey}.",
            triggers={"canonical": "switch window", "aliases": [q]},
            steps=steps,
        )

    # -------------------------------------------------------------------------
    # Mouse & Button Click Intent
    # -------------------------------------------------------------------------

    @classmethod
    def is_click_intent(cls, query: str) -> bool:
        """Determines if query is requesting to click a button or click the mouse."""
        q = query.strip().lower()
        if re.search(r"\b(?:double\s+click|right\s+click|left\s+click)\b", q):
            return True
        if re.match(r"^(?:please\s+)?(?:click|press|tap)\s*(?:the\s+)?(?:button|here|mouse)?\s*$", q):
            return True
        if re.search(r"^(?:please\s+)?(?:click|press)\s+(?:the\s+)?([a-zA-Z0-9\s_\-\.]{1,30}?)(?:\s+button)?$", q):
            return True
        return False

    @classmethod
    def synthesize_click_workflow(cls, query: str) -> WorkflowSpec:
        """Builds a WorkflowSpec to click at current position or target a specific UI button."""
        q = query.strip()
        q_lower = q.lower()
        spec_id = f"click_{uuid.uuid4().hex[:8]}"

        if "double click" in q_lower:
            action = ActionType.DOUBLE_CLICK
            desc = "Double click at current position"
            target = None
        elif "right click" in q_lower:
            action = ActionType.RIGHT_CLICK
            desc = "Right click at current position"
            target = None
        else:
            action = ActionType.CLICK
            # Check for button name
            btn_match = re.search(r"^(?:please\s+)?(?:click|press)\s+(?:the\s+)?(?:button\s+)?(.+?)(?:\s+button)?$", q, re.IGNORECASE)
            btn_name = ""
            if btn_match:
                candidate = btn_match.group(1).strip()
                if candidate.lower() not in ("here", "mouse", "button", "now", "it"):
                    btn_name = candidate

            if btn_name:
                desc = f"Click button '{btn_name}'"
                target = {
                    "tri_factor_anchor": {
                        "factor_1_ax": {
                            "ax_role": "AXButton",
                            "ax_title": btn_name,
                        }
                    }
                }
            else:
                desc = "Click at current position"
                target = None

        step = WorkflowStep(
            step_id="step_click_action",
            order=1,
            description=desc,
            action=action,
            target=target,
            timing={"pre_delay_ms": 50, "post_delay_ms": 150},
        )
        return WorkflowSpec(
            id=spec_id,
            name=desc,
            description=f"Executes {desc} using Clio's actuator.",
            triggers={"canonical": "click", "aliases": [q_lower]},
            steps=[step],
        )

    # -------------------------------------------------------------------------
    # Spotlight & Finder Search Intents
    # -------------------------------------------------------------------------

    @classmethod
    def is_spotlight_search_intent(cls, query: str) -> bool:
        """Determines if query is requesting Spotlight search on macOS."""
        q = query.strip().lower()
        return bool(re.search(r"\b(?:spotlight|search spotlight|spotlight search|search mac)\b", q))

    @classmethod
    def synthesize_spotlight_workflow(cls, query: str) -> WorkflowSpec:
        """Builds a WorkflowSpec to invoke Spotlight and perform system search."""
        clean = query.strip()
        clean = re.sub(r"^(?:please\s+)?(?:search\s+(?:spotlight\s+for|mac\s+for)?|spotlight\s+(?:search\s+for|search)?)\s*", "", clean, flags=re.IGNORECASE).strip()
        clean = clean.strip("'\"")
        spec_id = f"spotlight_{uuid.uuid4().hex[:8]}"
        steps = [
            WorkflowStep(
                step_id="step_spotlight_hotkey",
                order=1,
                description="Invoke Spotlight via Cmd+Space",
                action=ActionType.PRESS_HOTKEY,
                payload={"keys": ["cmd", "space"]},
                timing={"pre_delay_ms": 50, "post_delay_ms": 300},
            ),
        ]
        if clean:
            steps.append(
                WorkflowStep(
                    step_id="step_spotlight_type",
                    order=2,
                    description=f"Type '{clean}' into Spotlight",
                    action=ActionType.TYPE_TEXT,
                    payload={"text": clean, "interval": 0.02},
                    timing={"pre_delay_ms": 100, "post_delay_ms": 200},
                )
            )
            steps.append(
                WorkflowStep(
                    step_id="step_spotlight_enter",
                    order=3,
                    description="Press Return to open top result",
                    action=ActionType.PRESS_HOTKEY,
                    payload={"keys": ["enter"]},
                    timing={"pre_delay_ms": 100, "post_delay_ms": 100},
                )
            )
        return WorkflowSpec(
            id=spec_id,
            name=f"Spotlight Search: {clean or 'Open'}",
            description=f"Invokes macOS Spotlight and searches for '{clean}'.",
            triggers={"canonical": f"spotlight {clean.lower()}", "aliases": [query.lower()]},
            steps=steps,
        )

    @classmethod
    def is_finder_search_intent(cls, query: str) -> bool:
        """Determines if query is requesting to search for files in Finder."""
        q = query.strip().lower()
        return bool(re.search(r"\b(?:search finder|finder search|find file|find files|search for file|search for files|search file|search files|in finder)\b", q))

    @classmethod
    def synthesize_finder_search_workflow(cls, query: str) -> WorkflowSpec:
        """Builds a WorkflowSpec to search files in macOS Finder."""
        clean = query.strip()
        clean = re.sub(r"^(?:please\s+)?(?:search\s+(?:finder\s+for|for\s+files?|files?\s+for)?|find\s+files?\s+(?:named|called)?)\s*", "", clean, flags=re.IGNORECASE).strip()
        clean = re.sub(r"\s+in\s+finder$", "", clean, flags=re.IGNORECASE).strip()
        clean = clean.strip("'\"")
        spec_id = f"finder_search_{uuid.uuid4().hex[:8]}"
        steps = [
            WorkflowStep(
                step_id="step_launch_finder",
                order=1,
                description="Activate Finder",
                action=ActionType.LAUNCH_APP,
                payload={"app": "Finder", "bundle_id": "com.apple.finder"},
                timing={"pre_delay_ms": 50, "post_delay_ms": 300},
            ),
            WorkflowStep(
                step_id="step_finder_search_hotkey",
                order=2,
                description="Trigger Finder search via Cmd+F",
                action=ActionType.PRESS_HOTKEY,
                payload={"keys": ["cmd", "f"]},
                timing={"pre_delay_ms": 100, "post_delay_ms": 200},
            ),
        ]
        if clean:
            steps.append(
                WorkflowStep(
                    step_id="step_finder_type",
                    order=3,
                    description=f"Type '{clean}' into Finder search field",
                    action=ActionType.TYPE_TEXT,
                    payload={"text": clean, "interval": 0.02},
                    timing={"pre_delay_ms": 100, "post_delay_ms": 200},
                )
            )
        return WorkflowSpec(
            id=spec_id,
            name=f"Finder Search: {clean or 'Files'}",
            description=f"Searches files in Finder for '{clean}'.",
            triggers={"canonical": f"search finder {clean.lower()}", "aliases": [query.lower()]},
            steps=steps,
        )

    @classmethod
    def parse_intent(cls, query: str) -> Optional[WorkflowSpec]:
        """Main entry point: translates query into an on-the-fly executable WorkflowSpec."""
        if not query or not query.strip():
            return None

        # 1. Cursor movement intent (e.g. "move cursor to center", "move cursor to 500, 300")
        if cls.is_cursor_move_intent(query):
            return cls.synthesize_cursor_move_workflow(query)

        # 2. Window switching / swiping intent (e.g. "swipe between the window", "switch window")
        if cls.is_window_switch_intent(query):
            return cls.synthesize_window_switch_workflow(query)

        # 3. Mouse & Button clicking intent (e.g. "click", "click button Submit", "double click")
        if cls.is_click_intent(query):
            return cls.synthesize_click_workflow(query)

        # 4. Spotlight search intent (e.g. "spotlight search notes", "search mac for...")
        if cls.is_spotlight_search_intent(query):
            return cls.synthesize_spotlight_workflow(query)

        # 5. Finder search intent (e.g. "search finder for documents", "find files...")
        if cls.is_finder_search_intent(query):
            return cls.synthesize_finder_search_workflow(query)

        # 6. Web Search intent (Google, YouTube, GitHub, Reddit, Wikipedia, web search)
        if cls.is_search_intent(query):
            return cls.synthesize_search_workflow(query)

        # 7. Writing intent (Notes, TextEdit, text typing/drafting)
        if cls.is_write_intent(query):
            return cls.synthesize_writing_workflow(query)

        # 8. Browser tab intent
        if cls.is_browser_tab_intent(query):
            return cls.synthesize_browser_tab_workflow(query)

        # 9. App open intent
        if cls.is_app_open_intent(query):
            return cls.synthesize_app_open_workflow(query)

        # 10. Direct app name (e.g. user just types "Safari", "Chrome", "Notes", "Calculator")
        resolved = SystemAppRegistry.resolve_app(query)
        if resolved:
            return cls.synthesize_app_open_workflow(f"open {resolved['name']}")

        return None
