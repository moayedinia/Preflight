import sys
import unittest
from unittest.mock import patch, MagicMock

if "redis" not in sys.modules:
    sys.modules["redis"] = MagicMock()
if "neo4j" not in sys.modules:
    sys.modules["neo4j"] = MagicMock()

from app.core import embedder


class EmbedderConfigurationTests(unittest.TestCase):
    def test_vllm_base_url_ensures_v1_suffix(self):
        self.assertEqual(
            embedder.normalize_vllm_base_url("http://vllm.example:8000"),
            "http://vllm.example:8000/v1",
        )
        self.assertEqual(
            embedder.normalize_vllm_base_url("http://vllm.example:8000/v1/"),
            "http://vllm.example:8000/v1",
        )

    def test_invalid_vllm_url_is_rejected(self):
        with self.assertRaises(ValueError):
            embedder.normalize_vllm_base_url("vllm.example:8000")

    def test_reranker_is_rejected_before_calling_vllm(self):
        with self.assertRaisesRegex(embedder.EmbeddingError, "reranker, not an embedding model"):
            embedder.validate_embedding_model_name("jina-reranker-v2:latest")

    def test_connection_failure_does_not_generate_fake_vectors_by_default(self):
        with (
            patch.object(embedder, "get_active_vllm_url", return_value="http://vllm:8000/v1"),
            patch.object(embedder, "ALLOW_FAKE_EMBEDDINGS", False),
            patch.object(embedder, "_query_vllm", return_value=([], "connection refused")),
        ):
            with self.assertRaisesRegex(embedder.EmbeddingError, "connection refused"):
                embedder.get_embedding("def example(): pass")

    def test_dimension_mismatch_is_rejected(self):
        with (
            patch.object(embedder, "get_active_vllm_url", return_value="http://vllm:8000/v1"),
            patch.object(embedder, "EMBEDDING_DIM", 3),
            patch.object(embedder, "_query_vllm", return_value=([0.1, 0.2], "")),
        ):
            with self.assertRaisesRegex(embedder.EmbeddingError, "dimension mismatch"):
                embedder.get_embedding("code")

    def test_get_and_set_embedding_config(self):
        cfg = embedder.set_embedding_config(
            vllm_host="http://custom-vllm:8000",
            model="jinaai/jina-embeddings-v5-text-small-retrieval",
            dimension=512,
            allow_fallback=True
        )
        self.assertEqual(cfg["vllm_host"], "http://custom-vllm:8000/v1")
        self.assertEqual(cfg["model"], "jinaai/jina-embeddings-v5-text-small-retrieval")
        self.assertEqual(cfg["dimension"], 512)
        self.assertTrue(cfg["allow_fallback"])

        fetched = embedder.get_embedding_config()
        self.assertEqual(fetched["vllm_host"], "http://custom-vllm:8000/v1")
        self.assertEqual(fetched["model"], "jinaai/jina-embeddings-v5-text-small-retrieval")

    def test_test_vllm_embedding_connection_success(self):
        with (
            patch.object(embedder, "list_vllm_models", return_value=["jinaai/jina-embeddings-v5-text-small-retrieval"]),
            patch.object(embedder, "_query_vllm", return_value=([0.1] * 512, "")),
        ):
            result = embedder.test_vllm_embedding_connection("http://vllm:8000/v1", "jinaai/jina-embeddings-v5-text-small-retrieval")
            self.assertEqual(result["status"], "connected")
            self.assertTrue(result["model_present"])
            self.assertEqual(result["detected_dimension"], 512)

    def test_test_vllm_embedding_connection_failure(self):
        with patch.object(embedder, "list_vllm_models", return_value=[]):
            result = embedder.test_vllm_embedding_connection("http://unreachable:8005/v1", "jinaai/jina-embeddings-v5-text-small-retrieval")
            self.assertEqual(result["status"], "offline")

    def test_reembed_graph_entities(self):
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
            patch.object(embedder, "get_embedding", return_value=[0.1] * 512)
        ):
            res = embedder.reembed_graph_entities(repo_name="test_repo", force=True)
            self.assertEqual(res["status"], "success")
            self.assertEqual(res["embedded_successfully"], 2)
            self.assertEqual(res["errors"], 0)


if __name__ == "__main__":
    unittest.main()
