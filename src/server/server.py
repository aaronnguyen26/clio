"""Clio Spotlight Server and API Subsystem.

Belongs to Clio Spotlight HUD architecture.
Pure Python standard library (http.server, threading, json, queue).
Zero third-party pip dependencies.
"""

from __future__ import annotations

from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import queue
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from src.actuators.base import BaseActuator
from src.actuators.types import ActuatorMode
from src.actuators.factory import get_actuator
from src.actuators.virtual_cursor import VirtualCursor, VirtualCursorEvent
from src.companion.commentary import CommentaryEngine
from src.companion.dialogue import CompanionDialogueEngine, DialogueState
from src.executor.events import EventType, ExecutionEvent, ExecutionEventBus
from src.executor.executor import AutonomousWorkflowExecutor, ExecutionResult
from src.memory.capture import LiveDemonstrationCapture
from src.memory.engine import TaskMemoryEngine
from src.memory.models import WorkflowSpec
from src.memory.retrieval import NLRetrievalEngine

logger = logging.getLogger(__name__)


class ClioServer:
    """Manages the backend HTTP server, SSE event distribution, and workflow executor for Clio HUD."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8765,
        memory: Optional[TaskMemoryEngine] = None,
        actuator: Optional[BaseActuator] = None,
        tone: str = "vibrant",
        zero_delay: bool = False,
    ) -> None:
        self.host = host
        self.port = port
        self.zero_delay = zero_delay

        # Core subsystems
        self.bus = ExecutionEventBus()
        self.memory = memory or TaskMemoryEngine()
        self.actuator = actuator or get_actuator(mode=ActuatorMode.AUTO)

        # Determine if actuator is mock or live macOS
        from src.actuators.mock import MockActuator
        is_mock = isinstance(self.actuator, MockActuator) or (getattr(self.actuator, "mode", None) == "mock")
        if sys.platform != "darwin":
            is_mock = True

        self.virtual_cursor = VirtualCursor(
            initial_x=0.0,
            initial_y=0.0,
            mock=is_mock,
        )

        self.executor = AutonomousWorkflowExecutor(
            actuator=self.actuator,
            bus=self.bus,
            memory_engine=self.memory,
            virtual_cursor=self.virtual_cursor,
            zero_delay=zero_delay,
            use_virtual_cursor=True,
        )

        self.demonstration_capture = LiveDemonstrationCapture(
            memory=self.memory,
            mock=is_mock,
        )

        self.dialogue = CompanionDialogueEngine(
            memory=self.memory,
            tone=tone,
        )

        self.commentary = CommentaryEngine(
            bus=self.bus,
            tone=tone,
            throttle_ms=400.0,
        )

        # State tracking
        self._lock = threading.RLock()
        self._is_executing = False
        self._current_workflow: Optional[WorkflowSpec] = None
        self._current_step_index = 0
        self._total_steps = 0
        self._recent_commentary: List[str] = []
        self._sse_clients: List[queue.Queue[Dict[str, Any]]] = []
        self._dynamic_specs: Dict[str, WorkflowSpec] = {}

        # Wire up listeners
        self._wire_listeners()

        # HTTP Server
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._server_thread: Optional[threading.Thread] = None

    def _wire_listeners(self) -> None:
        """Connects pub/sub listeners to broadcast events to SSE queues."""
        # 1. Execution Event Bus
        def _on_execution_event(ev: ExecutionEvent) -> None:
            with self._lock:
                if ev.event_type == EventType.TASK_STARTED:
                    self._is_executing = True
                    self._current_step_index = 0
                    self._total_steps = ev.total_steps
                elif ev.event_type in (EventType.TASK_COMPLETED, EventType.TASK_FAILED, EventType.EMERGENCY_STOP):
                    self._is_executing = False
                    self._current_step_index = self._total_steps
                elif ev.event_type == EventType.ACTION_STARTING:
                    self._current_step_index = ev.step_index

            data = {
                "type": "execution",
                "event_type": ev.event_type.value,
                "task_id": ev.task_id,
                "step_index": ev.step_index,
                "total_steps": ev.total_steps,
                "message": ev.message,
                "target_app": ev.target_app,
                "timestamp": ev.timestamp,
            }
            self._broadcast_sse(data)

        self.bus.subscribe(_on_execution_event)

        # 2. Commentary Engine
        def _on_commentary_line(line: str) -> None:
            with self._lock:
                self._recent_commentary.append(line)
                if len(self._recent_commentary) > 30:
                    self._recent_commentary.pop(0)

            data = {
                "type": "commentary",
                "text": line,
                "timestamp": time.time(),
            }
            self._broadcast_sse(data)

        self.commentary.add_commentary_listener(_on_commentary_line)

        # 3. Virtual Cursor
        def _on_cursor_event(ev: VirtualCursorEvent) -> None:
            vel = getattr(ev, "velocity", (0.0, 0.0)) or (0.0, 0.0)
            data = {
                "type": "cursor",
                "x": round(getattr(ev, "x", 0.0), 1),
                "y": round(getattr(ev, "y", 0.0), 1),
                "vx": round(vel[0], 1) if len(vel) > 0 else 0.0,
                "vy": round(vel[1], 1) if len(vel) > 1 else 0.0,
                "state": ev.state.value if hasattr(ev.state, "value") else str(ev.state),
                "is_visible": getattr(ev, "is_visible", True),
                "target_pid": getattr(ev, "target_pid", None),
                "timestamp": getattr(ev, "timestamp", time.time()),
            }
            self._broadcast_sse(data)

        self.virtual_cursor.add_listener(_on_cursor_event)

    def _broadcast_sse(self, data: Dict[str, Any]) -> None:
        """Pushes a message dict to all active SSE queues."""
        with self._lock:
            dead_clients: List[queue.Queue[Dict[str, Any]]] = []
            for q in self._sse_clients:
                try:
                    q.put_nowait(data)
                except queue.Full:
                    dead_clients.append(q)
            for dead in dead_clients:
                if dead in self._sse_clients:
                    self._sse_clients.remove(dead)

    def register_sse_client(self) -> queue.Queue[Dict[str, Any]]:
        """Registers a new SSE listener queue."""
        q: queue.Queue[Dict[str, Any]] = queue.Queue(maxsize=100)
        with self._lock:
            self._sse_clients.append(q)
        return q

    def unregister_sse_client(self, q: queue.Queue[Dict[str, Any]]) -> None:
        """Unregisters an SSE listener queue."""
        with self._lock:
            if q in self._sse_clients:
                self._sse_clients.remove(q)

    def get_status(self) -> Dict[str, Any]:
        """Returns the current runtime status of Clio and the executor."""
        with self._lock:
            cur_wf_dict = None
            if self._current_workflow:
                cur_wf_dict = {
                    "id": self._current_workflow.id,
                    "name": self._current_workflow.name,
                    "description": self._current_workflow.description,
                }

            vc_state = (
                self.virtual_cursor.state.value
                if hasattr(self.virtual_cursor.state, "value")
                else str(self.virtual_cursor.state)
            )

            return {
                "status": "executing" if self._is_executing else "idle",
                "tone": self.commentary.tone,
                "current_workflow": cur_wf_dict,
                "current_step": self._current_step_index,
                "total_steps": self._total_steps,
                "virtual_cursor": {
                    "x": round(self.virtual_cursor.vx, 1),
                    "y": round(self.virtual_cursor.vy, 1),
                    "state": vc_state.lower(),
                    "is_visible": getattr(self.virtual_cursor, "is_visible", True),
                },
                "recording": {
                    "is_recording": self.demonstration_capture.is_recording,
                    "event_count": self.demonstration_capture.event_count,
                    "elapsed_seconds": round(self.demonstration_capture.elapsed_seconds, 1),
                },
                "recent_commentary": list(self._recent_commentary),
            }

    def set_tone(self, tone: str) -> None:
        """Updates tone profile across dialogue and commentary subsystems."""
        self.dialogue.tone = tone
        self.commentary.set_tone(tone)
        self._broadcast_sse({"type": "tone_changed", "tone": tone})

    def start_recording(self) -> Dict[str, Any]:
        """Starts capturing a live user demonstration."""
        success = self.demonstration_capture.start_recording()
        if success:
            self._broadcast_sse({
                "type": "recording",
                "status": "started",
                "timestamp": time.time(),
            })
        return {"success": success, "message": "Demonstration recording started."}

    def stop_recording(
        self,
        name: str = "Demonstrated Task",
        trigger: str = "",
        description: str = "",
    ) -> Dict[str, Any]:
        """Stops capturing, auto-dissects events into WorkflowSpec, and saves to memory."""
        spec = self.demonstration_capture.dissect_and_save(
            name=name,
            canonical_trigger=trigger,
            description=description,
        )
        self._broadcast_sse({
            "type": "recording",
            "status": "stopped",
            "workflow_id": spec.id,
            "name": spec.name,
            "step_count": len(spec.steps),
            "timestamp": time.time(),
        })
        return {
            "success": True,
            "workflow_id": spec.id,
            "name": spec.name,
            "canonical_trigger": spec.triggers.get("canonical", ""),
            "steps": [asdict(s) for s in spec.steps],
            "total_steps": len(spec.steps),
        }

    def feed_recording_event(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Ingests an action or screen context event during demonstration."""
        from src.memory.recorder import RawEvent, WindowBounds
        ev_type = data.get("event_type", "click")
        x = float(data.get("x", 0.0))
        y = float(data.get("y", 0.0))
        btn = data.get("button", "left")
        key = data.get("key", "")
        modifiers = data.get("modifiers", [])
        bundle = data.get("bundle_id") or data.get("app")
        window_bounds = None
        if "window_bounds" in data and isinstance(data["window_bounds"], dict):
            wb = data["window_bounds"]
            window_bounds = WindowBounds(
                x=float(wb.get("x", 0)),
                y=float(wb.get("y", 0)),
                width=float(wb.get("width", 0)),
                height=float(wb.get("height", 0)),
            )
        raw_ev = RawEvent(
            event_type=ev_type,
            timestamp=time.time(),
            x=x,
            y=y,
            button=btn,
            key=key,
            modifiers=modifiers,
            bundle_id=bundle,
            window_bounds=window_bounds,
            is_dock_item=bool(data.get("is_dock_item", False)),
            dock_item_title=str(data.get("dock_item_title", "")),
        )
        self.demonstration_capture.feed_event(raw_ev)
        return {"success": True, "event_count": self.demonstration_capture.event_count}

    def get_recording_status(self) -> Dict[str, Any]:
        """Returns the current demonstration recording status."""
        return {
            "is_recording": self.demonstration_capture.is_recording,
            "event_count": self.demonstration_capture.event_count,
            "elapsed_seconds": round(self.demonstration_capture.elapsed_seconds, 1),
        }

    def search_workflows(self, query: str) -> List[Dict[str, Any]]:
        """Searches remembered workflows via 4-tier NL retrieval and dynamic intent synthesis."""
        matches = self.dialogue.retrieval.query(query)
        results = []
        for m in matches:
            wf = self.memory.get_workflow(m.workflow_id)
            if wf:
                canonical = ""
                if isinstance(wf.triggers, dict):
                    canonical = wf.triggers.get("canonical", "")
                elif isinstance(wf.triggers, list) and wf.triggers:
                    canonical = wf.triggers[0]
                results.append(
                    {
                        "workflow_id": wf.id,
                        "name": wf.name,
                        "description": wf.description,
                        "confidence": round(m.confidence, 3),
                        "match_type": getattr(m, "match_type", "retrieval"),
                        "canonical_trigger": canonical,
                        "step_count": len(wf.steps),
                    }
                )

        # Dynamic Intent matching for arbitrary apps and browser tabs
        from src.executor.intent_synthesizer import DynamicIntentSynthesizer
        dyn_spec = DynamicIntentSynthesizer.parse_intent(query)
        if dyn_spec:
            with self._lock:
                self._dynamic_specs[dyn_spec.id] = dyn_spec
            results.append({
                "workflow_id": dyn_spec.id,
                "name": dyn_spec.name,
                "description": dyn_spec.description,
                "confidence": 0.85,
                "match_type": "dynamic_intent",
                "canonical_trigger": dyn_spec.triggers.get("canonical", query),
                "step_count": len(dyn_spec.steps),
            })
            results.sort(key=lambda x: x.get("confidence", 0.0), reverse=True)
        return results

    def list_workflows(self) -> List[Dict[str, Any]]:
        """Lists all workflows saved in memory."""
        raw_list = self.memory.list_workflows()
        out = []
        for wf in raw_list:
            full_wf = self.memory.get_workflow(wf["id"])
            if full_wf:
                canonical = ""
                if isinstance(full_wf.triggers, dict):
                    canonical = full_wf.triggers.get("canonical", "")
                elif isinstance(full_wf.triggers, list) and full_wf.triggers:
                    canonical = full_wf.triggers[0]
                out.append(
                    {
                        "id": full_wf.id,
                        "name": full_wf.name,
                        "description": full_wf.description,
                        "step_count": len(full_wf.steps),
                        "canonical_trigger": canonical,
                        "target_app": full_wf.target_app,
                    }
                )
        return out

    def delete_workflow(self, workflow_id: str) -> Dict[str, Any]:
        """Deletes a saved workflow from memory and notifies subscribers."""
        if not workflow_id:
            return {"success": False, "error": "No workflow_id provided."}
        success = self.memory.delete_workflow(workflow_id, soft=False)
        if success:
            self._broadcast_sse({
                "type": "workflow_deleted",
                "workflow_id": workflow_id,
                "timestamp": time.time(),
            })
            logger.info("Successfully deleted workflow: %s", workflow_id)
        return {"success": success, "workflow_id": workflow_id}

    def execute_workflow_async(
        self,
        workflow_id: Optional[str] = None,
        query: Optional[str] = None,
        background: bool = False,
    ) -> Dict[str, Any]:
        """Initiates hands-free workflow execution asynchronously."""
        with self._lock:
            if self._is_executing:
                return {
                    "success": False,
                    "error": "A workflow is already in progress.",
                }

        # Resolve spec
        spec: Optional[WorkflowSpec] = None
        if workflow_id:
            with self._lock:
                spec = self._dynamic_specs.get(workflow_id)
            if not spec:
                spec = self.memory.get_workflow(workflow_id)
            if not spec:
                return {"success": False, "error": f"Workflow '{workflow_id}' not found."}
        elif query:
            from src.executor.intent_synthesizer import DynamicIntentSynthesizer
            dyn_spec = DynamicIntentSynthesizer.parse_intent(query)

            matches = self.dialogue.retrieval.query(query)
            if matches and matches[0].confidence >= 0.70:
                spec = self.memory.get_workflow(matches[0].workflow_id)
            elif dyn_spec:
                spec = dyn_spec
            elif matches and matches[0].confidence >= 0.40:
                spec = self.memory.get_workflow(matches[0].workflow_id)

            if not spec:
                return {
                    "success": False,
                    "error": f"Could not find a workflow matching: '{query}'.",
                }
        else:
            return {"success": False, "error": "Either workflow_id or query must be provided."}

        with self._lock:
            self._is_executing = True
            self._current_workflow = spec
            self._current_step_index = 0
            self._total_steps = len(spec.steps)

        def _run_worker() -> None:
            try:
                assert spec is not None
                res = self.executor.execute_workflow(spec, background_mode=background)
                logger.info(f"Execution of '{spec.name}' finished with success={res.success} (background={background})")
            except Exception as e:
                logger.exception(f"Unexpected error executing workflow: {e}")
            finally:
                with self._lock:
                    self._is_executing = False
                    self._current_workflow = None

        worker_thread = threading.Thread(
            target=_run_worker,
            name=f"ClioWorker-{spec.id}",
            daemon=True,
        )
        worker_thread.start()

        return {
            "success": True,
            "message": f"Execution started for '{spec.name}'" + (" (Background Mode)" if background else ""),
            "workflow_id": spec.id,
            "name": spec.name,
            "total_steps": len(spec.steps),
            "background": background,
        }

    def cancel_execution(self) -> Dict[str, Any]:
        """Aborts the currently running workflow execution or confirms idle state."""
        # Issue stop through actuator
        try:
            self.actuator.stop()
        except Exception as e:
            logger.warning(f"Error resetting actuators during cancel: {e}")

        with self._lock:
            was_running = self._is_executing
            self._is_executing = False
            self._current_workflow = None

        if was_running:
            self._broadcast_sse(
                {
                    "type": "execution",
                    "event_type": "emergency_stop",
                    "message": "Workflow execution aborted by user request.",
                    "timestamp": time.time(),
                }
            )

        return {"success": True, "message": "Workflow execution cancelled."}

    def start(self) -> None:
        """Starts the HTTP server thread."""
        server_instance = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                # Suppress normal access logs to prevent clutter
                pass

            def do_OPTIONS(self) -> None:
                self.send_response(HTTPStatus.OK)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.end_headers()

            def _send_json(self, status_code: int, data: Any) -> None:
                payload = json.dumps(data).encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                path = parsed.path

                # 1. Root: Web UI
                if path in ("/", "/index.html"):
                    web_dir = Path(__file__).parent / "web"
                    index_file = web_dir / "index.html"
                    if index_file.exists():
                        content = index_file.read_bytes()
                        self.send_response(HTTPStatus.OK)
                        self.send_header("Content-Type", "text/html; charset=utf-8")
                        self.send_header("Content-Length", str(len(content)))
                        self.end_headers()
                        self.wfile.write(content)
                    else:
                        self.send_response(HTTPStatus.NOT_FOUND)
                        self.end_headers()
                    return

                # 2. SSE Stream
                if path == "/api/stream":
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "keep-alive")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()

                    client_q = server_instance.register_sse_client()
                    try:
                        # Send initial connection event
                        init_payload = json.dumps(
                            {"type": "connected", "status": server_instance.get_status()}
                        )
                        self.wfile.write(f"data: {init_payload}\n\n".encode("utf-8"))
                        self.wfile.flush()

                        while True:
                            try:
                                msg = client_q.get(timeout=10.0)
                                payload = json.dumps(msg)
                                self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                                self.wfile.flush()
                            except queue.Empty:
                                # Heartbeat ping
                                self.wfile.write(b": ping\n\n")
                                self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    finally:
                        server_instance.unregister_sse_client(client_q)
                    return

                # 3. Status
                if path == "/api/status":
                    self._send_json(HTTPStatus.OK, server_instance.get_status())
                    return

                # 4. Workflows List
                if path == "/api/workflows":
                    self._send_json(HTTPStatus.OK, server_instance.list_workflows())
                    return

                # 5. Search Workflows
                if path == "/api/search":
                    qs = parse_qs(parsed.query)
                    q = qs.get("q", [""])[0].strip()
                    if not q:
                        self._send_json(HTTPStatus.OK, [])
                        return
                    results = server_instance.search_workflows(q)
                    self._send_json(HTTPStatus.OK, results)
                    return

                # 6. Demonstration Recording Status
                if path == "/api/record/status":
                    self._send_json(HTTPStatus.OK, server_instance.get_recording_status())
                    return

                # Unknown GET
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

            def do_POST(self) -> None:
                parsed = urlparse(self.path)
                path = parsed.path

                content_len = int(self.headers.get("Content-Length", 0))
                body_bytes = self.rfile.read(content_len) if content_len > 0 else b"{}"
                try:
                    body = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
                except json.JSONDecodeError:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON body"})
                    return

                # 1. Execute
                if path == "/api/execute":
                    workflow_id = body.get("workflow_id")
                    query = body.get("query")
                    background = bool(body.get("background", False))
                    res = server_instance.execute_workflow_async(
                        workflow_id=workflow_id,
                        query=query,
                        background=background,
                    )
                    status_code = HTTPStatus.OK if res.get("success") else HTTPStatus.BAD_REQUEST
                    self._send_json(status_code, res)
                    return

                # 2. Demonstration Recording Start
                if path == "/api/record/start":
                    res = server_instance.start_recording()
                    self._send_json(HTTPStatus.OK, res)
                    return

                # 2b. Demonstration Event Feed (from client UI / screen monitor)
                if path == "/api/record/feed":
                    res = server_instance.feed_recording_event(body)
                    self._send_json(HTTPStatus.OK, res)
                    return

                # 3. Demonstration Recording Stop & Dissect
                if path == "/api/record/stop":
                    name = body.get("name", "Demonstrated Task").strip() or "Demonstrated Task"
                    trigger = body.get("trigger", "").strip()
                    desc = body.get("description", "").strip()
                    res = server_instance.stop_recording(
                        name=name,
                        trigger=trigger,
                        description=desc,
                    )
                    self._send_json(HTTPStatus.OK, res)
                    return

                # 4. Cancel
                if path == "/api/cancel":
                    res = server_instance.cancel_execution()
                    self._send_json(HTTPStatus.OK, res)
                    return

                # 5. Set Tone
                if path == "/api/tone":
                    tone = body.get("tone", "vibrant")
                    try:
                        server_instance.set_tone(tone)
                        self._send_json(HTTPStatus.OK, {"success": True, "tone": tone})
                    except ValueError as e:
                        self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(e)})
                    return

                # 6. Delete Workflow
                if path in ("/api/workflows/delete", "/api/workflow/delete"):
                    w_id = body.get("workflow_id") or body.get("id", "")
                    res = server_instance.delete_workflow(w_id)
                    status_code = HTTPStatus.OK if res.get("success") else HTTPStatus.BAD_REQUEST
                    self._send_json(status_code, res)
                    return

                # Unknown POST
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

            def do_DELETE(self) -> None:
                parsed = urlparse(self.path)
                path = parsed.path
                if path.startswith("/api/workflows/"):
                    w_id = path.split("/api/workflows/")[-1].strip()
                    res = server_instance.delete_workflow(w_id)
                    status_code = HTTPStatus.OK if res.get("success") else HTTPStatus.NOT_FOUND
                    self._send_json(status_code, res)
                    return
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

        ThreadingHTTPServer.allow_reuse_address = True
        try:
            self._httpd = ThreadingHTTPServer((self.host, self.port), _Handler)
        except OSError as e:
            if getattr(e, "errno", None) == 48:
                logger.warning("Port %d busy; clearing stale process and rebinding...", self.port)
                import subprocess, os
                subprocess.run(f"lsof -ti :{self.port} | grep -v '^{os.getpid()}$' | xargs kill -9", shell=True, check=False)
                time.sleep(0.5)
                self._httpd = ThreadingHTTPServer((self.host, self.port), _Handler)
            else:
                raise

        self._server_thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="ClioHTTPServer",
            daemon=True,
        )
        self._server_thread.start()
        logger.info(f"Clio Spotlight Server started at http://{self.host}:{self.port}")

    def stop(self) -> None:
        """Shuts down the HTTP server and stops all active resources."""
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._server_thread and self._server_thread.is_alive():
            self._server_thread.join(timeout=2.0)
            self._server_thread = None
        if hasattr(self.bus, "close"):
            self.bus.close()
        if hasattr(self.memory, "close"):
            self.memory.close()
        logger.info("Clio Spotlight Server stopped.")
