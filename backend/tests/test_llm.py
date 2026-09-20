import unittest
import sys
from types import SimpleNamespace
from unittest.mock import patch

with patch.dict(
    sys.modules,
    {"app.core.celery": SimpleNamespace(CELERY_BROKER_URL="redis://localhost:6379/0")},
):
    from app.core.llm import normalize_llm_base_url, _clean_slashes


class LlmConfigurationTests(unittest.TestCase):
    def test_adds_openai_suffix_for_ollama_root(self):
        self.assertEqual(
            normalize_llm_base_url("http://ollama.example:11434"),
            "http://ollama.example:11434/v1",
        )

    def test_preserves_openai_suffix(self):
        self.assertEqual(
            normalize_llm_base_url("http://ollama.example:11434/v1/"),
            "http://ollama.example:11434/v1",
        )

    def test_cleans_double_slashes(self):
        self.assertEqual(
            normalize_llm_base_url("http://172.25.47.2:8080//v1/"),
            "http://172.25.47.2:8080/v1",
        )
        self.assertEqual(
            normalize_llm_base_url("http://172.25.47.2:8080//"),
            "http://172.25.47.2:8080/v1",
        )

    def test_rejects_relative_url(self):
        with self.assertRaises(ValueError):
            normalize_llm_base_url("localhost:11434")


if __name__ == "__main__":
    unittest.main()
