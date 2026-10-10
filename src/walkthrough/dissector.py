"""Offline Pedagogical Walkthrough Dissector & Dynamic Task Synthesizer.

Path: src/walkthrough/dissector.py
Generates rich, contextual, multi-step WalkthroughPlans dynamically on-device
without requiring external network connections or cloud LLM API keys.
Accurately maps arbitrary macOS user prompts into distinct sequential steps,
application contexts, and physical coordinate anchor points.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple
import uuid

from src.walkthrough.models import (
    TeachingAction,
    WalkthroughMode,
    WalkthroughPlan,
    WalkthroughStep,
)

logger = logging.getLogger(__name__)


class OfflineWalkthroughDissector:
    """Dissects arbitrary user queries into structured macOS pedagogical walkthrough steps."""

    @classmethod
    def synthesize(
        cls,
        query: str,
        mode: WalkthroughMode = WalkthroughMode.GUIDED_DEMO,
        context: Optional[Dict[str, Any]] = None,
    ) -> WalkthroughPlan:
        """Synthesizes a distinct, customized WalkthroughPlan for any user query."""
        clean_q = cls._clean_query(query)
        spec_id = f"wt_ai_{uuid.uuid4().hex[:8]}"

        # Step 1: Detect Target Application & Sub-domain
        target_app, app_category = cls._detect_app_and_category(clean_q, context)

        # Step 2: Route to specialized category generator
        if app_category == "terminal":
            steps = cls._generate_terminal_steps(clean_q, target_app)
        elif app_category == "calculator":
            steps = cls._generate_calculator_steps(clean_q, target_app)
        elif app_category == "notes":
            steps = cls._generate_notes_steps(clean_q, target_app)
        elif app_category == "safari":
            steps = cls._generate_safari_steps(clean_q, target_app)
        elif app_category == "mail":
            steps = cls._generate_mail_steps(clean_q, target_app)
        elif app_category == "music":
            steps = cls._generate_music_steps(clean_q, target_app)
        elif app_category == "maps":
            steps = cls._generate_maps_steps(clean_q, target_app)
        elif app_category == "calendar":
            steps = cls._generate_calendar_steps(clean_q, target_app)
        elif app_category == "weather":
            steps = cls._generate_weather_steps(clean_q, target_app)
        elif app_category == "activity_monitor":
            steps = cls._generate_activity_monitor_steps(clean_q, target_app)
        elif app_category == "disk_utility":
            steps = cls._generate_disk_utility_steps(clean_q, target_app)
        elif app_category == "textedit":
            steps = cls._generate_textedit_steps(clean_q, target_app)
        elif app_category == "system_information":
            steps = cls._generate_system_information_steps(clean_q, target_app)
        elif app_category == "system_settings":
            steps = cls._generate_settings_steps(clean_q, target_app)
        elif app_category == "finder":
            steps = cls._generate_finder_steps(clean_q, target_app)
        elif app_category == "system_utility":
            steps = cls._generate_system_utility_steps(clean_q, target_app)
        elif app_category == "cursor_navigation":
            steps = cls._generate_cursor_navigation_steps(clean_q, target_app)
        elif app_category == "window_management":
            steps = cls._generate_window_management_steps(clean_q, target_app)
        elif app_category == "button_click":
            steps = cls._generate_button_click_steps(clean_q, target_app)
        else:
            steps = cls._generate_general_app_steps(clean_q, target_app)

        # Step 3: Format summary and return plan
        summary = f"Step-by-step interactive walkthrough demonstrating how to {clean_q} on macOS."
        return WalkthroughPlan(
            walkthrough_id=spec_id,
            goal=clean_q,
            summary=summary,
            target_app=target_app,
            steps=steps,
            mode=mode,
            source_tier="TIER_3_CLOUD_AI",
        )

    @classmethod
    def _clean_query(cls, query: str) -> str:
        """Strips conversational noise and prefixes."""
        q = query.strip()
        noise_patterns = [
            r"^(?:please\s+)?(?:can\s+you\s+)?(?:teach\s+me|show\s+me|guide\s+me|walk\s+me\s+through|walk\s+me|tutorial\s+on|tutorial)\s+(?:how\s+to\s+|to\s+)?",
            r"^(?:how\s+(?:do|can)\s+i\s+|how\s+to\s+)",
            r"^(?:learn\s+how\s+to\s+|learn\s+to\s+|learn\s+)",
            r"^(?:walkthrough\s+on\s+|walkthrough\s+)",
            r"^(?:i\s+want\s+to\s+|help\s+me\s+(?:to\s+)?)",
        ]
        for pat in noise_patterns:
            q = re.sub(pat, "", q, flags=re.IGNORECASE).strip()
        return q or "macOS Navigation"

    @staticmethod
    def _has_keyword(text: str, keywords: Tuple[str, ...]) -> bool:
        """Checks if any keyword matches as a distinct word or multi-word phrase in text."""
        for kw in keywords:
            if " " in kw or "-" in kw or "." in kw:
                if kw in text:
                    return True
            else:
                if re.search(rf"\b{re.escape(kw)}\b", text):
                    return True
        return False

    @classmethod
    def _detect_app_and_category(
        cls,
        clean_q: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, str]:
        """Detects the specific application and functional domain from query tokens."""
        q = clean_q.lower()
        has = cls._has_keyword

        # 1. Activity Monitor (check before generic memory or terminal)
        if has(q, ("activity monitor", "task manager", "cpu usage", "memory pressure", "kill process", "ram usage", "processes")):
            return "Activity Monitor", "activity_monitor"

        # 2. Disk Utility
        if has(q, ("disk utility", "format drive", "first aid", "partition", "format disk", "erase disk")):
            return "Disk Utility", "disk_utility"

        # 3. Terminal / Shell commands
        if has(q, ("terminal", "command line", "unix", "cli", "bash", "zsh", "ping", "curl", "brew", "git", "shell", "ipconfig", "ifconfig")):
            return "Terminal", "terminal"

        # 4. Calculator
        if has(q, ("calculator", "calculate", "math", "add numbers", "multiply", "divide", "subtract", "sum")):
            return "Calculator", "calculator"

        # 5. Notes
        if has(q, ("notes", "note", "shopping list", "to-do list", "memo", "checklist")):
            return "Notes", "notes"

        # 6. Safari / Web
        if has(q, ("safari", "browser", "browsing", "browse web", "browse the web", "website", "url", "web page", "web pages", "web history", "google search", "internet", "search web", "web", "tab", "tabs", "bookmark", "bookmarks", "private browsing")):
            return "Safari", "safari"

        # 7. Mail
        if has(q, ("mail", "email", "inbox", "compose email", "compose a message", "compose message", "send email", "message recipient")):
            return "Mail", "mail"

        # 8. Music
        if has(q, ("music", "song", "playlist", "play song", "spotify", "apple music", "album")):
            return "Music", "music"

        # 9. Maps
        if has(q, ("maps", "directions", "route", "location", "address", "map")):
            return "Maps", "maps"

        # 10. Calendar
        if has(q, ("calendar", "schedule", "event", "meeting", "reminder date", "appointment")):
            return "Calendar", "calendar"

        # 11. Weather
        if has(q, ("weather", "forecast", "temperature", "rain", "sunny", "climate")):
            return "Weather", "weather"

        # 12. TextEdit
        if has(q, ("textedit", "word processor", "draft document", "text file", "notepad")):
            return "TextEdit", "textedit"

        # 12b. System Information / About This Mac
        if has(q, ("system information", "system info", "about this mac", "system report", "mac specs", "hardware info")):
            return "System Information", "system_information"

        # 13. Cursor & Pointer Navigation
        if has(q, ("cursor", "mouse pointer", "move cursor", "move mouse", "pointer", "cursor navigation")):
            return "Finder", "cursor_navigation"

        # 14. Window Management, Moving & Switching Between Windows
        if has(
            q,
            (
                "move between windows",
                "move between window",
                "move between the windows",
                "switch between windows",
                "switch between window",
                "swipe window",
                "swipe between",
                "switch window",
                "switch windows",
                "cycle window",
                "cycle windows",
                "swipe desktop",
                "swipe spaces",
                "swipe between the window",
            ),
        ):
            return "Finder", "window_management"

        # 15. Interactive Controls & Button Clicking
        if has(q, ("click button", "click buttons", "click", "double click", "right click", "how to click")):
            return "Finder", "button_click"

        # 16. System Settings & Subpanes
        settings_keywords = (
            "settings", "preference", "preferences", "battery", "display", "displays", "screen resolution",
            "sound", "audio output", "volume", "trackpad", "mouse speed", "keyboard", "bluetooth",
            "wi-fi", "wifi", "network", "lock screen", "wallpaper", "appearance", "dark mode", "light mode",
            "accessibility", "notifications", "privacy", "security", "time machine", "software update"
        )
        if has(q, settings_keywords):
            return "System Settings", "system_settings"

        # 17. Finder / Files
        if has(q, ("finder", "file", "files", "folder", "folders", "desktop", "downloads", "documents", "trash", "bin", "directory", "external disk", "external drive")):
            return "Finder", "finder"

        # 18. System utilities & Command Bar
        if has(q, ("mission control", "stage manager", "spotlight", "command bar", "open command bar", "use command bar", "dock", "menu bar", "screenshot", "screen capture", "force quit", "sleep", "restart", "lock")):
            return "System Utilities", "system_utility"

        # Check desktop context
        if context and context.get("active_app") and context.get("active_app") not in ("Finder", "loginwindow"):
            return context["active_app"], "general_app"

        return "System Settings", "general_app"

    # =========================================================================
    # Specialized Walkthrough Generators
    # =========================================================================

    @classmethod
    def _generate_terminal_steps(cls, query: str, app: str) -> List[WalkthroughStep]:
        cmd_match = re.search(r"(?:run|execute|type|ping|curl)\s+([a-zA-Z0-9_\-\.\s/]+)", query, re.IGNORECASE)
        command_str = cmd_match.group(1).strip() if cmd_match else "echo 'Hello from Clio Walkthrough'"
        if "ping" in query.lower() and "google" in query.lower():
            command_str = "ping -c 4 google.com"

        return [
            WalkthroughStep(
                step_index=1,
                title="Launch Terminal via Spotlight",
                instruction="Press ⌘ + Space (Command + Space) to open Spotlight, or click Terminal in the Dock.",
                explanation="Spotlight gives you instantaneous keyboard access to launch Terminal or any utility.",
                action_type=TeachingAction.DEMONSTRATE_HOTKEY,
                hotkey_combo=["cmd", "space"],
                target_app=app,
                fallback_screen_coords=(640.0, 280.0),
                spotlight_bounds=(400.0, 250.0, 480.0, 60.0),
                pre_delay_seconds=0.4,
                post_delay_seconds=0.8,
            ),
            WalkthroughStep(
                step_index=2,
                title="Focus Command Prompt",
                instruction="Click inside the active Terminal window to focus the command line prompt.",
                explanation="Ensures keystrokes are received directly by the shell session.",
                action_type=TeachingAction.MOVE_AND_HOVER,
                target_app=app,
                target_element_query={"ax_role": "AXWindow", "ax_title": "Terminal"},
                fallback_screen_coords=(460.0, 320.0),
                spotlight_bounds=(200.0, 180.0, 440.0, 340.0),
                pre_delay_seconds=0.4,
                post_delay_seconds=0.6,
            ),
            WalkthroughStep(
                step_index=3,
                title="Execute Terminal Command",
                instruction=f"Type '{command_str}' and press Return to execute.",
                explanation=f"Executes '{command_str}' directly within your interactive zsh environment.",
                action_type=TeachingAction.DEMONSTRATE_TYPE,
                target_app=app,
                text_to_type=f"{command_str}\n",
                fallback_screen_coords=(420.0, 350.0),
                spotlight_bounds=(200.0, 180.0, 440.0, 340.0),
                pre_delay_seconds=0.4,
                post_delay_seconds=1.0,
            ),
            WalkthroughStep(
                step_index=4,
                title="Review Output",
                instruction="Observe the stdout response stream generated by the command.",
                explanation="Terminal streams live output and status codes back upon process completion.",
                action_type=TeachingAction.PULSE_BEACON,
                target_app=app,
                fallback_screen_coords=(420.0, 400.0),
                spotlight_bounds=(200.0, 250.0, 440.0, 200.0),
                pre_delay_seconds=0.5,
                post_delay_seconds=1.0,
            ),
        ]

    @classmethod
    def _generate_calculator_steps(cls, query: str, app: str) -> List[WalkthroughStep]:
        math_expr = "25 * 4"
        expr_match = re.search(r"(\d+\s*[\+\-\*/\^]\s*\d+)", query)
        if expr_match:
            math_expr = expr_match.group(1).strip()

        return [
            WalkthroughStep(
                step_index=1,
                title="Open Calculator via Spotlight",
                instruction="Press ⌘ + Space (Command + Space) to search for and launch Calculator.",
                explanation="Brings macOS Calculator to the front for quick mathematical operations.",
                action_type=TeachingAction.DEMONSTRATE_HOTKEY,
                hotkey_combo=["cmd", "space"],
                target_app=app,
                fallback_screen_coords=(640.0, 280.0),
                spotlight_bounds=(400.0, 250.0, 480.0, 60.0),
                pre_delay_seconds=0.4,
                post_delay_seconds=0.8,
            ),
            WalkthroughStep(
                step_index=2,
                title="Input Calculation Expression",
                instruction=f"Enter calculation: {math_expr} using the number pad or keyboard.",
                explanation=f"Calculates mathematical expression '{math_expr}'.",
                action_type=TeachingAction.DEMONSTRATE_TYPE,
                target_app=app,
                target_element_query={"ax_role": "AXWindow", "ax_title": "Calculator"},
                text_to_type=f"{math_expr}=",
                fallback_screen_coords=(530.0, 260.0),
                spotlight_bounds=(460.0, 260.0, 200.0, 240.0),
                pre_delay_seconds=0.5,
                post_delay_seconds=0.8,
            ),
            WalkthroughStep(
                step_index=3,
                title="Inspect Computed Result",
                instruction="Verify the evaluated answer displayed in the digital readout.",
                explanation="The top display register reflects the calculated arithmetic total.",
                action_type=TeachingAction.PULSE_BEACON,
                target_app=app,
                fallback_screen_coords=(560.0, 280.0),
                spotlight_bounds=(470.0, 270.0, 180.0, 50.0),
                pre_delay_seconds=0.5,
                post_delay_seconds=1.0,
            ),
        ]

    @classmethod
    def _generate_notes_steps(cls, query: str, app: str) -> List[WalkthroughStep]:
        note_topic = "Meeting Notes"
        if "shopping" in query.lower() or "grocery" in query.lower():
            note_topic = "Grocery Shopping List"
        elif "todo" in query.lower() or "checklist" in query.lower():
            note_topic = "Action Items Checklist"

        return [
            WalkthroughStep(
                step_index=1,
                title="Open Notes via Spotlight",
                instruction="Press ⌘ + Space (Command + Space) or click Notes in the Dock.",
                explanation="Access your synced iCloud and local notes notebooks.",
                action_type=TeachingAction.DEMONSTRATE_HOTKEY,
                hotkey_combo=["cmd", "space"],
                target_app=app,
                fallback_screen_coords=(640.0, 280.0),
                spotlight_bounds=(400.0, 250.0, 480.0, 60.0),
                pre_delay_seconds=0.4,
                post_delay_seconds=0.8,
            ),
            WalkthroughStep(
                step_index=2,
                title="Create New Note",
                instruction="Click the Compose button (square with pencil) or press Cmd+N.",
                explanation="Creates a blank new note document ready for instant typing.",
                action_type=TeachingAction.DEMONSTRATE_CLICK,
                target_app=app,
                target_element_query={"ax_role": "AXButton", "ax_title": "New Note"},
                fallback_screen_coords=(300.0, 72.0),
                spotlight_bounds=(280.0, 56.0, 40.0, 32.0),
                pre_delay_seconds=0.5,
                post_delay_seconds=0.8,
            ),
            WalkthroughStep(
                step_index=3,
                title="Draft Note Content",
                instruction=f"Type title '{note_topic}' and your itemized notes in the canvas.",
                explanation="The first line automatically formats as the note title.",
                action_type=TeachingAction.DEMONSTRATE_TYPE,
                target_app=app,
                text_to_type=f"{note_topic}\n- Item 1\n- Item 2\n",
                fallback_screen_coords=(600.0, 260.0),
                spotlight_bounds=(420.0, 120.0, 460.0, 400.0),
                pre_delay_seconds=0.5,
                post_delay_seconds=1.0,
            ),
        ]

    @classmethod
    def _generate_safari_steps(cls, query: str, app: str) -> List[WalkthroughStep]:
        target_dest = "apple.com"
        dest_match = re.search(r"(?:go to|visit|open|navigate to)\s+([a-zA-Z0-9\.\-_/]+)", query, re.IGNORECASE)
        if dest_match:
            target_dest = dest_match.group(1).strip()

        return [
            WalkthroughStep(
                step_index=1,
                title="Focus Safari",
                instruction="Click Safari in your Dock or press ⌘ + Space (Command + Space).",
                explanation="Brings the native macOS web browser to the foreground.",
                action_type=TeachingAction.DEMONSTRATE_HOTKEY,
                hotkey_combo=["cmd", "space"],
                target_app=app,
                fallback_screen_coords=(640.0, 280.0),
                spotlight_bounds=(400.0, 250.0, 480.0, 60.0),
                pre_delay_seconds=0.4,
                post_delay_seconds=0.8,
            ),
            WalkthroughStep(
                step_index=2,
                title="Select Smart Search Field",
                instruction="Click the unified address bar at the top or press Cmd+L.",
                explanation="Highlights the search / URL input field for new navigation.",
                action_type=TeachingAction.DEMONSTRATE_CLICK,
                target_app=app,
                target_element_query={"ax_role": "AXTextField", "ax_title": "Address"},
                fallback_screen_coords=(640.0, 78.0),
                spotlight_bounds=(320.0, 64.0, 640.0, 28.0),
                pre_delay_seconds=0.5,
                post_delay_seconds=0.8,
            ),
            WalkthroughStep(
                step_index=3,
                title="Navigate to URL",
                instruction=f"Type '{target_dest}' and press Return to browse.",
                explanation=f"Loads web destination '{target_dest}' in the active tab.",
                action_type=TeachingAction.DEMONSTRATE_TYPE,
                target_app=app,
                text_to_type=f"{target_dest}\n",
                fallback_screen_coords=(640.0, 78.0),
                spotlight_bounds=(320.0, 64.0, 640.0, 28.0),
                pre_delay_seconds=0.5,
                post_delay_seconds=1.0,
            ),
        ]

    @classmethod
    def _generate_mail_steps(cls, query: str, app: str) -> List[WalkthroughStep]:
        return [
            WalkthroughStep(
                step_index=1,
                title="Open Apple Mail via Spotlight",
                instruction="Press ⌘ + Space (Command + Space) or click Mail in the Dock.",
                explanation="Access unified mailboxes and email accounts.",
                action_type=TeachingAction.DEMONSTRATE_HOTKEY,
                hotkey_combo=["cmd", "space"],
                target_app=app,
                fallback_screen_coords=(640.0, 280.0),
                spotlight_bounds=(400.0, 250.0, 480.0, 60.0),
                pre_delay_seconds=0.4,
                post_delay_seconds=0.8,
            ),
            WalkthroughStep(
                step_index=2,
                title="Compose New Message",
                instruction="Click the Compose button in the toolbar or press Cmd+N.",
                explanation="Opens a new draft composition sheet.",
                action_type=TeachingAction.DEMONSTRATE_CLICK,
                target_app=app,
                target_element_query={"ax_role": "AXButton", "ax_title": "New Message"},
                fallback_screen_coords=(280.0, 70.0),
                spotlight_bounds=(260.0, 54.0, 40.0, 32.0),
                pre_delay_seconds=0.5,
                post_delay_seconds=0.8,
            ),
            WalkthroughStep(
                step_index=3,
                title="Fill Message Details",
                instruction="Enter the recipient address, subject header, and email body.",
                explanation="Prepares the message content for outbound sending.",
                action_type=TeachingAction.MOVE_AND_HOVER,
                target_app=app,
                fallback_screen_coords=(540.0, 260.0),
                spotlight_bounds=(400.0, 160.0, 480.0, 320.0),
                pre_delay_seconds=0.5,
                post_delay_seconds=1.0,
            ),
        ]

    @classmethod
    def _generate_music_steps(cls, query: str, app: str) -> List[WalkthroughStep]:
        return [
            WalkthroughStep(
                step_index=1,
                title="Launch Music via Spotlight",
                instruction="Press ⌘ + Space (Command + Space) or click Music in the Dock.",
                explanation="Brings your Apple Music library and playlists into focus.",
                action_type=TeachingAction.DEMONSTRATE_HOTKEY,
                hotkey_combo=["cmd", "space"],
                target_app=app,
                fallback_screen_coords=(640.0, 280.0),
                spotlight_bounds=(400.0, 250.0, 480.0, 60.0),
                pre_delay_seconds=0.4,
                post_delay_seconds=0.8,
            ),
            WalkthroughStep(
                step_index=2,
                title="Select Library / Search",
                instruction="Click the Search bar or select Playlists from the sidebar.",
                explanation="Allows finding specific artists, albums, or curated playlists.",
                action_type=TeachingAction.DEMONSTRATE_CLICK,
                target_app=app,
                target_element_query={"ax_role": "AXTextField", "ax_title": "Search"},
                fallback_screen_coords=(110.0, 78.0),
                spotlight_bounds=(40.0, 66.0, 140.0, 26.0),
                pre_delay_seconds=0.5,
                post_delay_seconds=0.8,
            ),
            WalkthroughStep(
                step_index=3,
                title="Play Audio Track",
                instruction="Double-click a track or hit the Play button in the top transport controls.",
                explanation="Starts playback through macOS system audio.",
                action_type=TeachingAction.DEMONSTRATE_CLICK,
                target_app=app,
                fallback_screen_coords=(640.0, 68.0),
                spotlight_bounds=(620.0, 52.0, 40.0, 32.0),
                pre_delay_seconds=0.5,
                post_delay_seconds=1.0,
            ),
        ]

    @classmethod
    def _generate_maps_steps(cls, query: str, app: str) -> List[WalkthroughStep]:
        dest = "Central Park"
        d_match = re.search(r"(?:to|in)\s+([a-zA-Z0-9\s]+)", query)
        if d_match:
            dest = d_match.group(1).strip()

        return [
            WalkthroughStep(
                step_index=1,
                title="Launch Apple Maps via Spotlight",
                instruction="Press ⌘ + Space (Command + Space) or click Maps in the Dock.",
                explanation="Provides turn-by-turn routing and geographic exploration.",
                action_type=TeachingAction.DEMONSTRATE_HOTKEY,
                hotkey_combo=["cmd", "space"],
                target_app=app,
                fallback_screen_coords=(640.0, 280.0),
                spotlight_bounds=(400.0, 250.0, 480.0, 60.0),
                pre_delay_seconds=0.4,
                post_delay_seconds=0.8,
            ),
            WalkthroughStep(
                step_index=2,
                title="Enter Destination",
                instruction=f"Type '{dest}' into the top search bar and press Return.",
                explanation=f"Locates geographic pin and info card for '{dest}'.",
                action_type=TeachingAction.DEMONSTRATE_TYPE,
                target_app=app,
                target_element_query={"ax_role": "AXTextField", "ax_title": "Search"},
                text_to_type=f"{dest}\n",
                fallback_screen_coords=(140.0, 74.0),
                spotlight_bounds=(40.0, 60.0, 200.0, 28.0),
                pre_delay_seconds=0.5,
                post_delay_seconds=0.8,
            ),
            WalkthroughStep(
                step_index=3,
                title="Initiate Route Directions",
                instruction="Click 'Directions' in the location card to view driving or transit routes.",
                explanation="Computes live traffic and estimated transit times.",
                action_type=TeachingAction.DEMONSTRATE_CLICK,
                target_app=app,
                target_element_query={"ax_role": "AXButton", "ax_title": "Directions"},
                fallback_screen_coords=(220.0, 180.0),
                spotlight_bounds=(160.0, 160.0, 120.0, 36.0),
                pre_delay_seconds=0.5,
                post_delay_seconds=1.0,
            ),
        ]

    @classmethod
    def _generate_calendar_steps(cls, query: str, app: str) -> List[WalkthroughStep]:
        return [
            WalkthroughStep(
                step_index=1,
                title="Open Calendar via Spotlight",
                instruction="Press ⌘ + Space (Command + Space) or click Calendar in the Dock.",
                explanation="Displays month, week, and day calendar schedules.",
                action_type=TeachingAction.DEMONSTRATE_HOTKEY,
                hotkey_combo=["cmd", "space"],
                target_app=app,
                fallback_screen_coords=(640.0, 280.0),
                spotlight_bounds=(400.0, 250.0, 480.0, 60.0),
                pre_delay_seconds=0.4,
                post_delay_seconds=0.8,
            ),
            WalkthroughStep(
                step_index=2,
                title="Create New Event",
                instruction="Click the '+' button in the top toolbar or press Cmd+N.",
                explanation="Opens the Quick Event creation dialog.",
                action_type=TeachingAction.DEMONSTRATE_CLICK,
                target_app=app,
                target_element_query={"ax_role": "AXButton", "ax_title": "New Event"},
                fallback_screen_coords=(180.0, 72.0),
                spotlight_bounds=(160.0, 56.0, 40.0, 32.0),
                pre_delay_seconds=0.5,
                post_delay_seconds=0.8,
            ),
            WalkthroughStep(
                step_index=3,
                title="Configure Event Schedule",
                instruction="Enter the event title, start time, end time, and calendar bucket.",
                explanation="Syncs event notifications across macOS and iCloud devices.",
                action_type=TeachingAction.MOVE_AND_HOVER,
                target_app=app,
                fallback_screen_coords=(460.0, 240.0),
                spotlight_bounds=(340.0, 160.0, 320.0, 220.0),
                pre_delay_seconds=0.5,
                post_delay_seconds=1.0,
            ),
        ]

    @classmethod
    def _generate_weather_steps(cls, query: str, app: str) -> List[WalkthroughStep]:
        return [
            WalkthroughStep(
                step_index=1,
                title="Open Weather via Spotlight",
                instruction="Press ⌘ + Space (Command + Space) or click Weather in Applications.",
                explanation="Displays current climate conditions, radar maps, and 10-day forecasts.",
                action_type=TeachingAction.DEMONSTRATE_HOTKEY,
                hotkey_combo=["cmd", "space"],
                target_app=app,
                fallback_screen_coords=(640.0, 280.0),
                spotlight_bounds=(400.0, 250.0, 480.0, 60.0),
                pre_delay_seconds=0.4,
                post_delay_seconds=0.8,
            ),
            WalkthroughStep(
                step_index=2,
                title="Select Location / Search",
                instruction="Click the Search bar or select a saved city in the sidebar.",
                explanation="Switches view to the requested geographic location.",
                action_type=TeachingAction.DEMONSTRATE_CLICK,
                target_app=app,
                target_element_query={"ax_role": "AXTextField", "ax_title": "Search"},
                fallback_screen_coords=(120.0, 76.0),
                spotlight_bounds=(40.0, 62.0, 160.0, 28.0),
                pre_delay_seconds=0.5,
                post_delay_seconds=0.8,
            ),
            WalkthroughStep(
                step_index=3,
                title="Review 10-Day Forecast",
                instruction="Scroll down the main window to inspect daily temperatures and precipitation chance.",
                explanation="Shows hourly temperature graphs and atmospheric predictions.",
                action_type=TeachingAction.PULSE_BEACON,
                target_app=app,
                fallback_screen_coords=(540.0, 380.0),
                spotlight_bounds=(340.0, 260.0, 480.0, 240.0),
                pre_delay_seconds=0.5,
                post_delay_seconds=1.0,
            ),
        ]

    @classmethod
    def _generate_activity_monitor_steps(cls, query: str, app: str) -> List[WalkthroughStep]:
        return [
            WalkthroughStep(
                step_index=1,
                title="Launch Activity Monitor via Spotlight",
                instruction="Press ⌘ + Space (Command + Space) or open Activity Monitor from Utilities.",
                explanation="Activity Monitor reveals real-time macOS resource utilization.",
                action_type=TeachingAction.DEMONSTRATE_HOTKEY,
                hotkey_combo=["cmd", "space"],
                target_app=app,
                fallback_screen_coords=(640.0, 280.0),
                spotlight_bounds=(400.0, 250.0, 480.0, 60.0),
                pre_delay_seconds=0.4,
                post_delay_seconds=0.8,
            ),
            WalkthroughStep(
                step_index=2,
                title="Switch Resource Tab",
                instruction="Click on 'CPU' or 'Memory' in the top segmented control.",
                explanation="Filters running processes by processor workload or RAM memory pressure.",
                action_type=TeachingAction.DEMONSTRATE_CLICK,
                target_app=app,
                target_element_query={"ax_role": "AXRadioButton", "ax_title": "CPU"},
                fallback_screen_coords=(220.0, 74.0),
                spotlight_bounds=(180.0, 60.0, 80.0, 28.0),
                pre_delay_seconds=0.5,
                post_delay_seconds=0.8,
            ),
            WalkthroughStep(
                step_index=3,
                title="Sort Processes by Utilization",
                instruction="Click the '% CPU' column header to sort heaviest consumer processes to the top.",
                explanation="Identifies resource-heavy applications draining power or memory.",
                action_type=TeachingAction.DEMONSTRATE_CLICK,
                target_app=app,
                target_element_query={"ax_role": "AXButton", "ax_title": "% CPU"},
                fallback_screen_coords=(380.0, 110.0),
                spotlight_bounds=(340.0, 98.0, 80.0, 24.0),
                pre_delay_seconds=0.5,
                post_delay_seconds=1.0,
            ),
        ]

    @classmethod
    def _generate_disk_utility_steps(cls, query: str, app: str) -> List[WalkthroughStep]:
        return [
            WalkthroughStep(
                step_index=1,
                title="Launch Disk Utility via Spotlight",
                instruction="Press ⌘ + Space (Command + Space) or open Disk Utility from Utilities.",
                explanation="Disk Utility manages APFS containers, partitions, and storage health.",
                action_type=TeachingAction.DEMONSTRATE_HOTKEY,
                hotkey_combo=["cmd", "space"],
                target_app=app,
                fallback_screen_coords=(640.0, 280.0),
                spotlight_bounds=(400.0, 250.0, 480.0, 60.0),
                pre_delay_seconds=0.4,
                post_delay_seconds=0.8,
            ),
            WalkthroughStep(
                step_index=2,
                title="Select Storage Volume",
                instruction="Click your Macintosh HD or external volume in the left sidebar hierarchy.",
                explanation="Selects the physical or APFS disk to inspect.",
                action_type=TeachingAction.DEMONSTRATE_CLICK,
                target_app=app,
                target_element_query={"ax_role": "AXRow", "ax_title": "Macintosh HD"},
                fallback_screen_coords=(120.0, 160.0),
                spotlight_bounds=(40.0, 130.0, 160.0, 36.0),
                pre_delay_seconds=0.5,
                post_delay_seconds=0.8,
            ),
            WalkthroughStep(
                step_index=3,
                title="Run First Aid",
                instruction="Click the 'First Aid' stethoscope button in the top toolbar.",
                explanation="Scans disk catalogs, directory structures, and verifies filesystem integrity.",
                action_type=TeachingAction.DEMONSTRATE_CLICK,
                target_app=app,
                target_element_query={"ax_role": "AXButton", "ax_title": "First Aid"},
                fallback_screen_coords=(460.0, 72.0),
                spotlight_bounds=(420.0, 56.0, 80.0, 32.0),
                pre_delay_seconds=0.5,
                post_delay_seconds=1.0,
            ),
        ]

    @classmethod
    def _generate_textedit_steps(cls, query: str, app: str) -> List[WalkthroughStep]:
        return [
            WalkthroughStep(
                step_index=1,
                title="Open TextEdit via Spotlight",
                instruction="Press ⌘ + Space (Command + Space) or launch TextEdit from Applications.",
                explanation="Standard macOS lightweight rich-text and plaintext editor.",
                action_type=TeachingAction.DEMONSTRATE_HOTKEY,
                hotkey_combo=["cmd", "space"],
                target_app=app,
                fallback_screen_coords=(640.0, 280.0),
                spotlight_bounds=(400.0, 250.0, 480.0, 60.0),
                pre_delay_seconds=0.4,
                post_delay_seconds=0.8,
            ),
            WalkthroughStep(
                step_index=2,
                title="Create New Document",
                instruction="Click 'New Document' in the open dialog or press Cmd+N.",
                explanation="Opens a clean text workspace.",
                action_type=TeachingAction.DEMONSTRATE_CLICK,
                target_app=app,
                target_element_query={"ax_role": "AXButton", "ax_title": "New Document"},
                fallback_screen_coords=(640.0, 520.0),
                spotlight_bounds=(580.0, 500.0, 120.0, 36.0),
                pre_delay_seconds=0.5,
                post_delay_seconds=0.8,
            ),
            WalkthroughStep(
                step_index=3,
                title="Type Text Content",
                instruction="Draft your document text and formatting inside the editor window.",
                explanation="Allows instant typing and macOS system spellcheck.",
                action_type=TeachingAction.DEMONSTRATE_TYPE,
                target_app=app,
                text_to_type="Sample drafted document generated by Clio Walkthrough.\n",
                fallback_screen_coords=(500.0, 300.0),
                spotlight_bounds=(300.0, 160.0, 480.0, 360.0),
                pre_delay_seconds=0.5,
                post_delay_seconds=1.0,
            ),
        ]

    @classmethod
    def _generate_system_information_steps(cls, query: str, app: str) -> List[WalkthroughStep]:
        return [
            WalkthroughStep(
                step_index=1,
                title="Open Apple Menu ",
                instruction="Click the Apple icon  in the top-left corner of your screen.",
                explanation="The Apple menu provides direct access to About This Mac and System Information.",
                action_type=TeachingAction.DEMONSTRATE_CLICK,
                target_app=app,
                target_element_query={"ax_role": "AXMenuBarItem", "ax_title": "Apple"},
                fallback_screen_coords=(18.0, 12.0),
                spotlight_bounds=(10.0, 2.0, 30.0, 20.0),
                pre_delay_seconds=0.4,
                post_delay_seconds=1.2,
            ),
            WalkthroughStep(
                step_index=2,
                title="Select About This Mac",
                instruction="Click 'About This Mac' from the Apple menu dropdown.",
                explanation="Opens the overview window showing your Mac model, chip, memory, and macOS version.",
                action_type=TeachingAction.DEMONSTRATE_CLICK,
                target_app=app,
                target_element_query={"ax_role": "AXMenuItem", "ax_title": "About This Mac"},
                fallback_screen_coords=(80.0, 40.0),
                spotlight_bounds=(6.0, 30.0, 220.0, 22.0),
                pre_delay_seconds=0.5,
                post_delay_seconds=1.2,
            ),
            WalkthroughStep(
                step_index=3,
                title="Click More Info...",
                instruction="Click the 'More Info...' button inside the About This Mac window.",
                explanation="Opens the detailed About section in System Settings with storage, display, and hardware specs.",
                action_type=TeachingAction.DEMONSTRATE_CLICK,
                target_app=app,
                target_element_query={"ax_role": "AXButton", "ax_title": "More Info..."},
                fallback_screen_coords=(640.0, 500.0),
                spotlight_bounds=(580.0, 480.0, 120.0, 32.0),
                pre_delay_seconds=0.5,
                post_delay_seconds=1.2,
            ),
            WalkthroughStep(
                step_index=4,
                title="Open System Report",
                instruction="Scroll down in About settings and click 'System Report...' to open full System Information.",
                explanation="Launches the complete System Information utility detailing hardware, network, and software diagnostics.",
                action_type=TeachingAction.DEMONSTRATE_CLICK,
                target_app=app,
                target_element_query={"ax_role": "AXButton", "ax_title": "System Report..."},
                fallback_screen_coords=(680.0, 720.0),
                spotlight_bounds=(610.0, 700.0, 140.0, 32.0),
                pre_delay_seconds=0.6,
                post_delay_seconds=1.4,
            ),
        ]

    @classmethod
    def _generate_settings_steps(cls, query: str, app: str) -> List[WalkthroughStep]:
        pane = "General"
        q_low = query.lower()
        if "sound" in q_low or "volume" in q_low:
            pane = "Sound"
        elif "bluetooth" in q_low:
            pane = "Bluetooth"
        elif "wifi" in q_low or "wi-fi" in q_low or "network" in q_low:
            pane = "Wi-Fi"
        elif "display" in q_low or "resolution" in q_low:
            pane = "Displays"
        elif "battery" in q_low or "energy" in q_low:
            pane = "Battery"
        elif "trackpad" in q_low or "mouse" in q_low:
            pane = "Trackpad"
        elif "wallpaper" in q_low:
            pane = "Wallpaper"
        elif "dark mode" in q_low or "appearance" in q_low:
            pane = "Appearance"

        return [
            WalkthroughStep(
                step_index=1,
                title="Open Apple Menu ",
                instruction="Click the Apple icon () in the top-left menu bar corner.",
                explanation="Access macOS system configuration preferences directly from the system menu.",
                action_type=TeachingAction.DEMONSTRATE_CLICK,
                target_app="System Settings",
                target_element_query={"ax_role": "AXMenuBarItem", "ax_title": "Apple"},
                fallback_screen_coords=(18.0, 12.0),
                spotlight_bounds=(10.0, 2.0, 30.0, 20.0),
                pre_delay_seconds=0.4,
                post_delay_seconds=0.8,
            ),
            WalkthroughStep(
                step_index=2,
                title="Select System Settings",
                instruction=f"Click 'System Settings...' in the Apple menu dropdown to open {pane} preferences.",
                explanation="Launches the macOS System Settings window.",
                action_type=TeachingAction.DEMONSTRATE_CLICK,
                target_app="System Settings",
                target_element_query={"ax_role": "AXMenuItem", "ax_title": "System Settings..."},
                fallback_screen_coords=(80.0, 86.0),
                spotlight_bounds=(6.0, 75.0, 220.0, 22.0),
                pre_delay_seconds=0.5,
                post_delay_seconds=0.8,
            ),
            WalkthroughStep(
                step_index=3,
                title=f"Select {pane} in Sidebar",
                instruction=f"Scroll through the left navigation pane and click '{pane}'.",
                explanation=f"Opens the '{pane}' preference panel.",
                action_type=TeachingAction.DEMONSTRATE_CLICK,
                target_app="System Settings",
                target_element_query={"ax_role": "AXRow", "ax_title": pane},
                fallback_screen_coords=(95.0, 260.0),
                spotlight_bounds=(20.0, 240.0, 150.0, 36.0),
                pre_delay_seconds=0.5,
                post_delay_seconds=0.8,
            ),
            WalkthroughStep(
                step_index=4,
                title=f"Adjust {pane} Settings",
                instruction=f"Configure options for '{query}' in the main details view.",
                explanation="Applies immediate preference adjustments to your macOS user profile.",
                action_type=TeachingAction.PULSE_BEACON,
                target_app="System Settings",
                fallback_screen_coords=(460.0, 280.0),
                spotlight_bounds=(320.0, 180.0, 360.0, 200.0),
                pre_delay_seconds=0.5,
                post_delay_seconds=1.0,
            ),
        ]

    @classmethod
    def _generate_finder_steps(cls, query: str, app: str) -> List[WalkthroughStep]:
        target_dir = "Desktop"
        q_low = query.lower()
        if "downloads" in q_low:
            target_dir = "Downloads"
        elif "documents" in q_low:
            target_dir = "Documents"
        elif "disk" in q_low:
            target_dir = "External Disk"

        return [
            WalkthroughStep(
                step_index=1,
                title="Focus Finder",
                instruction="Click anywhere on the Desktop background to make Finder the active application.",
                explanation="Focusing Finder activates file operations and Desktop menu bars.",
                action_type=TeachingAction.DEMONSTRATE_CLICK,
                target_app="Finder",
                fallback_screen_coords=(300.0, 300.0),
                spotlight_bounds=(100.0, 100.0, 400.0, 400.0),
                pre_delay_seconds=0.4,
                post_delay_seconds=0.8,
            ),
            WalkthroughStep(
                step_index=2,
                title=f"Navigate to {target_dir}",
                instruction=f"Open a new Finder window (Cmd+N) and select '{target_dir}'.",
                explanation=f"Displays file and folder contents located in '{target_dir}'.",
                action_type=TeachingAction.DEMONSTRATE_CLICK,
                target_app="Finder",
                target_element_query={"ax_role": "AXRow", "ax_title": target_dir},
                fallback_screen_coords=(90.0, 220.0),
                spotlight_bounds=(20.0, 200.0, 140.0, 36.0),
                pre_delay_seconds=0.5,
                post_delay_seconds=0.8,
            ),
            WalkthroughStep(
                step_index=3,
                title="Organize Files",
                instruction=f"Select or manage files according to: '{query}'.",
                explanation="Performs file manipulation (sorting, grouping, moving, or previewing).",
                action_type=TeachingAction.PULSE_BEACON,
                target_app="Finder",
                fallback_screen_coords=(440.0, 320.0),
                spotlight_bounds=(260.0, 180.0, 400.0, 300.0),
                pre_delay_seconds=0.5,
                post_delay_seconds=1.0,
            ),
        ]

    @classmethod
    def _generate_system_utility_steps(cls, query: str, app: str) -> List[WalkthroughStep]:
        return [
            WalkthroughStep(
                step_index=1,
                title="Activate System Shortcut",
                instruction=f"Invoke system utility for '{query}'.",
                explanation="macOS system shortcuts provide immediate desktop navigation.",
                action_type=TeachingAction.DEMONSTRATE_HOTKEY,
                hotkey_combo=["command", "space"],
                target_app="SystemUIServer",
                fallback_screen_coords=(640.0, 300.0),
                spotlight_bounds=(440.0, 260.0, 400.0, 70.0),
                pre_delay_seconds=0.4,
                post_delay_seconds=0.8,
            ),
            WalkthroughStep(
                step_index=2,
                title="Perform Desktop Action",
                instruction=f"Follow on-screen instructions for '{query}'.",
                explanation="Clio highlights the active control area.",
                action_type=TeachingAction.PULSE_BEACON,
                target_app="SystemUIServer",
                fallback_screen_coords=(640.0, 350.0),
                spotlight_bounds=(400.0, 280.0, 480.0, 140.0),
                pre_delay_seconds=0.5,
                post_delay_seconds=1.0,
            ),
        ]

    @classmethod
    def _generate_general_app_steps(cls, query: str, app: str) -> List[WalkthroughStep]:
        return [
            WalkthroughStep(
                step_index=1,
                title=f"Open {app} via Spotlight",
                instruction=f"Press ⌘ + Space (Command + Space) to search for and launch {app}.",
                explanation=f"Brings {app} into active foreground focus.",
                action_type=TeachingAction.DEMONSTRATE_HOTKEY,
                hotkey_combo=["cmd", "space"],
                target_app=app,
                fallback_screen_coords=(640.0, 280.0),
                spotlight_bounds=(400.0, 250.0, 480.0, 60.0),
                pre_delay_seconds=0.4,
                post_delay_seconds=0.8,
            ),
            WalkthroughStep(
                step_index=2,
                title="Locate Navigation Option",
                instruction=f"Look for the relevant setting or action for '{query}'.",
                explanation=f"Guides focus toward key interactive controls in {app}.",
                action_type=TeachingAction.MOVE_AND_HOVER,
                target_app=app,
                fallback_screen_coords=(380.0, 240.0),
                spotlight_bounds=(280.0, 180.0, 240.0, 120.0),
                pre_delay_seconds=0.5,
                post_delay_seconds=0.8,
            ),
            WalkthroughStep(
                step_index=3,
                title="Complete Action",
                instruction=f"Perform configuration or task: '{query}'.",
                explanation="Applies desired action in the active application.",
                action_type=TeachingAction.PULSE_BEACON,
                target_app=app,
                fallback_screen_coords=(520.0, 340.0),
                spotlight_bounds=(420.0, 280.0, 220.0, 120.0),
                pre_delay_seconds=0.5,
                post_delay_seconds=1.0,
            ),
        ]

    @classmethod
    def _generate_cursor_navigation_steps(cls, query: str, app: str) -> List[WalkthroughStep]:
        return [
            WalkthroughStep(
                step_index=1,
                title="Center Mouse Pointer",
                instruction="Move the mouse pointer to the center of your screen display.",
                explanation="Centering the cursor establishes a neutral reference point for screen navigation.",
                action_type=TeachingAction.MOVE_AND_HOVER,
                target_app=app,
                fallback_screen_coords=(640.0, 400.0),
                spotlight_bounds=(600.0, 360.0, 80.0, 80.0),
                pre_delay_seconds=0.4,
                post_delay_seconds=0.8,
            ),
            WalkthroughStep(
                step_index=2,
                title="Navigate to Top Menu Bar",
                instruction="Move the cursor smoothly to the top macOS menu bar.",
                explanation="The menu bar contains Apple menu controls, application menus, and menu bar extras.",
                action_type=TeachingAction.MOVE_AND_HOVER,
                target_app=app,
                fallback_screen_coords=(120.0, 12.0),
                spotlight_bounds=(10.0, 0.0, 240.0, 24.0),
                pre_delay_seconds=0.5,
                post_delay_seconds=0.8,
            ),
            WalkthroughStep(
                step_index=3,
                title="Move Pointer Toward Dock",
                instruction="Move cursor down toward the bottom of your screen to inspect Dock applications.",
                explanation="Hovering over Dock items reveals application titles and active indicators.",
                action_type=TeachingAction.MOVE_AND_HOVER,
                target_app=app,
                fallback_screen_coords=(640.0, 760.0),
                spotlight_bounds=(480.0, 720.0, 320.0, 70.0),
                pre_delay_seconds=0.5,
                post_delay_seconds=0.8,
            ),
            WalkthroughStep(
                step_index=4,
                title="Target Lock Beacon",
                instruction="Observe the glowing beacon confirming accurate cursor lock.",
                explanation="Clio emits a visual beacon ring to confirm the pointer has accurately reached its target.",
                action_type=TeachingAction.PULSE_BEACON,
                target_app=app,
                fallback_screen_coords=(640.0, 760.0),
                spotlight_bounds=(600.0, 720.0, 80.0, 80.0),
                pre_delay_seconds=0.5,
                post_delay_seconds=1.0,
            ),
        ]

    @classmethod
    def _generate_window_management_steps(cls, query: str, app: str) -> List[WalkthroughStep]:
        return [
            WalkthroughStep(
                step_index=1,
                title="Cycle Windows of Active App",
                instruction="Press ⌘ + ` (Command + Backtick) to switch between open windows of the active application.",
                explanation="Quickly alternates between multiple document or browser windows within the same app.",
                action_type=TeachingAction.DEMONSTRATE_HOTKEY,
                hotkey_combo=["cmd", "`"],
                target_app=app,
                fallback_screen_coords=(640.0, 400.0),
                spotlight_bounds=(200.0, 150.0, 880.0, 500.0),
                pre_delay_seconds=0.4,
                post_delay_seconds=0.8,
            ),
            WalkthroughStep(
                step_index=2,
                title="Switch Between Applications",
                instruction="Press ⌘ + Tab (Command + Tab) to switch to your most recently used application.",
                explanation="Holding Command while tapping Tab opens the App Switcher to jump between running apps.",
                action_type=TeachingAction.DEMONSTRATE_HOTKEY,
                hotkey_combo=["cmd", "tab"],
                target_app=app,
                fallback_screen_coords=(640.0, 400.0),
                spotlight_bounds=(300.0, 350.0, 680.0, 100.0),
                pre_delay_seconds=0.5,
                post_delay_seconds=0.8,
            ),
            WalkthroughStep(
                step_index=3,
                title="Swipe Between Desktops & Spaces",
                instruction="Swipe horizontally with three fingers on your trackpad or press Control + Right Arrow.",
                explanation="Glides seamlessly between macOS virtual desktops and full-screen applications.",
                action_type=TeachingAction.DEMONSTRATE_HOTKEY,
                hotkey_combo=["ctrl", "right"],
                target_app=app,
                fallback_screen_coords=(640.0, 400.0),
                spotlight_bounds=(100.0, 100.0, 1080.0, 600.0),
                pre_delay_seconds=0.5,
                post_delay_seconds=0.8,
            ),
            WalkthroughStep(
                step_index=4,
                title="Open Mission Control",
                instruction="Swipe up with three fingers on trackpad or press Control + Up Arrow.",
                explanation="Displays a panoramic overview of all open windows, workspaces, and full-screen apps.",
                action_type=TeachingAction.DEMONSTRATE_HOTKEY,
                hotkey_combo=["ctrl", "up"],
                target_app=app,
                fallback_screen_coords=(640.0, 200.0),
                spotlight_bounds=(100.0, 50.0, 1080.0, 700.0),
                pre_delay_seconds=0.5,
                post_delay_seconds=1.0,
            ),
        ]

    @classmethod
    def _generate_button_click_steps(cls, query: str, app: str) -> List[WalkthroughStep]:
        return [
            WalkthroughStep(
                step_index=1,
                title="Hover Over Target Button",
                instruction="Move your cursor directly over the clickable button or control.",
                explanation="Buttons respond with hover states and tooltips indicating their primary action.",
                action_type=TeachingAction.MOVE_AND_HOVER,
                target_app=app,
                fallback_screen_coords=(640.0, 450.0),
                spotlight_bounds=(580.0, 430.0, 120.0, 40.0),
                pre_delay_seconds=0.4,
                post_delay_seconds=0.8,
            ),
            WalkthroughStep(
                step_index=2,
                title="Perform Primary Left Click",
                instruction="Click the left mouse button (or press down on trackpad) to trigger the action.",
                explanation="The primary click selects items, confirms dialogs, and activates buttons.",
                action_type=TeachingAction.DEMONSTRATE_CLICK,
                target_app=app,
                fallback_screen_coords=(640.0, 450.0),
                spotlight_bounds=(580.0, 430.0, 120.0, 40.0),
                pre_delay_seconds=0.5,
                post_delay_seconds=0.8,
            ),
            WalkthroughStep(
                step_index=3,
                title="Perform Contextual Right Click",
                instruction="Click with two fingers on trackpad or hold Control and click to open the context menu.",
                explanation="Reveals context-sensitive menus and secondary options for the clicked element.",
                action_type=TeachingAction.DEMONSTRATE_RIGHT_CLICK,
                target_app=app,
                fallback_screen_coords=(640.0, 450.0),
                spotlight_bounds=(580.0, 430.0, 180.0, 140.0),
                pre_delay_seconds=0.5,
                post_delay_seconds=0.8,
            ),
            WalkthroughStep(
                step_index=4,
                title="Execute Double Click",
                instruction="Click twice rapidly to trigger default document opening or word selection.",
                explanation="Double clicking activates items or expands selection without opening a menu.",
                action_type=TeachingAction.DEMONSTRATE_DOUBLE_CLICK,
                target_app=app,
                fallback_screen_coords=(640.0, 450.0),
                spotlight_bounds=(580.0, 430.0, 120.0, 40.0),
                pre_delay_seconds=0.5,
                post_delay_seconds=1.0,
            ),
        ]
