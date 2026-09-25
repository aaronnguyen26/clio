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
import shutil
import socket
import tempfile
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
    temp_dir = tempfile.mkdtemp(prefix="clio_server_test_rec_")
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
    server.demonstration_capture._recordings_base_dir = Path(temp_dir)
    server.demonstration_capture._session_dir = Path(temp_dir) / server.demonstration_capture._session_id
    server.start()
    time.sleep(0.05)  # Allow thread to start

    yield server

    server.stop()
    shutil.rmtree(temp_dir, ignore_errors=True)


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
        """GET /api/workflows returns array of saved workflows with ordered steps."""
        url = f"http://{test_server.host}:{test_server.port}/api/workflows"
        with urlopen(url, timeout=3.0) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode("utf-8"))
            assert isinstance(data, list)
            assert len(data) >= 1
            assert data[0]["id"] == "wf_notes_weekly_todo"
            assert "Create Weekly To-Do" in data[0]["name"]
            assert "steps" in data[0]
            assert isinstance(data[0]["steps"], list)
            assert len(data[0]["steps"]) >= 1
            assert "description" in data[0]["steps"][0]
            assert "order" in data[0]["steps"][0]

    def test_get_single_workflow(self, test_server: ClioServer) -> None:
        """GET /api/workflows/<id> returns specific workflow with ordered steps."""
        url = f"http://{test_server.host}:{test_server.port}/api/workflows/wf_notes_weekly_todo"
        with urlopen(url, timeout=3.0) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode("utf-8"))
            assert data["id"] == "wf_notes_weekly_todo"
            assert "steps" in data
            assert len(data["steps"]) >= 1
            assert data["steps"][0]["order"] == 1

    def test_get_search(self, test_server: ClioServer) -> None:
        """GET /api/search?q=todo returns ranked matches with steps."""
        url = f"http://{test_server.host}:{test_server.port}/api/search?q=notes+todo"
        with urlopen(url, timeout=3.0) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode("utf-8"))
            assert isinstance(data, list)
            assert len(data) >= 1
            assert data[0]["workflow_id"] == "wf_notes_weekly_todo"
            assert data[0]["confidence"] > 0.0
            assert "steps" in data[0]
            assert isinstance(data[0]["steps"], list)

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

    def test_post_chat_greeting(self, test_server: ClioServer) -> None:
        """POST /api/chat handles greetings via companion dialogue."""
        url = f"http://{test_server.host}:{test_server.port}/api/chat"
        req = Request(
            url,
            data=json.dumps({"message": "Hello Clio!"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req, timeout=3.0) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode("utf-8"))
            assert "Autonomous Desktop Companion" in data["reply"] or "Hey" in data["reply"]
            assert data["state"] == "idle"

    def test_post_chat_executes_workflow(self, test_server: ClioServer) -> None:
        """POST /api/chat recognizes direct trigger and starts workflow."""
        url = f"http://{test_server.host}:{test_server.port}/api/chat"
        req = Request(
            url,
            data=json.dumps({"message": "write my weekly to-do list"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req, timeout=3.0) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode("utf-8"))
            assert data["state"] == "executing"
            assert data["triggered_workflow"] == "wf_notes_weekly_todo"
            assert data["execution"]["success"] is True

    def test_post_chat_empty_message_returns_400(self, test_server: ClioServer) -> None:
        """POST /api/chat with empty body returns 400 Bad Request."""
        url = f"http://{test_server.host}:{test_server.port}/api/chat"
        req = Request(
            url,
            data=json.dumps({"message": "   "}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(HTTPError) as exc_info:
            urlopen(req, timeout=3.0)
        assert exc_info.value.code == 400

    def test_execute_query_prioritizes_memory_workflow_over_dynamic_intent(
        self, test_server: ClioServer
    ) -> None:
        """POST /api/execute prioritizes real demonstrated workflows over dynamic intent."""
        # Query that could also be matched by dynamic intent synthesizer
        url = f"http://{test_server.host}:{test_server.port}/api/execute"
        req = Request(
            url,
            data=json.dumps({"query": "create weekly to-do"}).encode("utf-8"),
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

    def test_record_and_execute_by_anaphoric_reference(self, test_server: ClioServer) -> None:
        """Recording an action and then telling Clio 'perform that action' executes the recorded workflow."""
        # 1. Start recording
        start_url = f"http://{test_server.host}:{test_server.port}/api/record/start"
        start_req = Request(start_url, data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(start_req, timeout=3.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            assert data["success"] is True

        # Verify status says recording
        status_url = f"http://{test_server.host}:{test_server.port}/api/record/status"
        with urlopen(status_url, timeout=3.0) as resp:
            st = json.loads(resp.read().decode("utf-8"))
            assert st["is_recording"] is True

        # 2. Feed simulated event
        event_url = f"http://{test_server.host}:{test_server.port}/api/record/event"
        ev_payload = {
            "event_type": "click",
            "x": 200,
            "y": 300,
            "button": "left",
            "bundle_id": "com.apple.Notes",
            "window_bounds": {"x": 100, "y": 100, "width": 800, "height": 600},
        }
        ev_req = Request(
            event_url,
            data=json.dumps(ev_payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(ev_req, timeout=3.0) as resp:
            ev_data = json.loads(resp.read().decode("utf-8"))
            assert ev_data["success"] is True

        # 3. Stop recording
        stop_url = f"http://{test_server.host}:{test_server.port}/api/record/stop"
        stop_payload = {
            "name": "Test Recorded Note",
            "trigger": "record test note",
            "description": "A recorded note task",
        }
        stop_req = Request(
            stop_url,
            data=json.dumps(stop_payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(stop_req, timeout=3.0) as resp:
            stop_data = json.loads(resp.read().decode("utf-8"))
            assert stop_data["success"] is True
            recorded_id = stop_data["workflow_id"]
            assert recorded_id is not None

        # Verify status is not recording anymore, and last_recorded_workflow_id is set
        with urlopen(status_url, timeout=3.0) as resp:
            st = json.loads(resp.read().decode("utf-8"))
            assert st["is_recording"] is False
            assert st["last_recorded_workflow_id"] == recorded_id

        # 4. Tell Clio: "perform that action" via /api/execute
        exec_url = f"http://{test_server.host}:{test_server.port}/api/execute"
        exec_req = Request(
            exec_url,
            data=json.dumps({"query": "perform that action"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(exec_req, timeout=3.0) as resp:
            exec_data = json.loads(resp.read().decode("utf-8"))
            assert exec_data["success"] is True
            assert exec_data["workflow_id"] == recorded_id

    def test_record_and_chat_anaphoric_trigger(self, test_server: ClioServer) -> None:
        """Telling Clio via chat 'do that action' executes the recorded workflow."""
        test_server.start_recording()
        test_server.feed_recording_event({
            "event_type": "click",
            "x": 150,
            "y": 250,
            "button": "left",
            "bundle_id": "com.apple.Notes",
        })
        stop_res = test_server.stop_recording(name="Click Something", trigger="click something")
        recorded_id = stop_res["workflow_id"]

        chat_url = f"http://{test_server.host}:{test_server.port}/api/chat"
        chat_req = Request(
            chat_url,
            data=json.dumps({"message": "do that action"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(chat_req, timeout=3.0) as resp:
            chat_data = json.loads(resp.read().decode("utf-8"))
            assert chat_data["state"] == "executing"
            assert chat_data["triggered_workflow"] == recorded_id
            assert chat_data["execution"]["success"] is True

    def test_workflow_update_endpoint(self, test_server: ClioServer) -> None:
        """POST /api/workflows/update safely modifies workflow metadata in memory."""
        update_url = f"http://{test_server.host}:{test_server.port}/api/workflows/update"
        payload = {
            "workflow_id": "wf_notes_weekly_todo",
            "name": "Updated To-Do Workflow",
            "triggers": {"canonical": "run updated todo", "aliases": ["my todo"]},
            "description": "Updated description",
        }
        req = Request(
            update_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req, timeout=3.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            assert data["success"] is True
            assert data["name"] == "Updated To-Do Workflow"

        # Verify in memory
        wf = test_server.memory.get_workflow("wf_notes_weekly_todo")
        assert wf is not None
        assert wf.name == "Updated To-Do Workflow"
        assert wf.triggers["canonical"] == "run updated todo"

    def test_record_stop_ingests_client_buffered_events(self, test_server: ClioServer) -> None:
        """POST /api/record/stop with client-buffered events properly dissects them into steps."""
        test_server.start_recording()
        stop_url = f"http://{test_server.host}:{test_server.port}/api/record/stop"
        buffered_events = [
            {
                "event_type": "mouse_down",
                "x": 200,
                "y": 300,
                "button": "left",
                "bundle_id": "com.apple.calculator",
                "timestamp": time.time(),
            },
            {
                "event_type": "mouse_up",
                "x": 200,
                "y": 300,
                "button": "left",
                "bundle_id": "com.apple.calculator",
                "timestamp": time.time() + 0.05,
            },
            {
                "event_type": "key_down",
                "key": "5",
                "bundle_id": "com.apple.calculator",
                "timestamp": time.time() + 0.1,
            },
        ]
        stop_payload = {
            "name": "Calculate Five",
            "trigger": "calculate five",
            "events": buffered_events,
        }
        req = Request(
            stop_url,
            data=json.dumps(stop_payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req, timeout=3.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            assert data["success"] is True
            assert data["total_steps"] >= 2
            assert len(data["steps"]) >= 2
            # Check steps were persisted
            wf = test_server.memory.get_workflow(data["workflow_id"])
            assert wf is not None
            assert len(wf.steps) >= 2

    def test_feed_recording_event_supports_batch_events(self, test_server: ClioServer) -> None:
        """POST /api/record/feed supports batch events payload."""
        test_server.start_recording()
        feed_url = f"http://{test_server.host}:{test_server.port}/api/record/feed"
        batch_payload = {
            "events": [
                {"event_type": "click", "x": 100, "y": 100, "button": "left"},
                {"event_type": "click", "x": 150, "y": 150, "button": "left"},
            ]
        }
        req = Request(
            feed_url,
            data=json.dumps(batch_payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req, timeout=3.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            assert data["success"] is True
            assert data["ingested"] == 2
        test_server.stop_recording()

