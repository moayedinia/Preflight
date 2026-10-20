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
    def test_adds_openai_suffix_for_vllm_root(self):
        self.assertEqual(
            normalize_llm_base_url("http://vllm.example:8007"),
            "http://vllm.example:8007/v1",
        )

    def test_preserves_openai_suffix(self):
        self.assertEqual(
            normalize_llm_base_url("http://vllm.example:8007/v1/"),
            "http://vllm.example:8007/v1",
        )

    def test_cleans_double_slashes(self):
        self.assertEqual(
            normalize_llm_base_url("http://172.25.47.2:8007//v1/"),
            "http://172.25.47.2:8007/v1",
        )
        self.assertEqual(
            normalize_llm_base_url("http://172.25.47.2:8007//"),
            "http://172.25.47.2:8007/v1",
        )

    def test_rejects_relative_url(self):
        with self.assertRaises(ValueError):
            normalize_llm_base_url("localhost:8007")


if __name__ == "__main__":
    unittest.main()
