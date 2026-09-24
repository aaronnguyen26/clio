"""Unit tests for Main CLI Entry Point (src.main).

Tests cover:
- CLI argument parsing (--benchmark, --list, --seed, --chat, --teach, --tone, --live, --db)
- Auto-seeding of canonical benchmark workflows
- Workflow listing
- Non-interactive chat execution
- Teach mode parsing and abort logic
"""

from __future__ import annotations

import io
from pathlib import Path
import sys
from unittest.mock import patch
import pytest

from src.main import (
    get_memory_engine,
    list_workflows,
    main,
    run_benchmark_mode,
    run_chat_repl,
    run_teach_mode,
    seed_default_workflows,
)
from src.memory.engine import TaskMemoryEngine


class TestMainCLI:
    """Tests for main CLI entrypoint and subcommand routing."""

    def test_seed_default_workflows(self, tmp_path: Path) -> None:
        """Verifies canonical workflows are correctly seeded into a new database."""
        db_file = str(tmp_path / "test_memory.db")
        memory = TaskMemoryEngine(db_path=db_file)
        try:
            assert len(memory.list_workflows()) == 0
            seeded = seed_default_workflows(memory)
            assert seeded == 3
            assert len(memory.list_workflows()) == 3

            # Second call should not duplicate
            seeded_again = seed_default_workflows(memory)
            assert seeded_again == 0
            assert len(memory.list_workflows()) == 3
        finally:
            memory.close()

    def test_get_memory_engine_auto_seed(self, tmp_path: Path) -> None:
        """Verifies get_memory_engine auto-seeds when empty."""
        db_file = str(tmp_path / "auto_seed.db")
        memory = get_memory_engine(db_path=db_file, auto_seed=True)
        try:
            assert len(memory.list_workflows()) == 3
        finally:
            memory.close()

    def test_benchmark_mode_mock(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Verifies run_benchmark_mode runs and passes cleanly in mock mode."""
        exit_code = run_benchmark_mode(mock=True)
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "ALL BENCHMARKS CERTIFIED CLEAN!" in captured.out
        assert "R6/R7 PASS" in captured.out

    def test_list_workflows_empty_and_populated(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Verifies list_workflows displays registered workflows or clean empty state."""
        db_file = str(tmp_path / "list_test.db")
        memory = TaskMemoryEngine(db_path=db_file)
        
        # Test clean empty state
        exit_code = list_workflows(db_path=db_file)
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "No workflows found" in captured.out

        # Test populated state
        seed_default_workflows(memory)
        memory.close()

        exit_code = list_workflows(db_path=db_file)
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "Remembered Workflows" in captured.out
        assert "wf_notes_weekly_todo" in captured.out

    def test_chat_repl_piped_execution(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        """Verifies conversational chat repl processes user workflow commands and exits."""
        db_file = str(tmp_path / "chat_test.db")
        memory = TaskMemoryEngine(db_path=db_file)
        seed_default_workflows(memory)
        memory.close()

        inputs = iter(["write my weekly to-do list", "exit"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))

        exit_code = run_chat_repl(tone="concise", mock=True, db_path=db_file)
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "Clio — Autonomous Desktop Companion" in captured.out
        assert "Create Weekly To-Do in Apple Notes" in captured.out

    def test_teach_mode_aborted_on_empty_name(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        """Verifies teach mode aborts gracefully when workflow name is empty."""
        monkeypatch.setattr("builtins.input", lambda _: "")
        exit_code = run_teach_mode(db_path=":memory:")
        assert exit_code == 1
        captured = capsys.readouterr()
        assert "Aborted: Workflow name is required" in captured.out

    def test_teach_mode_successful_creation(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        """Verifies interactive teach mode builds and persists a new workflow."""
        db_file = str(tmp_path / "teach_test.db")
        inputs = iter([
            "Format Release Notes",      # Name
            "Creates release note",     # Desc
            "format release notes",      # Trigger
            "com.apple.Notes",           # App bundle
            "launch",                    # Step 1
            "focus",                     # Step 2
            "paste",                     # Step 3
            "Release Notes v1.0",        # text
            "done",                      # Finish
        ])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))

        exit_code = run_teach_mode(db_path=db_file)
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "Successfully recorded workflow 'Format Release Notes'" in captured.out

        memory = TaskMemoryEngine(db_path=db_file)
        try:
            wf = memory.get_workflow("wf_format_release_notes")
            assert wf is not None
            assert len(wf.steps) == 3
        finally:
            memory.close()

    def test_run_server_mode_interrupt(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        """Verifies run_server_mode starts and handles graceful KeyboardInterrupt."""
        from src.main import run_server_mode

        def _raise_interrupt(_: float) -> None:
            raise KeyboardInterrupt()

        monkeypatch.setattr("time.sleep", _raise_interrupt)
        code = run_server_mode(port=8769, mock=True, db_path=":memory:", tone="zen")
        assert code == 0
        captured = capsys.readouterr()
        assert "Clio Spotlight Server running" in captured.out
        assert "Stopping Clio Server" in captured.out

    def test_run_ui_mode_interrupt(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        """Verifies run_ui_mode opens browser and terminates gracefully."""
        from src.main import run_ui_mode

        monkeypatch.setattr("webbrowser.open", lambda _: True)
        monkeypatch.setattr("time.sleep", lambda _: (_ for _ in ()).throw(KeyboardInterrupt()))
        code = run_ui_mode(port=8770, mock=True, db_path=":memory:", tone="concise")
        assert code == 0
        captured = capsys.readouterr()
        assert "Clio Spotlight Floating Bar HUD active" in captured.out

    def test_main_cli_dispatch_options(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        """Verifies argument parsing and dispatching across all flags."""
        db_file = str(tmp_path / "cli_test.db")

        # --benchmark
        assert main(["--benchmark"]) == 0
        # --seed
        assert main(["--seed", "--db", db_file]) == 0
        # --list
        assert main(["--list", "--db", db_file]) == 0

        # Mock hud / ui / server runners
        monkeypatch.setattr("src.main.run_server_mode", lambda **kw: 42)
        assert main(["--server", "--port", "9999"]) == 42

        monkeypatch.setattr("src.main.run_ui_mode", lambda **kw: 43)
        assert main(["--ui"]) == 43

        monkeypatch.setattr("src.main.run_hud_mode", lambda **kw: 44)
        assert main(["--hud"]) == 44

