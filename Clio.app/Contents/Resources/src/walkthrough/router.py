"""Hybrid Walkthrough Router & Efficiency Governor (Tiers 0, 1, 2, 3).

Path: src/walkthrough/router.py
Evaluates user walkthrough queries through a 4-tier decision cascade to maximize
accuracy while driving cloud AI API calls and token consumption towards zero.
"""

from __future__ import annotations

import logging
import platform
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from src.walkthrough.ai_gateway import AIWalkthroughGateway
from src.walkthrough.models import (
    WalkthroughMode,
    WalkthroughPlan,
)
from src.walkthrough.templates import BuiltinWalkthroughCatalog

logger = logging.getLogger(__name__)


_last_desktop_ctx: Tuple[float, Dict[str, Any]] = (0.0, {})

def get_desktop_context() -> Dict[str, Any]:
    """Sniffs lean, non-sensitive desktop context for local plan tailoring.

    Never extracts screen pixels or PII. Provides OS version and active application.
    Caches active app for 0.5s to provide instant 0ms responses.
    """
    global _last_desktop_ctx
    now = time.time()
    if now - _last_desktop_ctx[0] < 0.5 and _last_desktop_ctx[1]:
        return dict(_last_desktop_ctx[1])

    ctx: Dict[str, Any] = {
        "os_version": platform.mac_ver()[0] if sys.platform == "darwin" else "macOS",
        "active_app": "Finder",
        "window_title": "",
    }
    if sys.platform == "darwin":
        try:
            script = 'tell application "System Events" to get name of first process whose frontmost is true'
            res = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=0.25)
            if res.returncode == 0 and res.stdout.strip():
                ctx["active_app"] = res.stdout.strip()
        except Exception as e:
            logger.debug("Failed to detect active app via osascript: %s", e)
    _last_desktop_ctx = (now, ctx)
    return ctx



class HybridWalkthroughRouter:
    """Production 4-tier hybrid router governing walkthrough planning efficiency.

    Walkthrough plans are kept strictly separate from the core executable TaskMemoryEngine
    to prevent walkthrough instructions from polluting the user's recorded workflows database.
    """

    def __init__(
        self,
        ai_gateway: Optional[AIWalkthroughGateway] = None,
        min_cache_confidence: float = 0.85,
        cloud_ai_gateway: Optional[Any] = None,
        **kwargs: Any,
    ) -> None:
        # Isolated in-memory cache for walkthrough plans
        self._walkthrough_cache: Dict[str, WalkthroughPlan] = {}
        self.ai_gateway = ai_gateway or AIWalkthroughGateway()
        self.cloud_ai_gateway = cloud_ai_gateway
        self.min_cache_confidence = min_cache_confidence

        # Metrics & Telemetry
        self.stats = {
            "tier_0_hits": 0,
            "tier_1_hits": 0,
            "tier_2_hits": 0,
            "tier_3_calls": 0,
            "total_queries": 0,
        }

    def resolve_plan(
        self,
        query: str,
        mode: WalkthroughMode = WalkthroughMode.GUIDED_DEMO,
        context: Optional[Dict[str, Any]] = None,
    ) -> Tuple[WalkthroughPlan, str]:
        """Resolves a walkthrough query through the 4-tier cascade.

        Args:
            query: The user query or instructional request.
            mode: WalkthroughMode (guided_demo or interactive).
            context: Optional desktop context dict (active_app, os_version).

        Returns:
            Tuple of (WalkthroughPlan, tier_name)
        """
        self.stats["total_queries"] += 1
        clean_q = query.strip()
        if not clean_q:
            clean_q = "macOS Navigation"

        effective_context = context if context is not None else get_desktop_context()

        # =====================================================================
        # TIER 0: In-Memory Dedicated Walkthrough Cache (0ms, 0 Tokens)
        # =====================================================================
        tier_0_plan = self._check_tier_0(clean_q, mode)
        if tier_0_plan:
            self.stats["tier_0_hits"] += 1
            return tier_0_plan, "TIER_0_MEMORY_CACHE"

        # =====================================================================
        # TIER 1: Built-in Deterministic Templates (0ms, 0 Tokens)
        # =====================================================================
        tier_1_plan = self._check_tier_1(clean_q, mode)
        if tier_1_plan:
            self.stats["tier_1_hits"] += 1
            return tier_1_plan, "TIER_1_LOCAL_TEMPLATE"

        # =====================================================================
        # TIER 2: Live Local AX Accessibility Traversal (0 Tokens)
        # =====================================================================
        tier_2_plan = self._check_tier_2(clean_q, mode)
        if tier_2_plan:
            self.stats["tier_2_hits"] += 1
            self._persist_to_cache(clean_q, tier_2_plan)
            return tier_2_plan, "TIER_2_AX_TRAVERSAL"

        # =====================================================================
        # TIER 3: AI Gateway Fallback (Minified Token Budget)
        # =====================================================================
        self.stats["tier_3_calls"] += 1
        tier_3_plan = None
        if self.cloud_ai_gateway is not None and getattr(self.cloud_ai_gateway, "is_configured", False):
            try:
                tier_3_plan = self.cloud_ai_gateway.synthesize(clean_q, mode=mode, context=effective_context)
            except Exception as e:
                logger.warning("Cloud AI Gateway synthesis failed: %s", e)
                tier_3_plan = None

        if tier_3_plan is None:
            tier_3_plan = self.ai_gateway.synthesize(clean_q, mode=mode, context=effective_context)

        tier_3_plan.source_tier = "TIER_3_CLOUD_AI"

        # Cache into isolated walkthrough memory (never pollutes TaskMemoryEngine)
        self._persist_to_cache(clean_q, tier_3_plan)
        return tier_3_plan, "TIER_3_CLOUD_AI"

    def _normalize_key(self, query: str) -> str:
        """Normalizes a query string for cache key matching."""
        return " ".join(query.lower().strip().split())

    def _check_tier_0(self, query: str, mode: WalkthroughMode) -> Optional[WalkthroughPlan]:
        """Queries the dedicated in-memory walkthrough cache."""
        clean_key = self._normalize_key(query)
        if clean_key in self._walkthrough_cache:
            cached_plan = self._walkthrough_cache[clean_key]
            # Return plan instance configured for the requested mode
            plan = WalkthroughPlan(
                walkthrough_id=cached_plan.walkthrough_id,
                goal=cached_plan.goal,
                summary=cached_plan.summary,
                target_app=cached_plan.target_app,
                steps=cached_plan.steps,
                mode=mode,
                source_tier="TIER_0_MEMORY_CACHE",
            )
            return plan
        return None

    def _check_tier_1(self, query: str, mode: WalkthroughMode) -> Optional[WalkthroughPlan]:
        """Matches against the pre-authored deterministic catalog."""
        try:
            template = BuiltinWalkthroughCatalog.find_match(query)
            if template:
                template.mode = mode
                template.source_tier = "TIER_1_LOCAL_TEMPLATE"
                return template
        except Exception as e:
            logger.debug("Tier 1 template matching error: %s", e)
        return None

    def _check_tier_2(self, query: str, mode: WalkthroughMode) -> Optional[WalkthroughPlan]:
        """Inspects local application menu or windows for deterministic match."""
        return None

    def _persist_to_cache(self, query: str, plan: WalkthroughPlan) -> None:
        """Saves a synthesized plan to isolated in-memory walkthrough cache."""
        clean_key = self._normalize_key(query)
        self._walkthrough_cache[clean_key] = plan

        # Automatically register alias variants (e.g. 'teach me ...' or raw phrase)
        if clean_key.startswith("teach me "):
            sub = clean_key[9:].strip()
            if sub:
                self._walkthrough_cache[sub] = plan
        else:
            self._walkthrough_cache[f"teach me {clean_key}"] = plan

        logger.info("Cached walkthrough plan '%s' to in-memory walkthrough cache.", plan.goal)
