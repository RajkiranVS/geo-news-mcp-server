'''
API-key middleware tests. MCP_API_KEY is set via monkeypatch before server is
imported, so the module never calls Secrets Manager.
'''
import importlib
import sys
import types

import pytest
from starlette.testclient import TestClient

API_KEY = "test-key"

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "pytest", "version": "0"},
    },
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


@pytest.fixture
def client(monkeypatch):
    '''A TestClient over a freshly imported server, with MCP_API_KEY='test-key'.'''
    monkeypatch.setenv("MCP_API_KEY", API_KEY)
    sys.modules.pop("server", None)  # MCP_API_KEY is read at import time
    server = importlib.import_module("server")
    with TestClient(server.app) as c:  # context manager runs the MCP lifespan
        yield c
    sys.modules.pop("server", None)


def test_health_needs_no_key(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_mcp_without_key_is_401(client):
    resp = client.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS)
    assert resp.status_code == 401
    assert resp.json() == {"error": "unauthorized"}


def test_mcp_with_wrong_key_is_401(client):
    resp = client.post("/mcp", json=INITIALIZE, headers={**MCP_HEADERS, "x-api-key": "wrong"})
    assert resp.status_code == 401


def test_non_ascii_key_is_401_not_500(client):
    # Starlette decodes header bytes as latin-1, so this arrives as a non-ASCII str.
    resp = client.post("/mcp", json=INITIALIZE, headers={**MCP_HEADERS, "x-api-key": "tëst".encode("latin-1")})
    assert resp.status_code == 401


def test_mcp_with_key_reaches_server(client):
    resp = client.post("/mcp", json=INITIALIZE, headers={**MCP_HEADERS, "x-api-key": API_KEY})
    assert resp.status_code == 200
    assert resp.json()["result"]["serverInfo"]["name"] == "Geo Political News Feeder"


# --- Secret parsing: plaintext and JSON formats ---

@pytest.fixture
def server_module(monkeypatch):
    monkeypatch.setenv("MCP_API_KEY", API_KEY)
    sys.modules.pop("server", None)
    yield importlib.import_module("server")
    sys.modules.pop("server", None)


def test_plaintext_secret_is_used_as_is(server_module):
    assert server_module._parse_secret("plain-key") == "plain-key"


def test_json_secret_returns_api_key_field(server_module):
    assert server_module._parse_secret('{"api_key": "json-key"}') == "json-key"


def test_json_secret_without_api_key_raises(server_module):
    with pytest.raises(RuntimeError, match="api_key"):
        server_module._parse_secret('{"other": "x"}')


def test_malformed_json_secret_raises_without_leaking(server_module):
    with pytest.raises(RuntimeError) as excinfo:
        server_module._parse_secret('{"api_key": "s3cret"')
    assert "s3cret" not in str(excinfo.value)
    assert excinfo.value.__cause__ is None


def test_load_api_key_parses_secrets_manager_json(monkeypatch, server_module):
    '''
    With no env var, _load_api_key reads Secrets Manager and parses JSON.
    botocore and aws_secretsmanager_caching are stubbed, as they are Lambda-only deps.
    '''
    class FakeCache:
        def __init__(self, *args, **kwargs):
            pass

        def get_secret_string(self, name):
            assert name == "MCP_API_KEY"
            return '{"api_key": "from-sm"}'

    caching = types.ModuleType("aws_secretsmanager_caching")
    caching.SecretCache = FakeCache
    caching.SecretCacheConfig = lambda: None
    botocore = types.ModuleType("botocore")
    session = types.ModuleType("botocore.session")
    session.get_session = lambda: types.SimpleNamespace(create_client=lambda *a, **kw: None)
    botocore.session = session

    monkeypatch.setitem(sys.modules, "aws_secretsmanager_caching", caching)
    monkeypatch.setitem(sys.modules, "botocore", botocore)
    monkeypatch.setitem(sys.modules, "botocore.session", session)
    monkeypatch.delenv("MCP_API_KEY")
    assert server_module._load_api_key() == "from-sm"


# --- Tool argument validation at the MCP layer ---

def _call_get_feeds(client, arguments: dict):
    payload = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "get_feeds", "arguments": arguments},
    }
    return client.post("/mcp", json=payload, headers={**MCP_HEADERS, "x-api-key": API_KEY})


def test_limit_above_ten_is_rejected(client, mock_http):
    resp = _call_get_feeds(client, {"feed": ["bbc_world"], "limit": 11})

    body = resp.json()
    # FastMCP reports argument validation failures as a tool error result.
    assert body.get("error") or body["result"]["isError"] is True
    mock_http.calls.assert_not_called()


def test_limit_ten_is_accepted(client, mock_http):
    resp = _call_get_feeds(client, {"feed": ["bbc_world"], "limit": 10})

    result = resp.json()["result"]
    assert result["isError"] is False
    assert len(result["structuredContent"]["items"]) == 10


def test_every_feed_is_described_to_the_agent(client):
    server = sys.modules["server"]
    from feeds import FEEDS

    for name, feed in FEEDS.items():
        assert feed.about in server.GET_FEEDS_DESCRIPTION
        assert feed.about in server.FEED_PARAM_DESCRIPTION
