"""
Regression tests for the pure resolution helpers behind the D4 (call-graph) and
D5 (clone detection) fixes.

D4 policy: graph.resolve_call_targets (imported from the REAL graph module; only
`neo4j` is stubbed so the driver created at import time is a no-op mock).
D5 policy: the dependency-free resolve.dedupe_rank_clone_pairs.
"""
import sys
import unittest
from unittest.mock import patch, MagicMock

# graph.py opens a Neo4j driver at import time; stub neo4j so it's a no-op.
with patch.dict(sys.modules, {"neo4j": MagicMock()}):
    from app.core.graph import resolve_call_targets, _short_call_name

from app.core.resolve import dedupe_rank_clone_pairs


class ShortCallNameTests(unittest.TestCase):
    def test_dotted(self):
        self.assertEqual(_short_call_name("utils.parse"), "parse")

    def test_parens(self):
        self.assertEqual(_short_call_name("parse("), "parse")

    def test_plain(self):
        self.assertEqual(_short_call_name("parse"), "parse")


class CallTargetResolutionTests(unittest.TestCase):
    """D4 — a call must link to at most one, unambiguously-named function."""

    def test_unique_name_resolves(self):
        self.assertEqual(resolve_call_targets("parse", {"parse": ["id:parse"]}), "id:parse")

    def test_qualified_name_resolves_to_short(self):
        self.assertEqual(resolve_call_targets("utils.parse", {"parse": ["id:parse"]}), "id:parse")

    def test_parens_stripped(self):
        self.assertEqual(resolve_call_targets("parse(", {"parse": ["id:parse"]}), "id:parse")

    def test_ambiguous_name_resolves_to_none(self):
        m = {"parse": ["id:parse_a", "id:parse_b"]}
        self.assertIsNone(resolve_call_targets("parse", m))

    def test_unknown_name_resolves_to_none(self):
        self.assertIsNone(resolve_call_targets("parse", {"other": ["id:other"]}))

    def test_empty_and_blank_names(self):
        self.assertIsNone(resolve_call_targets("", {"a": ["x"]}))
        self.assertIsNone(resolve_call_targets("   ", {"a": ["x"]}))
        self.assertIsNone(resolve_call_targets("parse", {}))


class CloneDedupeRankTests(unittest.TestCase):
    """D5 — clone pairs are de-duplicated by node pair and ranked by similarity."""

    def test_filters_below_threshold(self):
        self.assertEqual(dedupe_rank_clone_pairs([("a", "b", 0.90, "a||b")], threshold=0.95), [])

    def test_ranks_by_similarity_desc(self):
        rows = [
            ("a", "b", 0.96, "a||b"),
            ("c", "d", 0.99, "c||d"),
            ("e", "f", 0.97, "e||f"),
        ]
        out = dedupe_rank_clone_pairs(rows, threshold=0.95, limit=10)
        self.assertEqual([o["fn1"] for o in out], ["c", "e", "a"])
        self.assertEqual(out[0]["similarity"], 0.99)

    def test_dedupes_same_node_pair_different_names(self):
        # Same two nodes surfaced twice -> one row, keeping the highest similarity.
        rows = [
            ("parse", "util", 0.96, "idA||idB"),
            ("parse2", "util2", 0.98, "idA||idB"),
        ]
        out = dedupe_rank_clone_pairs(rows, threshold=0.95)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["similarity"], 0.98)

    def test_respects_limit(self):
        rows = [(f"a{i}", f"b{i}", 0.95 + i * 0.001, f"k{i}") for i in range(10)]
        out = dedupe_rank_clone_pairs(rows, threshold=0.95, limit=3)
        self.assertEqual(len(out), 3)
        self.assertEqual(out[0]["fn1"], "a9")

    def test_empty_input(self):
        self.assertEqual(dedupe_rank_clone_pairs([], threshold=0.95), [])


if __name__ == "__main__":
    unittest.main()
