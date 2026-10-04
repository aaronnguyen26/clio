"""AI Walkthrough Gateway & Offline Fallback Provider.

Path: src/walkthrough/ai_gateway.py
Manages interaction with cloud LLM providers (Gemini / Claude) for synthesizing
novel walkthrough plans with strict schema validation and token budgeting.
Includes a deterministic offline fallback provider when API keys or internet are absent.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, List, Optional
import uuid

from src.ai.providers import (
    BaseAIProvider,
    MockAIProvider,
    extract_json_payload,
)
from src.walkthrough.dissector import OfflineWalkthroughDissector
from src.walkthrough.models import (
    TeachingAction,
    WalkthroughMode,
    WalkthroughPlan,
    WalkthroughStep,
)

logger = logging.getLogger(__name__)

# Structured schema prompt for minimal token consumption
WALKTHROUGH_SYSTEM_PROMPT = """You are Clio's Autonomous Desktop Teaching Engine.
Decompose the requested macOS desktop task into 3 to 5 clear, sequential instructional steps.

MANDATORY RULES:
1. Every step must have:
   - 'step_index' (1, 2, 3...)
   - 'title' (short headline, e.g. "Open System Settings")
   - 'instruction' (imperative command, e.g. "Click the Apple icon  in the menu bar")
   - 'explanation' (pedagogical reason, e.g. "This opens system-wide preferences")
   - 'action_type' ("move_and_hover", "demonstrate_click", "wait_for_user_click", "demonstrate_hotkey", "demonstrate_type", "pulse_beacon")
   - 'target_app' (e.g. "System Settings", "Finder", "Safari")
   - 'target_element' (object with 'role' and 'title', e.g. {"role": "AXButton", "title": "Done"})
   - optional 'hotkey' (array of strings, e.g. ["cmd", "q"])
2. Output strictly valid JSON matching the schema. No markdown prose outside JSON.
"""

WALKTHROUGH_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "goal": {"type": "string"},
        "summary": {"type": "string"},
        "target_app": {"type": "string"},
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "step_index": {"type": "integer"},
                    "title": {"type": "string"},
                    "instruction": {"type": "string"},
                    "explanation": {"type": "string"},
                    "action_type": {"type": "string"},
                    "target_app": {"type": "string"},
                    "target_element": {
                        "type": "object",
                        "properties": {
                            "role": {"type": "string"},
                            "title": {"type": "string"},
                        },
                    },
                    "hotkey": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["step_index", "title", "instruction", "action_type"],
            },
        },
    },
    "required": ["goal", "summary", "target_app", "steps"],
}


class AIWalkthroughGateway:
    """Gateway for synthesizing novel WalkthroughPlans via AI API or offline mock."""

    def __init__(self, provider: Optional[Any] = None) -> None:
        self.provider = provider
        self.total_tokens_used = 0
        self.call_count = 0

    def synthesize(
        self,
        query: str,
        mode: WalkthroughMode = WalkthroughMode.GUIDED_DEMO,
        context: Optional[Dict[str, Any]] = None,
    ) -> WalkthroughPlan:
        """Synthesizes a WalkthroughPlan for an arbitrary user query with lean desktop context."""
        self.call_count += 1
        clean_query = query.strip()

        # If a live provider is available and configured with an API key, attempt live call
        if self.provider is not None and getattr(self.provider, "api_key", None):
            try:
                plan = self._call_live_provider(clean_query, mode, context)
                if plan:
                    return plan
            except Exception as e:
                logger.warning("Live AI provider failed; falling back to offline generator: %s", e)

        # Fallback to local intelligent offline generator
        return self._generate_offline_plan(clean_query, mode, context)

    def _call_live_provider(
        self,
        query: str,
        mode: WalkthroughMode,
        context: Optional[Dict[str, Any]] = None,
    ) -> Optional[WalkthroughPlan]:
        """Calls the configured AI provider with token-budgeted prompt incorporating lean context."""
        prompt = f"Synthesize a step-by-step macOS walkthrough to teach: '{query}'.\n"
        if context:
            active_app = context.get("active_app")
            window_title = context.get("window_title")
            os_ver = context.get("os_version")
            if active_app:
                prompt += f"Active Application: {active_app}\n"
            if window_title:
                prompt += f"Focused Window: {window_title}\n"
            if os_ver:
                prompt += f"macOS Environment: {os_ver}\n"

        # Call provider's LLM generation interface
        if hasattr(self.provider, "call_llm_json"):
            raw_json = self.provider.call_llm_json(
                system_prompt=WALKTHROUGH_SYSTEM_PROMPT,
                user_prompt=prompt,
                schema=WALKTHROUGH_RESPONSE_SCHEMA,
                max_tokens=1500,
            )
            parsed = extract_json_payload(raw_json)
            return self._parse_ai_dict(parsed, query, mode, context)
        return None

    def _generate_offline_plan(
        self,
        query: str,
        mode: WalkthroughMode,
        context: Optional[Dict[str, Any]] = None,
    ) -> WalkthroughPlan:
        """Generates a high-quality, structured WalkthroughPlan offline without external network."""
        return OfflineWalkthroughDissector.synthesize(query, mode=mode, context=context)

    def _parse_ai_dict(
        self,
        data: Dict[str, Any],
        query: str,
        mode: WalkthroughMode,
        context: Optional[Dict[str, Any]] = None,
    ) -> WalkthroughPlan:
        """Parses structured JSON response from AI provider into WalkthroughPlan."""
        spec_id = f"wt_ai_{uuid.uuid4().hex[:8]}"
        default_app = (context.get("active_app") if context else "") or "macOS"
        target_app = data.get("target_app", default_app)
        raw_steps = data.get("steps", [])
        steps: List[WalkthroughStep] = []

        for idx, s in enumerate(raw_steps, 1):
            act_str = s.get("action_type", "move_and_hover")
            try:
                act_type = TeachingAction(act_str)
            except ValueError:
                act_type = TeachingAction.MOVE_AND_HOVER

            elem_query = s.get("target_element") or {}
            hotkey = s.get("hotkey")

            coords = s.get("fallback_screen_coords")
            if coords and isinstance(coords, (list, tuple)) and len(coords) == 2:
                t_coords = (float(coords[0]), float(coords[1]))
            else:
                base_x = 420.0 + (idx * 55.0) % 320.0
                base_y = 220.0 + (idx * 65.0) % 260.0
                t_coords = (base_x, base_y)

            bounds = s.get("spotlight_bounds")
            if bounds and isinstance(bounds, (list, tuple)) and len(bounds) == 4:
                t_bounds = (float(bounds[0]), float(bounds[1]), float(bounds[2]), float(bounds[3]))
            else:
                t_bounds = (t_coords[0] - 50.0, t_coords[1] - 20.0, 120.0, 44.0)

            steps.append(
                WalkthroughStep(
                    step_index=int(s.get("step_index", idx)),
                    title=str(s.get("title", f"Step {idx}")),
                    instruction=str(s.get("instruction", "")),
                    explanation=str(s.get("explanation", "")),
                    action_type=act_type,
                    target_app=str(s.get("target_app", target_app)),
                    target_element_query=elem_query if isinstance(elem_query, dict) else {},
                    hotkey_combo=list(hotkey) if isinstance(hotkey, list) else None,
                    fallback_screen_coords=t_coords,
                    spotlight_bounds=t_bounds,
                )
            )

        return WalkthroughPlan(
            walkthrough_id=spec_id,
            goal=str(data.get("goal", query)),
            summary=str(data.get("summary", f"Walkthrough for {query}")),
            target_app=target_app,
            steps=steps,
            mode=mode,
            source_tier="TIER_3_CLOUD_AI",
        )
