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
            cls._create_wallpaper_template(),
            cls._create_sound_template(),
            cls._create_wifi_template(),
            cls._create_bluetooth_template(),
            cls._create_split_screen_template(),
            cls._create_force_quit_template(),
            cls._create_trash_template(),
            cls._create_do_not_disturb_template(),
            cls._create_spotlight_template(),
        ]

    @classmethod
    def find_match(cls, query: str) -> Optional[WalkthroughPlan]:
        """Matches a user query against the template catalog using regex & keyword semantics."""
        q = re.sub(r"\s+", " ", query.strip().lower())
        # Strip common conversational phrases
        clean = re.sub(
            r"^(?:please\s+)?(?:can\s+you\s+)?(?:teach|show|guide|walk|help)\s+(?:me\s+)?(?:how\s+to\s+|through\s+|navigate\s+to\s+(?:the\s+)?|to\s+)?",
            "",
            q,
        ).strip()
        clean = re.sub(r"^(?:how\s+(?:do|can)\s+i\s+|tutorial\s+on\s+|navigate\s+to\s+(?:the\s+)?)", "", clean).strip()

        if not clean:
            return None

        def _has(kw: str) -> bool:
            if " " in kw or "-" in kw:
                return kw in clean
            return bool(re.search(rf"\b{re.escape(kw)}\b", clean))

        # 1. Dark Mode / Appearance
        if any(_has(kw) for kw in ("dark mode", "light mode", "appearance", "toggle dark", "turn on dark mode", "switch to dark mode")):
            return cls._create_dark_mode_template()

        # 2. Hot Corners
        if any(_has(kw) for kw in ("hot corner", "hot corners", "corner shortcut")):
            return cls._create_hot_corners_template()

        # 3. Finder New Folder
        if any(_has(kw) for kw in ("new folder", "create a folder", "make a folder", "create folder")):
            return cls._create_finder_new_folder_template()

        # 4. Safari Bookmark
        if any(_has(kw) for kw in ("bookmark", "add bookmark", "save bookmark", "bookmark page")):
            return cls._create_safari_bookmark_template()

        # 5. Screenshot / Screen Recording
        if any(_has(kw) for kw in ("screenshot", "screen capture", "take a screenshot", "snip screen", "record screen")):
            return cls._create_screenshot_utility_template()

        # 6. Dock Magnification / Dock Size
        if any(_has(kw) for kw in ("dock size", "magnification", "dock magnification", "resize dock")):
            return cls._create_dock_magnification_template()

        # 7. Wallpaper / Desktop Background
        if any(_has(kw) for kw in ("wallpaper", "change wallpaper", "desktop background")):
            return cls._create_wallpaper_template()

        # 8. Sound / Audio Output / Volume
        if any(_has(kw) for kw in ("volume", "audio output", "sound")):
            return cls._create_sound_template()

        # 9. Wi-Fi
        if any(_has(kw) for kw in ("wifi", "connect to wifi", "wi-fi")):
            return cls._create_wifi_template()

        # 10. Bluetooth
        if any(_has(kw) for kw in ("bluetooth", "pair device")):
            return cls._create_bluetooth_template()

        # 11. Split Screen / Tile Window
        if any(_has(kw) for kw in ("split screen", "tile window")):
            return cls._create_split_screen_template()

        # 12. Force Quit / Unresponsive App
        if any(_has(kw) for kw in ("force quit", "unresponsive app")):
            return cls._create_force_quit_template()

        # 13. Empty Trash / Clear Trash
        if any(_has(kw) for kw in ("empty trash", "clear trash")):
            return cls._create_trash_template()

        # 14. Do Not Disturb / Focus Mode
        if any(_has(kw) for kw in ("do not disturb", "focus mode")):
            return cls._create_do_not_disturb_template()

        # 15. System Information / About This Mac / System Report
        if any(_has(kw) for kw in ("system information", "system info", "about this mac", "system report", "mac specs", "hardware info")):
            return cls._create_system_information_template()

        # 16. Spotlight / Command Bar / Search
        if any(_has(kw) for kw in ("spotlight", "command bar", "open command bar", "use command bar", "spotlight search", "search mac", "search", "how to search", "search files", "search web", "finder search")):
            return cls._create_spotlight_template()

        # 17. Window Switching & Moving Between Windows
        if any(_has(kw) for kw in (
            "move between windows",
            "move between window",
            "move between the windows",
            "move between the window",
            "swipe between the window",
            "swipe window",
            "swipe windows",
            "switch window",
            "switch windows",
            "switch between windows",
            "switch between window",
            "cycle windows",
            "window switch",
            "swipe between windows",
            "swipe desktops",
            "mission control",
        )):
            return cls._create_window_switching_template()

        # 18. Cursor Navigation / Move Cursor
        if any(_has(kw) for kw in ("move cursor", "move mouse", "cursor", "navigate cursor", "cursor navigation", "pointer", "point cursor")):
            return cls._create_cursor_navigation_template()

        # 19. Button Clicking & Controls
        if any(_has(kw) for kw in ("click button", "click buttons", "click", "double click", "right click", "how to click", "press button")):
            return cls._create_button_click_template()

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
                    fallback_screen_coords=(80.0, 86.0),
                    spotlight_bounds=(6.0, 75.0, 220.0, 22.0),
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

    @classmethod
    def _create_wallpaper_template(cls) -> WalkthroughPlan:
        return WalkthroughPlan(
            walkthrough_id=f"wt_wallpaper_{uuid.uuid4().hex[:6]}",
            goal="Change Desktop Wallpaper",
            summary="Demonstrates how to change desktop background pictures in macOS System Settings.",
            target_app="System Settings",
            mode=WalkthroughMode.GUIDED_DEMO,
            source_tier="TIER_1_LOCAL_TEMPLATE",
            steps=[
                WalkthroughStep(
                    step_index=1,
                    title="Open System Settings",
                    instruction="Open System Settings from Apple Menu > System Settings.",
                    explanation="Displays all macOS preferences.",
                    action_type=TeachingAction.DEMONSTRATE_CLICK,
                    target_app="System Settings",
                    fallback_screen_coords=(18.0, 12.0),
                    pre_delay_seconds=0.4,
                    post_delay_seconds=0.8,
                ),
                WalkthroughStep(
                    step_index=2,
                    title="Select Wallpaper",
                    instruction="Click 'Wallpaper' in the left sidebar navigation.",
                    explanation="Opens background images and dynamic desktop choices.",
                    action_type=TeachingAction.DEMONSTRATE_CLICK,
                    target_app="System Settings",
                    target_element_query={"ax_role": "AXRow", "ax_title": "Wallpaper"},
                    fallback_screen_coords=(100.0, 320.0),
                    spotlight_bounds=(20.0, 300.0, 140.0, 36.0),
                    pre_delay_seconds=0.5,
                    post_delay_seconds=0.8,
                ),
                WalkthroughStep(
                    step_index=3,
                    title="Choose New Wallpaper",
                    instruction="Click any thumbnail to set it as your desktop background.",
                    explanation="Applies the selected wallpaper immediately.",
                    action_type=TeachingAction.PULSE_BEACON,
                    target_app="System Settings",
                    fallback_screen_coords=(460.0, 240.0),
                    spotlight_bounds=(320.0, 180.0, 300.0, 200.0),
                    pre_delay_seconds=0.5,
                    post_delay_seconds=1.0,
                ),
            ],
        )

    @classmethod
    def _create_sound_template(cls) -> WalkthroughPlan:
        return WalkthroughPlan(
            walkthrough_id=f"wt_sound_{uuid.uuid4().hex[:6]}",
            goal="Adjust Sound & Audio Output",
            summary="Demonstrates how to adjust volume and choose audio output devices from Control Center.",
            target_app="ControlCenter",
            mode=WalkthroughMode.GUIDED_DEMO,
            source_tier="TIER_1_LOCAL_TEMPLATE",
            steps=[
                WalkthroughStep(
                    step_index=1,
                    title="Open Control Center",
                    instruction="Click the Control Center icon in the top right menu bar.",
                    explanation="Access volume, brightness, and quick controls.",
                    action_type=TeachingAction.DEMONSTRATE_CLICK,
                    target_app="ControlCenter",
                    fallback_screen_coords=(1380.0, 12.0),
                    spotlight_bounds=(1365.0, 2.0, 30.0, 20.0),
                    pre_delay_seconds=0.4,
                    post_delay_seconds=0.8,
                ),
                WalkthroughStep(
                    step_index=2,
                    title="Adjust Sound Slider",
                    instruction="Drag the Sound slider or click to expand audio destinations.",
                    explanation="Changes output volume or routes audio to external speakers.",
                    action_type=TeachingAction.MOVE_AND_HOVER,
                    target_app="ControlCenter",
                    target_element_query={"ax_role": "AXSlider", "ax_title": "Sound"},
                    fallback_screen_coords=(1320.0, 180.0),
                    spotlight_bounds=(1240.0, 160.0, 160.0, 36.0),
                    pre_delay_seconds=0.5,
                    post_delay_seconds=0.8,
                ),
                WalkthroughStep(
                    step_index=3,
                    title="Select Audio Output",
                    instruction="Select your headphones or external speakers from the device list.",
                    explanation="Routes system audio through the chosen device.",
                    action_type=TeachingAction.PULSE_BEACON,
                    target_app="ControlCenter",
                    fallback_screen_coords=(1320.0, 240.0),
                    spotlight_bounds=(1240.0, 220.0, 160.0, 40.0),
                    pre_delay_seconds=0.5,
                    post_delay_seconds=1.0,
                ),
            ],
        )

    @classmethod
    def _create_wifi_template(cls) -> WalkthroughPlan:
        return WalkthroughPlan(
            walkthrough_id=f"wt_wifi_{uuid.uuid4().hex[:6]}",
            goal="Connect to Wi-Fi Network",
            summary="Demonstrates how to browse and connect to available Wi-Fi networks.",
            target_app="ControlCenter",
            mode=WalkthroughMode.GUIDED_DEMO,
            source_tier="TIER_1_LOCAL_TEMPLATE",
            steps=[
                WalkthroughStep(
                    step_index=1,
                    title="Open Wi-Fi Menu",
                    instruction="Click the Wi-Fi icon in the top right menu bar.",
                    explanation="Shows available wireless networks in range.",
                    action_type=TeachingAction.DEMONSTRATE_CLICK,
                    target_app="ControlCenter",
                    fallback_screen_coords=(1340.0, 12.0),
                    spotlight_bounds=(1325.0, 2.0, 30.0, 20.0),
                    pre_delay_seconds=0.4,
                    post_delay_seconds=0.8,
                ),
                WalkthroughStep(
                    step_index=2,
                    title="Select Network",
                    instruction="Click your desired wireless network name from the list.",
                    explanation="Initiates network handshake.",
                    action_type=TeachingAction.DEMONSTRATE_CLICK,
                    target_app="ControlCenter",
                    target_element_query={"ax_role": "AXMenuItem", "ax_title": "Wi-Fi"},
                    fallback_screen_coords=(1340.0, 60.0),
                    spotlight_bounds=(1220.0, 40.0, 200.0, 30.0),
                    pre_delay_seconds=0.5,
                    post_delay_seconds=0.8,
                ),
                WalkthroughStep(
                    step_index=3,
                    title="Connect & Authenticate",
                    instruction="Enter password if prompted and click Join to connect.",
                    explanation="Establishes internet connectivity.",
                    action_type=TeachingAction.PULSE_BEACON,
                    target_app="ControlCenter",
                    fallback_screen_coords=(1340.0, 120.0),
                    spotlight_bounds=(1220.0, 100.0, 200.0, 40.0),
                    pre_delay_seconds=0.5,
                    post_delay_seconds=1.0,
                ),
            ],
        )

    @classmethod
    def _create_bluetooth_template(cls) -> WalkthroughPlan:
        return WalkthroughPlan(
            walkthrough_id=f"wt_bt_{uuid.uuid4().hex[:6]}",
            goal="Configure Bluetooth & Pair Device",
            summary="Demonstrates how to pair wireless headphones, mice, or keyboards via Bluetooth.",
            target_app="System Settings",
            mode=WalkthroughMode.GUIDED_DEMO,
            source_tier="TIER_1_LOCAL_TEMPLATE",
            steps=[
                WalkthroughStep(
                    step_index=1,
                    title="Open System Settings",
                    instruction="Open System Settings from Apple Menu > System Settings.",
                    explanation="Provides access to Bluetooth hardware pairing.",
                    action_type=TeachingAction.DEMONSTRATE_CLICK,
                    target_app="System Settings",
                    fallback_screen_coords=(18.0, 12.0),
                    pre_delay_seconds=0.4,
                    post_delay_seconds=0.8,
                ),
                WalkthroughStep(
                    step_index=2,
                    title="Select Bluetooth",
                    instruction="Click 'Bluetooth' in the settings sidebar.",
                    explanation="Scans for discoverable nearby Bluetooth devices.",
                    action_type=TeachingAction.DEMONSTRATE_CLICK,
                    target_app="System Settings",
                    target_element_query={"ax_role": "AXRow", "ax_title": "Bluetooth"},
                    fallback_screen_coords=(100.0, 180.0),
                    spotlight_bounds=(20.0, 160.0, 140.0, 36.0),
                    pre_delay_seconds=0.5,
                    post_delay_seconds=0.8,
                ),
                WalkthroughStep(
                    step_index=3,
                    title="Pair Nearby Device",
                    instruction="Locate your device under 'Nearby Devices' and click 'Connect'.",
                    explanation="Pairs the accessory with your Mac.",
                    action_type=TeachingAction.PULSE_BEACON,
                    target_app="System Settings",
                    fallback_screen_coords=(460.0, 220.0),
                    spotlight_bounds=(320.0, 180.0, 320.0, 50.0),
                    pre_delay_seconds=0.5,
                    post_delay_seconds=1.0,
                ),
            ],
        )

    @classmethod
    def _create_split_screen_template(cls) -> WalkthroughPlan:
        return WalkthroughPlan(
            walkthrough_id=f"wt_splitscreen_{uuid.uuid4().hex[:6]}",
            goal="Tile Window to Split Screen",
            summary="Demonstrates how to snap two windows side-by-side using macOS Split View.",
            target_app="WindowServer",
            mode=WalkthroughMode.GUIDED_DEMO,
            source_tier="TIER_1_LOCAL_TEMPLATE",
            steps=[
                WalkthroughStep(
                    step_index=1,
                    title="Hover Green Window Button",
                    instruction="Hover over the green full-screen button in the top-left of any window.",
                    explanation="Reveals Split View window snapping menu options.",
                    action_type=TeachingAction.MOVE_AND_HOVER,
                    target_app="WindowServer",
                    fallback_screen_coords=(38.0, 38.0),
                    spotlight_bounds=(20.0, 20.0, 36.0, 36.0),
                    pre_delay_seconds=0.4,
                    post_delay_seconds=0.8,
                ),
                WalkthroughStep(
                    step_index=2,
                    title="Tile Window to Left of Screen",
                    instruction="Click 'Tile Window to Left of Screen' from the menu.",
                    explanation="Locks this window to occupy the left 50% of the display.",
                    action_type=TeachingAction.DEMONSTRATE_CLICK,
                    target_app="WindowServer",
                    target_element_query={"ax_role": "AXMenuItem", "ax_title": "Tile Window to Left of Screen"},
                    fallback_screen_coords=(80.0, 80.0),
                    spotlight_bounds=(40.0, 60.0, 180.0, 30.0),
                    pre_delay_seconds=0.5,
                    post_delay_seconds=0.8,
                ),
                WalkthroughStep(
                    step_index=3,
                    title="Select Companion Window",
                    instruction="Click any open window on the right side to pair it side-by-side.",
                    explanation="Creates a unified two-window Split View workspace.",
                    action_type=TeachingAction.PULSE_BEACON,
                    target_app="WindowServer",
                    fallback_screen_coords=(960.0, 450.0),
                    spotlight_bounds=(720.0, 200.0, 480.0, 400.0),
                    pre_delay_seconds=0.5,
                    post_delay_seconds=1.0,
                ),
            ],
        )

    @classmethod
    def _create_force_quit_template(cls) -> WalkthroughPlan:
        return WalkthroughPlan(
            walkthrough_id=f"wt_forcequit_{uuid.uuid4().hex[:6]}",
            goal="Force Quit Unresponsive Application",
            summary="Demonstrates how to terminate frozen or unresponsive apps via Force Quit.",
            target_app="loginwindow",
            mode=WalkthroughMode.GUIDED_DEMO,
            source_tier="TIER_1_LOCAL_TEMPLATE",
            steps=[
                WalkthroughStep(
                    step_index=1,
                    title="Press Force Quit Hotkey",
                    instruction="Press ⌥ + ⌘ + Esc (Option + Command + Escape) together.",
                    explanation="Opens the Force Quit Applications manager dialog.",
                    action_type=TeachingAction.DEMONSTRATE_HOTKEY,
                    hotkey_combo=["cmd", "opt", "esc"],
                    target_app="loginwindow",
                    fallback_screen_coords=(640.0, 400.0),
                    spotlight_bounds=(480.0, 280.0, 320.0, 240.0),
                    pre_delay_seconds=0.4,
                    post_delay_seconds=0.8,
                ),
                WalkthroughStep(
                    step_index=2,
                    title="Select Unresponsive App",
                    instruction="Click the frozen application from the running process list.",
                    explanation="Highlights the target app for process termination.",
                    action_type=TeachingAction.DEMONSTRATE_CLICK,
                    target_app="loginwindow",
                    target_element_query={"ax_role": "AXRow"},
                    fallback_screen_coords=(640.0, 380.0),
                    spotlight_bounds=(500.0, 350.0, 280.0, 36.0),
                    pre_delay_seconds=0.5,
                    post_delay_seconds=0.8,
                ),
                WalkthroughStep(
                    step_index=3,
                    title="Click Force Quit",
                    instruction="Click 'Force Quit' in the bottom right corner of the window.",
                    explanation="Sends SIGKILL to immediately terminate the application process.",
                    action_type=TeachingAction.DEMONSTRATE_CLICK,
                    target_app="loginwindow",
                    target_element_query={"ax_role": "AXButton", "ax_title": "Force Quit"},
                    fallback_screen_coords=(700.0, 520.0),
                    spotlight_bounds=(640.0, 500.0, 100.0, 32.0),
                    pre_delay_seconds=0.5,
                    post_delay_seconds=1.0,
                ),
            ],
        )

    @classmethod
    def _create_trash_template(cls) -> WalkthroughPlan:
        return WalkthroughPlan(
            walkthrough_id=f"wt_trash_{uuid.uuid4().hex[:6]}",
            goal="Empty Trash in Finder",
            summary="Demonstrates how to permanently purge deleted files from the macOS Trash.",
            target_app="Finder",
            mode=WalkthroughMode.GUIDED_DEMO,
            source_tier="TIER_1_LOCAL_TEMPLATE",
            steps=[
                WalkthroughStep(
                    step_index=1,
                    title="Focus Finder",
                    instruction="Click on the desktop background to ensure Finder is the active app.",
                    explanation="Brings Finder menus to the top menu bar.",
                    action_type=TeachingAction.DEMONSTRATE_CLICK,
                    target_app="Finder",
                    fallback_screen_coords=(18.0, 12.0),
                    pre_delay_seconds=0.4,
                    post_delay_seconds=0.8,
                ),
                WalkthroughStep(
                    step_index=2,
                    title="Open Finder Menu",
                    instruction="Click 'Finder' in the top-left menu bar next to the Apple logo.",
                    explanation="Reveals Finder system operations including Empty Trash.",
                    action_type=TeachingAction.DEMONSTRATE_CLICK,
                    target_app="Finder",
                    target_element_query={"ax_role": "AXMenuBarItem", "ax_title": "Finder"},
                    fallback_screen_coords=(70.0, 12.0),
                    spotlight_bounds=(50.0, 2.0, 50.0, 20.0),
                    pre_delay_seconds=0.5,
                    post_delay_seconds=0.8,
                ),
                WalkthroughStep(
                    step_index=3,
                    title="Click Empty Trash",
                    instruction="Select 'Empty Trash...' or press ⇧ + ⌥ + ⌘ + Delete to purge immediately.",
                    explanation="Permanently clears deleted files and frees up disk space.",
                    action_type=TeachingAction.DEMONSTRATE_CLICK,
                    target_app="Finder",
                    target_element_query={"ax_role": "AXMenuItem", "ax_title": "Empty Trash..."},
                    fallback_screen_coords=(100.0, 120.0),
                    spotlight_bounds=(60.0, 105.0, 140.0, 26.0),
                    pre_delay_seconds=0.5,
                    post_delay_seconds=1.0,
                ),
            ],
        )

    @classmethod
    def _create_do_not_disturb_template(cls) -> WalkthroughPlan:
        return WalkthroughPlan(
            walkthrough_id=f"wt_dnd_{uuid.uuid4().hex[:6]}",
            goal="Enable Do Not Disturb & Focus Mode",
            summary="Demonstrates how to silence incoming notifications and calls with Focus modes.",
            target_app="ControlCenter",
            mode=WalkthroughMode.GUIDED_DEMO,
            source_tier="TIER_1_LOCAL_TEMPLATE",
            steps=[
                WalkthroughStep(
                    step_index=1,
                    title="Open Control Center",
                    instruction="Click the Control Center icon in the top right menu bar.",
                    explanation="Displays Focus mode options.",
                    action_type=TeachingAction.DEMONSTRATE_CLICK,
                    target_app="ControlCenter",
                    fallback_screen_coords=(1380.0, 12.0),
                    spotlight_bounds=(1365.0, 2.0, 30.0, 20.0),
                    pre_delay_seconds=0.4,
                    post_delay_seconds=0.8,
                ),
                WalkthroughStep(
                    step_index=2,
                    title="Click Focus Control",
                    instruction="Click the 'Focus' pill button in Control Center.",
                    explanation="Expands available Focus presets (Do Not Disturb, Work, Personal).",
                    action_type=TeachingAction.DEMONSTRATE_CLICK,
                    target_app="ControlCenter",
                    target_element_query={"ax_role": "AXButton", "ax_title": "Focus"},
                    fallback_screen_coords=(1320.0, 120.0),
                    spotlight_bounds=(1240.0, 100.0, 160.0, 36.0),
                    pre_delay_seconds=0.5,
                    post_delay_seconds=0.8,
                ),
                WalkthroughStep(
                    step_index=3,
                    title="Toggle Do Not Disturb",
                    instruction="Click 'Do Not Disturb' to silence notifications immediately.",
                    explanation="A moon icon appears in your menu bar while active.",
                    action_type=TeachingAction.PULSE_BEACON,
                    target_app="ControlCenter",
                    fallback_screen_coords=(1320.0, 160.0),
                    spotlight_bounds=(1240.0, 140.0, 160.0, 36.0),
                    pre_delay_seconds=0.5,
                    post_delay_seconds=1.0,
                ),
            ],
        )

    @classmethod
    def _create_spotlight_template(cls) -> WalkthroughPlan:
        return WalkthroughPlan(
            walkthrough_id=f"wt_spotlight_{uuid.uuid4().hex[:6]}",
            goal="Search Mac with Spotlight",
            summary="Demonstrates how to search for files, launch apps, and perform calculations with Spotlight.",
            target_app="Spotlight",
            mode=WalkthroughMode.GUIDED_DEMO,
            source_tier="TIER_1_LOCAL_TEMPLATE",
            steps=[
                WalkthroughStep(
                    step_index=1,
                    title="Press Spotlight Hotkey",
                    instruction="Press ⌘ + Space (Command + Space) together.",
                    explanation="Opens the centered macOS Spotlight search bar overlay.",
                    action_type=TeachingAction.DEMONSTRATE_HOTKEY,
                    hotkey_combo=["cmd", "space"],
                    target_app="Spotlight",
                    fallback_screen_coords=(640.0, 280.0),
                    spotlight_bounds=(400.0, 250.0, 480.0, 60.0),
                    pre_delay_seconds=0.4,
                    post_delay_seconds=0.8,
                ),
                WalkthroughStep(
                    step_index=2,
                    title="Type Search Query",
                    instruction="Type an app name, document title, or calculation (e.g. 'Safari').",
                    explanation="Spotlight indexes local files, web results, and system utilities.",
                    action_type=TeachingAction.DEMONSTRATE_TYPE,
                    text_to_type="Safari\n",
                    target_app="Spotlight",
                    target_element_query={"ax_role": "AXTextField", "ax_title": "Spotlight Search Field"},
                    fallback_screen_coords=(640.0, 280.0),
                    spotlight_bounds=(400.0, 250.0, 480.0, 60.0),
                    pre_delay_seconds=0.5,
                    post_delay_seconds=0.8,
                ),
                WalkthroughStep(
                    step_index=3,
                    title="Open Top Result",
                    instruction="Press Return ↵ to launch the top matching item immediately.",
                    explanation="Launches the application or opens the document.",
                    action_type=TeachingAction.PULSE_BEACON,
                    target_app="Spotlight",
                    fallback_screen_coords=(640.0, 340.0),
                    spotlight_bounds=(400.0, 310.0, 480.0, 50.0),
                    pre_delay_seconds=0.5,
                    post_delay_seconds=1.0,
                ),
            ],
        )

    @classmethod
    def _create_system_information_template(cls) -> WalkthroughPlan:
        return WalkthroughPlan(
            walkthrough_id=f"wt_system_info_{uuid.uuid4().hex[:6]}",
            goal="Navigate to System Information in macOS",
            summary="Demonstrates opening the Apple Menu, selecting About This Mac, opening More Info in System Settings, and launching the full System Report.",
            target_app="System Information",
            mode=WalkthroughMode.GUIDED_DEMO,
            source_tier="TIER_1_LOCAL_TEMPLATE",
            steps=[
                WalkthroughStep(
                    step_index=1,
                    title="Open Apple Menu",
                    instruction="Click the Apple icon  in the top-left corner of your screen.",
                    explanation="The Apple menu provides direct access to About This Mac and System Information.",
                    action_type=TeachingAction.DEMONSTRATE_CLICK,
                    target_app="ControlCenter",
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
                    target_app="ControlCenter",
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
                    target_app="System Information",
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
                    target_app="System Settings",
                    target_element_query={"ax_role": "AXButton", "ax_title": "System Report..."},
                    fallback_screen_coords=(680.0, 720.0),
                    spotlight_bounds=(610.0, 700.0, 140.0, 32.0),
                    pre_delay_seconds=0.6,
                    post_delay_seconds=1.4,
                ),
            ],
        )

    @classmethod
    def _create_cursor_navigation_template(cls) -> WalkthroughPlan:
        return WalkthroughPlan(
            walkthrough_id=f"wt_cursor_{uuid.uuid4().hex[:6]}",
            goal="Navigate and Move Cursor on macOS",
            summary="Demonstrates moving the mouse pointer smoothly across macOS interface landmarks, centering, and precision positioning.",
            target_app="Finder",
            mode=WalkthroughMode.GUIDED_DEMO,
            source_tier="TIER_1_LOCAL_TEMPLATE",
            steps=[
                WalkthroughStep(
                    step_index=1,
                    title="Center Mouse Pointer",
                    instruction="Move the mouse pointer to the center of your screen display.",
                    explanation="Centering the cursor establishes a neutral reference point for screen navigation.",
                    action_type=TeachingAction.MOVE_AND_HOVER,
                    target_app="Finder",
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
                    target_app="Finder",
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
                    target_app="Finder",
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
                    target_app="Finder",
                    fallback_screen_coords=(640.0, 760.0),
                    spotlight_bounds=(600.0, 720.0, 80.0, 80.0),
                    pre_delay_seconds=0.5,
                    post_delay_seconds=1.0,
                ),
            ],
        )

    @classmethod
    def _create_window_switching_template(cls) -> WalkthroughPlan:
        return WalkthroughPlan(
            walkthrough_id=f"wt_window_switch_{uuid.uuid4().hex[:6]}",
            goal="Switch and Swipe Between Windows in macOS",
            summary="Demonstrates switching between app windows using Command + `, switching applications with Command + Tab, and swiping spaces.",
            target_app="Finder",
            mode=WalkthroughMode.GUIDED_DEMO,
            source_tier="TIER_1_LOCAL_TEMPLATE",
            steps=[
                WalkthroughStep(
                    step_index=1,
                    title="Cycle Windows of Active App",
                    instruction="Press ⌘ + ` (Command + Backtick) to switch between open windows of the active application.",
                    explanation="Quickly alternates between multiple document or browser windows within the same app.",
                    action_type=TeachingAction.DEMONSTRATE_HOTKEY,
                    hotkey_combo=["cmd", "`"],
                    target_app="Finder",
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
                    target_app="Finder",
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
                    target_app="Finder",
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
                    target_app="Finder",
                    fallback_screen_coords=(640.0, 200.0),
                    spotlight_bounds=(100.0, 50.0, 1080.0, 700.0),
                    pre_delay_seconds=0.5,
                    post_delay_seconds=1.0,
                ),
            ],
        )

    @classmethod
    def _create_button_click_template(cls) -> WalkthroughPlan:
        return WalkthroughPlan(
            walkthrough_id=f"wt_click_button_{uuid.uuid4().hex[:6]}",
            goal="Click Buttons and Interactive Controls in macOS",
            summary="Demonstrates standard primary clicks, contextual right-clicks, and double-clicks on interface controls.",
            target_app="Finder",
            mode=WalkthroughMode.GUIDED_DEMO,
            source_tier="TIER_1_LOCAL_TEMPLATE",
            steps=[
                WalkthroughStep(
                    step_index=1,
                    title="Hover Over Target Button",
                    instruction="Move your cursor directly over the clickable button or control.",
                    explanation="Buttons respond with hover states and tooltips indicating their primary action.",
                    action_type=TeachingAction.MOVE_AND_HOVER,
                    target_app="Finder",
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
                    target_app="Finder",
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
                    target_app="Finder",
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
                    target_app="Finder",
                    fallback_screen_coords=(640.0, 450.0),
                    spotlight_bounds=(580.0, 430.0, 120.0, 40.0),
                    pre_delay_seconds=0.5,
                    post_delay_seconds=1.0,
                ),
            ],
        )

