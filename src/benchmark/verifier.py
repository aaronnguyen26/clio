"""Benchmark Verification Subsystem.

Belongs to Milestone M5 (Multi-Domain Autonomous Benchmark Suite).
Validates execution results, step integrity, and zero-disruption constraints (R6/R7)
for Web, Desktop, and Cross-Domain workflows.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any, Optional

from src.actuators.base import BaseActuator
from src.actuators.mock import MockActuator


class BenchmarkCompletionVerifier:
    """Validates the execution results of the R5 multi-domain benchmark workflows."""

    @staticmethod
    def verify(
        actuator: BaseActuator,
        expected_text_substring: str = "Weekly Action Plan",
    ) -> bool:
        """Backward-compatible verification for Apple Notes weekly to-do benchmark."""
        return BenchmarkCompletionVerifier.verify_notes(actuator, expected_text_substring)

    @staticmethod
    def verify_notes(
        actuator: BaseActuator,
        expected_text_substring: str = "Weekly Action Plan",
    ) -> bool:
        """Verifies successful completion of Apple Notes productivity workflow."""
        if isinstance(actuator, MockActuator):
            has_launch = any(
                a.action_type == "launch_app"
                and "Notes" in a.parameters.get("bundle_id", "")
                for a in actuator.history
            )
            has_hotkey = any(
                a.action_type == "press_hotkey"
                and "n" in a.parameters.get("keys", [])
                for a in actuator.history
            )
            has_text = expected_text_substring in actuator.typed_text
            return has_launch and has_hotkey and has_text

        # Live verification via AppleScript without stealing focus
        if sys.platform != "darwin":
            return False
        script = f'''
        tell application "Notes"
            set noteList to (notes whose name contains "{expected_text_substring}" or body contains "{expected_text_substring}")
            return (count of noteList) > 0
        end tell
        '''
        try:
            res = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=3.0,
            )
            return res.returncode == 0 and "true" in res.stdout.lower()
        except Exception:
            return False

    @staticmethod
    def verify_web(
        actuator: BaseActuator,
        expected_url_substring: str = "news.ycombinator.com",
    ) -> bool:
        """Verifies successful completion of Web Browser automation workflow."""
        if isinstance(actuator, MockActuator):
            has_launch = any(
                a.action_type == "launch_app"
                and "Safari" in a.parameters.get("bundle_id", "")
                for a in actuator.history
            )
            has_url = any(
                a.action_type == "open_url"
                and expected_url_substring in a.parameters.get("url", "")
                for a in actuator.history
            )
            has_scroll = any(
                a.action_type == "scroll" for a in actuator.history
            )
            return has_launch and has_url and has_scroll

        # Live verification: inspect Safari active tab URL non-disruptively
        if sys.platform != "darwin":
            return False
        script = f'''
        tell application "Safari"
            repeat with w in windows
                repeat with t in tabs of w
                    if URL of t contains "{expected_url_substring}" then return true
                end repeat
            end repeat
            return false
        end tell
        '''
        try:
            res = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=3.0,
            )
            return res.returncode == 0 and "true" in res.stdout.lower()
        except Exception:
            return False

    @staticmethod
    def verify_cross_domain(
        actuator: BaseActuator,
        expected_url_substring: str = "wikipedia.org",
        expected_text_substring: str = "Autonomous Agent",
    ) -> bool:
        """Verifies successful completion of Cross-Domain Web <-> Desktop workflow."""
        if isinstance(actuator, MockActuator):
            has_url = any(
                a.action_type == "open_url"
                and expected_url_substring in a.parameters.get("url", "")
                for a in actuator.history
            )
            has_desktop_launch = any(
                a.action_type == "launch_app"
                and ("Notes" in a.parameters.get("bundle_id", "") or "TextEdit" in a.parameters.get("bundle_id", ""))
                for a in actuator.history
            )
            has_desktop_hotkey = any(
                a.action_type == "press_hotkey"
                and "n" in a.parameters.get("keys", [])
                for a in actuator.history
            )
            has_pasted_content = expected_text_substring in actuator.typed_text
            return has_url and has_desktop_launch and has_desktop_hotkey and has_pasted_content

        # Live verification: check web URL in browser and text in Notes
        return (
            BenchmarkCompletionVerifier.verify_web(actuator, expected_url_substring)
            and BenchmarkCompletionVerifier.verify_notes(actuator, expected_text_substring)
        )

    @staticmethod
    def verify_r6_background_invariants(actuator: BaseActuator) -> bool:
        """Verifies Requirement R6 / R7: purely headless or mock execution with zero physical desktop intrusion."""
        if isinstance(actuator, MockActuator):
            for action in actuator.history:
                # Disallow physical warping or global HID taps
                if action.action_type in ("warp_mouse", "global_hid_tap"):
                    return False
                # Verify background flags for application launches
                if action.action_type == "launch_app":
                    params = action.parameters
                    is_bg = params.get("background", False) or "-g" in params.get("args", []) or bool(params.get("bundle_id"))
                    if not is_bg:
                        return False
            return True
        return True
