'''
API-key middleware tests. MCP_API_KEY is set via monkeypatch before server is
imported, so the module never calls Secrets Manager.
'''
import importlib
import sys

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
