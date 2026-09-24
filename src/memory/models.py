"""Workflow schema data models, validation, and serialization.

Belongs to FEAT-MEM-01 (WorkflowSchemaModels).
Pure Python standard library with zero third-party dependencies.
Supports typed dataclasses, schema validation, JSON, and standard-library YAML serialization.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import datetime
from enum import Enum
import json
import re
from typing import Any, Dict, List, Optional, Tuple, Union


class ActionType(str, Enum):
    """Supported actuator primitive actions."""
    LAUNCH_APP = "launch_app"
    FOCUS_APP = "focus_app"
    MOVE_MOUSE = "move_mouse"
    CLICK = "click"
    DOUBLE_CLICK = "double_click"
    RIGHT_CLICK = "right_click"
    DRAG = "drag"
    SCROLL = "scroll"
    TYPE_TEXT = "type_text"
    PASTE_TEXT = "paste_text"
    PRESS_HOTKEY = "press_hotkey"
    WAIT_WINDOW = "wait_window"
    WAIT = "wait"
    OPEN_URL = "open_url"


class CoordMode(str, Enum):
    """Target coordinate projection modes."""
    WINDOW_RELATIVE_RATIO = "window_relative_ratio"
    WINDOW_RELATIVE_OFFSET = "window_relative_offset"
    SCREEN_ABSOLUTE = "screen_absolute"


class ValidationError(ValueError):
    """Raised when workflow schema validation fails."""
    pass


class SerializationError(ValueError):
    """Raised when JSON or YAML serialization/deserialization fails."""
    pass


@dataclass
class TargetCoordinates:
    """Coordinate targeting metadata for mouse actions."""
    mode: CoordMode = CoordMode.WINDOW_RELATIVE_RATIO
    norm_x: Optional[float] = None
    norm_y: Optional[float] = None
    abs_x: Optional[int] = None
    abs_y: Optional[int] = None
    offset_x: Optional[int] = None
    offset_y: Optional[int] = None

    def validate(self) -> None:
        if self.mode == CoordMode.WINDOW_RELATIVE_RATIO:
            if self.norm_x is not None and not (0.0 <= float(self.norm_x) <= 1.0):
                raise ValidationError(f"norm_x must be within [0.0, 1.0], got {self.norm_x}")
            if self.norm_y is not None and not (0.0 <= float(self.norm_y) <= 1.0):
                raise ValidationError(f"norm_y must be within [0.0, 1.0], got {self.norm_y}")
        elif self.mode == CoordMode.SCREEN_ABSOLUTE:
            if self.abs_x is not None and self.abs_x < 0:
                raise ValidationError(f"abs_x must be >= 0, got {self.abs_x}")
            if self.abs_y is not None and self.abs_y < 0:
                raise ValidationError(f"abs_y must be >= 0, got {self.abs_y}")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode.value if isinstance(self.mode, CoordMode) else str(self.mode),
            "norm_x": self.norm_x,
            "norm_y": self.norm_y,
            "abs_x": self.abs_x,
            "abs_y": self.abs_y,
            "offset_x": self.offset_x,
            "offset_y": self.offset_y,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> TargetCoordinates:
        d = dict(data)
        if "mode" in d and isinstance(d["mode"], str):
            d["mode"] = CoordMode(d["mode"])
        return cls(**d)


@dataclass
class StepTiming:
    pre_delay_ms: int = 0
    post_delay_ms: int = 50
    timeout_ms: int = 5000

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> StepTiming:
        return cls(**data)


@dataclass
class StepVerification:
    condition: str = "none"
    expected: Optional[str] = None
    optional: bool = False
    timeout_ms: int = 5000

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> StepVerification:
        return cls(**data)


@dataclass
class StepErrorHandling:
    on_failure: str = "retry"  # "retry", "abort", "skip", "fallback"
    max_retries: int = 1
    fallback_step_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> StepErrorHandling:
        return cls(**data)


@dataclass
class WorkflowStep:
    step_id: str
    order: int
    description: str
    action: ActionType
    target: Dict[str, Any] = field(default_factory=dict)
    payload: Dict[str, Any] = field(default_factory=dict)
    timing: Dict[str, Any] = field(default_factory=lambda: {"pre_delay_ms": 0, "post_delay_ms": 50, "timeout_ms": 5000})
    verification: Dict[str, Any] = field(default_factory=dict)
    error_handling: Dict[str, Any] = field(default_factory=lambda: {"on_failure": "retry", "max_retries": 1})
    coordinates: Optional[TargetCoordinates] = None

    def validate(self) -> None:
        if not self.step_id or not isinstance(self.step_id, str):
            raise ValidationError("step_id must be a non-empty string")
        if not isinstance(self.order, int) or self.order < 1:
            raise ValidationError(f"order must be a positive integer >= 1, got {self.order}")
        if not isinstance(self.description, str):
            raise ValidationError("description must be a string")
        if not isinstance(self.action, (ActionType, str)):
            raise ValidationError(f"Invalid action type: {self.action}")
        if self.coordinates is not None:
            self.coordinates.validate()

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["action"] = self.action.value if isinstance(self.action, ActionType) else str(self.action)
        if self.coordinates is not None:
            d["coordinates"] = self.coordinates.to_dict()
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> WorkflowStep:
        d = dict(data)
        if "action" in d and isinstance(d["action"], str):
            try:
                d["action"] = ActionType(d["action"])
            except ValueError:
                pass
        if "coordinates" in d and d["coordinates"] is not None:
            if isinstance(d["coordinates"], dict):
                d["coordinates"] = TargetCoordinates.from_dict(d["coordinates"])
        return cls(**d)


@dataclass
class WorkflowSpec:
    id: str
    name: str
    description: str = ""
    schema_version: str = "1.0.0"
    version: int = 1
    author: str = "user"
    created_at: str = field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat())
    tags: List[str] = field(default_factory=list)
    triggers: Dict[str, Any] = field(default_factory=lambda: {"canonical": "", "aliases": [], "keywords": []})
    target_app: Dict[str, Any] = field(default_factory=dict)
    environment: Dict[str, Any] = field(default_factory=dict)
    parameters: Dict[str, Any] = field(default_factory=dict)
    pre_conditions: List[Dict[str, Any]] = field(default_factory=list)
    steps: List[WorkflowStep] = field(default_factory=list)
    post_conditions: List[Dict[str, Any]] = field(default_factory=list)

    def validate(self) -> None:
        if not self.id or not isinstance(self.id, str):
            raise ValidationError("Workflow id must be a non-empty string")
        if not re.match(r"^[a-zA-Z0-9_\-]+$", self.id):
            raise ValidationError(f"Workflow id must contain only alphanumeric, hyphen, or underscore characters: {self.id}")
        if not self.name or not isinstance(self.name, str):
            raise ValidationError("Workflow name must be a non-empty string")
        if not isinstance(self.version, int) or self.version < 1:
            raise ValidationError(f"Workflow version must be an integer >= 1, got {self.version}")
        if not isinstance(self.steps, list):
            raise ValidationError("Workflow steps must be a list")
        for step in self.steps:
            if not isinstance(step, WorkflowStep):
                raise ValidationError(f"Each step must be a WorkflowStep instance, got {type(step)}")
            step.validate()

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["steps"] = [s.to_dict() if isinstance(s, WorkflowStep) else s for s in self.steps]
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> WorkflowSpec:
        d = dict(data)
        steps_raw = d.pop("steps", [])
        steps = [WorkflowStep.from_dict(s) if isinstance(s, dict) else s for s in steps_raw]
        return cls(steps=steps, **d)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @classmethod
    def from_json(cls, json_str: str) -> WorkflowSpec:
        try:
            data = json.loads(json_str)
        except Exception as e:
            raise SerializationError(f"Failed to parse JSON for WorkflowSpec: {e}") from e
        return cls.from_dict(data)

    def to_yaml(self) -> str:
        """Pure Python standard library YAML serializer with zero 3rd-party dependencies."""
        try:
            import yaml
            return yaml.safe_dump(self.to_dict(), sort_keys=False)
        except ImportError:
            return self._to_yaml_native(self.to_dict())

    @classmethod
    def from_yaml(cls, yaml_str: str) -> WorkflowSpec:
        """Pure Python standard library YAML deserializer."""
        try:
            import yaml
            data = yaml.safe_load(yaml_str)
            return cls.from_dict(data)
        except ImportError:
            clean_str = yaml_str.strip()
            if clean_str.startswith("{"):
                return cls.from_json(clean_str)
            data = cls._from_yaml_native(clean_str)
            return cls.from_dict(data)

    @staticmethod
    def _to_yaml_native(obj: Any, indent_level: int = 0) -> str:
        """Recursive zero-dependency YAML dumper for primitives, dicts, and lists."""
        pad = "  " * indent_level
        if isinstance(obj, dict):
            if not obj:
                return "{}"
            lines = []
            for k, v in obj.items():
                if isinstance(v, dict):
                    if not v:
                        lines.append(f"{pad}{k}: {{}}")
                    else:
                        lines.append(f"{pad}{k}:")
                        lines.append(WorkflowSpec._to_yaml_native(v, indent_level + 1))
                elif isinstance(v, list):
                    if not v:
                        lines.append(f"{pad}{k}: []")
                    else:
                        lines.append(f"{pad}{k}:")
                        lines.append(WorkflowSpec._to_yaml_native(v, indent_level + 1))
                elif v is None:
                    lines.append(f"{pad}{k}: null")
                elif isinstance(v, bool):
                    lines.append(f"{pad}{k}: {'true' if v else 'false'}")
                elif isinstance(v, (int, float)):
                    lines.append(f"{pad}{k}: {v}")
                else:
                    lines.append(f"{pad}{k}: {json.dumps(str(v), ensure_ascii=False)}")
            return "\n".join(lines)
        elif isinstance(obj, list):
            if not obj:
                return "[]"
            lines = []
            for item in obj:
                if isinstance(item, dict):
                    if not item:
                        lines.append(f"{pad}- {{}}")
                    else:
                        lines.append(f"{pad}-")
                        lines.append(WorkflowSpec._to_yaml_native(item, indent_level + 1))
                elif isinstance(item, list):
                    if not item:
                        lines.append(f"{pad}- []")
                    else:
                        lines.append(f"{pad}-")
                        lines.append(WorkflowSpec._to_yaml_native(item, indent_level + 1))
                elif item is None:
                    lines.append(f"{pad}- null")
                elif isinstance(item, bool):
                    lines.append(f"{pad}- {'true' if item else 'false'}")
                elif isinstance(item, (int, float)):
                    lines.append(f"{pad}- {item}")
                else:
                    lines.append(f"{pad}- {json.dumps(str(item), ensure_ascii=False)}")
            return "\n".join(lines)
        return f"{pad}{obj}"

    @staticmethod
    def _from_yaml_native(text: str) -> Dict[str, Any]:
        """Genuine recursive, indentation-aware YAML parser supporting nested dicts and lists."""
        try:
            return json.loads(text)
        except Exception:
            pass

        def parse_scalar(val: str) -> Any:
            val = val.strip()
            if val in ("", "null", "~"):
                return None
            if val in ("true", "True"):
                return True
            if val in ("false", "False"):
                return False
            if val == "[]":
                return []
            if val == "{}":
                return {}
            if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                try:
                    return json.loads(val)
                except Exception:
                    return val[1:-1]
            if re.match(r"^-?\d+$", val):
                return int(val)
            if re.match(r"^-?\d+\.\d+$", val):
                return float(val)
            if (val.startswith("[") and val.endswith("]")) or (val.startswith("{") and val.endswith("}")):
                try:
                    return json.loads(val)
                except Exception:
                    pass
            return val

        lines: List[Tuple[int, str]] = []
        for raw in text.splitlines():
            line = raw.rstrip()
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            indent = len(line) - len(line.lstrip(" "))
            lines.append((indent, stripped))

        if not lines:
            return {}

        def parse_block(idx: int, min_indent: int) -> Tuple[Any, int]:
            if idx >= len(lines):
                return None, idx
            indent, text = lines[idx]
            if indent < min_indent:
                return None, idx

            # List block
            if text == "-" or text.startswith("- ") or text.startswith("-\t"):
                items = []
                while idx < len(lines):
                    cur_indent, cur_text = lines[idx]
                    if cur_indent < indent:
                        break
                    if cur_indent > indent:
                        break
                    if cur_text == "-":
                        idx += 1
                        if idx < len(lines) and lines[idx][0] > cur_indent:
                            val, idx = parse_block(idx, lines[idx][0])
                            items.append(val)
                        else:
                            items.append(None)
                    elif cur_text.startswith("- ") or cur_text.startswith("-\t"):
                        sub_text = cur_text[2:].strip()
                        is_quoted = (sub_text.startswith('"') and sub_text.endswith('"')) or (
                            sub_text.startswith("'") and sub_text.endswith("'")
                        )
                        is_inline_coll = (sub_text.startswith("{") and sub_text.endswith("}")) or (
                            sub_text.startswith("[") and sub_text.endswith("]")
                        )
                        m = None if (is_quoted or is_inline_coll) else re.match(r"^([a-zA-Z0-9_\-]+):(?:\s+(.*)|$)", sub_text)
                        if m:
                            k = m.group(1).strip()
                            v = m.group(2).strip() if m.group(2) else ""
                            item_dict: Dict[str, Any] = {}
                            if v:
                                item_dict[k] = parse_scalar(v)
                                idx += 1
                            else:
                                idx += 1
                                if idx < len(lines) and lines[idx][0] > cur_indent:
                                    val, idx = parse_block(idx, lines[idx][0])
                                    item_dict[k] = val
                                else:
                                    item_dict[k] = None
                            while idx < len(lines) and lines[idx][0] > cur_indent:
                                sub_ind, sub_line = lines[idx]
                                if ":" in sub_line and not sub_line.startswith("-"):
                                    sk, sv = sub_line.split(":", 1)
                                    sk = sk.strip()
                                    sv = sv.strip()
                                    if sv:
                                        item_dict[sk] = parse_scalar(sv)
                                        idx += 1
                                    else:
                                        idx += 1
                                        if idx < len(lines) and lines[idx][0] > sub_ind:
                                            sval, idx = parse_block(idx, lines[idx][0])
                                            item_dict[sk] = sval
                                        else:
                                            item_dict[sk] = None
                                else:
                                    break
                            items.append(item_dict)
                        else:
                            items.append(parse_scalar(sub_text))
                            idx += 1
                    else:
                        break
                return items, idx

            # Mapping block
            mapping: Dict[str, Any] = {}
            block_indent = indent
            while idx < len(lines):
                cur_indent, cur_text = lines[idx]
                if cur_indent < block_indent:
                    break
                if cur_indent > block_indent:
                    break
                if ":" in cur_text:
                    k, v = cur_text.split(":", 1)
                    k = k.strip()
                    v = v.strip()
                    if v:
                        mapping[k] = parse_scalar(v)
                        idx += 1
                    else:
                        idx += 1
                        if idx < len(lines) and lines[idx][0] > cur_indent:
                            val, idx = parse_block(idx, lines[idx][0])
                            mapping[k] = val if val is not None else {}
                        else:
                            mapping[k] = {}
                else:
                    idx += 1
            return mapping, idx

        res, _ = parse_block(0, 0)
        return res if isinstance(res, dict) else {}


@dataclass
class MatchResult:
    """Natural language retrieval match result."""
    workflow_id: str
    workflow_name: str
    confidence: float
    matched_trigger: str = ""
    spec: Optional[WorkflowSpec] = None
    tier: str = ""
    score_breakdown: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "workflow_name": self.workflow_name,
            "confidence": self.confidence,
            "matched_trigger": self.matched_trigger,
            "tier": self.tier,
            "score_breakdown": self.score_breakdown,
            "spec": self.spec.to_dict() if self.spec else None,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> MatchResult:
        d = dict(data)
        if "spec" in d and isinstance(d["spec"], dict):
            d["spec"] = WorkflowSpec.from_dict(d["spec"])
        return cls(**d)


@dataclass
class ExecutionRecord:
    """Execution telemetry record for runtime audit and debugging."""
    workflow_id: str
    status: str  # "success", "failed", "aborted", "emergency_stop", "running"
    duration_ms: int
    steps_completed: int
    error_message: Optional[str] = None
    timestamp: str = field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat())
    id: Optional[int] = None
    parameters_used: Dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.workflow_id:
            raise ValidationError("workflow_id must not be empty")
        if not isinstance(self.duration_ms, int) or self.duration_ms < 0:
            raise ValidationError("duration_ms must be an integer >= 0")
        if not isinstance(self.steps_completed, int) or self.steps_completed < 0:
            raise ValidationError("steps_completed must be an integer >= 0")

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ExecutionRecord:
        return cls(**data)
