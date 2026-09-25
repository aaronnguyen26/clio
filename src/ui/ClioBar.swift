import AppKit
import ApplicationServices
import AVFoundation
import AVKit
import Carbon
import Combine
import Foundation
import Speech
import SwiftUI

// MARK: - Design System Tokens (Stitch MCP: Obsidian Glass HUD — Single Color Scheme)

enum ObsidianTheme {
    // Canvas & Glass Surfaces
    static let bgGlass = Color(red: 0.05, green: 0.055, blue: 0.065).opacity(0.88)
    static let surface = Color(red: 0.09, green: 0.095, blue: 0.11)
    static let surfaceElevated = Color(red: 0.12, green: 0.125, blue: 0.14)
    static let surfaceHover = Color(red: 0.16, green: 0.165, blue: 0.185)
    static let cardGlass = Color(red: 0.07, green: 0.075, blue: 0.085).opacity(0.92)

    // Single Color Palette: Platinum White / Monochrome Slate Spectrum
    static let platinum = Color(red: 0.96, green: 0.965, blue: 0.975)     // #F5F6F8 Primary text & active accent
    static let platinumDim = Color(red: 0.88, green: 0.89, blue: 0.91)    // Secondary bright text
    static let slate = Color(red: 0.54, green: 0.56, blue: 0.60)          // #8A909A Muted labels & secondary context
    static let slateDark = Color(red: 0.38, green: 0.40, blue: 0.44)      // Inactive glyphs
    static let zinc = Color(red: 0.22, green: 0.23, blue: 0.25)           // Dividers & tracks

    // Borders & Hairline Highlights
    static let borderSubtle = Color(red: 0.96, green: 0.965, blue: 0.975).opacity(0.09)
    static let borderMedium = Color(red: 0.96, green: 0.965, blue: 0.975).opacity(0.18)
    static let borderActive = Color(red: 0.96, green: 0.965, blue: 0.975).opacity(0.45)
}

// MARK: - Server Launcher & Auto-Healing Backend Manager

final class ServerLauncher {
    static let shared = ServerLauncher()
    private var spawnedProcess: Process?

    func isServerReachable() async -> Bool {
        guard let url = URL(string: "http://127.0.0.1:8765/api/status") else { return false }
        var request = URLRequest(url: url)
        request.timeoutInterval = 1.2
        do {
            let (_, response) = try await URLSession.shared.data(for: request)
            if let http = response as? HTTPURLResponse, (200...299).contains(http.statusCode) {
                return true
            }
        } catch {
            return false
        }
        return false
    }

    func resolveProjectDirectory() -> String {
        let fm = FileManager.default

        // 1. Environment override
        if let env = ProcessInfo.processInfo.environment["CLIO_PROJECT_DIR"], fm.fileExists(atPath: env) {
            return env
        }

        // 2. Candidate paths based on binary / app location
        let candidates = [
            Bundle.main.resourceURL?.path ?? "",
            Bundle.main.bundleURL.appendingPathComponent("Contents/Resources").path,
            "/Users/minhnguyen/Desktop/Coding/imitate",
            Bundle.main.bundleURL.deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().path,
            Bundle.main.bundleURL.deletingLastPathComponent().path,
            Bundle.main.bundleURL.deletingLastPathComponent().deletingLastPathComponent().path,
            fm.currentDirectoryPath
        ]

        for path in candidates {
            if path.isEmpty { continue }
            let marker = (path as NSString).appendingPathComponent("src/main.py")
            if fm.fileExists(atPath: marker) {
                return path
            }
        }
        return "/Users/minhnguyen/Desktop/Coding/imitate"
    }

    func resolvePythonExecutable() -> String {
        let fm = FileManager.default
        let pythonCandidates = [
            "/opt/homebrew/bin/python3",
            "/opt/homebrew/bin/python3.14",
            "/opt/homebrew/bin/python3.13",
            "/opt/homebrew/bin/python3.12",
            "/usr/local/bin/python3",
            "/usr/bin/python3"
        ]
        for p in pythonCandidates {
            if fm.fileExists(atPath: p) {
                return p
            }
        }
        return "/usr/bin/env"
    }

    func ensureServerRunning() async {
        if await isServerReachable() {
            return
        }

        let projectDir = resolveProjectDirectory()
        let pyExe = resolvePythonExecutable()

        let proc = Process()
        if pyExe == "/usr/bin/env" {
            proc.executableURL = URL(fileURLWithPath: "/usr/bin/env")
            proc.arguments = ["python3", "-m", "src.main", "--server", "--port", "8765"]
        } else {
            proc.executableURL = URL(fileURLWithPath: pyExe)
            proc.arguments = ["-m", "src.main", "--server", "--port", "8765"]
        }

        proc.currentDirectoryURL = URL(fileURLWithPath: projectDir)

        var env = ProcessInfo.processInfo.environment
        env["PATH"] = "/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        env["PYTHONPATH"] = projectDir
        env["PYTHONUNBUFFERED"] = "1"
        proc.environment = env

        let logPath = "/tmp/clio_server.log"
        FileManager.default.createFile(atPath: logPath, contents: nil)
        if let fileHandle = FileHandle(forWritingAtPath: logPath) {
            proc.standardOutput = fileHandle
            proc.standardError = fileHandle
        }

        do {
            try proc.run()
            self.spawnedProcess = proc
            print("[ServerLauncher] Clio Core Engine spawned with PID: \(proc.processIdentifier) at \(projectDir)")
        } catch {
            print("[ServerLauncher] Failed to launch Clio Core Engine: \(error)")
        }

        // Wait up to 10 seconds for server to answer
        for _ in 0..<20 {
            try? await Task.sleep(nanoseconds: 500_000_000)
            if await isServerReachable() {
                print("[ServerLauncher] Clio Core Engine is now reachable on port 8765.")
                break
            }
        }
    }

    func terminate() {
        spawnedProcess?.terminate()
    }
}

// MARK: - Voice Dictation & Speech Recognition Manager

@MainActor
final class SpeechDictationManager: ObservableObject {
    @Published var isListening: Bool = false
    @Published var recognizedText: String = ""

    private var audioEngine: AVAudioEngine?
    private var recognitionRequest: SFSpeechAudioBufferRecognitionRequest?
    private var recognitionTask: SFSpeechRecognitionTask?
    private let speechRecognizer = SFSpeechRecognizer(locale: Locale(identifier: "en-US"))

    func toggleDictation(onRecognized: @escaping (String) -> Void, onFinished: @escaping (String) -> Void) {
        if isListening {
            stopListening(onFinished: onFinished)
        } else {
            startListening(onRecognized: onRecognized, onFinished: onFinished)
        }
    }

    func startListening(onRecognized: @escaping (String) -> Void, onFinished: @escaping (String) -> Void) {
        SFSpeechRecognizer.requestAuthorization { [weak self] authStatus in
            Task { @MainActor in
                guard let self = self else { return }
                guard authStatus == .authorized else { return }
                do {
                    try self.startRecordingSession(onRecognized: onRecognized, onFinished: onFinished)
                } catch {
                    self.stopListening(onFinished: onFinished)
                }
            }
        }
    }

    private func startRecordingSession(onRecognized: @escaping (String) -> Void, onFinished: @escaping (String) -> Void) throws {
        recognitionTask?.cancel()
        recognitionTask = nil

        let engine = AVAudioEngine()
        self.audioEngine = engine

        let request = SFSpeechAudioBufferRecognitionRequest()
        request.shouldReportPartialResults = true
        self.recognitionRequest = request

        guard let recognizer = speechRecognizer, recognizer.isAvailable else {
            return
        }

        let inputNode = engine.inputNode
        let recordingFormat = inputNode.outputFormat(forBus: 0)
        inputNode.installTap(onBus: 0, bufferSize: 1024, format: recordingFormat) { buffer, _ in
            request.append(buffer)
        }

        engine.prepare()
        try engine.start()

        self.isListening = true

        self.recognitionTask = recognizer.recognitionTask(with: request) { [weak self] result, error in
            Task { @MainActor in
                guard let self = self else { return }
                if let result = result {
                    let text = result.bestTranscription.formattedString
                    self.recognizedText = text
                    onRecognized(text)
                    if result.isFinal {
                        self.stopListening(onFinished: onFinished)
                    }
                }
                if error != nil {
                    self.stopListening(onFinished: onFinished)
                }
            }
        }
    }

    func stopListening(onFinished: @escaping (String) -> Void) {
        guard isListening else { return }
        audioEngine?.stop()
        audioEngine?.inputNode.removeTap(onBus: 0)
        recognitionRequest?.endAudio()
        recognitionTask?.cancel()
        recognitionTask = nil
        recognitionRequest = nil
        audioEngine = nil
        isListening = false
        let text = recognizedText.trimmingCharacters(in: .whitespacesAndNewlines)
        if !text.isEmpty {
            onFinished(text)
        }
    }
}

// MARK: - Models

struct WorkflowItem: Identifiable, Decodable {
    var id: String { workflow_id ?? rawId ?? UUID().uuidString }
    let workflow_id: String?
    let id_field: String?
    let name: String
    let description: String?
    let confidence: Double?
    let step_count: Int?
    let canonical_trigger: String?
    let video_path: String?
    let recording_score: Double?
    let recording_grade: String?

    enum CodingKeys: String, CodingKey {
        case workflow_id
        case id_field = "id"
        case name
        case description
        case confidence
        case step_count
        case canonical_trigger
        case video_path
        case recording_score
        case recording_grade
    }

    var rawId: String? { id_field }
    var displayName: String { name }
    var displayDesc: String { description ?? "" }
    var displaySteps: Int { step_count ?? 0 }
    var matchScore: Int { Int((confidence ?? 1.0) * 100) }
}

struct ScreenRecordingPlayerView: NSViewRepresentable {
    let videoURL: URL

    func makeNSView(context: Context) -> AVPlayerView {
        let playerView = AVPlayerView()
        let player = AVPlayer(url: videoURL)
        playerView.player = player
        playerView.controlsStyle = .inline
        playerView.showsFrameSteppingButtons = true
        playerView.showsFullScreenToggleButton = true
        playerView.videoGravity = .resizeAspect

        NotificationCenter.default.addObserver(
            forName: .AVPlayerItemDidPlayToEndTime,
            object: nil,
            queue: .main
        ) { [weak player] notif in
            if let curItem = player?.currentItem,
               let obj = notif.object as? AVPlayerItem,
               curItem == obj {
                player?.seek(to: .zero)
                player?.play()
            }
        }

        player.play()
        return playerView
    }

    func updateNSView(_ nsView: AVPlayerView, context: Context) {
        if let currentItem = nsView.player?.currentItem,
           let currentURL = (currentItem.asset as? AVURLAsset)?.url,
           currentURL == videoURL {
            return
        }
        let player = AVPlayer(url: videoURL)
        nsView.player = player
        player.play()
    }
}

struct ClioStatus: Decodable {
    let status: String
    let tone: String?
    let current_step: Int?
    let total_steps: Int?
    let virtual_cursor: VirtualCursorStatus?
    let recent_commentary: [String]?

    struct VirtualCursorStatus: Decodable {
        let x: Double
        let y: Double
        let state: String
    }
}

// MARK: - Independent Physical Virtual Cursor (Mac Arrow Shape with Custom Accent & Clio Label)

struct MacCursorArrowShape: Shape {
    func path(in rect: CGRect) -> Path {
        var path = Path()
        let w = rect.width
        let h = rect.height
        
        // Exact geometric proportions of iconic macOS pointer arrow
        path.move(to: CGPoint(x: 0, y: 0))                              // 1. Tip
        path.addLine(to: CGPoint(x: 0, y: h * 0.88))                    // 2. Left blade edge
        path.addLine(to: CGPoint(x: w * 0.28, y: h * 0.65))             // 3. Inner notch
        path.addLine(to: CGPoint(x: w * 0.54, y: h * 1.0))              // 4. Stem bottom-left
        path.addLine(to: CGPoint(x: w * 0.74, y: h * 0.88))             // 5. Stem bottom-right
        path.addLine(to: CGPoint(x: w * 0.48, y: h * 0.54))             // 6. Stem top-right
        path.addLine(to: CGPoint(x: w * 0.86, y: h * 0.54))             // 7. Right blade barb
        path.closeSubpath()                                             // 8. Close to tip
        return path
    }
}

struct VirtualCursorView: View {
    @ObservedObject var manager: VirtualCursorOverlayManager

    // Custom distinct vibrant color for Clio's physical cursor: Electric Violet/Amethyst
    private let arrowGradient = LinearGradient(
        colors: [
            Color(red: 0.72, green: 0.38, blue: 1.0),   // #B861FF
            Color(red: 0.48, green: 0.20, blue: 0.96)    // #7B33F5
        ],
        startPoint: .topLeading,
        endPoint: .bottomTrailing
    )

    var body: some View {
        ZStack(alignment: .topLeading) {
            // Click wave ripple from arrow tip
            if manager.isClicking {
                Circle()
                    .stroke(Color(red: 0.75, green: 0.40, blue: 1.0).opacity(0.85), lineWidth: 2)
                    .frame(width: 32, height: 32)
                    .scaleEffect(1.5)
                    .position(x: 2, y: 2)
                    .animation(.easeOut(duration: 0.25), value: manager.isClicking)
            }

            // Authentic Mac Pointer Arrow in Electric Purple Accent
            ZStack {
                // Black drop shadow/outline for crisp contrast on any background
                MacCursorArrowShape()
                    .stroke(Color.black, lineWidth: 2.5)
                    .frame(width: 17, height: 25)

                MacCursorArrowShape()
                    .fill(arrowGradient)
                    .frame(width: 17, height: 25)
                    .overlay(
                        MacCursorArrowShape()
                            .stroke(Color.white.opacity(0.4), lineWidth: 0.8)
                    )
                    .shadow(color: Color(red: 0.65, green: 0.30, blue: 1.0).opacity(0.6), radius: 4, x: 0, y: 1)
            }
            .scaleEffect(manager.isClicking ? 0.90 : 1.0)
            .animation(.easeInOut(duration: 0.1), value: manager.isClicking)
            .offset(x: 2, y: 2)

            // "Clio" Name Badge beside the pointer arrow
            HStack(spacing: 3) {
                Circle()
                    .fill(Color(red: 0.75, green: 0.40, blue: 1.0))
                    .frame(width: 4, height: 4)
                Text("Clio")
                    .font(.system(size: 9, weight: .bold, design: .rounded))
                    .foregroundColor(.white)
            }
            .padding(.horizontal, 6)
            .padding(.vertical, 2.5)
            .background(
                Capsule()
                    .fill(Color(red: 0.08, green: 0.08, blue: 0.12).opacity(0.92))
                    .overlay(
                        Capsule()
                            .stroke(Color(red: 0.75, green: 0.40, blue: 1.0).opacity(0.8), lineWidth: 1)
                    )
            )
            .shadow(color: Color.black.opacity(0.4), radius: 4, x: 0, y: 2)
            .offset(x: 20, y: 12)
        }
        .frame(width: 90, height: 45)
    }
}

@MainActor
final class VirtualCursorOverlayManager: ObservableObject {
    static let shared = VirtualCursorOverlayManager()
    private var window: NSPanel?
    @Published var currentX: CGFloat = -200
    @Published var currentY: CGFloat = -200
    @Published var currentState: String = "IDLE"
    @Published var isClicking: Bool = false

    init() {
        setupOverlay()
    }

    private func setupOverlay() {
        let panel = NSPanel(
            contentRect: NSRect(x: -200, y: -200, width: 90, height: 45),
            styleMask: [.nonactivatingPanel, .borderless],
            backing: .buffered,
            defer: false
        )
        panel.isFloatingPanel = true
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = false
        panel.ignoresMouseEvents = true

        let view = VirtualCursorView(manager: self)
        panel.contentView = NSHostingView(rootView: view)
        panel.orderOut(nil)
        self.window = panel
    }

    func updatePosition(x: CGFloat, y: CGFloat, state: String) {
        guard let screen = NSScreen.main else { return }
        let screenH = screen.frame.height
        // In macOS coordinates: tip of arrow is at (2, 2) in panel
        let winX = x - 2
        let winY = screenH - y - 43

        self.currentX = x
        self.currentY = y
        self.currentState = state
        self.isClicking = (state.uppercased() == "CLICKING")

        window?.setFrameOrigin(NSPoint(x: winX, y: winY))
        if window?.isVisible == false {
            window?.orderFront(nil)
        }
    }

    func hide() {
        window?.orderOut(nil)
        window?.setFrameOrigin(NSPoint(x: -200, y: -200))
        self.isClicking = false
        self.currentState = "IDLE"
    }
}

// MARK: - View Model

@MainActor
final class ClioViewModel: ObservableObject {
    @Published var query: String = ""
    @Published var workflows: [WorkflowItem] = []
    @Published var selectedIndex: Int = 0
    @Published var isExecuting: Bool = false
    @Published var isRecording: Bool = false
    @Published var isListening: Bool = false
    @Published var isBackgroundMode: Bool = true
    @Published var showSaveModal: Bool = false
    @Published var previewVideoURL: URL? = nil
    @Published var previewWorkflowId: String? = nil
    @Published var recordingScore: Double? = nil
    @Published var recordingGrade: String? = nil
    @Published var isStoppingRecording: Bool = false
    @Published var recordedName: String = ""
    @Published var recordedTrigger: String = ""
    @Published var currentTaskName: String = ""
    @Published var currentStepText: String = ""
    @Published var progress: Double = 0.0
    @Published var currentTone: String = "Vibrant"
    @Published var vcCoords: String = "VC (0, 0) • IDLE"
    @Published var commentary: [String] = []

    // Engine Connection Status
    @Published var isConnected: Bool = false
    @Published var statusPillText: String = "CONNECTING..."

    private var sseTask: Task<Void, Never>?
    private let baseURL = URL(string: "http://127.0.0.1:8765")!
    private let speechManager = SpeechDictationManager()

    init() {
        Task {
            await ServerLauncher.shared.ensureServerRunning()
            await checkConnectionAndStart()
        }
    }

    func checkConnectionAndStart() async {
        // Continuous auto-healing loop: guarantees connection even after server restarts or delays
        while true {
            if await ServerLauncher.shared.isServerReachable() {
                self.isConnected = true
                self.statusPillText = "CLIO • READY"
                await fetchWorkflows()
                await fetchStatus()
                startSSEStream()
                return
            }
            await ServerLauncher.shared.ensureServerRunning()
            try? await Task.sleep(nanoseconds: 800_000_000)
        }
    }

    func fetchWorkflows() async {
        guard let url = URL(string: "/api/workflows", relativeTo: baseURL) else { return }
        do {
            let (data, response) = try await URLSession.shared.data(from: url)
            if let http = response as? HTTPURLResponse, (200...299).contains(http.statusCode) {
                let items = try JSONDecoder().decode([WorkflowItem].self, from: data)
                self.workflows = items
                self.isConnected = true
                if self.selectedIndex >= items.count { self.selectedIndex = 0 }
            }
        } catch {
            // Silently suppress -1004 connection errors while retrying
            self.isConnected = false
        }
    }

    func fetchStatus() async {
        guard let url = URL(string: "/api/status", relativeTo: baseURL) else { return }
        do {
            let (data, response) = try await URLSession.shared.data(from: url)
            if let http = response as? HTTPURLResponse, (200...299).contains(http.statusCode) {
                let st = try JSONDecoder().decode(ClioStatus.self, from: data)
                self.isExecuting = (st.status == "executing")
                if let tone = st.tone { self.currentTone = tone.capitalized }
                if let vc = st.virtual_cursor {
                    self.vcCoords = "VC (\(Int(vc.x)), \(Int(vc.y))) • \(vc.state.uppercased())"
                }
                if let comm = st.recent_commentary { self.commentary = comm }
                self.statusPillText = self.isExecuting ? "CLIO • BUSY" : "CLIO • READY"
                self.isConnected = true
            }
        } catch {
            self.isConnected = false
        }
    }

    func search(text: String) async {
        guard !text.trimmingCharacters(in: .whitespaces).isEmpty else {
            await fetchWorkflows()
            return
        }
        guard let encoded = text.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed),
              let url = URL(string: "/api/search?q=\(encoded)", relativeTo: baseURL) else { return }
        do {
            let (data, _) = try await URLSession.shared.data(from: url)
            let items = try JSONDecoder().decode([WorkflowItem].self, from: data)
            self.workflows = items
            self.selectedIndex = 0
        } catch {
            // Silently handled
        }
    }

    func executeSelected() {
        let trimmed = query.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty else { return }

        let lower = trimmed.lowercased()

        // 1. Explicit user commands to close / dismiss the bar
        if ["close", "hide", "quit", "exit", "dismiss", "cancel", "done", "esc"].contains(lower) {
            self.query = ""
            AppDelegate.shared?.hidePanel()
            return
        }

        // Support command-line workflow deletion: "delete <name>" or "remove <name>"
        if lower.hasPrefix("delete ") || lower.hasPrefix("remove ") {
            let targetName = trimmed.dropFirst(7).trimmingCharacters(in: .whitespaces)
            if let match = workflows.first(where: {
                $0.displayName.localizedCaseInsensitiveContains(targetName) ||
                ($0.canonical_trigger?.localizedCaseInsensitiveContains(targetName) ?? false)
            }) {
                deleteWorkflow(id: match.id)
                self.query = ""
                AppDelegate.shared?.hidePanel()
                return
            }
        }

        let normQuery = trimmed.replacingOccurrences(of: "\\s+", with: " ", options: .regularExpression).lowercased()

        // Check if there is an exact or canonical trigger match in learned memory
        if let match = workflows.first(where: {
            let dName = $0.displayName.replacingOccurrences(of: "\\s+", with: " ", options: .regularExpression).lowercased()
            let cTrig = $0.canonical_trigger?.replacingOccurrences(of: "\\s+", with: " ", options: .regularExpression).lowercased()
            return dName == normQuery || cTrig == normQuery
        }) {
            executeById(match.id)
        } else if let topMatch = workflows.first, (topMatch.confidence ?? 0) >= 0.70 {
            executeById(topMatch.id)
        } else {
            executeByQuery(trimmed)
        }
        self.query = ""

        // Close the bar based on user command: task dispatched, clear screen for hands-free automation
        AppDelegate.shared?.hidePanel()
    }

    func deleteWorkflow(id: String) {
        guard let url = URL(string: "/api/workflows/delete", relativeTo: baseURL) else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["workflow_id": id])
        Task {
            _ = try? await URLSession.shared.data(for: req)
            await fetchWorkflows()
            if self.selectedIndex >= self.workflows.count {
                self.selectedIndex = max(0, self.workflows.count - 1)
            }
        }
    }

    func deleteSelected() {
        guard !workflows.isEmpty, selectedIndex >= 0, selectedIndex < workflows.count else { return }
        let wf = workflows[selectedIndex]
        deleteWorkflow(id: wf.id)
    }

    func toggleBackgroundMode() {
        isBackgroundMode.toggle()
    }

    func toggleDictation() {
        speechManager.toggleDictation(
            onRecognized: { [weak self] text in
                guard let self = self else { return }
                self.query = text
                self.isListening = true
            },
            onFinished: { [weak self] finalText in
                guard let self = self else { return }
                self.query = finalText
                self.isListening = false
                Task {
                    await self.search(text: finalText)
                }
            }
        )
        self.isListening = speechManager.isListening
    }

    func toggleRecording() {
        if !isRecording {
            startRecording()
        } else {
            stopRecordingAndShowPreview()
        }
    }

    private var recordingEventMonitor: Any?

    func startRecording() {
        // Reset inputs and preview state
        self.recordedName = ""
        self.recordedTrigger = ""
        self.previewVideoURL = nil
        self.previewWorkflowId = nil
        self.recordingScore = nil
        self.recordingGrade = nil
        self.showSaveModal = false
        self.isRecording = true
        self.startEventMonitoring()

        guard let url = URL(string: "/api/record/start", relativeTo: baseURL) else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = "{}".data(using: .utf8)
        Task {
            _ = try? await URLSession.shared.data(for: req)
        }
    }

    func stopRecordingAndShowPreview() {
        self.stopEventMonitoring()
        self.isStoppingRecording = true
        self.showSaveModal = true
        self.isRecording = false

        guard let url = URL(string: "/api/record/stop", relativeTo: baseURL) else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = "{\"name\":\"My Demonstrated Action\",\"trigger\":\"my demonstrated action\"}".data(using: .utf8)

        Task {
            do {
                let (data, resp) = try await URLSession.shared.data(for: req)
                if let http = resp as? HTTPURLResponse, (200...299).contains(http.statusCode) {
                    if let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                        await MainActor.run {
                            self.isStoppingRecording = false
                            self.previewWorkflowId = json["workflow_id"] as? String
                            if let vPath = json["video_path"] as? String, !vPath.isEmpty {
                                self.previewVideoURL = URL(fileURLWithPath: vPath)
                            }
                            self.recordingScore = json["recording_score"] as? Double
                            self.recordingGrade = json["recording_grade"] as? String
                            let origName = json["name"] as? String ?? ""
                            if self.recordedName.isEmpty {
                                self.recordedName = (origName == "My Demonstrated Action") ? "" : origName
                            }
                            let origTrig = json["canonical_trigger"] as? String ?? ""
                            if self.recordedTrigger.isEmpty {
                                self.recordedTrigger = (origTrig == "my demonstrated action") ? "" : origTrig
                            }
                        }
                        return
                    }
                }
                await MainActor.run {
                    self.isStoppingRecording = false
                }
            } catch {
                await MainActor.run {
                    self.isStoppingRecording = false
                }
            }
        }
    }

    func cancelRecording() {
        discardRecording()
    }

    func discardRecording() {
        self.stopEventMonitoring()
        self.isRecording = false
        self.showSaveModal = false
        self.previewVideoURL = nil
        self.recordingScore = nil
        self.recordingGrade = nil
        let wfId = self.previewWorkflowId
        self.previewWorkflowId = nil
        self.recordedName = ""
        self.recordedTrigger = ""

        if let wfId = wfId {
            guard let url = URL(string: "/api/workflows/delete", relativeTo: baseURL) else { return }
            var req = URLRequest(url: url)
            req.httpMethod = "POST"
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try? JSONSerialization.data(withJSONObject: ["workflow_id": wfId])
            Task {
                _ = try? await URLSession.shared.data(for: req)
                await self.fetchWorkflows()
            }
        }
    }

    func previewExistingWorkflowRecording(_ wf: WorkflowItem) {
        if let path = wf.video_path, !path.isEmpty, FileManager.default.fileExists(atPath: path) {
            self.previewVideoURL = URL(fileURLWithPath: path)
            self.previewWorkflowId = wf.id
            self.recordedName = wf.name
            self.recordedTrigger = wf.canonical_trigger ?? ""
            self.recordingScore = wf.recording_score
            self.recordingGrade = wf.recording_grade
            self.showSaveModal = true
        }
    }

    private func startEventMonitoring() {
        stopEventMonitoring()
        self.recordingEventMonitor = NSEvent.addGlobalMonitorForEvents(matching: [
            .leftMouseDown, .leftMouseUp, .rightMouseDown, .rightMouseUp, .keyDown, .scrollWheel
        ]) { [weak self] event in
            Task { @MainActor [weak self] in
                self?.sendFeedEvent(event)
            }
        }
    }

    private func stopEventMonitoring() {
        if let monitor = self.recordingEventMonitor {
            NSEvent.removeMonitor(monitor)
            self.recordingEventMonitor = nil
        }
    }

    private func sendFeedEvent(_ event: NSEvent) {
        guard let url = URL(string: "/api/record/feed", relativeTo: baseURL) else { return }
        let screenH = NSScreen.main?.frame.height ?? 900
        let mouseLoc = NSEvent.mouseLocation
        let x = Double(mouseLoc.x)
        let y = Double(screenH - mouseLoc.y)
        var targetBundle = NSWorkspace.shared.frontmostApplication?.bundleIdentifier ?? ""
        var targetAppName = NSWorkspace.shared.frontmostApplication?.localizedName ?? ""
        var isDockItem = false
        var dockTitle = ""
        var windowBoundsDict: [String: Double]? = nil

        let sys = AXUIElementCreateSystemWide()
        var elem: AXUIElement?
        if AXUIElementCopyElementAtPosition(sys, Float(mouseLoc.x), Float(mouseLoc.y), &elem) == .success, let elem = elem {
            var pid: pid_t = 0
            if AXUIElementGetPid(elem, &pid) == .success && pid > 0 {
                if let app = NSRunningApplication(processIdentifier: pid) {
                    targetBundle = app.bundleIdentifier ?? targetBundle
                    targetAppName = app.localizedName ?? targetAppName
                }
            }

            var winElem: AnyObject?
            if AXUIElementCopyAttributeValue(elem, kAXWindowAttribute as CFString, &winElem) == .success, let wElem = winElem {
                var posVal: AnyObject?
                var sizeVal: AnyObject?
                let axW = wElem as! AXUIElement
                if AXUIElementCopyAttributeValue(axW, kAXPositionAttribute as CFString, &posVal) == .success,
                   AXUIElementCopyAttributeValue(axW, kAXSizeAttribute as CFString, &sizeVal) == .success {
                    var pt = CGPoint.zero
                    var sz = CGSize.zero
                    AXValueGetValue(posVal as! AXValue, .cgPoint, &pt)
                    AXValueGetValue(sizeVal as! AXValue, .cgSize, &sz)
                    windowBoundsDict = [
                        "x": Double(pt.x),
                        "y": Double(pt.y),
                        "width": Double(sz.width),
                        "height": Double(sz.height)
                    ]
                }
            }

            var roleVal: AnyObject?
            if AXUIElementCopyAttributeValue(elem, kAXRoleAttribute as CFString, &roleVal) == .success,
               let role = roleVal as? String, role == "AXDockItem" {
                isDockItem = true
                var titleVal: AnyObject?
                if AXUIElementCopyAttributeValue(elem, kAXTitleAttribute as CFString, &titleVal) == .success,
                   let title = titleVal as? String {
                    dockTitle = title
                }
            }
        }

        var payload: [String: Any] = [
            "x": x,
            "y": y,
            "bundle_id": targetBundle,
            "app_name": targetAppName,
            "is_dock_item": isDockItem,
            "dock_item_title": dockTitle,
        ]
        if let wb = windowBoundsDict {
            payload["window_bounds"] = wb
        }

        if event.type == .leftMouseDown {
            payload["event_type"] = "mouse_down"
            payload["button"] = "left"
        } else if event.type == .leftMouseUp {
            payload["event_type"] = "mouse_up"
            payload["button"] = "left"
        } else if event.type == .rightMouseDown {
            payload["event_type"] = "mouse_down"
            payload["button"] = "right"
        } else if event.type == .rightMouseUp {
            payload["event_type"] = "mouse_up"
            payload["button"] = "right"
        } else if event.type == .scrollWheel {
            payload["event_type"] = "scroll"
            payload["dx"] = Double(event.scrollingDeltaX)
            payload["dy"] = Double(event.scrollingDeltaY)
        } else if event.type == .keyDown {
            payload["event_type"] = "key_down"
            var mods: [String] = []
            if event.modifierFlags.contains(.command) { mods.append("cmd") }
            if event.modifierFlags.contains(.shift) { mods.append("shift") }
            if event.modifierFlags.contains(.option) { mods.append("alt") }
            if event.modifierFlags.contains(.control) { mods.append("ctrl") }
            payload["modifiers"] = mods

            let keyStr: String
            switch event.keyCode {
            case 36: keyStr = "Return"
            case 48: keyStr = "Tab"
            case 49: keyStr = " "
            case 51: keyStr = "BackSpace"
            case 53: keyStr = "Escape"
            case 123: keyStr = "Left"
            case 124: keyStr = "Right"
            case 125: keyStr = "Down"
            case 126: keyStr = "Up"
            default:
                let unmod = event.charactersIgnoringModifiers ?? ""
                keyStr = unmod.isEmpty ? "k_\(event.keyCode)" : unmod
            }
            payload["key"] = keyStr
        }

        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: payload)
        Task {
            _ = try? await URLSession.shared.data(for: req)
        }
    }

    func saveRecordedWorkflow() {
        self.stopEventMonitoring()
        self.isRecording = false

        guard let wfId = previewWorkflowId else {
            self.showSaveModal = false
            self.previewVideoURL = nil
            self.recordedName = ""
            self.recordedTrigger = ""
            return
        }

        guard let url = URL(string: "/api/workflows/update", relativeTo: baseURL) else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let trimmedName = recordedName.trimmingCharacters(in: .whitespaces)
        let trimmedTrigger = recordedTrigger.trimmingCharacters(in: .whitespaces)
        let finalName = trimmedName.isEmpty ? "My Demonstrated Action" : trimmedName
        let finalTrigger = trimmedTrigger.isEmpty ? finalName.lowercased() : trimmedTrigger
        req.httpBody = try? JSONSerialization.data(withJSONObject: [
            "workflow_id": wfId,
            "name": finalName,
            "trigger": finalTrigger,
        ])
        Task {
            _ = try? await URLSession.shared.data(for: req)
            await MainActor.run {
                self.showSaveModal = false
                self.previewVideoURL = nil
                self.previewWorkflowId = nil
                self.recordingScore = nil
                self.recordingGrade = nil
                self.recordedName = ""
                self.recordedTrigger = ""
                self.query = ""
            }
            await self.fetchWorkflows()
        }
    }

    func stopAndSaveRecording() {
        if previewWorkflowId != nil {
            saveRecordedWorkflow()
        } else {
            stopRecordingAndShowPreview()
        }
    }

    func executeById(_ id: String) {
        self.currentStepText = ""
        self.currentTaskName = ""
        guard let url = URL(string: "/api/execute", relativeTo: baseURL) else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: [
            "workflow_id": id,
            "background": isBackgroundMode
        ])
        Task {
            do {
                let (data, response) = try await URLSession.shared.data(for: req)
                if let http = response as? HTTPURLResponse, (200...299).contains(http.statusCode) {
                    if let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                        if let name = json["name"] as? String {
                            self.currentTaskName = name
                            self.isExecuting = true
                        }
                    }
                }
            } catch { }
        }
    }

    func executeByQuery(_ q: String) {
        self.currentStepText = ""
        self.currentTaskName = ""
        guard let url = URL(string: "/api/execute", relativeTo: baseURL) else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: [
            "query": q,
            "background": isBackgroundMode
        ])
        Task {
            do {
                let (data, response) = try await URLSession.shared.data(for: req)
                if let http = response as? HTTPURLResponse, (200...299).contains(http.statusCode) {
                    if let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                        if let name = json["name"] as? String {
                            self.currentTaskName = name
                            self.isExecuting = true
                        }
                    }
                }
            } catch { }
        }
    }


    func cancelTask() {
        guard let url = URL(string: "/api/cancel", relativeTo: baseURL) else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = "{}".data(using: .utf8)
        Task {
            _ = try? await URLSession.shared.data(for: req)
        }
    }

    func cycleTone() {
        let tones = ["vibrant", "concise", "zen", "developer"]
        let lower = currentTone.lowercased()
        let idx = tones.firstIndex(of: lower) ?? 0
        let nextTone = tones[(idx + 1) % tones.count]
        currentTone = nextTone.capitalized

        guard let url = URL(string: "/api/tone", relativeTo: baseURL) else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["tone": nextTone])
        Task {
            _ = try? await URLSession.shared.data(for: req)
        }
    }

    func startSSEStream() {
        sseTask?.cancel()
        sseTask = Task {
            guard let url = URL(string: "/api/stream", relativeTo: baseURL) else { return }
            do {
                let (stream, _) = try await URLSession.shared.bytes(from: url)
                for try await line in stream.lines {
                    if Task.isCancelled { break }
                    if line.hasPrefix("data: ") {
                        let jsonStr = String(line.dropFirst(6))
                        if let data = jsonStr.data(using: .utf8),
                           let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                           let type = obj["type"] as? String {
                            await self.handleSSEEvent(type: type, obj: obj)
                        }
                    }
                }
            } catch {
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                if !Task.isCancelled {
                    self.startSSEStream()
                }
            }
        }
    }

    private func handleSSEEvent(type: String, obj: [String: Any]) async {
        if type == "execution" {
            let evType = obj["event_type"] as? String ?? ""
            if evType == "task_started" {
                self.isExecuting = true
                self.statusPillText = "CLIO • EXECUTING"
                self.currentTaskName = obj["task_id"] as? String ?? "Task"
                self.progress = 0.05
                self.currentStepText = obj["message"] as? String ?? "Starting..."
            } else if evType == "action_starting" {
                let step = obj["step_index"] as? Int ?? 1
                let total = max(1, obj["total_steps"] as? Int ?? 1)
                self.progress = Double(step) / Double(total)
                self.currentStepText = "Step \(step)/\(total): " + (obj["message"] as? String ?? "")
            } else if evType == "task_completed" || evType == "emergency_stop" {
                self.progress = 1.0
                self.currentStepText = obj["message"] as? String ?? "Finished"
                Task {
                    try? await Task.sleep(nanoseconds: 2_500_000_000)
                    self.isExecuting = false
                    self.currentStepText = ""
                    self.currentTaskName = ""
                    self.statusPillText = "CLIO • READY"
                    VirtualCursorOverlayManager.shared.hide()
                }
            }
        } else if type == "commentary" {
            if let text = obj["text"] as? String {
                self.commentary.append(text)
                if self.commentary.count > 25 { self.commentary.removeFirst() }
            }
        } else if type == "cursor" {
            let x = obj["x"] as? Double ?? 0
            let y = obj["y"] as? Double ?? 0
            let state = (obj["state"] as? String ?? "IDLE").uppercased()
            self.vcCoords = "VC (\(Int(x)), \(Int(y))) • \(state)"
            VirtualCursorOverlayManager.shared.updatePosition(x: CGFloat(x), y: CGFloat(y), state: state)
        } else if type == "ui" {
            let action = obj["action"] as? String ?? ""
            Task { @MainActor in
                if action == "show" {
                    AppDelegate.shared?.showPanel()
                } else if action == "hide" {
                    AppDelegate.shared?.hidePanel()
                } else if action == "toggle" {
                    AppDelegate.shared?.togglePanel()
                }
            }
        }
    }
}

// MARK: - SwiftUI View (Obsidian Glass HUD — Monochromatic Single Color Scheme)

struct ClioBarView: View {
    @StateObject private var vm = ClioViewModel()
    @FocusState private var isFieldFocused: Bool

    private var currentTargetHeight: CGFloat {
        if vm.showSaveModal {
            return 430
        }
        let trimmed = vm.query.trimmingCharacters(in: .whitespaces)
        if !trimmed.isEmpty && !vm.workflows.isEmpty {
            let rowCount = min(vm.workflows.count, 4)
            return 58 + CGFloat(rowCount * 38) + 16
        }
        return 58
    }

    var body: some View {
        VStack(spacing: 0) {
            // Main Top Pill Bar
            HStack(spacing: 10) {
                // Minimalist Obsidian Eclipse 'C' Monogram
                ZStack {
                    RoundedRectangle(cornerRadius: 8)
                        .fill(ObsidianTheme.surfaceElevated)
                        .overlay(
                            RoundedRectangle(cornerRadius: 8)
                                .stroke(ObsidianTheme.borderSubtle, lineWidth: 1)
                        )
                        .frame(width: 32, height: 32)
                    Circle()
                        .fill(Color(red: 0.16, green: 0.17, blue: 0.19))
                        .frame(width: 18, height: 18)
                        .overlay(
                            Circle()
                                .strokeBorder(
                                    AngularGradient(
                                        gradient: Gradient(colors: [
                                            ObsidianTheme.platinum,
                                            ObsidianTheme.platinum.opacity(0.9),
                                            Color.clear,
                                            Color.clear,
                                            Color.clear,
                                            ObsidianTheme.platinum.opacity(0.7)
                                        ]),
                                        center: .center,
                                        startAngle: .degrees(90),
                                        endAngle: .degrees(450)
                                    ),
                                    lineWidth: 2.2
                                )
                        )
                }

                // Command Search Input
                TextField("Ask clio to do anything...", text: $vm.query)
                    .textFieldStyle(.plain)
                    .font(.system(size: 16, weight: .regular))
                    .foregroundColor(ObsidianTheme.platinum)
                    .focused($isFieldFocused)
                    .onSubmit {
                        vm.executeSelected()
                    }
                    .onChange(of: vm.query) { newQuery in
                        Task { await vm.search(text: newQuery) }
                    }

                // Dictation Microphone Button
                Button(action: { vm.toggleDictation() }) {
                    HStack(spacing: 5) {
                        Image(systemName: vm.isListening ? "waveform" : "mic.fill")
                            .font(.system(size: 11, weight: .semibold))
                        if vm.isListening {
                            Text("LISTENING...")
                                .font(.system(size: 9, weight: .bold, design: .monospaced))
                        }
                    }
                    .foregroundColor(vm.isListening ? ObsidianTheme.surface : ObsidianTheme.platinum)
                    .padding(.horizontal, 9)
                    .padding(.vertical, 5)
                    .background(vm.isListening ? ObsidianTheme.platinum : ObsidianTheme.surfaceElevated)
                    .overlay(
                        Capsule().stroke(vm.isListening ? ObsidianTheme.platinum : ObsidianTheme.borderSubtle, lineWidth: 1)
                    )
                    .clipShape(Capsule())
                }
                .buttonStyle(.plain)
                .help("Dictate command with voice")

                // Single-Color Record Demonstration Button
                Button(action: { vm.toggleRecording() }) {
                    HStack(spacing: 6) {
                        Circle()
                            .fill(vm.isRecording ? ObsidianTheme.surface : ObsidianTheme.platinum)
                            .frame(width: 6, height: 6)
                            .opacity(vm.isRecording ? 1.0 : 0.6)
                        Text(vm.isRecording ? "● STOP & SAVE" : "RECORD")
                            .font(.system(size: 10, weight: .semibold, design: .monospaced))
                    }
                    .foregroundColor(vm.isRecording ? ObsidianTheme.surface : ObsidianTheme.platinum)
                    .padding(.horizontal, 9)
                    .padding(.vertical, 5)
                    .background(vm.isRecording ? ObsidianTheme.platinum : ObsidianTheme.surfaceElevated)
                    .overlay(
                        Capsule().stroke(vm.isRecording ? ObsidianTheme.platinum : ObsidianTheme.borderSubtle, lineWidth: 1)
                    )
                    .clipShape(Capsule())
                }
                .buttonStyle(.plain)
                .help("Record a new desktop demonstration")

                // Dismiss / Hide Bar Button
                Button(action: {
                    AppDelegate.shared?.hidePanel()
                }) {
                    Image(systemName: "xmark")
                        .font(.system(size: 10, weight: .bold))
                        .foregroundColor(ObsidianTheme.slate)
                        .frame(width: 24, height: 24)
                        .background(ObsidianTheme.surfaceElevated)
                        .clipShape(Circle())
                        .overlay(Circle().stroke(ObsidianTheme.borderSubtle, lineWidth: 1))
                }
                .buttonStyle(.plain)
                .help("Hide Clio Bar (Esc)")
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 12)

            // Screen Recording Preview & Save Demonstration Card (Obsidian Glass HUD)
            if vm.showSaveModal {
                Divider().background(ObsidianTheme.borderSubtle)
                VStack(alignment: .leading, spacing: 10) {
                    // Header Bar with Title, Quality Score, and Quick Action Buttons
                    HStack(spacing: 8) {
                        HStack(spacing: 6) {
                            Circle()
                                .fill(ObsidianTheme.platinum)
                                .frame(width: 6, height: 6)
                            Text("SCREEN RECORDING CAPTURED")
                                .font(.system(size: 10, weight: .bold, design: .monospaced))
                                .foregroundColor(ObsidianTheme.platinum)
                        }

                        // Quality Evaluation Score Badge
                        if let score = vm.recordingScore, let grade = vm.recordingGrade {
                            HStack(spacing: 4) {
                                Text("QUALITY:")
                                    .font(.system(size: 9, weight: .medium, design: .monospaced))
                                    .foregroundColor(ObsidianTheme.slate)
                                Text("\(Int(score))/100 • \(grade)")
                                    .font(.system(size: 9, weight: .bold, design: .monospaced))
                                    .foregroundColor(ObsidianTheme.platinum)
                            }
                            .padding(.horizontal, 8)
                            .padding(.vertical, 3)
                            .background(ObsidianTheme.surfaceElevated)
                            .overlay(Capsule().stroke(ObsidianTheme.borderSubtle, lineWidth: 1))
                            .clipShape(Capsule())
                        }

                        Spacer()

                        // External Player & Finder Quick Actions
                        if let videoURL = vm.previewVideoURL {
                            Button(action: {
                                NSWorkspace.shared.open(videoURL)
                            }) {
                                HStack(spacing: 4) {
                                    Image(systemName: "arrow.up.right.video")
                                        .font(.system(size: 9))
                                    Text("QUICKTIME")
                                        .font(.system(size: 9, weight: .semibold, design: .monospaced))
                                }
                                .foregroundColor(ObsidianTheme.slate)
                                .padding(.horizontal, 7)
                                .padding(.vertical, 3)
                                .background(ObsidianTheme.surface)
                                .cornerRadius(5)
                                .overlay(RoundedRectangle(cornerRadius: 5).stroke(ObsidianTheme.borderSubtle, lineWidth: 1))
                            }
                            .buttonStyle(.plain)
                            .help("Open full recording in QuickTime Player")

                            Button(action: {
                                NSWorkspace.shared.activateFileViewerSelecting([videoURL])
                            }) {
                                HStack(spacing: 4) {
                                    Image(systemName: "folder")
                                        .font(.system(size: 9))
                                    Text("FINDER")
                                        .font(.system(size: 9, weight: .semibold, design: .monospaced))
                                }
                                .foregroundColor(ObsidianTheme.slate)
                                .padding(.horizontal, 7)
                                .padding(.vertical, 3)
                                .background(ObsidianTheme.surface)
                                .cornerRadius(5)
                                .overlay(RoundedRectangle(cornerRadius: 5).stroke(ObsidianTheme.borderSubtle, lineWidth: 1))
                            }
                            .buttonStyle(.plain)
                            .help("Reveal recording video file in Finder")
                        }
                    }

                    // Native Video Player Surface
                    ZStack {
                        RoundedRectangle(cornerRadius: 8)
                            .fill(ObsidianTheme.surface)
                            .frame(height: 250)
                            .overlay(
                                RoundedRectangle(cornerRadius: 8)
                                    .stroke(ObsidianTheme.borderSubtle, lineWidth: 1)
                            )

                        if vm.isStoppingRecording {
                            VStack(spacing: 8) {
                                ProgressView()
                                    .scaleEffect(0.8)
                                Text("FINALIZING SCREEN RECORDING & EVALUATING QUALITY...")
                                    .font(.system(size: 10, weight: .medium, design: .monospaced))
                                    .foregroundColor(ObsidianTheme.slate)
                            }
                        } else if let videoURL = vm.previewVideoURL {
                            ScreenRecordingPlayerView(videoURL: videoURL)
                                .frame(height: 250)
                                .cornerRadius(8)
                                .clipped()
                        } else {
                            VStack(spacing: 6) {
                                Image(systemName: "video.slash")
                                    .font(.system(size: 24))
                                    .foregroundColor(ObsidianTheme.slateDark)
                                Text("No recording preview available")
                                    .font(.system(size: 11, weight: .medium))
                                    .foregroundColor(ObsidianTheme.slate)
                            }
                        }
                    }
                    .frame(height: 250)

                    // Action Labeling and Persistence Controls
                    HStack(spacing: 8) {
                        TextField("Action Name (e.g. Open Notes & Write)", text: $vm.recordedName)
                            .textFieldStyle(.plain)
                            .padding(7)
                            .background(ObsidianTheme.surface)
                            .overlay(RoundedRectangle(cornerRadius: 6).stroke(ObsidianTheme.borderSubtle, lineWidth: 1))
                            .cornerRadius(6)
                            .foregroundColor(ObsidianTheme.platinum)
                            .font(.system(size: 12))
                            .onSubmit {
                                vm.saveRecordedWorkflow()
                            }

                        TextField("Trigger phrase (e.g. 'open notes')", text: $vm.recordedTrigger)
                            .textFieldStyle(.plain)
                            .padding(7)
                            .background(ObsidianTheme.surface)
                            .overlay(RoundedRectangle(cornerRadius: 6).stroke(ObsidianTheme.borderSubtle, lineWidth: 1))
                            .cornerRadius(6)
                            .foregroundColor(ObsidianTheme.platinum)
                            .font(.system(size: 12))
                            .onSubmit {
                                vm.saveRecordedWorkflow()
                            }

                        Button("Discard") {
                            vm.discardRecording()
                        }
                        .font(.system(size: 11, weight: .medium))
                        .foregroundColor(ObsidianTheme.slate)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 7)
                        .background(ObsidianTheme.surface)
                        .cornerRadius(6)
                        .buttonStyle(.plain)
                        .help("Discard this screen recording")

                        Button("Save & Add") {
                            vm.saveRecordedWorkflow()
                        }
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundColor(ObsidianTheme.surface)
                        .padding(.horizontal, 12)
                        .padding(.vertical, 7)
                        .background(ObsidianTheme.platinum)
                        .cornerRadius(6)
                        .buttonStyle(.plain)
                        .help("Save demonstration and add to Clio's memory")
                    }
                }
                .padding(.horizontal, 14)
                .padding(.vertical, 12)
                .background(ObsidianTheme.cardGlass)
            } else if !vm.query.trimmingCharacters(in: .whitespaces).isEmpty && !vm.workflows.isEmpty {
                // Search Results / Suggested Workflows with Video Preview Buttons
                Divider().background(ObsidianTheme.borderSubtle)
                VStack(spacing: 2) {
                    ForEach(Array(vm.workflows.prefix(4).enumerated()), id: \.element.id) { idx, wf in
                        HStack(spacing: 8) {
                            Image(systemName: "command")
                                .font(.system(size: 11))
                                .foregroundColor(idx == vm.selectedIndex ? ObsidianTheme.platinum : ObsidianTheme.slateDark)

                            VStack(alignment: .leading, spacing: 2) {
                                Text(wf.displayName)
                                    .font(.system(size: 12, weight: .medium))
                                    .foregroundColor(idx == vm.selectedIndex ? ObsidianTheme.platinum : ObsidianTheme.platinumDim)
                                if let trig = wf.canonical_trigger, !trig.isEmpty {
                                    Text(trig)
                                        .font(.system(size: 10, design: .monospaced))
                                        .foregroundColor(ObsidianTheme.slate)
                                }
                            }

                            Spacer()

                            // If video recording exists, show "▶ VIDEO" button
                            if let vPath = wf.video_path, !vPath.isEmpty {
                                Button(action: {
                                    vm.previewExistingWorkflowRecording(wf)
                                }) {
                                    HStack(spacing: 3) {
                                        Image(systemName: "play.circle.fill")
                                            .font(.system(size: 10))
                                        Text("VIDEO")
                                            .font(.system(size: 9, weight: .bold, design: .monospaced))
                                    }
                                    .foregroundColor(ObsidianTheme.platinum)
                                    .padding(.horizontal, 6)
                                    .padding(.vertical, 3)
                                    .background(ObsidianTheme.surfaceElevated)
                                    .cornerRadius(4)
                                    .overlay(RoundedRectangle(cornerRadius: 4).stroke(ObsidianTheme.borderSubtle, lineWidth: 1))
                                }
                                .buttonStyle(.plain)
                                .help("View screen recording of this action")
                            }

                            Text("\(wf.displaySteps) steps")
                                .font(.system(size: 10, design: .monospaced))
                                .foregroundColor(ObsidianTheme.slate)
                        }
                        .padding(.horizontal, 14)
                        .padding(.vertical, 6)
                        .background(idx == vm.selectedIndex ? ObsidianTheme.surfaceElevated : Color.clear)
                        .cornerRadius(6)
                        .contentShape(Rectangle())
                        .onTapGesture {
                            vm.selectedIndex = idx
                            vm.executeById(wf.id)
                        }
                    }
                }
                .padding(.horizontal, 8)
                .padding(.vertical, 6)
                .background(ObsidianTheme.cardGlass)
            }

        }
        .frame(width: 680)
        .background(ObsidianTheme.bgGlass)
        .cornerRadius(12)
        .overlay(
            RoundedRectangle(cornerRadius: 12)
                .stroke(ObsidianTheme.borderSubtle, lineWidth: 1)
        )
        .shadow(color: Color.black.opacity(0.65), radius: 24, x: 0, y: 12)
        .onAppear {
            isFieldFocused = true
        }
        .onExitCommand {
            if vm.showSaveModal {
                vm.discardRecording()
            } else {
                AppDelegate.shared?.hidePanel()
            }
        }
        .onChange(of: vm.showSaveModal) { _ in
            AppDelegate.shared?.updatePanelHeight(currentTargetHeight)
        }
        .onChange(of: vm.workflows.count) { _ in
            AppDelegate.shared?.updatePanelHeight(currentTargetHeight)
        }
        .onChange(of: vm.query) { _ in
            AppDelegate.shared?.updatePanelHeight(currentTargetHeight)
        }
        .onReceive(NotificationCenter.default.publisher(for: NSNotification.Name("DeleteSelectedWorkflow"))) { _ in
            vm.deleteSelected()
        }
        .onReceive(NotificationCenter.default.publisher(for: NSNotification.Name("FocusClioField"))) { _ in
            isFieldFocused = true
        }
    }
}

// MARK: - AppKit Window Setup

final class SpotlightPanel: NSPanel {
    init(contentRect: NSRect) {
        super.init(
            contentRect: contentRect,
            styleMask: [.nonactivatingPanel, .fullSizeContentView, .borderless],
            backing: .buffered,
            defer: false
        )
        self.isFloatingPanel = true
        self.level = .floating
        self.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        self.isOpaque = false
        self.backgroundColor = .clear
        self.hasShadow = true
        self.isMovableByWindowBackground = true
        self.titleVisibility = .hidden
        self.titlebarAppearsTransparent = true
    }

    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { true }

    override func cancelOperation(_ sender: Any?) {
        AppDelegate.shared?.hidePanel()
    }

    override func performKeyEquivalent(with event: NSEvent) -> Bool {
        // Esc key (53)
        if event.keyCode == 53 {
            AppDelegate.shared?.hidePanel()
            return true
        }
        // Cmd + W
        if event.modifierFlags.contains(.command) && event.charactersIgnoringModifiers == "w" {
            AppDelegate.shared?.hidePanel()
            return true
        }
        // Cmd + Backspace (51) to delete selected workflow
        if event.modifierFlags.contains(.command) && event.keyCode == 51 {
            NotificationCenter.default.post(name: NSNotification.Name("DeleteSelectedWorkflow"), object: nil)
            return true
        }
        return super.performKeyEquivalent(with: event)
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    static private(set) var shared: AppDelegate?
    var panel: SpotlightPanel?
    private var statusItem: NSStatusItem?
    private var carbonHotKeys: [EventHotKeyRef] = []
    private var carbonEventHandler: EventHandlerRef?
    private var globalKeyMonitor: Any?
    private var localKeyMonitor: Any?

    override init() {
        super.init()
        AppDelegate.shared = self
    }

    func showPanel() {
        guard let panel = panel else { return }
        panel.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        NotificationCenter.default.post(name: NSNotification.Name("FocusClioField"), object: nil)
    }

    func hidePanel() {
        panel?.orderOut(nil)
    }

    func togglePanel() {
        if let p = panel, p.isVisible {
            hidePanel()
        } else {
            showPanel()
        }
    }

    func updatePanelHeight(_ newHeight: CGFloat) {
        guard let panel = panel, let screen = NSScreen.main ?? NSScreen.screens.first else { return }
        let screenRect = screen.visibleFrame
        let panelWidth: CGFloat = 680
        let currentFrame = panel.frame
        let newY = screenRect.origin.y + screenRect.height - newHeight - 120
        let newFrame = NSRect(x: currentFrame.origin.x, y: newY, width: panelWidth, height: newHeight)
        panel.setFrame(newFrame, display: true, animate: true)
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        // Verify accessibility quietly without opening Settings
        _ = AXIsProcessTrusted()
        _ = VirtualCursorOverlayManager.shared

        let screenRect = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        let panelWidth: CGFloat = 680
        let panelHeight: CGFloat = 58
        let x = screenRect.origin.x + (screenRect.width - panelWidth) / 2
        let y = screenRect.origin.y + screenRect.height - panelHeight - 120

        let panel = SpotlightPanel(contentRect: NSRect(x: x, y: y, width: panelWidth, height: panelHeight))
        panel.contentView = NSHostingView(rootView: ClioBarView())
        panel.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        self.panel = panel

        setupStatusItem()
        setupGlobalShortcut()
    }

    private func setupStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let btn = statusItem?.button {
            btn.title = " ⌘ Clio "
            btn.toolTip = "Clio Assistant — Click or press ⌘⇧Space / ⌘⌥Space / ⌘K to toggle"
            btn.target = self
            btn.action = #selector(statusItemClicked)
        }
    }

    @objc private func statusItemClicked() {
        togglePanel()
    }

    private func setupGlobalShortcut() {
        // 1. Carbon HotKeys (works globally across ALL apps without accessibility requirements)
        var eventType = EventTypeSpec(
            eventClass: OSType(kEventClassKeyboard),
            eventKind: UInt32(kEventHotKeyPressed)
        )

        let handler: EventHandlerUPP = { _, _, _ -> OSStatus in
            Task { @MainActor in
                AppDelegate.shared?.togglePanel()
            }
            return noErr
        }

        InstallEventHandler(GetApplicationEventTarget(), handler, 1, &eventType, nil, &carbonEventHandler)

        let shortcuts: [(key: Int, mods: Int, id: UInt32)] = [
            // Hotkey 1: Command + Shift + Space (⌘ ⇧ Space)
            (Int(kVK_Space), Int(cmdKey | shiftKey), 1),
            // Hotkey 2: Command + Option + Space (⌘ ⌥ Space)
            (Int(kVK_Space), Int(cmdKey | optionKey), 2),
            // Hotkey 3: Command + K (⌘ K)
            (Int(kVK_ANSI_K), Int(cmdKey), 3),
            // Hotkey 4: Command + Shift + K (⌘ ⇧ K)
            (Int(kVK_ANSI_K), Int(cmdKey | shiftKey), 4),
            // Hotkey 5: Command + Escape (⌘ Esc)
            (Int(kVK_Escape), Int(cmdKey), 5),
            // Hotkey 6: Option + Space (⌥ Space)
            (Int(kVK_Space), Int(optionKey), 6),
        ]

        for sc in shortcuts {
            var ref: EventHotKeyRef?
            let hotKeyID = EventHotKeyID(signature: OSType(0x434C494F), id: sc.id)
            let err = RegisterEventHotKey(
                UInt32(sc.key),
                UInt32(sc.mods),
                hotKeyID,
                GetApplicationEventTarget(),
                0,
                &ref
            )
            if err == noErr, let r = ref {
                carbonHotKeys.append(r)
            }
        }

        // 2. Global NSEvent monitor backup (when Accessibility is granted)
        globalKeyMonitor = NSEvent.addGlobalMonitorForEvents(matching: .keyDown) { [weak self] event in
            let mods = event.modifierFlags
            let isCmd = mods.contains(.command)
            let isOpt = mods.contains(.option)
            let isShift = mods.contains(.shift)

            // Command + Shift + Space, Command + Option + Space, Option + Space
            if event.keyCode == 49 && ((isCmd && isShift) || (isCmd && isOpt) || isOpt) {
                Task { @MainActor in self?.togglePanel() }
            }
            // Command + K or Command + Shift + K
            else if event.keyCode == 40 && isCmd {
                Task { @MainActor in self?.togglePanel() }
            }
            // Command + Escape
            else if event.keyCode == 53 && isCmd {
                Task { @MainActor in self?.togglePanel() }
            }
        }

        // 3. Local monitor so hotkeys work when Clio itself is focused
        localKeyMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            let mods = event.modifierFlags
            let isCmd = mods.contains(.command)
            let isShift = mods.contains(.shift)
            let isOpt = mods.contains(.option)

            if event.keyCode == 49 && ((isCmd && isShift) || (isCmd && isOpt) || isOpt) {
                self?.togglePanel()
                return nil
            } else if (event.keyCode == 40 || event.keyCode == 53) && isCmd {
                self?.togglePanel()
                return nil
            }
            return event
        }
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showPanel()
        return true
    }

    func applicationWillTerminate(_ notification: Notification) {
        for hk in carbonHotKeys {
            UnregisterEventHotKey(hk)
        }
        carbonHotKeys.removeAll()
        if let h = carbonEventHandler { RemoveEventHandler(h) }
        if let monitor = globalKeyMonitor { NSEvent.removeMonitor(monitor) }
        if let monitor = localKeyMonitor { NSEvent.removeMonitor(monitor) }
        ServerLauncher.shared.terminate()
    }
}

// MARK: - Main Entry Point

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
