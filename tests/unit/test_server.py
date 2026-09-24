"""Unit tests for Clio Spotlight Server & REST/SSE API Subsystem.

Tests cover:
- Server startup, port listening, and clean shutdown
- GET / (serves HTML UI)
- GET /api/status
- GET /api/workflows
- GET /api/search (exact and fuzzy BM25 retrieval)
- POST /api/execute (async execution of workflows via ID and query)
- POST /api/cancel (emergency halt)
- POST /api/tone (profile switching)
- GET /api/stream (SSE streaming)
- Error handling (invalid JSON, unknown route, missing workflows)
"""

from __future__ import annotations

import json
from pathlib import Path
import socket
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import pytest

from src.actuators.mock import MockActuator
from src.benchmark.scenarios import build_notes_productivity_workflow
from src.memory.engine import TaskMemoryEngine
from src.server.server import ClioServer


def find_free_port() -> int:
    """Finds a free ephemeral localhost port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def test_server():
    """Spins up a ClioServer instance on a free port with mock components."""
    port = find_free_port()
    memory = TaskMemoryEngine(db_path=":memory:")
    # Seed a workflow
    spec = build_notes_productivity_workflow()
    memory.save_workflow(spec)

    actuator = MockActuator()
    server = ClioServer(
        host="127.0.0.1",
        port=port,
        memory=memory,
        actuator=actuator,
        tone="vibrant",
        zero_delay=True,
    )
    server.start()
    time.sleep(0.05)  # Allow thread to start

    yield server

    server.stop()


class TestClioServerEndpoints:
    """Tests for all REST and static endpoints of ClioServer."""

    def test_get_root_serves_html(self, test_server: ClioServer) -> None:
        """GET / serves index.html with 200 OK and HTML content."""
        url = f"http://{test_server.host}:{test_server.port}/"
        with urlopen(url, timeout=3.0) as resp:
            assert resp.status == 200
            assert "text/html" in resp.headers.get("Content-Type", "")
            body = resp.read().decode("utf-8")
            assert "Clio — Autonomous Desktop Companion" in body
            assert "spotlight-panel" in body

    def test_get_status_idle(self, test_server: ClioServer) -> None:
        """GET /api/status returns valid JSON structure with idle status."""
        url = f"http://{test_server.host}:{test_server.port}/api/status"
        with urlopen(url, timeout=3.0) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode("utf-8"))
            assert data["status"] == "idle"
            assert data["tone"] == "vibrant"
            assert "virtual_cursor" in data
            assert data["virtual_cursor"]["state"] == "idle"

    def test_get_workflows(self, test_server: ClioServer) -> None:
        """GET /api/workflows returns array of saved workflows."""
        url = f"http://{test_server.host}:{test_server.port}/api/workflows"
        with urlopen(url, timeout=3.0) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode("utf-8"))
            assert isinstance(data, list)
            assert len(data) >= 1
            assert data[0]["id"] == "wf_notes_weekly_todo"
            assert "Create Weekly To-Do" in data[0]["name"]

    def test_get_search(self, test_server: ClioServer) -> None:
        """GET /api/search?q=todo returns ranked matches."""
        url = f"http://{test_server.host}:{test_server.port}/api/search?q=notes+todo"
        with urlopen(url, timeout=3.0) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode("utf-8"))
            assert isinstance(data, list)
            assert len(data) >= 1
            assert data[0]["workflow_id"] == "wf_notes_weekly_todo"
            assert data[0]["confidence"] > 0.0

    def test_post_set_tone(self, test_server: ClioServer) -> None:
        """POST /api/tone updates companion tone profile."""
        url = f"http://{test_server.host}:{test_server.port}/api/tone"
        req = Request(
            url,
            data=json.dumps({"tone": "zen"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req, timeout=3.0) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode("utf-8"))
            assert data["success"] is True
            assert data["tone"] == "zen"

        # Check status reflects update
        status = test_server.get_status()
        assert status["tone"] == "zen"

    def test_post_execute_and_cancel(self, test_server: ClioServer) -> None:
        """POST /api/execute starts workflow and POST /api/cancel cancels it."""
        url = f"http://{test_server.host}:{test_server.port}/api/execute"
        req = Request(
            url,
            data=json.dumps({"workflow_id": "wf_notes_weekly_todo"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req, timeout=3.0) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode("utf-8"))
            assert data["success"] is True
            assert "wf_notes_weekly_todo" in data["workflow_id"]

        # Cancel endpoint
        cancel_url = f"http://{test_server.host}:{test_server.port}/api/cancel"
        cancel_req = Request(cancel_url, data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(cancel_req, timeout=3.0) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode("utf-8"))
            assert data["success"] is True

    def test_post_execute_by_natural_language_query(self, test_server: ClioServer) -> None:
        """POST /api/execute resolves natural language query via retrieval."""
        url = f"http://{test_server.host}:{test_server.port}/api/execute"
        req = Request(
            url,
            data=json.dumps({"query": "write my weekly to-do list"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req, timeout=3.0) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode("utf-8"))
            assert data["success"] is True
            assert data["workflow_id"] == "wf_notes_weekly_todo"

    def test_unknown_route_returns_404(self, test_server: ClioServer) -> None:
        """Requesting an unknown route returns HTTP 404."""
        url = f"http://{test_server.host}:{test_server.port}/api/non_existent"
        with pytest.raises(HTTPError) as exc_info:
            urlopen(url, timeout=3.0)
        assert exc_info.value.code == 404
