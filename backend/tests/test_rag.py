import unittest
import sys
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

mock_modules = {
    "app.core.celery": SimpleNamespace(CELERY_BROKER_URL="redis://localhost:6379/0"),
    "app.core.graph": MagicMock(),
    "app.core.embedder": MagicMock(),
    "neo4j": MagicMock()
}

with patch.dict(sys.modules, mock_modules):
    from app.core.rag import classify_query_intent, extract_signature, serialize_graph_topology

class GraphRagTests(unittest.TestCase):
    def test_intent_classification(self):
        self.assertEqual(classify_query_intent("How does the authentication architecture work?"), "ARCHITECTURE")
        self.assertEqual(classify_query_intent("What is the impact and blast radius if I change this function?"), "IMPACT")
        self.assertEqual(classify_query_intent("Show me the source code implementation of verify_token"), "IMPLEMENTATION")

    def test_signature_extraction(self):
        code = """def create_order(
            user_id: int,
            items: list[Item],
            total: float = 0.0
        ) -> OrderResponse:
            # Body implementation
            order = Order(user_id=user_id)
            return order
        """
        sig = extract_signature(code, "create_order")
        self.assertIn("create_order", sig)
        self.assertIn("OrderResponse", sig)
        self.assertNotIn("order = Order", sig)

    def test_topology_serialization_is_compact(self):
        hits = [
            {
                "id": "fn:1",
                "name": "login",
                "file_path": "auth/service.py",
                "code": "def login():\n    pass\n" * 50, # large body
                "docstring": "Handles user credentials."
            }
        ]
        context = [
            {
                "function_id": "fn:1",
                "function_name": "login",
                "called_by": ["AuthController.handle_post"],
                "calls": ["UserRepository.find_by_email", "verify_pwd"],
                "class_name": "AuthService"
            }
        ]
        # In ARCHITECTURE mode, no full code should be emitted
        res = serialize_graph_topology(hits, context, "ARCHITECTURE")
        self.assertIn("AuthController.handle_post ➔ login", res)
        self.assertIn("login ➔ UserRepository.find_by_email", res)
        self.assertIn("Signature:", res)
        self.assertNotIn("Implementation Slice", res)
        self.assertLess(len(res), 1000)

if __name__ == "__main__":
    unittest.main()
