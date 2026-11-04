import sys
from unittest.mock import MagicMock
if 'neo4j' not in sys.modules:
    sys.modules['neo4j'] = MagicMock()
import os

os.environ["TESTING"] = "1"
os.environ["MOCK_LLM"] = "1"
os.environ["NO_PROXY"] = "*"

# Remove host environment proxy variables that cause httpx/TestClient to hang on testserver requests
for _k in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy", "GRPC_PROXY", "grpc_proxy"]:
    os.environ.pop(_k, None)
