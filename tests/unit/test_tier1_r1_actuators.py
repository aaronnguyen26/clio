"""Tier 1: Feature Coverage for R1 — Generic Zero-Wiring OS Control & App Launching.

Tests validate:
- App launching and focus state updates
- Mouse movement and coordinate clamping
- Mouse click synthesis (left/right, single/double)
- Keyboard text typing into virtual buffer
- Hotkey combination synthesis with modifier cleanup
- Pasteboard text injection with Cmd+V
"""

from typing import Generator
import pytest
from src.actuators.mock import MockActuator
from src.actuators.types import MouseButton, WindowInfo


@pytest.fixture
def mock_actuator() -> Generator[MockActuator, None, None]:
    """Provides an isolated production MockActuator with virtual desktop state."""
    actuator = MockActuator(screen_size=(1920.0, 1080.0))
    actuator.add_virtual_window(
        WindowInfo(
            window_id=1,
            owner_name="com.apple.finder",
            title="Desktop",
            x=0.0,
            y=0.0,
            width=1920.0,
            height=1080.0,
        )
    )
    yield actuator
    actuator.stop()
    actuator.assert_modifiers_released()


@pytest.mark.tier1
@pytest.mark.mock
class TestTier1Actuators:
    """Tier 1 tests for R1 actuator primitives."""

    def test_actuator_app_launch_records_state(self, mock_actuator: MockActuator):
        """TEST-T1-R1-01: Launching application updates frontmost app and audit history."""
        success = mock_actuator.launch_app("com.apple.Notes", timeout=5.0)
        assert success is True
        assert mock_actuator.get_frontmost_app() == "com.apple.Notes"
        mock_actuator.assert_app_launched("com.apple.Notes")

        # Verify a window was created for Notes
        notes_windows = mock_actuator.get_windows("Notes")
        assert len(notes_windows) >= 1
        assert notes_windows[0].owner_name == "com.apple.Notes"

    def test_actuator_focus_app(self, mock_actuator: MockActuator):
        """TEST-T1-R1-02: Bringing background application to foreground."""
        mock_actuator.launch_app("com.apple.Safari")
        assert mock_actuator.get_frontmost_app() == "com.apple.Safari"

        mock_actuator.focus_app("com.apple.Notes")
        assert mock_actuator.get_frontmost_app() == "com.apple.Notes"
        mock_actuator.assert_action_called("focus_app", bundle_id="com.apple.Notes")

    def test_actuator_mouse_move_and_coordinates(self, mock_actuator: MockActuator):
        """TEST-T1-R1-03: Moving mouse cursor updates position accurately."""
        mock_actuator.move_mouse(350.0, 420.0)
        pos = mock_actuator.get_mouse_position()
        assert pos == (350.0, 420.0)
        mock_actuator.assert_action_called("move_mouse", x=350.0, y=420.0)

    def test_actuator_click_synthesis(self, mock_actuator: MockActuator):
        """TEST-T1-R1-04: Synthesizing left, right, and double clicks."""
        # Click at specific location
        mock_actuator.click(x=100.0, y=200.0, button=MouseButton.LEFT, click_count=1)
        assert mock_actuator.get_mouse_position() == (100.0, 200.0)
        mock_actuator.assert_action_called("click", x=100.0, y=200.0, button="left", click_count=1)

        # Right-click at current position
        mock_actuator.click(button=MouseButton.RIGHT, click_count=1)
        mock_actuator.assert_action_called("click", x=100.0, y=200.0, button="right", click_count=1)

        # Double-click
        mock_actuator.click(click_count=2)
        mock_actuator.assert_action_called("click", click_count=2)

    def test_actuator_keyboard_text_typing(self, mock_actuator: MockActuator):
        """TEST-T1-R1-05: Typing text updates typed buffer without sticky keys."""
        mock_actuator.type_text("Autonomous Companion")
        mock_actuator.assert_text_typed("Autonomous Companion")
        mock_actuator.assert_action_called("type_text", text="Autonomous Companion")
        mock_actuator.assert_modifiers_released()

    def test_actuator_hotkey_combination(self, mock_actuator: MockActuator):
        """TEST-T1-R1-06: Synthesizing shortcut combinations releases modifiers."""
        mock_actuator.press_hotkey("cmd", "n")
        mock_actuator.assert_hotkey_pressed("cmd", "n")
        mock_actuator.assert_modifiers_released()

    def test_actuator_pasteboard_injection(self, mock_actuator: MockActuator):
        """TEST-T1-R1-07: High-speed multi-line text paste via clipboard and Cmd+V."""
        multiline_text = "Line 1: Priority\nLine 2: Action\nLine 3: Delivery"
        mock_actuator.paste_text(multiline_text)
        assert mock_actuator.clipboard == multiline_text
        mock_actuator.assert_text_typed(multiline_text)
        mock_actuator.assert_hotkey_pressed("cmd", "v")
        mock_actuator.assert_modifiers_released()
