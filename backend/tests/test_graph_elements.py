import sys
import unittest
from unittest.mock import patch, MagicMock

# Stub neo4j before importing graph module
if "neo4j" not in sys.modules:
    sys.modules["neo4j"] = MagicMock()
from app.core.graph import get_graph_elements

class MockNode:
    def __init__(self, node_id, labels, props):
        self.labels = set(labels)
        self.props = props
        self.element_id = node_id

    def get(self, key, default=None):
        return self.props.get(key, default)

    def __iter__(self):
        return iter(self.props.items())

    def __getitem__(self, key):
        return self.props[key]

    def keys(self):
        return self.props.keys()

    def values(self):
        return self.props.values()

    def items(self):
        return self.props.items()

class MockRelationship:
    def __init__(self, rel_id, rel_type):
        self.element_id = rel_id
        self.type = rel_type

class GraphElementsTests(unittest.TestCase):
    @patch("app.core.graph.resolve_cross_references")
    @patch("app.core.graph.driver")
    def test_get_graph_elements_returns_hierarchy_and_edges(self, mock_driver, mock_resolve):
        # Mock session and query results
        mock_session = MagicMock()
        mock_driver.session.return_value.__enter__.return_value = mock_session

        repo_node = MockNode("repo:1", ["Repository"], {"name": "test-repo", "id": "repo:1"})
        file_node = MockNode("file:1", ["File"], {"path": "src/main.py", "repo": "test-repo", "id": "file:1"})
        class_node = MockNode("cls:1", ["Class"], {"name": "TestClass", "repo": "test-repo", "id": "cls:1"})
        fn_node1 = MockNode("fn:1", ["Function"], {"name": "foo", "repo": "test-repo", "id": "fn:1"})
        fn_node2 = MockNode("fn:2", ["Function"], {"name": "bar", "repo": "test-repo", "id": "fn:2"})

        rel_contains1 = MockRelationship("r:1", "CONTAINS")
        rel_contains2 = MockRelationship("r:2", "CONTAINS")
        rel_has_method = MockRelationship("r:3", "HAS_METHOD")
        rel_calls = MockRelationship("r:4", "CALLS")

        # Step 1: structural hierarchy
        struct_records = [
            {"n": repo_node, "r": rel_contains1, "m": file_node, "r_start": repo_node, "r_end": file_node},
            {"n": file_node, "r": rel_contains2, "m": class_node, "r_start": file_node, "r_end": class_node},
            {"n": class_node, "r": rel_has_method, "m": fn_node1, "r_start": class_node, "r_end": fn_node1},
        ]
        # Step 2: semantic calls
        code_records = [
            {"n": fn_node1, "r": rel_calls, "m": fn_node2, "r_start": fn_node1, "r_end": fn_node2}
        ]
        # Step 3: standalone
        standalone_records = []

        mock_session.run.side_effect = [
            struct_records,
            code_records,
            standalone_records,
        ]

        result = get_graph_elements(repo_name="test-repo", limit=450)

        self.assertIn("nodes", result)
        self.assertIn("edges", result)

        node_types = {n["type"] for n in result["nodes"]}
        self.assertIn("Repository", node_types)
        self.assertIn("File", node_types)
        self.assertIn("Class", node_types)
        self.assertIn("Function", node_types)

        edge_types = {e["type"] for e in result["edges"]}
        self.assertIn("CONTAINS", edge_types)
        self.assertIn("HAS_METHOD", edge_types)
        self.assertIn("CALLS", edge_types)

        # Ensure all edge endpoints exist in the node set
        node_ids = {n["id"] for n in result["nodes"]}
        for edge in result["edges"]:
            self.assertIn(edge["from"], node_ids)
            self.assertIn(edge["to"], node_ids)

if __name__ == "__main__":
    unittest.main()
