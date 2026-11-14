"""Boundary tests: public readers cannot write or cross-origin ask."""
import asyncio
import pytest
from scripts.deploy.gateway import ReaderGateway


async def backend(scope, receive, send):
    if scope["method"] == "POST":
        await receive()
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def request(path, method="GET", headers=(), body=b"{}"):
    async def run():
        result = []
        async def receive():
            return {"type": "http.request", "body": body}
        async def send(event):
            result.append(event)
        await ReaderGateway(backend, "https://aitropos.ru")(
            {"type": "http", "method": method, "path": path, "headers": headers}, receive, send)
        return result[0]["status"]
    return asyncio.run(run())


@pytest.mark.parametrize("path,method", [
    ("/review", "GET"), ("/review-files/private.html", "GET"),
    ("/api/documents", "POST"),
    ("/api/documents/id/metadata", "PATCH"),
    ("/api/documents/id/review", "GET"), ("/api/documents/id/review", "POST"),
    ("/api/documents/id/reocr", "POST"), ("/api/documents/id/retry", "POST"),
    ("/api/import-url", "POST"),
    ("/api/jobs", "GET"), ("/api/jobs/job-1", "GET"),
    ("/api/ask/active", "GET"), ("/api/ask/active", "HEAD"),
    ("/api/ask/status/req-123", "GET"), ("/api/ask/status/req-123", "HEAD"),
    ("/openapi.json", "GET"),
    ("/api/sources/../documents", "GET"), ("/static/%2e%2e/secret", "GET"),
    ("/api/future-admin-route", "DELETE")])
def test_private_routes_denied(path, method):
    assert request(path, method) == 403


@pytest.mark.parametrize("path", [
    "/",
    "/documents",
    "/favicon.ico",
    "/static/app.js",
    "/api/documents",
    "/api/documents/id/original",
    "/api/documents/id/view",
    "/api/documents/id/original-info",
    "/api/documents/id/original-page/1",
    "/api/sources/id-123",
    "/api/sources/id-123/highlight",
])
def test_reader_routes(path):
    assert request(path) == 200


def test_reader_pages_hide_admin_controls(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from navigator.main import create_app

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    app = create_app(synthetic_fallback=True)
    with TestClient(app) as admin:
        html = admin.get("/documents").text
        assert 'href="/review"' in html and 'id="import-form"' in html
        reader = TestClient(ReaderGateway(app, "https://aitropos.ru"))
        response = reader.get("/documents", headers={"altropos.reader": "False"})
        assert response.status_code == 200
        assert 'id="documents-list"' in response.text
        for element in ('href="/review"', 'id="import-form"', 'document-card__review'):
            assert element not in response.text
        blocked = reader.get("/review")
        assert blocked.status_code == 403
        assert blocked.json()["error"]["code"] == "admin_required"


def test_csrf_and_body_bound():
    valid = [(b"origin", b"https://aitropos.ru"), (b"content-type", b"application/json")]
    assert request("/api/ask", "POST", valid) == 200
    assert request("/api/search", "POST", valid) == 200
    assert request("/api/ask", "POST", [(b"origin", b"https://www.aitropos.ru"), valid[1]]) == 200
    assert request("/api/ask", "POST", [
        (b"host", b"127.0.0.1:18000"), (b"origin", b"http://127.0.0.1:18000"), valid[1],
    ]) == 200
    assert request("/api/ask", "POST") == 403
    assert request("/api/ask", "POST", [(b"origin", b"https://evil.test"), valid[1]]) == 403
    assert request("/api/ask", "POST", valid + [valid[0]]) == 403
    assert request("/api/ask", "POST", [valid[0], (b"content-type", b"text/plain")]) == 403
    assert request("/api/ask", "POST", valid, b"x" * 32769) == 413


def test_parallel_ask_rejected():
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(scope, receive, send):
            entered.set()
            await release.wait()
            await backend(scope, receive, send)
        gate = ReaderGateway(slow, "https://aitropos.ru")
        scope = {"type": "http", "method": "POST", "path": "/api/ask", "headers": [
            (b"origin", b"https://aitropos.ru"), (b"content-type", b"application/json")]}
        async def receive():
            return {"type": "http.request", "body": b"{}"}
        events = []
        async def send(e):
            events.append(e)
        task = asyncio.create_task(gate(scope, receive, send))
        await entered.wait()
        await gate(scope, receive, send)
        assert events[0]["status"] == 429
        release.set()
        await task
    asyncio.run(run())


def test_slow_chunks_have_total_deadline():
    async def run():
        gate = ReaderGateway(backend, "https://aitropos.ru", body_timeout=0.05)
        events = []
        async def receive():
            await asyncio.sleep(0.02)
            return {"type": "http.request", "body": b"x", "more_body": True}
        async def send(event):
            events.append(event)
        await asyncio.wait_for(gate({"type": "http", "method": "POST", "path": "/api/ask", "headers": [
            (b"origin", b"https://aitropos.ru"), (b"content-type", b"application/json")]}, receive, send), timeout=1)
        assert events[0]["status"] == 408
        assert not gate.busy
    asyncio.run(run())


def test_public_question_deadline_cancels_backend():
    async def run():
        cancelled = asyncio.Event()
        async def slow(scope, receive, send):
            try:
                await asyncio.sleep(30)
            finally:
                cancelled.set()
        gateway = ReaderGateway(slow, "https://aitropos.ru", ask_timeout=0.05)
        events = []
        async def receive():
            return {"type": "http.request", "body": b"{}"}
        async def send(event):
            events.append(event)
        await asyncio.wait_for(gateway({"type": "http", "method": "POST", "path": "/api/ask", "headers": [
            (b"origin", b"https://aitropos.ru"), (b"content-type", b"application/json")]}, receive, send), timeout=1)
        assert events[0]["status"] == 504
        assert cancelled.is_set() and not gateway.busy
    asyncio.run(run())


@pytest.mark.parametrize("timeout", [0, -1, 111])
def test_public_deadline_cannot_exceed_proxy_budget(timeout):
    with pytest.raises(ValueError):
        ReaderGateway(backend, "https://aitropos.ru", ask_timeout=timeout)


def test_health_strips_internal_data():
    import json
    async def run():
        async def health(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": json.dumps({"status": "ok", "profile": "SECRET", "application": {"status": "ok", "documents": "SECRET"}, "ml": {"status": "ok", "url": "SECRET"}, "reasons": ["SECRET"]}).encode()})
        events = []
        async def send(event):
            events.append(event)
        async def receive():
            return {"type": "http.request"}
        await ReaderGateway(health, "https://aitropos.ru")({"type": "http", "path": "/health", "method": "GET"}, receive, send)
        assert events[0]["status"] == 200
        assert b"SECRET" not in events[1]["body"]
        assert json.loads(events[1]["body"])["ml"]["status"] == "ok"
    asyncio.run(run())


def test_offline_backup_restore(tmp_path):
    import os
    import sqlite3
    import subprocess
    import sys
    from pathlib import Path
    if os.name != "nt":
        pytest.skip("Windows deployment lock")
    root = Path(__file__).resolve().parents[1]
    data = tmp_path / "data"
    data.mkdir()
    with sqlite3.connect(data / "navigator.sqlite") as con:
        con.execute("CREATE TABLE artificial(value)")
    (data / "originals").mkdir()
    (data / "originals" / "fake.txt").write_text("ARTIFICIAL")
    archive, restored = tmp_path / "backup.zip", tmp_path / "restored"
    subprocess.run([sys.executable, str(root / "scripts/deploy/backup.py"), str(data), str(archive)], check=True)
    subprocess.run([sys.executable, str(root / "scripts/deploy/restore.py"), str(archive), str(restored)], check=True)
    assert (restored / "originals/fake.txt").read_text() == "ARTIFICIAL"
    refused = subprocess.run([sys.executable, str(root / "scripts/deploy/restore.py"), str(archive), str(restored)], capture_output=True)
    assert refused.returncode != 0
    from scripts.deploy.serve import offline_lock
    with offline_lock(data):
        refused = subprocess.run([sys.executable, str(root / "scripts/deploy/backup.py"), str(data), str(tmp_path / "busy.zip")], capture_output=True)
        assert refused.returncode != 0


def test_windows_supervisor_stops_venv_interpreter_tree(tmp_path):
    import os
    import socket
    import subprocess
    import sys
    import time
    from pathlib import Path
    if os.name != "nt":
        pytest.skip("Windows supervisor and venv redirector")
    import httpx
    from scripts.deploy.serve import offline_lock
    root = Path(__file__).resolve().parents[1]
    state = tmp_path / "state"
    config = tmp_path / "reader.env"
    data = tmp_path / "data"
    config.write_text(f"DATA_DIR={data.as_posix()}\nREVIEW_DIR={(tmp_path / 'review').as_posix()}\nML_ENABLED=false\nLLM_ENABLED=false\nPUBLIC_ORIGIN=https://aitropos.ru\n")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    supervisor = subprocess.Popen([
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
        str(root / "scripts/deploy/supervise.ps1"), "-Python", sys.executable,
        "-EnvFile", str(config), "-StateDirectory", str(state), "-ReaderPort", str(port),
    ], creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        with httpx.Client(trust_env=False, timeout=1) as client:
            deadline = time.monotonic() + 30
            while True:
                assert supervisor.poll() is None
                try:
                    if client.get(f"http://127.0.0.1:{port}/").status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                assert time.monotonic() < deadline
                time.sleep(0.2)
    finally:
        state.mkdir(exist_ok=True)
        (state / "stop.request").touch()
        supervisor.wait(timeout=20)
    assert supervisor.returncode == 0
    with socket.socket() as sock:
        sock.settimeout(1)
        assert sock.connect_ex(("127.0.0.1", port)) != 0, "Orphan interpreter still listening"
    with offline_lock(data):
        pass


def test_public_tabs_and_ask_work_through_reader_gateway_without_auth(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from navigator.config import Settings
    from navigator.main import create_app

    settings = Settings(
        _env_file=None,
        profile="development",
        data_dir=tmp_path / "data",
        review_dir=tmp_path / "review",
        ml_enabled=False,
        llm_enabled=False,
    )
    monkeypatch.setattr("navigator.main.get_settings", lambda: settings)
    app = ReaderGateway(create_app(synthetic_fallback=False), "https://aitropos.ru")
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/documents").status_code == 200
        docs_res = client.get("/api/documents")
        assert docs_res.status_code == 200 and docs_res.json()["documents"] == []
        active_res = client.get("/api/ask/active")
        assert active_res.status_code == 403
        ask_res = client.post(
            "/api/ask",
            json={"question": "Как зовут Ливанова, кто это?"},
            headers={"Origin": "https://aitropos.ru", "Content-Type": "application/json"},
        )
        assert ask_res.status_code == 200
        assert ask_res.json()["status"] == "not_found"
        # Admin/write endpoints stay blocked
        assert client.get("/review").status_code == 403
        assert client.post("/api/documents").status_code == 403
        assert client.patch("/api/documents/missing/metadata", json={}).status_code == 403
