"""Resolution-independent window-relative ratio coordinate projection.

Belongs to FEAT-MEM-07 (CoordinateAdapter).
Supports:
- Forward projection: normalized (norm_x, norm_y) in [0.0, 1.0] -> screen coordinates (int, int).
- Inverse projection: screen coordinates (screen_x, screen_y) -> normalized ratio (norm_x, norm_y).
- Strict clamping to [0.0, 1.0] boundaries and validation.
- Zero-dimension and negative-dimension window tolerance (ZeroDivisionError protection).
- Multi-display negative coordinates support.
- Duck-typed window objects and dictionary bounds support.
"""

from __future__ import annotations

import math
from typing import Any, Tuple, Union

try:
    from src.actuators.types import WindowInfo
except ImportError:
    WindowInfo = Any  # type: ignore


class CoordinateAdapter:
    """Resolution-independent window-relative ratio coordinate projection."""

    @staticmethod
    def _extract_window_bounds(window: Any) -> Tuple[float, float, float, float]:
        """Extracts (x, y, width, height) from WindowInfo, duck-typed object, or dict."""
        if hasattr(window, "x") and hasattr(window, "y") and hasattr(window, "width") and hasattr(window, "height"):
            return float(window.x), float(window.y), float(window.width), float(window.height)
        if isinstance(window, dict):
            return (
                float(window.get("x", 0.0)),
                float(window.get("y", 0.0)),
                float(window.get("width", 0.0)),
                float(window.get("height", 0.0)),
            )
        raise TypeError(f"Invalid window type: {type(window).__name__}. Expected WindowInfo or dict.")

    @classmethod
    def to_screen_coordinates(
        cls,
        norm_x: float,
        norm_y: float,
        window: Any,
        clamp: bool = True,
    ) -> Tuple[int, int]:
        """Converts normalized ratios in [0.0, 1.0] to absolute screen coordinates.

        Formula: screen_x = floor(window.x + (norm_x * window.width))
                 screen_y = floor(window.y + (norm_y * window.height))

        Args:
            norm_x: Normalized horizontal ratio (0.0 = left edge, 1.0 = right edge).
            norm_y: Normalized vertical ratio (0.0 = top edge, 1.0 = bottom edge).
            window: WindowInfo object or dict with x, y, width, height.
            clamp: If True (default), clamps norm_x and norm_y to [0.0, 1.0].

        Returns:
            Tuple of (screen_x, screen_y) as integers.
        """
        wx, wy, ww, wh = cls._extract_window_bounds(window)

        # Handle NaN or Inf safely
        if math.isnan(norm_x) or math.isinf(norm_x):
            norm_x = 0.0
        if math.isnan(norm_y) or math.isinf(norm_y):
            norm_y = 0.0

        if clamp:
            clamped_x = max(0.0, min(1.0, float(norm_x)))
            clamped_y = max(0.0, min(1.0, float(norm_y)))
        else:
            clamped_x = float(norm_x)
            clamped_y = float(norm_y)

        # Protect against negative window dimensions
        effective_width = max(0.0, ww)
        effective_height = max(0.0, wh)

        screen_x = int(math.floor(wx + (clamped_x * effective_width)))
        screen_y = int(math.floor(wy + (clamped_y * effective_height)))

        return screen_x, screen_y

    @classmethod
    def to_normalized_coordinates(
        cls,
        screen_x: float,
        screen_y: float,
        window: Any,
        clamp: bool = True,
    ) -> Tuple[float, float]:
        """Converts absolute screen coordinates to normalized window-relative ratios.

        Formula: norm_x = (screen_x - window.x) / window.width
                 norm_y = (screen_y - window.y) / window.height

        Args:
            screen_x: Absolute horizontal screen coordinate.
            screen_y: Absolute vertical screen coordinate.
            window: WindowInfo object or dict with x, y, width, height.
            clamp: If True (default), clamps result ratios to [0.0, 1.0].

        Returns:
            Tuple of (norm_x, norm_y) as floats.
        """
        wx, wy, ww, wh = cls._extract_window_bounds(window)

        # Guard against zero or negative dimensions (ZeroDivisionError)
        if ww <= 0.0:
            norm_x = 0.0
        else:
            norm_x = (float(screen_x) - wx) / ww

        if wh <= 0.0:
            norm_y = 0.0
        else:
            norm_y = (float(screen_y) - wy) / wh

        if clamp:
            norm_x = max(0.0, min(1.0, norm_x))
            norm_y = max(0.0, min(1.0, norm_y))

        return norm_x, norm_y

    # Backward-compatible alias for inverse projection
    from_screen_coordinates = to_normalized_coordinates

    @classmethod
    def validate_ratio(cls, norm_x: float, norm_y: float) -> bool:
        """Validates that ratios fall strictly within [0.0, 1.0] and are finite."""
        if math.isnan(norm_x) or math.isnan(norm_y) or math.isinf(norm_x) or math.isinf(norm_y):
            return False
        return 0.0 <= norm_x <= 1.0 and 0.0 <= norm_y <= 1.0

    @classmethod
    def to_screen_rect(
        cls,
        norm_x: float,
        norm_y: float,
        norm_w: float,
        norm_h: float,
        window: Any,
        clamp: bool = True,
    ) -> Tuple[int, int, int, int]:
        """Converts a normalized bounding box to screen coordinates."""
        sx, sy = cls.to_screen_coordinates(norm_x, norm_y, window, clamp=clamp)
        _, _, ww, wh = cls._extract_window_bounds(window)
        sw = int(math.floor(max(0.0, min(1.0, norm_w) if clamp else norm_w) * max(0.0, ww)))
        sh = int(math.floor(max(0.0, min(1.0, norm_h) if clamp else norm_h) * max(0.0, wh)))
        return sx, sy, sw, sh
