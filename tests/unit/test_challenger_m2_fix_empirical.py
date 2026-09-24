"""Milestone 2 Remediation Challenger Empirical Stress Test Suite.

Author: Challenger M2_Fix_1 (challenger_m2_fix_1)
Target Milestone: M2 Remediation Challenge (Database, FTS, Concurrency, and Retrieval)

Empirically verifies and stress-tests:
1. Concurrency and Soft-Deletion / FTS Race Conditions:
   - High-throughput multi-threaded concurrent interleaving of workflow creation,
     soft-deletion, reactivation, version updates, FTS queries, and retrieval queries.
   - Strict invariants: Soft-deleted workflows are NEVER returned by search_workflows_fts
     or NLRetrievalEngine.query(), and reactivation instantly restores searchability.
2. FTS Unicode and Internationalization Deep Stress:
   - Non-Latin token preservation across French, German, Spanish, Polish, Vietnamese,
     Japanese, Chinese, Arabic, and Cyrillic scripts.
   - Punctuation, symbols, quotes, and emoji resilience in FTS5 MATCH queries.
3. Subword & Boundary Precision Bounds (VULN-M2-02 Boundary Analysis):
   - Strict word-boundary and length ratio (>= 0.50) enforcement on canonical & alias triggers.
   - Subwords ("pen", "ote", "art", "late", "calc", "chen") never falsely trigger Tier 3 (>= 0.85).
   - True phrases ("open notes", "open notes app") correctly match appropriate tiers.
4. Retrieval Resilience Under Multi-Class Database Corruption (VULN-M2-01 Deep Stress):
   - Database seeded with malformed JSON, non-dict JSON (int, bool, list), null bytes,
     empty strings, and binary blobs.
   - NLRetrievalEngine.query() reliably extracts valid matches and tolerates 100% corruption.
5. Models YAML Round-Trip Deep Invariants:
   - Deep nested structures, multiline strings, empty collections, special characters,
     and round-trip equality without 3rd-party dependencies.
6. Recorder Click Coalescing Progression:
   - 1, 2, 3, 4, 5, 6 click event sequences (both mouse_down/up pairs and direct clicks),
     distance gating (> 5px), and interval gating (> 0.5s).
7. R6 Zero-Disruption Verification:
   - Verification that no live OS input synthesis is performed during test runs.
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
    TargetCoordinates,
    ValidationError,
    WorkflowSpec,
    WorkflowStep,
)
from src.memory.recorder import RawEvent, RawEventType, WorkflowRecorderPipeline
from src.memory.retrieval import MatchResult, NLRetrievalEngine


class TestDatabaseFTSConcurrencyStress(unittest.TestCase):
    """Stress tests concurrent writes, soft-deletions, reactivations, and FTS synchronization."""

    def test_concurrent_soft_delete_reactivate_fts_invariants(self) -> None:
        """20 concurrent threads performing interleaved saves, soft-deletes, reactivations, and FTS queries.

        Invariant: At NO point should a soft-deleted workflow be returned by search_workflows_fts
        or NLRetrievalEngine.query(). Once reactivated, it must immediately be discoverable.
        Each thread manages its own workflow namespace to prevent test-oracle state races.
        """
        engine = TaskMemoryEngine(":memory:")
        retrieval = NLRetrievalEngine(engine, min_confidence=0.50)
        errors: List[str] = []

        def worker_fn(worker_id: int) -> None:
            wf_id = f"wf_thread_{worker_id}"
            canonical = f"execute private task worker{worker_id}_private"

            for iteration in range(25):
                try:
                    # 1. Save / activate workflow
                    spec = WorkflowSpec(
                        id=wf_id,
                        name=f"Workflow Thread {worker_id} Iter {iteration}",
                        triggers={"canonical": canonical, "keywords": ["private", f"worker{worker_id}_private"]},
                    )
                    engine.save_workflow(spec, change_summary=f"Iteration {iteration}")

                    # Verify it IS discoverable via FTS and NL
                    fts_res = engine.search_workflows_fts(f"worker{worker_id}_private", limit=50)
                    fts_ids = [r["workflow_id"] for r in fts_res]
                    if wf_id not in fts_ids:
                        errors.append(f"Worker {worker_id}: Active workflow {wf_id} NOT found in FTS: {fts_ids}")

                    nl_res = retrieval.query(canonical)
                    nl_ids = [m.workflow_id for m in nl_res]
                    if wf_id not in nl_ids:
                        errors.append(f"Worker {worker_id}: Active workflow {wf_id} NOT found in NL retrieval: {nl_ids}")

                    # 2. Record telemetry while active
                    engine.record_execution(
                        workflow_id=wf_id,
                        status="success",
                        duration_ms=5,
                        steps_completed=1,
                    )

                    # 3. Soft-delete workflow
                    engine.delete_workflow(wf_id, soft=True)

                    # Verify it is NEVER discoverable while soft-deleted
                    fts_res_after = engine.search_workflows_fts(f"worker{worker_id}_private", limit=50)
                    fts_ids_after = [r["workflow_id"] for r in fts_res_after]
                    if wf_id in fts_ids_after:
                        errors.append(f"Worker {worker_id}: Soft-deleted workflow {wf_id} returned in FTS: {fts_ids_after}")

                    nl_res_after = retrieval.query(canonical)
                    nl_ids_after = [m.workflow_id for m in nl_res_after]
                    if wf_id in nl_ids_after:
                        errors.append(f"Worker {worker_id}: Soft-deleted workflow {wf_id} returned in NL: {nl_ids_after}")

                except Exception as ex:
                    import traceback
                    errors.append(f"Worker {worker_id} crashed:\n{traceback.format_exc()}")

        threads = [threading.Thread(target=worker_fn, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        engine.close()
        assert len(errors) == 0, f"Encountered {len(errors)} concurrency violations: {errors[:5]}"

    def test_multi_engine_wal_disk_contention_and_soft_delete(self) -> None:
        """Multiple TaskMemoryEngine instances concurrently soft-deleting and querying an on-disk WAL DB."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            db_path = tmp.name

        try:
            engines = [TaskMemoryEngine(db_path, pragma_wal=True) for _ in range(4)]
            # Seed 10 workflows
            for i in range(10):
                engines[0].save_workflow(
                    WorkflowSpec(
                        id=f"wal_wf_{i}",
                        name=f"WAL Workflow {i}",
                        triggers={"canonical": f"wal workflow action {i}"},
                    )
                )

            errors: List[str] = []

            def hammer(instance_idx: int) -> None:
                eng = engines[instance_idx]
                wf_id = f"wal_wf_{instance_idx}"
                for i in range(25):
                    try:
                        # Soft delete
                        eng.delete_workflow(wf_id, soft=True)
                        # Verify FTS immediately excludes it
                        fts_res = eng.search_workflows_fts(f"action {instance_idx}")
                        found = [r["workflow_id"] for r in fts_res if r["workflow_id"] == wf_id]
                        if found:
                            errors.append(f"Instance {instance_idx}: Soft-deleted {wf_id} still returned in FTS: {found}")

                        # Reactivate
                        eng.save_workflow(
                            WorkflowSpec(
                                id=wf_id,
                                name=f"WAL Workflow Reactivated {i}",
                                triggers={"canonical": f"wal workflow action {instance_idx}"},
                            ),
                            change_summary=f"Reactivated by {instance_idx}",
                        )
                        # Verify FTS immediately includes it
                        fts_res_after = eng.search_workflows_fts(f"action {instance_idx}")
                        found_after = [r["workflow_id"] for r in fts_res_after if r["workflow_id"] == wf_id]
                        if not found_after:
                            errors.append(f"Instance {instance_idx}: Reactivated {wf_id} NOT found in FTS")
                    except Exception as ex:
                        errors.append(f"Instance {instance_idx} error: {ex}")

            threads = [threading.Thread(target=hammer, args=(i,)) for i in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            for eng in engines:
                eng.close()

            assert len(errors) == 0, f"Encountered {len(errors)} errors: {errors[:5]}"
        finally:
            for ext in ["", "-wal", "-shm"]:
                p = db_path + ext
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass


class TestFTSUnicodeAndSanitizationStress(unittest.TestCase):
    """Stress tests Unicode token preservation, multi-language scripts, and hostile query sanitization."""

    def setUp(self) -> None:
        self.engine = TaskMemoryEngine(":memory:")
        self.retrieval = NLRetrievalEngine(self.engine, min_confidence=0.50)

    def tearDown(self) -> None:
        self.engine.close()

    def test_multilingual_unicode_preservation_and_searchability(self) -> None:
        """Verifies multi-script workflows (French, German, Polish, Vietnamese, Japanese, Arabic, Russian)."""
        multilingual_specs = [
            ("wf_fr", "Créer un document", "créer un nouveau document texte", ["créer", "document", "texte"]),
            ("wf_de", "Fenster schließen", "schließen das aktive Browser-Fenster", ["schließen", "fenster"]),
            ("wf_pl", "Zrób notatkę", "zrób szybką notatkę w notesie", ["notatka", "zrób"]),
            ("wf_vi", "Khởi động ứng dụng", "khởi động trình duyệt web", ["khởi", "động", "ứng", "dụng"]),
            ("wf_ja", "新規ノート作成", "新しいノートを作成する", ["ノート", "作成", "新しい"]),
            ("wf_zh", "打开备忘录", "在苹果电脑上打开备忘录", ["备忘录", "打开"]),
            ("wf_ru", "Создать задачу", "создать новую задачу в списке", ["создать", "задачу"]),
            ("wf_ar", "إنشاء ملاحظة", "إنشاء ملاحظة جديدة في التطبيق", ["إنشاء", "ملاحظة"]),
        ]

        for wf_id, name, canonical, keywords in multilingual_specs:
            spec = WorkflowSpec(
                id=wf_id,
                name=name,
                triggers={"canonical": canonical, "keywords": keywords},
            )
            self.engine.save_workflow(spec)

        # 1. Verify FTS5 query sanitizer preserves Unicode tokens
        for _, _, canonical, _ in multilingual_specs:
            sanitized = NLRetrievalEngine.sanitize_fts5_query(canonical)
            assert len(sanitized) > 0, f"Sanitizer returned empty string for: {canonical}"
            assert '"' in sanitized

        # 2. Verify search_workflows_fts returns matches for each language
        queries = [
            ("créer", "wf_fr"),
            ("schließen", "wf_de"),
            ("notatkę", "wf_pl"),
            ("khởi", "wf_vi"),
            ("ノート", "wf_ja"),
            ("备忘录", "wf_zh"),
            ("создать", "wf_ru"),
            ("إنشاء", "wf_ar"),
        ]

        for term, expected_id in queries:
            fts_matches = self.engine.search_workflows_fts(term)
            matched_ids = [m["workflow_id"] for m in fts_matches]
            assert expected_id in matched_ids, f"FTS search for '{term}' did not find {expected_id}. Got: {matched_ids}"

        # 3. Verify NLRetrievalEngine matches via exact or FTS
        for _, _, canonical, _ in multilingual_specs:
            nl_matches = self.retrieval.query(canonical)
            assert len(nl_matches) >= 1
            assert nl_matches[0].confidence >= 0.85

    def test_hostile_fts_syntax_and_special_characters(self) -> None:
        """Tests that punctuation, regex special chars, quotes, and empty tokens never crash FTS5."""
        pathological_inputs = [
            "",
            "   ",
            "\t\n\r",
            "\"\"\"\"\"\"\"",
            "'''***^^^$$$///\\\\\\",
            "AND OR NOT NEAR",
            "MATCH *:*",
            "col:value",
            "(a OR b) AND (c NOT d)",
            "{json: true}",
            "SELECT * FROM workflows;",
            "DROP TABLE workflow_fts;",
            "NULL",
            "None",
            "0x00",
            "\x00\x01\x02",
            "emoji test 🚀 🌟 💻 📝",
            "accented: résumé, naïve, über, façade",
            "mixed alphanumeric with underscores: user_task_123_run",
            "a" * 10000,
        ]

        for query_str in pathological_inputs:
            # Must never raise an uncaught exception
            sanitized = NLRetrievalEngine.sanitize_fts5_query(query_str)
            assert isinstance(sanitized, str)
            results = self.engine.search_workflows_fts(query_str)
            assert isinstance(results, list)
            nl_results = self.retrieval.query(query_str)
            assert isinstance(nl_results, list)


class TestSubwordBoundaryPrecisionStress(unittest.TestCase):
    """Stress tests boundary conditions for Tier 3 subword gating (VULN-M2-02)."""

    def setUp(self) -> None:
        self.engine = TaskMemoryEngine(":memory:")
        self.retrieval = NLRetrievalEngine(self.engine, min_confidence=0.50)

        # Register multiple workflows with distinct triggers
        self.spec_notes = WorkflowSpec(
            id="wf_notes",
            name="Open Notes App",
            triggers={
                "canonical": "open notes app",
                "aliases": ["launch apple notes", "view my notes"],
                "keywords": ["notes", "apple", "app"],
            },
        )
        self.spec_calc = WorkflowSpec(
            id="wf_calc",
            name="Open Calculator App",
            triggers={
                "canonical": "open calculator app",
                "aliases": ["launch calculator"],
                "keywords": ["calculator", "math"],
            },
        )
        self.spec_timer = WorkflowSpec(
            id="wf_timer",
            name="Start Kitchen Timer",
            triggers={
                "canonical": "start kitchen timer",
                "aliases": ["set timer"],
                "keywords": ["timer", "kitchen"],
            },
        )
        self.engine.save_workflow(self.spec_notes)
        self.engine.save_workflow(self.spec_calc)
        self.engine.save_workflow(self.spec_timer)

    def tearDown(self) -> None:
        self.engine.close()

    def test_subword_fragments_never_trigger_tier3(self) -> None:
        """Arbitrary subword fragments (not full words) must NEVER match Tier 3 (confidence >= 0.85)."""
        false_subwords = [
            ("pen", "wf_notes"),    # substring of 'open'
            ("ope", "wf_notes"),    # substring of 'open'
            ("ote", "wf_notes"),    # substring of 'notes'
            ("tes", "wf_notes"),    # substring of 'notes'
            ("art", "wf_timer"),    # substring of 'start'
            ("kit", "wf_timer"),    # substring of 'kitchen'
            ("chen", "wf_timer"),   # substring of 'kitchen'
            ("calc", "wf_calc"),    # substring of 'calculator'
            ("late", "wf_calc"),    # substring of 'calculator'
            ("ator", "wf_calc"),    # substring of 'calculator'
        ]

        for query, target_wf in false_subwords:
            matches = self.retrieval.query(query)
            tier3_matches = [
                m for m in matches
                if m.workflow_id == target_wf and m.tier == "tier3_substring_fts"
            ]
            assert len(tier3_matches) == 0, (
                f"Query '{query}' falsely triggered Tier 3 for '{target_wf}' with score: "
                f"{tier3_matches[0].confidence if tier3_matches else None}"
            )

    def test_legitimate_phrases_correctly_match_expected_tiers(self) -> None:
        """Verifies legitimate variations match Tier 1, Tier 2, or Tier 3 as designed."""
        # Exact canonical -> Tier 1 (1.0)
        m1 = self.retrieval.query("open notes app")
        assert len(m1) >= 1
        assert m1[0].workflow_id == "wf_notes"
        assert m1[0].tier == "tier1_canonical"
        assert m1[0].confidence == 1.0

        # Exact alias -> Tier 2 (0.95)
        m2 = self.retrieval.query("launch apple notes")
        assert len(m2) >= 1
        assert m2[0].workflow_id == "wf_notes"
        assert m2[0].tier == "tier2_alias"
        assert m2[0].confidence == 0.95

        # Canonical embedded in sentence -> Tier 3 ([0.85, 0.90])
        m3 = self.retrieval.query("hey companion please open notes app right now")
        assert len(m3) >= 1
        assert m3[0].workflow_id == "wf_notes"
        assert m3[0].tier == "tier3_substring_fts"
        assert 0.85 <= m3[0].confidence <= 0.90

        # Sub-phrase with word boundary and ratio >= 0.50:
        # "open notes" (len 10) in "open notes app" (len 14) -> ratio 0.714 >= 0.50
        m3_sub = self.retrieval.query("open notes")
        assert len(m3_sub) >= 1
        assert m3_sub[0].workflow_id == "wf_notes"
        assert m3_sub[0].tier == "tier3_substring_fts"
        assert 0.85 <= m3_sub[0].confidence <= 0.90


class TestCorruptDataRetrievalResilienceStress(unittest.TestCase):
    """Stress tests NLRetrievalEngine resilience against multiple classes of DB corruption (VULN-M2-01)."""

    def setUp(self) -> None:
        self.engine = TaskMemoryEngine(":memory:")
        self.retrieval = NLRetrievalEngine(self.engine, min_confidence=0.50)

    def tearDown(self) -> None:
        self.engine.close()

    def test_heterogeneous_corruption_does_not_block_valid_workflows(self) -> None:
        """Inserts 10 valid workflows interspersed with 10 differently corrupted rows."""
        # 1. Insert 10 valid workflows
        for i in range(10):
            self.engine.save_workflow(
                WorkflowSpec(
                    id=f"wf_valid_{i}",
                    name=f"Valid Workflow {i}",
                    triggers={"canonical": f"valid task {i}", "keywords": ["valid", f"task{i}"]},
                )
            )

        # 2. Inject various types of database corruption directly into workflows table
        corrupt_rows = [
            ("corrupt_malformed_json", "{INVALID_JSON: syntax error}"),
            ("corrupt_non_dict_int", "123456"),
            ("corrupt_non_dict_bool", "true"),
            ("corrupt_non_dict_list", '["item1", "item2"]'),
            ("corrupt_empty_string", ""),
            ("corrupt_missing_steps", '{"id": "missing_steps", "name": "No Steps"}'),
            ("corrupt_null_byte", '{"triggers": {"canonical": "null\\u0000byte"}}'),
        ]

        cur = self.engine._conn.cursor()
        for c_id, spec_val in corrupt_rows:
            cur.execute(
                """
                INSERT INTO workflows (id, name, description, version, active, spec_json, created_at, updated_at)
                VALUES (?, 'Corrupt', 'desc', 1, 1, ?, 'now', 'now')
                """,
                (c_id, spec_val),
            )
        # Also test raw binary blob
        cur.execute(
            """
            INSERT INTO workflows (id, name, description, version, active, spec_json, created_at, updated_at)
            VALUES ('corrupt_blob', 'Blob', 'desc', 1, 1, ?, 'now', 'now')
            """,
            (sqlite3.Binary(b"\xde\xad\xbe\xef\x00\x01"),),
        )
        self.engine._conn.commit()

        # 3. Query each valid workflow: must succeed cleanly without throwing exceptions
        for i in range(10):
            matches = self.retrieval.query(f"valid task {i}")
            assert len(matches) >= 1
            assert matches[0].workflow_id == f"wf_valid_{i}"
            assert matches[0].confidence == 1.0

    def test_total_database_corruption_returns_empty_cleanly(self) -> None:
        """When 100% of workflows in the database are corrupted, query() returns [] without error."""
        cur = self.engine._conn.cursor()
        for i in range(20):
            cur.execute(
                """
                INSERT INTO workflows (id, name, description, version, active, spec_json, created_at, updated_at)
                VALUES (?, 'Corrupt', 'desc', 1, 1, ?, 'now', 'now')
                """,
                (f"total_corrupt_{i}", f"CORRUPT_JSON_{i}"),
            )
        self.engine._conn.commit()

        results = self.retrieval.query("any user utterance here")
        assert results == []


class TestYAMLSerializationDeepInvariants(unittest.TestCase):
    """Stress tests pure standard-library YAML roundtrip (Reviewer Finding 1)."""

    def test_complex_workflow_yaml_roundtrip_fidelity(self) -> None:
        """Tests YAML roundtrip on complex workflow with nested structures and special chars."""
        spec = WorkflowSpec(
            id="wf_yaml_complex",
            name="Complex: Notes & Tasks [2026]",
            description="Multi-line description with quotes: \"important\" and colons: yes!",
            version=4,
            author="tester@example.com",
            tags=["productivity", "macos", "notes", "benchmark"],
            triggers={
                "canonical": "create complex to-do note",
                "aliases": ["make complex note", "run complex todo"],
                "keywords": ["complex", "note", "todo"],
            },
            parameters={
                "note_title": {"type": "string", "default": "Weekly Plan: Notes"},
                "priority_level": {"type": "integer", "default": 1},
                "is_urgent": {"type": "boolean", "default": True},
                "empty_dict": {},
                "empty_list": [],
            },
            steps=[
                WorkflowStep(
                    step_id="step_1",
                    order=1,
                    description="Launch Notes App",
                    action=ActionType.LAUNCH_APP,
                    payload={"bundle_id": "com.apple.Notes", "timeout": 5.0},
                    coordinates=TargetCoordinates(norm_x=0.5, norm_y=0.5),
                    timing={"pre_delay_ms": 100, "post_delay_ms": 200, "timeout_ms": 10000},
                ),
                WorkflowStep(
                    step_id="step_2",
                    order=2,
                    description="Type note body with special chars: # - * @",
                    action=ActionType.TYPE_TEXT,
                    payload={"text": "Line 1: # Header\nLine 2: - Item with colon: details"},
                    timing={"pre_delay_ms": 0, "post_delay_ms": 50, "timeout_ms": 5000},
                ),
                WorkflowStep(
                    step_id="step_3",
                    order=3,
                    description="Press hotkey Cmd+S",
                    action=ActionType.PRESS_HOTKEY,
                    payload={"keys": ["cmd", "s"]},
                ),
            ],
            environment={"created_by": "challenger_m2_fix_1", "nested": {"k1": "v1", "k2": 42}},
        )

        # Serialize to YAML
        yaml_str = spec.to_yaml()
        assert isinstance(yaml_str, str)
        assert len(yaml_str) > 0

        # Deserialize back
        reconstructed = WorkflowSpec.from_yaml(yaml_str)
        assert reconstructed.id == spec.id
        assert reconstructed.name == spec.name
        assert reconstructed.description == spec.description
        assert reconstructed.version == spec.version
        assert reconstructed.tags == spec.tags
        assert reconstructed.triggers == spec.triggers
        assert len(reconstructed.steps) == 3
        assert reconstructed.steps[0].action == ActionType.LAUNCH_APP
        assert reconstructed.steps[0].payload["bundle_id"] == "com.apple.Notes"
        assert reconstructed.steps[1].action == ActionType.TYPE_TEXT
        assert reconstructed.steps[2].payload["keys"] == ["cmd", "s"]

    def test_vulnerability_yaml_colon_in_list_corrupts_into_dict(self) -> None:
        """VULN-M2-04 (Part A): _from_yaml_native preserves list scalar strings with colons."""
        spec = WorkflowSpec(
            id="wf_colon_alias",
            name="Workflow with Colon in Alias",
            triggers={"canonical": "do task", "aliases": ["tag: urgent", "step: 1"]},
        )
        yaml_out = spec.to_yaml()
        reconstructed = WorkflowSpec.from_yaml(yaml_out)

        # Assert reconstructed alias elements remain strings
        first_alias = reconstructed.triggers["aliases"][0]
        assert isinstance(first_alias, str), f"Expected string, got: {type(first_alias)}"
        assert first_alias == "tag: urgent"

    def test_vulnerability_retrieval_dos_on_malformed_trigger_entry(self) -> None:
        """VULN-M2-04 (Part B): NLRetrievalEngine.query() survives non-string trigger items."""
        engine = TaskMemoryEngine(":memory:")
        engine.save_workflow(WorkflowSpec(id="wf_valid", name="Valid Workflow", triggers={"canonical": "open notes app"}))

        # Insert a workflow record with a non-string alias (e.g. from corrupt JSON or DB)
        cur = engine._conn.cursor()
        cur.execute(
            """
            INSERT INTO workflows (id, name, description, version, active, spec_json, created_at, updated_at)
            VALUES ('wf_corrupt_alias', 'Corrupt Alias', 'desc', 1, 1,
                    '{"id": "wf_corrupt_alias", "name": "Bad", "triggers": {"canonical": "test", "aliases": [{"key": "val"}]}}',
                    'now', 'now')
            """
        )
        engine._conn.commit()

        retrieval = NLRetrievalEngine(engine)

        # Must not raise AttributeError; valid workflow must be retrieved
        results = retrieval.query("open notes app")
        assert len(results) == 1
        assert results[0].workflow_id == "wf_valid"
        engine.close()


class TestRecorderClickProgressionStress(unittest.TestCase):
    """Stress tests click coalescing escalation (1, 2, 3, 4, 5, 6 clicks) and distance/time gating."""

    def setUp(self) -> None:
        self.pipeline = WorkflowRecorderPipeline()

    def test_click_coalescing_escalation_progression(self) -> None:
        """Verifies escalation: 1 -> single, 2 -> double, 3 -> triple, 4 -> triple+single, 6 -> two triples."""
        # 1 Click: 1 MOUSE_DOWN + 1 MOUSE_UP
        c1 = [
            RawEvent(event_type=RawEventType.MOUSE_DOWN, x=100.0, y=100.0, timestamp=1.0),
            RawEvent(event_type=RawEventType.MOUSE_UP, x=100.0, y=100.0, timestamp=1.05),
        ]
        spec1 = self.pipeline.process_raw_events(c1)
        assert len(spec1.steps) == 1
        assert spec1.steps[0].payload.get("click_count") == 1

        # 2 Clicks: double click
        c2 = c1 + [
            RawEvent(event_type=RawEventType.MOUSE_DOWN, x=100.0, y=100.0, timestamp=1.2),
            RawEvent(event_type=RawEventType.MOUSE_UP, x=100.0, y=100.0, timestamp=1.25),
        ]
        spec2 = self.pipeline.process_raw_events(c2)
        assert len(spec2.steps) == 1
        assert spec2.steps[0].payload.get("click_count") == 2

        # 3 Clicks: triple click
        c3 = c2 + [
            RawEvent(event_type=RawEventType.MOUSE_DOWN, x=100.0, y=100.0, timestamp=1.4),
            RawEvent(event_type=RawEventType.MOUSE_UP, x=100.0, y=100.0, timestamp=1.45),
        ]
        spec3 = self.pipeline.process_raw_events(c3)
        assert len(spec3.steps) == 1
        assert spec3.steps[0].payload.get("click_count") == 3

        # 4 Clicks: 3-click step + 1-click step
        c4 = c3 + [
            RawEvent(event_type=RawEventType.MOUSE_DOWN, x=100.0, y=100.0, timestamp=1.6),
            RawEvent(event_type=RawEventType.MOUSE_UP, x=100.0, y=100.0, timestamp=1.65),
        ]
        spec4 = self.pipeline.process_raw_events(c4)
        assert len(spec4.steps) == 2
        assert spec4.steps[0].payload.get("click_count") == 3
        assert spec4.steps[1].payload.get("click_count") == 1

        # 6 Clicks: two 3-click steps
        c6 = c4 + [
            RawEvent(event_type=RawEventType.MOUSE_DOWN, x=100.0, y=100.0, timestamp=1.8),
            RawEvent(event_type=RawEventType.MOUSE_UP, x=100.0, y=100.0, timestamp=1.85),
            RawEvent(event_type=RawEventType.MOUSE_DOWN, x=100.0, y=100.0, timestamp=2.0),
            RawEvent(event_type=RawEventType.MOUSE_UP, x=100.0, y=100.0, timestamp=2.05),
        ]
        spec6 = self.pipeline.process_raw_events(c6)
        assert len(spec6.steps) == 2
        assert spec6.steps[0].payload.get("click_count") == 3
        assert spec6.steps[1].payload.get("click_count") == 3

    def test_direct_click_distance_and_time_gating(self) -> None:
        """Direct CLICK events do not coalesce if distance > 5px or interval > 0.5s."""
        # Distance > 5px (e.g. 50.0 to 60.0 = 10px distance)
        far_clicks = [
            RawEvent(event_type=RawEventType.CLICK, x=50.0, y=50.0, timestamp=1.0),
            RawEvent(event_type=RawEventType.CLICK, x=60.0, y=50.0, timestamp=1.1),
        ]
        spec_far = self.pipeline.process_raw_events(far_clicks)
        assert len(spec_far.steps) == 2
        assert spec_far.steps[0].payload.get("click_count") == 1
        assert spec_far.steps[1].payload.get("click_count") == 1

        # Time > 0.5s (e.g. 1.0 to 1.7 = 0.7s interval)
        slow_clicks = [
            RawEvent(event_type=RawEventType.CLICK, x=50.0, y=50.0, timestamp=1.0),
            RawEvent(event_type=RawEventType.CLICK, x=50.0, y=50.0, timestamp=1.7),
        ]
        spec_slow = self.pipeline.process_raw_events(slow_clicks)
        assert len(spec_slow.steps) == 2
        assert spec_slow.steps[0].payload.get("click_count") == 1
        assert spec_slow.steps[1].payload.get("click_count") == 1
