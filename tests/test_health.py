"""Smoke test for the application shell, without models or network."""
from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PROFILE", "development")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_ENABLED", "false")
    monkeypatch.setenv("ML_ENABLED", "false")

    from navigator.config import get_settings

    get_settings.cache_clear()
    from navigator.main import create_app

    app = create_app(synthetic_fallback=True)
    with TestClient(app) as test_client:
        yield test_client
    get_settings.cache_clear()


def test_health_reports_profile_and_limited_mode(client):
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["profile"] == "development"
    assert data["status"] in {"ok", "limited", "degraded"}
    assert data["ml"]["status"] == "disabled"
    assert data["llm"]["status"] == "disabled"
    assert data["reasons"]


def test_pages_render_without_model_services(client):
    for path in ("/", "/documents"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert "text/html" in response.headers["content-type"]
        assert "Altropos" in response.text
    health = client.get("/health")
    assert "application/json" in health.headers["content-type"]


@pytest.mark.asyncio
@pytest.mark.parametrize("response_delay", [0, 2.0])
async def test_local_model_probe_ignores_unrelated_proxy(monkeypatch, response_delay):
    from navigator.ui.health import _probe

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            # A reachable remote ML service may exceed the former 1.5 s probe.
            time.sleep(response_delay)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ready"}')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    try:
        result = await _probe(f"http://127.0.0.1:{server.server_port}/health")
        assert result == {"status": "ok", "http_status": 200}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
