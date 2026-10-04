# Clio

Autonomous desktop companion and task automator for macOS.

Clio records everyday tasks on your Mac (opening apps, clicking buttons, typing text, and navigating windows), saves them as reusable workflows, and replays them hands-free using an on-screen virtual cursor.

---

## Features

- Floating Spotlight Bar: Lightweight, unobtrusive search bar summoned anywhere on your Mac.
- Demonstration Recording: Record actions directly on your screen and save them with custom trigger names.
- Virtual Cursor: Displays an on-screen pointer showing each automated step in real time.
- Natural Language Search: Search and trigger saved workflows by name, keyword, or action intent.
- Safe Background Execution: Interacts with native apps using macOS Accessibility and event systems.

---

## Getting Started

### Prerequisites

- Python 3.10 or later
- Xcode Command Line Tools (`xcode-select --install`)
- Accessibility permissions enabled in System Settings > Privacy & Security > Accessibility

### Build and Run

1. Clone the repository:
   ```bash
   git clone https://github.com/aaronnguyen26/clio.git
   cd clio
   ```

2. Build the native application bundle:
   ```bash
   ./scripts/build_app.sh
   ```

3. Start the backend server:
   ```bash
   python3 -m src.main --server --port 8765
   ```

4. Launch the application:
   ```bash
   open Clio.app
   ```

---

## How to Use

### Open and Close the Bar

- Open: Press `Option + Space` (or `Command + Shift + Space`), or click the Clio icon in the macOS menu bar.
- Close: Press `Escape` or `Command + W`, click the close button, or type `close` and press Enter.

### Record a Task

1. Open the bar with `Option + Space`.
2. Click Record.
3. Perform the task on your screen (for example, launch an app, click navigation items, or fill in fields).
4. Click Stop & Save, enter a trigger phrase (such as `open calendar`), and save.

### Run a Task

1. Open the bar with `Option + Space`.
2. Type your trigger phrase (for example, `open calendar`) and press Enter.
3. The bar hides automatically and Clio executes the saved actions on your screen.

---

## Documentation

Detailed technical specifications and design guides are located in the [docs/](docs/README.md) directory:

- [System Architecture](docs/architecture/system_architecture.md): System layout, screen capture pipeline, and execution model.
- [AI Dissection Pipeline](docs/architecture/ai_dissection_pipeline.md): Multimodal event processing, ground-truth detection, and provider integrations.
- [Test Infrastructure](docs/testing/test_infrastructure.md): Test tiers, coverage guidelines, and verification commands.

---

## License

MIT License.
