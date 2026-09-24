"""Milestone 2 Challenger Adversarial Stress Test Suite.

Author: Challenger M2-1 (teamwork_challenger_m2_1)
Belongs to Milestone 2: Teach-Mode Workflow Memory & Retrieval Engine.

Empirically stress-tests and verifies:
1. TaskMemoryEngine:
   - High-throughput multi-threaded concurrent access under RLock.
   - Multi-instance concurrent disk writes with SQLite WAL mode.
   - Deep version history immutability and audit log integrity across multi-step mutations.
   - Raw binary blob, null byte, and malformed JSON resilience in SQLite tables & triggers.
2. NLRetrievalEngine:
   - Hostile SQL injection and FTS5 syntax exploit payloads.
   - Exact tier scoring bounds across Tier 1 (1.0), Tier 2 (0.95), Tier 3 ([0.85, 0.90]), Tier 4 ([0.50, 0.85]).
   - Empirical vulnerability reproductions:
     * VULN-M2-01: Denial of Service in NLRetrievalEngine.query() on corrupt workflow in DB.
     * VULN-M2-02: False-positive Tier 3 trigger matches on unanchored 3-char subwords (e.g. 'pen' in 'open').
     * VULN-M2-03: FTS5 query sanitizer strips non-ASCII Unicode tokens despite unicode61 tokenizer.
"""

from __future__ import annotations

import os
import random
import sqlite3
import tempfile
import threading
import time
import unittest
from typing import Any, Dict, List

import pytest

from src.memory.engine import TaskMemoryEngine
from src.memory.models import (
    ActionType,
    ExecutionRecord,
    SerializationError,
    ValidationError,
    WorkflowSpec,
    WorkflowStep,
)
from src.memory.retrieval import NLRetrievalEngine


class TestTaskMemoryEngineStress(unittest.TestCase):
    """Concurrency, corruption resilience, and version immutability stress tests."""

    def test_concurrent_multi_thread_read_write_under_lock(self) -> None:
        """16 concurrent threads performing 800 mixed operations under RLock."""
        engine = TaskMemoryEngine(":memory:")
        errors: List[Exception] = []

        # Seed initial workflows
        for i in range(10):
            spec = WorkflowSpec(
                id=f"wf_seed_{i}",
                name=f"Seed Workflow {i}",
                triggers={"canonical": f"action {i}", "aliases": [f"do {i}"]},
            )
            engine.save_workflow(spec)

        def worker_fn(worker_id: int) -> None:
            for op_idx in range(50):
                try:
                    op = op_idx % 6
                    if op == 0:  # read
                        engine.get_workflow(f"wf_seed_{worker_id % 10}")
                    elif op == 1:  # write new
                        new_spec = WorkflowSpec(
                            id=f"wf_{worker_id}_{op_idx}",
                            name=f"Workflow {worker_id}-{op_idx}",
                            triggers={"canonical": f"worker {worker_id} op {op_idx}"},
                        )
                        engine.save_workflow(new_spec)
                    elif op == 2:  # update existing
                        update_spec = WorkflowSpec(
                            id=f"wf_seed_{worker_id % 10}",
                            name=f"Updated by {worker_id}",
                            triggers={"canonical": f"action {worker_id % 10}"},
                        )
                        engine.save_workflow(update_spec, change_summary=f"Worker {worker_id} update")
                    elif op == 3:  # record execution
                        engine.record_execution(
                            workflow_id=f"wf_seed_{worker_id % 10}",
                            status="success",
                            duration_ms=15,
                            steps_completed=3,
                        )
                    elif op == 4:  # FTS search
                        engine.search_workflows_fts(f"action {worker_id % 10}")
                    elif op == 5:  # list workflows
                        engine.list_workflows()
                except Exception as ex:
                    errors.append(ex)

        threads = [threading.Thread(target=worker_fn, args=(i,)) for i in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        engine.close()
        assert len(errors) == 0, f"Encountered {len(errors)} errors during concurrent execution: {errors[:3]}"

    def test_concurrent_multi_engine_disk_wal_stress(self) -> None:
        """8 threads across 4 distinct TaskMemoryEngine instances sharing on-disk SQLite WAL DB."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            db_path = tmp.name

        try:
            engines = [TaskMemoryEngine(db_path, pragma_wal=True) for _ in range(4)]
            errors: List[Exception] = []

            # Seed database
            for i in range(10):
                spec = WorkflowSpec(
                    id=f"shared_{i}",
                    name=f"Shared Workflow {i}",
                    triggers={"canonical": f"shared task {i}"},
                )
                engines[0].save_workflow(spec)

            def disk_worker_fn(thread_id: int) -> None:
                engine = engines[thread_id % len(engines)]
                for i in range(30):
                    try:
                        # Insert new
                        spec = WorkflowSpec(
                            id=f"disk_{thread_id}_{i}",
                            name=f"Disk WF {thread_id}-{i}",
                        )
                        engine.save_workflow(spec)

                        # Record telemetry
                        engine.record_execution(
                            workflow_id=f"disk_{thread_id}_{i}",
                            status="success",
                            duration_ms=25,
                            steps_completed=2,
                        )

                        # Retrieve
                        res = engine.get_workflow(f"disk_{thread_id}_{i}")
                        assert res is not None
                    except Exception as ex:
                        errors.append(ex)

            threads = [threading.Thread(target=disk_worker_fn, args=(t,)) for t in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            for eng in engines:
                eng.close()

            assert len(errors) == 0, f"Encountered {len(errors)} errors during WAL disk stress: {errors[:3]}"
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)
            wal = f"{db_path}-wal"
            shm = f"{db_path}-shm"
            if os.path.exists(wal):
                os.remove(wal)
            if os.path.exists(shm):
                os.remove(shm)

    def test_version_history_deep_immutability(self) -> None:
        """Ensures historical revisions are immutable across multiple updates and deletions."""
        engine = TaskMemoryEngine(":memory:")

        # Version 1
        spec_v1 = WorkflowSpec(
            id="audit_wf",
            name="Audit Workflow V1",
            description="Initial creation",
            steps=[WorkflowStep(step_id="s1", order=1, description="First step", action=ActionType.CLICK)],
        )
        engine.save_workflow(spec_v1, change_summary="Version 1 release")

        # Version 2
        spec_v2 = WorkflowSpec(
            id="audit_wf",
            name="Audit Workflow V2",
            description="Second iteration",
            steps=[
                WorkflowStep(step_id="s1", order=1, description="First step", action=ActionType.CLICK),
                WorkflowStep(step_id="s2", order=2, description="Second step", action=ActionType.TYPE_TEXT),
            ],
        )
        engine.save_workflow(spec_v2, change_summary="Added step 2")

        # Version 3
        spec_v3 = WorkflowSpec(
            id="audit_wf",
            name="Audit Workflow V3",
            description="Third iteration",
            steps=[
                WorkflowStep(step_id="s3", order=1, description="Completely new step", action=ActionType.PRESS_HOTKEY),
            ],
        )
        engine.save_workflow(spec_v3, change_summary="Rewrote step structure")

        # Verify current active version
        active = engine.get_workflow("audit_wf")
        assert active is not None
        assert active.version == 3
        assert active.name == "Audit Workflow V3"
        assert len(active.steps) == 1
        assert active.steps[0].step_id == "s3"

        # Verify version 1 snapshot immutability
        v1 = engine.get_workflow("audit_wf", version=1)
        assert v1 is not None
        assert v1.version == 1
        assert v1.name == "Audit Workflow V1"
        assert len(v1.steps) == 1
        assert v1.steps[0].step_id == "s1"

        # Verify version 2 snapshot immutability
        v2 = engine.get_workflow("audit_wf", version=2)
        assert v2 is not None
        assert v2.version == 2
        assert v2.name == "Audit Workflow V2"
        assert len(v2.steps) == 2
        assert v2.steps[1].step_id == "s2"

        # Verify audit history
        versions = engine.get_workflow_versions("audit_wf")
        assert len(versions) == 3
        assert [v["version"] for v in versions] == [1, 2, 3]
        assert versions[0]["change_summary"] == "Version 1 release"
        assert versions[1]["change_summary"] == "Added step 2"
        assert versions[2]["change_summary"] == "Rewrote step structure"

        # Test soft-delete behavior: active=0, but version history remains accessible
        engine.delete_workflow("audit_wf", soft=True)
        assert engine.get_workflow("audit_wf") is None
        assert engine.get_workflow("audit_wf", version=1) is not None
        assert len(engine.get_workflow_versions("audit_wf")) == 3

        # Test hard-delete behavior: CASCADE deletes all versions
        engine.delete_workflow("audit_wf", soft=False)
        assert engine.get_workflow("audit_wf", version=1) is None
        assert len(engine.get_workflow_versions("audit_wf")) == 0

        engine.close()

    def test_corrupt_data_raw_sql_and_trigger_resilience(self) -> None:
        """Malformed JSON strings, binary blobs, and null bytes do not crash SQLite triggers."""
        engine = TaskMemoryEngine(":memory:")
        cur = engine._conn.cursor()

        # 1. Insert malformed JSON text
        cur.execute(
            """
            INSERT INTO workflows (id, name, description, version, active, spec_json, created_at, updated_at)
            VALUES ('corrupt_text', 'Corrupt Text', 'desc', 1, 1, 'INVALID_JSON_CONTENT{{{', 'now', 'now')
            """
        )
        engine._conn.commit()

        # 2. Insert raw binary BLOB
        cur.execute(
            """
            INSERT INTO workflows (id, name, description, version, active, spec_json, created_at, updated_at)
            VALUES ('corrupt_blob', 'Corrupt Blob', 'desc', 1, 1, ?, 'now', 'now')
            """,
            (sqlite3.Binary(b"\x00\xff\xfe\xca\xfe\xba\xbe\x00"),),
        )
        engine._conn.commit()

        # 3. Update with another binary blob to test UPDATE trigger
        cur.execute(
            "UPDATE workflows SET spec_json = ? WHERE id = 'corrupt_blob'",
            (sqlite3.Binary(b"\xde\xad\xbe\xef"),),
        )
        engine._conn.commit()

        # 4. Insert parameterized null byte in JSON string
        cur.execute(
            """
            INSERT INTO workflows (id, name, description, version, active, spec_json, created_at, updated_at)
            VALUES ('null_byte_json', 'Null Byte', 'desc', 1, 1, ?, 'now', 'now')
            """,
            ('{"triggers": {"canonical": "hello\x00world"}}',),
        )
        engine._conn.commit()

        # Verify FTS virtual table did not crash or corrupt
        fts_res = engine.search_workflows_fts("anything")
        assert isinstance(fts_res, list)

        # Direct retrieval of corrupted workflow raises SerializationError cleanly
        with pytest.raises(SerializationError):
            engine.get_workflow("corrupt_text")

        engine.close()


class TestNLRetrievalEngineStress(unittest.TestCase):
    """Stress tests on NLRetrievalEngine: payloads, tier bounds, and empirical bug reproductions."""

    def setUp(self) -> None:
        self.engine = TaskMemoryEngine(":memory:")
        self.retrieval = NLRetrievalEngine(self.engine, min_confidence=0.50)

        # Seed reference workflow
        self.spec = WorkflowSpec(
            id="wf_notes_benchmark",
            name="Notes Weekly Benchmark",
            description="Autonomous Apple Notes to-do list automation",
            triggers={
                "canonical": "create weekly todo list in apple notes",
                "aliases": [
                    "generate weekly notes to-do",
                    "make weekly plan in notes",
                    "apple notes weekly tasks",
                ],
                "keywords": [
                    "notes",
                    "weekly",
                    "todo",
                    "apple",
                    "tasks",
                    "benchmark",
                    "checklist",
                ],
            },
        )
        self.engine.save_workflow(self.spec)

    def tearDown(self) -> None:
        self.engine.close()

    def test_sql_injection_and_hostile_query_payloads(self) -> None:
        """Adversarial SQL injection, FTS5 operators, huge strings, and edge characters."""
        hostile_payloads = [
            "'; DROP TABLE workflows; --",
            "' OR '1'='1",
            "\" UNION SELECT * FROM sqlite_master --",
            "\" UNION SELECT 1, 'injected', 3, 4, 5, 6, 7, 8 --",
            "OR AND NOT NEAR",
            "NEAR(notes todo, 10)",
            "column : notes",
            "\"notes\"*",
            "***^^^$$$///\\\\\\",
            " " * 100,
            "\t\r\n\x0b\x0c",
            "A" * 50000,  # 50k character payload
            "✨🔥💻📝🚀",  # Emojis
            "'; DELETE FROM workflow_versions; --",
        ]

        for payload in hostile_payloads:
            results = self.retrieval.query(payload)
            assert isinstance(results, list)

        # Ensure tables were NOT dropped or modified
        assert len(self.engine.list_workflows()) == 1
        assert self.engine.get_workflow("wf_notes_benchmark") is not None

    def test_exact_tier_scoring_bounds(self) -> None:
        """Empirically verifies exact tier score bounds across all 4 tiers."""
        # Tier 1: Canonical trigger match == 1.0
        r_t1 = self.retrieval.query("create weekly todo list in apple notes")
        assert len(r_t1) == 1
        assert r_t1[0].tier == "tier1_canonical"
        assert r_t1[0].confidence == 1.0

        # Tier 2: Alias trigger match == 0.95
        r_t2 = self.retrieval.query("generate weekly notes to-do")
        assert len(r_t2) == 1
        assert r_t2[0].tier == "tier2_alias"
        assert r_t2[0].confidence == 0.95

        # Tier 3: Substring match in [0.85, 0.90]
        # Query embedding canonical trigger in user sentence
        r_t3 = self.retrieval.query("can you please create weekly todo list in apple notes right now")
        assert len(r_t3) == 1
        assert r_t3[0].tier == "tier3_substring_fts"
        assert 0.85 <= r_t3[0].confidence <= 0.90

        # Tier 4: Token overlap / keyword matching in [0.50, 0.85]
        # Query using keywords without matching full substring
        retrieval_all = NLRetrievalEngine(self.engine, min_confidence=0.0)
        r_t4 = retrieval_all.query("organize benchmark checklist")
        assert len(r_t4) == 1
        assert r_t4[0].tier == "tier4_token_fuzzy"
        assert 0.50 <= r_t4[0].confidence <= 0.85

    def test_vulnerability_retrieval_dos_on_corrupt_spec(self) -> None:
        """VULN-M2-01: Verifies NLRetrievalEngine.query() skips corrupt workflows and returns valid matches.

        Remediated robust behavior: query() skips corrupt entries and cleanly returns valid matches
        without crashing or raising SerializationError.
        """
        # Insert a corrupt workflow directly into DB
        cur = self.engine._conn.cursor()
        cur.execute(
            """
            INSERT INTO workflows (id, name, description, version, active, spec_json, created_at, updated_at)
            VALUES ('corrupt_wf', 'Corrupt Entry', 'desc', 1, 1, 'INVALID_JSON_GARBAGE{{{', 'now', 'now')
            """
        )
        self.engine._conn.commit()

        # Query skips the corrupt record and successfully returns matches for valid workflow
        matches = self.retrieval.query("create weekly todo list in apple notes")
        assert len(matches) >= 1
        assert matches[0].workflow_id == "wf_notes_benchmark"
        assert matches[0].confidence == 1.0

    def test_vulnerability_tier3_false_positive_on_subword(self) -> None:
        """VULN-M2-02: Verifies subword gating prevents false Tier 3 triggers on 3-char subwords.

        Remediated behavior: query 'pen' does NOT match 'open notes app' at Tier 3 with >= 0.85
        confidence because it fails word boundary and length ratio checks.
        """
        # Register a workflow with trigger 'open notes app'
        spec_open = WorkflowSpec(
            id="wf_open",
            name="Open Notes App",
            triggers={"canonical": "open notes app"},
        )
        self.engine.save_workflow(spec_open)

        # Query 'pen' (a subword of 'open')
        matches = self.retrieval.query("pen")
        # Ensure it does NOT falsely trigger Tier 3 for open notes app
        tier3_false_matches = [
            m for m in matches
            if m.workflow_id == "wf_open" and m.tier == "tier3_substring_fts"
        ]
        assert len(tier3_false_matches) == 0

    def test_vulnerability_fts_sanitizer_drops_unicode(self) -> None:
        """VULN-M2-03: Verifies sanitize_fts5_query preserves non-ASCII Unicode characters.

        Remediated behavior: non-ASCII accented letters and non-Latin alphabets are preserved
        as valid search tokens matching the FTS5 unicode61 tokenizer.
        """
        # French query: 'créer une note'
        sanitized_fr = NLRetrievalEngine.sanitize_fts5_query("créer une note")
        assert '"créer"' in sanitized_fr
        assert '"une"' in sanitized_fr
        assert '"note"' in sanitized_fr

        # Japanese query: 'メモを作成'
        sanitized_ja = NLRetrievalEngine.sanitize_fts5_query("メモを作成")
        assert '"メモを作成"' in sanitized_ja
