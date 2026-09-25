"""Production 4-tier hybrid natural language retrieval engine for task workflows.

Belongs to FEAT-MEM-04 (NLRetrievalEngine).
Tiers:
  1. Exact canonical trigger match (confidence: 1.0)
  2. Exact alias match (confidence: 0.95)
  3. Substring & SQLite FTS5 BM25 match with rank scoring (confidence: 0.85 - 0.90)
  4. Token overlap (Jaccard similarity) & keyword fuzzy matching (confidence: 0.50 - 0.85)

All confidence scores are normalized to [0.0, 1.0] and filtered by min_confidence threshold.
Zero third-party dependencies (difflib, re, sqlite3 standard library).
"""

from __future__ import annotations

import difflib
import logging
import math
import re
import sqlite3
from typing import Any, Dict, List, Optional, Set, Tuple

from src.memory.models import MatchResult, WorkflowSpec

logger = logging.getLogger(__name__)


class NLRetrievalEngine:
    """Production-grade 4-tier hybrid natural language retrieval engine for task workflows."""

    def __init__(self, memory_engine: Any, min_confidence: float = 0.50):
        """Initialize the retrieval engine with a TaskMemoryEngine instance.

        Args:
            memory_engine: An instance of TaskMemoryEngine.
            min_confidence: Minimum confidence threshold [0.0, 1.0] to accept a match.
        """
        self.memory = memory_engine
        self.min_confidence = max(0.0, min(1.0, float(min_confidence)))

    @staticmethod
    def normalize_utterance(text: str) -> str:
        """Sanitize and normalize user utterance or trigger text.

        Collapses whitespace, strips outer punctuation and quotes, and lowercases.
        """
        if not isinstance(text, str):
            return ""
        if not text:
            return ""
        cleaned = text.strip().lower()
        # Collapse multiple spaces, tabs, newlines
        cleaned = re.sub(r"\s+", " ", cleaned)
        # Strip trailing/leading punctuation often attached in dialogue (e.g. "?", "!", ".")
        cleaned = re.sub(r"^[^\w\s]+|[^\w\s]+$", "", cleaned).strip()
        return cleaned

    @staticmethod
    def sanitize_fts5_query(query: str) -> str:
        """Sanitize raw natural language into a safe SQLite FTS5 MATCH expression.

        Prevents SQLite OperationalError on syntax operators like quotes, asterisks,
        OR, AND, NOT, NEAR, semicolons, and SQL injection strings.
        """
        tokens = re.findall(r"[\w]+", query, re.UNICODE)
        if not tokens:
            return ""
        # Double-quote each alphanumeric token to guarantee literal phrase/term evaluation
        return " ".join(f'"{t}"' for t in tokens)

    def _query_fts5(self, query: str) -> Dict[str, float]:
        """Execute FTS5 BM25 search on memory database if available.

        Returns a dictionary mapping workflow_id to normalized BM25 score in [0.85, 0.90].
        """
        fts_query = self.sanitize_fts5_query(query)
        if not fts_query:
            return {}

        conn = getattr(self.memory, "_conn", None)
        if not conn:
            return {}

        results: Dict[str, float] = {}
        try:
            lock = getattr(self.memory, "_lock", None)
            if lock:
                with lock:
                    cur = conn.cursor()
                    cur.execute(
                        """
                        SELECT workflow_id, bm25(workflow_fts, 5.0, 10.0, 2.0, 15.0) AS bm25_rank
                        FROM workflow_fts
                        WHERE workflow_fts MATCH ?
                        ORDER BY bm25_rank ASC
                        LIMIT 20
                        """,
                        (fts_query,),
                    )
                    rows = cur.fetchall()
            else:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT workflow_id, bm25(workflow_fts, 5.0, 10.0, 2.0, 15.0) AS bm25_rank
                    FROM workflow_fts
                    WHERE workflow_fts MATCH ?
                    ORDER BY bm25_rank ASC
                    LIMIT 20
                    """,
                    (fts_query,),
                )
                rows = cur.fetchall()

            for row in rows:
                try:
                    wf_id = row["workflow_id"] if isinstance(row, sqlite3.Row) else row[0]
                    raw_rank = row["bm25_rank"] if isinstance(row, sqlite3.Row) else row[1]
                    abs_rank = abs(float(raw_rank))
                    norm_score = 0.85 + 0.05 * (abs_rank / (abs_rank + 1.0))
                    results[wf_id] = round(norm_score, 4)
                except Exception:
                    continue
        except (sqlite3.OperationalError, sqlite3.DatabaseError) as ex:
            # FTS table does not exist or query failed; fail gracefully to other tiers
            logger.debug("FTS5 query skipped or unavailable: %s", ex)
            return {}
        except Exception as ex:
            logger.debug("FTS5 query failed: %s", ex)
            return {}

        return results

    def _match_tier1_canonical(
        self, norm_utterance: str, canonical: str
    ) -> Optional[Tuple[float, str]]:
        """Tier 1: Exact canonical trigger match (confidence: 1.0)."""
        if not canonical:
            return None
        if norm_utterance == canonical:
            return 1.0, canonical
        return None

    def _match_tier2_alias(
        self, norm_utterance: str, aliases: List[str]
    ) -> Optional[Tuple[float, str]]:
        """Tier 2: Exact alias trigger match (confidence: 0.95)."""
        for alias in aliases:
            if norm_utterance == alias:
                return 0.95, alias
        return None

    def _match_tier3_substring_and_fts(
        self,
        norm_utterance: str,
        canonical: str,
        aliases: List[str],
        fts_score: Optional[float],
    ) -> Optional[Tuple[float, str]]:
        """Tier 3: Substring & FTS5 BM25 match with rank scoring (confidence: 0.85 - 0.90)."""
        sub_candidates: List[Tuple[float, str]] = []

        # Substring match on canonical
        if canonical and len(norm_utterance) >= 3:
            if canonical in norm_utterance:
                length_disparity = min(1.0, len(canonical) / len(norm_utterance))
                score = round(0.85 + 0.05 * length_disparity, 3)
                score = min(0.90, max(0.85, score))
                sub_candidates.append((score, canonical))
            elif re.search(r"\b" + re.escape(norm_utterance) + r"\b", canonical):
                length_disparity = min(1.0, len(norm_utterance) / len(canonical))
                if length_disparity >= 0.50:
                    score = round(0.85 + 0.05 * length_disparity, 3)
                    score = min(0.90, max(0.85, score))
                    sub_candidates.append((score, canonical))

        # Substring match on aliases
        for alias in aliases:
            if alias and len(norm_utterance) >= 3:
                if alias in norm_utterance:
                    length_disparity = min(1.0, len(alias) / len(norm_utterance))
                    score = round(0.85 + 0.04 * length_disparity, 3)
                    score = min(0.89, max(0.85, score))
                    sub_candidates.append((score, alias))
                elif re.search(r"\b" + re.escape(norm_utterance) + r"\b", alias):
                    length_disparity = min(1.0, len(norm_utterance) / len(alias))
                    if length_disparity >= 0.50:
                        score = round(0.85 + 0.04 * length_disparity, 3)
                        score = min(0.89, max(0.85, score))
                        sub_candidates.append((score, alias))

        best_sub_score = max((s for s, _ in sub_candidates), default=0.0)
        best_sub_trigger = next((t for s, t in sub_candidates if s == best_sub_score), canonical)

        # Merge with FTS5 BM25 rank score if available
        if fts_score is not None:
            if fts_score > best_sub_score:
                return fts_score, f"fts5_bm25:{fts_score:.3f}"

        if best_sub_score >= 0.85:
            return best_sub_score, best_sub_trigger

        return None

    def _match_tier4_token_fuzzy_keyword(
        self,
        norm_utterance: str,
        canonical: str,
        aliases: List[str],
        keywords: List[str],
    ) -> Optional[Tuple[float, str]]:
        """Tier 4: Token overlap (Jaccard similarity) & keyword fuzzy matching (confidence: 0.50 - 0.85)."""
        CONVERSATIONAL_STOPWORDS = {
            "could", "you", "please", "kindly", "help", "me", "to",
            "the", "a", "an", "can", "i", "want", "would", "like",
        }
        raw_tokens_u = set(re.findall(r"\w+", norm_utterance))
        tokens_u = {t for t in raw_tokens_u if t not in CONVERSATIONAL_STOPWORDS}
        if not tokens_u:
            tokens_u = raw_tokens_u
        if not tokens_u:
            return None

        # 4a. Token Overlap (Jaccard Similarity) against canonical and aliases
        all_targets = [canonical] + aliases
        best_jaccard = 0.0
        best_jaccard_target = canonical

        for target in all_targets:
            if not target:
                continue
            raw_tokens_t = set(re.findall(r"\w+", target))
            tokens_t = {t for t in raw_tokens_t if t not in CONVERSATIONAL_STOPWORDS} or raw_tokens_t
            if not tokens_t:
                continue
            intersection = tokens_u.intersection(tokens_t)
            union = tokens_u.union(tokens_t)
            jaccard = len(intersection) / len(union) if union else 0.0
            if jaccard > best_jaccard:
                best_jaccard = jaccard
                best_jaccard_target = target

        jaccard_score = 0.0
        if best_jaccard >= 0.30:
            # Scale Jaccard from [0.3, 1.0] into [0.50, 0.85]
            jaccard_score = min(0.85, 0.50 + (best_jaccard * 0.35))

        # 4b. Fuzzy Sequence Matching (Levenshtein / Gestalt Pattern Matching)
        best_ratio = 0.0
        for target in all_targets:
            if not target:
                continue
            ratio = difflib.SequenceMatcher(None, norm_utterance, target).ratio()
            if ratio > best_ratio:
                best_ratio = ratio

        fuzzy_score = 0.0
        if best_ratio >= 0.60:
            # Scale [0.60, 1.0] into [0.50, 0.85]
            fuzzy_score = min(0.85, 0.50 + ((best_ratio - 0.60) / 0.40) * 0.35)

        # 4c. Keyword Matching
        matching_kw = [
            k for k in keywords if (k in norm_utterance or k in tokens_u)
        ]
        kw_score = 0.0
        if matching_kw:
            kw_coverage = len(matching_kw) / max(1, len(keywords))
            kw_score = min(0.80, 0.45 + (kw_coverage * 0.35))

        # Best Tier 4 score
        best_score = max(jaccard_score, fuzzy_score, kw_score)
        if best_score >= 0.50:
            best_score = min(0.85, round(best_score, 3))
            if best_score == jaccard_score:
                matched_label = f"jaccard:{best_jaccard_target}"
            elif best_score == fuzzy_score:
                matched_label = f"fuzzy:{canonical}"
            else:
                matched_label = f"keywords:{','.join(matching_kw)}"
            return best_score, matched_label

        return None

    def query(self, utterance: str, limit: Optional[int] = None) -> List[MatchResult]:
        """Query task workflows using the 4-tier hybrid retrieval pipeline.

        Args:
            utterance: Natural language user query.
            limit: Optional maximum number of matches to return.

        Returns:
            List of MatchResult sorted descending by confidence and tier.
        """
        cleaned = self.normalize_utterance(utterance)
        if not cleaned:
            return []

        # Fetch active workflows from memory store
        try:
            workflows = self.memory.list_workflows()
        except Exception as ex:
            logger.error("Failed to list workflows from memory: %s", ex)
            return []

        if not workflows:
            return []

        # Pre-query FTS5 BM25 index if available
        fts_scores = self._query_fts5(cleaned)

        tier_weights = {
            "tier1_canonical": 4,
            "tier2_alias": 3,
            "tier3_substring_fts": 2,
            "tier4_token_fuzzy": 1,
        }

        results: List[MatchResult] = []

        for wf_meta in workflows:
            wf_id = wf_meta["id"]
            try:
                spec = self.memory.get_workflow(wf_id)
                if not spec:
                    continue

                triggers = getattr(spec, "triggers", {}) or {}
                if not isinstance(triggers, dict):
                    triggers = {}
                canonical = self.normalize_utterance(triggers.get("canonical", ""))
                aliases_raw = triggers.get("aliases", [])
                if not isinstance(aliases_raw, (list, tuple)):
                    aliases_raw = []
                aliases = [
                    self.normalize_utterance(a)
                    for a in aliases_raw
                    if self.normalize_utterance(a)
                ]
                keywords_raw = triggers.get("keywords", [])
                if not isinstance(keywords_raw, (list, tuple)):
                    keywords_raw = []
                keywords = [
                    self.normalize_utterance(k)
                    for k in keywords_raw
                    if self.normalize_utterance(k)
                ]

                match_found: Optional[Tuple[float, str, str]] = None

                # 1. Tier 1: Exact canonical match (confidence 1.0)
                t1 = self._match_tier1_canonical(cleaned, canonical)
                if t1:
                    match_found = (t1[0], t1[1], "tier1_canonical")
                else:
                    # 2. Tier 2: Exact alias match (confidence 0.95)
                    t2 = self._match_tier2_alias(cleaned, aliases)
                    if t2:
                        match_found = (t2[0], t2[1], "tier2_alias")
                    else:
                        # 3. Tier 3: Substring & FTS5 BM25 match (confidence 0.85 - 0.90)
                        fts_score = fts_scores.get(spec.id)
                        t3 = self._match_tier3_substring_and_fts(cleaned, canonical, aliases, fts_score)
                        if t3:
                            match_found = (t3[0], t3[1], "tier3_substring_fts")
                        else:
                            # 4. Tier 4: Token overlap & keyword fuzzy matching (confidence 0.50 - 0.85)
                            t4 = self._match_tier4_token_fuzzy_keyword(cleaned, canonical, aliases, keywords)
                            if t4:
                                match_found = (t4[0], t4[1], "tier4_token_fuzzy")

                if match_found:
                    confidence, trigger_name, tier_name = match_found
                    norm_conf = max(0.0, min(1.0, float(confidence)))
                    if norm_conf >= self.min_confidence:
                        results.append(
                            MatchResult(
                                workflow_id=spec.id,
                                workflow_name=spec.name,
                                confidence=norm_conf,
                                matched_trigger=trigger_name,
                                spec=spec,
                                tier=tier_name,
                                score_breakdown={
                                    "tier_priority": float(tier_weights.get(tier_name, 0)),
                                    "raw_confidence": norm_conf,
                                },
                            )
                        )
            except Exception as ex:
                logger.warning("Skipping workflow %s during retrieval due to error: %s", wf_id, ex)
                continue

        # Sort descending by confidence, breaking ties by tier priority and recency
        results.sort(
            key=lambda r: (
                r.confidence,
                tier_weights.get(r.tier, 0),
                getattr(r.spec, "updated_at", ""),
            ),
            reverse=True,
        )

        if limit is not None and limit > 0:
            return results[:limit]
        return results

    def query_best(self, utterance: str) -> Optional[MatchResult]:
        """Convenience method returning the highest confidence match, or None."""
        matches = self.query(utterance, limit=1)
        return matches[0] if matches else None
