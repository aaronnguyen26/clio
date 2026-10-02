"""Walkthrough Data Models & Schema Contracts.

Path: src/walkthrough/models.py
Defines the schema for interactive on-screen pedagogical walkthroughs,
including educational steps, target element coordinates, and playback modes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
import time
from typing import Any, Dict, List, Optional, Tuple

from src.memory.models import (
    ActionType,
    CoordMode,
    TargetCoordinates,
    WorkflowSpec,
    WorkflowStep,
)


class WalkthroughMode(str, Enum):
    """Execution modality for the walkthrough tutor."""
    GUIDED_DEMO = "guided_demo"        # Clio moves cursor, explains, and demonstrates clicks
    INTERACTIVE_TUTOR = "interactive"  # Clio points to where to click, waits for user action


class TeachingAction(str, Enum):
    """Pedagogical actions performed during walkthrough steps."""
    MOVE_AND_HOVER = "move_and_hover"
    PULSE_BEACON = "pulse_beacon"
    DEMONSTRATE_CLICK = "demonstrate_click"
    WAIT_FOR_USER_CLICK = "wait_for_user_click"
    DEMONSTRATE_HOTKEY = "demonstrate_hotkey"
    DEMONSTRATE_TYPE = "demonstrate_type"
    HIGHLIGHT_REGION = "highlight_region"


@dataclass
class WalkthroughStep:
    """A single instructional step in an on-screen walkthrough."""
    step_index: int
    title: str
    instruction: str                      # Concise imperative instruction ("Click the Apple icon ")
    explanation: str                      # Context/reason ("Opens the macOS system menu")
    action_type: TeachingAction = TeachingAction.MOVE_AND_HOVER
    target_app: str = ""                  # e.g. "System Settings", "Finder"
    target_bundle_id: Optional[str] = None
    target_element_query: Dict[str, Any] = field(default_factory=dict) # {"ax_role": "AXMenuItem", "ax_title": "Displays"}
    fallback_screen_coords: Optional[Tuple[float, float]] = None       # (abs_x, abs_y)
    spotlight_bounds: Optional[Tuple[float, float, float, float]] = None # (x, y, w, h)
    hotkey_combo: Optional[List[str]] = None
    text_to_type: Optional[str] = None
    pre_delay_seconds: float = 0.5
    post_delay_seconds: float = 1.0

    def to_dict(self) -> Dict[str, Any]:
        """Serializes step to JSON-compatible dictionary."""
        d = asdict(self)
        d["action_type"] = self.action_type.value
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> WalkthroughStep:
        """Constructs WalkthroughStep from dictionary."""
        action_val = data.get("action_type", TeachingAction.MOVE_AND_HOVER.value)
        if isinstance(action_val, str):
            try:
                action_type = TeachingAction(action_val)
            except ValueError:
                action_type = TeachingAction.MOVE_AND_HOVER
        else:
            action_type = action_val

        coords = data.get("fallback_screen_coords")
        if coords and isinstance(coords, (list, tuple)) and len(coords) == 2:
            coords = (float(coords[0]), float(coords[1]))
        else:
            coords = None

        bounds = data.get("spotlight_bounds")
        if bounds and isinstance(bounds, (list, tuple)) and len(bounds) == 4:
            bounds = (float(bounds[0]), float(bounds[1]), float(bounds[2]), float(bounds[3]))
        else:
            bounds = None

        return cls(
            step_index=int(data.get("step_index", 1)),
            title=str(data.get("title", "")),
            instruction=str(data.get("instruction", "")),
            explanation=str(data.get("explanation", "")),
            action_type=action_type,
            target_app=str(data.get("target_app", "")),
            target_bundle_id=data.get("target_bundle_id"),
            target_element_query=dict(data.get("target_element_query", {})),
            fallback_screen_coords=coords,
            spotlight_bounds=bounds,
            hotkey_combo=list(data["hotkey_combo"]) if data.get("hotkey_combo") else None,
            text_to_type=data.get("text_to_type"),
            pre_delay_seconds=float(data.get("pre_delay_seconds", 0.5)),
            post_delay_seconds=float(data.get("post_delay_seconds", 1.0)),
        )


@dataclass
class WalkthroughPlan:
    """An end-to-end interactive desktop walkthrough plan."""
    walkthrough_id: str
    goal: str
    summary: str
    target_app: str
    steps: List[WalkthroughStep]
    mode: WalkthroughMode = WalkthroughMode.GUIDED_DEMO
    created_at: float = field(default_factory=time.time)
    source_tier: str = "UNKNOWN"

    def to_dict(self) -> Dict[str, Any]:
        """Serializes plan to JSON-compatible dictionary."""
        return {
            "walkthrough_id": self.walkthrough_id,
            "goal": self.goal,
            "summary": self.summary,
            "target_app": self.target_app,
            "mode": self.mode.value,
            "created_at": self.created_at,
            "source_tier": self.source_tier,
            "total_steps": len(self.steps),
            "steps": [s.to_dict() for s in self.steps],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> WalkthroughPlan:
        """Constructs WalkthroughPlan from dictionary."""
        mode_val = data.get("mode", WalkthroughMode.GUIDED_DEMO.value)
        try:
            mode = WalkthroughMode(mode_val)
        except ValueError:
            mode = WalkthroughMode.GUIDED_DEMO

        raw_steps = data.get("steps", [])
        steps = [WalkthroughStep.from_dict(s) for s in raw_steps]

        return cls(
            walkthrough_id=str(data.get("walkthrough_id", "")),
            goal=str(data.get("goal", "")),
            summary=str(data.get("summary", "")),
            target_app=str(data.get("target_app", "")),
            steps=steps,
            mode=mode,
            created_at=float(data.get("created_at", time.time())),
            source_tier=str(data.get("source_tier", "UNKNOWN")),
        )

    def to_workflow_spec(self) -> WorkflowSpec:
        """Converts WalkthroughPlan to an executable WorkflowSpec for persistent SQLite storage."""
        spec_steps: List[WorkflowStep] = []
        for s in self.steps:
            action = ActionType.MOVE_MOUSE
            payload: Dict[str, Any] = {}
            if s.action_type == TeachingAction.DEMONSTRATE_CLICK or s.action_type == TeachingAction.WAIT_FOR_USER_CLICK:
                action = ActionType.CLICK
            elif s.action_type == TeachingAction.DEMONSTRATE_HOTKEY:
                action = ActionType.PRESS_HOTKEY
                if s.hotkey_combo:
                    payload["keys"] = s.hotkey_combo
            elif s.action_type == TeachingAction.DEMONSTRATE_TYPE:
                action = ActionType.TYPE_TEXT
                if s.text_to_type:
                    payload["text"] = s.text_to_type

            coords = None
            if s.fallback_screen_coords:
                coords = TargetCoordinates(
                    mode=CoordMode.SCREEN_ABSOLUTE,
                    abs_x=s.fallback_screen_coords[0],
                    abs_y=s.fallback_screen_coords[1],
                )

            spec_steps.append(
                WorkflowStep(
                    step_id=f"step_{s.step_index}",
                    order=s.step_index,
                    description=s.instruction,
                    action=action,
                    coordinates=coords,
                    payload=payload,
                    target={"app_name": s.target_app, **s.target_element_query},
                    timing={
                        "pre_delay_ms": int(s.pre_delay_seconds * 1000),
                        "post_delay_ms": int(s.post_delay_seconds * 1000),
                    },
                )
            )

        return WorkflowSpec(
            id=self.walkthrough_id,
            name=f"Walkthrough: {self.goal}",
            description=self.summary,
            triggers={"canonical": self.goal.lower(), "aliases": [f"teach me {self.goal.lower()}"]},
            target_app={"app_name": self.target_app},
            steps=spec_steps,
        )

    @classmethod
    def from_workflow_spec(cls, spec: WorkflowSpec, mode: WalkthroughMode = WalkthroughMode.GUIDED_DEMO) -> WalkthroughPlan:
        """Reconstructs a pedagogical WalkthroughPlan from an existing WorkflowSpec."""
        steps: List[WalkthroughStep] = []
        for w_step in spec.steps:
            action_type = TeachingAction.MOVE_AND_HOVER
            if w_step.action == ActionType.CLICK:
                action_type = TeachingAction.DEMONSTRATE_CLICK
            elif w_step.action == ActionType.PRESS_HOTKEY:
                action_type = TeachingAction.DEMONSTRATE_HOTKEY
            elif w_step.action in (ActionType.TYPE_TEXT, ActionType.PASTE_TEXT):
                action_type = TeachingAction.DEMONSTRATE_TYPE

            coords = None
            if w_step.coordinates and w_step.coordinates.abs_x is not None and w_step.coordinates.abs_y is not None:
                coords = (float(w_step.coordinates.abs_x), float(w_step.coordinates.abs_y))

            app_name = ""
            if isinstance(w_step.target, dict):
                app_name = w_step.target.get("app_name", "")
            if not app_name and isinstance(spec.target_app, dict):
                app_name = spec.target_app.get("app_name", "")

            keys = None
            if isinstance(w_step.payload, dict) and "keys" in w_step.payload:
                keys = list(w_step.payload["keys"])

            text = None
            if isinstance(w_step.payload, dict) and "text" in w_step.payload:
                text = str(w_step.payload["text"])

            steps.append(
                WalkthroughStep(
                    step_index=w_step.order,
                    title=f"Step {w_step.order}",
                    instruction=w_step.description or f"Perform {w_step.action.value}",
                    explanation=f"Demonstrates {w_step.action.value} on {app_name or 'target element'}",
                    action_type=action_type,
                    target_app=app_name,
                    fallback_screen_coords=coords,
                    hotkey_combo=keys,
                    text_to_type=text,
                    pre_delay_seconds=float(w_step.timing.get("pre_delay_ms", 500)) / 1000.0 if isinstance(w_step.timing, dict) else 0.5,
                    post_delay_seconds=float(w_step.timing.get("post_delay_ms", 1000)) / 1000.0 if isinstance(w_step.timing, dict) else 1.0,
                )
            )

        return cls(
            walkthrough_id=spec.id,
            goal=spec.triggers.get("canonical", spec.name) if isinstance(spec.triggers, dict) else spec.name,
            summary=spec.description,
            target_app=spec.target_app.get("app_name", "") if isinstance(spec.target_app, dict) else "",
            steps=steps,
            mode=mode,
            source_tier="TIER_0_MEMORY_CACHE",
        )
