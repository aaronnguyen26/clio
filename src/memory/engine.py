"""Production Task Memory Engine.

Belongs to FEAT-MEM-02 (TaskMemoryEngine), FEAT-MEM-03 (FTS5SearchTable), and FEAT-MEM-08 (ExecutionTelemetryRecorder).
Pure Python standard library with SQLite WAL, thread locks, version history, and Porter FTS5.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Dict, List, Optional, Union

from src.memory.models import (
    ExecutionRecord,
    SerializationError,
    ValidationError,
    WorkflowSpec,
)

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = os.path.expanduser("~/.task_automator/task_memory.db")

CREDENTIAL_PATTERNS = [
    re.compile(r"sk-[a-zA-Z0-9_\-]{20,}"),
    re.compile(r"ghp_[a-zA-Z0-9]{20,}"),
    re.compile(r"github_pat_[a-zA-Z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"eyJ[a-zA-Z0-9_\-]{10,}\.eyJ[a-zA-Z0-9_\-]{10,}\.[a-zA-Z0-9_\-]+"),
    re.compile(r"(?:bearer\s+|token\s+|password\s*[:=]\s*)([a-zA-Z0-9_\-\.]{16,})", re.IGNORECASE),
]


def redact_sensitive_text(text: str) -> str:
    """Scrubs sensitive credentials, tokens, and secrets with variable placeholder."""
    if not text:
        return text
    redacted = text
    for pattern in CREDENTIAL_PATTERNS:
        if pattern.groups > 0:
            redacted = pattern.sub(lambda m: m.group(0).replace(m.group(1), "${SENSITIVE_PARAM}"), redacted)
        else:
            redacted = pattern.sub("${SENSITIVE_PARAM}", redacted)
    return redacted


class TaskMemoryEngine:
    """Embedded SQLite workflow store with WAL mode, versioning, and FTS5 search."""

    def __init__(self, db_path: Optional[str] = None, pragma_wal: bool = True) -> None:
        if db_path is None:
            db_path = DEFAULT_DB_PATH
        if db_path != ":memory:":
            db_dir = os.path.dirname(os.path.abspath(db_path))
            os.makedirs(db_dir, mode=0o700, exist_ok=True)
            try:
                os.chmod(db_dir, 0o700)
            except OSError:
                pass
            if not os.path.exists(db_path):
                try:
                    fd = os.open(db_path, os.O_CREAT | os.O_RDWR, 0o600)
                    os.close(fd)
                except OSError:
                    pass
            else:
                try:
                    os.chmod(db_path, 0o600)
                except OSError:
                    pass

        self.db_path = db_path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            db_path,
            check_same_thread=False,
            timeout=30.0,
        )
        self._conn.row_factory = sqlite3.Row
        self._is_closed = False

        with self._lock:
            # Enforce foreign keys & busy timeouts
            self._conn.execute("PRAGMA foreign_keys = ON;")
            self._conn.execute("PRAGMA busy_timeout = 30000;")
            if db_path != ":memory:" and pragma_wal:
                try:
                    self._conn.execute("PRAGMA journal_mode = WAL;")
                except sqlite3.OperationalError:
                    pass
                for ext in ["", "-wal", "-shm"]:
                    p = f"{db_path}{ext}"
                    if os.path.exists(p):
                        try:
                            os.chmod(p, 0o600)
                        except OSError:
                            pass

            self._init_db()

    def _init_db(self) -> None:
        """Executes schema.sql DDL to create tables, indexes, FTS5 table, and triggers."""
        schema_path = Path(__file__).parent / "schema.sql"
        if schema_path.exists():
            schema_sql = schema_path.read_text(encoding="utf-8")
        else:
            schema_sql = self._get_embedded_schema()

        cur = self._conn.cursor()
        try:
            cur.executescript(schema_sql)
            self._conn.commit()
        except (sqlite3.DatabaseError, sqlite3.OperationalError) as e:
            if "fts5" in str(e).lower():
                logger.warning("FTS5 table repair triggered: %s", e)
                try:
                    cur.execute("DROP TABLE IF EXISTS workflow_fts;")
                    cur.executescript(schema_sql)
                    self._conn.commit()
                except Exception as ex2:
                    logger.error("Failed to repair FTS5: %s", ex2)
            else:
                raise


    @staticmethod
    def _get_embedded_schema() -> str:
        return """
        PRAGMA foreign_keys = ON;
        CREATE TABLE IF NOT EXISTS workflows (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            version INTEGER NOT NULL DEFAULT 1,
            active INTEGER NOT NULL DEFAULT 1,
            spec_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS workflow_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workflow_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            change_summary TEXT,
            spec_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(workflow_id) REFERENCES workflows(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS executions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workflow_id TEXT NOT NULL,
            status TEXT NOT NULL,
            duration_ms INTEGER NOT NULL,
            steps_completed INTEGER NOT NULL,
            error_message TEXT,
            timestamp TEXT NOT NULL,
            parameters_json TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_workflows_active ON workflows(active);
        CREATE INDEX IF NOT EXISTS idx_workflows_updated_at ON workflows(updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_workflow_versions_wf_ver ON workflow_versions(workflow_id, version);
        CREATE INDEX IF NOT EXISTS idx_executions_workflow_id ON executions(workflow_id);
        CREATE INDEX IF NOT EXISTS idx_executions_timestamp ON executions(timestamp DESC);
        CREATE VIRTUAL TABLE IF NOT EXISTS workflow_fts USING fts5(
            workflow_id UNINDEXED,
            name,
            description,
            triggers,
            tokenize='porter unicode61'
        );
        CREATE TRIGGER IF NOT EXISTS trg_workflows_ai AFTER INSERT ON workflows
        BEGIN
            INSERT INTO workflow_fts(workflow_id, name, description, triggers)
            VALUES (
                new.id,
                new.name,
                coalesce(new.description, ''),
                CASE
                    WHEN json_valid(new.spec_json) THEN
                        coalesce(json_extract(new.spec_json, '$.triggers.canonical'), '') || ' ' ||
                        coalesce(json_extract(new.spec_json, '$.triggers.aliases'), '') || ' ' ||
                        coalesce(json_extract(new.spec_json, '$.triggers.keywords'), '')
                    ELSE ''
                END
            );
        END;
        CREATE TRIGGER IF NOT EXISTS trg_workflows_ad AFTER DELETE ON workflows
        BEGIN
            DELETE FROM workflow_fts WHERE workflow_id = old.id;
        END;
        CREATE TRIGGER IF NOT EXISTS trg_workflows_au AFTER UPDATE ON workflows
        BEGIN
            DELETE FROM workflow_fts WHERE workflow_id = old.id;
            INSERT INTO workflow_fts(workflow_id, name, description, triggers)
            VALUES (
                new.id,
                new.name,
                coalesce(new.description, ''),
                CASE
                    WHEN json_valid(new.spec_json) THEN
                        coalesce(json_extract(new.spec_json, '$.triggers.canonical'), '') || ' ' ||
                        coalesce(json_extract(new.spec_json, '$.triggers.aliases'), '') || ' ' ||
                        coalesce(json_extract(new.spec_json, '$.triggers.keywords'), '')
                    ELSE ''
                END
            );
        END;
        """

    def save_workflow(self, spec: Any, change_summary: str = "Initial recording") -> str:
        """Saves a workflow spec, auto-incrementing version if it already exists."""
        if hasattr(spec, "validate"):
            spec.validate()

        spec_id = getattr(spec, "id")
        name = getattr(spec, "name")
        description = getattr(spec, "description", "") or ""

        with self._lock:
            with self._conn:
                cur = self._conn.cursor()
                cur.execute("SELECT version FROM workflows WHERE id = ?", (spec_id,))
                row = cur.fetchone()

                now = datetime.datetime.now(datetime.timezone.utc).isoformat()
                if row:
                    new_version = row["version"] + 1
                    if hasattr(spec, "version"):
                        spec.version = new_version
                    if hasattr(spec, "updated_at"):
                        spec.updated_at = now
                    spec_json = spec.to_json() if hasattr(spec, "to_json") else json.dumps(spec.to_dict())
                    spec_json = redact_sensitive_text(spec_json)
                    self._conn.execute(
                        """
                        UPDATE workflows
                        SET name = ?, description = ?, version = ?, active = 1, spec_json = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (name, description, new_version, spec_json, now, spec_id),
                    )
                else:
                    new_version = getattr(spec, "version", 1) or 1
                    if hasattr(spec, "updated_at"):
                        spec.updated_at = now
                    created_at = getattr(spec, "created_at", now) or now
                    spec_json = spec.to_json() if hasattr(spec, "to_json") else json.dumps(spec.to_dict())
                    spec_json = redact_sensitive_text(spec_json)
                    self._conn.execute(
                        """
                        INSERT INTO workflows (id, name, description, version, active, spec_json, created_at, updated_at)
                        VALUES (?, ?, ?, ?, 1, ?, ?, ?)
                        """,
                        (spec_id, name, description, new_version, spec_json, created_at, now),
                    )

                # Record immutable version history in the same transaction
                self._conn.execute(
                    """
                    INSERT INTO workflow_versions (workflow_id, version, change_summary, spec_json, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (spec_id, new_version, change_summary, spec_json, now),
                )

            return spec_id

    def get_workflow(self, workflow_id: str, version: Optional[int] = None) -> Optional[WorkflowSpec]:
        """Retrieves a workflow by ID and optional version number."""
        with self._lock:
            cur = self._conn.cursor()
            if version is not None:
                cur.execute(
                    "SELECT spec_json FROM workflow_versions WHERE workflow_id = ? AND version = ?",
                    (workflow_id, version),
                )
            else:
                cur.execute(
                    "SELECT spec_json FROM workflows WHERE id = ? AND active = 1",
                    (workflow_id,),
                )
            row = cur.fetchone()
            if not row:
                return None

            # from_json will raise SerializationError on corrupt JSON
            return WorkflowSpec.from_json(row["spec_json"])

    def list_workflows(self, active_only: bool = True) -> List[Dict[str, Any]]:
        """Lists metadata for stored workflows."""
        with self._lock:
            cur = self._conn.cursor()
            query = "SELECT id, name, description, version, updated_at FROM workflows"
            if active_only:
                query += " WHERE active = 1"
            query += " ORDER BY updated_at DESC"
            cur.execute(query)
            return [dict(row) for row in cur.fetchall()]

    def record_execution(
        self,
        record: Optional[Union[ExecutionRecord, str]] = None,
        workflow_id: Optional[str] = None,
        status: Optional[str] = None,
        duration_ms: Optional[int] = None,
        steps_completed: Optional[int] = None,
        error_message: Optional[str] = None,
        parameters_used: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> int:
        """Records execution telemetry, accepting either an ExecutionRecord or separate arguments."""
        if isinstance(record, ExecutionRecord):
            rec = record
        elif isinstance(record, str):
            rec = ExecutionRecord(
                workflow_id=record,
                status=status or kwargs.get("status", "unknown"),
                duration_ms=duration_ms if duration_ms is not None else kwargs.get("duration_ms", 0),
                steps_completed=steps_completed if steps_completed is not None else kwargs.get("steps_completed", 0),
                error_message=error_message or kwargs.get("error_message"),
                parameters_used=parameters_used or kwargs.get("parameters_used", {}),
            )
        elif workflow_id is not None:
            rec = ExecutionRecord(
                workflow_id=workflow_id,
                status=status or kwargs.get("status", "unknown"),
                duration_ms=duration_ms if duration_ms is not None else kwargs.get("duration_ms", 0),
                steps_completed=steps_completed if steps_completed is not None else kwargs.get("steps_completed", 0),
                error_message=error_message or kwargs.get("error_message"),
                parameters_used=parameters_used or kwargs.get("parameters_used", {}),
            )
        else:
            raise ValidationError("Either record or workflow_id must be provided to record_execution")

        rec.validate()
        params_json = json.dumps(rec.parameters_used) if rec.parameters_used else None
        if params_json:
            params_json = redact_sensitive_text(params_json)
        error_msg = redact_sensitive_text(rec.error_message) if rec.error_message else None

        with self._lock:
            with self._conn:
                cur = self._conn.execute(
                    """
                    INSERT INTO executions (workflow_id, status, duration_ms, steps_completed, error_message, timestamp, parameters_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        rec.workflow_id,
                        rec.status,
                        rec.duration_ms,
                        rec.steps_completed,
                        error_msg,
                        rec.timestamp,
                        params_json,
                    ),
                )
                return cur.lastrowid  # type: ignore

    def get_workflow_versions(self, workflow_id: str) -> List[Dict[str, Any]]:
        """Returns the version audit history for a workflow."""
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                SELECT version, change_summary, created_at
                FROM workflow_versions
                WHERE workflow_id = ?
                ORDER BY version ASC
                """,
                (workflow_id,),
            )
            return [dict(row) for row in cur.fetchall()]

    def delete_workflow(self, workflow_id: str, soft: bool = True) -> bool:
        """Deletes a workflow. Soft deletion sets active=0; hard deletion deletes rows."""
        with self._lock:
            with self._conn:
                if soft:
                    cur = self._conn.execute(
                        "UPDATE workflows SET active = 0, updated_at = ? WHERE id = ?",
                        (datetime.datetime.now(datetime.timezone.utc).isoformat(), workflow_id),
                    )
                else:
                    cur = self._conn.execute("DELETE FROM workflows WHERE id = ?", (workflow_id,))
                return cur.rowcount > 0

    def search_workflows_fts(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Performs full-text search with Porter stemming over workflow triggers and descriptions."""
        tokens = re.findall(r"[\w]+", query, re.UNICODE)
        if not tokens:
            return []

        # Sanitize and construct prefix query: "token1"* "token2"*
        sanitized_query = " ".join(f'"{t}"*' for t in tokens)

        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute(
                    """
                    SELECT f.workflow_id, f.rank, snippet(workflow_fts, 1, '<b>', '</b>', '...', 10) AS snippet
                    FROM workflow_fts f
                    JOIN workflows w ON w.id = f.workflow_id
                    WHERE workflow_fts MATCH ? AND w.active = 1
                    ORDER BY f.rank
                    LIMIT ?
                    """,
                    (sanitized_query, limit),
                )
                return [dict(row) for row in cur.fetchall()]
            except sqlite3.OperationalError:
                return []

    def close(self) -> None:
        """Closes the underlying SQLite connection safely."""
        with self._lock:
            if not self._is_closed:
                self._conn.close()
                self._is_closed = True

    def __enter__(self) -> TaskMemoryEngine:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
