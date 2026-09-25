import AppKit
import AVFoundation
import CoreMedia
import Foundation
import ScreenCaptureKit

// MARK: - Clio Native Screen Recorder CLI
//
// Standalone ScreenCaptureKit recorder for full-screen recording.
// Captures all application windows, opening apps, window switches, and desktop context.
//
// Usage: clio-recorder <output_mov_path> [fps]

guard CommandLine.arguments.count > 1 else {
    fputs("Usage: clio-recorder <output_mov_path> [fps]\n", stderr)
    exit(1)
}

let outputPath = CommandLine.arguments[1]
let outputURL = URL(fileURLWithPath: outputPath)

var targetFPS: Int32 = 30
if CommandLine.arguments.count > 2, let fps = Int32(CommandLine.arguments[2]), fps > 0 {
    targetFPS = fps
}

final class StandaloneRecorder: NSObject, @unchecked Sendable, SCStreamOutput, SCStreamDelegate, SCRecordingOutputDelegate {
    private var stream: SCStream?
    private var recordingOutput: AnyObject?
    private var assetWriter: AVAssetWriter?
    private var videoInput: AVAssetWriterInput?
    private var isRecording = false
    private var isStopping = false
    private var sessionStarted = false
    private var lastPTS: CMTime = .invalid
    private let recordingQueue = DispatchQueue(label: "com.clio.recorder.queue", qos: .userInitiated)
    private var onFinished: (() -> Void)?

    /// Polls for screen capture permission with a timeout. Returns true once access is granted.
    private func waitForScreenCapturePermission(timeout: TimeInterval = 60.0) async -> Bool {
        if CGPreflightScreenCaptureAccess() { return true }
        // Request access — opens System Preferences but does not block.
        CGRequestScreenCaptureAccess()
        // Poll every 500ms until the user grants access or we time out.
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            try? await Task.sleep(nanoseconds: 500_000_000)  // 0.5s
            if CGPreflightScreenCaptureAccess() { return true }
        }
        return false
    }

    func start() async throws {
        // Wait for screen capture permission before proceeding.
        // CGRequestScreenCaptureAccess() is fire-and-forget: it opens System Preferences
        // but returns immediately. We must poll until the user actually grants access,
        // otherwise SCShareableContent will throw error -3801 (TCC denied).
        let hasPermission = await waitForScreenCapturePermission(timeout: 60.0)
        guard hasPermission else {
            fputs("Error: Screen Recording permission denied. Open System Settings > Privacy & Security > Screen Recording and enable Clio.\n", stderr)
            exit(1)
        }

        // Remove existing destination file if present
        try? FileManager.default.removeItem(at: outputURL)
        let dir = outputURL.deletingLastPathComponent()
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)

        let shareable = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
        guard let display = shareable.displays.first(where: { $0.displayID == CGMainDisplayID() }) ?? shareable.displays.first else {
            fputs("Error: No display available for recording\n", stderr)
            exit(1)
        }

        // Canonical whole-display capture filter: captures all windows, docks, menus, and opening applications
        let filter = SCContentFilter(display: display, excludingWindows: [])
        if #available(macOS 14.2, *) {
            filter.includeMenuBar = true
        }

        let targetScreen = NSScreen.screens.first(where: {
            ($0.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? CGDirectDisplayID) == display.displayID
        }) ?? NSScreen.main
        let scale = targetScreen?.backingScaleFactor ?? 2.0
        let recWidth = Int(Double(display.width) * scale) & ~1
        let recHeight = Int(Double(display.height) * scale) & ~1

        let config = SCStreamConfiguration()
        config.width = recWidth
        config.height = recHeight
        config.minimumFrameInterval = CMTime(value: 1, timescale: targetFPS)
        config.queueDepth = 8
        config.showsCursor = true
        config.capturesAudio = false
        config.pixelFormat = kCVPixelFormatType_32BGRA

        if #available(macOS 14.0, *) {
            config.ignoreGlobalClipDisplay = true
            config.captureResolution = .best
            config.preservesAspectRatio = true
        }
        if #available(macOS 15.0, *) {
            config.showMouseClicks = true
        }

        let newStream = SCStream(filter: filter, configuration: config, delegate: self)

        if #available(macOS 15.0, *) {
            let recConfig = SCRecordingOutputConfiguration()
            recConfig.outputURL = outputURL
            recConfig.outputFileType = .mov
            recConfig.videoCodecType = .h264
            let recOutput = SCRecordingOutput(configuration: recConfig, delegate: self)
            try newStream.addRecordingOutput(recOutput)
            self.recordingOutput = recOutput
        } else {
            let writer = try AVAssetWriter(outputURL: outputURL, fileType: .mov)
            let videoSettings: [String: Any] = [
                AVVideoCodecKey: AVVideoCodecType.h264,
                AVVideoWidthKey: recWidth,
                AVVideoHeightKey: recHeight,
                AVVideoCompressionPropertiesKey: [
                    AVVideoAverageBitRateKey: 12_000_000,
                    AVVideoProfileLevelKey: AVVideoProfileLevelH264HighAutoLevel,
                    AVVideoExpectedSourceFrameRateKey: targetFPS
                ]
            ]
            let input = AVAssetWriterInput(mediaType: .video, outputSettings: videoSettings)
            input.expectsMediaDataInRealTime = true
            guard writer.canAdd(input) else {
                fputs("Error: AVAssetWriter cannot add input\n", stderr)
                exit(1)
            }
            writer.add(input)
            self.assetWriter = writer
            self.videoInput = input
            writer.startWriting()
            try newStream.addStreamOutput(self, type: .screen, sampleHandlerQueue: self.recordingQueue)
        }

        self.isRecording = true
        try await newStream.startCapture()
        self.stream = newStream

        // macOS <15 AVAssetWriter signal ready immediately after startCapture
        if #unavailable(macOS 15.0) {
            print("RECORDING_STARTED")
            fflush(stdout)
        }
    }

    func stop(completion: @escaping () -> Void) {
        guard isRecording, !isStopping else {
            completion()
            return
        }
        isStopping = true
        isRecording = false
        self.onFinished = completion

        Task {
            if let str = self.stream {
                try? await str.stopCapture()
                self.stream = nil
            }

            if #available(macOS 15.0, *) {
                // SCRecordingOutput delegate recordingOutputDidFinishRecording will fire and finish
                DispatchQueue.global().asyncAfter(deadline: .now() + 2.5) { [weak self] in
                    self?.finishRecording()
                }
            } else {
                self.recordingQueue.async { [weak self] in
                    guard let self = self else { return }
                    self.videoInput?.markAsFinished()
                    if let writer = self.assetWriter, writer.status == .writing {
                        if !self.sessionStarted {
                            writer.startSession(atSourceTime: CMTime.zero)
                        }
                        writer.finishWriting { [weak self] in
                            self?.finishRecording()
                        }
                    } else {
                        self.finishRecording()
                    }
                }
            }
        }
    }

    private func finishRecording() {
        guard let finish = self.onFinished else { return }
        self.onFinished = nil
        finish()
    }

    // MARK: - SCRecordingOutputDelegate (macOS 15+)
    nonisolated func recordingOutputDidStartRecording(_ recordingOutput: SCRecordingOutput) {
        print("RECORDING_STARTED")
        fflush(stdout)
    }

    nonisolated func recordingOutput(_ recordingOutput: SCRecordingOutput, didFailWithError error: Error) {
        fputs("SCRecordingOutput failed: \(error)\n", stderr)
        DispatchQueue.main.async {
            self.finishRecording()
        }
    }

    nonisolated func recordingOutputDidFinishRecording(_ recordingOutput: SCRecordingOutput) {
        DispatchQueue.main.async {
            self.finishRecording()
        }
    }

    // MARK: - SCStreamOutput (macOS <15 fallback)
    nonisolated func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .screen, isRecording else { return }
        guard CMSampleBufferIsValid(sampleBuffer), CMSampleBufferDataIsReady(sampleBuffer) else { return }
        guard CMSampleBufferGetImageBuffer(sampleBuffer) != nil else { return }

        guard let writer = self.assetWriter, let input = self.videoInput else { return }
        let pts = CMSampleBufferGetPresentationTimeStamp(sampleBuffer)
        guard pts.isValid else { return }

        if self.lastPTS.isValid && pts <= self.lastPTS { return }

        if !self.sessionStarted && writer.status == .writing {
            writer.startSession(atSourceTime: pts)
            self.sessionStarted = true
        }

        if writer.status == .writing && input.isReadyForMoreMediaData {
            if input.append(sampleBuffer) {
                self.lastPTS = pts
            }
        }
    }

    // MARK: - SCStreamDelegate
    nonisolated func stream(_ stream: SCStream, didStopWithError error: Error) {
        fputs("SCStream stopped: \(error)\n", stderr)
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)

let recorder = StandaloneRecorder()

// Handle termination signals cleanly
signal(SIGINT, SIG_IGN)
signal(SIGTERM, SIG_IGN)

func setupSignalHandler(sig: Int32) {
    let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    source.setEventHandler {
        recorder.stop {
            print("RECORDING_FINISHED")
            fflush(stdout)
            exit(0)
        }
    }
    source.resume()
}

setupSignalHandler(sig: SIGINT)
setupSignalHandler(sig: SIGTERM)

// Also listen for stdin EOF / newline as a stop trigger
DispatchQueue.global(qos: .userInitiated).async {
    var line: String?
    repeat {
        line = readLine()
    } while line == nil || line!.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty

    DispatchQueue.main.async {
        recorder.stop {
            print("RECORDING_FINISHED")
            fflush(stdout)
            exit(0)
        }
    }
}

Task {
    do {
        try await recorder.start()
    } catch {
        fputs("Failed starting screen recording: \(error)\n", stderr)
        exit(1)
    }
}

app.run()
