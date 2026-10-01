"""Semantic Workflow Dissection Engine (Triad of Dissection & Tri-Factor Anchors).

Path: src/ai/dissector.py
Belongs to Multimodal AI Workflow Dissection Pipeline.

Implements:
1. Zero-Pixel Privacy Gate (Section 3.4): Suppresses visual frames for AXSecureTextField.
2. Clean Coordinates (Section 3.2): Normalized vectors [ymin, xmin, ymax, xmax] without pixel corruption.
3. Single-Pass Payload Assembly (Section 2 & 3.5): Compact JSON event sequence (<2KB) + max 2 keyframes.
4. Tri-Factor Target Anchor Compiler (Section 5): Compiles (AX Path, Window Ratio, Visual Crop).
5. Offline / Deterministic Fallback Mode (Section 6): Rule-based synthesis when AI is unavailable.
6. Zero codebase disruption (Section 7): Outputs standard WorkflowSpec dataclass.

Zero external dependencies: pure Python standard library.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import datetime
import json
import logging
import os
from pathlib import Path
import re
import time
from typing import Any, Dict, List, Optional, Tuple, Union
import uuid

from src.ai.credentials import get_credential_manager
from src.ai.providers import BaseAIProvider, get_ai_provider
from src.memory.models import (
    ActionType,
    CoordMode,
    TargetCoordinates,
    WorkflowSpec,
    WorkflowStep,
)
from src.memory.recorder import RawEvent, RawEventType, WindowBounds, WorkflowRecorderPipeline

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# 1. Tri-Factor Target Anchor (Section 5)
# -----------------------------------------------------------------------------

@dataclass
class TriFactorAnchor:
    """Self-healing target lookup anchor combining Accessibility, Window Ratio, and Visual Crop.

    Fallback Hierarchy during execution:
      1. Factor 1 (AX Query): kAXRole + kAXTitle + bundle_id in target window.
      2. Factor 2 (Window Ratio): norm_x / norm_y projected into live target window bounds.
      3. Factor 3 (Clean Visual Crop): Pre-action keyframe crop for template matching.
    """
    # Factor 1: Accessibility Path
    ax_role: Optional[str] = None
    ax_title: Optional[str] = None
    bundle_id: Optional[str] = None
    element_id: Optional[str] = None

    # Factor 2: Window-Relative Normalized Ratios (0.0 to 1.0)
    norm_x: Optional[float] = None
    norm_y: Optional[float] = None
    # Bounding box in normalized coords: [ymin, xmin, ymax, xmax]
    target_box: Optional[List[float]] = None

    # Factor 3: Clean Visual Crop
    visual_crop_path: Optional[str] = None
    crop_width: Optional[int] = None
    crop_height: Optional[int] = None
    visual_delta: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "factor_1_ax": {
                "ax_role": self.ax_role,
                "ax_title": self.ax_title,
                "bundle_id": self.bundle_id,
                "element_id": self.element_id,
            },
            "factor_2_ratio": {
                "norm_x": self.norm_x,
                "norm_y": self.norm_y,
                "target_box": self.target_box,
            },
            "factor_3_visual": {
                "crop_path": self.visual_crop_path,
                "crop_width": self.crop_width,
                "crop_height": self.crop_height,
                "visual_delta": self.visual_delta,
            },
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> TriFactorAnchor:
        f1 = data.get("factor_1_ax", {})
        f2 = data.get("factor_2_ratio", {})
        f3 = data.get("factor_3_visual", {})

        return cls(
            ax_role=f1.get("ax_role") or data.get("ax_role"),
            ax_title=f1.get("ax_title") or data.get("ax_title"),
            bundle_id=f1.get("bundle_id") or data.get("bundle_id"),
            element_id=f1.get("element_id") or data.get("element_id"),
            norm_x=f2.get("norm_x") if f2.get("norm_x") is not None else data.get("norm_x"),
            norm_y=f2.get("norm_y") if f2.get("norm_y") is not None else data.get("norm_y"),
            target_box=f2.get("target_box") or data.get("target_box"),
            visual_crop_path=f3.get("crop_path") or data.get("visual_crop_path"),
            crop_width=f3.get("crop_width") or data.get("crop_width"),
            crop_height=f3.get("crop_height") or data.get("crop_height"),
            visual_delta=float(f3.get("visual_delta", data.get("visual_delta", 0.0))),
        )


# -----------------------------------------------------------------------------
# 2. Zero-Pixel Privacy Gate (Section 3.4)
# -----------------------------------------------------------------------------

class ZeroPixelPrivacyFilter:
    """Enforces zero-pixel redaction on sensitive password fields and secure inputs."""

    SENSITIVE_FIELD_INDICATORS = {
        "axsecuretextfield",
        "securetextfield",
        "password",
        "passwd",
        "creditcard",
        "cvv",
        "ssn",
        "pin",
    }

    @classmethod
    def is_sensitive_element(
        cls,
        ax_role: Optional[str] = None,
        ax_subrole: Optional[str] = None,
        ax_title: Optional[str] = None,
        bundle_id: Optional[str] = None,
    ) -> bool:
        """Returns True if the element is an AXSecureTextField or password entry field."""
        if ax_subrole and ax_subrole.strip().lower() in ("axsecuretextfield", "securetextfield"):
            return True
        if ax_role and ax_role.strip().lower() == "axsecuretextfield":
            return True

        # Check title / placeholder tokens
        t_clean = (ax_title or "").strip().lower()
        if t_clean in cls.SENSITIVE_FIELD_INDICATORS:
            return True

        return False

    @classmethod
    def sanitize_event(cls, ev: RawEvent) -> Tuple[RawEvent, bool]:
        """Inspects event; if sensitive, strips keystroke content and returns (sanitized_event, is_sensitive)."""
        is_sensitive = False
        ax_subrole = getattr(ev, "ax_subrole", "") or ""
        ax_role = getattr(ev, "ax_role", "") or ""
        ax_title = getattr(ev, "ax_title", "") or ""

        if cls.is_sensitive_element(ax_role=ax_role, ax_subrole=ax_subrole, ax_title=ax_title, bundle_id=ev.bundle_id):
            is_sensitive = True

        if is_sensitive:
            sanitized = RawEvent(
                event_type=ev.event_type,
                timestamp=ev.timestamp,
                x=ev.x,
                y=ev.y,
                button=ev.button,
                key="<sensitive_input>" if ev.key else "",
                modifiers=ev.modifiers,
                bundle_id=ev.bundle_id,
                window_bounds=ev.window_bounds,
                is_dock_item=ev.is_dock_item,
                dock_item_title=ev.dock_item_title,
                recognized_text="[REDACTED_PASSWORD]",
                visual_delta=0.0,
            )
            return sanitized, True

        return ev, False


# -----------------------------------------------------------------------------
# 3. Single-Pass Payload Packer (Section 2 & 3.5)
# -----------------------------------------------------------------------------

class DissectionPayloadPacker:
    """Assembles compact JSON event sequence (<2KB text) + at most 2 summary keyframes."""

    @classmethod
    def assemble_payload(
        cls,
        raw_events: List[RawEvent],
        context_metadata: Dict[str, Any],
        captured_frames: List[Dict[str, Any]],
        baseline_steps: Optional[List[WorkflowStep]] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[bytes], Optional[bytes]]:
        """Compacts events into JSON representations and resolves initial/terminal keyframes.

        Returns:
            (event_sequence, initial_frame_bytes, final_frame_bytes)
        """
        compact_events: List[Dict[str, Any]] = []

        # Zero-pixel filter pass
        privacy_filtered_events: List[RawEvent] = []
        has_sensitive_step = False
        for ev in raw_events:
            sanitized, is_sens = ZeroPixelPrivacyFilter.sanitize_event(ev)
            if is_sens:
                has_sensitive_step = True
            privacy_filtered_events.append(sanitized)

        # Build compact semantic action items from raw events (fallback/baseline)
        for idx, ev in enumerate(privacy_filtered_events):
            ev_type_str = ev.event_type.value if hasattr(ev.event_type, "value") else str(ev.event_type).lower()
            item: Dict[str, Any] = {
                "index": idx + 1,
                "timestamp": round(ev.timestamp, 3),
                "type": ev_type_str,
            }

            if ev.bundle_id:
                item["app"] = ev.bundle_id.split(".")[-1]
                item["bundle_id"] = ev.bundle_id

            if ev.key:
                item["key"] = ev.key
                if ev.modifiers:
                    item["modifiers"] = ev.modifiers

            if ev_type_str in ("mouse_down", "mousedown", "click", "mouse_drag", "mousedrag"):
                item["button"] = ev.button
                # Calculate window-relative normalized ratio box [ymin, xmin, ymax, xmax]
                if ev.window_bounds and ev.window_bounds.width > 0 and ev.window_bounds.height > 0:
                    wb = ev.window_bounds
                    nx = max(0.0, min(1.0, (ev.x - wb.x) / wb.width))
                    ny = max(0.0, min(1.0, (ev.y - wb.y) / wb.height))
                    item["norm_x"] = round(nx, 4)
                    item["norm_y"] = round(ny, 4)
                    # Coordinate format: Normalized [ymin, xmin, ymax, xmax] (0.0 to 1.0)
                    box_pad_x = 20.0 / wb.width
                    box_pad_y = 20.0 / wb.height
                    item["target_box"] = [
                        round(max(0.0, ny - box_pad_y), 4),
                        round(max(0.0, nx - box_pad_x), 4),
                        round(min(1.0, ny + box_pad_y), 4),
                        round(min(1.0, nx + box_pad_x), 4),
                    ]
                else:
                    item["screen_x"] = int(ev.x)
                    item["screen_y"] = int(ev.y)

            if ev.recognized_text:
                item["recognized_text"] = ev.recognized_text
            if ev.dock_item_title:
                item["dock_item_title"] = ev.dock_item_title

            compact_events.append(item)

        # If high-level coalesced steps are provided, prefer them for coherent AI synthesis
        if baseline_steps:
            structured_sequence: List[Dict[str, Any]] = []
            for s in baseline_steps:
                st_act = s.action.value if hasattr(s.action, "value") else str(s.action)
                t = s.target if isinstance(s.target, dict) else {}
                app = t.get("app_name") or (t.get("bundle_id", "").split(".")[-1] if t.get("bundle_id") else "")
                rec_txt = t.get("recognized_text") or t.get("ax_title") or ""
                coords = s.coordinates
                nx = t.get("norm_x") if t.get("norm_x") is not None else (coords.norm_x if coords else None)
                ny = t.get("norm_y") if t.get("norm_y") is not None else (coords.norm_y if coords else None)

                s_item: Dict[str, Any] = {
                    "step_index": s.order,
                    "order": s.order,
                    "action": st_act,
                    "description": s.description,
                }
                if app:
                    s_item["app"] = app
                if t.get("bundle_id"):
                    s_item["bundle_id"] = t["bundle_id"]
                if rec_txt:
                    s_item["recognized_text"] = rec_txt

                if s.payload:
                    if "text" in s.payload:
                        s_item["text"] = s.payload["text"]
                    if "keys" in s.payload:
                        s_item["keys"] = s.payload["keys"]

                if nx is not None and ny is not None:
                    s_item["norm_x"] = round(float(nx), 4)
                    s_item["norm_y"] = round(float(ny), 4)

                anchor = t.get("tri_factor_anchor")
                if isinstance(anchor, dict) and "factor_2_ratio" in anchor:
                    tbox = anchor["factor_2_ratio"].get("target_box")
                    if tbox:
                        s_item["target_box"] = tbox

                structured_sequence.append(s_item)
            effective_sequence = structured_sequence
        else:
            effective_sequence = compact_events

        # Select at most 2 summary keyframes (initial window state + terminal result state)
        initial_bytes: Optional[bytes] = None
        final_bytes: Optional[bytes] = None

        if captured_frames and not has_sensitive_step:
            valid_frames = [f for f in captured_frames if f.get("path") and os.path.exists(f["path"])]
            if valid_frames:
                # 1. Initial keyframe (start of action)
                first_f = valid_frames[0]
                try:
                    p = Path(first_f["path"])
                    initial_bytes = cls._compress_keyframe(p)
                except Exception as e:
                    logger.debug("Failed reading initial keyframe: %s", e)

                # 2. Terminal keyframe (final outcome)
                last_f = valid_frames[-1]
                if last_f != first_f:
                    try:
                        p = Path(last_f["path"])
                        final_bytes = cls._compress_keyframe(p)
                    except Exception as e:
                        logger.debug("Failed reading terminal keyframe: %s", e)

        return effective_sequence, initial_bytes, final_bytes

    @staticmethod
    def _compress_keyframe(image_path: Path, max_dimension: int = 800, quality: int = 60) -> Optional[bytes]:
        """Compresses keyframe using macOS native sips tool to drastically minimize AI token cost."""
        if not image_path.exists() or image_path.stat().st_size == 0:
            return None
        # If already lightweight (< 60KB), return as is
        if image_path.stat().st_size <= 60 * 1024:
            return image_path.read_bytes()

        import tempfile
        import subprocess
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                tmp_path = tmp.name

            cmd = [
                "sips",
                "-Z", str(max_dimension),
                "-s", "format", "jpeg",
                "-s", "formatOptions", str(quality),
                str(image_path),
                "--out", tmp_path,
            ]
            res = subprocess.run(cmd, capture_output=True, check=False, timeout=1.0)
            if res.returncode == 0 and os.path.exists(tmp_path):
                compressed = Path(tmp_path).read_bytes()
                return compressed
        except Exception as e:
            logger.debug("Native keyframe compression fallback: %s", e)
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

        try:
            return image_path.read_bytes()
        except Exception:
            return None


# -----------------------------------------------------------------------------
# 4. Multimodal Workflow Dissector Engine (The Core Engine)
# -----------------------------------------------------------------------------

class MultimodalWorkflowDissector:
    """Orchestrates AI-assisted semantic dissection with automatic deterministic offline fallback."""

    def __init__(
        self,
        provider: Optional[BaseAIProvider] = None,
        recorder_pipeline: Optional[WorkflowRecorderPipeline] = None,
        auto_detect_provider: bool = True,
    ) -> None:
        self.provider = provider
        self.auto_detect_provider = auto_detect_provider
        self.recorder_pipeline = recorder_pipeline or WorkflowRecorderPipeline()

    def dissect(
        self,
        raw_events: List[RawEvent],
        session_id: str,
        name: str = "Demonstrated Task",
        canonical_trigger: str = "",
        description: str = "",
        target_bundle_id: str = "",
        captured_frames: Optional[List[Dict[str, Any]]] = None,
        session_dir: Optional[Union[str, Path]] = None,
    ) -> WorkflowSpec:
        """Executes single-pass dissection.

        If AI provider is available, invokes single-pass AI synthesis.
        If offline, no key, or on AI failure, seamlessly falls back to the deterministic pipeline.
        """
        captured_frames = captured_frames or []

        # 1. First, run local deterministic baseline via WorkflowRecorderPipeline (Ground Truth)
        baseline_spec = self.recorder_pipeline.process_raw_events(
            raw_events=raw_events,
            name=name,
            canonical_trigger=canonical_trigger or name.lower(),
            description=description,
            target_bundle_id=target_bundle_id,
        )

        # 2. Compile Tri-Factor Anchors on the baseline steps
        self._compile_tri_factor_anchors(baseline_spec, raw_events, captured_frames)

        # 3. Check AI Provider availability
        active_provider = self.provider or (get_ai_provider() if self.auto_detect_provider else None)
        if not active_provider:
            logger.info("No AI provider configured. Engaging Offline Deterministic Fallback Mode.")
            return baseline_spec

        # 4. Pack payload for AI provider
        context_metadata = {
            "session_id": session_id,
            "name": name,
            "canonical_trigger": canonical_trigger,
            "description": description,
            "target_app": target_bundle_id.split(".")[-1] if target_bundle_id else "Desktop",
            "target_bundle_id": target_bundle_id,
            "event_count": len(raw_events),
            "step_count": len(baseline_spec.steps),
        }

        compact_events, initial_bytes, final_bytes = DissectionPayloadPacker.assemble_payload(
            raw_events=raw_events,
            context_metadata=context_metadata,
            captured_frames=captured_frames,
            baseline_steps=baseline_spec.steps,
        )

        # 5. Single-Pass High-Level AI Synthesis
        try:
            start_t = time.time()
            ai_data = active_provider.dissect_workflow(
                event_sequence=compact_events,
                context_metadata=context_metadata,
                initial_frame_bytes=initial_bytes,
                final_frame_bytes=final_bytes,
            )
            elapsed = time.time() - start_t
            logger.info("AI Dissection completed via '%s' in %.3fs", active_provider.provider_name, elapsed)

            # 6. Merge AI semantics with deterministic ground truth
            synthesized_spec = self._merge_ai_synthesis(
                baseline_spec=baseline_spec,
                ai_data=ai_data,
                provider_name=active_provider.provider_name,
            )
            return synthesized_spec

        except Exception as e:
            logger.warning(
                "AI provider '%s' failed (%s). Safely falling back to deterministic baseline.",
                active_provider.provider_name,
                e,
            )
            baseline_spec.environment["ai_dissection_status"] = "fallback"
            baseline_spec.environment["ai_dissection_error"] = str(e)
            return baseline_spec

    def _compile_tri_factor_anchors(
        self,
        spec: WorkflowSpec,
        raw_events: List[RawEvent],
        captured_frames: List[Dict[str, Any]],
    ) -> None:
        """Compiles Tri-Factor Target Anchors onto each step in the specification."""
        # Find visual frames associated with pre-actions
        frame_lookup: Dict[str, str] = {}
        for f in captured_frames:
            label = f.get("label", "")
            path = f.get("path", "")
            if label and path and os.path.exists(path):
                frame_lookup[label] = path

        for step in spec.steps:
            target = step.target if isinstance(step.target, dict) else {}
            coords = step.coordinates

            norm_x = target.get("norm_x")
            norm_y = target.get("norm_y")
            if norm_x is None and coords and coords.norm_x is not None:
                norm_x = coords.norm_x
                norm_y = coords.norm_y

            # Factor 1: Accessibility
            ax_role = target.get("ax_role") or ("AXButton" if step.action == ActionType.CLICK else None)
            ax_title = target.get("ax_title")
            click_x = target.get("screen_x") or (coords.abs_x if coords else None)
            click_y = target.get("screen_y") or (coords.abs_y if coords else None)

            # Find matching event in raw_events closest to click_x, click_y
            best_match_ev: Optional[RawEvent] = None
            if click_x is not None and click_y is not None:
                min_d = float("inf")
                for ev in raw_events:
                    ev_t = ev.event_type.value if hasattr(ev.event_type, "value") else str(ev.event_type).lower()
                    if ev_t in ("mouse_down", "mousedown", "click", "mouse_drag", "mousedrag"):
                        d = ((ev.x - click_x) ** 2 + (ev.y - click_y) ** 2) ** 0.5
                        if d < min_d:
                            min_d = d
                            best_match_ev = ev

            if not ax_title:
                if best_match_ev and getattr(best_match_ev, "ax_title", None):
                    ax_title = best_match_ev.ax_title
                    if getattr(best_match_ev, "ax_role", None):
                        ax_role = getattr(best_match_ev, "ax_role")
                elif best_match_ev and getattr(best_match_ev, "recognized_text", None):
                    ax_title = best_match_ev.recognized_text

            if not ax_title:
                ax_title = target.get("element_name") or target.get("app_name")
            bundle_id = target.get("bundle_id") or (best_match_ev.bundle_id if best_match_ev else None)

            # Enrich target with recognized_text and visual_delta from video probe if present
            if not target.get("recognized_text") and best_match_ev and getattr(best_match_ev, "recognized_text", None):
                target["recognized_text"] = best_match_ev.recognized_text
            if not target.get("visual_delta") and best_match_ev and getattr(best_match_ev, "visual_delta", 0.0) > 0.0:
                target["visual_delta"] = float(best_match_ev.visual_delta)

            # Factor 2: Window-Relative Ratio
            target_box = None
            if norm_x is not None and norm_y is not None:
                target_box = [
                    round(max(0.0, norm_y - 0.05), 4),
                    round(max(0.0, norm_x - 0.05), 4),
                    round(min(1.0, norm_y + 0.05), 4),
                    round(min(1.0, norm_x + 0.05), 4),
                ]

            # Factor 3: Clean Visual Crop
            crop_path = None
            for key, p in frame_lookup.items():
                if (
                    step.step_id in key
                    or f"action_{step.order}" in key
                    or f"action_{step.order}_" in key
                    or f"step_{step.order}" in key
                    or f"pre_action_{step.order - 1}" in key
                    or f"pre_action_{step.order}" in key
                ):
                    crop_path = p
                    break

            anchor = TriFactorAnchor(
                ax_role=ax_role,
                ax_title=ax_title,
                bundle_id=bundle_id,
                norm_x=norm_x,
                norm_y=norm_y,
                target_box=target_box,
                visual_crop_path=crop_path,
                visual_delta=float(target.get("visual_delta", 0.0)),
            )

            # Store in target["tri_factor_anchor"]
            target["tri_factor_anchor"] = anchor.to_dict()
            step.target = target

    def _merge_ai_synthesis(
        self,
        baseline_spec: WorkflowSpec,
        ai_data: Dict[str, Any],
        provider_name: str,
    ) -> WorkflowSpec:
        """Merges AI-synthesized high-level metadata (name, triggers, parameters, step descriptions)
        with deterministic ground-truth coordinates and anchors.
        """
        # 1. Update workflow title & descriptions
        ai_name = ai_data.get("workflow_name")
        if ai_name and isinstance(ai_name, str):
            baseline_spec.name = ai_name.strip()

        ai_summary = ai_data.get("summary")
        if ai_summary and isinstance(ai_summary, str):
            baseline_spec.description = ai_summary.strip()

        ai_trigger = ai_data.get("canonical_trigger")
        if ai_trigger and isinstance(ai_trigger, str):
            if isinstance(baseline_spec.triggers, dict):
                baseline_spec.triggers["canonical"] = ai_trigger.strip()
                aliases = list(baseline_spec.triggers.get("aliases", []))
                if ai_trigger.strip() not in aliases:
                    aliases.append(ai_trigger.strip())
                baseline_spec.triggers["aliases"] = aliases

        # 2. Extract and incorporate parameters
        ai_params = ai_data.get("parameters", [])
        param_dict: Dict[str, Any] = {}
        param_replacements: Dict[str, str] = {}
        if isinstance(ai_params, list) and ai_params:
            for p in ai_params:
                if isinstance(p, dict) and "name" in p:
                    p_name = str(p["name"]).strip()
                    def_val = str(p.get("default_value", ""))
                    param_dict[p_name] = def_val
                    if def_val:
                        param_replacements[def_val] = f"{{{{{p_name}}}}}"
            baseline_spec.parameters = param_dict

        # 3. Match and enhance steps with AI semantics (resilient to schema variances)
        ai_steps = ai_data.get("steps", [])
        if isinstance(ai_steps, list) and ai_steps and baseline_spec.steps:
            baseline_by_order: Dict[int, WorkflowStep] = {s.order: s for s in baseline_spec.steps}
            matched_orders: set[int] = set()

            for ai_step in ai_steps:
                if not isinstance(ai_step, dict):
                    continue

                target_step: Optional[WorkflowStep] = None
                # Priority 1: Match by step_index
                ref_idx = ai_step.get("step_index")
                if ref_idx is not None and int(ref_idx) in baseline_by_order:
                    target_step = baseline_by_order[int(ref_idx)]

                # Priority 2: Match by order or step_number
                if target_step is None:
                    order_val = ai_step.get("order") or ai_step.get("step_number")
                    if order_val is not None and int(order_val) in baseline_by_order:
                        if int(order_val) not in matched_orders:
                            target_step = baseline_by_order[int(order_val)]

                # Priority 3: Match sequentially by action type
                if target_step is None:
                    ai_act = str(ai_step.get("action", "")).lower().replace("_", "")
                    for b_ord, b_st in baseline_by_order.items():
                        if b_ord not in matched_orders:
                            b_act = (b_st.action.value if hasattr(b_st.action, "value") else str(b_st.action)).lower().replace("_", "")
                            if ai_act and (ai_act == b_act or (ai_act in ("typetext", "pastetext") and b_act in ("typetext", "pastetext"))):
                                target_step = b_st
                                break

                if target_step is None:
                    continue

                matched_orders.add(target_step.order)

                # Update semantic description
                desc = ai_step.get("description") or ai_step.get("name") or ai_step.get("step_name")
                if desc and isinstance(desc, str) and desc.strip():
                    target_step.description = desc.strip()

                # Adopt parameterized payload text or keys if present
                ai_payload = ai_step.get("payload")
                if isinstance(ai_payload, dict):
                    if "text" in ai_payload and target_step.action in (ActionType.TYPE_TEXT, ActionType.PASTE_TEXT):
                        target_step.payload["text"] = str(ai_payload["text"])
                    if "keys" in ai_payload and target_step.action == ActionType.PRESS_HOTKEY:
                        target_step.payload["keys"] = ai_payload["keys"]

                # Apply parameter templating to text if payload text contains literal default value
                if target_step.action in (ActionType.TYPE_TEXT, ActionType.PASTE_TEXT):
                    cur_text = str(target_step.payload.get("text", ""))
                    for def_v, tmpl in param_replacements.items():
                        if def_v in cur_text and tmpl not in cur_text:
                            target_step.payload["text"] = cur_text.replace(def_v, tmpl)
                            if def_v in target_step.description:
                                target_step.description = target_step.description.replace(def_v, tmpl)
                            break

                # Enrich target semantics from AI if provided
                ai_target = ai_step.get("target")
                if isinstance(ai_target, dict) and isinstance(target_step.target, dict):
                    if "element_name" in ai_target and ai_target["element_name"]:
                        target_step.target["element_name"] = str(ai_target["element_name"])
                    if "app_name" in ai_target and ai_target["app_name"] and not target_step.target.get("app_name"):
                        target_step.target["app_name"] = str(ai_target["app_name"])
                    if "bundle_id" in ai_target and ai_target["bundle_id"] and not target_step.target.get("bundle_id"):
                        target_step.target["bundle_id"] = str(ai_target["bundle_id"])

        # 4. Mark environment metadata
        if hasattr(baseline_spec, "environment") and isinstance(baseline_spec.environment, dict):
            baseline_spec.environment["ai_dissection_status"] = "success"
            baseline_spec.environment["ai_provider"] = provider_name
            baseline_spec.environment["ai_dissected_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()

        return baseline_spec
