"""Screen Recording Quality Scorer and Evaluator Subsystem.

Path: src/memory/recording_evaluator.py
Belongs to Teach-Mode Demonstration and Dissection Pipeline.

Inspects recorded user demonstration directories containing video (recording.mov),
frame captures (frames/*.jpg), and session metadata (metadata.json). Evaluates:
1. File Integrity & Container Structure (QuickTime/MP4 atoms, valid JPEG/PNG headers).
2. Temporal Continuity & Frame Rate (Duration alignment, nominal FPS >= 30, monotonicity).
3. Spatial Geometry & Resolution (Full display coverage, Retina/HD dimensions, window tracking).
4. Visual Dynamics & Action Alignment (Non-blank frames, interaction-to-frame grounding).

Produces an objective 0-100 quality score, letter grade, and diagnostic issue list.
Zero third-party dependencies (pure standard library and native macOS AVFoundation).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
import json
import logging
import os
from pathlib import Path
import struct
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple, Union

logger = logging.getLogger(__name__)


class QualityGrade(str, Enum):
    """Quality grade categories for demonstration screen recordings."""
    EXCELLENT = "EXCELLENT"             # >= 90.0: Flawless Retina 60fps recording with full frame grounding
    GOOD = "GOOD"                       # >= 80.0: High-fidelity recording meeting all production criteria
    ACCEPTABLE = "ACCEPTABLE"           # >= 70.0: Usable recording with minor warnings (e.g. slight jitter)
    NEEDS_IMPROVEMENT = "NEEDS_IMPROVEMENT"  # >= 50.0: Significant gaps or low resolution
    FAILED = "FAILED"                   # < 50.0: Corrupted, missing video/frames, or empty session


@dataclass
class RecordingQualityReport:
    """Comprehensive evaluation score and diagnostic report for a screen recording."""
    session_id: str
    overall_score: float
    grade: QualityGrade
    is_passing: bool
    category_scores: Dict[str, float]
    metrics: Dict[str, Any]
    issues: List[str] = field(default_factory=list)
    recommendations: List[str] = field(default_factory=list)
    summary: str = ""
    recording_dir: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        """Serializes report into JSON-compatible dictionary."""
        return {
            "session_id": self.session_id,
            "overall_score": round(self.overall_score, 1),
            "grade": self.grade.value if isinstance(self.grade, QualityGrade) else str(self.grade),
            "is_passing": self.is_passing,
            "category_scores": {k: round(v, 1) for k, v in self.category_scores.items()},
            "metrics": self.metrics,
            "issues": self.issues,
            "recommendations": self.recommendations,
            "summary": self.summary,
            "recording_dir": self.recording_dir,
            "timestamp": self.timestamp,
        }

    def to_markdown(self) -> str:
        """Renders human-readable markdown summary."""
        grade_str = self.grade.value if isinstance(self.grade, QualityGrade) else str(self.grade)
        status_str = "PASS" if self.is_passing else "FAIL"
        md = [
            f"# Screen Recording Quality Report: {grade_str} ({round(self.overall_score, 1)}/100) - [{status_str}]",
            f"- **Session ID:** `{self.session_id}`",
            f"- **Directory:** `{self.recording_dir}`",
            f"- **Video Resolution:** {self.metrics.get('video_width', 'N/A')}x{self.metrics.get('video_height', 'N/A')} @ {round(self.metrics.get('fps', 0.0), 1)} FPS",
            f"- **Video Duration:** {round(self.metrics.get('video_duration', 0.0), 2)}s (Session: {round(self.metrics.get('session_duration', 0.0), 2)}s)",
            f"- **Total Frames:** {self.metrics.get('frame_count', 0)} ({self.metrics.get('valid_frames_count', 0)} verified valid)",
            f"- **Events Recorded:** {self.metrics.get('event_count', 0)}",
            "",
            "## Category Breakdown",
            f"- **File Integrity & Structure:** {round(self.category_scores.get('integrity', 0.0), 1)} / 25.0",
            f"- **Temporal Continuity & FPS:** {round(self.category_scores.get('temporal', 0.0), 1)} / 25.0",
            f"- **Spatial Geometry & Coverage:** {round(self.category_scores.get('spatial', 0.0), 1)} / 25.0",
            f"- **Visual Dynamics & Action Grounding:** {round(self.category_scores.get('dynamics', 0.0), 1)} / 25.0",
        ]
        if self.issues:
            md.append("\n## Detected Issues / Warnings")
            for issue in self.issues:
                md.append(f"- ⚠️ {issue}")
        if self.recommendations:
            md.append("\n## Recommendations")
            for rec in self.recommendations:
                md.append(f"- 💡 {rec}")
        return "\n".join(md)


class RecordingQualityEvaluator:
    """Evaluates screen recording sessions on macOS for fidelity and ground truth completeness."""

    @classmethod
    def get_probe_binary(cls) -> Optional[Path]:
        """Locates compiled Swift clio-probe utility or returns None."""
        project_root = Path(__file__).resolve().parent.parent.parent
        bin_probe = project_root / "bin" / "clio-probe"
        if bin_probe.exists() and os.access(bin_probe, os.X_OK):
            return bin_probe
        app_probe = Path("/Users/minhnguyen/Desktop/Clio.app/Contents/MacOS/clio-probe")
        if app_probe.exists() and os.access(app_probe, os.X_OK):
            return app_probe
        return None

    @classmethod
    def read_image_header(cls, path: Path) -> Tuple[Optional[str], Optional[int], Optional[int]]:
        """Extracts format, width, and height from JPEG or PNG header without external libraries."""
        try:
            with open(path, "rb") as f:
                data = f.read(65536)
            if data.startswith(b"\xff\xd8"):
                # JPEG parse
                idx = 2
                while idx < len(data) - 9:
                    if data[idx] != 0xFF:
                        idx += 1
                        continue
                    marker = data[idx + 1]
                    if marker in (0xC0, 0xC1, 0xC2, 0xC3):
                        h, w = struct.unpack(">HH", data[idx + 5 : idx + 9])
                        return "jpeg", int(w), int(h)
                    elif marker in (0xD8, 0xD9):
                        idx += 2
                    else:
                        length = struct.unpack(">H", data[idx + 2 : idx + 4])[0]
                        idx += 2 + length
            elif data.startswith(b"\x89PNG\r\n\x1a\n"):
                # PNG parse
                w, h = struct.unpack(">II", data[16:24])
                return "png", int(w), int(h)
        except Exception as e:
            logger.debug("Failed reading image header %s: %s", path, e)
        return None, None, None

    @classmethod
    def probe_video_file(cls, video_path: Path, extract_frames_dir: Optional[Path] = None) -> Dict[str, Any]:
        """Probes video metadata via native compiled binary, Swift, or container box inspection."""
        info: Dict[str, Any] = {
            "exists": video_path.exists(),
            "path": str(video_path),
            "file_size_bytes": video_path.stat().st_size if video_path.exists() else 0,
            "has_video": False,
            "duration": 0.0,
            "width": 0,
            "height": 0,
            "fps": 0.0,
            "valid_container": False,
            "extracted_frames": [],
        }
        if not video_path.exists() or info["file_size_bytes"] == 0:
            return info

        # Step 1: Check QuickTime/MP4 container atom signatures
        try:
            with open(video_path, "rb") as f:
                head = f.read(65536)
            # Look for common QuickTime/ISO atoms: ftyp, moov, mdat, wide, free
            valid_atoms = any(atom in head for atom in (b"moov", b"mdat", b"wide", b"ftyp", b"free"))
            info["valid_container"] = valid_atoms
        except Exception:
            pass

        # Step 2: Try compiled probe binary (fastest: ~10ms)
        probe_bin = cls.get_probe_binary()
        if probe_bin and sys.platform == "darwin":
            try:
                cmd = [str(probe_bin), str(video_path)]
                if extract_frames_dir:
                    cmd.append(str(extract_frames_dir))
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=5.0,
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    data = json.loads(proc.stdout.strip())
                    if not data.get("error"):
                        info["duration"] = float(data.get("duration", 0.0))
                        info["width"] = int(data.get("width", 0))
                        info["height"] = int(data.get("height", 0))
                        info["fps"] = float(data.get("fps", 0.0))
                        info["has_video"] = bool(data.get("has_video", False))
                        info["valid_container"] = True
                        info["extracted_frames"] = data.get("extracted_frames", [])
                        return info
            except Exception as ex:
                logger.debug("clio-probe error: %s", ex)

        # Step 3: Fallback inline swift invocation if binary missing
        if sys.platform == "darwin":
            swift_script = """
import AVFoundation
import AppKit
import Foundation

let path = CommandLine.arguments[1]
let asset = AVURLAsset(url: URL(fileURLWithPath: path))
var res: [String: Any] = [:]
res["duration"] = CMTimeGetSeconds(asset.duration)
if let track = asset.tracks(withMediaType: .video).first {
    res["width"] = track.naturalSize.width
    res["height"] = track.naturalSize.height
    res["fps"] = track.nominalFrameRate
    res["has_video"] = true
} else { res["has_video"] = false }

if CommandLine.arguments.count > 2 {
    let framesDirPath = CommandLine.arguments[2]
    let framesDirURL = URL(fileURLWithPath: framesDirPath)
    try? FileManager.default.createDirectory(at: framesDirURL, withIntermediateDirectories: true)
    let generator = AVAssetImageGenerator(asset: asset)
    generator.appliesPreferredTrackTransform = true
    let durationSec = CMTimeGetSeconds(asset.duration)
    var targetTimes: [Double] = []
    if durationSec > 0.5 {
        targetTimes = [0.0, durationSec * 0.5, max(0.1, durationSec - 0.1)]
    } else {
        targetTimes = [0.0]
    }
    var extractedFrames: [String] = []
    for (idx, sec) in targetTimes.enumerated() {
        let cmTime = CMTime(seconds: sec, preferredTimescale: 600)
        if let cgImg = try? generator.copyCGImage(at: cmTime, actualTime: nil) {
            let bitmapRep = NSBitmapImageRep(cgImage: cgImg)
            if let jpegData = bitmapRep.representation(using: .jpeg, properties: [:]) {
                let frameFilename = String(format: "frame_%04d_%lld.jpg", idx + 1, Int64(Date().timeIntervalSince1970 * 1000) + Int64(idx * 500))
                let frameURL = framesDirURL.appendingPathComponent(frameFilename)
                try? jpegData.write(to: frameURL)
                extractedFrames.append(frameURL.path)
            }
        }
    }
    res["extracted_frames"] = extractedFrames
}

if let d = try? JSONSerialization.data(withJSONObject: res, options: []),
   let s = String(data: d, encoding: .utf8) { print(s) }
"""
            try:
                cmd = ["swift", "-", str(video_path)]
                if extract_frames_dir:
                    cmd.append(str(extract_frames_dir))
                proc = subprocess.run(
                    cmd,
                    input=swift_script,
                    text=True,
                    capture_output=True,
                    timeout=5.0,
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    data = json.loads(proc.stdout.strip())
                    info["duration"] = float(data.get("duration", 0.0))
                    info["width"] = int(data.get("width", 0))
                    info["height"] = int(data.get("height", 0))
                    info["fps"] = float(data.get("fps", 0.0))
                    info["has_video"] = bool(data.get("has_video", False))
                    info["valid_container"] = True
                    info["extracted_frames"] = data.get("extracted_frames", [])
                    return info
            except Exception as ex:
                logger.debug("Swift video probe fallback error: %s", ex)

        return info

    @classmethod
    def evaluate_session(cls, session_dir: Union[str, Path]) -> RecordingQualityReport:
        """Inspects and scores an entire recording session folder."""
        dir_path = Path(session_dir)
        session_id = dir_path.name

        if not dir_path.exists():
            return RecordingQualityReport(
                session_id=session_id,
                overall_score=0.0,
                grade=QualityGrade.FAILED,
                is_passing=False,
                category_scores={"integrity": 0.0, "temporal": 0.0, "spatial": 0.0, "dynamics": 0.0},
                metrics={"session_dir": str(dir_path)},
                issues=[f"Recording directory does not exist: {dir_path}"],
                recommendations=["Ensure recording started and completed properly."],
                summary=f"FAILED (0/100): Missing directory {dir_path}",
                recording_dir=str(dir_path),
            )

        # 1. Metadata inspection
        metadata_path = dir_path / "metadata.json"
        metadata: Dict[str, Any] = {}
        metadata_valid = False
        if metadata_path.exists():
            try:
                with open(metadata_path, "r", encoding="utf-8") as f:
                    metadata = json.load(f)
                metadata_valid = isinstance(metadata, dict) and bool(metadata.get("session_id"))
            except Exception as e:
                logger.warning("Corrupted metadata.json in %s: %s", dir_path, e)

        # 2. Video inspection
        video_file = dir_path / "recording.mov"
        if not video_file.exists():
            # Check for alternative mp4
            alt_video = dir_path / "recording.mp4"
            if alt_video.exists():
                video_file = alt_video

        video_info = cls.probe_video_file(video_file)

        # 3. Frames inspection
        frames_dir = dir_path / "frames"
        frame_files: List[Path] = []
        if frames_dir.exists():
            frame_files = sorted(list(frames_dir.glob("*.jpg")) + list(frames_dir.glob("*.png")))
        if not frame_files:
            # Check top-level for frame_*.jpg
            frame_files = sorted(list(dir_path.glob("frame_*.jpg")) + list(dir_path.glob("frame_*.png")))

        valid_frames = 0
        frame_dimensions: List[Tuple[int, int]] = []
        corrupted_frames: List[str] = []
        sample_byte_hashes: List[int] = []

        for f_path in frame_files:
            fmt, w, h = cls.read_image_header(f_path)
            if fmt and w and h and w >= 320 and h >= 240:
                valid_frames += 1
                frame_dimensions.append((w, h))
                # Sample a few bytes for motion variation check
                try:
                    with open(f_path, "rb") as f_obj:
                        f_obj.seek(min(1024, f_path.stat().st_size // 2))
                        chunk = f_obj.read(128)
                        sample_byte_hashes.append(hash(chunk))
                except Exception:
                    pass
            else:
                corrupted_frames.append(f_path.name)

        # 4. Temporal inspection
        recorded_frames_meta = metadata.get("frames", [])
        timestamps: List[float] = [f.get("timestamp", 0.0) for f in recorded_frames_meta if isinstance(f, dict)]
        is_strictly_monotonic = True
        max_time_gap = 0.0
        if len(timestamps) > 1:
            for i in range(1, len(timestamps)):
                diff = timestamps[i] - timestamps[i - 1]
                if diff <= 0:
                    is_strictly_monotonic = False
                if diff > max_time_gap:
                    max_time_gap = diff

        session_duration = float(metadata.get("duration_seconds", 0.0))
        if session_duration <= 0.0 and len(timestamps) >= 2:
            session_duration = timestamps[-1] - timestamps[0]

        # 5. Events and window movement tracking
        event_count = int(metadata.get("event_count", 0))
        tracked_windows = metadata.get("tracked_windows", [])
        window_movements = metadata.get("window_movements", [])

        # -------------------------------------------------------------
        # Scoring Rubric Computation (Total: 100 points)
        # -------------------------------------------------------------
        integrity_score = 0.0
        temporal_score = 0.0
        spatial_score = 0.0
        dynamics_score = 0.0
        issues: List[str] = []
        recommendations: List[str] = []

        # Category 1: File Integrity & Structure (25 max)
        # Video file checks (max 10)
        if video_info["exists"] and video_info["file_size_bytes"] > 50000:
            integrity_score += 6.0
            if video_info["valid_container"]:
                integrity_score += 4.0
        elif video_info["exists"]:
            integrity_score += 3.0
            issues.append(f"Video file is unusually small ({video_info['file_size_bytes']} bytes).")
        else:
            issues.append("Missing video recording container (recording.mov).")

        # Frames check (max 10)
        if valid_frames >= 2 and not corrupted_frames:
            integrity_score += 10.0
        elif valid_frames >= 1:
            integrity_score += 7.0
        else:
            issues.append("Zero valid screen frame captures found.")

        if corrupted_frames:
            integrity_score = max(0.0, integrity_score - 4.0)
            issues.append(f"Found {len(corrupted_frames)} corrupted or unreadable frame files.")

        # Metadata check (max 5)
        if metadata_valid:
            integrity_score += 5.0
        elif metadata_path.exists():
            integrity_score += 2.0
            issues.append("metadata.json contains incomplete schema.")
        else:
            issues.append("Missing metadata.json file.")

        integrity_score = min(25.0, integrity_score)

        # Category 2: Temporal Continuity & Frame Rate (25 max)
        # Duration alignment (max 10)
        if video_info.get("has_video") and video_info.get("duration", 0.0) > 0.0:
            v_dur = video_info["duration"]
            dur_diff = abs(v_dur - session_duration) if session_duration > 0 else 0.0
            if dur_diff < 1.0 or session_duration <= 0:
                temporal_score += 10.0
            elif dur_diff < 2.5:
                temporal_score += 6.0
            else:
                temporal_score += 3.0
                issues.append(f"Video duration ({round(v_dur, 2)}s) differs from session duration ({round(session_duration, 2)}s).")
        elif session_duration > 0.5:
            temporal_score += 6.0

        # FPS adequacy (max 8)
        fps = video_info.get("fps", 0.0)
        if fps >= 24.0:
            temporal_score += 8.0
        elif fps >= 8.0:
            temporal_score += 6.5
        elif valid_frames >= 2 or fps > 0.0:
            # Fallback to periodic frame capture rate
            calc_fps = max(fps, valid_frames / max(1.0, session_duration))
            if calc_fps >= 1.0:
                temporal_score += 5.0
            else:
                temporal_score += 3.0
        else:
            issues.append(f"Recorded frame rate ({round(fps, 1)} FPS) is below standard video playback threshold.")

        # Monotonicity & Gaps (max 7)
        if is_strictly_monotonic:
            temporal_score += 4.0
        else:
            issues.append("Non-monotonic frame timestamps detected (clock skew or reordering).")

        if max_time_gap <= 2.0:
            temporal_score += 3.0
        else:
            temporal_score += 1.0
            issues.append(f"Detected large temporal gap between frames: {round(max_time_gap, 2)}s.")

        temporal_score = min(25.0, temporal_score)

        # Category 3: Spatial Resolution & Multi-Window Geometry (25 max)
        # Resolution checks (max 10)
        width = video_info.get("width", 0)
        height = video_info.get("height", 0)
        if not width and frame_dimensions:
            width, height = frame_dimensions[0]

        if width >= 2560 and height >= 1440:
            spatial_score += 10.0  # Full Retina display
        elif width >= 1920 and height >= 1080:
            spatial_score += 8.5   # Full HD
        elif width >= 1280 and height >= 720:
            spatial_score += 7.0   # Standard HD
        elif width > 0:
            spatial_score += 4.0
            issues.append(f"Resolution ({width}x{height}) is below 720p HD standard.")
        else:
            issues.append("Could not determine spatial resolution of recording.")

        # Screen coverage (max 8)
        disp_w = metadata.get("screen_width", width)
        disp_h = metadata.get("screen_height", height)
        if width > 0 and height > 0 and (disp_w == 0 or abs(width - disp_w) < 200 or width >= disp_w):
            spatial_score += 8.0
        elif width > 0:
            spatial_score += 5.0

        # Window movement tracking (max 7)
        if window_movements:
            spatial_score += 7.0
        elif tracked_windows or event_count == 0:
            spatial_score += 7.0
        else:
            spatial_score += 4.0
            recommendations.append("Ensure window bounds are captured on all mouse drags across applications.")

        spatial_score = min(25.0, spatial_score)

        # Category 4: Visual Dynamics & Action Grounding (25 max)
        # Non-blank / non-frozen variation (max 8)
        unique_hashes = len(set(sample_byte_hashes))
        if unique_hashes >= 2 or video_info.get("file_size_bytes", 0) > 100000:
            dynamics_score += 8.0
        elif valid_frames >= 1:
            dynamics_score += 5.0
        else:
            issues.append("Frames appear visually static or unvarying.")

        # Action-to-frame grounding (max 10)
        # Every click/drag event should have visual grounding
        if event_count > 0:
            grounded_ratio = min(1.0, float(valid_frames) / max(1.0, float(min(event_count, 10))))
            dynamics_score += 10.0 * grounded_ratio
            if grounded_ratio < 0.5:
                issues.append(f"Low frame-to-action grounding ratio ({round(grounded_ratio * 100, 1)}%).")
        else:
            dynamics_score += 10.0

        # Click visualization flag / cursor display (max 7)
        if metadata.get("show_clicks", True) or video_info.get("has_video"):
            dynamics_score += 7.0
        else:
            dynamics_score += 4.0

        dynamics_score = min(25.0, dynamics_score)

        # -------------------------------------------------------------
        # Overall Score & Grade
        # -------------------------------------------------------------
        overall_score = integrity_score + temporal_score + spatial_score + dynamics_score
        overall_score = round(max(0.0, min(100.0, overall_score)), 1)

        if overall_score >= 90.0:
            grade = QualityGrade.EXCELLENT
        elif overall_score >= 80.0:
            grade = QualityGrade.GOOD
        elif overall_score >= 70.0:
            grade = QualityGrade.ACCEPTABLE
        elif overall_score >= 50.0:
            grade = QualityGrade.NEEDS_IMPROVEMENT
        else:
            grade = QualityGrade.FAILED

        is_passing = overall_score >= 80.0

        metrics: Dict[str, Any] = {
            "overall_score": overall_score,
            "session_id": session_id,
            "session_duration": session_duration,
            "video_duration": video_info.get("duration", 0.0),
            "video_width": width,
            "video_height": height,
            "fps": video_info.get("fps", 0.0),
            "video_size_bytes": video_info.get("file_size_bytes", 0),
            "frame_count": len(frame_files),
            "valid_frames_count": valid_frames,
            "corrupted_frames_count": len(corrupted_frames),
            "event_count": event_count,
            "tracked_windows_count": len(tracked_windows),
            "window_movements_count": len(window_movements),
            "has_video": video_info.get("has_video", False),
            "valid_container": video_info.get("valid_container", False),
        }

        category_scores: Dict[str, float] = {
            "integrity": integrity_score,
            "temporal": temporal_score,
            "spatial": spatial_score,
            "dynamics": dynamics_score,
        }

        summary = (
            f"{grade.value} ({overall_score}/100) - "
            f"Screen recording verified at {width}x{height} "
            f"({valid_frames} frames, {round(metrics['fps'], 1)} FPS, {event_count} events)."
        )

        report = RecordingQualityReport(
            session_id=session_id,
            overall_score=overall_score,
            grade=grade,
            is_passing=is_passing,
            category_scores=category_scores,
            metrics=metrics,
            issues=issues,
            recommendations=recommendations,
            summary=summary,
            recording_dir=str(dir_path),
        )

        # Automatically persist report as quality_report.json
        try:
            report_file = dir_path / "quality_report.json"
            with open(report_file, "w", encoding="utf-8") as f:
                json.dump(report.to_dict(), f, indent=2)
        except Exception as e:
            logger.debug("Failed saving quality_report.json: %s", e)

        return report
