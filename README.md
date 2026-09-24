# Clio ⌘ — Autonomous Desktop Companion & Hands-Free Task Automator

Clio is an autonomous macOS desktop companion and imitation-learning task automator. It observes user demonstrations (mouse clicks, keystrokes, gestures, dock activations, window movements), dissects them into declarative multi-step workflows, persists them in memory, and re-executes them hands-free using a virtual cursor overlay.

![macOS 14+](https://img.shields.io/badge/macOS-14.0%2B-blue)
![SwiftUI](https://img.shields.io/badge/UI-SwiftUI%20%7C%20AppKit-black)
![Python 3.12+](https://img.shields.io/badge/Backend-Python%203.12%2B-brightgreen)
![License](https://img.shields.io/badge/License-MIT-purple)

---

## 🌟 Features

- **Obsidian Floating HUD Bar**: Minimalist, monochromatic 58px floating spotlight interface (`⌥ Space` or `⌘ Space`). Never clutters the screen with intrusive drawer expansions.
- **Hands-Free Demonstration Recording**: Press **RECORD**, perform any actions (e.g. open apps, click dock icons, type inputs, click buttons), and press **● STOP & SAVE** to name the action.
- **4-Stage Dissection Pipeline**: Automatically normalizes screen coordinates, merges rapid typing/click events, attaches bundle IDs & window relative bounds, and synthesizes reproducible `WorkflowSpec` models.
- **Virtual Cursor Overlay (`VirtualCursor`)**: Animates an independent on-screen pointer showing exactly what step Clio is performing in real-time, with smooth quadratic bezier curves and state badges (`CLICKING`, `TYPING`, `APPROACHING`).
- **Strict Background Non-Bleed Execution**: Automated clicks and hotkeys are routed via native `kCGHIDEventTap` and Accessibility APIs without stealing focus or disrupting foreground user tasks.
- **Fast 4-Tier Semantic Retrieval & Intent Synthesizer**: Instant trigger matching, synonym aliases, FTS5 full-text search, and dynamic web/app intent synthesis (`open yt`, `open github`, `open chatgpt`, `open notes`, etc.).
- **Live SSE Telemetry Stream**: Real-time event broadcasting (`/api/stream`) for coordinates, action states, and conversational commentary.

---

## 📐 Architecture

```
┌────────────────────────────────────────────────────────┐
│            Obsidian HUD Bar (SwiftUI / AppKit)         │
│  - Command Input   - Voice Dictation   - Record Button  │
└──────────────────────────┬─────────────────────────────┘
                           │ HTTP / SSE (Port 8765)
┌──────────────────────────▼─────────────────────────────┐
│               Clio Core Engine (Python 3)               │
│                                                        │
│  1. Demonstration Capture & 4-Stage Pipeline           │
│     - Global Event Tap Monitor                         │
│     - Window Relative Coordinate Normalizer            │
│                                                        │
│  2. Persistent SQLite Memory (WAL Mode + FTS5)         │
│     - TaskMemoryEngine                                 │
│     - 4-Tier Natural Language Retrieval                │
│                                                        │
│  3. Executor & Intent Synthesizer                      │
│     - Dynamic Intent Parser (Apps, Web, Workflows)     │
│     - Multi-Step Resilient Execution Loop              │
│                                                        │
│  4. Actuation Layer                                    │
│     - VirtualCursor (Overlay Window & Smooth Bezier)   │
│     - Native macOS CGEvent Actuator (Background Mode)  │
└────────────────────────────────────────────────────────┘
```

---

## 🚀 Getting Started

### Prerequisites

- macOS Sonoma (14.0) or later
- Python 3.12+ (or Homebrew Python 3.14)
- Xcode Command Line Tools (`xcode-select --install`)
- Accessibility permissions enabled in **System Settings > Privacy & Security > Accessibility**

### Quick Build & Run

1. **Clone the repository**:
   ```bash
   git clone https://github.com/aaronnguyen26/clio.git
   cd clio
   ```

2. **Build the native App Bundle**:
   ```bash
   ./scripts/build_app.sh
   ```

3. **Start the Clio Backend Server**:
   ```bash
   python3 -m src.main --server --port 8765
   ```

4. **Launch Clio**:
   ```bash
   open Clio.app
   ```

---

## 🛠️ Usage

### Recording a New Action
1. Open Clio Bar (`⌥ Space`).
2. Click **RECORD**.
3. Perform the task on your Mac (e.g. click a Dock item, open an app, navigate to a URL).
4. Click **● STOP & SAVE** on the bar.
5. Enter a friendly name and trigger phrase (e.g., `open yt`), then click **Save & Add**.

### Running an Action
1. Summon the bar (`⌥ Space`).
2. Type your trigger (e.g., `open yt` or `open calendar`) and press `Enter`.
3. Clio's virtual cursor will navigate to the target, focus the app, and perform the demonstrated sequence hands-free!

---

## 🧪 Testing

Comprehensive test suites verify multi-step demonstration recording, intent synthesis, retrieval, and virtual cursor non-bleed execution:

```bash
# Run unit & integration test suites
python3 run_tests.py

# Run complex multi-step audit
python3 test_complex_multistep_audit.py

# Run full end-to-end user flow test
python3 test_user_flows.py
```

---

## 📄 License

MIT License. Designed and built with ❤️ on macOS.
