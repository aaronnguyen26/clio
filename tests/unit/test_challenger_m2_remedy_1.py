"""Empirical Challenger M2 Remedy 1 Stress Test Harness.

Author: Challenger M2 Remedy 1 (challenger_m2_remedy_1)
Target Milestone: M2 Remediation Verification (Database, FTS, Concurrency, Retrieval, YAML, Coalescing)

Empirical Challenge Areas:
1. VULN-M2-04: Non-string trigger inputs (dicts, ints, None, nested lists, booleans, floats, bytes)
   in database workflows do not crash NLRetrievalEngine.query() or normalize_utterance().
2. VULN-M2-05: Strings containing colons, URLs (http://, https://), colons without space,
   colons with space in list items all preserve scalar identity and YAML roundtrip.
3. DEFECT-M2-01: Rapid mixed clicks (left then right, right then left, middle click) at same
   coordinates never coalesce across differing buttons.
4. Concurrency: Multi-threaded queries and soft-delete FTS synchronization on both in-memory
   and on-disk SQLite WAL engines.
"""

from __future__ import annotations

import math
import os
import random
import sqlite3
import tempfile
import threading
import time
import unittest
from typing import Any, Dict, List, Optional

import pytest

from src.actuators.types import MouseButton
from src.memory.engine import TaskMemoryEngine
from src.memory.models import (
    ActionType,
    ExecutionRecord,
    MatchResult,
    SerializationError,
    TargetCoordinates,
    ValidationError,
    WorkflowSpec,
    WorkflowStep,
)
from src.memory.recorder import (
    RawEvent,
    RawEventType,
    RecorderConfig,
    WorkflowRecorderPipeline,
)
from src.memory.retrieval import NLRetrievalEngine


# ==============================================================================
# 1. VULN-M2-04: Non-String Trigger Inputs In Database Workflows
# ==============================================================================


class TestVulnM204NonStringTriggersEmpirical(unittest.TestCase):
    """Empirical challenge suite for VULN-M2-04: Malformed and non-string trigger fields."""

    def setUp(self) -> None:
        self.engine = TaskMemoryEngine(":memory:")
        self.retrieval = NLRetrievalEngine(self.engine, min_confidence=0.50)

    def tearDown(self) -> None:
        self.engine.close()

    def test_normalize_utterance_resilience_to_all_non_string_types(self) -> None:
        """normalize_utterance must cleanly return empty string for any non-string type."""
        adversarial_inputs = [
            None,
            42,
            -100,
            3.14159,
            True,
            False,
            [],
            [1, 2, "test"],
            {},
            {"canonical": "open notes"},
            {"nested": {"dict": True}},
            b"raw bytes",
            object(),
            set(["a", "b"]),
        ]
        for inp in adversarial_inputs:
            res = NLRetrievalEngine.normalize_utterance(inp)  # type: ignore[arg-type]
            self.assertEqual(
                res,
                "",
                f"normalize_utterance({inp!r}) returned {res!r} instead of empty string",
            )

    def test_heterogeneous_malformed_trigger_database_isolation(self) -> None:
        """Seed DB with 12 distinct corrupt trigger variations and 1 valid workflow.

        Invariant: NLRetrievalEngine.query() must NEVER crash, must isolate the
        corrupted records, and must successfully match and retrieve the valid workflow.
        """
        # Save a valid target workflow
        valid_spec = WorkflowSpec(
            id="wf_valid_target",
            name="Apple Notes Creation",
            triggers={
                "canonical": "create new apple note",
                "aliases": ["new note in notes", "make a note"],
                "keywords": ["notes", "apple", "document"],
            },
            steps=[
                WorkflowStep(
                    step_id="step_1",
                    order=1,
                    description="Launch Notes",
                    action=ActionType.LAUNCH_APP,
                    payload={"bundle_id": "com.apple.Notes"},
                )
            ],
        )
        self.engine.save_workflow(valid_spec)

        # Inject 12 distinct malformed trigger specs directly into the database
        malformed_specs = [
            # 1. Triggers is None
            '{"id": "wf_bad_1", "name": "Bad 1", "triggers": null}',
            # 2. Triggers is an integer
            '{"id": "wf_bad_2", "name": "Bad 2", "triggers": 12345}',
            # 3. Triggers is a list
            '{"id": "wf_bad_3", "name": "Bad 3", "triggers": ["create new apple note"]}',
            # 4. Triggers is a string
            '{"id": "wf_bad_4", "name": "Bad 4", "triggers": "create new apple note"}',
            # 5. Canonical is a dict
            '{"id": "wf_bad_5", "name": "Bad 5", "triggers": {"canonical": {"nested": "value"}}}',
            # 6. Canonical is an int
            '{"id": "wf_bad_6", "name": "Bad 6", "triggers": {"canonical": 9999}}',
            # 7. Canonical is None
            '{"id": "wf_bad_7", "name": "Bad 7", "triggers": {"canonical": null}}',
            # 8. Aliases is an integer
            '{"id": "wf_bad_8", "name": "Bad 8", "triggers": {"canonical": "test", "aliases": 42}}',
            # 9. Aliases is a dict
            '{"id": "wf_bad_9", "name": "Bad 9", "triggers": {"canonical": "test", "aliases": {"key": "val"}}}',
            # 10. Aliases list contains mixed non-strings (dicts, ints, lists, None, booleans)
            '{"id": "wf_bad_10", "name": "Bad 10", "triggers": {"canonical": "test", "aliases": [{"dict": 1}, 100, null, true, ["nested"]]}}',
            # 11. Keywords is a dict
            '{"id": "wf_bad_11", "name": "Bad 11", "triggers": {"canonical": "test", "keywords": {"bad": 1}}}',
            # 12. Keywords list contains mixed non-strings
            '{"id": "wf_bad_12", "name": "Bad 12", "triggers": {"canonical": "test", "keywords": [{"nested": "kw"}, 999, null]}}',
        ]

        cur = self.engine._conn.cursor()
        for idx, raw_json in enumerate(malformed_specs, start=1):
            cur.execute(
                """
                INSERT INTO workflows (id, name, description, version, active, spec_json, created_at, updated_at)
                VALUES (?, ?, 'Adversarial corrupt workflow', 1, 1, ?, '2026-09-23T00:00:00Z', '2026-09-23T00:00:00Z')
                """,
                (f"wf_bad_{idx}", f"Bad Workflow {idx}", raw_json),
            )
        self.engine._conn.commit()

        # Query Tier 1 exact canonical match
        t1_results = self.retrieval.query("create new apple note")
        self.assertGreaterEqual(len(t1_results), 1)
        self.assertEqual(t1_results[0].workflow_id, "wf_valid_target")
        self.assertEqual(t1_results[0].confidence, 1.0)
        self.assertEqual(t1_results[0].tier, "tier1_canonical")

        # Query Tier 2 exact alias match
        t2_results = self.retrieval.query("new note in notes")
        self.assertGreaterEqual(len(t2_results), 1)
        self.assertEqual(t2_results[0].workflow_id, "wf_valid_target")
        self.assertEqual(t2_results[0].confidence, 0.95)
        self.assertEqual(t2_results[0].tier, "tier2_alias")

        # Query Tier 3 substring match
        t3_results = self.retrieval.query("please create new apple note today")
        self.assertGreaterEqual(len(t3_results), 1)
        self.assertEqual(t3_results[0].workflow_id, "wf_valid_target")
        self.assertGreaterEqual(t3_results[0].confidence, 0.85)
        self.assertLessEqual(t3_results[0].confidence, 0.90)

        # Query Tier 4 token fuzzy match
        t4_results = self.retrieval.query("apple document notes")
        self.assertGreaterEqual(len(t4_results), 1)
        self.assertEqual(t4_results[0].workflow_id, "wf_valid_target")
        self.assertGreaterEqual(t4_results[0].confidence, 0.50)

        # Query best
        best = self.retrieval.query_best("create new apple note")
        self.assertIsNotNone(best)
        assert best is not None
        self.assertEqual(best.workflow_id, "wf_valid_target")

    def test_all_corrupt_database_clean_empty_return(self) -> None:
        """When database contains ONLY malformed workflows, query() must return [] cleanly."""
        cur = self.engine._conn.cursor()
        for i in range(5):
            cur.execute(
                """
                INSERT INTO workflows (id, name, description, version, active, spec_json, created_at, updated_at)
                VALUES (?, 'CorruptOnly', 'desc', 1, 1, '{"id": "c", "triggers": {"canonical": {"k": "v"}, "aliases": [1, 2, 3]}}', 'now', 'now')
                """,
                (f"corrupt_{i}",),
            )
        self.engine._conn.commit()

        results = self.retrieval.query("any random trigger")
        self.assertEqual(results, [])
        self.assertIsNone(self.retrieval.query_best("any random trigger"))


# ==============================================================================
# 2. VULN-M2-05: Strings With Colons, URLs, and YAML Roundtrip Invariants
# ==============================================================================


class TestVulnM205YamlColonAndUrlRoundtripEmpirical(unittest.TestCase):
    """Empirical challenge suite for VULN-M2-05: YAML string scalar preservation."""

    def test_yaml_roundtrip_exhaustive_colon_variations(self) -> None:
        """Exhaustively verify strings with colons across tags, triggers, payloads, and parameters.

        Invariants:
        1. URLs ('http://...', 'https://...') in lists must remain strings.
        2. Colons without spaces ('tag:urgent', '12:30:00', '::1') must remain strings.
        3. Colons with spaces ('note: see step 1') must remain strings.
        4. Reconstructed spec must match original spec identically.
        """
        test_strings = [
            "http://localhost:8080/v1/api/endpoint",
            "https://user:password@service.apple.com:443/notes?filter=all&sort=desc#anchor",
            "ftp://files.example.org:21/dump.tar.gz",
            "file:///Users/username/Desktop/file.txt",
            "tag:urgent",
            "env:macos",
            "version:2.0.1",
            "time:12:30:45",
            "ipv6:::1",
            "ratio:16:9",
            "step 1: open browser",
            "error: execution failed: timeout exceeded",
            "label: urgent: high priority: review now",
            "colon:no_space:after_colon",
            "trailing colon:",
            ":leading colon",
            "日本語: テスト",
            "Tiếng Việt: Thử nghiệm",
        ]

        spec = WorkflowSpec(
            id="wf_exhaustive_colons",
            name="Exhaustive Colon and URL Workflow",
            description="Testing YAML round-trip with varied colon placements",
            version=1,
            tags=test_strings,
            triggers={
                "canonical": "open https://apple.com/notes",
                "aliases": test_strings,
                "keywords": [f"kw:{s}" for s in test_strings[:5]],
            },
            parameters={"api_url": "https://api.github.com:443", "header": "Authorization: Bearer token:xyz"},
            steps=[
                WorkflowStep(
                    step_id="step_1",
                    order=1,
                    description="Step with colon: in description",
                    action=ActionType.TYPE_TEXT,
                    payload={"text": "Url: https://google.com", "list_payload": test_strings},
                )
            ],
            environment={"source:url": "https://localhost:9000"},
        )

        yaml_output = spec.to_yaml()
        self.assertIsInstance(yaml_output, str)
        self.assertGreater(len(yaml_output), 0)

        # Deserialize back
        reconstructed = WorkflowSpec.from_yaml(yaml_output)

        # Assert ID and basic metadata
        self.assertEqual(reconstructed.id, spec.id)
        self.assertEqual(reconstructed.name, spec.name)
        self.assertEqual(reconstructed.description, spec.description)

        # Assert tags: every single item must remain a string scalar
        self.assertEqual(len(reconstructed.tags), len(test_strings))
        for orig, recon in zip(test_strings, reconstructed.tags):
            self.assertIsInstance(
                recon,
                str,
                f"Tag {orig!r} deserialized as {type(recon)}: {recon!r}",
            )
            self.assertEqual(recon, orig)

        # Assert trigger aliases: every item must remain a string scalar
        self.assertEqual(len(reconstructed.triggers["aliases"]), len(test_strings))
        for orig, recon in zip(test_strings, reconstructed.triggers["aliases"]):
            self.assertIsInstance(
                recon,
                str,
                f"Alias {orig!r} deserialized as {type(recon)}: {recon!r}",
            )
            self.assertEqual(recon, orig)

        # Assert step payload list: every item must remain a string scalar
        payload_list = reconstructed.steps[0].payload.get("list_payload", [])
        self.assertEqual(len(payload_list), len(test_strings))
        for orig, recon in zip(test_strings, payload_list):
            self.assertIsInstance(
                recon,
                str,
                f"Payload list item {orig!r} deserialized as {type(recon)}: {recon!r}",
            )
            self.assertEqual(recon, orig)

    def test_yaml_unquoted_scalars_with_colons_in_manual_yaml(self) -> None:
        """Verify _from_yaml_native parses unquoted list items containing colons without trailing space as scalars."""
        manual_yaml = """
id: wf_manual_urls
name: Manual URLs
tags:
  - http://example.com/api
  - https://apple.com/notes
  - colon_no_space:value
  - ratio:16:9
  - "quoted: with space"
  - 'single quoted: with space'
"""
        parsed = WorkflowSpec._from_yaml_native(manual_yaml)
        tags = parsed.get("tags", [])
        self.assertEqual(len(tags), 6)
        self.assertEqual(tags[0], "http://example.com/api")
        self.assertEqual(tags[1], "https://apple.com/notes")
        self.assertEqual(tags[2], "colon_no_space:value")
        self.assertEqual(tags[3], "ratio:16:9")
        self.assertEqual(tags[4], "quoted: with space")
        self.assertEqual(tags[5], "single quoted: with space")
        for tag in tags:
            self.assertIsInstance(tag, str)

    def test_yaml_empty_collections_inside_lists_roundtrip(self) -> None:
        """Verify empty dicts and empty lists inside lists roundtrip cleanly without breaking indentation."""
        spec = WorkflowSpec(
            id="wf_empty_colls",
            name="Empty Collections In List",
            steps=[
                WorkflowStep(
                    step_id="step_1",
                    order=1,
                    description="Mixed empty collections in list payload",
                    action=ActionType.TYPE_TEXT,
                    payload={"mixed": [{}, [], {"k": "v"}, "url: https://apple.com", {}, []]},
                )
            ],
        )
        yaml_out = spec.to_yaml()
        recon = WorkflowSpec.from_yaml(yaml_out)
        mixed = recon.steps[0].payload["mixed"]
        self.assertEqual(mixed[0], {})
        self.assertEqual(mixed[1], [])
        self.assertEqual(mixed[2], {"k": "v"})
        self.assertEqual(mixed[3], "url: https://apple.com")
        self.assertEqual(mixed[4], {})
        self.assertEqual(mixed[5], [])


# ==============================================================================
# 3. DEFECT-M2-01: Mouse Click Coalescing Button Isolation
# ==============================================================================


class TestDefectM201MixedButtonClickCoalescingEmpirical(unittest.TestCase):
    """Empirical challenge suite for DEFECT-M2-01: Mouse button separation during click coalescing."""

    def setUp(self) -> None:
        self.pipeline = WorkflowRecorderPipeline()

    def test_rapid_mixed_clicks_never_coalesce_across_differing_buttons(self) -> None:
        """Rapid clicks at IDENTICAL coordinates within double-click interval MUST NOT coalesce if buttons differ.

        Test permutations:
        1. Left then Right (dt=0.05s, dist=0) -> 2 distinct steps
        2. Right then Left (dt=0.05s, dist=0) -> 2 distinct steps
        3. Left then Middle (dt=0.05s, dist=0) -> 2 distinct steps
        4. Middle then Right then Left (dt=0.05s, dist=0) -> 3 distinct steps
        5. Left, Right, Left, Right, Middle (dt=0.05s, dist=0) -> 5 distinct steps
        """
        permutations = [
            ([MouseButton.LEFT, MouseButton.RIGHT], 2, ["left", "right"]),
            ([MouseButton.RIGHT, MouseButton.LEFT], 2, ["right", "left"]),
            ([MouseButton.LEFT, MouseButton.MIDDLE], 2, ["left", "middle"]),
            ([MouseButton.MIDDLE, MouseButton.RIGHT, MouseButton.LEFT], 3, ["middle", "right", "left"]),
            (
                [MouseButton.LEFT, MouseButton.RIGHT, MouseButton.LEFT, MouseButton.RIGHT, MouseButton.MIDDLE],
                5,
                ["left", "right", "left", "right", "middle"],
            ),
        ]

        for buttons, expected_count, expected_buttons in permutations:
            with self.subTest(sequence=[b.value for b in buttons]):
                events: List[RawEvent] = []
                t = 100.0
                for btn in buttons:
                    events.append(
                        RawEvent(
                            event_type=RawEventType.MOUSE_DOWN,
                            x=500.0,
                            y=400.0,
                            button=btn,
                            timestamp=t,
                        )
                    )
                    t += 0.02
                    events.append(
                        RawEvent(
                            event_type=RawEventType.MOUSE_UP,
                            x=500.0,
                            y=400.0,
                            button=btn,
                            timestamp=t,
                        )
                    )
                    t += 0.03  # Within 0.5s double_click_interval_s

                spec = self.pipeline.process_raw_events(events)
                click_steps = [s for s in spec.steps if s.action == ActionType.CLICK]
                self.assertEqual(
                    len(click_steps),
                    expected_count,
                    f"Expected {expected_count} distinct steps for buttons {[b.value for b in buttons]}, got {len(click_steps)}",
                )
                actual_buttons = [
                    str(getattr(s.payload.get("button"), "value", s.payload.get("button"))).lower()
                    for s in click_steps
                ]
                # Normalize button aliases ('center' vs 'middle')
                actual_buttons_norm = ["middle" if b == "center" else b for b in actual_buttons]
                expected_buttons_norm = ["middle" if b == "center" else b for b in expected_buttons]
                self.assertEqual(actual_buttons_norm, expected_buttons_norm)
                # Verify click_count is 1 for each distinct step
                for step in click_steps:
                    self.assertEqual(step.payload.get("click_count", 1), 1)

    def test_same_button_clicks_properly_coalesce_into_double_and_triple_clicks(self) -> None:
        """Same button clicks at same position within interval MUST coalesce (double/triple click)."""
        test_cases = [
            (MouseButton.LEFT, 2, 1, 2, "Double click"),
            (MouseButton.LEFT, 3, 1, 3, "Triple click"),
            (MouseButton.RIGHT, 2, 1, 2, "Double click"),
            (MouseButton.MIDDLE, 2, 1, 2, "Double click"),
            (MouseButton.MIDDLE, 3, 1, 3, "Triple click"),
        ]

        for btn, num_clicks, expected_steps, expected_click_count, expected_desc in test_cases:
            with self.subTest(btn=btn.value, num_clicks=num_clicks):
                events: List[RawEvent] = []
                t = 200.0
                for _ in range(num_clicks):
                    events.append(
                        RawEvent(
                            event_type=RawEventType.MOUSE_DOWN,
                            x=300.0,
                            y=300.0,
                            button=btn,
                            timestamp=t,
                        )
                    )
                    t += 0.02
                    events.append(
                        RawEvent(
                            event_type=RawEventType.MOUSE_UP,
                            x=300.0,
                            y=300.0,
                            button=btn,
                            timestamp=t,
                        )
                    )
                    t += 0.03

                spec = self.pipeline.process_raw_events(events)
                click_steps = [s for s in spec.steps if s.action == ActionType.CLICK]
                self.assertEqual(len(click_steps), expected_steps)
                self.assertEqual(click_steps[0].payload.get("click_count"), expected_click_count)
                self.assertIn(expected_desc, click_steps[0].description)

    def test_mixed_coalescing_and_non_coalescing_chains(self) -> None:
        """Chains of same-button double clicks interleaved with other button clicks.

        Pattern: [LEFT, LEFT, RIGHT, RIGHT, LEFT, MIDDLE, MIDDLE]
        Expected steps:
        - Step 1: LEFT, click_count=2
        - Step 2: RIGHT, click_count=2
        - Step 3: LEFT, click_count=1
        - Step 4: MIDDLE, click_count=2
        """
        sequence = [
            MouseButton.LEFT,
            MouseButton.LEFT,
            MouseButton.RIGHT,
            MouseButton.RIGHT,
            MouseButton.LEFT,
            MouseButton.MIDDLE,
            MouseButton.MIDDLE,
        ]

        events: List[RawEvent] = []
        t = 300.0
        for btn in sequence:
            events.append(
                RawEvent(
                    event_type=RawEventType.MOUSE_DOWN,
                    x=400.0,
                    y=400.0,
                    button=btn,
                    timestamp=t,
                )
            )
            t += 0.02
            events.append(
                RawEvent(
                    event_type=RawEventType.MOUSE_UP,
                    x=400.0,
                    y=400.0,
                    button=btn,
                    timestamp=t,
                )
            )
            t += 0.03

        spec = self.pipeline.process_raw_events(events)
        click_steps = [s for s in spec.steps if s.action == ActionType.CLICK]
        self.assertEqual(len(click_steps), 4)

        # Step 1: LEFT x2
        self.assertEqual(str(getattr(click_steps[0].payload["button"], "value", click_steps[0].payload["button"])).lower(), "left")
        self.assertEqual(click_steps[0].payload["click_count"], 2)

        # Step 2: RIGHT x2
        self.assertEqual(str(getattr(click_steps[1].payload["button"], "value", click_steps[1].payload["button"])).lower(), "right")
        self.assertEqual(click_steps[1].payload["click_count"], 2)

        # Step 3: LEFT x1
        self.assertEqual(str(getattr(click_steps[2].payload["button"], "value", click_steps[2].payload["button"])).lower(), "left")
        self.assertEqual(click_steps[2].payload["click_count"], 1)

        # Step 4: MIDDLE x2
        btn_4 = str(getattr(click_steps[3].payload["button"], "value", click_steps[3].payload["button"])).lower()
        self.assertIn(btn_4, ["center", "middle"])
        self.assertEqual(click_steps[3].payload["click_count"], 2)


# ==============================================================================
# 4. Concurrency: Multi-Threaded Queries & Soft-Delete FTS Synchronization
# ==============================================================================


class TestConcurrencyMultiThreadedAndFtsSoftDeleteEmpirical(unittest.TestCase):
    """Empirical challenge suite for high-concurrency multi-threaded queries and FTS soft-delete synchronization."""

    def test_concurrent_nl_queries_and_soft_delete_synchronization_in_memory(self) -> None:
        """High-concurrency test on in-memory SQLite database.

        Concurrent threads:
        - 8 Reader threads executing NLRetrievalEngine.query() continuously.
        - 4 Writer threads saving new workflows, soft-deleting existing workflows, and reactivating them.
        - 2 FTS Reader threads querying search_workflows_fts().

        Invariants:
        1. Zero crashes, zero database locks, zero SQLite OperationalErrors.
        2. Soft-deleted workflows are NEVER returned by NL query() or FTS search while inactive.
        3. Reactivated workflows are immediately discoverable again.
        """
        engine = TaskMemoryEngine(":memory:")
        retrieval = NLRetrievalEngine(engine, min_confidence=0.50)
        errors: List[str] = []
        stop_event = threading.Event()

        # Seed 10 baseline workflows
        for i in range(10):
            engine.save_workflow(
                WorkflowSpec(
                    id=f"wf_seed_{i}",
                    name=f"Seed Workflow {i}",
                    triggers={
                        "canonical": f"execute task seed_{i}",
                        "aliases": [f"run seed_{i}"],
                        "keywords": ["seed", f"task_{i}"],
                    },
                )
            )

        def reader_worker(reader_id: int) -> None:
            queries = [
                "execute task seed_0",
                "run seed_1",
                "execute task seed_2",
                "nonexistent random task",
                "seed",
            ]
            while not stop_event.is_set():
                q = random.choice(queries)
                try:
                    res = retrieval.query(q)
                    for r in res:
                        # If a result is returned, verify it is active in database
                        cur = engine._conn.cursor()
                        with engine._lock:
                            cur.execute("SELECT active FROM workflows WHERE id = ?", (r.workflow_id,))
                            row = cur.fetchone()
                            if row and row["active"] == 0:
                                errors.append(f"Reader {reader_id}: Returned soft-deleted workflow {r.workflow_id}")
                except Exception as ex:
                    errors.append(f"Reader {reader_id} crashed: {type(ex).__name__}: {ex}")
                time.sleep(0.005)

        def fts_worker(fts_id: int) -> None:
            while not stop_event.is_set():
                try:
                    res = engine.search_workflows_fts("seed", limit=10)
                    for r in res:
                        wf_id = r["workflow_id"]
                        cur = engine._conn.cursor()
                        with engine._lock:
                            cur.execute("SELECT active FROM workflows WHERE id = ?", (wf_id,))
                            row = cur.fetchone()
                            if row and row["active"] == 0:
                                errors.append(f"FTS worker {fts_id}: FTS returned soft-deleted workflow {wf_id}")
                except Exception as ex:
                    errors.append(f"FTS worker {fts_id} crashed: {type(ex).__name__}: {ex}")
                time.sleep(0.008)

        def writer_worker(writer_id: int) -> None:
            wf_id = f"wf_dynamic_writer_{writer_id}"
            canonical = f"dynamic task from writer_{writer_id}"

            for iteration in range(20):
                if stop_event.is_set():
                    break
                try:
                    # 1. Save workflow
                    spec = WorkflowSpec(
                        id=wf_id,
                        name=f"Dynamic {writer_id} Iter {iteration}",
                        triggers={"canonical": canonical, "keywords": ["dynamic", f"writer_{writer_id}"]},
                    )
                    engine.save_workflow(spec, change_summary=f"Iteration {iteration}")

                    # 2. Query it to confirm active
                    matches = retrieval.query(canonical)
                    match_ids = [m.workflow_id for m in matches]
                    if wf_id not in match_ids:
                        errors.append(f"Writer {writer_id}: Newly saved {wf_id} not retrievable")

                    # 3. Soft-delete
                    engine.delete_workflow(wf_id, soft=True)

                    # 4. Invariant check: Must NOT be returned
                    matches_after = retrieval.query(canonical)
                    match_ids_after = [m.workflow_id for m in matches_after]
                    if wf_id in match_ids_after:
                        errors.append(f"Writer {writer_id}: Soft-deleted {wf_id} was returned in NL query: {match_ids_after}")

                    fts_res = engine.search_workflows_fts(f"writer_{writer_id}")
                    fts_ids = [r["workflow_id"] for r in fts_res]
                    if wf_id in fts_ids:
                        errors.append(f"Writer {writer_id}: Soft-deleted {wf_id} was returned in FTS: {fts_ids}")

                    # 5. Reactivate
                    engine.save_workflow(spec, change_summary="Reactivating")
                    matches_reactivated = retrieval.query(canonical)
                    reactivated_ids = [m.workflow_id for m in matches_reactivated]
                    if wf_id not in reactivated_ids:
                        errors.append(f"Writer {writer_id}: Reactivated {wf_id} not retrievable")

                except Exception as ex:
                    errors.append(f"Writer {writer_id} crashed: {type(ex).__name__}: {ex}")
                time.sleep(0.01)

        # Launch threads
        threads: List[threading.Thread] = []
        for i in range(8):
            t = threading.Thread(target=reader_worker, args=(i,), daemon=True)
            threads.append(t)
        for i in range(2):
            t = threading.Thread(target=fts_worker, args=(i,), daemon=True)
            threads.append(t)
        for i in range(4):
            t = threading.Thread(target=writer_worker, args=(i,), daemon=True)
            threads.append(t)

        for t in threads:
            t.start()

        # Let the stress test run for 2.5 seconds
        time.sleep(2.5)
        stop_event.set()

        for t in threads:
            t.join(timeout=2.0)

        engine.close()
        self.assertEqual(errors, [], f"Encountered concurrency errors: {errors[:10]}")

    def test_on_disk_sqlite_wal_multi_engine_concurrent_hammering(self) -> None:
        """Multi-engine disk contention test on SQLite WAL database file.

        Multiple distinct TaskMemoryEngine instances pointing to the SAME on-disk file,
        simulating concurrent companion processes and daemon workers.
        """
        temp_dir = tempfile.mkdtemp()
        db_path = os.path.join(temp_dir, "concurrent_stress.db")
        errors: List[str] = []

        try:
            # Initialize database with primary engine
            init_engine = TaskMemoryEngine(db_path, pragma_wal=True)
            init_engine.save_workflow(
                WorkflowSpec(
                    id="wf_shared_anchor",
                    name="Shared Anchor",
                    triggers={"canonical": "open shared document", "keywords": ["shared", "anchor"]},
                )
            )
            init_engine.close()

            def process_worker(pid: int) -> None:
                try:
                    worker_engine = TaskMemoryEngine(db_path, pragma_wal=True)
                    worker_retrieval = NLRetrievalEngine(worker_engine, min_confidence=0.50)

                    for cycle in range(15):
                        wf_id = f"wf_proc_{pid}_{cycle}"
                        # Save
                        worker_engine.save_workflow(
                            WorkflowSpec(
                                id=wf_id,
                                name=f"Proc {pid} Cycle {cycle}",
                                triggers={"canonical": f"process task {pid}_{cycle}", "keywords": [f"proc_{pid}"]},
                            )
                        )
                        # Query shared anchor
                        res = worker_retrieval.query("open shared document")
                        if not res or res[0].workflow_id != "wf_shared_anchor":
                            errors.append(f"Process {pid}: Failed to retrieve shared anchor")

                        # Soft delete
                        worker_engine.delete_workflow(wf_id, soft=True)

                        # Confirm deletion
                        after = worker_retrieval.query(f"process task {pid}_{cycle}")
                        after_ids = [m.workflow_id for m in after]
                        if wf_id in after_ids:
                            errors.append(f"Process {pid}: Soft deleted {wf_id} was retrieved")

                    worker_engine.close()
                except Exception as ex:
                    errors.append(f"Process worker {pid} crashed: {type(ex).__name__}: {ex}")

            proc_threads = [
                threading.Thread(target=process_worker, args=(i,))
                for i in range(4)
            ]
            for pt in proc_threads:
                pt.start()
            for pt in proc_threads:
                pt.join(timeout=10.0)

            self.assertEqual(errors, [], f"On-disk WAL contention errors: {errors}")

        finally:
            for fname in os.listdir(temp_dir):
                try:
                    os.remove(os.path.join(temp_dir, fname))
                except OSError:
                    pass
            try:
                os.rmdir(temp_dir)
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main()
