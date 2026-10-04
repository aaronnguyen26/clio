"""Cloud AI Walkthrough Gateway Subsystem.

Path: src/walkthrough/cloud_ai/cloud_gateway.py
Preserves the complete cloud LLM orchestration architecture for walkthrough generation
when Gemini/Claude API keys are provisioned.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional
import uuid

from src.ai.credentials import get_credential_manager
from src.ai.providers import BaseAIProvider, get_ai_provider
from src.walkthrough.models import (
    TeachingAction,
    WalkthroughMode,
    WalkthroughPlan,
    WalkthroughStep,
)

logger = logging.getLogger(__name__)

WALKTHROUGH_SYSTEM_PROMPT = """You are Clio, an Autonomous Desktop Teaching Engine for macOS.
Given a user query or teaching goal, synthesize a precise, step-by-step instructional walkthrough.
Each step must specify:
1. title: Short title of the instructional step.
2. instruction: Clear, imperative pedagogical instruction.
3. explanation: Why this step is taken.
4. action_type: One of 'move_and_hover', 'demonstrate_click', 'wait_for_user_click', 'demonstrate_hotkey', 'demonstrate_type', 'pulse_beacon', 'highlight_region'.
5. target_app: Target macOS application name.
6. target_element: Optional dict with 'role' and 'title'.
7. hotkey_combo: Optional list of strings for keyboard shortcuts (e.g. ['command', 'space']).
8. text_to_type: Optional string if text is entered.
9. fallback_screen_coords: Approximate screen (x, y) if known.
10. spotlight_bounds: Approximate (x, y, width, height) bounding box.
Return strictly valid JSON conforming to the schema."""

WALKTHROUGH_RESPONSE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["goal", "summary", "target_app", "steps"],
    "properties": {
        "goal": {"type": "string"},
        "summary": {"type": "string"},
        "target_app": {"type": "string"},
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["step_index", "title", "instruction", "action_type", "target_app"],
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
                    "hotkey_combo": {"type": "array", "items": {"type": "string"}},
                    "text_to_type": {"type": "string"},
                    "fallback_screen_coords": {
                        "type": "array",
                        "items": {"type": "number"},
                        "minItems": 2,
                        "maxItems": 2,
                    },
                    "spotlight_bounds": {
                        "type": "array",
                        "items": {"type": "number"},
                        "minItems": 4,
                        "maxItems": 4,
                    },
                },
            },
        },
    },
}


class CloudAIWalkthroughGateway:
    """Gateway orchestrating cloud LLM calls for walkthrough synthesis."""

    def __init__(self, provider: Optional[Any] = None) -> None:
        if provider is not None:
            self.provider = provider
        else:
            try:
                self.provider = get_ai_provider()
            except Exception:
                self.provider = None

    @property
    def is_configured(self) -> bool:
        """Returns True if a live cloud provider with an active API key is available."""
        if self.provider is None:
            return False
        return bool(getattr(self.provider, "api_key", None))

    def synthesize(
        self,
        query: str,
        mode: WalkthroughMode = WalkthroughMode.GUIDED_DEMO,
        context: Optional[Dict[str, Any]] = None,
    ) -> Optional[WalkthroughPlan]:
        """Synthesizes a WalkthroughPlan using the cloud provider. Returns None if unconfigured."""
        if not self.is_configured:
            return None

        prompt = f"Synthesize a step-by-step macOS walkthrough to teach: '{query.strip()}'.\n"
        if context:
            active_app = context.get("active_app")
            if active_app:
                prompt += f"Active Application: {active_app}\n"

        try:
            if hasattr(self.provider, "call_llm_json"):
                raw_json = self.provider.call_llm_json(
                    system_prompt=WALKTHROUGH_SYSTEM_PROMPT,
                    user_prompt=prompt,
                    schema=WALKTHROUGH_RESPONSE_SCHEMA,
                    max_tokens=1500,
                )
                data = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
                return self._parse_ai_dict(data, query, mode)
        except Exception as e:
            logger.warning("Cloud AI synthesis failed: %s", e)
            return None
        return None

    def _parse_ai_dict(
        self,
        data: Dict[str, Any],
        query: str,
        mode: WalkthroughMode,
    ) -> WalkthroughPlan:
        """Parses structured JSON dictionary into a WalkthroughPlan."""
        spec_id = f"wt_ai_{uuid.uuid4().hex[:8]}"
        goal = data.get("goal") or query
        summary = data.get("summary") or f"Walkthrough for {goal}"
        target_app = data.get("target_app") or "System Settings"

        steps: List[WalkthroughStep] = []
        raw_steps = data.get("steps") or []
        for idx, s in enumerate(raw_steps, start=1):
            action_str = s.get("action_type", "move_and_hover")
            try:
                action_type = TeachingAction(action_str)
            except ValueError:
                action_type = TeachingAction.MOVE_AND_HOVER

            step_elem = s.get("target_element")
            elem_query = None
            if isinstance(step_elem, dict):
                elem_query = {
                    "ax_role": step_elem.get("role", ""),
                    "ax_title": step_elem.get("title", ""),
                }

            coords = s.get("fallback_screen_coords")
            t_coords = tuple(coords) if coords and len(coords) == 2 else (640.0, 400.0)

            bounds = s.get("spotlight_bounds")
            t_bounds = tuple(bounds) if bounds and len(bounds) == 4 else None

            steps.append(
                WalkthroughStep(
                    step_index=idx,
                    title=s.get("title", f"Step {idx}"),
                    instruction=s.get("instruction", ""),
                    explanation=s.get("explanation", ""),
                    action_type=action_type,
                    target_app=s.get("target_app", target_app),
                    target_element_query=elem_query,
                    hotkey_combo=s.get("hotkey_combo"),
                    text_to_type=s.get("text_to_type"),
                    fallback_screen_coords=t_coords,
                    spotlight_bounds=t_bounds,
                )
            )

        return WalkthroughPlan(
            walkthrough_id=spec_id,
            goal=goal,
            summary=summary,
            target_app=target_app,
            steps=steps,
            mode=mode,
            source_tier="TIER_3_CLOUD_AI",
        )
