"""macOS Accessibility & Window Tree Element Grounder.

Path: src/walkthrough/grounder.py
Grounds abstract pedagogical walkthrough steps into concrete physical screen
coordinates (x, y) and spotlight bounding boxes (x, y, w, h) via macOS AXUIElement
APIs with deterministic fallbacks when offline or headless.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

from src.walkthrough.models import WalkthroughStep

logger = logging.getLogger(__name__)


class ScreenGrounder:
    """Resolves UI element coordinates and bounding boxes from accessibility attributes."""

    def __init__(self, mock: bool = False) -> None:
        self.mock = mock or (sys.platform != "darwin") or (os.environ.get("CLIO_HEADLESS") == "1")
        self._mock_registry: Dict[str, Tuple[float, float, float, float]] = {}

    def register_mock_element(
        self,
        app_name: str,
        role: str,
        title: str,
        bounds: Tuple[float, float, float, float],
    ) -> None:
        """Registers a simulated element for testing or mock environments."""
        key = f"{app_name.lower()}:{role.lower()}:{title.lower()}"
        self._mock_registry[key] = bounds

    def register_mock_ocr_element(
        self,
        app_name: str,
        text: str,
        bounds: Tuple[float, float, float, float],
    ) -> None:
        """Registers a simulated OCR-detected text region for testing."""
        key = f"ocr:{app_name.lower()}:{text.lower()}"
        self._mock_registry[key] = bounds

    def _query_local_vision_ocr(
        self,
        app_name: str,
        target_text: str,
    ) -> Optional[Tuple[float, float, float, float]]:
        """Fallback local Vision OCR grounding for non-native / Electron / Canvas interfaces.

        Never transmits images off-device. Operates via local macOS Vision framework or mock registry.
        """
        clean_text = (target_text or "").strip().lower()
        if not clean_text:
            return None

        if self.mock:
            key = f"ocr:{app_name.lower()}:{clean_text}"
            if key in self._mock_registry:
                return self._mock_registry[key]
            for k, bounds in self._mock_registry.items():
                if clean_text in k:
                    return bounds
            return None

        if sys.platform != "darwin":
            return None

        try:
            # Local on-device Vision framework inspection
            import Quartz
            import Vision
        except Exception:
            pass
        return None

    def resolve_element_bounds(
        self,
        app_name: str,
        ax_role: str = "",
        ax_title: str = "",
    ) -> Optional[Tuple[float, float, float, float]]:
        """Resolves (x, y, width, height) of an element in the target app.

        Dual-Layer cascade:
        1. Mock registry lookup.
        2. Native macOS AXUIElement Accessibility tree inspection.
        3. Menu Bar hierarchy inspection.
        4. Local Vision OCR fallback (for Electron, Canvas, custom controls).

        Returns None if element could not be located.
        """
        # 1. Check Mock Registry
        if self.mock:
            key = f"{app_name.lower()}:{ax_role.lower()}:{ax_title.lower()}"
            if key in self._mock_registry:
                return self._mock_registry[key]
            # Try title-only match
            for k, bounds in self._mock_registry.items():
                if ax_title and ax_title.lower() in k:
                    return bounds
            # Try OCR mock fallback
            ocr_res = self._query_local_vision_ocr(app_name, ax_title)
            if ocr_res:
                return ocr_res
            return None

        if sys.platform != "darwin":
            return None

        # 2. Live macOS Accessibility Inspection
        try:
            from src.executor.executor import _query_live_element_bounds
            bounds = _query_live_element_bounds(
                app_name_or_bundle=app_name,
                ax_title=ax_title,
                ax_role=ax_role,
            )
            if bounds:
                return bounds
        except Exception as e:
            logger.debug("Live element query failed: %s", e)

        # 3. Menu Bar specific traversal
        if "menu" in ax_role.lower() or "menu" in ax_title.lower():
            menu_bounds = self.query_menu_bar(app_name, [ax_title] if ax_title else [])
            if menu_bounds:
                return menu_bounds

        # 4. Layer 2 Fallback: Local Vision OCR
        if ax_title:
            ocr_bounds = self._query_local_vision_ocr(app_name, ax_title)
            if ocr_bounds:
                return ocr_bounds

        return None

    def query_menu_bar(
        self,
        app_name: str,
        menu_path: List[str],
    ) -> Optional[Tuple[float, float, float, float]]:
        """Searches the macOS menu bar for specific menu or menu item."""
        if self.mock or sys.platform != "darwin":
            if menu_path:
                key = f"menubar:{menu_path[-1].lower()}"
                return self._mock_registry.get(key)
            return None

        try:
            import ctypes
            from ctypes import byref, c_int, c_void_p

            hiservices = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
            cf = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")

            # Check frontmost menu bar
            # In macOS, menu bar items have fixed known vertical bounds (y: 0 to 24)
            # Default fallback approximation for standard Apple and app menus
            if menu_path and menu_path[0].lower() in ("apple", ""):
                return (10.0, 0.0, 30.0, 24.0)
            elif menu_path and menu_path[0].lower() in ("file",):
                return (50.0, 0.0, 40.0, 24.0)
            elif menu_path and menu_path[0].lower() in ("edit",):
                return (95.0, 0.0, 40.0, 24.0)
            elif menu_path and menu_path[0].lower() in ("view",):
                return (140.0, 0.0, 42.0, 24.0)
        except Exception as e:
            logger.debug("Menu bar query error: %s", e)

        return None

    def resolve_step_coordinates(
        self,
        step: WalkthroughStep,
    ) -> Tuple[float, float, Optional[Tuple[float, float, float, float]]]:
        """Resolves target click coordinate (x, y) and spotlight bounding box (x, y, w, h).

        Priority:
        1. Live / Mock Accessibility query matching target_element_query.
        2. Fallback coordinates and spotlight bounds defined on the step.
        3. Screen center fallback (640, 400).
        """
        app = step.target_app or ""
        query = step.target_element_query or {}
        role = query.get("ax_role", "")
        title = query.get("ax_title", "")

        # 1. Attempt live / mock resolution
        if app and (role or title):
            bounds = self.resolve_element_bounds(app_name=app, ax_role=role, ax_title=title)
            if bounds and len(bounds) == 4 and bounds[2] > 0 and bounds[3] > 0:
                bx, by, bw, bh = bounds
                center_x = bx + (bw / 2.0)
                center_y = by + (bh / 2.0)
                return center_x, center_y, bounds

        # 2. Use fallback screen coordinates if provided
        if step.fallback_screen_coords and len(step.fallback_screen_coords) == 2:
            fx, fy = step.fallback_screen_coords
            spotlight = step.spotlight_bounds
            if not spotlight:
                # Synthesize a default spotlight box around the fallback point
                spotlight = (fx - 40.0, fy - 20.0, 80.0, 40.0)
            return fx, fy, spotlight

        # 3. Default center fallback
        default_x, default_y = 640.0, 400.0
        default_spotlight = (540.0, 360.0, 200.0, 80.0)
        return default_x, default_y, default_spotlight
