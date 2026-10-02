"""Built-in Deterministic Walkthrough Catalog for Core macOS Tasks (Tier 1).

Path: src/walkthrough/templates.py
Provides pre-authored, high-precision walkthrough plans for top macOS tasks.
Executes with zero AI API token consumption and sub-millisecond lookup latency.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional
import uuid

from src.walkthrough.models import (
    TeachingAction,
    WalkthroughMode,
    WalkthroughPlan,
    WalkthroughStep,
)


class BuiltinWalkthroughCatalog:
    """Repository of high-precision built-in macOS walkthrough templates."""

    @classmethod
    def get_all_templates(cls) -> List[WalkthroughPlan]:
        """Returns all pre-authored deterministic walkthrough plans."""
        return [
            cls._create_dark_mode_template(),
            cls._create_hot_corners_template(),
            cls._create_finder_new_folder_template(),
            cls._create_safari_bookmark_template(),
            cls._create_screenshot_utility_template(),
            cls._create_dock_magnification_template(),
        ]

    @classmethod
    def find_match(cls, query: str) -> Optional[WalkthroughPlan]:
        """Matches a user query against the template catalog using regex & keyword semantics."""
        q = re.sub(r"\s+", " ", query.strip().lower())
        # Strip common conversational phrases
        clean = re.sub(
            r"^(?:please\s+)?(?:can\s+you\s+)?(?:teach|show|guide|walk)\s+(?:me\s+)?(?:how\s+to\s+|through\s+)?",
            "",
            q,
        ).strip()
        clean = re.sub(r"^(?:how\s+(?:do|can)\s+i\s+|tutorial\s+on\s+)", "", clean).strip()

        # 1. Dark Mode / Appearance
        if any(kw in clean for kw in ("dark mode", "light mode", "appearance", "toggle dark", "turn on dark mode", "switch to dark mode")):
            return cls._create_dark_mode_template()

        # 2. Hot Corners
        if any(kw in clean for kw in ("hot corner", "hot corners", "corner shortcut")):
            return cls._create_hot_corners_template()

        # 3. Finder New Folder
        if any(kw in clean for kw in ("new folder", "create a folder", "make a folder", "create folder")):
            return cls._create_finder_new_folder_template()

        # 4. Safari Bookmark
        if any(kw in clean for kw in ("bookmark", "add bookmark", "save bookmark", "bookmark page")):
            return cls._create_safari_bookmark_template()

        # 5. Screenshot / Screen Recording
        if any(kw in clean for kw in ("screenshot", "screen capture", "take a screenshot", "snip screen", "record screen")):
            return cls._create_screenshot_utility_template()

        # 6. Dock Magnification / Dock Size
        if any(kw in clean for kw in ("dock size", "magnification", "dock magnification", "resize dock")):
            return cls._create_dock_magnification_template()

        return None

    # =========================================================================
    # Template Authors
    # =========================================================================

    @classmethod
    def _create_dark_mode_template(cls) -> WalkthroughPlan:
        return WalkthroughPlan(
            walkthrough_id=f"wt_dark_mode_{uuid.uuid4().hex[:6]}",
            goal="Toggle Dark Mode in macOS",
            summary="Demonstrates how to open System Settings, navigate to Appearance, and toggle between Light and Dark mode.",
            target_app="System Settings",
            mode=WalkthroughMode.GUIDED_DEMO,
            source_tier="TIER_1_LOCAL_TEMPLATE",
            steps=[
                WalkthroughStep(
                    step_index=1,
                    title="Open Apple Menu",
                    instruction="Click the Apple icon  in the top-left corner of your screen.",
                    explanation="The Apple menu provides quick access to system-wide controls and System Settings.",
                    action_type=TeachingAction.DEMONSTRATE_CLICK,
                    target_app="ControlCenter",
                    target_element_query={"ax_role": "AXMenuBarItem", "ax_title": "Apple"},
                    fallback_screen_coords=(18.0, 12.0),
                    spotlight_bounds=(10.0, 2.0, 30.0, 20.0),
                    pre_delay_seconds=0.4,
                    post_delay_seconds=0.8,
                ),
                WalkthroughStep(
                    step_index=2,
                    title="Select System Settings",
                    instruction="Click on 'System Settings...' from the dropdown menu.",
                    explanation="Opens the macOS configuration center.",
                    action_type=TeachingAction.DEMONSTRATE_CLICK,
                    target_app="ControlCenter",
                    target_element_query={"ax_role": "AXMenuItem", "ax_title": "System Settings..."},
                    fallback_screen_coords=(45.0, 60.0),
                    spotlight_bounds=(12.0, 48.0, 150.0, 24.0),
                    pre_delay_seconds=0.5,
                    post_delay_seconds=1.0,
                ),
                WalkthroughStep(
                    step_index=3,
                    title="Navigate to Appearance",
                    instruction="Click 'Appearance' in the left-hand sidebar.",
                    explanation="Appearance settings control macOS accent colors, window themes, and Dark Mode.",
                    action_type=TeachingAction.DEMONSTRATE_CLICK,
                    target_app="System Settings",
                    target_element_query={"ax_role": "AXRow", "ax_title": "Appearance"},
                    fallback_screen_coords=(100.0, 200.0),
                    spotlight_bounds=(30.0, 185.0, 140.0, 30.0),
                    pre_delay_seconds=0.6,
                    post_delay_seconds=0.8,
                ),
                WalkthroughStep(
                    step_index=4,
                    title="Select Dark Mode",
                    instruction="Click the 'Dark' theme thumbnail to switch your macOS interface.",
                    explanation="You can also select 'Auto' to transition between Light and Dark mode based on the time of day.",
                    action_type=TeachingAction.PULSE_BEACON,
                    target_app="System Settings",
                    target_element_query={"ax_role": "AXRadioButton", "ax_title": "Dark"},
                    fallback_screen_coords=(360.0, 150.0),
                    spotlight_bounds=(320.0, 120.0, 80.0, 60.0),
                    pre_delay_seconds=0.6,
                    post_delay_seconds=1.2,
                ),
            ],
        )

    @classmethod
    def _create_hot_corners_template(cls) -> WalkthroughPlan:
        return WalkthroughPlan(
            walkthrough_id=f"wt_hot_corners_{uuid.uuid4().hex[:6]}",
            goal="Configure Hot Corners in macOS",
            summary="Shows how to navigate to Desktop & Dock settings and customize Hot Corner triggers.",
            target_app="System Settings",
            mode=WalkthroughMode.GUIDED_DEMO,
            source_tier="TIER_1_LOCAL_TEMPLATE",
            steps=[
                WalkthroughStep(
                    step_index=1,
                    title="Launch System Settings",
                    instruction="Open System Settings from your Dock or Apple menu.",
                    explanation="System Settings houses your display, dock, and accessibility options.",
                    action_type=TeachingAction.DEMONSTRATE_CLICK,
                    target_app="System Settings",
                    fallback_screen_coords=(18.0, 12.0),
                    pre_delay_seconds=0.4,
                    post_delay_seconds=0.8,
                ),
                WalkthroughStep(
                    step_index=2,
                    title="Open Desktop & Dock",
                    instruction="Click 'Desktop & Dock' in the sidebar.",
                    explanation="Here you can configure your Stage Manager, widgets, windows, and Hot Corners.",
                    action_type=TeachingAction.DEMONSTRATE_CLICK,
                    target_app="System Settings",
                    target_element_query={"ax_role": "AXRow", "ax_title": "Desktop & Dock"},
                    fallback_screen_coords=(100.0, 280.0),
                    spotlight_bounds=(30.0, 265.0, 140.0, 30.0),
                    pre_delay_seconds=0.5,
                    post_delay_seconds=0.8,
                ),
                WalkthroughStep(
                    step_index=3,
                    title="Scroll to Hot Corners",
                    instruction="Scroll down to the bottom and click the 'Hot Corners...' button.",
                    explanation="Hot Corners allow moving your mouse into any corner to trigger Mission Control, Lock Screen, or Quick Note.",
                    action_type=TeachingAction.DEMONSTRATE_CLICK,
                    target_app="System Settings",
                    target_element_query={"ax_role": "AXButton", "ax_title": "Hot Corners..."},
                    fallback_screen_coords=(580.0, 520.0),
                    spotlight_bounds=(510.0, 505.0, 140.0, 30.0),
                    pre_delay_seconds=0.6,
                    post_delay_seconds=1.0,
                ),
                WalkthroughStep(
                    step_index=4,
                    title="Configure Corner Action",
                    instruction="Click any corner dropdown to assign an instant shortcut action.",
                    explanation="Hold modifier keys (Command, Option, Shift) while selecting to require them before triggering.",
                    action_type=TeachingAction.PULSE_BEACON,
                    target_app="System Settings",
                    fallback_screen_coords=(450.0, 300.0),
                    spotlight_bounds=(380.0, 280.0, 140.0, 36.0),
                    pre_delay_seconds=0.6,
                    post_delay_seconds=1.2,
                ),
            ],
        )

    @classmethod
    def _create_finder_new_folder_template(cls) -> WalkthroughPlan:
        return WalkthroughPlan(
            walkthrough_id=f"wt_new_folder_{uuid.uuid4().hex[:6]}",
            goal="Create a New Folder in Finder",
            summary="Demonstrates creating and organizing folders using Finder menus and keyboard shortcuts.",
            target_app="Finder",
            mode=WalkthroughMode.GUIDED_DEMO,
            source_tier="TIER_1_LOCAL_TEMPLATE",
            steps=[
                WalkthroughStep(
                    step_index=1,
                    title="Focus Finder",
                    instruction="Click your desktop or Finder icon in the Dock.",
                    explanation="Brings Finder and its top menu bar into active focus.",
                    action_type=TeachingAction.DEMONSTRATE_CLICK,
                    target_app="Finder",
                    fallback_screen_coords=(40.0, 750.0),
                    pre_delay_seconds=0.3,
                    post_delay_seconds=0.6,
                ),
                WalkthroughStep(
                    step_index=2,
                    title="Click File Menu",
                    instruction="Click 'File' in the menu bar at the top of your screen.",
                    explanation="The File menu contains folder creation, new window, and file tagging actions.",
                    action_type=TeachingAction.DEMONSTRATE_CLICK,
                    target_app="Finder",
                    target_element_query={"ax_role": "AXMenuBarItem", "ax_title": "File"},
                    fallback_screen_coords=(90.0, 12.0),
                    spotlight_bounds=(75.0, 2.0, 35.0, 20.0),
                    pre_delay_seconds=0.4,
                    post_delay_seconds=0.6,
                ),
                WalkthroughStep(
                    step_index=3,
                    title="Choose New Folder",
                    instruction="Click 'New Folder' (or press ⇧ + ⌘ + N on your keyboard).",
                    explanation="Creates an untitled folder ready for renaming.",
                    action_type=TeachingAction.DEMONSTRATE_CLICK,
                    target_app="Finder",
                    target_element_query={"ax_role": "AXMenuItem", "ax_title": "New Folder"},
                    hotkey_combo=["shift", "cmd", "n"],
                    fallback_screen_coords=(110.0, 35.0),
                    spotlight_bounds=(85.0, 25.0, 140.0, 24.0),
                    pre_delay_seconds=0.4,
                    post_delay_seconds=0.8,
                ),
                WalkthroughStep(
                    step_index=4,
                    title="Rename Your Folder",
                    instruction="Type a name for your new folder and press Return ↵.",
                    explanation="Organizing files into clearly named folders keeps your desktop clean.",
                    action_type=TeachingAction.DEMONSTRATE_TYPE,
                    target_app="Finder",
                    text_to_type="Projects",
                    fallback_screen_coords=(400.0, 300.0),
                    pre_delay_seconds=0.5,
                    post_delay_seconds=1.0,
                ),
            ],
        )

    @classmethod
    def _create_safari_bookmark_template(cls) -> WalkthroughPlan:
        return WalkthroughPlan(
            walkthrough_id=f"wt_safari_bookmark_{uuid.uuid4().hex[:6]}",
            goal="Add a Bookmark in Safari",
            summary="Demonstrates how to bookmark the active website for instant future access.",
            target_app="Safari",
            mode=WalkthroughMode.GUIDED_DEMO,
            source_tier="TIER_1_LOCAL_TEMPLATE",
            steps=[
                WalkthroughStep(
                    step_index=1,
                    title="Activate Safari",
                    instruction="Ensure your Safari browser window is frontmost.",
                    explanation="Clio will bookmark whatever webpage you currently have open.",
                    action_type=TeachingAction.MOVE_AND_HOVER,
                    target_app="Safari",
                    fallback_screen_coords=(500.0, 200.0),
                    pre_delay_seconds=0.3,
                    post_delay_seconds=0.5,
                ),
                WalkthroughStep(
                    step_index=2,
                    title="Open Bookmarks Menu",
                    instruction="Click 'Bookmarks' in the top menu bar.",
                    explanation="You can also press ⌘ + D directly from any webpage.",
                    action_type=TeachingAction.DEMONSTRATE_CLICK,
                    target_app="Safari",
                    target_element_query={"ax_role": "AXMenuBarItem", "ax_title": "Bookmarks"},
                    fallback_screen_coords=(220.0, 12.0),
                    spotlight_bounds=(195.0, 2.0, 75.0, 20.0),
                    hotkey_combo=["cmd", "d"],
                    pre_delay_seconds=0.4,
                    post_delay_seconds=0.6,
                ),
                WalkthroughStep(
                    step_index=3,
                    title="Click Add Bookmark",
                    instruction="Click 'Add Bookmark...' from the dropdown.",
                    explanation="A sheet will appear allowing you to select a folder (Favorites or Reading List).",
                    action_type=TeachingAction.DEMONSTRATE_CLICK,
                    target_app="Safari",
                    target_element_query={"ax_role": "AXMenuItem", "ax_title": "Add Bookmark..."},
                    fallback_screen_coords=(235.0, 35.0),
                    spotlight_bounds=(200.0, 25.0, 150.0, 24.0),
                    pre_delay_seconds=0.4,
                    post_delay_seconds=0.8,
                ),
                WalkthroughStep(
                    step_index=4,
                    title="Confirm Bookmark",
                    instruction="Click 'Add' to save the page into your Favorites bar.",
                    explanation="The site will now appear on your new tab start page.",
                    action_type=TeachingAction.PULSE_BEACON,
                    target_app="Safari",
                    target_element_query={"ax_role": "AXButton", "ax_title": "Add"},
                    fallback_screen_coords=(600.0, 220.0),
                    spotlight_bounds=(560.0, 205.0, 80.0, 30.0),
                    pre_delay_seconds=0.5,
                    post_delay_seconds=1.0,
                ),
            ],
        )

    @classmethod
    def _create_screenshot_utility_template(cls) -> WalkthroughPlan:
        return WalkthroughPlan(
            walkthrough_id=f"wt_screenshot_{uuid.uuid4().hex[:6]}",
            goal="Take a Screenshot or Screen Recording",
            summary="Demonstrates the macOS Screenshot HUD utility and hotkeys.",
            target_app="SystemUIServer",
            mode=WalkthroughMode.GUIDED_DEMO,
            source_tier="TIER_1_LOCAL_TEMPLATE",
            steps=[
                WalkthroughStep(
                    step_index=1,
                    title="Press Screenshot Hotkey",
                    instruction="Press ⇧ + ⌘ + 5 (Shift + Command + 5) together.",
                    explanation="Invokes the full macOS Screenshot and Screen Recording HUD overlay.",
                    action_type=TeachingAction.DEMONSTRATE_HOTKEY,
                    hotkey_combo=["shift", "cmd", "5"],
                    fallback_screen_coords=(640.0, 680.0),
                    pre_delay_seconds=0.4,
                    post_delay_seconds=0.8,
                ),
                WalkthroughStep(
                    step_index=2,
                    title="Choose Capture Mode",
                    instruction="Select 'Capture Entire Screen', 'Capture Selected Window', or 'Capture Selected Portion'.",
                    explanation="You can also choose to record video of your entire display or a cropped box.",
                    action_type=TeachingAction.PULSE_BEACON,
                    fallback_screen_coords=(550.0, 720.0),
                    spotlight_bounds=(480.0, 705.0, 200.0, 35.0),
                    pre_delay_seconds=0.5,
                    post_delay_seconds=1.0,
                ),
                WalkthroughStep(
                    step_index=3,
                    title="Click Capture",
                    instruction="Click 'Capture' to take the picture, or press Return ↵.",
                    explanation="The thumbnail appears in the bottom right corner, saving automatically to Desktop.",
                    action_type=TeachingAction.DEMONSTRATE_CLICK,
                    fallback_screen_coords=(720.0, 720.0),
                    spotlight_bounds=(680.0, 705.0, 70.0, 35.0),
                    pre_delay_seconds=0.5,
                    post_delay_seconds=1.0,
                ),
            ],
        )

    @classmethod
    def _create_dock_magnification_template(cls) -> WalkthroughPlan:
        return WalkthroughPlan(
            walkthrough_id=f"wt_dock_mag_{uuid.uuid4().hex[:6]}",
            goal="Enable Dock Magnification in macOS",
            summary="Demonstrates how to turn on the classic icon magnification effect when hovering over the Dock.",
            target_app="System Settings",
            mode=WalkthroughMode.GUIDED_DEMO,
            source_tier="TIER_1_LOCAL_TEMPLATE",
            steps=[
                WalkthroughStep(
                    step_index=1,
                    title="Open System Settings",
                    instruction="Launch System Settings from Apple Menu > System Settings.",
                    explanation="All dock visual styles and animations are managed in Desktop & Dock settings.",
                    action_type=TeachingAction.DEMONSTRATE_CLICK,
                    target_app="System Settings",
                    fallback_screen_coords=(18.0, 12.0),
                    pre_delay_seconds=0.4,
                    post_delay_seconds=0.8,
                ),
                WalkthroughStep(
                    step_index=2,
                    title="Open Desktop & Dock",
                    instruction="Click 'Desktop & Dock' in the settings sidebar.",
                    explanation="Controls Dock sizing, position on screen, and autohide behavior.",
                    action_type=TeachingAction.DEMONSTRATE_CLICK,
                    target_app="System Settings",
                    target_element_query={"ax_role": "AXRow", "ax_title": "Desktop & Dock"},
                    fallback_screen_coords=(100.0, 280.0),
                    spotlight_bounds=(30.0, 265.0, 140.0, 30.0),
                    pre_delay_seconds=0.5,
                    post_delay_seconds=0.8,
                ),
                WalkthroughStep(
                    step_index=3,
                    title="Toggle Magnification",
                    instruction="Switch on the 'Magnification' checkbox toggle.",
                    explanation="Icons under your cursor will smoothly swell as you move across the Dock.",
                    action_type=TeachingAction.DEMONSTRATE_CLICK,
                    target_app="System Settings",
                    target_element_query={"ax_role": "AXCheckBox", "ax_title": "Magnification"},
                    fallback_screen_coords=(520.0, 200.0),
                    spotlight_bounds=(480.0, 190.0, 120.0, 24.0),
                    pre_delay_seconds=0.5,
                    post_delay_seconds=1.0,
                ),
            ],
        )
