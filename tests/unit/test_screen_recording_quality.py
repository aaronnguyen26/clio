"""Tests for Screen Recording Pipeline and Quality Evaluator.

Path: tests/unit/test_screen_recording_quality.py
Validates:
1. Live macOS screen recording when clicking 'Record' (native screencapture -v -k).
2. Video file (recording.mov) persistence, container validity, and Retina resolution.
3. Frame image captures (frames/frame_*.jpg) visual grounding.
4. Window movement tracking across mouse drags and window repositioning.
5. Objective 0-100 quality scoring, letter grading, and diagnostic issue reporting.
6. Server endpoints /api/record/quality and /api/record/frames.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
import socket
import tempfile
import time
import unittest
import urllib.request

from src.actuators.mock import MockActuator
from src.memory.capture import LiveDemonstrationCapture
from src.memory.engine import TaskMemoryEngine
from src.memory.recorder import RawEvent, RawEventType, WindowBounds
from src.memory.recording_evaluator import (
    QualityGrade,
    RecordingQualityEvaluator,
    RecordingQualityReport,
)
from src.server.server import ClioServer


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class TestScreenRecordingAndQuality(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="clio_test_recordings_")
        self.memory = TaskMemoryEngine(db_path=":memory:")

    def tearDown(self) -> None:
        self.memory.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @unittest.skipUnless(sys.platform == "darwin", "Requires macOS native display and screencapture")
    def test_real_screen_recording_and_quality_score(self) -> None:
        """Records the actual macOS screen, saves video and frames, and evaluates quality."""
        capture = LiveDemonstrationCapture(
            memory=self.memory,
            mock=False,
            recordings_dir=self.temp_dir,
        )

        # 1. Start recording
        started = capture.start_recording()
        self.assertTrue(started)
        self.assertTrue(capture.is_recording)

        session_dir = capture.session_dir
        self.assertTrue(session_dir.exists())
        self.assertTrue(session_dir.is_dir())

        # 2. Record real desktop actions for ~1.5 seconds
        time.sleep(0.5)

        # Feed mouse down/up with window bounds
        now = time.time()
        bounds1 = WindowBounds(x=100.0, y=100.0, width=900.0, height=600.0)
        capture.feed_event(
            RawEvent(
                event_type=RawEventType.MOUSE_DOWN,
                timestamp=now,
                x=150.0,
                y=120.0,
                button="left",
                bundle_id="com.apple.Terminal",
                window_bounds=bounds1,
            )
        )
        time.sleep(0.3)
        capture.feed_event(
            RawEvent(
                event_type=RawEventType.MOUSE_UP,
                timestamp=time.time(),
                x=150.0,
                y=120.0,
                button="left",
                bundle_id="com.apple.Terminal",
                window_bounds=bounds1,
            )
        )
        time.sleep(0.6)

        # 3. Stop recording and dissect
        spec = capture.dissect_and_save(
            name="Test Screen Capture Task",
            canonical_trigger="test screen capture",
        )
        self.assertFalse(capture.is_recording)

        # 4. Verify directory contents
        video_file = session_dir / "recording.mov"
        metadata_file = session_dir / "metadata.json"
        quality_file = session_dir / "quality_report.json"
        frames_dir = session_dir / "frames"

        self.assertTrue(video_file.exists(), f"Video file {video_file} was not created.")
        self.assertGreater(video_file.stat().st_size, 50000, "Video file size is suspiciously small.")
        self.assertTrue(metadata_file.exists(), "metadata.json was not created.")
        self.assertTrue(quality_file.exists(), "quality_report.json was not created.")

        frames = list(frames_dir.glob("*.jpg"))
        self.assertGreaterEqual(len(frames), 1, "At least 1 screen frame should be captured.")

        # 5. Evaluate Quality Report
        report = RecordingQualityEvaluator.evaluate_session(session_dir)
        self.assertIsInstance(report, RecordingQualityReport)
        self.assertTrue(report.is_passing, f"Recording quality failed ({report.overall_score}/100): {report.issues}")
        self.assertGreaterEqual(report.overall_score, 80.0)
        self.assertIn(report.grade, (QualityGrade.GOOD, QualityGrade.EXCELLENT))

        # Check metrics
        self.assertTrue(report.metrics.get("has_video"))
        self.assertTrue(report.metrics.get("valid_container"))
        self.assertGreater(report.metrics.get("video_width", 0), 1000)
        self.assertGreater(report.metrics.get("video_height", 0), 700)
        self.assertGreater(report.metrics.get("fps", 0.0), 5.0)

        # Environment on WorkflowSpec should contain report
        self.assertIn("recording_quality", spec.environment)
        self.assertGreaterEqual(spec.environment["recording_score"], 80.0)

    def test_window_movement_tracking_during_recording(self) -> None:
        """Simulates window movement drag and verifies bounds tracking in metadata and scoring."""
        capture = LiveDemonstrationCapture(
            memory=self.memory,
            mock=True,
            recordings_dir=self.temp_dir,
        )
        capture.start_recording()
        session_dir = capture.session_dir

        t0 = time.time()
        init_bounds = WindowBounds(x=100.0, y=100.0, width=800.0, height=600.0)
        moved_bounds = WindowBounds(x=350.0, y=280.0, width=800.0, height=600.0)

        # Interaction 1: Click at initial window position
        capture.feed_event(
            RawEvent(
                event_type=RawEventType.MOUSE_DOWN,
                timestamp=t0,
                x=120.0,
                y=110.0,
                bundle_id="com.apple.finder",
                window_bounds=init_bounds,
            )
        )

        # Interaction 2: User drags window to new location
        capture.feed_event(
            RawEvent(
                event_type=RawEventType.MOUSE_DRAG,
                timestamp=t0 + 0.1,
                x=370.0,
                y=290.0,
                bundle_id="com.apple.finder",
                window_bounds=moved_bounds,
            )
        )

        # Interaction 3: Click inside the moved window
        capture.feed_event(
            RawEvent(
                event_type=RawEventType.MOUSE_DOWN,
                timestamp=t0 + 0.2,
                x=400.0,
                y=320.0,
                bundle_id="com.apple.finder",
                window_bounds=moved_bounds,
            )
        )

        capture.stop_recording()

        # Verify metadata records window movements
        meta_file = session_dir / "metadata.json"
        self.assertTrue(meta_file.exists())
        with open(meta_file, "r") as f:
            meta = json.load(f)

        self.assertIn("window_movements", meta)
        movements = meta["window_movements"]
        self.assertGreaterEqual(len(movements), 1)
        self.assertEqual(movements[0]["bundle_id"], "com.apple.finder")
        self.assertAlmostEqual(movements[0]["delta_x"], 250.0, delta=1.0)
        self.assertAlmostEqual(movements[0]["delta_y"], 180.0, delta=1.0)

        # Evaluate quality
        report = RecordingQualityEvaluator.evaluate_session(session_dir)
        self.assertIn("spatial", report.category_scores)
        self.assertGreaterEqual(report.category_scores["spatial"], 15.0)

    def test_recording_quality_evaluator_defect_detection(self) -> None:
        """Tests that corrupted video and missing metadata properly downgrade quality score."""
        bad_dir = Path(self.temp_dir) / "corrupted_session"
        bad_dir.mkdir(parents=True, exist_ok=True)

        # Create corrupted video (random invalid bytes, not QuickTime/MP4 container)
        corrupted_video = bad_dir / "recording.mov"
        corrupted_video.write_bytes(b"CORRUPTED_NOT_A_VIDEO_FILE_AT_ALL_JUST_RANDOM_GARBAGE")

        # Create empty metadata
        meta_file = bad_dir / "metadata.json"
        meta_file.write_text("{}", encoding="utf-8")

        report = RecordingQualityEvaluator.evaluate_session(bad_dir)
        self.assertFalse(report.is_passing)
        self.assertLess(report.overall_score, 70.0)
        self.assertIn(report.grade, (QualityGrade.FAILED, QualityGrade.NEEDS_IMPROVEMENT))
        self.assertTrue(any("corrupt" in issue.lower() or "container" in issue.lower() or "missing" in issue.lower() or "zero" in issue.lower() for issue in report.issues))

    def test_server_record_quality_and_frames_endpoints(self) -> None:
        """Validates that the Clio server exposes /api/record/quality and /api/record/frames."""
        port = find_free_port()
        server = ClioServer(
            host="127.0.0.1",
            port=port,
            memory=self.memory,
            actuator=MockActuator(),
            zero_delay=True,
        )
        server.demonstration_capture._mock = True
        server.start()
        time.sleep(0.05)
        base_url = f"http://127.0.0.1:{port}"

        try:
            # 1. Start recording via POST /api/record/start
            req = urllib.request.Request(f"{base_url}/api/record/start", data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req) as resp:
                self.assertEqual(resp.status, 200)
                res = json.loads(resp.read().decode())
                self.assertTrue(res.get("success"))

            # 2. Check status via GET /api/record/status
            with urllib.request.urlopen(f"{base_url}/api/record/status") as resp:
                self.assertEqual(resp.status, 200)
                st = json.loads(resp.read().decode())
                self.assertTrue(st.get("is_recording"))

            # 3. Feed an event via POST /api/record/feed
            feed_data = json.dumps({
                "event_type": "click",
                "x": 200.0,
                "y": 150.0,
                "button": "left",
                "bundle_id": "com.apple.calculator",
                "window_bounds": {"x": 100, "y": 100, "width": 400, "height": 500},
            }).encode()
            req = urllib.request.Request(f"{base_url}/api/record/feed", data=feed_data, headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req) as resp:
                self.assertEqual(resp.status, 200)

            # 4. Check frames endpoint GET /api/record/frames
            with urllib.request.urlopen(f"{base_url}/api/record/frames") as resp:
                self.assertEqual(resp.status, 200)
                frames_res = json.loads(resp.read().decode())
                self.assertIn("frames", frames_res)

            # 5. Stop recording via POST /api/record/stop
            stop_data = json.dumps({"name": "Test Server Task", "trigger": "test server"}).encode()
            req = urllib.request.Request(f"{base_url}/api/record/stop", data=stop_data, headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req) as resp:
                self.assertEqual(resp.status, 200)
                stop_res = json.loads(resp.read().decode())
                self.assertTrue(stop_res.get("success"))
                self.assertIn("recording_score", stop_res)
                self.assertIn("recording_grade", stop_res)
                self.assertIn("recording_dir", stop_res)

            # 6. Query quality report via GET /api/record/quality
            with urllib.request.urlopen(f"{base_url}/api/record/quality") as resp:
                self.assertEqual(resp.status, 200)
                q_res = json.loads(resp.read().decode())
                self.assertIn("overall_score", q_res)
                self.assertIn("category_scores", q_res)
                self.assertIn("grade", q_res)

        finally:
            server.stop()


if __name__ == "__main__":
    unittest.main()
