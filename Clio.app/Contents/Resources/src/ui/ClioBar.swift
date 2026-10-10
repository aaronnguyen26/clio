import AppKit
import ApplicationServices
import AVFoundation
import AVKit
import Carbon
import Combine
import Foundation
import ScreenCaptureKit
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
        let tokenFile = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".clio/session_token")
        if let data = try? Data(contentsOf: tokenFile),
           let token = String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines),
           !token.isEmpty {
            request.setValue(token, forHTTPHeaderField: "X-Clio-Token")
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        request.setValue("ClioBar", forHTTPHeaderField: "X-Clio-Client")
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

        // 2. Candidate paths (prioritize bundled resources and active workspace folder)
        let candidates = [
            Bundle.main.bundleURL.appendingPathComponent("Contents/Resources").path,
            Bundle.main.bundleURL.deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().path,
            Bundle.main.bundleURL.deletingLastPathComponent().path,
            Bundle.main.bundleURL.deletingLastPathComponent().deletingLastPathComponent().path,
            fm.currentDirectoryPath,
            Bundle.main.resourceURL?.path ?? "",
        ]

        for path in candidates {
            if path.isEmpty { continue }
            let marker = (path as NSString).appendingPathComponent("src/main.py")
            if fm.fileExists(atPath: marker) {
                return path
            }
        }
        return Bundle.main.bundleURL.appendingPathComponent("Contents/Resources").path
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
            proc.arguments = ["python3", "-B", "-m", "src.main", "--server", "--port", "8765"]
        } else {
            proc.executableURL = URL(fileURLWithPath: pyExe)
            proc.arguments = ["-B", "-m", "src.main", "--server", "--port", "8765"]
        }

        proc.currentDirectoryURL = URL(fileURLWithPath: projectDir)

        var env = ProcessInfo.processInfo.environment
        env["PATH"] = "/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        env["PYTHONPATH"] = projectDir
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["CLIO_PROJECT_DIR"] = projectDir
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

    func toggleDictation(
        onRecognized: @escaping (String) -> Void,
        onFinished: @escaping (String) -> Void,
        onError: (() -> Void)? = nil
    ) {
        if isListening {
            stopListening(onFinished: onFinished)
        } else {
            startListening(onRecognized: onRecognized, onFinished: onFinished, onError: onError)
        }
    }

    func startListening(
        onRecognized: @escaping (String) -> Void,
        onFinished: @escaping (String) -> Void,
        onError: (() -> Void)? = nil
    ) {
        cleanup()
        isListening = true
        recognizedText = ""

        SFSpeechRecognizer.requestAuthorization { [weak self] authStatus in
            Task { @MainActor in
                guard let self = self else { return }
                guard authStatus == .authorized else {
                    self.isListening = false
                    onError?()
                    return
                }
                do {
                    try self.startRecordingSession(onRecognized: onRecognized, onFinished: onFinished, onError: onError)
                } catch {
                    self.cleanup()
                    self.isListening = false
                    onError?()
                }
            }
        }
    }

    private func startRecordingSession(
        onRecognized: @escaping (String) -> Void,
        onFinished: @escaping (String) -> Void,
        onError: (() -> Void)? = nil
    ) throws {
        cleanup()

        let engine = AVAudioEngine()
        self.audioEngine = engine

        let request = SFSpeechAudioBufferRecognitionRequest()
        request.shouldReportPartialResults = true
        self.recognitionRequest = request

        guard let recognizer = speechRecognizer, recognizer.isAvailable else {
            cleanup()
            isListening = false
            onError?()
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
                    onError?()
                }
            }
        }
    }

    func stopListening(onFinished: @escaping (String) -> Void) {
        let text = recognizedText.trimmingCharacters(in: .whitespacesAndNewlines)
        cleanup()
        isListening = false
        if !text.isEmpty {
            onFinished(text)
        }
    }

    private func cleanup() {
        if let engine = audioEngine, engine.isRunning {
            engine.stop()
            engine.inputNode.removeTap(onBus: 0)
        }
        recognitionRequest?.endAudio()
        recognitionTask?.cancel()
        recognitionTask = nil
        recognitionRequest = nil
        audioEngine = nil
    }
}

// MARK: - Voice Dictation Animated Waveform Audio Bars

struct WaveformBarsView: View {
    @State private var animating: Bool = false

    var body: some View {
        HStack(spacing: 2) {
            ForEach(0..<4) { index in
                RoundedRectangle(cornerRadius: 1)
                    .fill(ObsidianTheme.surface)
                    .frame(width: 2, height: animating ? CGFloat([9, 13, 7, 11][index]) : CGFloat([3, 5, 4, 3][index]))
                    .animation(
                        Animation.easeInOut(duration: 0.45)
                            .repeatForever(autoreverses: true)
                            .delay(Double(index) * 0.12),
                        value: animating
                    )
            }
        }
        .frame(height: 13)
        .onAppear {
            animating = true
        }
    }
}


// MARK: - Models

struct WorkflowStepItem: Identifiable, Decodable {
    var id: String { step_id ?? "step_\(order ?? 0)" }
    let step_id: String?
    let order: Int?
    let description: String?
    let action: String?

    enum CodingKeys: String, CodingKey {
        case step_id
        case order
        case description
        case action
    }

    init(
        step_id: String? = nil,
        id: String? = nil,
        order: Int? = nil,
        step_index: Int? = nil,
        instruction: String? = nil,
        action: String? = nil,
        app: String? = nil,
        target_role: String? = nil,
        description: String? = nil
    ) {
        self.step_id = step_id ?? id
        self.order = order ?? step_index
        self.description = description ?? instruction
        self.action = action
    }

    var displayOrder: Int { order ?? 1 }
    var displayDescription: String {
        if let d = description, !d.isEmpty {
            var sanitized = d
            sanitized = sanitized.replacingOccurrences(of: #"\s*at\s*\(\d+[\.,]?\d*,\s*\d+[\.,]?\d*\)"#, with: "", options: .regularExpression)
            sanitized = sanitized.replacingOccurrences(of: #"\s*from\s*\(\d+[\.,]?\d*,\s*\d+[\.,]?\d*\)\s*to\s*\(\d+[\.,]?\d*,\s*\d+[\.,]?\d*\)"#, with: "", options: .regularExpression)
            sanitized = sanitized.replacingOccurrences(of: #"\s*to\s*\(\d+[\.,]?\d*,\s*\d+[\.,]?\d*\)"#, with: "", options: .regularExpression)
            sanitized = sanitized.replacingOccurrences(of: #"\s*left button"#, with: "", options: .regularExpression)
            sanitized = sanitized.replacingOccurrences(of: #"\s*right button"#, with: " right-click", options: .regularExpression)
            sanitized = sanitized.trimmingCharacters(in: .whitespacesAndNewlines)
            if sanitized.lowercased() == "click" {
                return "Click target"
            }
            if !sanitized.isEmpty { return sanitized }
            return d
        }
        let act = action ?? "action"
        return act.replacingOccurrences(of: "_", with: " ").capitalized
    }
    var actionIcon: String {
        switch action {
        case "click": return "hand.point.up.left.fill"
        case "double_click": return "hand.tap.fill"
        case "right_click": return "contextualmenu.and.cursor"
        case "drag": return "arrow.up.and.down.and.arrow.left.and.right"
        case "type_text": return "character.cursor.ibeam"
        case "key_combo", "press_key": return "keyboard"
        case "focus_app", "launch_app": return "app.fill"
        case "wait": return "clock"
        case "scroll": return "arrow.up.and.down"
        case "move_mouse", "move": return "cursorarrow.motionlines"
        default: return "circle.fill"
        }
    }
    var actionBadge: String {
        switch action {
        case "click": return "CLICK"
        case "double_click": return "DBL-CLICK"
        case "right_click": return "R-CLICK"
        case "drag": return "DRAG"
        case "type_text": return "TYPE"
        case "key_combo", "press_key": return "KEY"
        case "focus_app", "launch_app": return "FOCUS"
        case "wait": return "WAIT"
        case "scroll": return "SCROLL"
        case "move_mouse", "move": return "MOVE"
        default: return action?.uppercased() ?? "STEP"
        }
    }
}

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
    let steps: [WorkflowStepItem]?

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
        case steps
    }

    var rawId: String? { id_field }
    var displayName: String { name }
    var displayDesc: String { description ?? "" }
    var displaySteps: Int { steps?.count ?? step_count ?? 0 }
    var matchScore: Int { Int((confidence ?? 1.0) * 100) }
    var orderedSteps: [WorkflowStepItem] {
        guard let s = steps else { return [] }
        return s.sorted(by: { $0.displayOrder < $1.displayOrder })
    }

    init(
        workflow_id: String? = nil,
        id_field: String? = nil,
        name: String,
        description: String? = nil,
        confidence: Double? = nil,
        step_count: Int? = 1,
        canonical_trigger: String? = nil,
        video_path: String? = nil,
        recording_score: Double? = nil,
        recording_grade: String? = nil,
        steps: [WorkflowStepItem]? = nil
    ) {
        self.workflow_id = workflow_id
        self.id_field = id_field
        self.name = name
        self.description = description
        self.confidence = confidence
        self.step_count = step_count
        self.canonical_trigger = canonical_trigger
        self.video_path = video_path
        self.recording_score = recording_score
        self.recording_grade = recording_grade
        self.steps = steps
    }
}

// MARK: - Slash Command Shortcuts Registry & Catalog

struct SlashShortcutItem: Identifiable, Equatable {
    let id: String
    let command: String
    let title: String
    let description: String
    let iconName: String
    let badge: String
    let shortcutKey: String?
}

struct SlashShortcutCatalog {
    // Extensible catalog of slash command shortcuts for the Clio automation bar.
    // Adding any new shortcut here automatically exposes it in the '/' falldown dropdown.
    static let shortcuts: [SlashShortcutItem] = [
        SlashShortcutItem(
            id: "shortcut_memory",
            command: "/memory",
            title: "Memory Space",
            description: "Inspect saved automations, neural memory, and demonstrated actions",
            iconName: "brain.head.profile",
            badge: "ACTION ↵",
            shortcutKey: "⌘1"
        ),
        SlashShortcutItem(
            id: "shortcut_record",
            command: "/record",
            title: "Record Demonstration",
            description: "Capture screen demonstration and teach Clio a new automation",
            iconName: "record.circle",
            badge: "ACTION ↵",
            shortcutKey: "⌘2"
        ),
        SlashShortcutItem(
            id: "shortcut_teach",
            command: "/teach",
            title: "Teach Me (Walkthrough)",
            description: "Switch to interactive guided walkthrough mode",
            iconName: "graduationcap",
            badge: "ACTION ↵",
            shortcutKey: "⌘3"
        ),
        SlashShortcutItem(
            id: "shortcut_recent",
            command: "/recent",
            title: "Recent Automations",
            description: "View and immediately re-run recently used automations",
            iconName: "clock.arrow.circlepath",
            badge: "RUN ↵",
            shortcutKey: "⌘4"
        ),
        SlashShortcutItem(
            id: "shortcut_stop",
            command: "/stop",
            title: "Emergency Stop / Halt",
            description: "Immediately halt running automations, virtual cursor, and walkthroughs",
            iconName: "stop.circle.fill",
            badge: "HALT ↵",
            shortcutKey: "⌘5"
        ),
        SlashShortcutItem(
            id: "shortcut_status",
            command: "/status",
            title: "System Health & Permissions",
            description: "Check engine, AI connection, and macOS accessibility permissions",
            iconName: "heart.text.square",
            badge: "DIAGNOSE ↵",
            shortcutKey: "⌘6"
        ),
        SlashShortcutItem(
            id: "shortcut_ai",
            command: "/ai",
            title: "Ask AI Companion",
            description: "Ask a question or query Clio reasoning engine without automating",
            iconName: "sparkles",
            badge: "PROMPT ↵",
            shortcutKey: "⌘7"
        ),
        SlashShortcutItem(
            id: "shortcut_help",
            command: "/help",
            title: "Help & Documentation",
            description: "Open system help guides and keyboard shortcuts",
            iconName: "questionmark.circle",
            badge: "GUIDE ↵",
            shortcutKey: "⌘8"
        ),
        SlashShortcutItem(
            id: "shortcut_limits",
            command: "/limits",
            title: "Usage Limits & API Quota",
            description: "View and configure Walkthrough, Automation, and Gemini Free API key limits",
            iconName: "speedometer",
            badge: "QUOTA ↵",
            shortcutKey: "⌘9"
        ),
        SlashShortcutItem(
            id: "shortcut_settings",
            command: "/settings",
            title: "Settings & Preferences",
            description: "Configure Usage Limits, AI Models, Walkthrough, and Safety",
            iconName: "gearshape",
            badge: "CONFIG ↵",
            shortcutKey: "⌘,"
        ),
    ]

    static func matchingShortcuts(for query: String) -> [SlashShortcutItem] {
        let q = query.trimmingCharacters(in: .whitespaces).lowercased()
        guard q.hasPrefix("/") else { return [] }
        if q == "/" {
            return shortcuts
        }
        let clean = q
        let queryWithoutSlash = String(clean.dropFirst()).trimmingCharacters(in: .whitespaces)

        return shortcuts.filter { item in
            let cmd = item.command.lowercased()
            let title = item.title.lowercased()
            let desc = item.description.lowercased()

            if cmd.hasPrefix(clean) { return true }
            if item.id == "shortcut_limits" && ("/quota".hasPrefix(clean) || "/usage".hasPrefix(clean)) {
                return true
            }
            if item.id == "shortcut_settings" && ("/preferences".hasPrefix(clean) || "/config".hasPrefix(clean) || "/options".hasPrefix(clean)) {
                return true
            }
            if queryWithoutSlash.isEmpty { return true }

            if queryWithoutSlash.count >= 3 {
                let titleTokens = title.split(separator: " ").map { String($0) }
                if titleTokens.contains(where: { $0.hasPrefix(queryWithoutSlash) }) ||
                   title.contains(queryWithoutSlash) ||
                   desc.contains(queryWithoutSlash) {
                    return true
                }
            }

            return false
        }
    }
}

// MARK: - Curated System Recommendations Catalog

struct SystemRecommendationCatalog {
    static let items: [WorkflowItem] = [
        WorkflowItem(
            workflow_id: "sys_home",
            name: "Open Home Folder",
            description: "Finder • ~/",
            confidence: 0.99,
            step_count: 1,
            canonical_trigger: "open home folder",
            steps: [
                WorkflowStepItem(
                    id: "step_home_1",
                    step_index: 1,
                    instruction: "Open user home folder in Finder",
                    action: "launch_app",
                    app: "Finder",
                    target_role: "AXApplication",
                    description: "Reveal user directory ~/"
                )
            ]
        ),
        WorkflowItem(
            workflow_id: "sys_help",
            name: "Help & Documentation",
            description: "System Help • ⌘?",
            confidence: 0.99,
            step_count: 1,
            canonical_trigger: "help and documentation",
            steps: [
                WorkflowStepItem(
                    id: "step_help_1",
                    step_index: 1,
                    instruction: "Open macOS Help Documentation",
                    action: "launch_app",
                    app: "HelpViewer",
                    target_role: "AXApplication",
                    description: "Display Mac user guide & shortcuts"
                )
            ]
        ),
        WorkflowItem(
            workflow_id: "sys_hotcorners",
            name: "Hot Corners Settings",
            description: "Desktop & Dock preferences",
            confidence: 0.99,
            step_count: 1,
            canonical_trigger: "hot corners settings",
            steps: [
                WorkflowStepItem(
                    id: "step_hotcorners_1",
                    step_index: 1,
                    instruction: "Open Hot Corners configuration in System Settings",
                    action: "launch_app",
                    app: "System Settings",
                    target_role: "AXApplication",
                    description: "Configure display corner gestures"
                )
            ]
        ),
        WorkflowItem(
            workflow_id: "sys_hide_others",
            name: "Hide Other Applications",
            description: "Window Management • ⌥⌘H",
            confidence: 0.98,
            step_count: 1,
            canonical_trigger: "hide other applications",
            steps: [
                WorkflowStepItem(
                    id: "step_hide_1",
                    step_index: 1,
                    instruction: "Hide background windows",
                    action: "key_combo",
                    app: "Finder",
                    target_role: "AXApplication",
                    description: "Focus exclusively on active window"
                )
            ]
        ),
        WorkflowItem(
            workflow_id: "sys_photos",
            name: "Open Photos",
            description: "Apple Photos Library",
            confidence: 0.99,
            step_count: 1,
            canonical_trigger: "open photos",
            steps: [
                WorkflowStepItem(
                    id: "step_photos_1",
                    step_index: 1,
                    instruction: "Launch Photos application",
                    action: "launch_app",
                    app: "Photos",
                    target_role: "AXApplication",
                    description: "Browse photo albums and memories"
                )
            ]
        ),
        WorkflowItem(
            workflow_id: "sys_podcasts",
            name: "Open Podcasts",
            description: "Apple Podcasts",
            confidence: 0.99,
            step_count: 1,
            canonical_trigger: "open podcasts",
            steps: [
                WorkflowStepItem(
                    id: "step_podcasts_1",
                    step_index: 1,
                    instruction: "Launch Podcasts application",
                    action: "launch_app",
                    app: "Podcasts",
                    target_role: "AXApplication",
                    description: "Listen to audio shows and episodes"
                )
            ]
        ),
        WorkflowItem(
            workflow_id: "sys_preview",
            name: "Open Preview",
            description: "PDF & Image Viewer",
            confidence: 0.99,
            step_count: 1,
            canonical_trigger: "open preview",
            steps: [
                WorkflowStepItem(
                    id: "step_preview_1",
                    step_index: 1,
                    instruction: "Launch Preview application",
                    action: "launch_app",
                    app: "Preview",
                    target_role: "AXApplication",
                    description: "View and annotate documents"
                )
            ]
        ),
        WorkflowItem(
            workflow_id: "sys_settings",
            name: "Open System Settings",
            description: "macOS Preferences & Controls",
            confidence: 0.99,
            step_count: 1,
            canonical_trigger: "open system settings",
            steps: [
                WorkflowStepItem(
                    id: "step_settings_1",
                    step_index: 1,
                    instruction: "Open macOS System Settings",
                    action: "launch_app",
                    app: "System Settings",
                    target_role: "AXApplication",
                    description: "Manage system preferences"
                )
            ]
        ),
        WorkflowItem(
            workflow_id: "sys_calculator",
            name: "Open Calculator",
            description: "macOS Calculator",
            confidence: 0.99,
            step_count: 1,
            canonical_trigger: "open calculator",
            steps: [
                WorkflowStepItem(
                    id: "step_calc_1",
                    step_index: 1,
                    instruction: "Launch Calculator application",
                    action: "launch_app",
                    app: "Calculator",
                    target_role: "AXApplication",
                    description: "Standard, scientific, and programmer math"
                )
            ]
        ),
        WorkflowItem(
            workflow_id: "sys_calendar",
            name: "Open Calendar",
            description: "Apple Calendar",
            confidence: 0.99,
            step_count: 1,
            canonical_trigger: "open calendar",
            steps: [
                WorkflowStepItem(
                    id: "step_cal_1",
                    step_index: 1,
                    instruction: "Launch Calendar application",
                    action: "launch_app",
                    app: "Calendar",
                    target_role: "AXApplication",
                    description: "Manage schedule and meetings"
                )
            ]
        ),
        WorkflowItem(
            workflow_id: "sys_safari",
            name: "Open Safari",
            description: "Web Browser",
            confidence: 0.99,
            step_count: 1,
            canonical_trigger: "open safari",
            steps: [
                WorkflowStepItem(
                    id: "step_safari_1",
                    step_index: 1,
                    instruction: "Launch Safari browser",
                    action: "launch_app",
                    app: "Safari",
                    target_role: "AXApplication",
                    description: "Browse the web"
                )
            ]
        ),
        WorkflowItem(
            workflow_id: "sys_terminal",
            name: "Open Terminal",
            description: "Command Line Shell",
            confidence: 0.99,
            step_count: 1,
            canonical_trigger: "open terminal",
            steps: [
                WorkflowStepItem(
                    id: "step_term_1",
                    step_index: 1,
                    instruction: "Launch Terminal application",
                    action: "launch_app",
                    app: "Terminal",
                    target_role: "AXApplication",
                    description: "Execute zsh shell commands"
                )
            ]
        ),
        WorkflowItem(
            workflow_id: "sys_textedit",
            name: "Open TextEdit",
            description: "Rich Text & Plain Text Editor",
            confidence: 0.99,
            step_count: 1,
            canonical_trigger: "open textedit",
            steps: [
                WorkflowStepItem(
                    id: "step_textedit_1",
                    step_index: 1,
                    instruction: "Launch TextEdit application",
                    action: "launch_app",
                    app: "TextEdit",
                    target_role: "AXApplication",
                    description: "Edit text notes and documents"
                )
            ]
        ),
        WorkflowItem(
            workflow_id: "sys_notes",
            name: "Open Notes",
            description: "Apple Notes",
            confidence: 0.99,
            step_count: 1,
            canonical_trigger: "open notes",
            steps: [
                WorkflowStepItem(
                    id: "step_notes_1",
                    step_index: 1,
                    instruction: "Launch Notes application",
                    action: "launch_app",
                    app: "Notes",
                    target_role: "AXApplication",
                    description: "Capture thoughts, checklists, and documents"
                )
            ]
        ),
        WorkflowItem(
            workflow_id: "sys_messages",
            name: "Open Messages",
            description: "iMessage & SMS",
            confidence: 0.99,
            step_count: 1,
            canonical_trigger: "open messages",
            steps: [
                WorkflowStepItem(
                    id: "step_msg_1",
                    step_index: 1,
                    instruction: "Launch Messages application",
                    action: "launch_app",
                    app: "Messages",
                    target_role: "AXApplication",
                    description: "Chat and send messages"
                )
            ]
        ),
        WorkflowItem(
            workflow_id: "sys_mail",
            name: "Open Mail",
            description: "Apple Mail Client",
            confidence: 0.99,
            step_count: 1,
            canonical_trigger: "open mail",
            steps: [
                WorkflowStepItem(
                    id: "step_mail_1",
                    step_index: 1,
                    instruction: "Launch Mail application",
                    action: "launch_app",
                    app: "Mail",
                    target_role: "AXApplication",
                    description: "Read and compose emails"
                )
            ]
        ),
        WorkflowItem(
            workflow_id: "sys_music",
            name: "Open Music",
            description: "Apple Music",
            confidence: 0.99,
            step_count: 1,
            canonical_trigger: "open music",
            steps: [
                WorkflowStepItem(
                    id: "step_music_1",
                    step_index: 1,
                    instruction: "Launch Music application",
                    action: "launch_app",
                    app: "Music",
                    target_role: "AXApplication",
                    description: "Play tracks and playlists"
                )
            ]
        ),
        WorkflowItem(
            workflow_id: "sys_finder",
            name: "Open Finder",
            description: "macOS File Manager",
            confidence: 0.99,
            step_count: 1,
            canonical_trigger: "open finder",
            steps: [
                WorkflowStepItem(
                    id: "step_finder_1",
                    step_index: 1,
                    instruction: "Bring Finder to front",
                    action: "launch_app",
                    app: "Finder",
                    target_role: "AXApplication",
                    description: "Browse files and folders"
                )
            ]
        ),
        WorkflowItem(
            workflow_id: "sys_activity",
            name: "Open Activity Monitor",
            description: "CPU & Memory Performance",
            confidence: 0.99,
            step_count: 1,
            canonical_trigger: "open activity monitor",
            steps: [
                WorkflowStepItem(
                    id: "step_act_1",
                    step_index: 1,
                    instruction: "Launch Activity Monitor",
                    action: "launch_app",
                    app: "Activity Monitor",
                    target_role: "AXApplication",
                    description: "Inspect running processes and memory"
                )
            ]
        )
    ]
}


// MARK: - Swift-Native Screen Recorder (Apple ScreenCaptureKit + SCRecordingOutput)
//
// Modern ScreenCaptureKit pipeline:
//   1. Uses Apple's modern ScreenCaptureKit (SCStream + SCContentFilter) to record the primary display.
//   2. Direct-to-container hardware recording via SCRecordingOutput on macOS 15+ Sequoia (same architecture as QuickTime / screencaptureui).
//   3. Captures the ENTIRE desktop, including all application windows, context switches, window movement, menus, and overlays.
//   4. Runs completely inside the authorized Clio.app bundle process, eliminating repeated TCC privacy notifications.
//   5. Fallback to AVAssetWriter with hardware H.264 encoding for older macOS releases (< 15.0).
final class SwiftScreenRecorder: NSObject, @unchecked Sendable, SCStreamOutput, SCStreamDelegate {
    static let shared = SwiftScreenRecorder()

    private var stream: SCStream?
    private var recordingOutput: AnyObject?
    private var assetWriter: AVAssetWriter?
    private var videoInput: AVAssetWriterInput?
    private var isRecording = false
    private var sessionStarted = false
    private var frameCount = 0
    private var lastPTS: CMTime = .invalid
    private var startCompletion: ((URL?, Error?) -> Void)?
    private var stopCompletion: ((URL?, Error?) -> Void)?
    private var currentOutputURL: URL?
    private let recordingQueue = DispatchQueue(label: "com.clio.ScreenRecorderQueue", qos: .userInitiated)

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

    /// Starts recording the full primary display via Apple ScreenCaptureKit.
    /// Captures all application windows, context switches, menus, opening apps, and cursor movement across windows.
    @discardableResult
    func startRecording(completion: ((URL?, Error?) -> Void)? = nil) -> URL? {
        stopExistingSession()

        // Output destination: <project_dir>/recordings/<session_id>/recording.mov
        let projectDir = ServerLauncher.shared.resolveProjectDirectory()
        let sessionID = String(UUID().uuidString.prefix(8))
        let recordingsDir = URL(fileURLWithPath: projectDir)
            .appendingPathComponent("recordings/\(sessionID)")
        try? FileManager.default.createDirectory(at: recordingsDir, withIntermediateDirectories: true)
        let outputURL = recordingsDir.appendingPathComponent("recording.mov")

        self.currentOutputURL = outputURL
        self.isRecording = true
        self.sessionStarted = false
        self.frameCount = 0
        self.lastPTS = .invalid
        self.startCompletion = completion

        Task {
            // Wait for screen capture permission before proceeding.
            // CGRequestScreenCaptureAccess() is fire-and-forget: it opens System Preferences
            // but returns immediately. We must poll until the user actually grants access,
            // otherwise SCShareableContent will throw error -3801 (TCC denied).
            let hasPermission = await self.waitForScreenCapturePermission(timeout: 60.0)
            guard hasPermission else {
                print("SwiftScreenRecorder: Screen capture permission was not granted. Please enable Clio in System Settings > Privacy & Security > Screen Recording.")
                self.notifyStartFailed(error: NSError(domain: "SwiftScreenRecorder", code: -3801, userInfo: [NSLocalizedDescriptionKey: "Screen Recording permission denied. Open System Settings > Privacy & Security > Screen Recording and enable Clio."]))
                return
            }

            do {
                let shareable = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
                guard let display = shareable.displays.first(where: { $0.displayID == CGMainDisplayID() }) ?? shareable.displays.first else {
                    print("SwiftScreenRecorder: No display available for recording")
                    self.notifyStartFailed(error: NSError(domain: "SwiftScreenRecorder", code: -1, userInfo: [NSLocalizedDescriptionKey: "No display available"]))
                    return
                }

                // Canonical whole-display capture: captures all on-screen application windows, menubar, dock, and newly opening apps.
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
                config.minimumFrameInterval = CMTime(value: 1, timescale: 30)
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
                    // Modern macOS 15 Sequoia native hardware recorder:
                    // Direct-to-container recording via SCRecordingOutput (same architecture as QuickTime / screencaptureui).
                    // Captures all windows, full Retina resolution, zero dropped buffers, and no AVAssetWriter errors.
                    let recConfig = SCRecordingOutputConfiguration()
                    recConfig.outputURL = outputURL
                    recConfig.outputFileType = .mov
                    recConfig.videoCodecType = .h264
                    let recOutput = SCRecordingOutput(configuration: recConfig, delegate: self)
                    try newStream.addRecordingOutput(recOutput)
                    self.recordingOutput = recOutput
                    print("SwiftScreenRecorder: Using macOS 15 native SCRecordingOutput pipeline")
                } else {
                    // Fallback for macOS 14/13 using AVAssetWriter
                    let writer = try AVAssetWriter(outputURL: outputURL, fileType: .mov)
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
                    let input = AVAssetWriterInput(mediaType: .video, outputSettings: videoSettings)
                    input.expectsMediaDataInRealTime = true

                    guard writer.canAdd(input) else {
                        print("SwiftScreenRecorder: AVAssetWriter cannot add video input")
                        self.notifyStartFailed(error: NSError(domain: "SwiftScreenRecorder", code: -2, userInfo: [NSLocalizedDescriptionKey: "Cannot add AVAssetWriterInput"]))
                        return
                    }
                    writer.add(input)

                    self.assetWriter = writer
                    self.videoInput = input

                    writer.startWriting()
                    try newStream.addStreamOutput(self, type: .screen, sampleHandlerQueue: self.recordingQueue)
                }

                try await newStream.startCapture()
                self.stream = newStream
                print("SwiftScreenRecorder: ScreenCaptureKit recording stream running on display \(display.displayID)")

                if #unavailable(macOS 15.0) {
                    self.notifyStartSucceeded()
                }
            } catch {
                print("SwiftScreenRecorder: Failed to start ScreenCaptureKit: \(error)")
                self.notifyStartFailed(error: error)
            }
        }

        return outputURL
    }

    private func notifyStartSucceeded() {
        guard let completion = self.startCompletion else { return }
        self.startCompletion = nil
        let targetURL = self.currentOutputURL
        DispatchQueue.main.async {
            completion(targetURL, nil)
        }
    }

    private func notifyStartFailed(error: Error) {
        guard let completion = self.startCompletion else { return }
        self.startCompletion = nil
        DispatchQueue.main.async {
            completion(nil, error)
        }
    }

    /// Stops the current recording session and delivers the finalized .mov URL via completion.
    func stopRecording(completion: @escaping (URL?, Error?) -> Void) {
        guard isRecording else {
            completion(nil, nil)
            return
        }
        self.isRecording = false
        self.stopCompletion = completion

        Task {
            if let stream = self.stream {
                try? await stream.stopCapture()
                self.stream = nil
            }

            // Safety timeout: Ensure completion is called within 3.0s if delegate doesn't fire
            DispatchQueue.global().asyncAfter(deadline: .now() + 3.0) { [weak self] in
                guard let self = self, self.stopCompletion != nil else { return }
                self.handleRecordingFinished(error: nil)
            }

            // On macOS < 15 fallback using AVAssetWriter:
            if self.assetWriter != nil {
                self.recordingQueue.async { [weak self] in
                    guard let self = self else { return }
                    self.videoInput?.markAsFinished()
                    if let writer = self.assetWriter, writer.status == .writing {
                        if !self.sessionStarted {
                            writer.startSession(atSourceTime: CMTime.zero)
                        }
                        writer.finishWriting { [weak self] in
                            guard let self = self else { return }
                            self.handleRecordingFinished(error: writer.error)
                        }
                    } else {
                        self.handleRecordingFinished(error: nil)
                    }
                }
            }
        }
    }

    func handleRecordingFinished(error: Error?) {
        guard let completion = self.stopCompletion else { return }
        self.stopCompletion = nil
        let targetURL = self.currentOutputURL

        let validURL: URL? = {
            guard let u = targetURL else { return nil }
            if let attrs = try? FileManager.default.attributesOfItem(atPath: u.path),
               let size = attrs[.size] as? Int64,
               size > 1024 {
                return u
            }
            return nil
        }()

        self.cleanup()
        DispatchQueue.main.async {
            completion(validURL, error)
        }
    }

    // MARK: - SCStreamOutput
    nonisolated func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .screen else { return }
        guard CMSampleBufferIsValid(sampleBuffer) else { return }
        guard CMSampleBufferDataIsReady(sampleBuffer) else { return }
        guard CMSampleBufferGetImageBuffer(sampleBuffer) != nil else { return }

        // Only append complete frames containing rendered window/screen content
        if let attachmentsArray = CMSampleBufferGetSampleAttachmentsArray(sampleBuffer, createIfNecessary: false) as? [[SCStreamFrameInfo: Any]],
           let attachments = attachmentsArray.first,
           let statusRaw = attachments[.status] as? Int,
           let status = SCFrameStatus(rawValue: statusRaw) {
            if status != .complete {
                return
            }
        }

        guard self.isRecording else { return }
        guard let writer = self.assetWriter, let input = self.videoInput else { return }

        let pts = CMSampleBufferGetPresentationTimeStamp(sampleBuffer)
        guard pts.isValid else { return }

        // Guarantee strictly monotonic presentation timestamps for AVAssetWriter
        if self.lastPTS.isValid && pts <= self.lastPTS {
            return
        }

        if !self.sessionStarted && writer.status == .writing {
            writer.startSession(atSourceTime: pts)
            self.sessionStarted = true
        }

        if writer.status == .writing && input.isReadyForMoreMediaData {
            if input.append(sampleBuffer) {
                self.lastPTS = pts
                self.frameCount += 1
            }
        }
    }

    // MARK: - SCStreamDelegate
    nonisolated func stream(_ stream: SCStream, didStopWithError error: Error) {
        print("SwiftScreenRecorder: SCStream stopped with error: \(error)")
    }

    private func cleanup() {
        startCompletion = nil
        stopCompletion = nil
        stream = nil
        recordingOutput = nil
        assetWriter = nil
        videoInput = nil
        currentOutputURL = nil
        sessionStarted = false
        frameCount = 0
        lastPTS = .invalid
    }

    private func stopExistingSession() {
        isRecording = false
        if let stream = stream {
            Task { try? await stream.stopCapture() }
        }
        if let writer = assetWriter, writer.status == .writing {
            videoInput?.markAsFinished()
            writer.cancelWriting()
        }
        cleanup()
    }
}

@available(macOS 15.0, *)
extension SwiftScreenRecorder: SCRecordingOutputDelegate {
    nonisolated func recordingOutputDidStartRecording(_ recordingOutput: SCRecordingOutput) {
        print("SwiftScreenRecorder: SCRecordingOutput started recording.")
        Task { @MainActor in
            SwiftScreenRecorder.shared.notifyStartSucceeded()
        }
    }

    nonisolated func recordingOutput(_ recordingOutput: SCRecordingOutput, didFailWithError error: Error) {
        print("SwiftScreenRecorder: SCRecordingOutput failed with error: \(error)")
        Task { @MainActor in
            SwiftScreenRecorder.shared.notifyStartFailed(error: error)
            SwiftScreenRecorder.shared.handleRecordingFinished(error: error)
        }
    }

    nonisolated func recordingOutputDidFinishRecording(_ recordingOutput: SCRecordingOutput) {
        print("SwiftScreenRecorder: SCRecordingOutput finished recording.")
        Task { @MainActor in
            SwiftScreenRecorder.shared.handleRecordingFinished(error: nil)
        }
    }
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

    // Authentic Obsidian Monochrome Cursor Arrow
    private let arrowGradient = LinearGradient(
        colors: [
            Color(red: 0.18, green: 0.19, blue: 0.22),
            Color(red: 0.06, green: 0.06, blue: 0.08)
        ],
        startPoint: .topLeading,
        endPoint: .bottomTrailing
    )

    var body: some View {
        ZStack(alignment: .topLeading) {
            // Click wave ripple from arrow tip
            if manager.isClicking {
                Circle()
                    .stroke(Color.white.opacity(0.85), lineWidth: 2)
                    .frame(width: 32, height: 32)
                    .scaleEffect(1.5)
                    .position(x: 2, y: 2)
                    .animation(.easeOut(duration: 0.25), value: manager.isClicking)
            }

            // Authentic Mac Pointer Arrow in Obsidian Black
            ZStack {
                // Drop shadow/outer outline for crisp contrast on any background
                MacCursorArrowShape()
                    .stroke(Color.black, lineWidth: 2.5)
                    .frame(width: 17, height: 25)

                MacCursorArrowShape()
                    .fill(arrowGradient)
                    .frame(width: 17, height: 25)
                    .overlay(
                        MacCursorArrowShape()
                            .stroke(Color.white.opacity(0.85), lineWidth: 0.9)
                    )
                    .shadow(color: Color.black.opacity(0.65), radius: 4, x: 0, y: 1)
            }
            .scaleEffect(manager.isClicking ? 0.90 : 1.0)
            .animation(.easeInOut(duration: 0.1), value: manager.isClicking)
            .offset(x: 2, y: 2)

            // "Clio" Name Badge beside the pointer arrow
            HStack(spacing: 3) {
                Circle()
                    .fill(Color.white)
                    .frame(width: 4, height: 4)
                Text("Clio")
                    .font(.system(size: 9, weight: .bold, design: .rounded))
                    .foregroundColor(.white)
            }
            .padding(.horizontal, 6)
            .padding(.vertical, 2.5)
            .background(
                Capsule()
                    .fill(Color(red: 0.08, green: 0.08, blue: 0.10).opacity(0.95))
                    .overlay(
                        Capsule()
                            .stroke(Color.white.opacity(0.3), lineWidth: 1)
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
    private var clickResetWorkItem: DispatchWorkItem?
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
        panel.level = .screenSaver
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
        if x < 0 || y < 0 {
            hide()
            return
        }
        guard let screen = NSScreen.screens.first ?? NSScreen.main else { return }
        let screenH = screen.frame.height
        // In macOS coordinates: tip of arrow is at (2, 2) in panel
        let winX = x - 2
        let winY = screenH - y - 43

        self.currentX = x
        self.currentY = y
        self.currentState = state
        let upper = state.uppercased()
        let activeAction = (upper == "CLICKING" || upper == "PULSING" || upper == "DRAGGING" || upper == "HIGHLIGHTING" || upper == "DEMONSTRATING")

        if activeAction {
            self.isClicking = true
            clickResetWorkItem?.cancel()
            let workItem = DispatchWorkItem { [weak self] in
                self?.isClicking = false
            }
            self.clickResetWorkItem = workItem
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.35, execute: workItem)
        } else if clickResetWorkItem == nil {
            self.isClicking = false
        }

        window?.setFrameOrigin(NSPoint(x: winX, y: winY))
        if window?.isVisible == false {
            window?.orderFrontRegardless()
        }
    }

    func hide() {
        clickResetWorkItem?.cancel()
        clickResetWorkItem = nil
        window?.orderOut(nil)
        window?.setFrameOrigin(NSPoint(x: -200, y: -200))
        self.isClicking = false
        self.currentState = "IDLE"
    }
}

// MARK: - Walkthrough Tutorial Card & Spotlight Overlay

struct WalkthroughStepSummary: Identifiable, Equatable {
    let id: Int
    let stepIndex: Int
    let title: String
    let instruction: String
}

struct WalkthroughCalloutCardView: View {
    @ObservedObject var manager: WalkthroughOverlayManager

    var body: some View {
        VStack(spacing: 5) {
            // Header Row: Micro cap icon + "CLIO TUTOR", step badge, Spacer, dismiss 'x'
            HStack(spacing: 6) {
                HStack(spacing: 4) {
                    Image(systemName: "graduationcap.fill")
                        .font(.system(size: 10, weight: .semibold))
                        .foregroundColor(ObsidianTheme.platinum)
                    Text("CLIO TUTOR")
                        .font(.system(size: 8.5, weight: .bold, design: .monospaced))
                        .foregroundColor(ObsidianTheme.platinum)
                }

                Text(manager.stepBadge)
                    .font(.system(size: 7.5, weight: .semibold, design: .monospaced))
                    .foregroundColor(ObsidianTheme.platinum.opacity(0.8))
                    .padding(.horizontal, 5)
                    .padding(.vertical, 1.5)
                    .background(Capsule().fill(Color.white.opacity(0.08)))
                    .overlay(Capsule().stroke(Color.white.opacity(0.14), lineWidth: 0.7))

                Spacer()

                Button(action: { manager.stopWalkthrough() }) {
                    Image(systemName: "xmark")
                        .font(.system(size: 9, weight: .medium))
                        .foregroundColor(.white.opacity(0.4))
                }
                .buttonStyle(.plain)
                .help("Stop Walkthrough")
            }

            // Body instruction (compact, 1-2 lines)
            Text(manager.instruction)
                .font(.system(size: 11, weight: .medium, design: .rounded))
                .foregroundColor(.white)
                .lineLimit(2)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)

            Spacer(minLength: 0)

            // Ultra-thin 2px progress bar
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    Capsule()
                        .fill(Color.white.opacity(0.12))
                    Capsule()
                        .fill(
                            LinearGradient(
                                colors: [Color.white.opacity(0.7), Color.white],
                                startPoint: .leading,
                                endPoint: .trailing
                            )
                        )
                        .frame(width: max(4, geo.size.width * CGFloat(manager.currentStepIndex) / CGFloat(max(1, manager.totalSteps))))
                }
            }
            .frame(height: 2)
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
        .frame(width: 310, height: 72)
        .background(
            RoundedRectangle(cornerRadius: 10)
                .fill(Color(red: 0.08, green: 0.08, blue: 0.10).opacity(0.96))
                .overlay(
                    RoundedRectangle(cornerRadius: 10)
                        .stroke(Color.white.opacity(0.16), lineWidth: 1)
                )
        )
        .shadow(color: Color.black.opacity(0.5), radius: 12, x: 0, y: 4)
    }
}

@MainActor
final class WalkthroughOverlayManager: ObservableObject {
    static let shared = WalkthroughOverlayManager()
    private var window: NSPanel?
    private var statusPollTimer: Timer?

    @Published var isVisible: Bool = false
    @Published var goal: String = ""
    @Published var stepBadge: String = "STEP 1 OF 3"
    @Published var instruction: String = ""
    @Published var explanation: String = ""
    @Published var steps: [WalkthroughStepSummary] = []
    @Published var currentStepIndex: Int = 1
    @Published var totalSteps: Int = 1
    @Published var autoAdvance: Bool = true

    init() {
        setupOverlay()
    }

    private func setupOverlay() {
        let panel = NSPanel(
            contentRect: NSRect(x: -600, y: -600, width: 310, height: 72),
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
        panel.isMovableByWindowBackground = false
        panel.isMovable = false

        let view = WalkthroughCalloutCardView(manager: self)
        panel.contentView = NSHostingView(rootView: view)
        panel.orderOut(nil)
        self.window = panel
    }

    private func ensurePollingActive() {
        guard statusPollTimer == nil else { return }
        statusPollTimer = Timer.scheduledTimer(withTimeInterval: 0.15, repeats: true) { [weak self] _ in
            Task { @MainActor in
                self?.pollStatusOnce()
            }
        }
    }

    private func pollStatusOnce() {
        guard self.isVisible, let url = URL(string: "http://127.0.0.1:8765/api/walkthrough/status") else { return }
        Task {
            guard let (data, resp) = try? await URLSession.shared.data(from: url),
                  let http = resp as? HTTPURLResponse, http.statusCode == 200,
                  let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
            await MainActor.run {
                guard self.isVisible else { return }
                let status = (json["status"] as? String ?? "").uppercased()
                if status == "IDLE" { return }
                self.update(data: json)
                if let cx = json["cursor_x"] as? Double, let cy = json["cursor_y"] as? Double {
                    VirtualCursorOverlayManager.shared.updatePosition(x: CGFloat(cx), y: CGFloat(cy), state: status)
                }
            }
        }
    }

    func update(data: [String: Any]) {
        guard let screen = NSScreen.screens.first ?? NSScreen.main else { return }
        let screenRect = screen.visibleFrame

        if let rawSteps = data["steps"] as? [[String: Any]] {
            self.steps = rawSteps.compactMap { dict in
                let idx = dict["step_index"] as? Int ?? 0
                let title = dict["title"] as? String ?? "Step \(idx)"
                let inst = dict["instruction"] as? String ?? ""
                return WalkthroughStepSummary(id: idx, stepIndex: idx, title: title, instruction: inst)
            }
        }

        let total = max(1, data["total_steps"] as? Int ?? (self.steps.isEmpty ? 1 : self.steps.count))
        let rawIdx = data["current_step_index"] as? Int ?? 1
        let curIdx = max(1, min(rawIdx == 0 ? 1 : rawIdx, total))

        self.currentStepIndex = curIdx
        self.totalSteps = total
        self.stepBadge = "STEP \(curIdx) OF \(total)"
        self.autoAdvance = data["auto_advance"] as? Bool ?? true
        if let g = data["goal"] as? String, !g.isEmpty {
            self.goal = g
        }

        if let step = data["step"] as? [String: Any], let inst = step["instruction"] as? String, !inst.isEmpty {
            self.instruction = inst
            self.explanation = step["explanation"] as? String ?? ""
        } else if let matching = self.steps.first(where: { $0.stepIndex == curIdx }) {
            self.instruction = matching.instruction
        } else if let first = self.steps.first, self.instruction.isEmpty {
            self.instruction = first.instruction
        }

        let status = (data["status"] as? String ?? "").uppercased()

        if status == "COMPLETED" || status == "CANCELLED" || status == "IDLE" {
            hide()
            VirtualCursorOverlayManager.shared.hide()
            AppDelegate.shared?.showPanel()
            return
        }

        let cardW: CGFloat = 310
        let cardH: CGFloat = 72
        let padding: CGFloat = 20

        // Stationed firmly on the bottom-right corner of the screen
        let targetX = screenRect.origin.x + screenRect.width - cardW - padding
        let targetY = screenRect.origin.y + padding

        window?.setFrame(NSRect(x: targetX, y: targetY, width: cardW, height: cardH), display: true)
        if window?.isVisible == false {
            window?.orderFrontRegardless()
        }
        self.isVisible = true
        ensurePollingActive()
    }

    func hide() {
        statusPollTimer?.invalidate()
        statusPollTimer = nil
        window?.orderOut(nil)
        window?.setFrameOrigin(NSPoint(x: -600, y: -600))
        self.isVisible = false
    }

    func stopWalkthrough() {
        guard let url = URL(string: "http://127.0.0.1:8765/api/walkthrough/stop") else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: [:])
        Task {
            _ = try? await URLSession.shared.data(for: req)
            await MainActor.run {
                self.hide()
                VirtualCursorOverlayManager.shared.hide()
                AppDelegate.shared?.showPanel()
            }
        }
    }
}

// MARK: - Settings Tab Navigation

enum SettingsTab: String, CaseIterable, Identifiable {
    case usage = "Quota"
    case ai = "Models"
    case walkthrough = "Tutor"
    case safety = "Safety"
    case storage = "Storage"

    var id: String { rawValue }

    var icon: String {
        switch self {
        case .usage: return "chart.pie.fill"
        case .ai: return "sparkles"
        case .walkthrough: return "graduationcap.fill"
        case .safety: return "shield.fill"
        case .storage: return "internaldrive.fill"
        }
    }
}

// MARK: - View Model

@MainActor
final class ClioViewModel: ObservableObject {
    @Published var query: String = "" {
        didSet {
            let trimmed = query.trimmingCharacters(in: .whitespaces).lowercased()
            if !trimmed.hasPrefix("/memory") {
                self.isMemorySpaceOpen = false
            }
            if !trimmed.hasPrefix("/status") {
                self.isStatusPanelOpen = false
            }
            if !trimmed.hasPrefix("/help") {
                self.isHelpPanelOpen = false
            }
            if !trimmed.hasPrefix("/ai") {
                self.aiCompanionResponse = ""
                self.aiTriggeredWorkflow = nil
            }
            if !trimmed.isEmpty && !trimmed.hasPrefix("/settings") && !trimmed.hasPrefix("/preferences") && !trimmed.hasPrefix("/config") && !trimmed.hasPrefix("/limits") && !trimmed.hasPrefix("/quota") && !trimmed.hasPrefix("/usage") {
                self.isSettingsPanelOpen = false
                self.isUsageLimitPanelOpen = false
            }
            self.isRecommendationExplicitlySelected = false
            updateRecommendationsSynchronously(for: query)
        }
    }
    @Published var workflows: [WorkflowItem] = []
    @Published var recommendations: [WorkflowItem] = []
    @Published var selectedIndex: Int = 0
    @Published var isRecommendationExplicitlySelected: Bool = false
    @Published var isExecuting: Bool = false
    @Published var isRecording: Bool = false
    @Published var isListening: Bool = false
    @Published var isWalkthroughMode: Bool = false
    @Published var isBackgroundMode: Bool = false
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

    // Workflow Steps Inspection (show steps in order before taking action)
    @Published var inspectWorkflow: WorkflowItem? = nil

    // All saved workflows in persistent memory (preserved across search queries)
    @Published var allSavedWorkflows: [WorkflowItem] = []

    @Published var isMemorySpaceOpen: Bool = false
    @Published var isStatusPanelOpen: Bool = false
    @Published var isHelpPanelOpen: Bool = false
    @Published var isUsageLimitPanelOpen: Bool = false
    @Published var isSettingsPanelOpen: Bool = false
    @Published var selectedSettingsTab: SettingsTab = .usage

    // Settings Specific Preferences
    @Published var aiModelSelection: String = "gemini-2.5-flash"
    @Published var aiApiKeyInput: String = ""
    @Published var aiKeyObscured: Bool = true
    @Published var aiTestStatusText: String = ""
    @Published var isTestingAIKey: Bool = false
    @Published var walkthroughDefaultMode: String = "guided_demo"
    @Published var walkthroughAdvanceDelay: Double = 2.5
    @Published var virtualCursorSpeed: String = "smooth"
    @Published var audioChimesEnabled: Bool = true
    @Published var cornerFailsafeEnabled: Bool = true
    @Published var requireActionConfirmation: Bool = true
    @Published var storageMetrics: [String: Any] = [:]
    @Published var storageStatusSummary: String = "4 Workflows • 12.0MB Storage"
    @Published var isClearingStorage: Bool = false

    @Published var systemAccessibilityGranted: Bool = false
    @Published var aiProviderName: String = "Local Engine"
    @Published var aiProviderStatus: String = "Ready"

    // App Usage Limits & Gemini Free-Tier Quota Telemetry
    @Published var walkthroughUsed: Int = 0
    @Published var walkthroughDailyLimit: Int = 25
    @Published var walkthroughRemaining: Int = 25
    @Published var walkthroughRpmUsed: Int = 0
    @Published var walkthroughRpmLimit: Int = 5
    @Published var walkthroughCloudUsed: Int = 0
    @Published var walkthroughLocalUsed: Int = 0
    @Published var walkthroughExhausted: Bool = false

    @Published var automationUsed: Int = 0
    @Published var automationDailyLimit: Int = 25
    @Published var automationRemaining: Int = 25
    @Published var automationRpmUsed: Int = 0
    @Published var automationRpmLimit: Int = 5
    @Published var automationCloudUsed: Int = 0
    @Published var automationLocalUsed: Int = 0
    @Published var automationExhausted: Bool = false

    @Published var geminiActiveModel: String = "gemini-2.5-flash"
    @Published var geminiCallsToday: Int = 0
    @Published var geminiDailyLimit: Int = 250
    @Published var geminiRpmUsed: Int = 0
    @Published var geminiRpmLimit: Int = 15
    @Published var geminiRateLimited: Bool = false
    @Published var geminiCooldownSeconds: Int = 0
    @Published var usageResetFormatted: String = "24H 00M"
    @Published var usageLimitReachedBanner: String? = nil

    @Published var aiCompanionPrompt: String = ""
    @Published var aiCompanionResponse: String = ""
    @Published var isAILoading: Bool = false
    @Published var aiTriggeredWorkflow: String? = nil

    var matchingSlashCommands: [SlashShortcutItem] {
        let trimmed = query.trimmingCharacters(in: .whitespaces)
        guard trimmed.hasPrefix("/"), !isMemorySpaceOpen, !isStatusPanelOpen, !isHelpPanelOpen else { return [] }
        guard !isUsageLimitPanelOpen && !isSettingsPanelOpen else { return [] }
        return SlashShortcutCatalog.matchingShortcuts(for: trimmed)
    }

    var isSlashMenuVisible: Bool {
        guard !isWalkthroughMode, !showSaveModal, inspectWorkflow == nil else { return false }
        let trimmed = query.trimmingCharacters(in: .whitespaces)
        guard trimmed.hasPrefix("/"), !isMemorySpaceOpen, !isStatusPanelOpen, !isHelpPanelOpen else { return false }
        guard !isUsageLimitPanelOpen && !isSettingsPanelOpen else { return false }
        let lower = trimmed.lowercased()
        if lower.hasPrefix("/memory ") || lower.hasPrefix("/recent ") || lower.hasPrefix("/ai ") || lower == "/status" || lower == "/help" || lower == "/limits" || lower == "/quota" || lower == "/usage" || lower == "/settings" || lower == "/preferences" || lower == "/config" {
            return false
        }
        return !matchingSlashCommands.isEmpty
    }

    var isSettingsCommand: Bool {
        guard !showSaveModal else { return false }
        if isSettingsPanelOpen {
            return true
        }
        guard !isWalkthroughMode else { return false }
        let trimmed = query.trimmingCharacters(in: .whitespaces).lowercased()
        if (trimmed == "/settings" || trimmed == "/preferences" || trimmed == "/config" || trimmed == "/limits" || trimmed == "/quota" || trimmed == "/usage") && !isSlashMenuVisible {
            return true
        }
        return false
    }

    var isUsageLimitCommand: Bool {
        return isSettingsCommand
    }

    var isMemoryCommand: Bool {
        guard !isWalkthroughMode, !showSaveModal else { return false }
        let trimmed = query.trimmingCharacters(in: .whitespaces).lowercased()
        guard trimmed.hasPrefix("/memory") else { return false }
        if isMemorySpaceOpen {
            return true
        }
        if trimmed.hasPrefix("/memory ") {
            return true
        }
        if trimmed == "/memory" && !isSlashMenuVisible {
            return true
        }
        return false
    }

    var isRecentCommand: Bool {
        guard !isWalkthroughMode, !showSaveModal else { return false }
        let trimmed = query.trimmingCharacters(in: .whitespaces).lowercased()
        guard trimmed.hasPrefix("/recent") else { return false }
        if trimmed.hasPrefix("/recent ") {
            return true
        }
        if trimmed == "/recent" && !isSlashMenuVisible {
            return true
        }
        return false
    }

    var recentWorkflows: [WorkflowItem] {
        let trimmed = query.trimmingCharacters(in: .whitespaces).lowercased()
        var searchFilter = ""
        if trimmed.hasPrefix("/recent ") {
            searchFilter = String(trimmed.dropFirst(8)).trimmingCharacters(in: .whitespaces)
        }

        var sourceList: [WorkflowItem] = []
        var seenIds = Set<String>()
        for item in (allSavedWorkflows + workflows) {
            if !seenIds.contains(item.id) {
                seenIds.insert(item.id)
                sourceList.append(item)
            }
        }
        for sysItem in SystemRecommendationCatalog.items {
            if !seenIds.contains(sysItem.id) {
                seenIds.insert(sysItem.id)
                sourceList.append(sysItem)
            }
        }

        if searchFilter.isEmpty {
            return Array(sourceList.prefix(5))
        } else {
            return sourceList.filter {
                $0.name.lowercased().contains(searchFilter) ||
                ($0.canonical_trigger?.lowercased().contains(searchFilter) ?? false) ||
                ($0.description?.lowercased().contains(searchFilter) ?? false)
            }
        }
    }

    var isStatusCommand: Bool {
        guard !isWalkthroughMode, !showSaveModal else { return false }
        let trimmed = query.trimmingCharacters(in: .whitespaces).lowercased()
        guard trimmed.hasPrefix("/status") else { return false }
        if isStatusPanelOpen {
            return true
        }
        if trimmed == "/status" && !isSlashMenuVisible {
            return true
        }
        return false
    }

    var isHelpCommand: Bool {
        guard !isWalkthroughMode, !showSaveModal else { return false }
        let trimmed = query.trimmingCharacters(in: .whitespaces).lowercased()
        guard trimmed.hasPrefix("/help") else { return false }
        if isHelpPanelOpen {
            return true
        }
        if trimmed == "/help" && !isSlashMenuVisible {
            return true
        }
        return false
    }

    var isAICommand: Bool {
        guard !isWalkthroughMode, !showSaveModal else { return false }
        let trimmed = query.trimmingCharacters(in: .whitespaces).lowercased()
        guard trimmed.hasPrefix("/ai") else { return false }
        if trimmed.hasPrefix("/ai ") {
            return true
        }
        if trimmed == "/ai" && !isSlashMenuVisible {
            return true
        }
        return false
    }

    var memoryWorkflows: [WorkflowItem] {
        let trimmed = query.trimmingCharacters(in: .whitespaces).lowercased()
        var searchFilter = ""
        if trimmed.hasPrefix("/memory ") {
            searchFilter = String(trimmed.dropFirst(8)).trimmingCharacters(in: .whitespaces)
        }

        let sourceList = !allSavedWorkflows.isEmpty ? allSavedWorkflows : workflows
        if searchFilter.isEmpty {
            return sourceList
        }
        return sourceList.filter {
            $0.displayName.localizedCaseInsensitiveContains(searchFilter) ||
            ($0.canonical_trigger?.localizedCaseInsensitiveContains(searchFilter) ?? false) ||
            ($0.description?.localizedCaseInsensitiveContains(searchFilter) ?? false)
        }
    }

    func recommendationMatches(item: WorkflowItem, query: String) -> Bool {
        let q = query.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard !q.isEmpty else { return false }

        let name = item.displayName.lowercased()
        let trig = item.canonical_trigger?.lowercased() ?? ""
        let desc = item.displayDesc.lowercased()

        // 1. Direct prefix matches on name or trigger
        if name.hasPrefix(q) || trig.hasPrefix(q) {
            return true
        }

        // 2. Token / word boundary matches: any individual word begins with query
        let words = (name + " " + trig).components(separatedBy: CharacterSet.alphanumerics.inverted).filter { !$0.isEmpty }
        if words.contains(where: { $0.hasPrefix(q) }) {
            return true
        }

        // 3. For queries with 3 or more characters, allow substring containment
        if q.count >= 3 {
            if name.contains(q) || trig.contains(q) || desc.contains(q) {
                return true
            }
        }

        return false
    }

    func updateRecommendationsSynchronously(for rawQuery: String) {
        let trimmed = rawQuery.trimmingCharacters(in: .whitespaces)
        if isWalkthroughMode || isMemoryCommand || isSlashMenuVisible || trimmed.hasPrefix("/") || trimmed.isEmpty {
            self.recommendations = []
            self.selectedIndex = 0
            return
        }

        var candidates: [WorkflowItem] = []
        var seenIds = Set<String>()

        // 1. User saved custom workflows
        for item in (allSavedWorkflows + workflows) {
            if !seenIds.contains(item.id) && recommendationMatches(item: item, query: trimmed) {
                seenIds.insert(item.id)
                candidates.append(item)
            }
        }

        // 2. Built-in system recommendations
        for sysItem in SystemRecommendationCatalog.items {
            if !seenIds.contains(sysItem.id) && recommendationMatches(item: sysItem, query: trimmed) {
                seenIds.insert(sysItem.id)
                candidates.append(sysItem)
            }
        }

        // 3. Sort candidates to prioritize exact prefix starts
        let lowerQ = trimmed.lowercased()
        candidates.sort { a, b in
            let aName = a.displayName.lowercased()
            let bName = b.displayName.lowercased()
            let aExact = aName.hasPrefix(lowerQ)
            let bExact = bName.hasPrefix(lowerQ)
            if aExact && !bExact { return true }
            if !aExact && bExact { return false }
            return (a.confidence ?? 0) > (b.confidence ?? 0)
        }

        self.recommendations = Array(candidates.prefix(4))
        if self.selectedIndex >= self.recommendations.count {
            self.selectedIndex = 0
        }
    }

    var currentInspectedIndex: Int? {
        guard let current = inspectWorkflow else { return nil }
        let list = isMemoryCommand ? memoryWorkflows : workflows
        return list.firstIndex(where: { $0.id == current.id })
    }

    func inspectPreviousWorkflow() {
        let list = isMemoryCommand ? memoryWorkflows : workflows
        guard let idx = currentInspectedIndex, idx > 0 else { return }
        inspectWorkflowDetails(list[idx - 1])
    }

    func inspectNextWorkflow() {
        let list = isMemoryCommand ? memoryWorkflows : workflows
        guard let idx = currentInspectedIndex, idx < list.count - 1 else { return }
        inspectWorkflowDetails(list[idx + 1])
    }

    func selectNextWorkflow() {
        if inspectWorkflow != nil {
            inspectNextWorkflow()
            return
        }
        if isSlashMenuVisible {
            let count = matchingSlashCommands.count
            guard count > 0 else { return }
            selectedIndex = min(selectedIndex + 1, count - 1)
            return
        }
        if isRecentCommand {
            let count = recentWorkflows.count
            guard count > 0 else { return }
            selectedIndex = min(selectedIndex + 1, count - 1)
            return
        }
        let count = isMemoryCommand ? memoryWorkflows.count : recommendations.count
        guard count > 0 else { return }
        isRecommendationExplicitlySelected = true
        selectedIndex = min(selectedIndex + 1, count - 1)
    }

    func selectPreviousWorkflow() {
        if inspectWorkflow != nil {
            inspectPreviousWorkflow()
            return
        }
        if isSlashMenuVisible {
            let count = matchingSlashCommands.count
            guard count > 0 else { return }
            selectedIndex = max(selectedIndex - 1, 0)
            return
        }
        if isRecentCommand {
            let count = recentWorkflows.count
            guard count > 0 else { return }
            selectedIndex = max(selectedIndex - 1, 0)
            return
        }
        let count = isMemoryCommand ? memoryWorkflows.count : recommendations.count
        guard count > 0 else { return }
        isRecommendationExplicitlySelected = true
        selectedIndex = max(selectedIndex - 1, 0)
    }

    private var sseTask: Task<Void, Never>?
    private let baseURL = URL(string: "http://127.0.0.1:8765")!
    private let speechManager = SpeechDictationManager()

    private var sessionToken: String {
        let tokenFile = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".clio/session_token")
        if let data = try? Data(contentsOf: tokenFile),
           let token = String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines),
           !token.isEmpty {
            return token
        }
        return ""
    }

    func makeAuthorizedRequest(url: URL, method: String = "GET") -> URLRequest {
        var req = URLRequest(url: url)
        req.httpMethod = method
        let token = sessionToken
        if !token.isEmpty {
            req.setValue(token, forHTTPHeaderField: "X-Clio-Token")
            req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        req.setValue("ClioBar", forHTTPHeaderField: "X-Clio-Client")
        return req
    }

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

    func deduplicateWorkflows(_ items: [WorkflowItem]) -> [WorkflowItem] {
        var seen = Set<String>()
        var deduped: [WorkflowItem] = []
        for item in items {
            func cleanToken(_ str: String) -> String {
                var s = str.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
                s = s.replacingOccurrences(of: "https://", with: "")
                     .replacingOccurrences(of: "http://", with: "")
                     .replacingOccurrences(of: "www.", with: "")
                for domain in [".com", ".org", ".net", ".io", ".ai", ".co", ".app"] {
                    s = s.replacingOccurrences(of: domain, with: "")
                }
                let prefixes = [
                    "open ", "launch ", "focus ", "start ", "goto ", "view ", "tab ", "opentab ",
                    "tab: ", "show ", "switch to ", "switch ", "run ", "play ", "the ", "open a ", "open the "
                ]
                var changed = true
                while changed {
                    changed = false
                    for p in prefixes {
                        if s.hasPrefix(p) {
                            s = String(s.dropFirst(p.count)).trimmingCharacters(in: .whitespaces)
                            changed = true
                        }
                    }
                }
                let suffixes = [" app", " application", " please", " now", " window", " tab"]
                changed = true
                while changed {
                    changed = false
                    for suf in suffixes {
                        if s.hasSuffix(suf) {
                            s = String(s.dropLast(suf.count)).trimmingCharacters(in: .whitespaces)
                            changed = true
                        }
                    }
                }
                // Filter alphanumeric only
                s = s.filter { $0.isLetter || $0.isNumber }

                // Synonym / alias table
                if s == "yt" || s.contains("youtube") { return "youtube" }
                if s == "fb" || s.contains("facebook") { return "facebook" }
                if s == "gg" || s.contains("google") { return "google" }
                if s == "ig" || s.contains("instagram") { return "instagram" }
                if s == "msg" || s.contains("message") || s == "imessage" { return "message" }
                if s == "calc" || s.contains("calculator") { return "calculator" }
                if s == "term" || s.contains("terminal") || s == "iterm" || s == "iterm2" { return "terminal" }
                if s.contains("photo") { return "photo" }
                if s.contains("note") { return "note" }
                if s.contains("reminder") { return "reminder" }
                if s.contains("calendar") { return "calendar" }
                if s.contains("contact") { return "contact" }
                if s.contains("setting") || s.contains("preference") { return "setting" }
                if s == "vscode" || s == "visualstudiocode" { return "code" }

                // Plural to singular normalization
                if s.hasSuffix("ies") && s.count > 4 {
                    s = String(s.dropLast(3)) + "y"
                } else if s.hasSuffix("es") && s.count > 4 && !s.hasSuffix("sses") && !s.hasSuffix("uses") && !s.hasSuffix("ises") {
                    s = String(s.dropLast(1))
                } else if s.hasSuffix("s") && s.count > 3 && !s.hasSuffix("ss") && !s.hasSuffix("us") && !s.hasSuffix("is") && !s.hasSuffix("as") {
                    s = String(s.dropLast(1))
                }

                if s == "photo" { return "photo" }
                if s == "note" { return "note" }
                if s == "message" { return "message" }
                if s == "setting" { return "setting" }

                return s
            }
            let key = cleanToken(item.displayName)
            let trigKey = cleanToken(item.canonical_trigger ?? "")
            let primary = !key.isEmpty ? key : trigKey
            if !primary.isEmpty && seen.contains(primary) {
                continue
            }
            if !primary.isEmpty { seen.insert(primary) }
            if !key.isEmpty { seen.insert(key) }
            if !trigKey.isEmpty { seen.insert(trigKey) }
            deduped.append(item)
        }
        return deduped
    }

    func fetchWorkflows() async {
        guard let url = URL(string: "/api/workflows", relativeTo: baseURL) else { return }
        do {
            let req = makeAuthorizedRequest(url: url)
            let (data, response) = try await URLSession.shared.data(for: req)
            if let http = response as? HTTPURLResponse, (200...299).contains(http.statusCode) {
                let items = try JSONDecoder().decode([WorkflowItem].self, from: data)
                let deduped = self.deduplicateWorkflows(items)
                self.workflows = deduped
                self.allSavedWorkflows = deduped
                self.isConnected = true
                self.updateRecommendationsSynchronously(for: self.query)
                if self.selectedIndex >= self.recommendations.count { self.selectedIndex = 0 }
            }
        } catch {
            // Silently suppress -1004 connection errors while retrying
            self.isConnected = false
        }
    }

    func fetchStatus() async {
        guard let url = URL(string: "/api/status", relativeTo: baseURL) else { return }
        do {
            let req = makeAuthorizedRequest(url: url)
            let (data, response) = try await URLSession.shared.data(for: req)
            if let http = response as? HTTPURLResponse, (200...299).contains(http.statusCode) {
                let st = try JSONDecoder().decode(ClioStatus.self, from: data)
                self.isExecuting = (st.status == "executing")
                if let tone = st.tone { self.currentTone = tone.capitalized }
                if let vc = st.virtual_cursor {
                    self.vcCoords = "VC (\(Int(vc.x)), \(Int(vc.y))) • \(vc.state.uppercased())"
                }
                if let comm = st.recent_commentary { self.commentary = comm }
                if let rawJson = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                   let usageDict = rawJson["usage"] as? [String: Any] {
                    self.applyUsagePayload(usageDict)
                }
                self.statusPillText = self.isExecuting ? "CLIO • BUSY" : "CLIO • READY"
                self.isConnected = true
            }
        } catch {
            self.isConnected = false
        }
    }

    func fetchUsageStatus() async {
        guard let url = URL(string: "/api/usage", relativeTo: baseURL) else { return }
        do {
            let req = makeAuthorizedRequest(url: url)
            let (data, response) = try await URLSession.shared.data(for: req)
            if let http = response as? HTTPURLResponse, (200...299).contains(http.statusCode),
               let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                if let usageDict = json["usage"] as? [String: Any] {
                    self.applyUsagePayload(usageDict)
                } else {
                    self.applyUsagePayload(json)
                }
            }
        } catch { }
    }

    func applyUsagePayload(_ dict: [String: Any]) {
        if let resetStr = dict["reset_formatted"] as? String {
            self.usageResetFormatted = resetStr
        }
        if let wt = dict["walkthrough"] as? [String: Any] {
            self.walkthroughUsed = wt["used"] as? Int ?? self.walkthroughUsed
            self.walkthroughDailyLimit = max(1, wt["daily_limit"] as? Int ?? self.walkthroughDailyLimit)
            self.walkthroughRemaining = max(0, wt["remaining"] as? Int ?? (self.walkthroughDailyLimit - self.walkthroughUsed))
            self.walkthroughRpmUsed = wt["rpm_used"] as? Int ?? self.walkthroughRpmUsed
            self.walkthroughRpmLimit = max(1, wt["rpm_limit"] as? Int ?? self.walkthroughRpmLimit)
            self.walkthroughCloudUsed = wt["cloud_ai_used"] as? Int ?? self.walkthroughCloudUsed
            self.walkthroughLocalUsed = wt["local_used"] as? Int ?? self.walkthroughLocalUsed
            self.walkthroughExhausted = wt["exhausted"] as? Bool ?? (self.walkthroughRemaining <= 0)
        }
        if let auto = dict["automation"] as? [String: Any] {
            self.automationUsed = auto["used"] as? Int ?? self.automationUsed
            self.automationDailyLimit = max(1, auto["daily_limit"] as? Int ?? self.automationDailyLimit)
            self.automationRemaining = max(0, auto["remaining"] as? Int ?? (self.automationDailyLimit - self.automationUsed))
            self.automationRpmUsed = auto["rpm_used"] as? Int ?? self.automationRpmUsed
            self.automationRpmLimit = max(1, auto["rpm_limit"] as? Int ?? self.automationRpmLimit)
            self.automationCloudUsed = auto["cloud_ai_used"] as? Int ?? self.automationCloudUsed
            self.automationLocalUsed = auto["local_used"] as? Int ?? self.automationLocalUsed
            self.automationExhausted = auto["exhausted"] as? Bool ?? (self.automationRemaining <= 0)
        }
        if let gem = dict["gemini_api"] as? [String: Any] {
            self.geminiActiveModel = gem["active_model"] as? String ?? self.geminiActiveModel
            self.geminiCallsToday = gem["calls_today"] as? Int ?? self.geminiCallsToday
            self.geminiDailyLimit = max(1, gem["daily_limit"] as? Int ?? self.geminiDailyLimit)
            self.geminiRpmUsed = gem["rpm_used"] as? Int ?? self.geminiRpmUsed
            self.geminiRpmLimit = max(1, gem["rpm_limit"] as? Int ?? self.geminiRpmLimit)
            self.geminiRateLimited = gem["rate_limited"] as? Bool ?? false
            self.geminiCooldownSeconds = gem["cooldown_seconds"] as? Int ?? 0
        }
        if !self.walkthroughExhausted && !self.automationExhausted {
            if self.usageLimitReachedBanner != nil {
                self.usageLimitReachedBanner = nil
            }
        }
    }

    func toggleSettingsPanel(tab: SettingsTab = .usage) {
        withAnimation(.easeInOut(duration: 0.2)) {
            if self.isSettingsPanelOpen && self.selectedSettingsTab == tab {
                self.isSettingsPanelOpen = false
                self.isUsageLimitPanelOpen = false
                self.query = ""
            } else {
                self.isMemorySpaceOpen = false
                self.isStatusPanelOpen = false
                self.isHelpPanelOpen = false
                self.isWalkthroughMode = false
                self.inspectWorkflow = nil
                self.selectedSettingsTab = tab
                self.isSettingsPanelOpen = true
                self.isUsageLimitPanelOpen = (tab == .usage)
                self.query = ""
                Task {
                    await self.fetchUsageStatus()
                    await self.fetchAIStatus()
                    await self.fetchStorageStatus()
                }
            }
        }
    }

    func toggleUsageLimitPanel() {
        toggleSettingsPanel(tab: .usage)
    }

    func fetchStorageStatus() async {
        guard let url = URL(string: "/api/system/storage", relativeTo: baseURL) else { return }
        do {
            let req = makeAuthorizedRequest(url: url)
            let (data, response) = try await URLSession.shared.data(for: req)
            if let http = response as? HTTPURLResponse, (200...299).contains(http.statusCode),
               let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                await MainActor.run {
                    self.storageMetrics = json
                    let wfCount = json["workflow_count"] as? Int ?? self.allSavedWorkflows.count
                    let mbRec = json["recordings_size_mb"] as? Double ?? 0.0
                    let mbDb = json["db_size_mb"] as? Double ?? 0.0
                    self.storageStatusSummary = "\(wfCount) Workflows • \(String(format: "%.1f", mbRec + mbDb))MB Storage"
                }
            }
        } catch {
            await MainActor.run {
                self.storageStatusSummary = "\(self.allSavedWorkflows.count) Workflows in Memory"
            }
        }
    }

    func clearStorageCache() async {
        guard let url = URL(string: "/api/system/cache/clear", relativeTo: baseURL) else { return }
        self.isClearingStorage = true
        do {
            var req = makeAuthorizedRequest(url: url, method: "POST")
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try? JSONSerialization.data(withJSONObject: [:])
            let (data, response) = try await URLSession.shared.data(for: req)
            if let http = response as? HTTPURLResponse, (200...299).contains(http.statusCode),
               let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                await MainActor.run {
                    if let st = json["storage"] as? [String: Any] {
                        self.storageMetrics = st
                    }
                    self.statusPillText = "CACHE CLEARED"
                }
            }
        } catch {
            // Suppress or log silently
        }
        self.isClearingStorage = false
        await fetchStorageStatus()
    }

    func saveAIModelConfig(model: String, apiKey: String? = nil) async {
        guard let url = URL(string: "/api/ai/config", relativeTo: baseURL) else { return }
        self.aiModelSelection = model
        var payload: [String: Any] = ["model": model, "provider": "gemini"]
        if let key = apiKey, !key.trimmingCharacters(in: .whitespaces).isEmpty {
            payload["api_key"] = key.trimmingCharacters(in: .whitespaces)
        }
        do {
            var req = makeAuthorizedRequest(url: url, method: "POST")
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try? JSONSerialization.data(withJSONObject: payload)
            let (_, response) = try await URLSession.shared.data(for: req)
            if let http = response as? HTTPURLResponse, (200...299).contains(http.statusCode) {
                await MainActor.run {
                    self.geminiActiveModel = model
                }
                await fetchAIStatus()
                await fetchUsageStatus()
            }
        } catch {
            // Suppress or log silently
        }
    }

    func testAIConnection(apiKey: String? = nil) async {
        guard let url = URL(string: "/api/ai/test", relativeTo: baseURL) else { return }
        self.isTestingAIKey = true
        self.aiTestStatusText = "Testing connection..."
        var payload: [String: Any] = ["provider": "gemini"]
        if let key = apiKey, !key.trimmingCharacters(in: .whitespaces).isEmpty {
            payload["api_key"] = key.trimmingCharacters(in: .whitespaces)
        }
        do {
            var req = makeAuthorizedRequest(url: url, method: "POST")
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try? JSONSerialization.data(withJSONObject: payload)
            let (data, response) = try await URLSession.shared.data(for: req)
            if let http = response as? HTTPURLResponse, (200...299).contains(http.statusCode),
               let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                let valid = json["valid"] as? Bool ?? false
                await MainActor.run {
                    self.aiTestStatusText = valid ? "CONNECTED • Key Valid (Gemini API Active)" : "FAILED • Invalid Key or Quota Exhausted"
                }
            } else {
                await MainActor.run {
                    self.aiTestStatusText = "FAILED • API Connection Error"
                }
            }
        } catch {
            await MainActor.run {
                self.aiTestStatusText = "FAILED • Could not connect to engine"
            }
        }
        self.isTestingAIKey = false
    }

    func resetUsageQuota(feature: String? = nil) {
        guard let url = URL(string: "/api/usage/reset", relativeTo: baseURL) else { return }
        var req = makeAuthorizedRequest(url: url, method: "POST")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        var body: [String: Any] = [:]
        if let f = feature {
            body["feature"] = f
        }
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        self.usageLimitReachedBanner = nil
        Task {
            if let (data, resp) = try? await URLSession.shared.data(for: req),
               let http = resp as? HTTPURLResponse, (200...299).contains(http.statusCode),
               let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let usageDict = json["usage"] as? [String: Any] {
                await MainActor.run {
                    self.applyUsagePayload(usageDict)
                    self.usageLimitReachedBanner = nil
                }
            }
        }
    }

    func adjustFeatureLimit(feature: String, delta: Int) {
        guard let url = URL(string: "/api/usage/config", relativeTo: baseURL) else { return }
        var req = makeAuthorizedRequest(url: url, method: "POST")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        var body: [String: Any] = [:]
        if feature == "walkthrough" {
            let next = max(1, min(500, self.walkthroughDailyLimit + delta))
            body["walkthrough_daily_limit"] = next
        } else if feature == "automation" {
            let next = max(1, min(500, self.automationDailyLimit + delta))
            body["automation_daily_limit"] = next
        }
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        Task {
            if let (data, resp) = try? await URLSession.shared.data(for: req),
               let http = resp as? HTTPURLResponse, (200...299).contains(http.statusCode),
               let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let usageDict = json["usage"] as? [String: Any] {
                await MainActor.run {
                    self.applyUsagePayload(usageDict)
                }
            }
        }
    }

    private var searchTask: Task<Void, Never>?

    func search(text: String) async {
        if isWalkthroughMode {
            self.recommendations = []
            self.workflows = []
            self.inspectWorkflow = nil
            self.selectedIndex = 0
            return
        }
        let lowerSearch = text.trimmingCharacters(in: .whitespaces).lowercased()
        if !lowerSearch.isEmpty && !["/limits", "/quota", "/usage", "/settings", "/preferences", "/config"].contains(lowerSearch) && (isUsageLimitPanelOpen || isSettingsPanelOpen) {
            self.isUsageLimitPanelOpen = false
            self.isSettingsPanelOpen = false
        }
        if isSlashMenuVisible || text.trimmingCharacters(in: .whitespaces).hasPrefix("/") {
            self.recommendations = []
            self.selectedIndex = 0
            return
        }
        if isMemoryCommand {
            if allSavedWorkflows.isEmpty {
                await fetchWorkflows()
            }
            self.selectedIndex = 0
            return
        }
        // Immediate synchronous filtering eliminates 100% of flicker
        self.updateRecommendationsSynchronously(for: text)

        let trimmed = text.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty else {
            await fetchWorkflows()
            self.inspectWorkflow = nil
            return
        }

        // Cancel previous pending search task to avoid out-of-order race conditions
        searchTask?.cancel()
        searchTask = Task { [weak self] in
            guard let self = self else { return }
            guard let encoded = trimmed.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed),
                  let url = URL(string: "/api/search?q=\(encoded)", relativeTo: self.baseURL) else { return }
            do {
                let req = self.makeAuthorizedRequest(url: url)
                let (data, _) = try await URLSession.shared.data(for: req)
                if Task.isCancelled || self.isWalkthroughMode { return }
                let items = try JSONDecoder().decode([WorkflowItem].self, from: data)
                let deduped = self.deduplicateWorkflows(items)
                await MainActor.run {
                    if Task.isCancelled { return }
                    for item in deduped {
                        if !self.allSavedWorkflows.contains(where: { $0.id == item.id }) {
                            self.allSavedWorkflows.append(item)
                        }
                    }
                    self.updateRecommendationsSynchronously(for: self.query)
                    self.checkAndInspectQuery(trimmed)
                }
            } catch {
                // Silently handled
            }
        }
    }

    func checkAndInspectQuery(_ text: String) {
        if isWalkthroughMode {
            self.inspectWorkflow = nil
            return
        }
        let trimmed = text.trimmingCharacters(in: .whitespaces).lowercased()
        guard !trimmed.isEmpty else {
            self.inspectWorkflow = nil
            return
        }
        // If the query directly matches a known workflow name or canonical trigger,
        // show the steps in order to perform that action without executing yet.
        if let exactMatch = workflows.first(where: {
            $0.displayName.trimmingCharacters(in: .whitespaces).lowercased() == trimmed ||
            ($0.canonical_trigger?.trimmingCharacters(in: .whitespaces).lowercased() == trimmed)
        }) {
            inspectWorkflowDetails(exactMatch)
        } else if let cur = inspectWorkflow {
            let nameMatch = cur.displayName.localizedCaseInsensitiveContains(trimmed)
            let trigMatch = cur.canonical_trigger?.localizedCaseInsensitiveContains(trimmed) ?? false
            if !nameMatch && !trigMatch {
                self.inspectWorkflow = nil
            }
        }
    }

    func inspectWorkflowDetails(_ wf: WorkflowItem) {
        if isWalkthroughMode {
            self.inspectWorkflow = nil
            return
        }
        self.inspectWorkflow = wf
        // If steps are not yet populated, fetch from server
        if wf.steps == nil || wf.steps!.isEmpty {
            guard let url = URL(string: "/api/workflows/\(wf.id)", relativeTo: baseURL) else { return }
            Task {
                do {
                    let req = makeAuthorizedRequest(url: url)
                    let (data, response) = try await URLSession.shared.data(for: req)
                    if let http = response as? HTTPURLResponse, (200...299).contains(http.statusCode) {
                        let fullItem = try JSONDecoder().decode(WorkflowItem.self, from: data)
                        await MainActor.run {
                            if self.inspectWorkflow?.id == wf.id {
                                self.inspectWorkflow = fullItem
                            }
                        }
                    }
                } catch {
                    // Silently handled
                }
            }
        }
    }

    func runInspectedWorkflow() {
        guard let wf = inspectWorkflow else { return }
        let idToRun = wf.id
        self.inspectWorkflow = nil
        AppDelegate.shared?.hidePanel()
        self.executeById(idToRun)
    }

    func dismissInspection() {
        self.inspectWorkflow = nil
    }

    func toggleWalkthroughMode() {
        isWalkthroughMode.toggle()
        if isWalkthroughMode {
            self.recommendations = []
            self.workflows = []
            self.inspectWorkflow = nil
            self.isMemorySpaceOpen = false
            self.isUsageLimitPanelOpen = false
            self.statusPillText = "CLIO • TEACH ME"
        } else {
            self.statusPillText = "CLIO • READY"
            self.updateRecommendationsSynchronously(for: self.query)
        }
    }

    func startWalkthrough(query: String) {
        let trimmed = query.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty else { return }

        // Immediately dismiss panel so the desktop and walkthrough are not obstructed
        self.query = ""
        self.isWalkthroughMode = false
        self.statusPillText = "CLIO • TEACHING"
        AppDelegate.shared?.hidePanel()

        guard let url = URL(string: "/api/walkthrough/start", relativeTo: baseURL) else { return }
        var req = makeAuthorizedRequest(url: url, method: "POST")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")

        let frontmostApp = NSWorkspace.shared.frontmostApplication?.localizedName ?? "Finder"
        let osVersion = ProcessInfo.processInfo.operatingSystemVersionString
        let payload: [String: Any] = [
            "query": trimmed,
            "mode": "guided_demo",
            "context": [
                "active_app": frontmostApp,
                "os_version": osVersion,
            ],
        ]
        req.httpBody = try? JSONSerialization.data(withJSONObject: payload)

        Task {
            if let (data, response) = try? await URLSession.shared.data(for: req),
               let http = response as? HTTPURLResponse,
               let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                await MainActor.run {
                    if let usageDict = json["usage"] as? [String: Any] {
                        self.applyUsagePayload(usageDict)
                    }
                    if http.statusCode == 429 || (json["limit_reached"] as? Bool == true) {
                        self.usageLimitReachedBanner = json["error"] as? String ?? "Daily Walkthrough usage limit reached."
                        self.statusPillText = "QUOTA LIMIT REACHED"
                        self.isUsageLimitPanelOpen = true
                        AppDelegate.shared?.showPanel()
                        return
                    }
                    if (200...299).contains(http.statusCode), !WalkthroughOverlayManager.shared.isVisible {
                        if let telemetry = json["telemetry"] as? [String: Any] {
                            WalkthroughOverlayManager.shared.update(data: telemetry)
                        } else if let plan = json["plan"] as? [String: Any] {
                            var overlayData: [String: Any] = [
                                "status": "NAVIGATING",
                                "goal": plan["goal"] as? String ?? trimmed,
                                "current_step_index": 1,
                                "total_steps": (plan["steps"] as? [[String: Any]])?.count ?? 1,
                                "steps": plan["steps"] ?? []
                            ]
                            if let steps = plan["steps"] as? [[String: Any]], let firstStep = steps.first {
                                overlayData["step"] = firstStep
                            }
                            WalkthroughOverlayManager.shared.update(data: overlayData)
                        }
                    }
                }
            }
        }
    }

    func executeSelected() {
        let trimmed = query.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty else { return }

        let lower = trimmed.lowercased()

        // 1. Explicit user commands to close / dismiss the bar
        if ["close", "hide", "quit", "exit", "dismiss", "cancel", "done", "esc"].contains(lower) {
            self.query = ""
            self.inspectWorkflow = nil
            AppDelegate.shared?.hidePanel()
            return
        }

        // 1a. Settings & Usage Limits Panel Execution
        if lower == "/limits" || lower == "/quota" || lower == "/usage" || lower == "/settings" || lower == "/preferences" || lower == "/config" {
            self.isMemorySpaceOpen = false
            self.isStatusPanelOpen = false
            self.isHelpPanelOpen = false
            self.selectedSettingsTab = (lower == "/settings" || lower == "/preferences" || lower == "/config") ? .usage : .usage
            self.isSettingsPanelOpen = true
            self.isUsageLimitPanelOpen = true
            self.query = ""
            Task {
                await fetchUsageStatus()
                await fetchAIStatus()
                await fetchStorageStatus()
            }
            return
        }

        // 1b. Walkthrough Mode or Trigger Execution
        var targetWalkthroughQuery = trimmed
        if lower.hasPrefix("/teach ") {
            targetWalkthroughQuery = String(trimmed.dropFirst(7)).trimmingCharacters(in: .whitespaces)
        } else if lower.hasPrefix("/walkthrough ") {
            targetWalkthroughQuery = String(trimmed.dropFirst(13)).trimmingCharacters(in: .whitespaces)
        } else if lower == "/teach" || lower == "/walkthrough" {
            targetWalkthroughQuery = ""
        }

        let isWalkthroughTrigger = lower.hasPrefix("teach ") ||
                                   lower.hasPrefix("teach me") ||
                                   lower.hasPrefix("help me") ||
                                   lower.hasPrefix("navigate") ||
                                   lower.hasPrefix("how do i") ||
                                   lower.hasPrefix("how can i") ||
                                   lower.hasPrefix("how to") ||
                                   lower.hasPrefix("show me") ||
                                   lower.hasPrefix("guide me") ||
                                   lower.hasPrefix("walk me through") ||
                                   lower.hasPrefix("walkthrough") ||
                                   lower.hasPrefix("tutorial") ||
                                   lower.hasPrefix("learn") ||
                                   lower.hasPrefix("/teach") ||
                                   lower.hasPrefix("/walkthrough") ||
                                   lower.contains("command bar") ||
                                   lower.contains("move between window") ||
                                   lower.contains("switch between window") ||
                                   lower.contains("switch window")
        if isWalkthroughMode || isWalkthroughTrigger {
            if targetWalkthroughQuery.isEmpty {
                withAnimation(.easeInOut(duration: 0.25)) {
                    self.isWalkthroughMode = true
                }
                return
            }
            startWalkthrough(query: targetWalkthroughQuery)
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
                self.inspectWorkflow = nil
                AppDelegate.shared?.hidePanel()
                return
            }
        }

        // If currently inspecting steps and user presses Return, perform the action based on the dissected steps!
        if inspectWorkflow != nil {
            runInspectedWorkflow()
            return
        }

        // Slash Commands Selection: execute or switch to the selected shortcut
        if isSlashMenuVisible {
            let list = matchingSlashCommands
            if selectedIndex >= 0 && selectedIndex < list.count {
                executeSlashShortcut(list[selectedIndex])
            } else if let first = list.first {
                executeSlashShortcut(first)
            }
            return
        }

        // Memory Space Selection
        if isMemoryCommand {
            let list = memoryWorkflows
            if selectedIndex >= 0 && selectedIndex < list.count {
                inspectWorkflowDetails(list[selectedIndex])
            } else if let first = list.first {
                inspectWorkflowDetails(first)
            }
            return
        }

        // Literal "/stop" execution
        if lower == "/stop" {
            if let stopItem = SlashShortcutCatalog.shortcuts.first(where: { $0.id == "shortcut_stop" }) {
                executeSlashShortcut(stopItem)
                return
            }
        }

        // Recent Automations Execution
        if lower == "/recent" || isRecentCommand {
            self.isMemorySpaceOpen = false
            self.isStatusPanelOpen = false
            self.isHelpPanelOpen = false
            self.isUsageLimitPanelOpen = false
            self.query = ""
            if let recent = recentWorkflows.first {
                executeWorkflowOrSystemAction(recent)
            } else if let fallback = SystemRecommendationCatalog.items.first {
                executeWorkflowOrSystemAction(fallback)
            }
            return
        }

        // Help Guide Execution
        if lower == "/help" || isHelpCommand {
            self.isMemorySpaceOpen = false
            self.isStatusPanelOpen = false
            self.isUsageLimitPanelOpen = false
            self.isHelpPanelOpen = true
            self.query = "/help"
            return
        }

        // Status Diagnostics Selection
        if lower == "/status" || isStatusCommand {
            self.isUsageLimitPanelOpen = false
            checkSystemHealth()
            return
        }

        // AI Companion Query
        if isAICommand {
            let prompt: String
            if trimmed.lowercased().hasPrefix("/ai ") {
                prompt = String(trimmed.dropFirst(4)).trimmingCharacters(in: .whitespaces)
            } else if trimmed.lowercased() == "/ai" {
                prompt = ""
            } else {
                prompt = trimmed
            }
            if !prompt.isEmpty {
                Task { await askAICompanion(prompt: prompt) }
            }
            return
        }

        // Recommendations selection: executes system actions or inspects matching actions ONLY IF explicitly selected by user!
        if isRecommendationExplicitlySelected && !recommendations.isEmpty && selectedIndex >= 0 && selectedIndex < recommendations.count {
            let selectedItem = recommendations[selectedIndex]
            executeWorkflowOrSystemAction(selectedItem)
            return
        }

        let normQuery = trimmed.replacingOccurrences(of: "\\s+", with: " ", options: .regularExpression).lowercased()

        // When the user types an action matching an existing saved workflow, inspect its steps:
        if let match = (allSavedWorkflows + workflows).first(where: {
            let dName = $0.displayName.replacingOccurrences(of: "\\s+", with: " ", options: .regularExpression).lowercased()
            let cTrig = $0.canonical_trigger?.replacingOccurrences(of: "\\s+", with: " ", options: .regularExpression).lowercased()
            return dName == normQuery || cTrig == normQuery
        }) {
            inspectWorkflowDetails(match)
            return
        }

        // Check if there is an explicitly selected workflow from arrow keys
        if isRecommendationExplicitlySelected && selectedIndex >= 0 && selectedIndex < workflows.count {
            inspectWorkflowDetails(workflows[selectedIndex])
            return
        }

        // If in walkthrough mode or walkthrough trigger prompt: launch Walkthrough Tutor dynamically!
        if isWalkthroughMode || isWalkthroughTrigger {
            startWalkthrough(query: trimmed)
            return
        }

        // Direct command bar execution: execute dynamically via autonomous automation engine!
        executeByQuery(trimmed)
        self.query = ""
        self.inspectWorkflow = nil
        AppDelegate.shared?.hidePanel()
    }

    func executeSlashShortcut(_ item: SlashShortcutItem) {
        switch item.id {
        case "shortcut_memory":
            self.isStatusPanelOpen = false
            self.isHelpPanelOpen = false
            self.isUsageLimitPanelOpen = false
            self.isMemorySpaceOpen = true
            self.query = "/memory "
            self.selectedIndex = 0
            if allSavedWorkflows.isEmpty {
                Task { await fetchWorkflows() }
            }
        case "shortcut_record":
            self.isMemorySpaceOpen = false
            self.isStatusPanelOpen = false
            self.isHelpPanelOpen = false
            self.isUsageLimitPanelOpen = false
            self.query = ""
            self.selectedIndex = 0
            self.toggleRecording()
        case "shortcut_teach":
            self.isMemorySpaceOpen = false
            self.isStatusPanelOpen = false
            self.isHelpPanelOpen = false
            self.isUsageLimitPanelOpen = false
            self.query = ""
            self.selectedIndex = 0
            withAnimation(.easeInOut(duration: 0.25)) {
                self.isWalkthroughMode = true
            }
        case "shortcut_recent":
            self.isMemorySpaceOpen = false
            self.isStatusPanelOpen = false
            self.isHelpPanelOpen = false
            self.isUsageLimitPanelOpen = false
            self.query = ""
            self.selectedIndex = 0
            if let recent = self.recentWorkflows.first {
                self.executeWorkflowOrSystemAction(recent)
            } else if let fallback = SystemRecommendationCatalog.items.first {
                self.executeWorkflowOrSystemAction(fallback)
            }
        case "shortcut_stop":
            self.isMemorySpaceOpen = false
            self.isStatusPanelOpen = false
            self.isHelpPanelOpen = false
            self.isUsageLimitPanelOpen = false
            self.query = ""
            self.selectedIndex = 0
            self.cancelTask()
            WalkthroughOverlayManager.shared.stopWalkthrough()
            VirtualCursorOverlayManager.shared.hide()
            if self.isRecording {
                self.discardRecording()
            }
            withAnimation(.easeInOut(duration: 0.2)) {
                self.statusPillText = "ALL HALTED • SAFE"
            }
        case "shortcut_status":
            self.isMemorySpaceOpen = false
            self.isHelpPanelOpen = false
            self.isUsageLimitPanelOpen = false
            self.isStatusPanelOpen = true
            self.query = "/status"
            self.selectedIndex = 0
            self.checkSystemHealth()
        case "shortcut_ai":
            self.isMemorySpaceOpen = false
            self.isStatusPanelOpen = false
            self.isHelpPanelOpen = false
            self.isUsageLimitPanelOpen = false
            self.query = "/ai "
            self.selectedIndex = 0
        case "shortcut_help":
            self.isMemorySpaceOpen = false
            self.isStatusPanelOpen = false
            self.isUsageLimitPanelOpen = false
            self.isHelpPanelOpen = true
            self.query = "/help"
            self.selectedIndex = 0
        case "shortcut_limits":
            self.isMemorySpaceOpen = false
            self.isStatusPanelOpen = false
            self.isHelpPanelOpen = false
            self.selectedSettingsTab = .usage
            self.isSettingsPanelOpen = true
            self.isUsageLimitPanelOpen = true
            self.query = ""
            self.selectedIndex = 0
            Task {
                await fetchUsageStatus()
                await fetchAIStatus()
                await fetchStorageStatus()
            }
        case "shortcut_settings":
            self.isMemorySpaceOpen = false
            self.isStatusPanelOpen = false
            self.isHelpPanelOpen = false
            self.selectedSettingsTab = .usage
            self.isSettingsPanelOpen = true
            self.isUsageLimitPanelOpen = true
            self.query = ""
            self.selectedIndex = 0
            Task {
                await fetchUsageStatus()
                await fetchAIStatus()
                await fetchStorageStatus()
            }
        default:
            self.isMemorySpaceOpen = false
            self.isStatusPanelOpen = false
            self.isHelpPanelOpen = false
            self.isUsageLimitPanelOpen = false
            self.isSettingsPanelOpen = false
            self.query = item.command + " "
            self.selectedIndex = 0
        }
    }

    func checkSystemHealth() {
        self.systemAccessibilityGranted = AXIsProcessTrusted()
        Task {
            await fetchStatus()
            await fetchAIStatus()
            await fetchUsageStatus()
        }
    }

    func fetchAIStatus() async {
        guard let url = URL(string: "/api/ai/status", relativeTo: baseURL) else { return }
        do {
            let req = makeAuthorizedRequest(url: url)
            let (data, response) = try await URLSession.shared.data(for: req)
            if let http = response as? HTTPURLResponse, (200...299).contains(http.statusCode),
               let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                let provider = json["active_provider"] as? String ?? "Local Engine"
                let status = json["status"] as? String ?? "Ready"
                self.aiProviderName = provider.capitalized
                self.aiProviderStatus = status.capitalized
                if let usageDict = json["usage"] as? [String: Any] {
                    self.applyUsagePayload(usageDict)
                }
            }
        } catch {
            self.aiProviderName = "Local Engine"
            self.aiProviderStatus = "Fallback Active"
        }
    }

    func openAccessibilitySettings() {
        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility") {
            NSWorkspace.shared.open(url)
        }
    }

    func askAICompanion(prompt: String) async {
        self.isAILoading = true
        self.aiCompanionPrompt = prompt
        self.aiCompanionResponse = ""
        self.aiTriggeredWorkflow = nil

        guard let url = URL(string: "/api/chat", relativeTo: baseURL) else {
            self.isAILoading = false
            return
        }
        var req = makeAuthorizedRequest(url: url, method: "POST")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let payload: [String: Any] = ["message": prompt]
        req.httpBody = try? JSONSerialization.data(withJSONObject: payload)

        do {
            let (data, response) = try await URLSession.shared.data(for: req)
            self.isAILoading = false
            if let http = response as? HTTPURLResponse, (200...299).contains(http.statusCode),
               let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                let reply = json["reply"] as? String ?? "I'm here to help with your Mac automation."
                self.aiCompanionResponse = reply
                if let triggered = json["triggered_workflow"] as? String {
                    self.aiTriggeredWorkflow = triggered
                }
            } else {
                self.aiCompanionResponse = "Clio companion received: '\(prompt)'. How else can I assist with your workflow?"
            }
        } catch {
            self.isAILoading = false
            self.aiCompanionResponse = "Clio companion: Could not reach chat engine. Make sure the local server is running."
        }
    }

    func executeWorkflowOrSystemAction(_ item: WorkflowItem) {
        if self.automationExhausted {
            self.usageLimitReachedBanner = "Daily Automation limit reached (\(self.automationUsed)/\(self.automationDailyLimit) uses today)."
            self.statusPillText = "QUOTA LIMIT REACHED"
            self.isUsageLimitPanelOpen = true
            AppDelegate.shared?.showPanel()
            return
        }
        if item.id.hasPrefix("sys_") {
            executeSystemAction(item.id)
            self.query = ""
            self.inspectWorkflow = nil
            withAnimation(.easeInOut(duration: 0.2)) {
                self.statusPillText = "RUNNING: \(item.displayName.uppercased())"
            }
            AppDelegate.shared?.hidePanel()
            return
        }
        self.query = ""
        self.inspectWorkflow = nil
        withAnimation(.easeInOut(duration: 0.2)) {
            self.statusPillText = "RUNNING: \(item.displayName.uppercased())"
        }
        AppDelegate.shared?.hidePanel()
        self.executeById(item.id)
    }

    private func launchSystemApp(bundleId: String, appName: String) {
        if let appURL = NSWorkspace.shared.urlForApplication(withBundleIdentifier: bundleId) {
            let config = NSWorkspace.OpenConfiguration()
            config.activates = true
            NSWorkspace.shared.openApplication(at: appURL, configuration: config, completionHandler: nil)
            return
        }
        let systemPath = "/System/Applications/\(appName).app"
        let normalPath = "/Applications/\(appName).app"
        let path = FileManager.default.fileExists(atPath: systemPath) ? systemPath : normalPath
        let url = URL(fileURLWithPath: path)
        let config = NSWorkspace.OpenConfiguration()
        config.activates = true
        NSWorkspace.shared.openApplication(at: url, configuration: config, completionHandler: nil)
    }

    func executeSystemAction(_ id: String) {
        switch id {
        case "sys_home":
            NSWorkspace.shared.open(FileManager.default.homeDirectoryForCurrentUser)
        case "sys_help":
            if let url = URL(string: "help:") {
                NSWorkspace.shared.open(url)
            }
        case "sys_hotcorners":
            if let url = URL(string: "x-apple.systempreferences:com.apple.preference.expose") {
                NSWorkspace.shared.open(url)
            } else {
                NSWorkspace.shared.open(URL(fileURLWithPath: "/System/Applications/System Settings.app"))
            }
        case "sys_hide_others":
            NSWorkspace.shared.hideOtherApplications()
        case "sys_photos":
            launchSystemApp(bundleId: "com.apple.Photos", appName: "Photos")
        case "sys_podcasts":
            launchSystemApp(bundleId: "com.apple.podcasts", appName: "Podcasts")
        case "sys_preview":
            launchSystemApp(bundleId: "com.apple.Preview", appName: "Preview")
        case "sys_settings":
            if let url = URL(string: "x-apple.systempreferences:") {
                NSWorkspace.shared.open(url)
            } else {
                NSWorkspace.shared.open(URL(fileURLWithPath: "/System/Applications/System Settings.app"))
            }
        case "sys_calculator":
            launchSystemApp(bundleId: "com.apple.calculator", appName: "Calculator")
        case "sys_calendar":
            launchSystemApp(bundleId: "com.apple.iCal", appName: "Calendar")
        case "sys_safari":
            launchSystemApp(bundleId: "com.apple.Safari", appName: "Safari")
        case "sys_terminal":
            launchSystemApp(bundleId: "com.apple.Terminal", appName: "Utilities/Terminal")
        case "sys_textedit":
            launchSystemApp(bundleId: "com.apple.TextEdit", appName: "TextEdit")
        case "sys_notes":
            launchSystemApp(bundleId: "com.apple.Notes", appName: "Notes")
        case "sys_messages":
            launchSystemApp(bundleId: "com.apple.MobileSMS", appName: "Messages")
        case "sys_mail":
            launchSystemApp(bundleId: "com.apple.mail", appName: "Mail")
        case "sys_music":
            launchSystemApp(bundleId: "com.apple.Music", appName: "Music")
        case "sys_finder":
            launchSystemApp(bundleId: "com.apple.finder", appName: "Finder")
        case "sys_activity":
            launchSystemApp(bundleId: "com.apple.ActivityMonitor", appName: "Utilities/Activity Monitor")
        default:
            break
        }
    }

    func deleteWorkflow(id: String) {
        // Remove associated recording directory from disk on client side if cached
        let targetWf = (allSavedWorkflows + workflows).first(where: { $0.id == id })
        if let videoPath = targetWf?.video_path, !videoPath.isEmpty {
            let videoURL = URL(fileURLWithPath: videoPath)
            let sessionDir = videoURL.deletingLastPathComponent()
            if sessionDir.lastPathComponent != "recordings" && sessionDir.path.contains("/recordings/") {
                try? FileManager.default.removeItem(at: sessionDir)
            }
        }

        // Optimistically remove from state so UI updates immediately
        self.allSavedWorkflows.removeAll(where: { $0.id == id })
        self.workflows.removeAll(where: { $0.id == id })
        if self.inspectWorkflow?.id == id {
            self.inspectWorkflow = nil
        }
        let count = self.isMemoryCommand ? self.memoryWorkflows.count : self.workflows.count
        if self.selectedIndex >= count {
            self.selectedIndex = max(0, count - 1)
        }

        guard let url = URL(string: "/api/workflows/delete", relativeTo: baseURL) else { return }
        var req = makeAuthorizedRequest(url: url, method: "POST")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["workflow_id": id])
        Task {
            _ = try? await URLSession.shared.data(for: req)
            await fetchWorkflows()
        }
    }

    func deleteSelected() {
        if let inspected = inspectWorkflow {
            deleteWorkflow(id: inspected.id)
            return
        }
        let list = isMemoryCommand ? memoryWorkflows : workflows
        guard !list.isEmpty, selectedIndex >= 0, selectedIndex < list.count else { return }
        let wf = list[selectedIndex]
        deleteWorkflow(id: wf.id)
    }

    func toggleBackgroundMode() {
        isBackgroundMode.toggle()
    }

    func toggleDictation() {
        if isListening {
            // Immediately toggle UI state off
            isListening = false
            speechManager.stopListening { [weak self] finalText in
                guard let self = self else { return }
                if !finalText.isEmpty {
                    self.query = finalText
                    Task {
                        await self.search(text: finalText)
                    }
                }
            }
        } else {
            // Immediately toggle UI state on
            isListening = true
            speechManager.startListening(
                onRecognized: { [weak self] text in
                    guard let self = self, self.isListening else { return }
                    self.query = text
                },
                onFinished: { [weak self] finalText in
                    guard let self = self else { return }
                    self.isListening = false
                    if !finalText.isEmpty {
                        self.query = finalText
                        Task {
                            await self.search(text: finalText)
                        }
                    }
                },
                onError: { [weak self] in
                    guard let self = self else { return }
                    self.isListening = false
                }
            )
        }
    }

    func toggleRecording() {
        if !isRecording {
            startRecording()
        } else {
            stopRecordingAndShowPreview()
        }
    }

    private var recordingEventMonitor: Any?
    private var recordedEvents: [[String: Any]] = []
    private var lastRecordedMovePoint: NSPoint? = nil
    private var lastRecordedMoveTime: TimeInterval = 0

    func startRecording() {
        // Clean up previous unsaved preview recording from disk
        if let oldVideoURL = self.previewVideoURL {
            try? FileManager.default.removeItem(at: oldVideoURL.deletingLastPathComponent())
        }

        // Reset inputs, preview state, and client event buffer
        self.recordedName = ""
        self.recordedTrigger = ""
        self.previewVideoURL = nil
        self.previewWorkflowId = nil
        self.recordingScore = nil
        self.recordingGrade = nil
        self.showSaveModal = false
        self.isRecording = true
        self.isMemorySpaceOpen = false
        self.recordedEvents.removeAll()
        self.lastRecordedMovePoint = nil
        self.lastRecordedMoveTime = 0
        self.startEventMonitoring()

        // Notify Python backend immediately so event capture session is active from t=0
        if let startUrl = URL(string: "/api/record/start", relativeTo: self.baseURL) {
            var startReq = makeAuthorizedRequest(url: startUrl, method: "POST")
            startReq.setValue("application/json", forHTTPHeaderField: "Content-Type")
            startReq.httpBody = try? JSONSerialization.data(withJSONObject: [:])
            Task {
                _ = try? await URLSession.shared.data(for: startReq)
            }
        }

        // Start native Swift screen recording (runs inside authorized Clio.app process).
        // This avoids the repeated "would like to record" TCC notification that occurs when
        // screencapture is spawned from Python (separate TCC identity in macOS 15 Sequoia).
        _ = SwiftScreenRecorder.shared.startRecording { [weak self] videoURL, error in
            guard let self = self else { return }

            // Handle recording start failure (e.g. TCC permission denied)
            if let error = error {
                print("startRecording: failed - \(error.localizedDescription)")
                DispatchQueue.main.async {
                    self.isRecording = false
                    // Open Screen Recording privacy settings so the user can grant access
                    if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture") {
                        NSWorkspace.shared.open(url)
                    }
                }
                return
            }

            guard let videoURL = videoURL else { return }

            // Update Python backend with the pre-allocated swift_video_path
            guard let url = URL(string: "/api/record/start", relativeTo: self.baseURL) else { return }
            var req = makeAuthorizedRequest(url: url, method: "POST")
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            let body: [String: Any] = ["swift_video_path": videoURL.path]
            req.httpBody = try? JSONSerialization.data(withJSONObject: body)
            Task {
                _ = try? await URLSession.shared.data(for: req)
            }
        }
    }

    func stopRecordingAndShowPreview() {
        self.stopEventMonitoring()
        self.isStoppingRecording = true
        self.showSaveModal = true
        self.isRecording = false

        let buffered = self.recordedEvents

        // Stop Swift-native recording first — waits for AVCaptureFileOutput to finalize
        // the .mov container (moov atom written) before notifying Python.
        SwiftScreenRecorder.shared.stopRecording { [weak self] videoURL, _ in
            guard let self = self else { return }
            let videoPath = videoURL?.path ?? ""

            guard let url = URL(string: "/api/record/stop", relativeTo: self.baseURL) else { return }
            var req = makeAuthorizedRequest(url: url, method: "POST")
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            var body: [String: Any] = [
                "name": "My Demonstrated Action",
                "trigger": "my demonstrated action",
                "events": buffered
            ]
            if !videoPath.isEmpty {
                body["swift_video_path"] = videoPath
            }
            req.httpBody = try? JSONSerialization.data(withJSONObject: body)

            Task {
                do {
                    let (data, resp) = try await URLSession.shared.data(for: req)
                    if let http = resp as? HTTPURLResponse, (200...299).contains(http.statusCode) {
                        if let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                            await MainActor.run {
                                self.isStoppingRecording = false
                                self.previewWorkflowId = json["workflow_id"] as? String
                                // Strictly prefer Swift-recorded full screen video with active windows over any fallback
                                if !videoPath.isEmpty && FileManager.default.fileExists(atPath: videoPath) {
                                    self.previewVideoURL = URL(fileURLWithPath: videoPath)
                                } else if let vPath = json["video_path"] as? String, !vPath.isEmpty && FileManager.default.fileExists(atPath: vPath) {
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
                        // Even if Python failed, show the locally-recorded video
                        if !videoPath.isEmpty {
                            self.previewVideoURL = URL(fileURLWithPath: videoPath)
                        }
                    }
                } catch {
                    await MainActor.run {
                        self.isStoppingRecording = false
                        if !videoPath.isEmpty {
                            self.previewVideoURL = URL(fileURLWithPath: videoPath)
                        }
                    }
                }
            }
        }
    }

    func cancelRecording() {
        if isRecording {
            SwiftScreenRecorder.shared.stopRecording { videoURL, _ in
                if let vUrl = videoURL {
                    try? FileManager.default.removeItem(at: vUrl.deletingLastPathComponent())
                }
            }
        }
        discardRecording()
    }

    func discardRecording() {
        let vUrl = self.previewVideoURL
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

        // Delete discarded/unsaved recording directory from disk for memory efficiency
        if let vUrl = vUrl {
            let sessionFolder = vUrl.deletingLastPathComponent()
            try? FileManager.default.removeItem(at: sessionFolder)
        }

        // Notify backend to discard and clean up disk
        guard let url = URL(string: "/api/record/discard", relativeTo: baseURL) else { return }
        var req = makeAuthorizedRequest(url: url, method: "POST")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        var body: [String: Any] = [:]
        if let wfId = wfId {
            body["workflow_id"] = wfId
        }
        if let vUrl = vUrl {
            body["video_path"] = vUrl.path
        }
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        Task {
            _ = try? await URLSession.shared.data(for: req)
            await self.fetchWorkflows()
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
            .leftMouseDown, .leftMouseUp, .rightMouseDown, .rightMouseUp, .leftMouseDragged, .keyDown, .scrollWheel, .mouseMoved
        ]) { [weak self] event in
            let screenH = NSScreen.screens.first?.frame.height ?? 900
            let eventPoint: CGPoint
            if let cgPt = event.cgEvent?.location {
                eventPoint = cgPt
            } else {
                let loc = event.locationInWindow
                eventPoint = CGPoint(x: loc.x, y: screenH - loc.y)
            }

            if event.type == .mouseMoved {
                let now = Date().timeIntervalSince1970
                if let last = self?.lastRecordedMovePoint, let lastT = self?.lastRecordedMoveTime {
                    let dx = eventPoint.x - last.x
                    let dy = eventPoint.y - last.y
                    // Throttle: minimum 5px displacement or 30ms interval (~33Hz max)
                    if (dx * dx + dy * dy < 25.0) && (now - lastT < 0.030) {
                        return
                    }
                }
                self?.lastRecordedMovePoint = eventPoint
                self?.lastRecordedMoveTime = now
            }
            Task { @MainActor [weak self] in
                self?.sendFeedEvent(event, location: eventPoint)
            }
        }
    }

    private func stopEventMonitoring() {
        if let monitor = self.recordingEventMonitor {
            NSEvent.removeMonitor(monitor)
            self.recordingEventMonitor = nil
        }
    }

    private func sendFeedEvent(_ event: NSEvent, location: CGPoint) {
        let x = Double(location.x)
        let y = Double(location.y)
        var targetBundle = NSWorkspace.shared.frontmostApplication?.bundleIdentifier ?? ""
        var targetAppName = NSWorkspace.shared.frontmostApplication?.localizedName ?? ""
        var isDockItem = false
        var dockTitle = ""
        var windowBoundsDict: [String: Double]? = nil

        // Only query element at mouse position for discrete clicks/drags (never for mouse move or keyboard typing)
        if event.type != .keyDown && event.type != .mouseMoved {
            let sys = AXUIElementCreateSystemWide()
            var elem: AXUIElement?
            if AXUIElementCopyElementAtPosition(sys, Float(x), Float(y), &elem) == .success, let elem = elem {
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
        }

        var payload: [String: Any] = [
            "x": x,
            "y": y,
            "bundle_id": targetBundle,
            "app_name": targetAppName,
            "is_dock_item": isDockItem,
            "dock_item_title": dockTitle,
            "timestamp": Date().timeIntervalSince1970
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
        } else if event.type == .leftMouseDragged {
            payload["event_type"] = "mouse_drag"
            payload["button"] = "left"
        } else if event.type == .rightMouseDown {
            payload["event_type"] = "mouse_down"
            payload["button"] = "right"
        } else if event.type == .rightMouseUp {
            payload["event_type"] = "mouse_up"
            payload["button"] = "right"
        } else if event.type == .mouseMoved {
            payload["event_type"] = "mouse_move"
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

        // Buffer locally to guarantee zero event loss
        self.recordedEvents.append(payload)

        // Asynchronously post discrete events to backend immediately.
        // High-frequency mouse moves are preserved in the buffer and sent on stop to avoid socket saturation.
        if event.type != .mouseMoved {
            guard let url = URL(string: "/api/record/feed", relativeTo: baseURL) else { return }
            var req = makeAuthorizedRequest(url: url, method: "POST")
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try? JSONSerialization.data(withJSONObject: payload)
            Task {
                _ = try? await URLSession.shared.data(for: req)
            }
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
        var req = makeAuthorizedRequest(url: url, method: "POST")
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
        var req = makeAuthorizedRequest(url: url, method: "POST")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: [
            "workflow_id": id,
            "background": isBackgroundMode
        ])
        Task {
            do {
                let (data, response) = try await URLSession.shared.data(for: req)
                if let http = response as? HTTPURLResponse,
                   let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                    await MainActor.run {
                        if let usageDict = json["usage"] as? [String: Any] {
                            self.applyUsagePayload(usageDict)
                        }
                        if http.statusCode == 429 || (json["limit_reached"] as? Bool == true) {
                            self.usageLimitReachedBanner = json["error"] as? String ?? "Daily Automation usage limit reached."
                            self.statusPillText = "QUOTA LIMIT REACHED"
                            self.isUsageLimitPanelOpen = true
                            AppDelegate.shared?.showPanel()
                            return
                        }
                        if (200...299).contains(http.statusCode), let name = json["name"] as? String {
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
        var req = makeAuthorizedRequest(url: url, method: "POST")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: [
            "query": q,
            "background": isBackgroundMode
        ])
        Task {
            do {
                let (data, response) = try await URLSession.shared.data(for: req)
                if let http = response as? HTTPURLResponse,
                   let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                    await MainActor.run {
                        if let usageDict = json["usage"] as? [String: Any] {
                            self.applyUsagePayload(usageDict)
                        }
                        if http.statusCode == 429 || (json["limit_reached"] as? Bool == true) {
                            self.usageLimitReachedBanner = json["error"] as? String ?? "Daily usage limit reached."
                            self.statusPillText = "QUOTA LIMIT REACHED"
                            self.isUsageLimitPanelOpen = true
                            AppDelegate.shared?.showPanel()
                            return
                        }
                        if (200...299).contains(http.statusCode) {
                            let mode = json["mode"] as? String ?? ""
                            if mode == "walkthrough", let plan = json["plan"] as? [String: Any] {
                                self.statusPillText = "CLIO • TEACHING"
                                AppDelegate.shared?.hidePanel()
                                if !WalkthroughOverlayManager.shared.isVisible {
                                    if let telemetry = json["telemetry"] as? [String: Any] {
                                        WalkthroughOverlayManager.shared.update(data: telemetry)
                                    } else {
                                        if let steps = plan["steps"] as? [[String: Any]], let firstStep = steps.first {
                                            self.currentStepText = firstStep["instruction"] as? String ?? ""
                                        }
                                        var overlayData: [String: Any] = [
                                            "status": "NAVIGATING",
                                            "goal": plan["goal"] as? String ?? q,
                                            "current_step_index": 1,
                                            "total_steps": (plan["steps"] as? [[String: Any]])?.count ?? 1,
                                            "steps": plan["steps"] ?? []
                                        ]
                                        if let steps = plan["steps"] as? [[String: Any]], let firstStep = steps.first {
                                            overlayData["step"] = firstStep
                                        }
                                        WalkthroughOverlayManager.shared.update(data: overlayData)
                                    }
                                }
                            } else if let name = json["name"] as? String {
                                self.currentTaskName = name
                                self.isExecuting = true
                            }
                        }
                    }
                }
            } catch { }
        }
    }


    func cancelTask() {
        guard let url = URL(string: "/api/cancel", relativeTo: baseURL) else { return }
        var req = makeAuthorizedRequest(url: url, method: "POST")
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
        var req = makeAuthorizedRequest(url: url, method: "POST")
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
                let req = makeAuthorizedRequest(url: url)
                let (stream, _) = try await URLSession.shared.bytes(for: req)
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
            let isVisible = obj["is_visible"] as? Bool ?? true
            self.vcCoords = "VC (\(Int(x)), \(Int(y))) • \(state)"
            if !isVisible || x < 0 || y < 0 {
                VirtualCursorOverlayManager.shared.hide()
            } else {
                VirtualCursorOverlayManager.shared.updatePosition(x: CGFloat(x), y: CGFloat(y), state: state)
            }
        } else if type == "walkthrough" {
            Task { @MainActor in
                WalkthroughOverlayManager.shared.update(data: obj)
                let status = (obj["status"] as? String ?? "").uppercased()
                if status == "COMPLETED" || status == "CANCELLED" {
                    self.statusPillText = "CLIO • READY"
                    self.currentStepText = ""
                    WalkthroughOverlayManager.shared.hide()
                    VirtualCursorOverlayManager.shared.hide()
                    AppDelegate.shared?.showPanel()
                } else {
                    self.statusPillText = "CLIO • TEACHING"
                    if let step = obj["step"] as? [String: Any], let inst = step["instruction"] as? String {
                        self.currentStepText = inst
                    }
                }
            }
        } else if type == "usage" {
            Task { @MainActor in
                if let usageDict = obj["usage"] as? [String: Any] {
                    self.applyUsagePayload(usageDict)
                } else {
                    self.applyUsagePayload(obj)
                }
                if obj["limit_reached"] as? Bool == true {
                    self.usageLimitReachedBanner = obj["error"] as? String ?? "Daily feature usage limit reached."
                    self.statusPillText = "QUOTA LIMIT REACHED"
                    self.isUsageLimitPanelOpen = true
                    AppDelegate.shared?.showPanel()
                }
            }
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
        var base: CGFloat = 58
        if WalkthroughOverlayManager.shared.isVisible {
            base += 32
        }
        if vm.showSaveModal {
            return base + 372
        }
        if !vm.isWalkthroughMode, let inspected = vm.inspectWorkflow {
            let count = max(1, inspected.orderedSteps.count)
            let rows = min(count, 5)
            return base + 48 + CGFloat(rows * 36) + 48 + 24
        }
        if vm.isSlashMenuVisible {
            let count = max(1, vm.matchingSlashCommands.count)
            return base + CGFloat(min(count, 8) * 44) + 38
        }
        if vm.isMemoryCommand {
            let count = vm.memoryWorkflows.count
            if count == 0 {
                return base + 140
            }
            let rows = min(count, 5)
            return base + 36 + CGFloat(rows * 48) + 34
        }
        if vm.isRecentCommand {
            let count = vm.recentWorkflows.count
            if count == 0 {
                return base + 130
            }
            let rows = min(count, 5)
            return base + 36 + CGFloat(rows * 48) + 34
        }
        if vm.isStatusCommand {
            return base + 210
        }
        if vm.isSettingsCommand || vm.isUsageLimitCommand {
            switch vm.selectedSettingsTab {
            case .usage:
                let hasBanner = (vm.usageLimitReachedBanner != nil || vm.walkthroughExhausted || vm.automationExhausted || vm.geminiRateLimited)
                return base + (hasBanner ? 215 : 180)
            case .ai:
                return base + 145
            case .walkthrough:
                return base + 125
            case .safety:
                return base + 155
            case .storage:
                return base + 115
            }
        }
        if vm.isHelpCommand {
            return base + 300
        }
        if vm.isAICommand {
            if vm.isAILoading {
                return base + 80
            } else if !vm.aiCompanionResponse.isEmpty {
                return base + 190
            } else {
                return base + 110
            }
        }
        if !vm.isWalkthroughMode {
            let trimmed = vm.query.trimmingCharacters(in: .whitespaces)
            if !trimmed.isEmpty && !vm.recommendations.isEmpty {
                let rowCount = min(vm.recommendations.count, 4)
                return base + CGFloat(rowCount * 38) + 16 + 22
            }
        }
        return base
    }

    private var hasActiveContentPanels: Bool {
        vm.showSaveModal ||
        (!vm.isWalkthroughMode && vm.inspectWorkflow != nil) ||
        vm.isSlashMenuVisible ||
        vm.isMemoryCommand ||
        vm.isRecentCommand ||
        vm.isStatusCommand ||
        vm.isUsageLimitCommand ||
        vm.isHelpCommand ||
        vm.isAICommand ||
        WalkthroughOverlayManager.shared.isVisible ||
        (!vm.isWalkthroughMode && !vm.query.trimmingCharacters(in: .whitespaces).isEmpty && !vm.recommendations.isEmpty)
    }

    private var barCornerRadius: CGFloat {
        hasActiveContentPanels ? 22 : 29
    }

    var body: some View {
        barContainerView
    }

    private var barContainerView: some View {
        mainVStackView
            .frame(width: 720)
            .background(
                RoundedRectangle(cornerRadius: barCornerRadius, style: .continuous)
                    .fill(ObsidianTheme.bgGlass)
            )
            .clipShape(RoundedRectangle(cornerRadius: barCornerRadius, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: barCornerRadius, style: .continuous)
                    .stroke(Color.white.opacity(0.18), lineWidth: 1.2)
            )
            .onAppear {
                isFieldFocused = true
            }
            .onReceive(NotificationCenter.default.publisher(for: NSNotification.Name("FocusClioField"))) { _ in
                isFieldFocused = true
            }
            .modifier(ClioBarEventModifier(vm: vm, currentTargetHeight: currentTargetHeight))
    }

    private var mainVStackView: some View {
        VStack(spacing: 0) {
            topPillBarView

            if WalkthroughOverlayManager.shared.isVisible {
                walkthroughActiveIndicatorView
            }

            contentPanelsView
        }
    }
}

// MARK: - Event and Notification Handling Modifier for Swift Compiler Performance

struct ClioBarNotificationModifier: ViewModifier {
    @ObservedObject var vm: ClioViewModel

    func body(content: Content) -> some View {
        content
            .onExitCommand {
                handleEscape()
            }
            .onReceive(NotificationCenter.default.publisher(for: NSNotification.Name("ClioBarEscapeKey"))) { _ in
                handleEscape()
            }
            .onReceive(NotificationCenter.default.publisher(for: NSNotification.Name("DeleteSelectedWorkflow"))) { _ in
                vm.deleteSelected()
            }
            .onReceive(NotificationCenter.default.publisher(for: NSNotification.Name("SelectNextWorkflow"))) { _ in
                vm.selectNextWorkflow()
            }
            .onReceive(NotificationCenter.default.publisher(for: NSNotification.Name("SelectPrevWorkflow"))) { _ in
                vm.selectPreviousWorkflow()
            }
            .onReceive(NotificationCenter.default.publisher(for: NSNotification.Name("StepNextWorkflow"))) { _ in
                if vm.inspectWorkflow != nil {
                    vm.inspectNextWorkflow()
                }
            }
            .onReceive(NotificationCenter.default.publisher(for: NSNotification.Name("StepPrevWorkflow"))) { _ in
                if vm.inspectWorkflow != nil {
                    vm.inspectPreviousWorkflow()
                }
            }
            .onReceive(NotificationCenter.default.publisher(for: NSNotification.Name("TriggerNumberedShortcut"))) { notif in
                guard vm.isSlashMenuVisible, let char = notif.object as? String, let num = Int(char), num >= 1 else { return }
                let idx = num - 1
                if idx < vm.matchingSlashCommands.count {
                    let item = vm.matchingSlashCommands[idx]
                    vm.selectedIndex = idx
                    vm.executeSlashShortcut(item)
                }
            }
            .onReceive(NotificationCenter.default.publisher(for: NSNotification.Name("ClioBarToggleSettings"))) { _ in
                vm.toggleSettingsPanel()
            }
    }

    private func handleEscape() {
        if vm.showSaveModal {
            vm.discardRecording()
        } else if vm.inspectWorkflow != nil {
            vm.dismissInspection()
        } else if vm.isSettingsPanelOpen {
            vm.isSettingsPanelOpen = false
            vm.isUsageLimitPanelOpen = false
            vm.query = ""
        } else if vm.isUsageLimitCommand {
            vm.isUsageLimitPanelOpen = false
            vm.usageLimitReachedBanner = nil
            vm.query = ""
        } else if vm.isMemoryCommand {
            vm.isMemorySpaceOpen = false
            vm.query = ""
        } else if vm.isRecentCommand {
            vm.query = ""
        } else if vm.isStatusCommand {
            vm.isStatusPanelOpen = false
            vm.query = ""
        } else if vm.isHelpCommand {
            vm.isHelpPanelOpen = false
            vm.query = ""
        } else if vm.isAICommand {
            vm.aiCompanionResponse = ""
            vm.query = ""
        } else if vm.isSlashMenuVisible {
            vm.isMemorySpaceOpen = false
            vm.isStatusPanelOpen = false
            vm.isUsageLimitPanelOpen = false
            vm.query = ""
        } else if !vm.query.isEmpty {
            vm.isMemorySpaceOpen = false
            vm.isStatusPanelOpen = false
            vm.isUsageLimitPanelOpen = false
            vm.query = ""
        } else {
            AppDelegate.shared?.hidePanel()
        }
    }
}

struct ClioBarHeightModifier: ViewModifier {
    @ObservedObject var vm: ClioViewModel
    let currentTargetHeight: CGFloat

    func body(content: Content) -> some View {
        content
            .onChange(of: currentTargetHeight) { newH in
                AppDelegate.shared?.updatePanelHeight(newH)
            }
            .onChange(of: vm.query) { _ in
                AppDelegate.shared?.updatePanelHeight(currentTargetHeight)
            }
            .onChange(of: vm.isSlashMenuVisible) { _ in
                AppDelegate.shared?.updatePanelHeight(currentTargetHeight)
            }
            .onChange(of: vm.isHelpPanelOpen) { _ in
                AppDelegate.shared?.updatePanelHeight(currentTargetHeight)
            }
            .onChange(of: vm.isStatusPanelOpen) { _ in
                AppDelegate.shared?.updatePanelHeight(currentTargetHeight)
            }
            .onChange(of: vm.isUsageLimitPanelOpen) { _ in
                AppDelegate.shared?.updatePanelHeight(currentTargetHeight)
            }
            .onChange(of: vm.isSettingsPanelOpen) { _ in
                AppDelegate.shared?.updatePanelHeight(currentTargetHeight)
            }
            .onChange(of: vm.selectedSettingsTab) { _ in
                AppDelegate.shared?.updatePanelHeight(currentTargetHeight)
            }
            .onChange(of: vm.isMemorySpaceOpen) { _ in
                AppDelegate.shared?.updatePanelHeight(currentTargetHeight)
            }
            .onChange(of: vm.recommendations.count) { _ in
                AppDelegate.shared?.updatePanelHeight(currentTargetHeight)
            }
            .onChange(of: vm.isWalkthroughMode) { _ in
                AppDelegate.shared?.updatePanelHeight(currentTargetHeight)
            }
    }
}

struct ClioBarEventModifier: ViewModifier {
    @ObservedObject var vm: ClioViewModel
    let currentTargetHeight: CGFloat

    func body(content: Content) -> some View {
        content
            .modifier(ClioBarNotificationModifier(vm: vm))
            .modifier(ClioBarHeightModifier(vm: vm, currentTargetHeight: currentTargetHeight))
    }
}

extension ClioBarView {

    // MARK: - Subviews for Fast Type Checking

    @ViewBuilder
    private var topPillBarView: some View {
        HStack(spacing: 10) {
            // Minimalist Obsidian Eclipse 'C' Monogram (Clean, unadorned mark)
            ZStack {
                Circle()
                    .fill(ObsidianTheme.surfaceElevated)
                    .overlay(
                        Circle()
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

            // Command Search Input with Smooth Animated Word Transition
            ZStack(alignment: .leading) {
                if vm.query.isEmpty {
                    Text(vm.isSettingsPanelOpen ? "Settings & Preferences..." :
                         (vm.isWalkthroughMode ? "Ask clio to teach you anything..." :
                          (vm.isMemoryCommand ? "Filter memory space (e.g. 'youtube', 'notes')..." :
                           (vm.isRecentCommand ? "Search recent automations..." :
                            (vm.isAICommand ? "Ask Clio AI anything..." :
                             (vm.isStatusCommand ? "System diagnostics & permissions..." : "Ask clio to do anything..."))))))
                        .font(.system(size: 16, weight: .regular))
                        .foregroundColor(ObsidianTheme.platinum.opacity(0.45))
                        .transition(.asymmetric(
                            insertion: .opacity.combined(with: .offset(y: 3)),
                            removal: .opacity.combined(with: .offset(y: -3))
                        ))
                        .id(vm.isSettingsPanelOpen ? "settings_placeholder" : (vm.isWalkthroughMode ? "teach_placeholder" : (vm.isMemoryCommand ? "memory_placeholder" : (vm.isRecentCommand ? "recent_placeholder" : (vm.isAICommand ? "ai_placeholder" : (vm.isStatusCommand ? "status_placeholder" : "do_placeholder"))))))
                }

                TextField(
                    "",
                    text: $vm.query
                )
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
            }

            // Interactive Walkthrough Button (Monochrome Obsidian/Platinum) - Fixed frame prevents any layout movement
            Button(action: {
                withAnimation(.easeInOut(duration: 0.25)) {
                    vm.toggleWalkthroughMode()
                }
                isFieldFocused = true
            }) {
                HStack(spacing: 5) {
                    Image(systemName: vm.isWalkthroughMode ? "graduationcap.fill" : "graduationcap")
                        .font(.system(size: 11, weight: .semibold))
                    Text(vm.isWalkthroughMode ? "TEACH ME" : "WALKTHROUGH")
                        .font(.system(size: 10, weight: .semibold, design: .monospaced))
                }
                .frame(width: 96, height: 16)
                .foregroundColor(vm.isWalkthroughMode ? ObsidianTheme.surface : ObsidianTheme.platinum)
                .padding(.horizontal, 9)
                .padding(.vertical, 5)
                .background(
                    vm.isWalkthroughMode ? ObsidianTheme.platinum : ObsidianTheme.surfaceElevated
                )
                .overlay(
                    Capsule().stroke(
                        vm.isWalkthroughMode ? ObsidianTheme.platinum : ObsidianTheme.borderSubtle,
                        lineWidth: 1
                    )
                )
                .clipShape(Capsule())
                .shadow(color: vm.isWalkthroughMode ? Color.white.opacity(0.15) : Color.clear, radius: 4)
            }
            .buttonStyle(.plain)
            .help("Toggle interactive walkthrough mode (Clio teaches you on screen)")

            // Dictation Microphone Button
            Button(action: {
                withAnimation(.easeInOut(duration: 0.2)) {
                    vm.toggleDictation()
                }
            }) {
                HStack(spacing: 5) {
                    if vm.isListening {
                        WaveformBarsView()
                        Text("LISTENING...")
                            .font(.system(size: 9, weight: .bold, design: .monospaced))
                    } else {
                        Image(systemName: "mic.fill")
                            .font(.system(size: 11, weight: .semibold))
                    }
                }
                .foregroundColor(vm.isListening ? ObsidianTheme.surface : ObsidianTheme.platinum)
                .padding(.horizontal, vm.isListening ? 10 : 9)
                .padding(.vertical, 5)
                .background(vm.isListening ? ObsidianTheme.platinum : ObsidianTheme.surfaceElevated)
                .overlay(
                    Capsule().stroke(vm.isListening ? ObsidianTheme.platinum : ObsidianTheme.borderSubtle, lineWidth: 1)
                )
                .clipShape(Capsule())
                .shadow(color: vm.isListening ? Color.white.opacity(0.25) : Color.clear, radius: 6)
            }
            .buttonStyle(.plain)
            .help(vm.isListening ? "Click to stop listening and dictate" : "Dictate command with voice")

            // Compact Micro-Pill Record Button (Hidden when Walkthrough mode is active, while audio option remains)
            if !vm.isWalkthroughMode {
                Button(action: { vm.toggleRecording() }) {
                    HStack(spacing: 4) {
                        Circle()
                            .fill(vm.isRecording ? ObsidianTheme.surface : ObsidianTheme.platinum)
                            .frame(width: 5, height: 5)
                            .opacity(vm.isRecording ? 1.0 : 0.7)
                        Text(vm.isRecording ? "STOP" : "REC")
                            .font(.system(size: 9, weight: .semibold, design: .monospaced))
                    }
                    .foregroundColor(vm.isRecording ? ObsidianTheme.surface : ObsidianTheme.platinum)
                    .padding(.horizontal, 7)
                    .padding(.vertical, 4)
                    .background(vm.isRecording ? ObsidianTheme.platinum : ObsidianTheme.surfaceElevated)
                    .overlay(
                        Capsule().stroke(vm.isRecording ? ObsidianTheme.platinum : ObsidianTheme.borderSubtle, lineWidth: 1)
                    )
                    .clipShape(Capsule())
                }
                .buttonStyle(.plain)
                .help("Record a new desktop demonstration")
                .transition(.opacity)
            }

            // Settings Button (on the right side of the REC button)
            Button(action: {
                withAnimation(.easeInOut(duration: 0.25)) {
                    vm.toggleSettingsPanel()
                }
            }) {
                Image(systemName: vm.isSettingsPanelOpen ? "gearshape.fill" : "gearshape")
                    .font(.system(size: 11, weight: .medium))
                    .foregroundColor(vm.isSettingsPanelOpen ? ObsidianTheme.surface : ObsidianTheme.platinum)
                    .frame(width: 24, height: 24)
                    .background(vm.isSettingsPanelOpen ? ObsidianTheme.platinum : ObsidianTheme.surfaceElevated)
                    .clipShape(Circle())
                    .overlay(Circle().stroke(vm.isSettingsPanelOpen ? ObsidianTheme.platinum : ObsidianTheme.borderSubtle, lineWidth: 1))
                    .contentShape(Circle())
            }
            .buttonStyle(.plain)
            .help("Settings & Preferences (Usage Limits, AI Models, Walkthrough, Safety, Storage) (⌘,)")

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
    }

    @ViewBuilder
    private var walkthroughActiveIndicatorView: some View {
        Divider().background(ObsidianTheme.borderSubtle)
        HStack(spacing: 8) {
            Image(systemName: "graduationcap.fill")
                .font(.system(size: 11))
                .foregroundColor(ObsidianTheme.platinum)
            Text("WALKTHROUGH ACTIVE:")
                .font(.system(size: 9, weight: .bold, design: .monospaced))
                .foregroundColor(ObsidianTheme.platinum)
            Text(WalkthroughOverlayManager.shared.goal.isEmpty ? WalkthroughOverlayManager.shared.instruction : WalkthroughOverlayManager.shared.goal)
                .font(.system(size: 11, weight: .medium))
                .foregroundColor(ObsidianTheme.platinum)
                .lineLimit(1)
            Spacer()
            Button(action: {
                WalkthroughOverlayManager.shared.stopWalkthrough()
            }) {
                HStack(spacing: 4) {
                    Image(systemName: "xmark.circle")
                        .font(.system(size: 9))
                    Text("Stop")
                        .font(.system(size: 10, weight: .semibold, design: .monospaced))
                }
                .foregroundColor(Color.red.opacity(0.85))
                .padding(.horizontal, 8)
                .padding(.vertical, 3)
                .background(Color.red.opacity(0.12))
                .cornerRadius(4)
                .overlay(RoundedRectangle(cornerRadius: 4).stroke(Color.red.opacity(0.3), lineWidth: 1))
            }
            .buttonStyle(.plain)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 6)
        .background(ObsidianTheme.surfaceElevated)
    }

    @ViewBuilder
    private var contentPanelsView: some View {
        if vm.showSaveModal {
            saveModalView
        } else if !vm.isWalkthroughMode, let inspected = vm.inspectWorkflow {
            inspectedWorkflowView(inspected)
        } else if vm.isSlashMenuVisible {
            slashCommandsMenuView
        } else if vm.isSettingsCommand || vm.isUsageLimitCommand {
            settingsPanelView
        } else if vm.isMemoryCommand {
            memorySpaceView
        } else if vm.isRecentCommand {
            recentWorkflowsView
        } else if vm.isStatusCommand {
            statusDiagnosticView
        } else if vm.isHelpCommand {
            helpGuideView
        } else if vm.isAICommand {
            aiCompanionView
        } else if !vm.isWalkthroughMode && !vm.query.trimmingCharacters(in: .whitespaces).isEmpty && !vm.recommendations.isEmpty {
            searchResultsView
        }
    }

    @ViewBuilder
    private var saveModalView: some View {
        Divider().background(ObsidianTheme.borderSubtle)
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                HStack(spacing: 6) {
                    Circle()
                        .fill(ObsidianTheme.platinum)
                        .frame(width: 6, height: 6)
                    Text("SCREEN RECORDING CAPTURED")
                        .font(.system(size: 10, weight: .bold, design: .monospaced))
                        .foregroundColor(ObsidianTheme.platinum)
                }

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
    }


    @ViewBuilder
    private var memorySpaceView: some View {
        Divider().background(ObsidianTheme.borderSubtle)
        VStack(spacing: 0) {
            // Header
            HStack(spacing: 8) {
                Image(systemName: "brain.head.profile")
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundColor(ObsidianTheme.platinum)
                Text("MEMORY SPACE")
                    .font(.system(size: 11, weight: .bold, design: .monospaced))
                    .foregroundColor(ObsidianTheme.platinum)

                Spacer()

                Text("\(vm.memoryWorkflows.count) SAVED ACTIONS")
                    .font(.system(size: 9, weight: .bold, design: .monospaced))
                    .foregroundColor(ObsidianTheme.slate)
                    .padding(.horizontal, 6)
                    .padding(.vertical, 2)
                    .background(ObsidianTheme.surfaceElevated)
                    .cornerRadius(4)

                Button(action: {
                    vm.query = ""
                }) {
                    Image(systemName: "xmark")
                        .font(.system(size: 9, weight: .bold))
                        .foregroundColor(ObsidianTheme.slate)
                        .frame(width: 18, height: 18)
                        .background(ObsidianTheme.surfaceElevated)
                        .clipShape(Circle())
                }
                .buttonStyle(.plain)
                .help("Exit Memory Space (Esc)")
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 8)
            .background(ObsidianTheme.surfaceElevated.opacity(0.3))

            Divider().background(ObsidianTheme.borderSubtle.opacity(0.5))

            // Workflow List or Empty State
            if vm.memoryWorkflows.isEmpty {
                VStack(spacing: 8) {
                    Image(systemName: "sparkles.rectangle.stack")
                        .font(.system(size: 24))
                        .foregroundColor(ObsidianTheme.slateDark)
                        .padding(.top, 16)
                    Text("Memory Space is Empty")
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundColor(ObsidianTheme.platinumDim)
                    Text("Demonstrate and record actions with the RECORD button above to teach Clio your workflows.")
                        .font(.system(size: 11))
                        .foregroundColor(ObsidianTheme.slate)
                        .multilineTextAlignment(.center)
                        .padding(.horizontal, 30)
                        .padding(.bottom, 16)
                }
                .frame(maxWidth: .infinity)
            } else {
                ScrollView(.vertical, showsIndicators: vm.memoryWorkflows.count > 5) {
                    VStack(spacing: 2) {
                        ForEach(Array(vm.memoryWorkflows.enumerated()), id: \.element.id) { idx, wf in
                            MemorySpaceRowView(
                                index: idx,
                                wf: wf,
                                isSelected: idx == vm.selectedIndex,
                                onPreview: { vm.previewExistingWorkflowRecording(wf) },
                                onSelect: {
                                    vm.selectedIndex = idx
                                    vm.inspectWorkflowDetails(wf)
                                },
                                onDelete: {
                                    vm.deleteWorkflow(id: wf.id)
                                }
                            )
                        }
                    }
                    .padding(.horizontal, 8)
                    .padding(.vertical, 6)
                }
                .frame(maxHeight: 240)
            }

            Divider().background(ObsidianTheme.borderSubtle.opacity(0.5))

            // Footer
            HStack {
                Text("Click any action to inspect steps • ⏎ to view • ⌫ back")
                    .font(.system(size: 9, design: .monospaced))
                    .foregroundColor(ObsidianTheme.slate)
                Spacer()
                Button(action: {
                    vm.toggleRecording()
                }) {
                    HStack(spacing: 4) {
                        Circle()
                            .fill(ObsidianTheme.platinum)
                            .frame(width: 5, height: 5)
                        Text("+ Record New")
                            .font(.system(size: 9, weight: .bold, design: .monospaced))
                    }
                    .foregroundColor(ObsidianTheme.platinum)
                    .padding(.horizontal, 6)
                    .padding(.vertical, 2)
                    .background(ObsidianTheme.surfaceElevated)
                    .cornerRadius(4)
                }
                .buttonStyle(.plain)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 6)
            .background(ObsidianTheme.surfaceElevated.opacity(0.2))
        }
        .background(ObsidianTheme.cardGlass)
    }

    @ViewBuilder
    private func inspectedWorkflowView(_ inspected: WorkflowItem) -> some View {
        Divider().background(ObsidianTheme.borderSubtle)
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .center, spacing: 8) {
                Image(systemName: "list.bullet.rectangle.fill")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundColor(ObsidianTheme.platinum)

                VStack(alignment: .leading, spacing: 2) {
                    Text(inspected.displayName)
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundColor(ObsidianTheme.platinum)
                    if let trig = inspected.canonical_trigger, !trig.isEmpty {
                        Text("trigger: \"\(trig)\"")
                            .font(.system(size: 10, design: .monospaced))
                            .foregroundColor(ObsidianTheme.slate)
                    }
                }

                Spacer()

                if let idx = vm.currentInspectedIndex {
                    let total = vm.isMemoryCommand ? vm.memoryWorkflows.count : vm.workflows.count
                    if total > 1 {
                        HStack(spacing: 5) {
                            Button(action: { vm.inspectPreviousWorkflow() }) {
                                Image(systemName: "chevron.left")
                                    .font(.system(size: 9, weight: .bold))
                                    .foregroundColor(idx > 0 ? ObsidianTheme.platinum : ObsidianTheme.slateDark)
                                    .frame(width: 20, height: 20)
                                    .background(ObsidianTheme.surfaceElevated)
                                    .clipShape(Circle())
                            }
                            .buttonStyle(.plain)
                            .disabled(idx == 0)
                            .help("Previous action in memory (Left Arrow)")

                            Text("\(idx + 1) of \(total)")
                                .font(.system(size: 10, weight: .bold, design: .monospaced))
                                .foregroundColor(ObsidianTheme.slate)

                            Button(action: { vm.inspectNextWorkflow() }) {
                                Image(systemName: "chevron.right")
                                    .font(.system(size: 9, weight: .bold))
                                    .foregroundColor(idx < total - 1 ? ObsidianTheme.platinum : ObsidianTheme.slateDark)
                                    .frame(width: 20, height: 20)
                                    .background(ObsidianTheme.surfaceElevated)
                                    .clipShape(Circle())
                            }
                            .buttonStyle(.plain)
                            .disabled(idx >= total - 1)
                            .help("Next action in memory (Right Arrow)")
                        }
                        .padding(.horizontal, 4)
                    }
                }

                if let vPath = inspected.video_path, !vPath.isEmpty {
                    Button(action: {
                        vm.previewExistingWorkflowRecording(inspected)
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
                    .help("View recorded demonstration video")
                }

                Text("\(inspected.orderedSteps.count) steps")
                    .font(.system(size: 10, weight: .medium, design: .monospaced))
                    .foregroundColor(ObsidianTheme.slate)
                    .padding(.horizontal, 6)
                    .padding(.vertical, 2)
                    .background(ObsidianTheme.surfaceElevated)
                    .cornerRadius(4)
            }
            .padding(.horizontal, 14)
            .padding(.top, 10)

            ScrollView(.vertical, showsIndicators: inspected.orderedSteps.count > 5) {
                VStack(spacing: 4) {
                    if inspected.orderedSteps.isEmpty {
                        HStack {
                            Spacer()
                            Text("No dissected steps recorded for this workflow.")
                                .font(.system(size: 11))
                                .foregroundColor(ObsidianTheme.slate)
                                .padding(.vertical, 12)
                            Spacer()
                        }
                    } else {
                        ForEach(inspected.orderedSteps) { step in
                            HStack(spacing: 8) {
                                Text("\(step.displayOrder)")
                                    .font(.system(size: 10, weight: .bold, design: .monospaced))
                                    .foregroundColor(ObsidianTheme.platinum)
                                    .frame(width: 18, height: 18)
                                    .background(ObsidianTheme.zinc)
                                    .clipShape(Circle())

                                Image(systemName: step.actionIcon)
                                    .font(.system(size: 11))
                                    .foregroundColor(ObsidianTheme.slate)
                                    .frame(width: 16)

                                Text(step.displayDescription)
                                    .font(.system(size: 11, weight: .regular))
                                    .foregroundColor(ObsidianTheme.platinumDim)
                                    .lineLimit(1)

                                Spacer()

                                Text(step.actionBadge)
                                    .font(.system(size: 8, weight: .bold, design: .monospaced))
                                    .foregroundColor(ObsidianTheme.slate)
                                    .padding(.horizontal, 4)
                                    .padding(.vertical, 1)
                                    .background(ObsidianTheme.surfaceElevated)
                                    .cornerRadius(3)
                            }
                            .padding(.horizontal, 10)
                            .padding(.vertical, 5)
                            .background(ObsidianTheme.surfaceElevated.opacity(0.4))
                            .cornerRadius(6)
                        }
                    }
                }
                .padding(.horizontal, 14)
            }
            .frame(maxHeight: 180)

            HStack(spacing: 8) {
                Button(action: {
                    vm.dismissInspection()
                }) {
                    HStack(spacing: 4) {
                        Image(systemName: "arrow.left")
                            .font(.system(size: 9, weight: .semibold))
                        Text(vm.isMemoryCommand ? "Memory Space" : "Back")
                            .font(.system(size: 11, weight: .medium))
                        Text("Esc")
                            .font(.system(size: 9, design: .monospaced))
                            .foregroundColor(ObsidianTheme.slate)
                    }
                    .foregroundColor(ObsidianTheme.slate)
                    .padding(.horizontal, 10)
                    .padding(.vertical, 5)
                    .background(ObsidianTheme.surfaceElevated)
                    .cornerRadius(6)
                    .overlay(RoundedRectangle(cornerRadius: 6).stroke(ObsidianTheme.borderSubtle, lineWidth: 1))
                }
                .buttonStyle(.plain)

                // Delete Action Button
                Button(action: {
                    vm.deleteWorkflow(id: inspected.id)
                }) {
                    HStack(spacing: 4) {
                        Image(systemName: "trash")
                            .font(.system(size: 9))
                        Text("Delete Action")
                            .font(.system(size: 11, weight: .medium))
                    }
                    .foregroundColor(Color.red.opacity(0.85))
                    .padding(.horizontal, 10)
                    .padding(.vertical, 5)
                    .background(ObsidianTheme.surfaceElevated)
                    .cornerRadius(6)
                    .overlay(RoundedRectangle(cornerRadius: 6).stroke(Color.red.opacity(0.3), lineWidth: 1))
                }
                .buttonStyle(.plain)
                .help("Delete this action and associated recording")

                Spacer()

                Button(action: {
                    vm.runInspectedWorkflow()
                }) {
                    HStack(spacing: 5) {
                        Image(systemName: "play.fill")
                            .font(.system(size: 9))
                        Text("Perform Action")
                            .font(.system(size: 11, weight: .bold))
                        Text("⏎")
                            .font(.system(size: 10, design: .monospaced))
                            .foregroundColor(ObsidianTheme.slateDark)
                    }
                    .foregroundColor(ObsidianTheme.surface)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 6)
                    .background(ObsidianTheme.platinum)
                    .cornerRadius(6)
                }
                .buttonStyle(.plain)
                .help("Perform this action in the background based on the dissected steps")
            }
            .padding(.horizontal, 14)
            .padding(.bottom, 10)
        }
        .background(ObsidianTheme.cardGlass)
    }

    @ViewBuilder
    private var searchResultsView: some View {
        Divider().background(ObsidianTheme.borderSubtle)
        VStack(spacing: 2) {
            ForEach(Array(vm.recommendations.prefix(4).enumerated()), id: \.element.id) { idx, wf in
                SearchResultRowView(
                    wf: wf,
                    isSelected: idx == vm.selectedIndex,
                    onPreview: { vm.previewExistingWorkflowRecording(wf) },
                    onSelect: {
                        vm.selectedIndex = idx
                        vm.executeWorkflowOrSystemAction(wf)
                    },
                    onDelete: {
                        vm.deleteWorkflow(id: wf.id)
                    }
                )
            }
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 6)
        .background(ObsidianTheme.cardGlass)

        Divider().background(ObsidianTheme.borderSubtle.opacity(0.35))
        HStack {
            Text("↑↓ to navigate • ⏎ to perform • Esc to clear")
                .font(.system(size: 9, design: .monospaced))
                .foregroundColor(ObsidianTheme.slate)
            Spacer()
            Text("\(vm.recommendations.count) MATCHES")
                .font(.system(size: 8.5, weight: .bold, design: .monospaced))
                .foregroundColor(ObsidianTheme.slateDark)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 4)
        .background(ObsidianTheme.surfaceElevated.opacity(0.2))
    }

    @ViewBuilder
    private var slashCommandsMenuView: some View {
        Divider().background(ObsidianTheme.borderSubtle)
        VStack(spacing: 0) {
            // Command Items in ScrollView for smooth scrolling when multiple shortcuts exist
            ScrollView(.vertical, showsIndicators: vm.matchingSlashCommands.count > 8) {
                VStack(spacing: 2) {
                    ForEach(Array(vm.matchingSlashCommands.enumerated()), id: \.element.id) { idx, item in
                        SlashCommandRowView(
                            item: item,
                            isSelected: idx == vm.selectedIndex,
                            onSelect: {
                                vm.selectedIndex = idx
                                vm.executeSlashShortcut(item)
                            }
                        )
                    }
                }
                .padding(.horizontal, 8)
                .padding(.vertical, 6)
            }
            .frame(maxHeight: CGFloat(min(vm.matchingSlashCommands.count, 8) * 44 + 12))

            Divider().background(ObsidianTheme.borderSubtle.opacity(0.35))

            // Footer
            HStack {
                Text("↑↓ Navigate • ⏎ Perform • Esc Clear")
                    .font(.system(size: 9, design: .monospaced))
                    .foregroundColor(ObsidianTheme.slate)
                Spacer()
                Text("\(vm.matchingSlashCommands.count) COMMANDS")
                    .font(.system(size: 8.5, weight: .bold, design: .monospaced))
                    .foregroundColor(ObsidianTheme.slateDark)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 5)
            .background(ObsidianTheme.surfaceElevated.opacity(0.2))
        }
        .background(ObsidianTheme.cardGlass)
    }

    @ViewBuilder
    private var recentWorkflowsView: some View {
        Divider().background(ObsidianTheme.borderSubtle)
        VStack(spacing: 0) {
            // Header
            HStack(spacing: 6) {
                Image(systemName: "clock.arrow.circlepath")
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundColor(ObsidianTheme.platinum)
                Text("RECENT AUTOMATIONS")
                    .font(.system(size: 9.5, weight: .bold, design: .monospaced))
                    .foregroundColor(ObsidianTheme.platinum)
                Spacer()
                Text("⏎ TO RUN • ↑↓ NAVIGATE")
                    .font(.system(size: 8.5, weight: .medium, design: .monospaced))
                    .foregroundColor(ObsidianTheme.slateDark)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 7)
            .background(ObsidianTheme.surfaceElevated.opacity(0.35))

            Divider().background(ObsidianTheme.borderSubtle.opacity(0.4))

            if vm.recentWorkflows.isEmpty {
                VStack(spacing: 6) {
                    Image(systemName: "clock")
                        .font(.system(size: 18))
                        .foregroundColor(ObsidianTheme.slateDark)
                    Text("No Recent Automations Found")
                        .font(.system(size: 12, weight: .medium))
                        .foregroundColor(ObsidianTheme.slate)
                    Text("Capture your first workflow with /record")
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundColor(ObsidianTheme.slateDark)
                }
                .frame(maxWidth: .infinity)
                .padding(.vertical, 24)
            } else {
                VStack(spacing: 2) {
                    ForEach(Array(vm.recentWorkflows.prefix(5).enumerated()), id: \.element.id) { idx, wf in
                        MemorySpaceRowView(
                            index: idx,
                            wf: wf,
                            isSelected: idx == vm.selectedIndex,
                            onPreview: {
                                vm.previewExistingWorkflowRecording(wf)
                            },
                            onSelect: {
                                vm.selectedIndex = idx
                                vm.executeWorkflowOrSystemAction(wf)
                            },
                            onDelete: {
                                vm.deleteWorkflow(id: wf.id)
                            }
                        )
                    }
                }
                .padding(.horizontal, 8)
                .padding(.vertical, 6)
            }

            Divider().background(ObsidianTheme.borderSubtle.opacity(0.35))

            // Footer
            HStack {
                Text("↑↓ Navigate • ⏎ Inspect & Run • Esc Clear")
                    .font(.system(size: 9, design: .monospaced))
                    .foregroundColor(ObsidianTheme.slate)
                Spacer()
                Text("\(vm.recentWorkflows.count) AUTOMATIONS")
                    .font(.system(size: 8.5, weight: .bold, design: .monospaced))
                    .foregroundColor(ObsidianTheme.slateDark)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 4)
            .background(ObsidianTheme.surfaceElevated.opacity(0.2))
        }
        .background(ObsidianTheme.cardGlass)
    }

    @ViewBuilder
    private var statusDiagnosticView: some View {
        Divider().background(ObsidianTheme.borderSubtle)
        VStack(spacing: 0) {
            // Header
            HStack(spacing: 6) {
                Image(systemName: "heart.text.square")
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundColor(ObsidianTheme.platinum)
                Text("SYSTEM HEALTH & PERMISSIONS")
                    .font(.system(size: 9.5, weight: .bold, design: .monospaced))
                    .foregroundColor(ObsidianTheme.platinum)
                Spacer()
                Text("DIAGNOSTIC TELEMETRY")
                    .font(.system(size: 8.5, weight: .medium, design: .monospaced))
                    .foregroundColor(ObsidianTheme.slateDark)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 7)
            .background(ObsidianTheme.surfaceElevated.opacity(0.35))

            Divider().background(ObsidianTheme.borderSubtle.opacity(0.4))

            VStack(spacing: 8) {
                // Row 1: macOS Accessibility
                HStack(spacing: 10) {
                    ZStack {
                        RoundedRectangle(cornerRadius: 6)
                            .fill(ObsidianTheme.surfaceElevated)
                            .frame(width: 28, height: 28)
                        Image(systemName: vm.systemAccessibilityGranted ? "lock.open.fill" : "exclamationmark.triangle.fill")
                            .font(.system(size: 12))
                            .foregroundColor(vm.systemAccessibilityGranted ? ObsidianTheme.platinum : Color.orange)
                    }

                    VStack(alignment: .leading, spacing: 2) {
                        Text("macOS Accessibility Permissions")
                            .font(.system(size: 12, weight: .medium))
                            .foregroundColor(ObsidianTheme.platinum)
                        Text(vm.systemAccessibilityGranted ? "Granted — Synthetic events authorized" : "Required for keyboard and mouse automation")
                            .font(.system(size: 10, design: .monospaced))
                            .foregroundColor(ObsidianTheme.slate)
                    }

                    Spacer()

                    if !vm.systemAccessibilityGranted {
                        Button(action: { vm.openAccessibilitySettings() }) {
                            Text("Open Settings")
                                .font(.system(size: 9.5, weight: .bold, design: .monospaced))
                                .foregroundColor(ObsidianTheme.surface)
                                .padding(.horizontal, 8)
                                .padding(.vertical, 4)
                                .background(ObsidianTheme.platinum)
                                .cornerRadius(5)
                        }
                        .buttonStyle(.plain)
                    } else {
                        Text("GRANTED")
                            .font(.system(size: 8.5, weight: .bold, design: .monospaced))
                            .foregroundColor(ObsidianTheme.surface)
                            .padding(.horizontal, 6)
                            .padding(.vertical, 2.5)
                            .background(ObsidianTheme.platinum)
                            .cornerRadius(4)
                    }
                }

                Divider().background(ObsidianTheme.borderSubtle.opacity(0.2))

                // Row 2: Local Engine Daemon
                HStack(spacing: 10) {
                    ZStack {
                        RoundedRectangle(cornerRadius: 6)
                            .fill(ObsidianTheme.surfaceElevated)
                            .frame(width: 28, height: 28)
                        Image(systemName: "cpu")
                            .font(.system(size: 12))
                            .foregroundColor(ObsidianTheme.platinum)
                    }

                    VStack(alignment: .leading, spacing: 2) {
                        Text("Clio Engine Daemon")
                            .font(.system(size: 12, weight: .medium))
                            .foregroundColor(ObsidianTheme.platinum)
                        Text(vm.isConnected ? "Connected to 127.0.0.1:8765 • SSE Stream Active" : "Connecting to local service...")
                            .font(.system(size: 10, design: .monospaced))
                            .foregroundColor(ObsidianTheme.slate)
                    }

                    Spacer()

                    Text(vm.isConnected ? "ONLINE" : "CONNECTING")
                        .font(.system(size: 8.5, weight: .bold, design: .monospaced))
                        .foregroundColor((vm.isConnected) ? ObsidianTheme.surface : ObsidianTheme.slateDark)
                        .padding(.horizontal, 6)
                        .padding(.vertical, 2.5)
                        .background((vm.isConnected) ? ObsidianTheme.platinum : ObsidianTheme.surfaceElevated)
                        .cornerRadius(4)
                }

                Divider().background(ObsidianTheme.borderSubtle.opacity(0.2))

                // Row 3: AI Engine Status
                HStack(spacing: 10) {
                    ZStack {
                        RoundedRectangle(cornerRadius: 6)
                            .fill(ObsidianTheme.surfaceElevated)
                            .frame(width: 28, height: 28)
                        Image(systemName: "sparkles")
                            .font(.system(size: 12))
                            .foregroundColor(ObsidianTheme.platinum)
                    }

                    VStack(alignment: .leading, spacing: 2) {
                        Text("AI Reasoning Engine")
                            .font(.system(size: 12, weight: .medium))
                            .foregroundColor(ObsidianTheme.platinum)
                        Text("\(vm.aiProviderName) • Provider \(vm.aiProviderStatus)")
                            .font(.system(size: 10, design: .monospaced))
                            .foregroundColor(ObsidianTheme.slate)
                    }

                    Spacer()

                    Text("READY")
                        .font(.system(size: 8.5, weight: .bold, design: .monospaced))
                        .foregroundColor(ObsidianTheme.surface)
                        .padding(.horizontal, 6)
                        .padding(.vertical, 2.5)
                        .background(ObsidianTheme.platinum)
                        .cornerRadius(4)
                }
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 10)

            Divider().background(ObsidianTheme.borderSubtle.opacity(0.35))

            // Footer
            HStack {
                Button(action: { vm.checkSystemHealth() }) {
                    HStack(spacing: 4) {
                        Image(systemName: "arrow.clockwise")
                            .font(.system(size: 8))
                        Text("Re-check System")
                            .font(.system(size: 9, design: .monospaced))
                    }
                    .foregroundColor(ObsidianTheme.platinumDim)
                }
                .buttonStyle(.plain)

                Spacer()

                Button(action: {
                    vm.isStatusPanelOpen = false
                    vm.query = ""
                }) {
                    Text("Dismiss (Esc)")
                        .font(.system(size: 8.5, weight: .semibold, design: .monospaced))
                        .foregroundColor(ObsidianTheme.slateDark)
                }
                .buttonStyle(.plain)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 5)
            .background(ObsidianTheme.surfaceElevated.opacity(0.2))
        }
        .background(ObsidianTheme.cardGlass)
    }

    @ViewBuilder
    private var topBarQuotaPillView: some View {
        let isExhausted = vm.walkthroughExhausted || vm.automationExhausted
        let isLow = vm.walkthroughRemaining <= 5 || vm.automationRemaining <= 5 || vm.geminiRateLimited
        let dotColor: Color = isExhausted
            ? Color.red.opacity(0.9)
            : (isLow ? Color.orange.opacity(0.9) : Color(red: 0.06, green: 0.73, blue: 0.51))

        Button(action: {
            vm.toggleUsageLimitPanel()
            isFieldFocused = true
        }) {
            HStack(spacing: 5) {
                Circle()
                    .fill(dotColor)
                    .frame(width: 5.5, height: 5.5)

                Image(systemName: "graduationcap.fill")
                    .font(.system(size: 8.5, weight: .semibold))
                Text("\(vm.walkthroughRemaining)/\(vm.walkthroughDailyLimit)")
                    .font(.system(size: 9.5, weight: .bold, design: .monospaced))

                Text("•")
                    .font(.system(size: 9, weight: .bold))
                    .foregroundColor(vm.isUsageLimitCommand ? ObsidianTheme.surface.opacity(0.6) : ObsidianTheme.slateDark)

                Image(systemName: "bolt.fill")
                    .font(.system(size: 8.5, weight: .semibold))
                Text("\(vm.automationRemaining)/\(vm.automationDailyLimit)")
                    .font(.system(size: 9.5, weight: .bold, design: .monospaced))
            }
            .foregroundColor(vm.isUsageLimitCommand ? ObsidianTheme.surface : ObsidianTheme.platinum)
            .padding(.horizontal, 8)
            .padding(.vertical, 5)
            .background(vm.isUsageLimitCommand ? ObsidianTheme.platinum : ObsidianTheme.surfaceElevated)
            .overlay(
                Capsule().stroke(
                    isExhausted ? Color.red.opacity(0.55) : (vm.isUsageLimitCommand ? ObsidianTheme.platinum : ObsidianTheme.borderSubtle),
                    lineWidth: 1
                )
            )
            .clipShape(Capsule())
        }
        .buttonStyle(.plain)
        .help("App Usage Limits & Gemini Free-Tier Quota — Walkthrough: \(vm.walkthroughRemaining)/\(vm.walkthroughDailyLimit) left • Automation: \(vm.automationRemaining)/\(vm.automationDailyLimit) left (/limits)")
    }

    @ViewBuilder
    private var usageLimitPanelView: some View {
        VStack(spacing: 0) {
            // Header
            HStack(spacing: 6) {
                Circle()
                    .fill(Color(red: 0.06, green: 0.73, blue: 0.51))
                    .frame(width: 5, height: 5)
                Text("USAGE LIMITS & GEMINI FREE-TIER QUOTA")
                    .font(.system(size: 8.5, weight: .bold, design: .monospaced))
                    .foregroundColor(ObsidianTheme.platinumDim)

                Spacer()

                HStack(spacing: 3) {
                    Image(systemName: "lock.fill")
                        .font(.system(size: 7.5))
                    Text("CAP 25 • FIXED")
                        .font(.system(size: 8, weight: .bold, design: .monospaced))
                }
                .foregroundColor(ObsidianTheme.platinumDim)
                .padding(.horizontal, 5)
                .padding(.vertical, 2)
                .background(ObsidianTheme.surfaceElevated.opacity(0.8))
                .cornerRadius(4)
                .help("NON-NEGOTIABLE • STRICT DAILY CEILING: 25 USES")
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 6)
            .background(ObsidianTheme.surfaceElevated.opacity(0.25))

            Divider().background(ObsidianTheme.borderSubtle.opacity(0.4))

            if vm.usageLimitReachedBanner != nil || vm.walkthroughExhausted || vm.automationExhausted || vm.geminiRateLimited {
                usageLimitWarningBannerView
                Divider().background(ObsidianTheme.borderSubtle.opacity(0.3))
            }

            // Dual Feature Quota Cards (Walkthrough & Automation)
            HStack(spacing: 10) {
                usageFeatureQuotaCard(
                    featureKey: "walkthrough",
                    title: "WALKTHROUGH USES",
                    iconName: "graduationcap.fill",
                    used: vm.walkthroughUsed,
                    remaining: vm.walkthroughRemaining,
                    dailyLimit: vm.walkthroughDailyLimit,
                    exhausted: vm.walkthroughExhausted
                )

                usageFeatureQuotaCard(
                    featureKey: "automation",
                    title: "AUTOMATION USES",
                    iconName: "bolt.fill",
                    used: vm.automationUsed,
                    remaining: vm.automationRemaining,
                    dailyLimit: vm.automationDailyLimit,
                    exhausted: vm.automationExhausted
                )
            }
            .padding(.horizontal, 14)
            .padding(.top, 8)
            .padding(.bottom, 6)

            // Minimal Gemini Telemetry Strip
            HStack(spacing: 8) {
                Image(systemName: "sparkles")
                    .font(.system(size: 9))
                    .foregroundColor(ObsidianTheme.platinum)
                Text(vm.geminiActiveModel)
                    .font(.system(size: 8.5, weight: .bold, design: .monospaced))
                    .foregroundColor(ObsidianTheme.platinum)
                Text("•")
                    .foregroundColor(ObsidianTheme.slateDark)
                Text("\(vm.geminiCallsToday)/\(vm.geminiDailyLimit) calls")
                    .font(.system(size: 8.5, design: .monospaced))
                    .foregroundColor(ObsidianTheme.platinumDim)
                Spacer()
                Text(vm.geminiRateLimited ? "Cooldown (\(vm.geminiCooldownSeconds)s)" : "Ready")
                    .font(.system(size: 8, weight: .bold, design: .monospaced))
                    .foregroundColor(vm.geminiRateLimited ? Color.orange : Color(red: 0.06, green: 0.73, blue: 0.51))
                    .padding(.horizontal, 5)
                    .padding(.vertical, 1.5)
                    .background(ObsidianTheme.surfaceElevated)
                    .cornerRadius(3)
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 5)
            .background(ObsidianTheme.surface.opacity(0.8))
            .overlay(
                RoundedRectangle(cornerRadius: 6)
                    .stroke(ObsidianTheme.borderSubtle, lineWidth: 1)
            )
            .cornerRadius(6)
            .padding(.horizontal, 14)
            .padding(.bottom, 8)
        }
        .background(ObsidianTheme.cardGlass)
    }

    @ViewBuilder
    private var usageLimitWarningBannerView: some View {
        let message = vm.usageLimitReachedBanner ?? (
            vm.walkthroughExhausted ? "Daily Walkthrough limit reached (\(vm.walkthroughUsed)/\(vm.walkthroughDailyLimit))." :
            (vm.automationExhausted ? "Daily Automation limit reached (\(vm.automationUsed)/\(vm.automationDailyLimit))." :
             "Gemini rate limit cooldown (\(vm.geminiCooldownSeconds)s) — Local engine active.")
        )
        HStack(spacing: 6) {
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.system(size: 10, weight: .bold))
                .foregroundColor(Color.orange)

            Text(message)
                .font(.system(size: 9.5, weight: .semibold, design: .monospaced))
                .foregroundColor(ObsidianTheme.platinum)
                .lineLimit(1)

            Spacer()

            Text("RESETS MIDNIGHT")
                .font(.system(size: 7.5, weight: .bold, design: .monospaced))
                .foregroundColor(ObsidianTheme.platinumDim)
                .padding(.horizontal, 5)
                .padding(.vertical, 2)
                .background(ObsidianTheme.surfaceElevated)
                .cornerRadius(3)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 5)
        .background(Color.orange.opacity(0.12))
    }

    @ViewBuilder
    private func usageFeatureQuotaCard(
        featureKey: String,
        title: String,
        iconName: String,
        used: Int,
        remaining: Int,
        dailyLimit: Int,
        exhausted: Bool
    ) -> some View {
        let ratio = CGFloat(max(0, min(remaining, dailyLimit))) / CGFloat(max(1, dailyLimit))
        let isLow = !exhausted && remaining <= 5
        let barColor: Color = exhausted
            ? Color.red.opacity(0.85)
            : (isLow ? Color.orange.opacity(0.9) : ObsidianTheme.platinum)

        VStack(alignment: .leading, spacing: 6) {
            // Header
            HStack(spacing: 5) {
                Image(systemName: iconName)
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundColor(exhausted ? Color.red.opacity(0.85) : ObsidianTheme.platinum)
                Text(title)
                    .font(.system(size: 9.5, weight: .bold, design: .monospaced))
                    .foregroundColor(ObsidianTheme.platinum)
                Spacer()
                if exhausted {
                    Text("0 LEFT")
                        .font(.system(size: 7.5, weight: .bold, design: .monospaced))
                        .foregroundColor(Color.red)
                }
            }

            // Numbers
            HStack(alignment: .firstTextBaseline, spacing: 4) {
                Text("\(remaining)")
                    .font(.system(size: 19, weight: .bold, design: .monospaced))
                    .foregroundColor(exhausted ? Color.red.opacity(0.9) : ObsidianTheme.platinum)
                Text("/ \(dailyLimit) left")
                    .font(.system(size: 9.5, weight: .semibold, design: .monospaced))
                    .foregroundColor(ObsidianTheme.platinumDim)
                Spacer()
                Text("\(used) used")
                    .font(.system(size: 8.5, design: .monospaced))
                    .foregroundColor(ObsidianTheme.slate)
            }

            // Progress Bar
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    Capsule()
                        .fill(Color.white.opacity(0.10))
                    Capsule()
                        .fill(barColor)
                        .frame(width: max(3, geo.size.width * ratio))
                }
            }
            .frame(height: 4)

            // Badge
            HStack {
                Spacer()
                Text("CAP \(dailyLimit) • FIXED")
                    .font(.system(size: 7.5, weight: .bold, design: .monospaced))
                    .foregroundColor(ObsidianTheme.slate)
                    .help("NON-NEGOTIABLE • STRICT DAILY CEILING: 25 USES")
            }
        }
        .padding(9)
        .frame(maxWidth: .infinity)
        .background(ObsidianTheme.surface.opacity(0.85))
        .overlay(
            RoundedRectangle(cornerRadius: 8)
                .stroke(exhausted ? Color.red.opacity(0.45) : ObsidianTheme.borderSubtle, lineWidth: 1)
        )
        .cornerRadius(8)
    }

    @ViewBuilder
    private var settingsPanelView: some View {
        Divider().background(ObsidianTheme.borderSubtle)
        VStack(spacing: 0) {
            // Header & Tab Navigation
            HStack(spacing: 8) {
                HStack(spacing: 5) {
                    Image(systemName: "gearshape.fill")
                        .font(.system(size: 10, weight: .bold))
                        .foregroundColor(ObsidianTheme.platinum)
                    Text("SETTINGS")
                        .font(.system(size: 9.5, weight: .bold, design: .monospaced))
                        .foregroundColor(ObsidianTheme.platinum)
                }

                Spacer()

                // Horizontal Tab Buttons (Obsidian Segmented Navigation)
                HStack(spacing: 4) {
                    ForEach(SettingsTab.allCases) { tab in
                        Button(action: {
                            withAnimation(.easeInOut(duration: 0.15)) {
                                vm.selectedSettingsTab = tab
                                vm.query = ""
                            }
                        }) {
                            HStack(spacing: 4) {
                                Image(systemName: tab.icon)
                                    .font(.system(size: 8.5))
                                Text(tab.rawValue)
                                    .font(.system(size: 8.5, weight: .bold, design: .monospaced))
                                if vm.selectedSettingsTab == tab {
                                    Circle()
                                        .fill(Color.cyan)
                                        .frame(width: 4, height: 4)
                                }
                            }
                            .foregroundColor(vm.selectedSettingsTab == tab ? ObsidianTheme.surface : ObsidianTheme.platinum)
                            .padding(.horizontal, 6)
                            .padding(.vertical, 3)
                            .background(vm.selectedSettingsTab == tab ? ObsidianTheme.platinum : ObsidianTheme.surfaceElevated)
                            .cornerRadius(4)
                        }
                        .buttonStyle(.plain)
                    }
                }

                Spacer()

                Button(action: {
                    vm.isSettingsPanelOpen = false
                    vm.isUsageLimitPanelOpen = false
                    if ["/settings", "/preferences", "/config", "/limits", "/quota", "/usage"].contains(vm.query.trimmingCharacters(in: .whitespaces).lowercased()) {
                        vm.query = ""
                    }
                }) {
                    Text("✕")
                        .font(.system(size: 9, weight: .bold))
                        .foregroundColor(ObsidianTheme.slateDark)
                        .padding(.horizontal, 4)
                }
                .buttonStyle(.plain)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 6)
            .background(ObsidianTheme.surfaceElevated.opacity(0.35))

            Divider().background(ObsidianTheme.borderSubtle.opacity(0.4))

            // Body Switcher
            switch vm.selectedSettingsTab {
            case .usage:
                usageLimitPanelView
            case .ai:
                settingsAIModelsView
            case .walkthrough:
                settingsWalkthroughView
            case .safety:
                settingsSafetyView
            case .storage:
                settingsStorageView
            }

            Divider().background(ObsidianTheme.borderSubtle.opacity(0.35))

            // Footer
            HStack {
                Text("ESC Close • TAB Next • ⌘, Settings & Preferences")
                    .font(.system(size: 8.5, design: .monospaced))
                    .foregroundColor(ObsidianTheme.slate)
                Spacer()
                Text("Clio")
                    .font(.system(size: 8, weight: .bold, design: .monospaced))
                    .foregroundColor(ObsidianTheme.slateDark)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 3.5)
            .background(ObsidianTheme.surfaceElevated.opacity(0.2))
        }
        .background(ObsidianTheme.cardGlass)
    }

    @ViewBuilder
    private var settingsAIModelsView: some View {
        VStack(spacing: 8) {
            // 3 Model Cards
            HStack(spacing: 6) {
                modelSelectCard(
                    modelId: "gemini-2.5-flash",
                    title: "Gemini 2.5 Flash",
                    tag: "Fast",
                    isSelected: vm.geminiActiveModel == "gemini-2.5-flash"
                )
                modelSelectCard(
                    modelId: "gemini-2.5-flash-lite",
                    title: "Flash-Lite",
                    tag: "Instant",
                    isSelected: vm.geminiActiveModel == "gemini-2.5-flash-lite"
                )
                modelSelectCard(
                    modelId: "gemini-1.5-pro",
                    title: "1.5 Pro",
                    tag: "Deep",
                    isSelected: vm.geminiActiveModel == "gemini-1.5-pro"
                )
            }
            .padding(.horizontal, 14)
            .padding(.top, 8)

            // API Key & Test Connection
            HStack(spacing: 6) {
                Image(systemName: "key.fill")
                    .font(.system(size: 9))
                    .foregroundColor(ObsidianTheme.slate)

                if vm.aiKeyObscured {
                    SecureField("Gemini API Key...", text: $vm.aiApiKeyInput)
                        .textFieldStyle(.plain)
                        .font(.system(size: 10.5, design: .monospaced))
                        .foregroundColor(ObsidianTheme.platinum)
                } else {
                    TextField("Gemini API Key...", text: $vm.aiApiKeyInput)
                        .textFieldStyle(.plain)
                        .font(.system(size: 10.5, design: .monospaced))
                        .foregroundColor(ObsidianTheme.platinum)
                }

                Button(action: { vm.aiKeyObscured.toggle() }) {
                    Image(systemName: vm.aiKeyObscured ? "eye.slash" : "eye")
                        .font(.system(size: 9.5))
                        .foregroundColor(ObsidianTheme.slate)
                }
                .buttonStyle(.plain)

                Button(action: {
                    Task { await vm.testAIConnection(apiKey: vm.aiApiKeyInput) }
                }) {
                    HStack(spacing: 3) {
                        if vm.isTestingAIKey {
                            ProgressView().scaleEffect(0.4)
                        }
                        Text("Test")
                            .font(.system(size: 9, weight: .bold, design: .monospaced))
                    }
                    .foregroundColor(ObsidianTheme.surface)
                    .padding(.horizontal, 7)
                    .padding(.vertical, 3)
                    .background(ObsidianTheme.platinum)
                    .cornerRadius(4)
                }
                .buttonStyle(.plain)
            }
            .padding(.horizontal, 8)
            .padding(.vertical, 6)
            .background(ObsidianTheme.surface)
            .overlay(RoundedRectangle(cornerRadius: 6).stroke(ObsidianTheme.borderSubtle, lineWidth: 1))
            .cornerRadius(6)
            .padding(.horizontal, 14)

            if !vm.aiTestStatusText.isEmpty {
                HStack {
                    Text(vm.aiTestStatusText)
                        .font(.system(size: 8, weight: .bold, design: .monospaced))
                        .foregroundColor(vm.aiTestStatusText.contains("CONNECTED") ? Color(red: 0.06, green: 0.73, blue: 0.51) : Color.orange)
                    Spacer()
                }
                .padding(.horizontal, 14)
            }
        }
        .padding(.bottom, 8)
    }

    @ViewBuilder
    private func modelSelectCard(modelId: String, title: String, tag: String, isSelected: Bool) -> some View {
        Button(action: {
            Task { await vm.saveAIModelConfig(model: modelId) }
        }) {
            VStack(alignment: .leading, spacing: 3) {
                HStack {
                    Text(title)
                        .font(.system(size: 9.5, weight: .bold, design: .monospaced))
                        .foregroundColor(ObsidianTheme.platinum)
                        .lineLimit(1)
                    Spacer()
                    if isSelected {
                        Circle()
                            .fill(Color.cyan)
                            .frame(width: 4, height: 4)
                    }
                }
                Text(tag)
                    .font(.system(size: 7.5, weight: .semibold, design: .monospaced))
                    .foregroundColor(isSelected ? ObsidianTheme.surface : ObsidianTheme.slate)
                    .padding(.horizontal, 4)
                    .padding(.vertical, 1)
                    .background(isSelected ? ObsidianTheme.platinum : ObsidianTheme.surfaceElevated)
                    .cornerRadius(3)
            }
            .padding(7)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(isSelected ? ObsidianTheme.surfaceElevated.opacity(0.8) : ObsidianTheme.surface.opacity(0.5))
            .overlay(
                RoundedRectangle(cornerRadius: 6)
                    .stroke(isSelected ? ObsidianTheme.platinum : ObsidianTheme.borderSubtle, lineWidth: 1)
            )
            .cornerRadius(6)
        }
        .buttonStyle(.plain)
    }

    @ViewBuilder
    private var settingsWalkthroughView: some View {
        VStack(spacing: 8) {
            // Mode Switcher Pills
            HStack(spacing: 8) {
                Button(action: { vm.walkthroughDefaultMode = "guided_demo" }) {
                    HStack(spacing: 5) {
                        Image(systemName: "play.circle.fill")
                            .font(.system(size: 10))
                        Text("Guided Demo")
                            .font(.system(size: 9.5, weight: .bold, design: .monospaced))
                        Spacer()
                        if vm.walkthroughDefaultMode == "guided_demo" {
                            Circle().fill(Color.cyan).frame(width: 4, height: 4)
                        }
                    }
                    .foregroundColor(vm.walkthroughDefaultMode == "guided_demo" ? ObsidianTheme.surface : ObsidianTheme.platinum)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 6)
                    .background(vm.walkthroughDefaultMode == "guided_demo" ? ObsidianTheme.platinum : ObsidianTheme.surfaceElevated)
                    .cornerRadius(6)
                }
                .buttonStyle(.plain)

                Button(action: { vm.walkthroughDefaultMode = "interactive" }) {
                    HStack(spacing: 5) {
                        Image(systemName: "hand.tap.fill")
                            .font(.system(size: 10))
                        Text("Interactive")
                            .font(.system(size: 9.5, weight: .bold, design: .monospaced))
                        Spacer()
                        if vm.walkthroughDefaultMode == "interactive" {
                            Circle().fill(Color.cyan).frame(width: 4, height: 4)
                        }
                    }
                    .foregroundColor(vm.walkthroughDefaultMode == "interactive" ? ObsidianTheme.surface : ObsidianTheme.platinum)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 6)
                    .background(vm.walkthroughDefaultMode == "interactive" ? ObsidianTheme.platinum : ObsidianTheme.surfaceElevated)
                    .cornerRadius(6)
                }
                .buttonStyle(.plain)
            }
            .padding(.horizontal, 14)
            .padding(.top, 8)

            // Step Delay & Audio Chimes in one clean row
            HStack(spacing: 12) {
                HStack(spacing: 4) {
                    Text("Delay:")
                        .font(.system(size: 8.5, weight: .bold, design: .monospaced))
                        .foregroundColor(ObsidianTheme.slate)
                    ForEach([1.5, 2.5, 4.0], id: \.self) { d in
                        Button(action: { vm.walkthroughAdvanceDelay = d }) {
                            Text("\(String(format: "%.1f", d))s")
                                .font(.system(size: 8.5, weight: .bold, design: .monospaced))
                                .foregroundColor(vm.walkthroughAdvanceDelay == d ? ObsidianTheme.surface : ObsidianTheme.platinum)
                                .padding(.horizontal, 6)
                                .padding(.vertical, 2.5)
                                .background(vm.walkthroughAdvanceDelay == d ? ObsidianTheme.platinum : ObsidianTheme.surfaceElevated)
                                .cornerRadius(3)
                        }
                        .buttonStyle(.plain)
                    }
                }

                Spacer()

                Button(action: { vm.audioChimesEnabled.toggle() }) {
                    HStack(spacing: 4) {
                        Image(systemName: vm.audioChimesEnabled ? "speaker.wave.2.fill" : "speaker.slash")
                            .font(.system(size: 8.5))
                        Text(vm.audioChimesEnabled ? "Chimes: On" : "Silent")
                            .font(.system(size: 8.5, weight: .bold, design: .monospaced))
                    }
                    .foregroundColor(vm.audioChimesEnabled ? ObsidianTheme.surface : ObsidianTheme.platinum)
                    .padding(.horizontal, 7)
                    .padding(.vertical, 3)
                    .background(vm.audioChimesEnabled ? ObsidianTheme.platinum : ObsidianTheme.surfaceElevated)
                    .cornerRadius(4)
                }
                .buttonStyle(.plain)
            }
            .padding(.horizontal, 14)
            .padding(.bottom, 8)
        }
    }

    @ViewBuilder
    private var settingsSafetyView: some View {
        VStack(spacing: 6) {
            // Corner Stop
            HStack {
                HStack(spacing: 6) {
                    Image(systemName: "hand.raised.fill")
                        .font(.system(size: 10))
                        .foregroundColor(ObsidianTheme.platinum)
                    Text("Corner Stop")
                        .font(.system(size: 9.5, weight: .bold, design: .monospaced))
                        .foregroundColor(ObsidianTheme.platinum)
                }
                Spacer()
                Button(action: { vm.cornerFailsafeEnabled.toggle() }) {
                    Text(vm.cornerFailsafeEnabled ? "ON" : "OFF")
                        .font(.system(size: 8, weight: .bold, design: .monospaced))
                        .foregroundColor(vm.cornerFailsafeEnabled ? ObsidianTheme.surface : ObsidianTheme.slate)
                        .padding(.horizontal, 7)
                        .padding(.vertical, 2.5)
                        .background(vm.cornerFailsafeEnabled ? ObsidianTheme.platinum : ObsidianTheme.surfaceElevated)
                        .cornerRadius(3)
                }
                .buttonStyle(.plain)
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 6)
            .background(ObsidianTheme.surface)
            .overlay(RoundedRectangle(cornerRadius: 6).stroke(ObsidianTheme.borderSubtle, lineWidth: 1))
            .cornerRadius(6)

            // Background Mode
            HStack {
                HStack(spacing: 6) {
                    Image(systemName: "shield.fill")
                        .font(.system(size: 10))
                        .foregroundColor(ObsidianTheme.platinum)
                    Text("Background Sandbox")
                        .font(.system(size: 9.5, weight: .bold, design: .monospaced))
                        .foregroundColor(ObsidianTheme.platinum)
                }
                Spacer()
                Button(action: { vm.isBackgroundMode.toggle() }) {
                    Text(vm.isBackgroundMode ? "ON" : "OFF")
                        .font(.system(size: 8, weight: .bold, design: .monospaced))
                        .foregroundColor(vm.isBackgroundMode ? ObsidianTheme.surface : ObsidianTheme.slate)
                        .padding(.horizontal, 7)
                        .padding(.vertical, 2.5)
                        .background(vm.isBackgroundMode ? ObsidianTheme.platinum : ObsidianTheme.surfaceElevated)
                        .cornerRadius(3)
                }
                .buttonStyle(.plain)
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 6)
            .background(ObsidianTheme.surface)
            .overlay(RoundedRectangle(cornerRadius: 6).stroke(ObsidianTheme.borderSubtle, lineWidth: 1))
            .cornerRadius(6)

            // Zero Drift Invariant
            HStack {
                HStack(spacing: 6) {
                    Image(systemName: "cursorarrow.motionlines")
                        .font(.system(size: 10))
                        .foregroundColor(ObsidianTheme.platinum)
                    Text("Zero Drift Invariant")
                        .font(.system(size: 9.5, weight: .bold, design: .monospaced))
                        .foregroundColor(ObsidianTheme.platinum)
                }
                Spacer()
                Text("0.0px Active")
                    .font(.system(size: 8, weight: .bold, design: .monospaced))
                    .foregroundColor(Color(red: 0.06, green: 0.73, blue: 0.51))
                    .padding(.horizontal, 5)
                    .padding(.vertical, 2)
                    .background(Color(red: 0.06, green: 0.73, blue: 0.51).opacity(0.12))
                    .cornerRadius(3)
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 6)
            .background(ObsidianTheme.surface)
            .overlay(RoundedRectangle(cornerRadius: 6).stroke(ObsidianTheme.borderSubtle, lineWidth: 1))
            .cornerRadius(6)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 8)
    }

    @ViewBuilder
    private var settingsStorageView: some View {
        VStack(spacing: 8) {
            HStack(spacing: 8) {
                // Workflows tile
                HStack {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Workflows")
                            .font(.system(size: 8.5, weight: .bold, design: .monospaced))
                            .foregroundColor(ObsidianTheme.slate)
                        Text("\(vm.allSavedWorkflows.count)")
                            .font(.system(size: 16, weight: .bold, design: .monospaced))
                            .foregroundColor(ObsidianTheme.platinum)
                    }
                    Spacer()
                    Button(action: {
                        let homeRec = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".clio/recordings")
                        NSWorkspace.shared.open(homeRec)
                    }) {
                        HStack(spacing: 3) {
                            Image(systemName: "folder")
                                .font(.system(size: 8.5))
                            Text("Finder")
                                .font(.system(size: 8.5, weight: .semibold, design: .monospaced))
                        }
                        .foregroundColor(ObsidianTheme.platinum)
                        .padding(.horizontal, 6)
                        .padding(.vertical, 3)
                        .background(ObsidianTheme.surfaceElevated)
                        .cornerRadius(3)
                    }
                    .buttonStyle(.plain)
                }
                .padding(8)
                .frame(maxWidth: .infinity)
                .background(ObsidianTheme.surface)
                .overlay(RoundedRectangle(cornerRadius: 6).stroke(ObsidianTheme.borderSubtle, lineWidth: 1))
                .cornerRadius(6)

                // Cache tile
                let recSize = vm.storageMetrics["recordings_size_mb"] as? Double ?? 0.0
                HStack {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Cache")
                            .font(.system(size: 8.5, weight: .bold, design: .monospaced))
                            .foregroundColor(ObsidianTheme.slate)
                        Text("\(String(format: "%.1f", recSize)) MB")
                            .font(.system(size: 16, weight: .bold, design: .monospaced))
                            .foregroundColor(ObsidianTheme.platinum)
                    }
                    Spacer()
                    Button(action: {
                        Task { await vm.clearStorageCache() }
                    }) {
                        HStack(spacing: 3) {
                            if vm.isClearingStorage {
                                ProgressView().scaleEffect(0.4)
                            } else {
                                Image(systemName: "trash")
                                    .font(.system(size: 8.5))
                            }
                            Text("Clear")
                                .font(.system(size: 8.5, weight: .bold, design: .monospaced))
                        }
                        .foregroundColor(ObsidianTheme.surface)
                        .padding(.horizontal, 6)
                        .padding(.vertical, 3)
                        .background(ObsidianTheme.platinum)
                        .cornerRadius(3)
                    }
                    .buttonStyle(.plain)
                }
                .padding(8)
                .frame(maxWidth: .infinity)
                .background(ObsidianTheme.surface)
                .overlay(RoundedRectangle(cornerRadius: 6).stroke(ObsidianTheme.borderSubtle, lineWidth: 1))
                .cornerRadius(6)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 8)
        }
    }

    @ViewBuilder
    private var aiCompanionView: some View {
        Divider().background(ObsidianTheme.borderSubtle)
        VStack(spacing: 0) {
            // Header
            HStack(spacing: 6) {
                Image(systemName: "sparkles")
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundColor(ObsidianTheme.platinum)
                Text("CLIO AI COMPANION")
                    .font(.system(size: 9.5, weight: .bold, design: .monospaced))
                    .foregroundColor(ObsidianTheme.platinum)
                Spacer()
                Text(vm.isAILoading ? "GENERATING..." : "REASONING ENGINE")
                    .font(.system(size: 8.5, weight: .medium, design: .monospaced))
                    .foregroundColor(ObsidianTheme.slateDark)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 7)
            .background(ObsidianTheme.surfaceElevated.opacity(0.35))

            Divider().background(ObsidianTheme.borderSubtle.opacity(0.4))

            if vm.isAILoading {
                HStack(spacing: 10) {
                    ProgressView()
                        .scaleEffect(0.7)
                        .progressViewStyle(CircularProgressViewStyle(tint: ObsidianTheme.platinum))
                    Text("Asking Clio companion: \"\(vm.aiCompanionPrompt)\"...")
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundColor(ObsidianTheme.slate)
                    Spacer()
                }
                .padding(.horizontal, 14)
                .padding(.vertical, 16)
            } else if !vm.aiCompanionResponse.isEmpty {
                VStack(alignment: .leading, spacing: 8) {
                    Text(vm.aiCompanionResponse)
                        .font(.system(size: 12, weight: .regular))
                        .foregroundColor(ObsidianTheme.platinum)
                        .fixedSize(horizontal: false, vertical: true)
                        .textSelection(.enabled)

                    if let triggered = vm.aiTriggeredWorkflow {
                        HStack(spacing: 6) {
                            Image(systemName: "bolt.fill")
                                .font(.system(size: 10))
                                .foregroundColor(ObsidianTheme.platinum)
                            Text("Matched Automation: \(triggered)")
                                .font(.system(size: 10, weight: .semibold, design: .monospaced))
                                .foregroundColor(ObsidianTheme.platinum)
                            Spacer()
                        }
                        .padding(.horizontal, 8)
                        .padding(.vertical, 4)
                        .background(ObsidianTheme.surfaceElevated)
                        .cornerRadius(5)
                    }

                    HStack(spacing: 8) {
                        Button(action: {
                            NSPasteboard.general.clearContents()
                            NSPasteboard.general.setString(vm.aiCompanionResponse, forType: .string)
                        }) {
                            HStack(spacing: 4) {
                                Image(systemName: "doc.on.doc")
                                    .font(.system(size: 9))
                                Text("Copy Answer")
                                    .font(.system(size: 9.5, weight: .semibold, design: .monospaced))
                            }
                            .foregroundColor(ObsidianTheme.platinum)
                            .padding(.horizontal, 8)
                            .padding(.vertical, 4)
                            .background(ObsidianTheme.surfaceElevated)
                            .cornerRadius(5)
                        }
                        .buttonStyle(.plain)

                        Spacer()

                        Button(action: {
                            vm.aiCompanionResponse = ""
                            vm.query = "/ai "
                        }) {
                            Text("Ask Another")
                                .font(.system(size: 9.5, weight: .medium, design: .monospaced))
                                .foregroundColor(ObsidianTheme.slate)
                        }
                        .buttonStyle(.plain)
                    }
                }
                .padding(.horizontal, 14)
                .padding(.vertical, 10)
            } else {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Type your question in the bar above and press ⏎ to ask Clio.")
                        .font(.system(size: 11.5, weight: .regular))
                        .foregroundColor(ObsidianTheme.slate)

                    HStack(spacing: 6) {
                        Text("Suggested:")
                            .font(.system(size: 9, weight: .bold, design: .monospaced))
                            .foregroundColor(ObsidianTheme.slateDark)

                        Button(action: {
                            vm.query = "/ai How do I split screen on Mac?"
                            Task { await vm.askAICompanion(prompt: "How do I split screen on Mac?") }
                        }) {
                            Text("\"Split screen on Mac\"")
                                .font(.system(size: 9, design: .monospaced))
                                .foregroundColor(ObsidianTheme.platinum)
                                .padding(.horizontal, 6)
                                .padding(.vertical, 2.5)
                                .background(ObsidianTheme.surfaceElevated)
                                .cornerRadius(4)
                        }
                        .buttonStyle(.plain)

                        Button(action: {
                            vm.query = "/ai How do I take a screenshot?"
                            Task { await vm.askAICompanion(prompt: "How do I take a screenshot?") }
                        }) {
                            Text("\"Take a screenshot\"")
                                .font(.system(size: 9, design: .monospaced))
                                .foregroundColor(ObsidianTheme.platinum)
                                .padding(.horizontal, 6)
                                .padding(.vertical, 2.5)
                                .background(ObsidianTheme.surfaceElevated)
                                .cornerRadius(4)
                        }
                        .buttonStyle(.plain)
                    }
                }
                .padding(.horizontal, 14)
                .padding(.vertical, 10)
            }

            Divider().background(ObsidianTheme.borderSubtle.opacity(0.35))

            // Footer
            HStack {
                Text("⏎ Send Query • Esc Clear")
                    .font(.system(size: 9, design: .monospaced))
                    .foregroundColor(ObsidianTheme.slate)
                Spacer()
                Text("CLIO REASONING")
                    .font(.system(size: 8.5, weight: .bold, design: .monospaced))
                    .foregroundColor(ObsidianTheme.slateDark)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 4)
            .background(ObsidianTheme.surfaceElevated.opacity(0.2))
        }
        .background(ObsidianTheme.cardGlass)
    }

    @ViewBuilder
    private var helpGuideView: some View {
        Divider().background(ObsidianTheme.borderSubtle)
        VStack(spacing: 0) {
            // Header
            HStack(spacing: 6) {
                Image(systemName: "questionmark.circle.fill")
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundColor(ObsidianTheme.platinum)
                Text("CLIO SYSTEM GUIDE & SHORTCUTS")
                    .font(.system(size: 9.5, weight: .bold, design: .monospaced))
                    .foregroundColor(ObsidianTheme.platinum)
                Spacer()
                Text("v1.0 • HUD QUICKSTART")
                    .font(.system(size: 8.5, weight: .medium, design: .monospaced))
                    .foregroundColor(ObsidianTheme.slateDark)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 7)
            .background(ObsidianTheme.surfaceElevated.opacity(0.35))

            Divider().background(ObsidianTheme.borderSubtle.opacity(0.4))

            ScrollView(.vertical, showsIndicators: true) {
                VStack(alignment: .leading, spacing: 10) {
                    // Section 1: Productivity Slash Commands
                    Text("SLASH COMMANDS")
                        .font(.system(size: 9, weight: .bold, design: .monospaced))
                        .foregroundColor(ObsidianTheme.slate)
                        .padding(.top, 4)

                    VStack(spacing: 4) {
                        ForEach(SlashShortcutCatalog.shortcuts, id: \.id) { item in
                            HStack(spacing: 8) {
                                Text(item.command)
                                    .font(.system(size: 11, weight: .bold, design: .monospaced))
                                    .foregroundColor(ObsidianTheme.platinum)
                                    .frame(width: 65, alignment: .leading)
                                Text(item.title)
                                    .font(.system(size: 11, weight: .medium))
                                    .foregroundColor(ObsidianTheme.platinumDim)
                                Spacer()
                                if let key = item.shortcutKey {
                                    Text(key)
                                        .font(.system(size: 9, weight: .bold, design: .monospaced))
                                        .foregroundColor(ObsidianTheme.slate)
                                        .padding(.horizontal, 5)
                                        .padding(.vertical, 2)
                                        .background(ObsidianTheme.surfaceElevated)
                                        .cornerRadius(4)
                                }
                            }
                            .padding(.horizontal, 8)
                            .padding(.vertical, 4)
                            .background(ObsidianTheme.surfaceElevated.opacity(0.2))
                            .cornerRadius(5)
                        }
                    }

                    Divider().background(ObsidianTheme.borderSubtle.opacity(0.25))

                    // Section 2: Key Navigation
                    Text("KEYBOARD CONTROLS")
                        .font(.system(size: 9, weight: .bold, design: .monospaced))
                        .foregroundColor(ObsidianTheme.slate)

                    VStack(spacing: 4) {
                        helpKeyRow(keys: "⏎ Return", desc: "Execute command / Run highlighted action")
                        helpKeyRow(keys: "Esc", desc: "Clear input / Dismiss active panel")
                        helpKeyRow(keys: "↑ / ↓", desc: "Navigate commands & recommendation items")
                        helpKeyRow(keys: "⌘1 – ⌘8", desc: "Quick-fire corresponding slash command")
                        helpKeyRow(keys: "⌘D", desc: "Toggle voice dictation audio recording")
                    }

                    Divider().background(ObsidianTheme.borderSubtle.opacity(0.25))

                    // Section 3: Action Buttons
                    HStack {
                        Button(action: {
                            if let url = URL(string: "https://github.com/aaronnguyen26/clio#readme") {
                                NSWorkspace.shared.open(url)
                            }
                        }) {
                            HStack(spacing: 5) {
                                Image(systemName: "safari")
                                    .font(.system(size: 10))
                                Text("Open Documentation")
                                    .font(.system(size: 9.5, weight: .semibold, design: .monospaced))
                            }
                            .foregroundColor(ObsidianTheme.surface)
                            .padding(.horizontal, 10)
                            .padding(.vertical, 5)
                            .background(ObsidianTheme.platinum)
                            .cornerRadius(5)
                        }
                        .buttonStyle(.plain)

                        Spacer()

                        Button(action: {
                            vm.isHelpPanelOpen = false
                            vm.query = ""
                        }) {
                            Text("Dismiss Help")
                                .font(.system(size: 9.5, weight: .medium, design: .monospaced))
                                .foregroundColor(ObsidianTheme.slate)
                                .padding(.horizontal, 10)
                                .padding(.vertical, 5)
                                .background(ObsidianTheme.surfaceElevated)
                                .cornerRadius(5)
                        }
                        .buttonStyle(.plain)
                    }
                    .padding(.top, 4)
                }
                .padding(.horizontal, 14)
                .padding(.vertical, 10)
            }
            .frame(maxHeight: 280)

            Divider().background(ObsidianTheme.borderSubtle.opacity(0.35))

            // Footer
            HStack {
                Text("Esc to close • ⏎ or ⌘1-8 to execute")
                    .font(.system(size: 9, design: .monospaced))
                    .foregroundColor(ObsidianTheme.slate)
                Spacer()
                Text("SYSTEM MANUAL")
                    .font(.system(size: 8.5, weight: .bold, design: .monospaced))
                    .foregroundColor(ObsidianTheme.slateDark)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 4)
            .background(ObsidianTheme.surfaceElevated.opacity(0.2))
        }
        .background(ObsidianTheme.cardGlass)
    }

    private func helpKeyRow(keys: String, desc: String) -> some View {
        HStack(spacing: 8) {
            Text(keys)
                .font(.system(size: 10, weight: .bold, design: .monospaced))
                .foregroundColor(ObsidianTheme.platinum)
                .frame(width: 80, alignment: .leading)
            Text(desc)
                .font(.system(size: 10.5))
                .foregroundColor(ObsidianTheme.slate)
            Spacer()
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 3)
    }
}

struct SlashCommandRowView: View {
    let item: SlashShortcutItem
    let isSelected: Bool
    let onSelect: () -> Void

    @State private var isHovered: Bool = false

    var body: some View {
        Button(action: onSelect) {
            HStack(spacing: 10) {
                // Icon in Squircle
                ZStack {
                    RoundedRectangle(cornerRadius: 6)
                        .fill((isSelected || isHovered) ? ObsidianTheme.platinum : ObsidianTheme.zinc)
                        .frame(width: 26, height: 26)
                    Image(systemName: item.iconName)
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundColor((isSelected || isHovered) ? ObsidianTheme.surface : ObsidianTheme.platinum)
                }

                // Command and Description
                VStack(alignment: .leading, spacing: 2) {
                    HStack(spacing: 6) {
                        Text(item.command)
                            .font(.system(size: 12.5, weight: .bold, design: .monospaced))
                            .foregroundColor((isSelected || isHovered) ? ObsidianTheme.platinum : ObsidianTheme.platinumDim)
                        Text("—")
                            .font(.system(size: 10))
                            .foregroundColor(ObsidianTheme.slateDark)
                        Text(item.title)
                            .font(.system(size: 12, weight: .medium))
                            .foregroundColor((isSelected || isHovered) ? ObsidianTheme.platinum : ObsidianTheme.slate)
                    }

                    Text(item.description)
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundColor(ObsidianTheme.slate)
                        .lineLimit(1)
                }

                Spacer()

                if let key = item.shortcutKey {
                    Text(key)
                        .font(.system(size: 9, weight: .bold, design: .monospaced))
                        .foregroundColor(ObsidianTheme.slateDark)
                        .padding(.horizontal, 5)
                        .padding(.vertical, 2)
                        .background(ObsidianTheme.surfaceElevated.opacity(0.7))
                        .cornerRadius(4)
                }

                Text(item.badge)
                    .font(.system(size: 8.5, weight: .bold, design: .monospaced))
                    .foregroundColor((isSelected || isHovered) ? ObsidianTheme.surface : ObsidianTheme.slateDark)
                    .padding(.horizontal, 6)
                    .padding(.vertical, 2.5)
                    .background((isSelected || isHovered) ? ObsidianTheme.platinum : ObsidianTheme.surfaceElevated.opacity(0.5))
                    .cornerRadius(4)
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 6)
            .background((isSelected || isHovered) ? ObsidianTheme.surfaceElevated : Color.clear)
            .cornerRadius(7)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hovering in
            isHovered = hovering
        }
    }
}

struct SearchResultRowView: View {
    let wf: WorkflowItem
    let isSelected: Bool
    let onPreview: () -> Void
    let onSelect: () -> Void
    let onDelete: () -> Void

    @State private var isHovered: Bool = false

    private var rowIconName: String {
        switch wf.id {
        case "sys_home": return "house.fill"
        case "sys_help": return "questionmark.circle.fill"
        case "sys_hotcorners": return "macwindow.badge.plus"
        case "sys_hide_others": return "eye.slash.fill"
        case "sys_photos": return "photo.fill"
        case "sys_podcasts": return "antenna.radiowaves.left.and.right"
        case "sys_preview": return "doc.text.magnifyingglass"
        case "sys_settings": return "gearshape.fill"
        case "sys_calculator": return "plus.slash.minus"
        case "sys_calendar": return "calendar"
        case "sys_safari": return "safari.fill"
        case "sys_terminal": return "terminal.fill"
        case "sys_textedit": return "doc.text.fill"
        case "sys_notes": return "note.text"
        case "sys_messages": return "message.fill"
        case "sys_mail": return "envelope.fill"
        case "sys_music": return "music.note"
        case "sys_finder": return "folder.fill"
        case "sys_activity": return "chart.bar.xaxis"
        default: return "command"
        }
    }

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: rowIconName)
                .font(.system(size: 11))
                .foregroundColor((isSelected || isHovered) ? ObsidianTheme.platinum : ObsidianTheme.slateDark)
                .frame(width: 16)

            VStack(alignment: .leading, spacing: 2) {
                Text(wf.displayName)
                    .font(.system(size: 12, weight: .medium))
                    .foregroundColor((isSelected || isHovered) ? ObsidianTheme.platinum : ObsidianTheme.platinumDim)
                if let desc = wf.description, !desc.isEmpty {
                    Text(desc)
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundColor(ObsidianTheme.slate)
                } else if let trig = wf.canonical_trigger, !trig.isEmpty {
                    Text(trig)
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundColor(ObsidianTheme.slate)
                }
            }

            Spacer()

            if let vPath = wf.video_path, !vPath.isEmpty {
                Button(action: onPreview) {
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

            if wf.id.hasPrefix("sys_") {
                Text("System Action")
                    .font(.system(size: 9, weight: .semibold, design: .monospaced))
                    .foregroundColor(ObsidianTheme.slateDark)
                    .padding(.horizontal, 6)
                    .padding(.vertical, 2)
                    .background(ObsidianTheme.surfaceElevated.opacity(0.6))
                    .cornerRadius(4)
            } else {
                Text("\(wf.displaySteps) steps")
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundColor(ObsidianTheme.slate)

                // Explicit Delete Button
                Button(action: onDelete) {
                    Image(systemName: "trash")
                        .font(.system(size: 10))
                        .foregroundColor(ObsidianTheme.slateDark)
                        .frame(width: 20, height: 20)
                        .background(ObsidianTheme.surfaceElevated)
                        .cornerRadius(4)
                }
                .buttonStyle(.plain)
                .help("Delete action and associated recording")
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 6)
        .background((isSelected || isHovered) ? ObsidianTheme.surfaceElevated : Color.clear)
        .cornerRadius(6)
        .contentShape(Rectangle())
        .onHover { hovering in
            isHovered = hovering
        }
        .onTapGesture {
            onSelect()
        }
    }
}

struct MemorySpaceRowView: View {
    let index: Int
    let wf: WorkflowItem
    let isSelected: Bool
    let onPreview: () -> Void
    let onSelect: () -> Void
    let onDelete: () -> Void

    @State private var isHovered: Bool = false

    var body: some View {
        HStack(spacing: 10) {
            // Index Number Pill
            Text("#\(index + 1)")
                .font(.system(size: 10, weight: .bold, design: .monospaced))
                .foregroundColor(isSelected ? ObsidianTheme.surface : ObsidianTheme.platinumDim)
                .frame(width: 26, height: 22)
                .background(isSelected ? ObsidianTheme.platinum : ObsidianTheme.zinc)
                .cornerRadius(4)

            // Title & Trigger
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    Text(wf.displayName)
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundColor((isSelected || isHovered) ? ObsidianTheme.platinum : ObsidianTheme.platinumDim)
                        .lineLimit(1)
                }

                if let trig = wf.canonical_trigger, !trig.isEmpty {
                    Text("trigger: \"\(trig)\"")
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundColor(ObsidianTheme.slate)
                        .lineLimit(1)
                }
            }

            Spacer()

            // Video Badge (if screen recording exists)
            if let vPath = wf.video_path, !vPath.isEmpty {
                Button(action: onPreview) {
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
                .help("View demonstration video")
            }

            // Step count badge
            Text("\(wf.displaySteps) steps")
                .font(.system(size: 10, weight: .medium, design: .monospaced))
                .foregroundColor(ObsidianTheme.slate)
                .padding(.horizontal, 6)
                .padding(.vertical, 2)
                .background(ObsidianTheme.surfaceElevated)
                .cornerRadius(4)

            // Explicit Delete Button
            Button(action: onDelete) {
                Image(systemName: "trash")
                    .font(.system(size: 10))
                    .foregroundColor(ObsidianTheme.slateDark)
                    .frame(width: 22, height: 22)
                    .background(ObsidianTheme.surfaceElevated)
                    .cornerRadius(4)
            }
            .buttonStyle(.plain)
            .help("Delete action and associated recording")

            // Inspect arrow
            Image(systemName: "chevron.right")
                .font(.system(size: 10, weight: .semibold))
                .foregroundColor((isSelected || isHovered) ? ObsidianTheme.platinum : ObsidianTheme.slateDark)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background((isSelected || isHovered) ? ObsidianTheme.surfaceElevated : Color.clear)
        .cornerRadius(8)
        .contentShape(Rectangle())
        .onHover { hovering in
            isHovered = hovering
        }
        .onTapGesture {
            onSelect()
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
        self.hasShadow = false
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
            NotificationCenter.default.post(name: NSNotification.Name("ClioBarEscapeKey"), object: nil)
            return true
        }

        let isCmd = event.modifierFlags.contains(.command)
        let isShift = event.modifierFlags.contains(.shift)
        let chars = event.charactersIgnoringModifiers?.lowercased() ?? ""

        // Standard macOS Command shortcuts for seamless Mac user experience
        if isCmd {
            switch chars {
            case "w":
                AppDelegate.shared?.hidePanel()
                return true
            case "q":
                NSApp.terminate(nil)
                return true
            case "c":
                if NSApp.sendAction(#selector(NSText.copy(_:)), to: nil, from: self) {
                    return true
                }
            case "v":
                if NSApp.sendAction(#selector(NSText.paste(_:)), to: nil, from: self) {
                    return true
                }
            case "x":
                if NSApp.sendAction(#selector(NSText.cut(_:)), to: nil, from: self) {
                    return true
                }
            case "a":
                if NSApp.sendAction(#selector(NSText.selectAll(_:)), to: nil, from: self) {
                    return true
                }
            case "z":
                let action = isShift ? Selector(("redo:")) : Selector(("undo:"))
                if NSApp.sendAction(action, to: nil, from: self) {
                    return true
                }
            case ",":
                NotificationCenter.default.post(name: NSNotification.Name("ClioBarToggleSettings"), object: nil)
                return true
            case "1", "2", "3", "4", "5", "6", "7", "8":
                NotificationCenter.default.post(name: NSNotification.Name("TriggerNumberedShortcut"), object: chars)
                return true
            case "9":
                NotificationCenter.default.post(name: NSNotification.Name("TriggerNumberedShortcut"), object: chars)
                return true
            default:
                break
            }
        }

        // Cmd + Backspace (51):
        // If user is currently editing text in a text view, delete to beginning of line (macOS standard).
        // Otherwise, if in workflow inspection/selection, delete selected workflow.
        if isCmd && event.keyCode == 51 {
            let isTextEditing = (self.firstResponder is NSTextView || self.firstResponder is NSText)
            if isTextEditing {
                if NSApp.sendAction(#selector(NSResponder.deleteToBeginningOfLine(_:)), to: nil, from: self) {
                    return true
                }
            } else {
                NotificationCenter.default.post(name: NSNotification.Name("DeleteSelectedWorkflow"), object: nil)
                return true
            }
        }

        // Down Arrow (125)
        if event.keyCode == 125 {
            NotificationCenter.default.post(name: NSNotification.Name("SelectNextWorkflow"), object: nil)
            return true
        }
        // Up Arrow (126)
        if event.keyCode == 126 {
            NotificationCenter.default.post(name: NSNotification.Name("SelectPrevWorkflow"), object: nil)
            return true
        }
        // Left Arrow (123) with Cmd or Option
        if event.keyCode == 123 && (isCmd || event.modifierFlags.contains(.option)) {
            NotificationCenter.default.post(name: NSNotification.Name("StepPrevWorkflow"), object: nil)
            return true
        }
        // Right Arrow (124) with Cmd or Option
        if event.keyCode == 124 && (isCmd || event.modifierFlags.contains(.option)) {
            NotificationCenter.default.post(name: NSNotification.Name("StepNextWorkflow"), object: nil)
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
    private var previousFrontApp: NSRunningApplication?

    override init() {
        super.init()
        AppDelegate.shared = self
    }

    func showPanel() {
        guard let panel = panel else { return }
        if let front = NSWorkspace.shared.frontmostApplication,
           front.processIdentifier != ProcessInfo.processInfo.processIdentifier {
            previousFrontApp = front
        }
        panel.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        NotificationCenter.default.post(name: NSNotification.Name("FocusClioField"), object: nil)
    }

    func hidePanel() {
        panel?.orderOut(nil)
        NSApp.deactivate()
        if let prev = previousFrontApp, !prev.isTerminated {
            prev.activate(options: [])
        }
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
        let currentFrame = panel.frame
        // If height is already identical, do not trigger window animation to avoid glitch/jitter
        if abs(currentFrame.height - newHeight) < 0.5 {
            return
        }
        let screenRect = screen.visibleFrame
        let panelWidth: CGFloat = 720
        let newY = screenRect.origin.y + screenRect.height - newHeight - 120
        let newFrame = NSRect(x: currentFrame.origin.x, y: newY, width: panelWidth, height: newHeight)
        panel.setFrame(newFrame, display: true, animate: true)
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        // Preflight and request screen capture access from the Swift process context
        if !CGPreflightScreenCaptureAccess() {
            CGRequestScreenCaptureAccess()
        }

        // Verify accessibility quietly without opening Settings
        _ = AXIsProcessTrusted()
        _ = VirtualCursorOverlayManager.shared

        setupMainMenu()

        let screenRect = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        let panelWidth: CGFloat = 720
        let panelHeight: CGFloat = 58
        let x = screenRect.origin.x + (screenRect.width - panelWidth) / 2
        let y = screenRect.origin.y + screenRect.height - panelHeight - 120

        let panel = SpotlightPanel(contentRect: NSRect(x: x, y: y, width: panelWidth, height: panelHeight))
        let hostingView = NSHostingView(rootView: ClioBarView())
        hostingView.wantsLayer = true
        hostingView.layer?.backgroundColor = NSColor.clear.cgColor
        panel.contentView = hostingView
        panel.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        self.panel = panel

        setupStatusItem()
        setupGlobalShortcut()
    }

    private func setupMainMenu() {
        let mainMenu = NSMenu()

        // 1. Application Menu
        let appMenuItem = NSMenuItem()
        let appMenu = NSMenu(title: "Clio")
        appMenu.addItem(withTitle: "About Clio", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(withTitle: "Settings…", action: #selector(openSettingsFromMenu), keyEquivalent: ",")
        appMenu.addItem(NSMenuItem.separator())
        appMenu.addItem(withTitle: "Hide Clio", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        let hideOthersItem = NSMenuItem(title: "Hide Others", action: #selector(NSApplication.hideOtherApplications(_:)), keyEquivalent: "h")
        hideOthersItem.keyEquivalentModifierMask = [.command, .option]
        appMenu.addItem(hideOthersItem)
        appMenu.addItem(withTitle: "Show All", action: #selector(NSApplication.unhideAllApplications(_:)), keyEquivalent: "")
        appMenu.addItem(NSMenuItem.separator())
        appMenu.addItem(withTitle: "Quit Clio", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appMenuItem.submenu = appMenu
        mainMenu.addItem(appMenuItem)

        // 2. Edit Menu (Standard macOS Clipboard & Text editing)
        let editMenuItem = NSMenuItem()
        let editMenu = NSMenu(title: "Edit")
        editMenu.addItem(withTitle: "Undo", action: #selector(UndoManager.undo), keyEquivalent: "z")
        let redoItem = NSMenuItem(title: "Redo", action: #selector(UndoManager.redo), keyEquivalent: "z")
        redoItem.keyEquivalentModifierMask = [.command, .shift]
        editMenu.addItem(redoItem)
        editMenu.addItem(NSMenuItem.separator())
        editMenu.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        let pastePlainItem = NSMenuItem(title: "Paste and Match Style", action: #selector(NSTextView.pasteAsPlainText(_:)), keyEquivalent: "v")
        pastePlainItem.keyEquivalentModifierMask = [.command, .option, .shift]
        editMenu.addItem(pastePlainItem)
        editMenu.addItem(withTitle: "Delete", action: #selector(NSText.delete(_:)), keyEquivalent: "")
        editMenu.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editMenuItem.submenu = editMenu
        mainMenu.addItem(editMenuItem)

        // 3. Window Menu
        let windowMenuItem = NSMenuItem()
        let windowMenu = NSMenu(title: "Window")
        windowMenu.addItem(withTitle: "Close", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        windowMenu.addItem(withTitle: "Minimize", action: #selector(NSWindow.miniaturize(_:)), keyEquivalent: "m")
        windowMenuItem.submenu = windowMenu
        mainMenu.addItem(windowMenuItem)

        NSApp.mainMenu = mainMenu
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

    @objc private func openSettingsFromMenu() {
        showPanel()
        NotificationCenter.default.post(name: NSNotification.Name("ClioBarToggleSettings"), object: nil)
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

if CommandLine.arguments.contains("--test-record") {
    let recorder = SwiftScreenRecorder.shared
    print("[TestRecord] Starting recording...")
    let outURL = recorder.startRecording()
    print("[TestRecord] startRecording returned URL: \(String(describing: outURL))")
    var completed = false
    Task {
        try? await Task.sleep(nanoseconds: 3_000_000_000)
        print("[TestRecord] Stopping recording...")
        recorder.stopRecording { url, err in
            print("[TestRecord] stopRecording completion - URL: \(String(describing: url)), Error: \(String(describing: err))")
            if let u = url {
                print("[TestRecord] Video file exists: \(FileManager.default.fileExists(atPath: u.path))")
                if let attrs = try? FileManager.default.attributesOfItem(atPath: u.path),
                   let size = attrs[.size] as? Int64 {
                    print("[TestRecord] File size: \(size) bytes")
                }
            }
            completed = true
        }
    }
    while !completed {
        RunLoop.main.run(until: Date(timeIntervalSinceNow: 0.1))
    }
    exit(0)
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
