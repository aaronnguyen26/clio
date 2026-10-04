"""Task Memory Engine subsystem for Autonomous Desktop Companion.

Contains workflow schema models, SQLite persistence with WAL mode,
FTS5 full-text search with Porter stemming, 4-tier hybrid NL retrieval,
parameter interpolation, coordinate projection, and demonstration recording.
Zero third-party dependencies (pure standard library).
"""

from src.memory.coordinates import CoordinateAdapter
from src.memory.engine import TaskMemoryEngine
from src.memory.models import (
    ActionType,
    CoordMode,
    ExecutionRecord,
    MatchResult,
    SerializationError,
    StepErrorHandling,
    StepTiming,
    StepVerification,
    TargetCoordinates,
    ValidationError,
    WorkflowSpec,
    WorkflowStep,
)
from src.memory.parameters import ParameterEngine
from src.memory.recorder import (
    EventCaptureStage,
    EventCoalescingStage,
    NoiseFilterStage,
    PauseClampingStage,
    RawEvent,
    RawEventType,
    RawInputEvent,
    RecorderConfig,
    WindowBounds,
    WorkflowRecorderPipeline,
)
from src.memory.recording_evaluator import (
    QualityGrade,
    RecordingQualityEvaluator,
    RecordingQualityReport,
)
from src.memory.retrieval import NLRetrievalEngine

__all__ = [
    # Models
    "ActionType",
    "CoordMode",
    "ExecutionRecord",
    "MatchResult",
    "SerializationError",
    "StepErrorHandling",
    "StepTiming",
    "StepVerification",
    "TargetCoordinates",
    "ValidationError",
    "WorkflowSpec",
    "WorkflowStep",
    # Core Engines
    "TaskMemoryEngine",
    "NLRetrievalEngine",
    "ParameterEngine",
    "CoordinateAdapter",
    # Recorder Pipeline
    "EventCaptureStage",
    "EventCoalescingStage",
    "NoiseFilterStage",
    "PauseClampingStage",
    "RawEvent",
    "RawEventType",
    "RawInputEvent",
    "RecorderConfig",
    "WindowBounds",
    "WorkflowRecorderPipeline",
    # Screen Recording & Quality Evaluation
    "QualityGrade",
    "RecordingQualityReport",
    "RecordingQualityEvaluator",
]
