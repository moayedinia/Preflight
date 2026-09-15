import unittest
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
