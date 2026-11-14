"""Web login replaces the proxy password; the demo account is a user, never an administrator."""
import io
import re
import time

import pytest
from fastapi.testclient import TestClient

from navigator import auth
from navigator.auth import AuthConfig, hash_password, issue_token, read_token, safe_next, verify_password
from navigator.config import Settings
from navigator.main import create_app
from scripts.deploy.gateway import ReaderGateway

ORIGIN = {"Origin": "https://aitropos.ru"}
ADMIN_USER, ADMIN_PASSWORD = "ops-root", "Correct-Horse-Battery-9"


def make_app(tmp_path, monkeypatch, **overrides):
    values = dict(
        _env_file=None, profile="development", data_dir=tmp_path / "data", review_dir=tmp_path / "review",
        ml_enabled=False, llm_enabled=False, auth_required=True, public_origin="https://aitropos.ru",
        session_secret="s" * 40, admin_username=ADMIN_USER,
        admin_password_hash=hash_password(ADMIN_PASSWORD, rounds=2000),
    )
    values.update(overrides)
    settings = Settings(**values)
    monkeypatch.setattr("navigator.main.get_settings", lambda: settings)
    return create_app(synthetic_fallback=False)


def browser(app, gateway=True):
    target = ReaderGateway(app, "https://aitropos.ru", admin_routes=True) if gateway else app
    return TestClient(target, base_url="https://aitropos.ru", follow_redirects=False)


def login(client, username="admin", password="qwerty", **extra):
    return client.post("/login", data={"username": username, "password": password, **extra}, headers=ORIGIN)


def docx_bytes(text="Тестовый регламент приёма заявлений."):
    import docx
    document = docx.Document()
    document.add_paragraph(text)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


# ---------------------------------------------------------------------------- login page

def test_anonymous_visitors_get_the_login_page_not_a_proxy_challenge(tmp_path, monkeypatch):
    with browser(make_app(tmp_path, monkeypatch)) as client:
        page = client.get("/documents", headers={"Accept": "text/html"})
        assert page.status_code == 303 and page.headers["location"].startswith("/login?next=")
        assert "www-authenticate" not in page.headers
        api = client.get("/api/documents")
        assert api.status_code == 401 and api.json()["error"]["code"] == "auth_required"
        assert client.get("/api/documents/x/original").status_code == 401
        form = client.get("/login")
        assert form.status_code == 200
        # Demo credentials are filled in automatically.
        assert re.search(r'name="username"[^>]*value="admin"', form.text)
        assert re.search(r'name="password"[^>]*value="qwerty"', form.text)
        assert client.get("/static/css/app.css").status_code == 200


def test_login_form_prefill_can_be_switched_off(tmp_path, monkeypatch):
    with browser(make_app(tmp_path, monkeypatch, demo_login_enabled=False)) as client:
        form = client.get("/login").text
        assert 'value="admin"' not in form and 'value="qwerty"' not in form
        assert login(client).status_code == 401


def test_demo_login_creates_a_user_session_with_hardened_cookie(tmp_path, monkeypatch):
    with browser(make_app(tmp_path, monkeypatch)) as client:
        response = login(client, next="/documents")
        assert response.status_code == 303 and response.headers["location"] == "/documents"
        cookie = response.headers["set-cookie"]
        assert cookie.startswith("__Host-altropos_session=") and "HttpOnly" in cookie
        assert "Secure" in cookie and "SameSite=Lax" in cookie and "Path=/" in cookie
        session = client.get("/api/session").json()
        assert session == {"authenticated": True, "auth_required": True, "username": "admin", "is_admin": False}
        page = client.get("/documents").text
        assert 'id="upload-form"' in page and 'id="import-form"' in page and 'id="metadata-dialog"' in page
        for hidden in ('href="/review"', "document-card__review", "document-card__reocr"):
            assert hidden not in page
        assert client.get("/").status_code == 200


def test_wrong_credentials_and_open_redirects_are_rejected(tmp_path, monkeypatch):
    with browser(make_app(tmp_path, monkeypatch)) as client:
        bad = login(client, password="wrong")
        assert bad.status_code == 401 and "set-cookie" not in bad.headers and "Неверный" in bad.text
        for target in ("//evil.test/x", "https://evil.test", "/\\evil.test", "javascript:alert(1)", "/login"):
            assert safe_next(target) == "/"
        ok = login(client, next="//evil.test/")
        assert ok.headers["location"] == "/"


def test_logout_ends_the_session(tmp_path, monkeypatch):
    with browser(make_app(tmp_path, monkeypatch)) as client:
        login(client)
        assert client.get("/api/documents").status_code == 200
        out = client.post("/logout", headers=ORIGIN)
        assert out.status_code == 303 and out.headers["location"] == "/login"
        assert client.get("/api/documents").status_code == 401


def test_failed_logins_are_throttled(tmp_path, monkeypatch):
    with browser(make_app(tmp_path, monkeypatch)) as client:
        codes = [login(client, "admin", f"bad{i}").status_code for i in range(8)]
        assert codes[:6] == [401] * 6 and 429 in codes
        blocked = login(client)
        assert blocked.status_code == 429 and int(blocked.headers["retry-after"]) > 0


# ---------------------------------------------------------------------------- roles

def test_demo_account_cannot_review_or_approve_ocr_even_with_direct_api_calls(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    with browser(app, gateway=False) as client:
        login(client)
        assert client.get("/review", headers={"Accept": "text/html"}).status_code == 403
        assert client.get("/review-files/index.html").status_code == 403
        for method, path, kwargs in [
            ("get", "/api/documents/doc-1/review", {}),
            ("post", "/api/documents/doc-1/review", {"json": {"decision": "approved"}}),
            ("post", "/api/documents/doc-1/reocr", {"json": {}}),
        ]:
            response = getattr(client, method)(path, headers=ORIGIN, **kwargs)
            assert response.status_code == 403, path
            assert response.json()["error"]["code"] == "admin_required"


def test_metadata_cannot_be_used_to_approve_doubtful_ocr(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    with browser(app) as client:
        login(client)
        uploaded = client.post("/api/documents", headers=ORIGIN,
                               files={"file": ("order.docx", docx_bytes(), "application/octet-stream")})
        assert uploaded.status_code == 200
        doc_id = uploaded.json()["document_id"]
        deadline = time.monotonic() + 10
        while client.get(f"/api/jobs/{uploaded.json()['job_id']}").json()["status"] in {"queued", "processing", "running"}:
            assert time.monotonic() < deadline, "Artificial document processing did not finish"
            time.sleep(0.05)
        denied = client.patch(f"/api/documents/{doc_id}/metadata", headers=ORIGIN, json={"reviewed": True})
        assert denied.status_code == 403 and denied.json()["error"]["code"] == "admin_required"
        denied = client.patch(f"/api/documents/{doc_id}/metadata", headers=ORIGIN, json={"reviewed": False})
        assert denied.status_code == 403
        allowed = client.patch(f"/api/documents/{doc_id}/metadata", headers=ORIGIN,
                               json={"title": "Приказ о приёме", "number": "12"})
        assert allowed.status_code == 200 and allowed.json()["title"] == "Приказ о приёме"


def test_separate_secret_administrator_gets_review_access(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    with browser(app) as client:
        assert login(client, ADMIN_USER, "qwerty").status_code == 401
        assert login(client, "admin", ADMIN_PASSWORD).status_code == 401
        assert login(client, ADMIN_USER, ADMIN_PASSWORD).status_code == 303
        assert client.get("/api/session").json()["is_admin"] is True
        assert 'href="/review"' in client.get("/documents").text
        assert client.get("/review", headers={"Accept": "text/html"}).status_code in {200, 303, 307}
        # Past the guard: the unknown document is a 404, not a 403.
        assert client.get("/api/documents/missing/review").status_code == 404


def test_public_demo_credentials_can_never_become_the_administrator(tmp_path, monkeypatch):
    for kwargs in ({"admin_username": "admin"},
                   {"admin_password_hash": hash_password("qwerty", rounds=2000)},
                   {"admin_password_hash": "plain-text-password"},
                   {"admin_username": "", "admin_password_hash": ""}):
        app = make_app(tmp_path, monkeypatch, **kwargs)
        assert not app.state.auth.admin_enabled
        with browser(app) as client:
            login(client)
            assert client.get("/api/session").json()["is_admin"] is False
            assert client.get("/api/documents/x/review").status_code == 403


def test_forged_tampered_and_expired_sessions_are_rejected(tmp_path, monkeypatch):
    config = make_app(tmp_path, monkeypatch).state.auth
    token, _ = issue_token(config, "admin", "user")
    assert read_token(config, token)["r"] == "user"
    version, payload, signature = token.split(".")
    import base64, json
    body = json.loads(base64.urlsafe_b64decode(payload + "=="))
    body["r"] = "admin"
    forged = base64.urlsafe_b64encode(json.dumps(body).encode()).rstrip(b"=").decode()
    assert read_token(config, f"{version}.{forged}.{signature}") is None
    assert read_token(config, token[:-2] + "xx") is None
    assert read_token(config, "garbage") is None
    old, _ = issue_token(config, "admin", "user", now=time.time() - 10 * 24 * 3600)
    assert read_token(config, old) is None
    other = AuthConfig(required=True, secret=b"another-secret-another-secret-123456")
    assert read_token(other, token) is None
    # Admin tokens are honoured only for the configured administrator account.
    stolen, _ = issue_token(config, "someone-else", "admin")
    assert read_token(config, stolen) is None

    app = make_app(tmp_path, monkeypatch)
    with browser(app) as client:
        client.cookies.set("__Host-altropos_session", f"{version}.{forged}.{signature}")
        assert client.get("/api/documents").status_code == 401


def test_cross_site_writes_are_rejected(tmp_path, monkeypatch):
    with browser(make_app(tmp_path, monkeypatch), gateway=False) as client:
        login(client)
        for headers in ({"Origin": "https://evil.test"}, {}, {"Origin": "null"}):
            response = client.post("/api/import-url", headers=headers, json={"url": "https://mipt.ru/"})
            assert response.status_code == 403
        assert client.post("/api/import-url", headers={"Sec-Fetch-Site": "same-origin"},
                           json={"url": "https://example.com/"}).status_code != 403


def test_origin_requires_https_and_one_unambiguous_header(tmp_path, monkeypatch):
    with browser(make_app(tmp_path, monkeypatch), gateway=False) as client:
        login(client)
        for origin in ('http://aitropos.ru', 'ftp://aitropos.ru', 'https://[',
                       'https://evil@aitropos.ru', 'https://aitropos.ru/path'):
            assert client.post('/api/import-url', headers={'Origin': origin}, json={}).status_code == 403
        assert client.post('/api/import-url', headers=[('Origin', 'https://evil.test'),
                          ('Origin', 'https://aitropos.ru')], json={}).status_code == 403


def test_password_hash_roundtrip():
    stored = hash_password("s3cret", rounds=1000)
    assert stored.startswith("pbkdf2_sha256$1000$")
    assert verify_password("s3cret", stored) and not verify_password("s3cret ", stored)
    assert not verify_password("x", "pbkdf2_sha256$bad") and not verify_password("x", "")


def test_admin_paths_cover_every_review_route():
    for path in ("/review", "/review-files/a/b.html", "/api/documents/ab/review", "/api/documents/ab/reocr"):
        assert auth.is_admin_path(path)
    for path in ("/documents", "/api/documents", "/api/documents/ab/retry", "/api/documents/ab/metadata",
                 "/api/import-url", "/api/documents/ab/original"):
        assert not auth.is_admin_path(path)


# ---------------------------------------------------------------------------- local mode

def test_local_server_without_auth_keeps_every_function(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch, auth_required=False)
    with TestClient(app) as client:
        page = client.get("/documents").text
        assert 'href="/review"' in page and "document-card__review" in page
        assert client.get("/api/documents/missing/review").status_code == 404
        assert client.get("/login", follow_redirects=False).status_code == 303
        assert client.get("/api/session").json()["is_admin"] is True


# ---------------------------------------------------------------------------- web upload, end to end

def test_signed_in_user_uploads_processes_and_finds_documents_through_the_edge(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    with browser(app) as client:
        assert client.post("/api/documents", headers=ORIGIN,
                           files={"file": ("a.docx", docx_bytes(), "application/octet-stream")}).status_code == 401
        login(client)
        first_bytes = docx_bytes("Первый регламент.")  # python-docx stamps the time: build once
        first = client.post("/api/documents", headers=ORIGIN,
                            files={"file": ("a.docx", first_bytes, "application/octet-stream")})
        second = client.post("/api/documents", headers=ORIGIN,
                             files={"file": ("b.docx", docx_bytes("Второй регламент."), "application/octet-stream")})
        assert first.status_code == second.status_code == 200
        ids = {first.json()["document_id"], second.json()["document_id"]}
        assert len(ids) == 2
        for _ in range(100):
            docs = client.get("/api/documents").json()["documents"]
            if {d["status"] for d in docs if d["id"] in ids} <= {"ready", "needs_review", "failed"}:
                break
            time.sleep(0.05)
        assert {d["id"] for d in docs} >= ids
        assert all(d["status"] in {"ready", "needs_review"} for d in docs if d["id"] in ids)
        assert client.get(f"/api/documents/{first.json()['document_id']}/original").status_code == 200
        duplicate = client.post("/api/documents", headers=ORIGIN,
                                files={"file": ("a-copy.docx", first_bytes, "application/octet-stream")})
        assert duplicate.json()["duplicate"] is True
        bad = client.post("/api/documents", headers=ORIGIN,
                          files={"file": ("virus.exe", b"MZ", "application/octet-stream")})
        assert bad.status_code == 422


def test_ask_requests_belong_to_the_asking_session(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    with browser(app) as alice, browser(app) as bob:
        login(alice)
        login(bob)
        headers = {**ORIGIN, "Content-Type": "application/json", "X-Request-Id": "alice-req-1"}
        answered = alice.post("/api/ask", headers=headers, json={"question": "Как подать заявление?"})
        assert answered.status_code == 200
        assert alice.get("/api/ask/status/alice-req-1").status_code == 200
        assert bob.get("/api/ask/status/alice-req-1").status_code == 404
        assert bob.get("/api/ask/active").json() == {"active": None}


def test_login_form_works_when_the_browser_reports_a_null_origin(tmp_path, monkeypatch):
    """Regression: ``Referrer-Policy: no-referrer`` made browsers send ``Origin: null`` with the
    same-origin login form, so signing in failed with 403 behind the public edge."""
    with browser(make_app(tmp_path, monkeypatch)) as client:
        page = client.get("/login")
        assert page.headers["referrer-policy"] == "same-origin"
        assert client.get("/documents", headers={"Accept": "text/html"}).headers["referrer-policy"] == "same-origin"
        form = {"username": "admin", "password": "qwerty"}
        forged = client.post("/login", data=form, headers={"Origin": "null"})
        assert forged.status_code == 403  # a null origin alone proves nothing
        cross = client.post("/login", data=form, headers={"Origin": "null", "Sec-Fetch-Site": "cross-site"})
        assert cross.status_code == 403
        ok = client.post("/login", data=form, headers={"Origin": "null", "Sec-Fetch-Site": "same-origin"})
        assert ok.status_code == 303 and "set-cookie" in ok.headers
