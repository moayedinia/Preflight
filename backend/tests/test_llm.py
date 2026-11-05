import unittest
import sys
import os
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

sys.modules.setdefault(
    "app.core.celery",
    SimpleNamespace(CELERY_BROKER_URL="redis://localhost:6379/0"),
)
from app.core.llm import normalize_llm_base_url, _clean_slashes, verify_llm_connection, set_llm_config


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

    @patch.dict(os.environ, {"TESTING": "", "MOCK_LLM": ""})
    @patch("app.core.llm.execute_llm_http")
    def test_verify_llm_connection_success_with_valid_model(self, mock_http):
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.status_code = 200
        mock_http.return_value = mock_resp

        set_llm_config("https://api.openai.com/v1", "gpt-4o-mini", "sk-test-valid-key")
        is_conn, msg = verify_llm_connection(model="gpt-4o-mini")
        self.assertTrue(is_conn)
        self.assertIn("gpt-4o-mini", msg)

    @patch.dict(os.environ, {"TESTING": "", "MOCK_LLM": ""})
    @patch("app.core.llm.execute_llm_http")
    def test_verify_llm_connection_fails_on_bogus_model(self, mock_http):
        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_resp.status_code = 404
        mock_resp.json.return_value = {
            "error": {"message": "The model `fake-model-123` does not exist"}
        }
        mock_http.return_value = mock_resp

        set_llm_config("https://api.openai.com/v1", "fake-model-123", "sk-test-key")
        is_conn, msg = verify_llm_connection(model="fake-model-123")
        self.assertFalse(is_conn)
        self.assertIn("fake-model-123", msg)
        self.assertIn("does not exist", msg)


    def test_extract_content_when_content_empty_salvages_code_from_reasoning(self):
        from app.core.llm import _extract_content_from_response
        import requests

        mock_resp = MagicMock(spec=requests.Response)
        mock_resp.text = '''{"id":"chatcmpl-123","choices":[{"index":0,"message":{"role":"assistant","content":"","reasoning":"We need answer only code block. Need fix defect\n\n```python\ndef fixed():\n    return True\n```"}}}'''
        mock_resp.json.return_value = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "reasoning": "We need answer only code block. Need fix defect\n\n```python\ndef fixed():\n    return True\n```"
                    }
                }
            ]
        }
        extracted = _extract_content_from_response(mock_resp)
        self.assertIn("def fixed():", extracted)
        self.assertNotIn("We need answer only code block", extracted)

    def test_extract_content_prefers_content_over_reasoning_when_content_present(self):
        from app.core.llm import _extract_content_from_response
        import requests

        mock_resp = MagicMock(spec=requests.Response)
        mock_resp.text = '''{"id":"chatcmpl-456","choices":[{"index":0,"message":{"role":"assistant","content":"<think>thinking</think>Actual answer here.","reasoning":"Internal reasoning"}}}'''
        mock_resp.json.return_value = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "<think>thinking</think>Actual answer here.",
                        "reasoning": "Internal reasoning"
                    }
                }
            ]
        }
        extracted = _extract_content_from_response(mock_resp)
        self.assertEqual(extracted, "Actual answer here.")
        self.assertNotIn("Internal reasoning", extracted)

if __name__ == "__main__":
    unittest.main()
