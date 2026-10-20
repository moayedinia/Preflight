import sys
import unittest
from unittest.mock import patch, MagicMock

if "redis" not in sys.modules:
    sys.modules["redis"] = MagicMock()
if "neo4j" not in sys.modules:
    sys.modules["neo4j"] = MagicMock()

from app.core import embedder


class EmbedderConfigurationTests(unittest.TestCase):
    def test_ollama_base_url_strips_openai_suffix(self):
        self.assertEqual(
            embedder.normalize_ollama_base_url("http://ollama.example:11434/v1/"),
            "http://ollama.example:11434",
        )

    def test_invalid_ollama_url_is_rejected(self):
        with self.assertRaises(ValueError):
            embedder.normalize_ollama_base_url("ollama.example:11434")

    def test_reranker_is_rejected_before_calling_ollama(self):
        with self.assertRaisesRegex(embedder.EmbeddingError, "reranker, not an embedding model"):
            embedder.validate_embedding_model_name("jina-reranker-v2:latest")

    def test_connection_failure_does_not_generate_fake_vectors_by_default(self):
        with (
            patch.object(embedder, "OLLAMA_BASE_URL", "http://ollama:11434"),
            patch.object(embedder, "ALLOW_FAKE_EMBEDDINGS", False),
            patch.object(embedder, "_query_ollama", return_value=([], "connection refused")),
        ):
            with self.assertRaisesRegex(embedder.EmbeddingError, "connection refused"):
                embedder.get_embedding("def example(): pass")

    def test_dimension_mismatch_is_rejected(self):
        with (
            patch.object(embedder, "OLLAMA_BASE_URL", "http://ollama:11434"),
            patch.object(embedder, "EMBEDDING_DIM", 3),
            patch.object(embedder, "_query_ollama", return_value=([0.1, 0.2], "")),
        ):
            with self.assertRaisesRegex(embedder.EmbeddingError, "dimension mismatch"):
                embedder.get_embedding("code")

    def test_get_and_set_embedding_config(self):
        cfg = embedder.set_embedding_config(
            ollama_host="http://custom-ollama:11434",
            model="test-embed-model",
            dimension=512,
            allow_fallback=True
        )
        self.assertEqual(cfg["ollama_host"], "http://custom-ollama:11434")
        self.assertEqual(cfg["model"], "test-embed-model")
        self.assertEqual(cfg["dimension"], 512)
        self.assertTrue(cfg["allow_fallback"])

        fetched = embedder.get_embedding_config()
        self.assertEqual(fetched["ollama_host"], "http://custom-ollama:11434")
        self.assertEqual(fetched["model"], "test-embed-model")

    def test_test_ollama_embedding_connection_success(self):
        with (
            patch.object(embedder, "list_ollama_models", return_value=["test-model", "qwen:0.5b"]),
            patch.object(embedder, "_query_ollama", return_value=([0.1] * 768, "")),
        ):
            result = embedder.test_ollama_embedding_connection("http://ollama:11434", "test-model")
            self.assertEqual(result["status"], "connected")
            self.assertTrue(result["model_present"])
            self.assertEqual(result["detected_dimension"], 768)

    def test_test_ollama_embedding_connection_failure(self):
        with patch.object(embedder, "list_ollama_models", return_value=[]):
            result = embedder.test_ollama_embedding_connection("http://unreachable:11434", "test-model")
            self.assertEqual(result["status"], "offline")

    def test_pull_ollama_model_action(self):
        with patch.object(embedder, "pull_ollama_model", return_value=True):
            result = embedder.pull_ollama_model_action("http://ollama:11434", "test-model")
            self.assertEqual(result["status"], "success")

    def test_reembed_graph_entities(self):
        from unittest.mock import MagicMock
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_driver.session.return_value.__enter__.return_value = mock_session
        
        # 1 function, 1 class
        mock_session.run.side_effect = [
            [{"id": "fn1", "name": "foo", "code": "def foo(): pass", "file_path": "a.py"}], # functions
            [{"id": "cls1", "name": "Bar", "file_path": "b.py"}], # classes
            MagicMock(), # update fn
            MagicMock(), # update cls
        ]
        
        with (
            patch("app.core.graph.driver", mock_driver),
            patch.object(embedder, "get_embedding", return_value=[0.1] * 768)
        ):
            res = embedder.reembed_graph_entities(repo_name="test_repo", force=True)
            self.assertEqual(res["status"], "success")
            self.assertEqual(res["embedded_successfully"], 2)
            self.assertEqual(res["errors"], 0)


if __name__ == "__main__":
    unittest.main()
