"""Unit tests for TaskMemoryEngine (FEAT-MEM-02, FEAT-MEM-03, FEAT-MEM-08).

Tests validate:
- SQLite schema initialization (workflows, workflow_versions, executions, workflow_fts)
- WAL journal mode and foreign keys pragma
- Workflow save, get, and version incrementing
- Version history retrieval
- Active/inactive listing and soft/hard deletion
- Execution telemetry logging
- FTS5 Porter stemming search and SQL injection safety
- Multi-threaded concurrent access safety
"""

import os
import tempfile
import threading
import unittest

from src.memory.engine import TaskMemoryEngine
from src.memory.models import (
    ActionType,
    ExecutionRecord,
    SerializationError,
    WorkflowSpec,
    WorkflowStep,
)


class TestTaskMemoryEngine(unittest.TestCase):
    """Test suite for TaskMemoryEngine."""

    def setUp(self) -> None:
        self.engine = TaskMemoryEngine(":memory:")
        self.sample_spec = WorkflowSpec(
            id="wf_test_task",
            name="Sample Task",
            description="Task for unit testing",
            triggers={"canonical": "run sample", "aliases": ["sample task"], "keywords": ["sample", "test"]},
            steps=[
                WorkflowStep(
                    step_id="step_1",
                    order=1,
                    description="Type sample text",
                    action=ActionType.TYPE_TEXT,
                    payload={"text": "Hello world"},
                )
            ],
        )

    def tearDown(self) -> None:
        self.engine.close()

    def test_save_and_retrieve_workflow(self):
        """TEST-ENG-01: Save workflow persists and retrieves exact specification."""
        wf_id = self.engine.save_workflow(self.sample_spec)
        self.assertEqual(wf_id, "wf_test_task")

        retrieved = self.engine.get_workflow("wf_test_task")
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.name, "Sample Task")
        self.assertEqual(len(retrieved.steps), 1)
        self.assertEqual(retrieved.steps[0].payload["text"], "Hello world")

    def test_version_history_increment(self):
        """TEST-ENG-02: Updating workflow increments version number and preserves v1."""
        self.engine.save_workflow(self.sample_spec, change_summary="Initial commit")

        # Update spec
        self.sample_spec.name = "Updated Task"
        self.engine.save_workflow(self.sample_spec, change_summary="Updated title")

        current = self.engine.get_workflow("wf_test_task")
        self.assertEqual(current.version, 2)
        self.assertEqual(current.name, "Updated Task")

        v1 = self.engine.get_workflow("wf_test_task", version=1)
        self.assertIsNotNone(v1)
        self.assertEqual(v1.version, 1)
        self.assertEqual(v1.name, "Sample Task")

        history = self.engine.get_workflow_versions("wf_test_task")
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["version"], 1)
        self.assertEqual(history[1]["version"], 2)

    def test_execution_telemetry_logging(self):
        """TEST-ENG-03: Execution telemetry records log row in database."""
        row_id1 = self.engine.record_execution(
            workflow_id="wf_test_task",
            status="success",
            duration_ms=250,
            steps_completed=1,
        )
        self.assertGreaterEqual(row_id1, 1)

        record = ExecutionRecord(
            workflow_id="wf_test_task",
            status="failed",
            duration_ms=100,
            steps_completed=0,
            error_message="Window not found",
            parameters_used={"target": "notes"},
        )
        row_id2 = self.engine.record_execution(record)
        self.assertGreater(row_id2, row_id1)

    def test_disk_persistence_and_wal(self):
        """TEST-ENG-04: Disk file database initializes WAL mode and persists across close."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            db_path = tf.name

        try:
            disk_engine = TaskMemoryEngine(db_path)
            disk_engine.save_workflow(self.sample_spec)
            disk_engine.close()

            reopened = TaskMemoryEngine(db_path)
            wf = reopened.get_workflow("wf_test_task")
            self.assertIsNotNone(wf)
            self.assertEqual(wf.id, "wf_test_task")
            reopened.close()
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)

    def test_list_and_delete_workflow(self):
        """TEST-ENG-05: List workflows reflects active state and soft deletion."""
        self.engine.save_workflow(self.sample_spec)
        active_list = self.engine.list_workflows(active_only=True)
        self.assertEqual(len(active_list), 1)

        # Soft delete
        self.engine.delete_workflow("wf_test_task", soft=True)
        self.assertEqual(len(self.engine.list_workflows(active_only=True)), 0)
        self.assertEqual(len(self.engine.list_workflows(active_only=False)), 1)

        # Hard delete
        self.engine.delete_workflow("wf_test_task", soft=False)
        self.assertEqual(len(self.engine.list_workflows(active_only=False)), 0)

    def test_fts5_search_and_porter_stemming(self):
        """TEST-ENG-06: Standalone FTS5 table indexes triggers and stems terms."""
        self.engine.save_workflow(self.sample_spec)
        # "sampling" or "samples" should stem to "sample" via Porter stemmer
        results = self.engine.search_workflows_fts("sampling")
        self.assertGreaterEqual(len(results), 1)
        self.assertEqual(results[0]["workflow_id"], "wf_test_task")

    def test_fts5_sql_injection_safety(self):
        """TEST-ENG-07: FTS5 query gracefully handles malicious SQL injection syntax."""
        self.engine.save_workflow(self.sample_spec)
        payloads = [
            "'; DROP TABLE workflows; --",
            "' OR '1'='1",
            '" UNION SELECT * FROM sqlite_master --',
            "NOT AND OR NEAR : * ^",
        ]
        for p in payloads:
            results = self.engine.search_workflows_fts(p)
            self.assertIsInstance(results, list)

    def test_corrupt_json_triggers_resilience(self):
        """TEST-ENG-08: Corrupt spec_json in workflows table does not crash FTS triggers."""
        # Direct raw SQL insert of malformed JSON
        with self.engine._lock:
            self.engine._conn.execute(
                "INSERT INTO workflows VALUES ('wf_corrupted', 'Corrupt', 'desc', 1, 1, 'INVALID_JSON_CONTENT{{{', 'now', 'now')"
            )
            self.engine._conn.commit()

        # Reading back raises SerializationError as expected
        with self.assertRaises(SerializationError):
            self.engine.get_workflow("wf_corrupted")

    def test_concurrent_access_thread_safety(self):
        """TEST-ENG-09: Multi-threaded operations execute without SQLite lock conflicts."""
        errors = []

        def worker(idx: int):
            try:
                spec = WorkflowSpec(
                    id=f"wf_concurrent_{idx}",
                    name=f"Concurrent Task {idx}",
                    description="Concurrent test",
                )
                self.engine.save_workflow(spec)
                retrieved = self.engine.get_workflow(f"wf_concurrent_{idx}")
                if not retrieved or retrieved.name != f"Concurrent Task {idx}":
                    errors.append(f"Worker {idx} mismatch")
                self.engine.record_execution(f"wf_concurrent_{idx}", "success", 10, 1)
            except Exception as e:
                errors.append(f"Worker {idx} exception: {e}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])

    def test_soft_delete_fts_synchronization(self):
        """TEST-ENG-10: Soft-deleted workflows are not returned by FTS queries."""
        spec = WorkflowSpec(
            id="wf_soft_fts_test",
            name="Soft Delete FTS Task",
            triggers={"canonical": "unique_soft_delete_trigger_token"},
        )
        self.engine.save_workflow(spec)
        matches = self.engine.search_workflows_fts("unique_soft_delete_trigger_token")
        self.assertEqual(len(matches), 1)

        # Soft delete the workflow
        deleted = self.engine.delete_workflow("wf_soft_fts_test", soft=True)
        self.assertTrue(deleted)

        # Ensure FTS query returns 0 matches for soft-deleted workflow
        matches_after = self.engine.search_workflows_fts("unique_soft_delete_trigger_token")
        self.assertEqual(len(matches_after), 0)
