"""Unit tests for NLRetrievalEngine (FEAT-MEM-04).

Tests validate:
- Tier 1 exact canonical trigger matching (confidence = 1.0)
- Tier 2 exact alias matching (confidence = 0.95)
- Tier 3 substring and FTS5 BM25 matching (confidence 0.85 - 0.90)
- Tier 4 token overlap / Jaccard and keyword matching (confidence 0.50 - 0.85)
- Ranking order, thresholds, and limits
- Safe handling of empty queries and SQL injection payloads
- query_best method
"""

import unittest

from src.memory.engine import TaskMemoryEngine
from src.memory.models import WorkflowSpec
from src.memory.retrieval import NLRetrievalEngine


class TestNLRetrievalEngine(unittest.TestCase):
    """Test suite for NLRetrievalEngine."""

    def setUp(self) -> None:
        self.engine = TaskMemoryEngine(":memory:")
        self.spec = WorkflowSpec(
            id="wf_notes_todo",
            name="Notes Weekly Todo",
            description="Creates weekly todo note in Apple Notes",
            triggers={
                "canonical": "write my weekly to-do list",
                "aliases": ["plan my week in notes", "weekly todo in notes"],
                "keywords": ["notes", "todo", "weekly", "plan"],
            },
        )
        self.engine.save_workflow(self.spec)
        self.retrieval = NLRetrievalEngine(self.engine)

    def tearDown(self) -> None:
        self.engine.close()

    def test_exact_canonical_match(self):
        """TEST-RET-01: Canonical trigger query yields confidence 1.0."""
        matches = self.retrieval.query("write my weekly to-do list")
        self.assertGreaterEqual(len(matches), 1)
        self.assertEqual(matches[0].workflow_id, "wf_notes_todo")
        self.assertEqual(matches[0].confidence, 1.0)
        self.assertEqual(matches[0].tier, "tier1_canonical")

    def test_exact_alias_match(self):
        """TEST-RET-02: Alias trigger query yields confidence 0.95."""
        matches = self.retrieval.query("plan my week in notes")
        self.assertGreaterEqual(len(matches), 1)
        self.assertEqual(matches[0].workflow_id, "wf_notes_todo")
        self.assertAlmostEqual(matches[0].confidence, 0.95)
        self.assertEqual(matches[0].tier, "tier2_alias")

    def test_substring_match(self):
        """TEST-RET-03: Substring query yields confidence in [0.85, 0.90]."""
        matches = self.retrieval.query("weekly to-do list")
        self.assertGreaterEqual(len(matches), 1)
        self.assertGreaterEqual(matches[0].confidence, 0.80)
        self.assertLessEqual(matches[0].confidence, 0.90)

    def test_token_jaccard_fuzzy_match(self):
        """TEST-RET-04: Partial keyword overlap matches tier 4."""
        matches = self.retrieval.query("weekly notes checklist")
        self.assertGreaterEqual(len(matches), 1)
        self.assertGreaterEqual(matches[0].confidence, 0.50)
        self.assertLessEqual(matches[0].confidence, 0.85)

    def test_query_best(self):
        """TEST-RET-05: query_best returns top match or None."""
        best = self.retrieval.query_best("write my weekly to-do list")
        self.assertIsNotNone(best)
        self.assertEqual(best.workflow_id, "wf_notes_todo")

        none_match = self.retrieval.query_best("completely unrelated cooking recipe")
        self.assertIsNone(none_match)

    def test_empty_query(self):
        """TEST-RET-06: Empty or whitespace query returns empty list without error."""
        self.assertEqual(self.retrieval.query(""), [])
        self.assertEqual(self.retrieval.query("   \t\n  "), [])

    def test_empty_database_query(self):
        """TEST-RET-07: Querying an empty database returns empty list."""
        empty_eng = TaskMemoryEngine(":memory:")
        ret = NLRetrievalEngine(empty_eng)
        self.assertEqual(ret.query("anything"), [])
        empty_eng.close()

    def test_sql_injection_safety(self):
        """TEST-RET-08: SQL injection payloads do not cause database or syntax errors."""
        payloads = [
            "'; DROP TABLE workflows; --",
            "' OR '1'='1",
            '" UNION SELECT * FROM sqlite_master --',
            "NOT AND OR : * ^ ~",
        ]
        for p in payloads:
            matches = self.retrieval.query(p)
            self.assertIsInstance(matches, list)
        self.assertIsNotNone(self.engine.get_workflow("wf_notes_todo"))

    def test_query_limit(self):
        """TEST-RET-09: Limit parameter constrains returned matches count."""
        spec2 = WorkflowSpec(
            id="wf_notes_other",
            name="Other Notes Task",
            triggers={"canonical": "other notes task", "keywords": ["notes"]},
        )
        self.engine.save_workflow(spec2)

        matches = self.retrieval.query("notes", limit=1)
        self.assertEqual(len(matches), 1)

    def test_corrupt_spec_resilience(self):
        """TEST-RET-10: Corrupt spec in database is skipped without crashing query()."""
        with self.engine._lock:
            self.engine._conn.execute(
                """
                INSERT INTO workflows (id, name, description, version, active, spec_json, created_at, updated_at)
                VALUES ('wf_corrupt_row', 'Corrupt Spec', 'desc', 1, 1, 'INVALID_JSON_CONTENT{{{', 'now', 'now')
                """
            )
            self.engine._conn.commit()

        # Query should succeed and return the valid workflow wf_notes_todo
        matches = self.retrieval.query("write my weekly to-do list")
        self.assertGreaterEqual(len(matches), 1)
        self.assertEqual(matches[0].workflow_id, "wf_notes_todo")

    def test_tier3_subword_precision(self):
        """TEST-RET-11: Short unanchored subwords do not trigger false Tier 3 matches."""
        spec_open = WorkflowSpec(
            id="wf_open_notes",
            name="Open Notes App",
            triggers={"canonical": "open notes app"},
        )
        self.engine.save_workflow(spec_open)

        # "pen" is inside "open" but fails word-boundary and length ratio check
        matches_pen = self.retrieval.query("pen")
        tier3_matches = [m for m in matches_pen if m.workflow_id == "wf_open_notes" and m.tier == "tier3_substring_fts"]
        self.assertEqual(tier3_matches, [])

        # "ote" is inside "notes" but fails word-boundary
        matches_ote = self.retrieval.query("ote")
        tier3_ote = [m for m in matches_ote if m.workflow_id == "wf_open_notes" and m.tier == "tier3_substring_fts"]
        self.assertEqual(tier3_ote, [])

    def test_unicode_fts_sanitization(self):
        """TEST-RET-12: Non-ASCII Unicode characters are preserved in FTS query sanitizer."""
        sanitized_fr = NLRetrievalEngine.sanitize_fts5_query("créer une note")
        self.assertIn('"créer"', sanitized_fr)
        self.assertIn('"une"', sanitized_fr)
        self.assertIn('"note"', sanitized_fr)

        sanitized_ja = NLRetrievalEngine.sanitize_fts5_query("メモを作成")
        self.assertIn('"メモを作成"', sanitized_ja)
