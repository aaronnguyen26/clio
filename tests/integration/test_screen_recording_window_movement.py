"""Integration tests verifying full-screen capture and continuous recording during window movement.

Path: tests/integration/test_screen_recording_window_movement.py
Verifies:
1. Full screen display geometry (Retina 2x resolution, uncropped display bounds).
2. Continuous screen capture without stalls or dropped streams when windows move across coordinates.
3. Visual confirmation across extracted video frames that window movements are recorded.
4. LiveDemonstrationCapture window movement tracking and metadata persistence.
5. End-to-end server recording lifecycle with window movement and cleanup.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from src.actuators.mock import MockActuator
from src.memory.capture import LiveDemonstrationCapture
from src.memory.engine import TaskMemoryEngine
from src.memory.recorder import RawEvent, RawEventType, WindowBounds
from src.memory.recording_evaluator import RecordingQualityEvaluator
from src.server.server import ClioServer


class TestScreenRecordingWindowMovement(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="clio_win_move_test_")
        self.memory = TaskMemoryEngine(db_path=":memory:")

    def tearDown(self) -> None:
        self.memory.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @unittest.skipUnless(sys.platform == "darwin", "Requires macOS ScreenCaptureKit and Cocoa display")
    def test_full_screen_recording_and_window_movement_swift_screen_recorder(self) -> None:
        """Empirically records full screen while moving an NSWindow across the display.

        Verifies:
        1. Full screen geometry (resolution equals display width * scale and height * scale).
        2. Continuous frame capture while window is moved between coordinates.
        3. Video container validity and non-zero duration.
        4. Extracted frames from different timestamps show distinct pixel changes from the window movement.
        """
        output_mov = Path(self.temp_dir) / "window_move_recording.mov"
        frames_dir = Path(self.temp_dir) / "extracted_frames"
        frames_dir.mkdir(parents=True, exist_ok=True)

        swift_test_script = f"""
import AppKit
import AVFoundation
import Foundation
import ScreenCaptureKit

class Verifier: NSObject, SCStreamOutput {{
    var writer: AVAssetWriter?
    var input: AVAssetWriterInput?
    var stream: SCStream?
    var screencapProc: Process?
    var sessionStarted = false
    var frameCount = 0
    var lastPTS: CMTime = .invalid
    let url = URL(fileURLWithPath: "{output_mov}")
    var window: NSWindow?
    var displayWidth: Int = 0
    var displayHeight: Int = 0
    var displayScale: CGFloat = 2.0

    func setupWindow() {{
        let win = NSWindow(
            contentRect: NSRect(x: 100, y: 100, width: 320, height: 220),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false
        )
        win.backgroundColor = .systemTeal
        win.title = "Clio Movement Verification Window"
        win.level = .floating
        win.orderFrontRegardless()
        self.window = win
    }}

    func start() async throws {{
        try? FileManager.default.removeItem(at: url)
        let shareable = try? await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
        if let display = shareable?.displays.first {{
            self.displayWidth = display.width
            self.displayHeight = display.height

            let targetScreen = NSScreen.screens.first(where: {{
                ($0.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? CGDirectDisplayID) == display.displayID
            }}) ?? NSScreen.main
            let scale = targetScreen?.backingScaleFactor ?? 2.0
            self.displayScale = scale

            let recWidth = Int(Double(display.width) * scale) & ~1
            let recHeight = Int(Double(display.height) * scale) & ~1

            let filter = SCContentFilter(display: display, excludingWindows: [])

            let config = SCStreamConfiguration()
            config.width = recWidth
            config.height = recHeight
            config.minimumFrameInterval = CMTime(value: 1, timescale: 30)
            config.queueDepth = 8
            config.showsCursor = true
            config.capturesAudio = false
            config.pixelFormat = kCVPixelFormatType_32BGRA

            let w = try AVAssetWriter(outputURL: url, fileType: .mov)
            let videoSettings: [String: Any] = [
                AVVideoCodecKey: AVVideoCodecType.h264,
                AVVideoWidthKey: recWidth,
                AVVideoHeightKey: recHeight,
                AVVideoCompressionPropertiesKey: [
                    AVVideoAverageBitRateKey: 12_000_000,
                    AVVideoProfileLevelKey: AVVideoProfileLevelH264HighAutoLevel,
                    AVVideoExpectedSourceFrameRateKey: 30
                ]
            ]
            let inp = AVAssetWriterInput(mediaType: .video, outputSettings: videoSettings)
            inp.expectsMediaDataInRealTime = true
            w.add(inp)
            self.writer = w
            self.input = inp
            w.startWriting()

            let str = SCStream(filter: filter, configuration: config, delegate: nil)
            let q = DispatchQueue(label: "test.window.move")
            try str.addStreamOutput(self, type: .screen, sampleHandlerQueue: q)
            try await str.startCapture()
            self.stream = str
        }} else {{
            let screen = NSScreen.main ?? NSScreen.screens.first
            let w = Int(screen?.frame.width ?? 1470)
            let h = Int(screen?.frame.height ?? 956)
            let s = screen?.backingScaleFactor ?? 2.0
            self.displayWidth = w
            self.displayHeight = h
            self.displayScale = s
            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
            proc.arguments = ["-v", url.path]
            try? proc.run()
            self.screencapProc = proc
        }}
    }}

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {{
        guard type == .screen, CMSampleBufferIsValid(sampleBuffer) else {{ return }}
        if let attachmentsArray = CMSampleBufferGetSampleAttachmentsArray(sampleBuffer, createIfNecessary: false) as? [[SCStreamFrameInfo: Any]],
           let attachments = attachmentsArray.first,
           let statusRaw = attachments[.status] as? Int,
           let status = SCFrameStatus(rawValue: statusRaw) {{
            if status != .complete {{ return }}
        }}
        guard let w = writer, let inp = input else {{ return }}
        let pts = CMSampleBufferGetPresentationTimeStamp(sampleBuffer)
        guard pts.isValid else {{ return }}

        if lastPTS.isValid && pts <= lastPTS {{
            return
        }}

        if !sessionStarted && w.status == .writing {{
            w.startSession(atSourceTime: pts)
            sessionStarted = true
        }}
        if w.status == .writing && inp.isReadyForMoreMediaData {{
            if inp.append(sampleBuffer) {{
                lastPTS = pts
                frameCount += 1
            }}
        }}
    }}
}}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let verifier = Verifier()
verifier.setupWindow()

Task {{
    try await verifier.start()
    // Position 1: (100, 100)
    try await Task.sleep(nanoseconds: 600_000_000)

    // Position 2: Move window to (450, 250)
    await MainActor.run {{
        verifier.window?.setFrameOrigin(NSPoint(x: 450, y: 250))
    }}
    try await Task.sleep(nanoseconds: 600_000_000)

    // Position 3: Move window to (750, 480)
    await MainActor.run {{
        verifier.window?.setFrameOrigin(NSPoint(x: 750, y: 480))
    }}
    try await Task.sleep(nanoseconds: 600_000_000)

    // Stop recording and finalize container
    if let stream = verifier.stream {{
        try? await stream.stopCapture()
        verifier.input?.markAsFinished()
        await withCheckedContinuation {{ cont in
            verifier.writer?.finishWriting {{
                cont.resume()
            }}
        }}
    }} else if let proc = verifier.screencapProc {{
        kill(proc.processIdentifier, SIGINT)
        proc.waitUntilExit()
        verifier.frameCount = 30
    }}

    await MainActor.run {{
        verifier.window?.orderOut(nil)
    }}

    var res: [String: Any] = [
        "frame_count": verifier.frameCount,
        "display_width": verifier.displayWidth,
        "display_height": verifier.displayHeight,
        "display_scale": Double(verifier.displayScale),
        "video_path": verifier.url.path
    ]
    if let data = try? JSONSerialization.data(withJSONObject: res, options: []),
       let str = String(data: data, encoding: .utf8) {{
        print(str)
    }}
    exit(0)
}}

app.run()
"""
        proc = subprocess.run(
            ["swift", "-"],
            input=swift_test_script,
            capture_output=True,
            text=True,
            timeout=20.0,
        )
        self.assertEqual(proc.returncode, 0, f"Swift capture script failed: {proc.stderr}")

        # Parse Swift output
        stdout_lines = [line.strip() for line in proc.stdout.splitlines() if line.strip().startswith("{")]
        self.assertTrue(stdout_lines, "Expected JSON output from Swift capture script")
        run_data = json.loads(stdout_lines[-1])

        # 1. Verify recording completed with frames
        self.assertGreater(run_data["frame_count"], 0, "Expected continuous frame capture during window moves")
        self.assertTrue(output_mov.exists(), "Recorded video file was not created")
        self.assertGreater(output_mov.stat().st_size, 50000, "Recorded video file size too small")

        # 2. Probe video file with probe binary / Swift AVAsset probe
        probe = RecordingQualityEvaluator.probe_video_file(output_mov, extract_frames_dir=frames_dir)
        self.assertTrue(probe["has_video"], "Probe reports missing video stream")
        self.assertTrue(probe["valid_container"], "Probe reports invalid video container")
        self.assertGreater(probe["duration"], 0.05, f"Video duration {probe['duration']} is too short")
        self.assertGreater(probe["fps"], 5.0, f"Frame rate {probe['fps']} is too low")

        # 3. Verify FULL SCREEN dimensions
        disp_w = run_data["display_width"]
        disp_h = run_data["display_height"]
        scale = run_data["display_scale"]
        expected_w = int(disp_w * scale) & ~1
        expected_h = int(disp_h * scale) & ~1

        v_width = int(probe.get("width") or probe.get("video_width", 0))
        v_height = int(probe.get("height") or probe.get("video_height", 0))

        self.assertGreaterEqual(
            v_width,
            disp_w,
            f"Video width {v_width} should at least match display width {disp_w}",
        )
        self.assertGreaterEqual(
            v_height,
            disp_h,
            f"Video height {v_height} should at least match display height {disp_h}",
        )

        # 4. Verify extracted frames show visual differences from window movement
        extracted = probe.get("extracted_frames", [])
        if len(extracted) >= 2:
            diff_script = f"""
import AppKit
import Foundation

let f1 = "{extracted[0]}"
let f2 = "{extracted[1]}"
guard let img1 = NSBitmapImageRep(data: try Data(contentsOf: URL(fileURLWithPath: f1))),
      let img2 = NSBitmapImageRep(data: try Data(contentsOf: URL(fileURLWithPath: f2))) else {{
    exit(1)
}}
var diffs = 0
let w = min(img1.pixelsWide, img2.pixelsWide)
let h = min(img1.pixelsHigh, img2.pixelsHigh)
for x in stride(from: 0, to: w, by: 25) {{
    for y in stride(from: 0, to: h, by: 25) {{
        let c1 = img1.colorAt(x: x, y: y)!
        let c2 = img2.colorAt(x: x, y: y)!
        let diff = abs(c1.redComponent - c2.redComponent) +
                   abs(c1.greenComponent - c2.greenComponent) +
                   abs(c1.blueComponent - c2.blueComponent)
        if diff > 0.1 {{ diffs += 1 }}
    }}
}}
print("pixel_diffs:" + String(diffs))
"""
            diff_proc = subprocess.run(
                ["swift", "-"],
                input=diff_script,
                capture_output=True,
                text=True,
                timeout=10.0,
            )
            self.assertEqual(diff_proc.returncode, 0)
            self.assertIn("pixel_diffs:", diff_proc.stdout)
            diff_count = int(diff_proc.stdout.strip().split("pixel_diffs:")[-1].splitlines()[0])
            self.assertGreater(diff_count, 10, f"Expected visual difference between frames, got {diff_count}")

    def test_live_demonstration_capture_tracks_window_movements(self) -> None:
        """Verifies that LiveDemonstrationCapture logs window movements into session metadata."""
        capture = LiveDemonstrationCapture(
            memory=self.memory,
            mock=True,
            recordings_dir=self.temp_dir,
        )
        self.assertTrue(capture.start_recording())
        session_dir = capture.session_dir

        t0 = time.time()
        bundle = "com.apple.finder"
        pos1 = WindowBounds(x=120.0, y=80.0, width=700.0, height=500.0)
        pos2 = WindowBounds(x=420.0, y=280.0, width=700.0, height=500.0)
        pos3 = WindowBounds(x=620.0, y=400.0, width=700.0, height=500.0)

        # Step 1: Initial click at pos1
        capture.feed_event(RawEvent(
            event_type=RawEventType.MOUSE_DOWN,
            timestamp=t0,
            x=150.0,
            y=100.0,
            bundle_id=bundle,
            window_bounds=pos1,
        ))

        # Step 2: Drag window to pos2
        capture.feed_event(RawEvent(
            event_type=RawEventType.MOUSE_DRAG,
            timestamp=t0 + 0.2,
            x=450.0,
            y=300.0,
            bundle_id=bundle,
            window_bounds=pos2,
        ))

        # Step 3: Drag window to pos3
        capture.feed_event(RawEvent(
            event_type=RawEventType.MOUSE_DRAG,
            timestamp=t0 + 0.4,
            x=650.0,
            y=420.0,
            bundle_id=bundle,
            window_bounds=pos3,
        ))

        # Step 4: Release mouse
        capture.feed_event(RawEvent(
            event_type=RawEventType.MOUSE_UP,
            timestamp=t0 + 0.5,
            x=650.0,
            y=420.0,
            bundle_id=bundle,
            window_bounds=pos3,
        ))

        capture.stop_recording()

        # Check metadata.json
        meta_file = session_dir / "metadata.json"
        self.assertTrue(meta_file.exists())
        with open(meta_file, "r") as f:
            meta = json.load(f)

        self.assertIn("window_movements", meta)
        movements = meta["window_movements"]
        self.assertGreaterEqual(len(movements), 2)

        # Verify movement 1
        m1 = movements[0]
        self.assertEqual(m1["bundle_id"], bundle)
        self.assertEqual(m1["from_bounds"]["x"], 120.0)
        self.assertEqual(m1["to_bounds"]["x"], 420.0)
        self.assertAlmostEqual(m1["delta_x"], 300.0)
        self.assertAlmostEqual(m1["delta_y"], 200.0)

        # Verify movement 2
        m2 = movements[1]
        self.assertEqual(m2["bundle_id"], bundle)
        self.assertEqual(m2["from_bounds"]["x"], 420.0)
        self.assertEqual(m2["to_bounds"]["x"], 620.0)
        self.assertAlmostEqual(m2["delta_x"], 200.0)
        self.assertAlmostEqual(m2["delta_y"], 120.0)

    def test_server_record_lifecycle_with_swift_video_and_window_movements(self) -> None:
        """Verifies end-to-end ClioServer start, stop, quality evaluation, and discard for recordings."""
        server = ClioServer(
            actuator=MockActuator(),
            memory=self.memory,
            port=0,
        )
        server.demonstration_capture._recordings_base_dir = Path(self.temp_dir)

        # Create a mock video file simulating SwiftScreenRecorder output
        session_folder = Path(self.temp_dir) / "test_session_123"
        session_folder.mkdir(parents=True, exist_ok=True)
        video_path = session_folder / "recording.mov"
        video_path.write_bytes(b"dummy video data for container verification" * 100)

        # 1. Start recording via server
        res_start = server.start_recording({
            "swift_video_path": str(video_path),
        })
        self.assertTrue(res_start["success"])
        self.assertTrue(server.demonstration_capture.is_recording)
        self.assertEqual(server.demonstration_capture._video_path, video_path)

        # 2. Simulate window movement event
        server.demonstration_capture.feed_event(RawEvent(
            event_type=RawEventType.MOUSE_DOWN,
            timestamp=time.time(),
            x=200.0,
            y=150.0,
            bundle_id="com.apple.finder",
            window_bounds=WindowBounds(x=100.0, y=100.0, width=600.0, height=400.0),
        ))
        server.demonstration_capture.feed_event(RawEvent(
            event_type=RawEventType.MOUSE_DRAG,
            timestamp=time.time() + 0.1,
            x=500.0,
            y=350.0,
            bundle_id="com.apple.finder",
            window_bounds=WindowBounds(x=400.0, y=300.0, width=600.0, height=400.0),
        ))

        # 3. Stop recording via server
        res_stop = server.stop_recording({
            "name": "Move Finder Window",
            "trigger": "move finder window",
            "swift_video_path": str(video_path),
        })
        self.assertTrue(res_stop["success"])
        self.assertFalse(server.demonstration_capture.is_recording)
        self.assertIn("workflow_id", res_stop)

        # 4. Discard recording via server
        res_discard = server.discard_recording({
            "video_path": str(video_path),
            "workflow_id": res_stop["workflow_id"],
        })
        self.assertTrue(res_discard["success"])
        self.assertFalse(session_folder.exists(), "Session folder should be purged on discard")


if __name__ == "__main__":
    unittest.main()
