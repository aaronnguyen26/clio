"""Unit tests for CoordinateAdapter (FEAT-MEM-07).

Tests validate:
- Forward ratio-to-screen projection (top-left, center, bottom-right)
- Strict boundary clamping (<0.0 to 0.0, >1.0 to 1.0)
- Inverse screen-to-ratio projection (exact and un-clamped)
- Round-trip projection fidelity
- Handling of edge cases: zero dimensions, negative coordinates, NaN/Inf
- Dict and duck-typed window object compatibility
- Rect conversion and validation utilities
"""

import unittest

from src.actuators.types import WindowInfo
from src.memory.coordinates import CoordinateAdapter


class TestCoordinateAdapter(unittest.TestCase):
    """Test suite for CoordinateAdapter."""

    def setUp(self) -> None:
        self.window = WindowInfo(
            window_id=1,
            owner_name="com.apple.Notes",
            title="Notes",
            x=200.0,
            y=100.0,
            width=800.0,
            height=600.0,
        )

    def test_forward_projection_standard_points(self):
        """TEST-COORD-01: Projects standard normalized coordinates to screen pixels."""
        # Top-left (0.0, 0.0) -> (200, 100)
        sx, sy = CoordinateAdapter.to_screen_coordinates(0.0, 0.0, self.window)
        self.assertEqual((sx, sy), (200, 100))

        # Center (0.5, 0.5) -> (600, 400)
        sx, sy = CoordinateAdapter.to_screen_coordinates(0.5, 0.5, self.window)
        self.assertEqual((sx, sy), (600, 400))

        # Bottom-right (1.0, 1.0) -> (1000, 700)
        sx, sy = CoordinateAdapter.to_screen_coordinates(1.0, 1.0, self.window)
        self.assertEqual((sx, sy), (1000, 700))

    def test_forward_projection_clamping(self):
        """TEST-COORD-02: Clamps negative and overflow ratios strictly to [0.0, 1.0]."""
        # Negative ratio clamped to 0.0
        sx, sy = CoordinateAdapter.to_screen_coordinates(-0.5, -1.2, self.window)
        self.assertEqual((sx, sy), (200, 100))

        # Overflow ratio clamped to 1.0
        sx, sy = CoordinateAdapter.to_screen_coordinates(2.5, 3.0, self.window)
        self.assertEqual((sx, sy), (1000, 700))

    def test_inverse_projection_standard_points(self):
        """TEST-COORD-03: Inverse projects screen coordinates to normalized ratios."""
        # Top-left (200, 100) -> (0.0, 0.0)
        nx, ny = CoordinateAdapter.to_normalized_coordinates(200.0, 100.0, self.window)
        self.assertAlmostEqual(nx, 0.0)
        self.assertAlmostEqual(ny, 0.0)

        # Center (600, 400) -> (0.5, 0.5)
        nx, ny = CoordinateAdapter.to_normalized_coordinates(600.0, 400.0, self.window)
        self.assertAlmostEqual(nx, 0.5)
        self.assertAlmostEqual(ny, 0.5)

        # Bottom-right (1000, 700) -> (1.0, 1.0)
        nx, ny = CoordinateAdapter.to_normalized_coordinates(1000.0, 700.0, self.window)
        self.assertAlmostEqual(nx, 1.0)
        self.assertAlmostEqual(ny, 1.0)

    def test_inverse_projection_clamping_and_unclamped(self):
        """TEST-COORD-04: Inverse projection clamps to [0.0, 1.0] unless clamp=False."""
        # Outside window (1200, 850) with clamp=True -> (1.0, 1.0)
        nx, ny = CoordinateAdapter.to_normalized_coordinates(1200.0, 850.0, self.window, clamp=True)
        self.assertEqual((nx, ny), (1.0, 1.0))

        # Outside window (1200, 850) with clamp=False -> (1.25, 1.25)
        nx, ny = CoordinateAdapter.to_normalized_coordinates(1200.0, 850.0, self.window, clamp=False)
        self.assertAlmostEqual(nx, 1.25)
        self.assertAlmostEqual(ny, 1.25)

    def test_round_trip_projection_fidelity(self):
        """TEST-COORD-05: Forward and inverse projection preserve coordinates."""
        for rx, ry in [(0.1, 0.2), (0.33, 0.67), (0.8, 0.9)]:
            sx, sy = CoordinateAdapter.to_screen_coordinates(rx, ry, self.window)
            nx, ny = CoordinateAdapter.to_normalized_coordinates(sx, sy, self.window)
            self.assertAlmostEqual(nx, rx, delta=0.01)
            self.assertAlmostEqual(ny, ry, delta=0.01)

    def test_zero_and_negative_window_dimensions(self):
        """TEST-COORD-06: Zero or negative window dimensions do not raise ZeroDivisionError."""
        zero_win = {"x": 50.0, "y": 50.0, "width": 0.0, "height": 0.0}
        sx, sy = CoordinateAdapter.to_screen_coordinates(0.5, 0.5, zero_win)
        self.assertEqual((sx, sy), (50, 50))

        nx, ny = CoordinateAdapter.to_normalized_coordinates(100.0, 100.0, zero_win)
        self.assertEqual((nx, ny), (0.0, 0.0))

    def test_negative_window_origin_multi_monitor(self):
        """TEST-COORD-07: Correctly handles negative origin (secondary monitor)."""
        multi_win = WindowInfo(
            window_id=2, owner_name="App", title="Win", x=-1920.0, y=0.0, width=1920.0, height=1080.0
        )
        sx, sy = CoordinateAdapter.to_screen_coordinates(0.5, 0.5, multi_win)
        self.assertEqual((sx, sy), (-960, 540))

        nx, ny = CoordinateAdapter.to_normalized_coordinates(-960.0, 540.0, multi_win)
        self.assertAlmostEqual(nx, 0.5)
        self.assertAlmostEqual(ny, 0.5)

    def test_dict_duck_typing(self):
        """TEST-COORD-08: Accepts plain dictionary of window bounds."""
        d_win = {"x": 100.0, "y": 100.0, "width": 400.0, "height": 300.0}
        sx, sy = CoordinateAdapter.to_screen_coordinates(0.25, 0.5, d_win)
        self.assertEqual((sx, sy), (200, 250))

    def test_ratio_validation_and_rect(self):
        """TEST-COORD-09: validate_ratio and to_screen_rect functionality."""
        self.assertTrue(CoordinateAdapter.validate_ratio(0.5, 0.5))
        self.assertFalse(CoordinateAdapter.validate_ratio(-0.1, 0.5))
        self.assertFalse(CoordinateAdapter.validate_ratio(1.1, 0.5))
        self.assertFalse(CoordinateAdapter.validate_ratio(float("nan"), 0.5))

        rx, ry, rw, rh = CoordinateAdapter.to_screen_rect(0.1, 0.1, 0.5, 0.5, self.window)
        self.assertEqual((rx, ry), (280, 160))
        self.assertEqual((rw, rh), (400, 300))
