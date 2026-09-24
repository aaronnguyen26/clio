"""Main Entry Point for Autonomous Desktop Companion (Task Memory Automator).

Provides interactive CLI REPL and programmatic invocation for:
- Conversational companion mode (`--chat`)
- Multi-domain benchmark execution (`--benchmark`)
- Learned workflow management (`--list`)
- Teach mode demonstration recorder (`--teach`)
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import sys
from typing import Any, Dict, List, Optional

from src.actuators.types import ActuatorMode
from src.actuators.factory import get_actuator
from src.benchmark.runner import BenchmarkRunner
from src.benchmark.scenarios import (
    build_cross_domain_workflow,
    build_notes_productivity_workflow,
    build_web_automation_workflow,
)
from src.companion.session import CompanionSession
from src.memory.engine import TaskMemoryEngine
from src.memory.models import ActionType, WorkflowSpec, WorkflowStep


DEFAULT_DB_DIR = Path.home() / ".task_automator"
DEFAULT_DB_PATH = str(DEFAULT_DB_DIR / "task_memory.db")


def seed_default_workflows(memory: TaskMemoryEngine) -> int:
    """Seeds the 3 canonical benchmark workflows into memory if not present."""
    defaults = [
        build_notes_productivity_workflow(),
        build_web_automation_workflow(),
        build_cross_domain_workflow(),
    ]
    added = 0
    for wf in defaults:
        existing = memory.get_workflow(wf.id)
        if not existing:
            memory.save_workflow(wf)
            added += 1
    return added


def get_memory_engine(db_path: Optional[str] = None, auto_seed: bool = False) -> TaskMemoryEngine:
    """Returns a TaskMemoryEngine configured with persistent storage (auto_seed defaults to False)."""
    target_path = db_path or DEFAULT_DB_PATH
    if target_path != ":memory:":
        Path(target_path).parent.mkdir(parents=True, exist_ok=True)
    memory = TaskMemoryEngine(db_path=target_path)
    if auto_seed:
        workflows = memory.list_workflows()
        if not workflows:
            seed_default_workflows(memory)
    return memory


def run_benchmark_mode(mock: bool = True) -> int:
    """Runs the multi-domain benchmark suite and prints the consolidated scorecard."""
    print("=" * 60)
    print("🚀 Running Multi-Domain Autonomous Benchmark Suite")
    print("=" * 60)
    print(f"Mode: {'Headless Simulation (Mock)' if mock else 'Live OS Control'}")

    actuator = get_actuator(mode=ActuatorMode.MOCK if mock else ActuatorMode.MACOS)
    runner = BenchmarkRunner(actuator=actuator, zero_delay=True)
    reports = runner.run_all()

    all_passed = True
    print("\n--- Benchmark Results ---")
    for rep in reports:
        status_icon = "✅" if rep.success and rep.verified else "❌"
        r6_icon = "🛡️ R6/R7 PASS" if rep.r6_compliant else "⚠️ R6 FAIL"
        print(
            f"{status_icon} [{rep.scenario_name}] "
            f"Steps: {rep.steps_completed}/{rep.total_steps} | "
            f"Time: {rep.duration_seconds:.3f}s | {r6_icon}"
        )
        if rep.error:
            print(f"   Error: {rep.error}")
        if not (rep.success and rep.verified):
            all_passed = False

    print("=" * 60)
    if all_passed:
        print("🎉 ALL BENCHMARKS CERTIFIED CLEAN!")
        return 0
    else:
        print("❌ SOME BENCHMARKS FAILED.")
        return 1


def run_chat_repl(
    tone: str = "vibrant",
    mock: bool = True,
    db_path: Optional[str] = None,
) -> int:
    """Runs the interactive conversational REPL with the desktop companion."""
    print("=" * 60)
    print("🌟 Clio — Autonomous Desktop Companion")
    print("=" * 60)
    print("Type your message or workflow command below. Type 'exit' or 'quit' to quit.\n")

    memory = get_memory_engine(db_path=db_path)
    actuator = get_actuator(mode=ActuatorMode.MOCK if mock else ActuatorMode.MACOS)
    session = CompanionSession(
        actuator=actuator,
        memory=memory,
        tone=tone,
        zero_delay=True,
    )
    # Greet
    greeting = session.interact("Hello Clio!")
    print(f"Clio: {greeting}\n")

    try:
        while True:
            try:
                user_input = input("You: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nGoodbye!")
                break

            if not user_input:
                continue
            if user_input.lower() in ("exit", "quit", "q"):
                print("Clio: Have an awesome day! 👋")
                break

            reply = session.interact(user_input)
            print(f"Clio: {reply}\n")
    finally:
        session.close()

    return 0


def list_workflows(db_path: Optional[str] = None) -> int:
    """Lists all remembered task workflows in task memory."""
    memory = get_memory_engine(db_path=db_path)
    try:
        workflows = memory.list_workflows()
        if not workflows:
            print("No workflows found in task memory database.")
            return 0

        print(f"Remembered Workflows ({len(workflows)} total):")
        for wf in workflows:
            print(f"  • {wf['id']}: '{wf['name']}' ({wf['description']})")
        return 0
    finally:
        memory.close()


def run_teach_mode(db_path: Optional[str] = None) -> int:
    """Interactively creates a new workflow via step prompts and saves it to task memory."""
    print("=" * 60)
    print("🎓 Clio Teach Mode — Record a New Task Workflow")
    print("=" * 60)
    try:
        wf_name = input("Workflow Name (e.g. 'Format Release Notes'): ").strip()
        if not wf_name:
            print("Aborted: Workflow name is required.")
            return 1
        wf_desc = input("Workflow Description: ").strip() or wf_name
        trigger = input("Natural Language Trigger (e.g. 'format my release notes'): ").strip() or wf_name.lower()
        app_bundle = input("Target App Bundle ID (e.g. 'com.apple.Notes' or 'com.apple.Safari'): ").strip() or "com.apple.Notes"

        print("\nEnter steps sequentially. Supported actions: launch, focus, hotkey, type, paste, open_url.")
        print("Enter an empty line when finished.\n")

        steps: List[WorkflowStep] = []
        step_idx = 1
        while True:
            action_raw = input(f"Step {step_idx} Action [launch/focus/hotkey/type/paste/open_url/done]: ").strip().lower()
            if not action_raw or action_raw in ("done", "finish", "q"):
                break

            if action_raw == "launch":
                steps.append(
                    WorkflowStep(
                        step_id=f"step_{step_idx}",
                        order=step_idx,
                        description=f"Launch application {app_bundle}",
                        action=ActionType.LAUNCH_APP,
                        target={"bundle_id": app_bundle},
                    )
                )
            elif action_raw == "focus":
                steps.append(
                    WorkflowStep(
                        step_id=f"step_{step_idx}",
                        order=step_idx,
                        description=f"Focus application {app_bundle}",
                        action=ActionType.FOCUS_APP,
                        target={"bundle_id": app_bundle},
                    )
                )
            elif action_raw == "hotkey":
                keys_str = input("  Keys separated by comma (e.g. 'cmd,n'): ").strip()
                keys = [k.strip().lower() for k in keys_str.split(",") if k.strip()]
                steps.append(
                    WorkflowStep(
                        step_id=f"step_{step_idx}",
                        order=step_idx,
                        description=f"Press hotkey: {'+'.join(keys)}",
                        action=ActionType.PRESS_HOTKEY,
                        payload={"keys": keys},
                    )
                )
            elif action_raw in ("type", "text"):
                text = input("  Text to type: ")
                steps.append(
                    WorkflowStep(
                        step_id=f"step_{step_idx}",
                        order=step_idx,
                        description=f"Type text: {text[:30]}...",
                        action=ActionType.TYPE_TEXT,
                        payload={"text": text},
                    )
                )
            elif action_raw == "paste":
                text = input("  Text to paste: ")
                steps.append(
                    WorkflowStep(
                        step_id=f"step_{step_idx}",
                        order=step_idx,
                        description=f"Paste text: {text[:30]}...",
                        action=ActionType.PASTE_TEXT,
                        payload={"text": text},
                    )
                )
            elif action_raw == "open_url":
                url = input("  URL: ").strip()
                steps.append(
                    WorkflowStep(
                        step_id=f"step_{step_idx}",
                        order=step_idx,
                        description=f"Open URL: {url}",
                        action=ActionType.OPEN_URL,
                        payload={"url": url},
                    )
                )
            else:
                print(f"Unknown action '{action_raw}'. Try launch, focus, hotkey, type, paste, open_url.")
                continue

            step_idx += 1

        if not steps:
            print("No steps recorded. Aborting.")
            return 1

        slug = re.sub(r"[^a-z0-9_]+", "_", wf_name.lower()).strip("_")
        wf_id = f"wf_{slug}"
        spec = WorkflowSpec(
            id=wf_id,
            name=wf_name,
            description=wf_desc,
            triggers={"canonical": trigger, "aliases": []},
            target_app={"bundle_id": app_bundle},
            steps=steps,
        )

        memory = get_memory_engine(db_path=db_path)
        try:
            memory.save_workflow(spec)
            print(f"\n🎉 Successfully recorded workflow '{wf_name}' ({wf_id}) with {len(steps)} steps!")
            print(f"You can now invoke it with: \"{trigger}\" in chat mode!")
            return 0
        finally:
            memory.close()
    except (EOFError, KeyboardInterrupt):
        print("\nTeach mode aborted.")
        return 1


def run_server_mode(
    host: str = "127.0.0.1",
    port: int = 8765,
    mock: Optional[bool] = None,
    db_path: Optional[str] = None,
    tone: str = "vibrant",
) -> int:
    """Runs the Clio Spotlight REST & SSE server in the foreground."""
    import time
    from src.server.server import ClioServer

    is_mock = (sys.platform != "darwin" or os.environ.get("CI") == "true") if mock is None else bool(mock)
    memory = get_memory_engine(db_path=db_path)
    actuator = get_actuator(mode=ActuatorMode.MOCK if is_mock else ActuatorMode.MACOS)
    server = ClioServer(
        host=host,
        port=port,
        memory=memory,
        actuator=actuator,
        tone=tone,
    )
    server.start()
    print("=" * 60)
    print(f"🌟 Clio Spotlight Server running at http://{host}:{port}")
    print(f"   Mode: {'Headless Simulation (Mock)' if is_mock else 'Live OS Control'}")
    print(f"   Tone: {tone.capitalize()}")
    print("   Press Ctrl+C to terminate.")
    print("=" * 60)
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\nStopping Clio Server...")
    finally:
        server.stop()
    return 0


def run_ui_mode(
    host: str = "127.0.0.1",
    port: int = 8765,
    mock: Optional[bool] = None,
    db_path: Optional[str] = None,
    tone: str = "vibrant",
) -> int:
    """Spawns the Clio server and opens the web Spotlight Floating Bar in the browser."""
    import subprocess
    import time
    import webbrowser
    from src.server.server import ClioServer

    is_mock = (sys.platform != "darwin" or os.environ.get("CI") == "true") if mock is None else bool(mock)
    memory = get_memory_engine(db_path=db_path)
    actuator = get_actuator(mode=ActuatorMode.MOCK if is_mock else ActuatorMode.MACOS)
    server = ClioServer(
        host=host,
        port=port,
        memory=memory,
        actuator=actuator,
        tone=tone,
    )
    server.start()
    url = f"http://{host}:{port}/"
    print("=" * 60)
    print(f"🌟 Clio Spotlight Floating Bar HUD active at: {url}")
    print("   Opening floating bar in your default browser...")
    print("   Press Ctrl+C to stop the companion HUD.")
    print("=" * 60)
    try:
        webbrowser.open(url)
    except Exception:
        subprocess.Popen(["open", url])

    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\nStopping Clio HUD...")
    finally:
        server.stop()
    return 0


def run_hud_mode(
    host: str = "127.0.0.1",
    port: int = 8765,
    mock: Optional[bool] = None,
    db_path: Optional[str] = None,
    tone: str = "vibrant",
) -> int:
    """Spawns the Clio server and launches the native macOS Spotlight Floating Bar (ClioBar)."""
    import subprocess
    from src.server.server import ClioServer

    is_mock = (sys.platform != "darwin" or os.environ.get("CI") == "true") if mock is None else bool(mock)

    bin_path = Path(__file__).parent.parent / "bin" / "clio-bar"
    if not bin_path.exists():
        print(f"Native binary {bin_path} not found. Compiling with swiftc...")
        src_path = Path(__file__).parent.parent / "src" / "ui" / "ClioBar.swift"
        bin_path.parent.mkdir(parents=True, exist_ok=True)
        res = subprocess.run(
            [
                "swiftc", "-O", "-target", "arm64-apple-macosx15.0",
                "-framework", "AppKit", "-framework", "ApplicationServices",
                "-framework", "SwiftUI", "-framework", "Combine",
                "-framework", "AVFoundation", "-framework", "Speech",
                str(src_path), "-o", str(bin_path),
            ],
            capture_output=True,
            text=True,
        )
        if res.returncode != 0:
            print(f"Compilation warning: {res.stderr}")
            print("Falling back to browser Spotlight HUD...")
            return run_ui_mode(host=host, port=port, mock=is_mock, db_path=db_path, tone=tone)

    memory = get_memory_engine(db_path=db_path)
    actuator = get_actuator(mode=ActuatorMode.MOCK if is_mock else ActuatorMode.MACOS)
    server = ClioServer(
        host=host,
        port=port,
        memory=memory,
        actuator=actuator,
        tone=tone,
    )
    server.start()
    print("=" * 60)
    print(f"🌟 Clio Spotlight Server running at http://{host}:{port}")
    print(f"🚀 Launching native macOS Spotlight Floating Bar: {bin_path}")
    print("   Press Ctrl+C or Cmd+Q to exit.")
    print("=" * 60)

    proc = subprocess.Popen([str(bin_path)])
    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
    finally:
        server.stop()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    """CLI argument parser entry point."""
    parser = argparse.ArgumentParser(
        description="Autonomous Desktop Companion (Task Memory Automator)"
    )
    parser.add_argument(
        "--hud",
        action="store_true",
        help="Launch the native macOS Spotlight Floating Bar app",
    )
    parser.add_argument(
        "--ui",
        action="store_true",
        help="Launch the web Spotlight Floating Bar HUD in your browser",
    )
    parser.add_argument(
        "--server",
        action="store_true",
        help="Run the Clio Spotlight HTTP/SSE server in foreground",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Server port (default: 8765)",
    )
    parser.add_argument(
        "--benchmark",
        action="store_true",
        help="Run the multi-domain autonomous benchmark suite",
    )
    parser.add_argument(
        "--chat",
        action="store_true",
        help="Launch interactive conversational companion chat REPL",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List all learned workflows in task memory",
    )
    parser.add_argument(
        "--teach",
        action="store_true",
        help="Interactively demonstrate/record a new workflow",
    )
    parser.add_argument(
        "--seed",
        action="store_true",
        help="Seed the 3 canonical benchmark workflows into task memory",
    )
    parser.add_argument(
        "--tone",
        choices=["vibrant", "concise", "zen", "developer"],
        default="vibrant",
        help="Select companion commentary tone profile (default: vibrant)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Enable live OS hardware/process control instead of headless simulation",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Force headless simulation mode (mock actuator and mock capture)",
    )
    parser.add_argument(
        "--db",
        type=str,
        default=None,
        help="Path to SQLite task memory database (defaults to ~/.task_automator/task_memory.db)",
    )

    args = parser.parse_args(argv)

    force_mock = bool(args.mock)
    is_live = bool(args.live) or (sys.platform == "darwin" and os.environ.get("CI") != "true" and not force_mock)
    use_mock = not is_live

    if args.hud:
        return run_hud_mode(
            port=args.port,
            mock=use_mock,
            db_path=args.db,
            tone=args.tone,
        )
    elif args.ui:
        return run_ui_mode(
            port=args.port,
            mock=use_mock,
            db_path=args.db,
            tone=args.tone,
        )
    elif args.server:
        return run_server_mode(
            port=args.port,
            mock=use_mock,
            db_path=args.db,
            tone=args.tone,
        )
    elif args.seed:
        memory = get_memory_engine(db_path=args.db, auto_seed=False)
        try:
            count = seed_default_workflows(memory)
            print(f"Seeded {count} canonical workflows into task memory.")
            return 0
        finally:
            memory.close()
    elif args.benchmark:
        return run_benchmark_mode(mock=not args.live)
    elif args.list:
        return list_workflows(db_path=args.db)
    elif args.teach:
        return run_teach_mode(db_path=args.db)
    else:
        # Default or --chat
        return run_chat_repl(tone=args.tone, mock=not args.live, db_path=args.db)


if __name__ == "__main__":
    sys.exit(main())
