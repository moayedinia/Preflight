"""
Regression tests for the NL2Cypher read-only guard (D2 security fix).

The guard lives in app.core.rag as validate_readonly_cypher. It is a pure function
(no Neo4j / LLM dependency), so we stub the heavy modules the same way test_rag.py
does and exercise the validator directly against known-bad and known-good Cypher.

These tests encode the security contract: any Cypher that could mutate, delete, or
abuse the database MUST raise ValueError, while legitimate read-only queries pass
(and get a LIMIT appended when missing).
"""
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

mock_modules = {
    "app.core.celery": SimpleNamespace(CELERY_BROKER_URL="redis://localhost:6379/0"),
    "app.core.graph": MagicMock(),
    "app.core.embedder": MagicMock(),
    "neo4j": MagicMock(),
}

with patch.dict(sys.modules, mock_modules):
    from app.core.rag import validate_readonly_cypher


class Nl2CypherGuardTests(unittest.TestCase):
    def _assert_blocked(self, cypher):
        with self.assertRaises(ValueError):
            validate_readonly_cypher(cypher)

    def test_blocks_delete(self):
        self._assert_blocked("MATCH (n) DETACH DELETE n")
        self._assert_blocked("MATCH (n)\nMATCH (m) DELETE n")

    def test_blocks_set(self):
        self._assert_blocked("MATCH (n) SET n.x = 1")

    def test_blocks_create_merge_remove(self):
        self._assert_blocked("MATCH (n) CREATE (n)-[:X]->(m)")
        self._assert_blocked("CREATE (n:File)")
        self._assert_blocked("MATCH (n) MERGE (n)")
        self._assert_blocked("MATCH (n) REMOVE n.prop")

    def test_blocks_dangerous_procedures(self):
        self._assert_blocked("MATCH (n) CALL db.killServer()")
        self._assert_blocked("MATCH (n) CALL db.reset()")
        self._assert_blocked("MATCH (n) CALL apoc.load.json('http://x/y')")

    def test_blocks_load_and_drop(self):
        self._assert_blocked("MATCH (n) LOAD CSV FROM 'http://x/y' AS l")
        self._assert_blocked("DROP INDEX function_embeddings")

    def test_blocks_multiple_statements(self):
        self._assert_blocked("MATCH (n) RETURN n; DROP INDEX foo")
        self._assert_blocked("MATCH (n) RETURN n ; MATCH (m) DELETE m")

    def test_blocks_non_read_start(self):
        self._assert_blocked("CREATE (n:File)")
        self._assert_blocked("")  # empty

    def test_allows_simple_read(self):
        out = validate_readonly_cypher("MATCH (fn:Function) WHERE fn.repo='x' RETURN fn.name LIMIT 10")
        self.assertIn("RETURN fn.name", out)

    def test_appends_limit_when_missing(self):
        out = validate_readonly_cypher("MATCH (f:File) RETURN f.path")
        self.assertTrue(out.lower().endswith("limit 50"), out)

    def test_allows_read_with_relationships(self):
        out = validate_readonly_cypher("MATCH (a:File)-[:IMPORTS]->(m) RETURN a.path, m.name")
        self.assertIn("a.path", out)
        self.assertIn("LIMIT", out)

    def test_strips_trailing_semicolon_on_single_read(self):
        out = validate_readonly_cypher("MATCH (n) RETURN n.id;")
        self.assertNotIn(";", out)
        self.assertIn("LIMIT", out)


if __name__ == "__main__":
    unittest.main()
