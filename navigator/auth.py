"""Web login, signed sessions and the user/administrator split.

Two kinds of accounts exist when ``AUTH_REQUIRED`` is on (the public VPS deployment):

* the public demo account (``admin`` / ``qwerty`` by default) -- its credentials are printed on
  the login page, so it carries **no** administrative privilege: every function of the local
  version except OCR review/approval;
* the administrator -- a *separate* account whose username and PBKDF2 password hash come only
  from the server environment (``ADMIN_USERNAME``, ``ADMIN_PASSWORD_HASH``). Without them no
  one can obtain the administrator role through the web.

The role lives in an HMAC-signed, expiring cookie. Authorization is checked on the server for
every request (``AuthMiddleware``) and again inside the review endpoints (``require_admin``),
so a direct API request cannot skip what the UI hides. When ``AUTH_REQUIRED`` is off (local
loopback use) the caller is the local operator and keeps every function, as before.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

log = logging.getLogger("navigator.auth")

ROLE_USER = "user"
ROLE_ADMIN = "admin"
ROLE_KEY = "altropos.role"
USER_KEY = "altropos.user"
SID_KEY = "altropos.sid"

PBKDF2_ROUNDS = 600_000
LOGIN_BODY_LIMIT = 4096

# Routes only the administrator may use: reviewing, approving and re-running doubtful OCR.
ADMIN_PATHS = (
    re.compile(r"^/review(?:/.*)?$"),
    re.compile(r"^/review-files(?:/.*)?$"),
    re.compile(r"^/api/documents/[^/]+/review/?$"),
    re.compile(r"^/api/documents/[^/]+/reocr/?$"),
)
PUBLIC_PATHS = (
    re.compile(r"^/login/?$"),
    re.compile(r"^/logout/?$"),
    re.compile(r"^/static/[A-Za-z0-9_./-]+$"),
    re.compile(r"^/favicon\.ico$"),
    re.compile(r"^/health$"),  # the public edge replaces the body with a sanitized status
)
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def is_admin_path(path: str) -> bool:
    return any(rule.match(path) for rule in ADMIN_PATHS)


# ---------------------------------------------------------------- password hashing

def hash_password(password: str, *, rounds: int = PBKDF2_ROUNDS, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return "pbkdf2_sha256${}${}${}".format(
        rounds, base64.b64encode(salt).decode(), base64.b64encode(digest).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, rounds, salt, digest = stored.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        expected = base64.b64decode(digest, validate=True)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                     base64.b64decode(salt, validate=True), int(rounds))
    except (ValueError, binascii.Error):
        return False
    return hmac.compare_digest(actual, expected)


# ---------------------------------------------------------------- configuration

@dataclass
class AuthConfig:
    required: bool = False
    secret: bytes = b""
    demo_enabled: bool = True
    demo_username: str = "admin"
    demo_password: str = "qwerty"
    admin_username: str = ""
    admin_password_hash: str = ""
    user_ttl: int = 12 * 3600
    admin_ttl: int = 2 * 3600
    public_origin: str = ""
    cookie_name: str = "altropos_session"
    revoked: set[str] = field(default_factory=set)

    @property
    def admin_enabled(self) -> bool:
        return bool(self.admin_username and self.admin_password_hash)

    @classmethod
    def from_settings(cls, settings: Any, *, required: bool | None = None) -> "AuthConfig":
        required = bool(getattr(settings, "auth_required", False) if required is None else required)
        config = cls(
            required=required,
            demo_enabled=bool(getattr(settings, "demo_login_enabled", True)),
            demo_username=getattr(settings, "demo_username", "admin") or "admin",
            demo_password=getattr(settings, "demo_password", "qwerty") or "qwerty",
            admin_username=(getattr(settings, "admin_username", "") or "").strip(),
            admin_password_hash=(getattr(settings, "admin_password_hash", "") or "").strip(),
            user_ttl=int(getattr(settings, "session_hours", 12)) * 3600,
            admin_ttl=int(getattr(settings, "admin_session_minutes", 120)) * 60,
            public_origin=(getattr(settings, "public_origin", "") or "").rstrip("/"),
        )
        if not required:
            return config
        if config.admin_username and config.admin_password_hash:
            if config.demo_enabled and config.admin_username.lower() == config.demo_username.lower():
                # The demo credentials are public: a same-name administrator would be guessable.
                log.error("ADMIN_USERNAME equals the public demo username; web administrator disabled")
                config.admin_username = config.admin_password_hash = ""
            elif config.demo_enabled and verify_password(config.demo_password, config.admin_password_hash):
                log.error("Administrator password equals the public demo password; web administrator disabled")
                config.admin_username = config.admin_password_hash = ""
            elif not config.admin_password_hash.startswith("pbkdf2_sha256$"):
                log.error("ADMIN_PASSWORD_HASH must be a pbkdf2_sha256 hash "
                          "(scripts/deploy/make_admin_hash.py); web administrator disabled")
                config.admin_username = config.admin_password_hash = ""
        else:
            log.warning("No ADMIN_USERNAME/ADMIN_PASSWORD_HASH: OCR review is unavailable through the web")
        configured = (getattr(settings, "session_secret", "") or "").strip()
        if configured and len(configured) < 32:
            raise ValueError("SESSION_SECRET must contain at least 32 characters")
        config.secret = configured.encode() if configured else _persistent_secret(
            Path(getattr(settings, "data_dir", "runtime")))
        if not config.demo_enabled and not config.admin_enabled:
            log.error("Authentication is required but no account can sign in")
        return config


def _persistent_secret(data_dir: Path) -> bytes:
    """Random secret kept next to the data (0600); sessions survive restarts."""
    path = data_dir / ".session-secret"
    try:
        if path.is_file():
            value = path.read_text().strip()
            if len(value) >= 32:
                return value.encode()
        data_dir.mkdir(parents=True, exist_ok=True)
        value = secrets.token_hex(32)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(value)
        return value.encode()
    except OSError:
        log.warning("Cannot persist the session secret; sessions end when the server restarts")
        return secrets.token_hex(32).encode()


# ---------------------------------------------------------------- sessions

def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def issue_token(config: AuthConfig, username: str, role: str, *, now: float | None = None) -> tuple[str, int]:
    now = int(now if now is not None else time.time())
    ttl = config.admin_ttl if role == ROLE_ADMIN else config.user_ttl
    payload = _b64(json.dumps({"u": username, "r": role, "iat": now, "exp": now + ttl,
                               "n": secrets.token_hex(8)}, separators=(",", ":")).encode())
    signature = _b64(hmac.new(config.secret, payload.encode(), hashlib.sha256).digest())
    return f"v1.{payload}.{signature}", ttl


def read_token(config: AuthConfig, token: str | None, *, now: float | None = None) -> dict | None:
    if not token or token.count(".") != 2:
        return None
    version, payload, signature = token.split(".")
    if version != "v1":
        return None
    expected = _b64(hmac.new(config.secret, payload.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        data = json.loads(_unb64(payload))
    except (ValueError, binascii.Error):
        return None
    if not isinstance(data, dict) or data.get("r") not in (ROLE_USER, ROLE_ADMIN):
        return None
    if int(data.get("exp", 0)) <= int(now if now is not None else time.time()):
        return None
    if data.get("n") in config.revoked:
        return None
    # The admin role is only honoured while the account still exists in the configuration.
    if data["r"] == ROLE_ADMIN and not (config.admin_enabled and data.get("u") == config.admin_username):
        return None
    if data["r"] == ROLE_USER and not (config.demo_enabled and str(data.get("u", "")).lower()
                                       == config.demo_username.lower()):
        return None
    return data


class LoginThrottle:
    """Slows password guessing: global for the administrator, per client otherwise."""

    def __init__(self, limit: int = 6, window: int = 600, lockout: int = 600):
        self.limit, self.window, self.lockout = limit, window, lockout
        self.failures: dict[str, deque[float]] = {}
        self.locked_until: dict[str, float] = {}

    def retry_after(self, key: str, now: float | None = None) -> int:
        now = now if now is not None else time.time()
        until = self.locked_until.get(key, 0)
        return max(0, int(until - now) + 1) if until > now else 0

    def failed(self, key: str, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        bucket = self.failures.setdefault(key, deque())
        bucket.append(now)
        while bucket and bucket[0] < now - self.window:
            bucket.popleft()
        if len(bucket) >= self.limit:
            self.locked_until[key] = now + self.lockout
            bucket.clear()

    def succeeded(self, key: str) -> None:
        self.failures.pop(key, None)


def authenticate(config: AuthConfig, username: str, password: str) -> tuple[str, str] | None:
    """Return ``(username, role)``. Both accounts are always compared (no early exit)."""
    username = (username or "").strip()
    demo_ok = False
    if config.demo_enabled:
        demo_ok = (hmac.compare_digest(username.lower().encode(), config.demo_username.lower().encode())
                   & hmac.compare_digest(password.encode(), config.demo_password.encode()))
    admin_ok = False
    if config.admin_enabled:
        user_match = hmac.compare_digest(username.encode(), config.admin_username.encode())
        # Always pay the PBKDF2 cost so response time does not reveal the account name.
        password_ok = verify_password(password, config.admin_password_hash)
        admin_ok = user_match and password_ok
    if admin_ok:
        return config.admin_username, ROLE_ADMIN
    if demo_ok:
        return config.demo_username, ROLE_USER
    return None


def safe_next(target: str | None) -> str:
    """Only same-site absolute paths; never an open redirect."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    if any(ord(c) < 32 for c in target):
        return "/"
    path = urlsplit(target).path
    if re.match(r"^/(login|logout)/?$", path):
        return "/"
    return target


# ---------------------------------------------------------------- ASGI middleware

def _headers(scope) -> dict[bytes, bytes]:
    return {k.lower(): v for k, v in scope.get("headers", [])}


def client_key(scope) -> str:
    """Rightmost X-Forwarded-For (added by the trusted reverse proxy), else the peer."""
    forwarded = _headers(scope).get(b"x-forwarded-for", b"").decode("latin1")
    if forwarded:
        return forwarded.split(",")[-1].strip()[:64] or "unknown"
    client = scope.get("client")
    return client[0] if client else "unknown"


def request_is_https(config: AuthConfig, scope) -> bool:
    if not config.public_origin.startswith("https://"):
        return False
    host = _headers(scope).get(b"host", b"").decode("latin1").split(":")[0].lower()
    return host not in {"", "127.0.0.1", "localhost", "[::1]"}


def session_cookie(config: AuthConfig, scope, token: str, ttl: int) -> bytes:
    secure = request_is_https(config, scope)
    name = ("__Host-" if secure else "") + config.cookie_name
    parts = [f"{name}={token}", "Path=/", f"Max-Age={ttl}", "HttpOnly", "SameSite=Lax"]
    if secure:
        parts.append("Secure")
    return "; ".join(parts).encode()


def clear_cookies(config: AuthConfig) -> list[bytes]:
    base = "Path=/; Max-Age=0; HttpOnly; SameSite=Lax"
    return [f"{config.cookie_name}=; {base}".encode(),
            f"__Host-{config.cookie_name}=; {base}; Secure".encode()]


def token_from_scope(config: AuthConfig, scope) -> str | None:
    raw = _headers(scope).get(b"cookie", b"").decode("latin1")
    if not raw:
        return None
    jar = SimpleCookie()
    try:
        jar.load(raw)
    except Exception:
        return None
    for name in (f"__Host-{config.cookie_name}", config.cookie_name):
        if name in jar:
            return jar[name].value
    return None


def same_origin(config: AuthConfig, scope) -> bool:
    """CSRF guard for state-changing requests: Origin must be this site."""
    if sum(k.lower() == b"origin" for k, _ in scope.get("headers", [])) > 1:
        return False
    headers = _headers(scope)
    origin = headers.get(b"origin", b"").decode("latin1")
    host = headers.get(b"host", b"").decode("latin1").strip().lower()
    fetch_site = headers.get(b"sec-fetch-site", b"").decode("latin1").lower()
    if origin:
        if origin == "null":
            # Privacy-restricted same-origin navigations (forms) report "null"; the browser's own
            # Sec-Fetch-Site (a forbidden header no page can set) proves the request is same-origin.
            return fetch_site == "same-origin"
        try:
            parsed = urlsplit(origin)
        except ValueError:
            return False
        expected_scheme = urlsplit(config.public_origin).scheme or scope.get("scheme", "http")
        if parsed.scheme != expected_scheme or parsed.username or parsed.password or parsed.path not in {"", "/"}:
            return False
        allowed = {host}
        if config.public_origin:
            allowed.add(urlsplit(config.public_origin).netloc.lower())
            netloc = urlsplit(config.public_origin).netloc.lower()
            allowed.add(netloc[4:] if netloc.startswith("www.") else "www." + netloc)
        return parsed.netloc.lower() in allowed
    # No Origin (older clients, some same-origin forms): require browser's own same-origin proof.
    return fetch_site in {"same-origin", "none"}


class AuthMiddleware:
    def __init__(self, app, config: AuthConfig):
        self.app, self.config = app, config
        self.throttle = LoginThrottle()
        self.admin_throttle = LoginThrottle(limit=8, window=900, lockout=900)

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        config = self.config
        if not config.required:
            scope[ROLE_KEY], scope[USER_KEY] = ROLE_ADMIN, "local"
            return await self.app(scope, receive, send)
        if scope["type"] == "websocket":
            return await self._deny(scope, receive, send, 403, "forbidden")

        path, method = scope["path"], scope["method"]
        if method in UNSAFE_METHODS and not same_origin(config, scope):
            return await self._deny(scope, receive, send, 403, "csrf", "Запрос с чужого источника отклонён.")

        session = read_token(config, token_from_scope(config, scope))
        if session:
            scope[ROLE_KEY], scope[USER_KEY], scope[SID_KEY] = session["r"], session["u"], session.get("n")
        else:
            scope[ROLE_KEY], scope[USER_KEY], scope[SID_KEY] = None, None, None

        if any(rule.match(path) for rule in PUBLIC_PATHS):
            if path.rstrip("/") == "/login" and method == "POST":
                return await self._login(scope, receive, send)
            if path.rstrip("/") == "/logout":
                return await self._logout(scope, receive, send, session)
            return await self.app(scope, receive, send)

        if session is None:
            return await self._unauthenticated(scope, receive, send)
        if is_admin_path(path) and session["r"] != ROLE_ADMIN:
            return await self._deny(scope, receive, send, 403, "admin_required",
                                    "Проверка и подтверждение распознавания доступны только администратору.")
        return await self.app(scope, receive, send)

    # -- responses
    async def _respond(self, scope, send, status, body: bytes, content_type: str,
                       extra: list[tuple[bytes, bytes]] | None = None):
        headers = [(b"content-type", content_type.encode()), (b"cache-control", b"no-store"),
                   (b"content-length", str(len(body)).encode()),
                   (b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"same-origin"),
                   (b"x-frame-options", b"SAMEORIGIN"), *(extra or [])]
        if content_type.startswith("text/html"):
            from navigator.main import PAGE_CSP
            headers.append((b"content-security-policy", PAGE_CSP.encode()))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body if scope["method"] != "HEAD" else b""})

    def _wants_html(self, scope) -> bool:
        accept = _headers(scope).get(b"accept", b"").decode("latin1")
        path = scope["path"]
        return scope["method"] in ("GET", "HEAD") and not path.startswith("/api/") and "text/html" in accept

    async def _deny(self, scope, receive, send, status, code, message="Доступ запрещён."):
        if scope["type"] == "websocket":
            return await send({"type": "websocket.close", "code": 1008})
        body = json.dumps({"error": {"code": code, "message": message, "retryable": False}},
                          ensure_ascii=False).encode()
        if self._wants_html(scope) and code == "admin_required":
            page = ("<!doctype html><meta charset=utf-8><title>Нужен администратор</title>"
                    f"<body style='font-family:system-ui;max-width:36rem;margin:3rem auto;padding:0 1rem'>"
                    f"<h1>Нужен администратор</h1><p>{message}</p>"
                    "<p><a href='/documents'>Вернуться к документам</a> · "
                    "<a href='/login'>Войти под другой учётной записью</a></p>").encode()
            return await self._respond(scope, send, status, page, "text/html; charset=utf-8")
        await self._respond(scope, send, status, body, "application/json")

    async def _unauthenticated(self, scope, receive, send):
        if self._wants_html(scope):
            target = scope["path"] + (("?" + scope["query_string"].decode("latin1")) if scope.get("query_string") else "")
            location = "/login?next=" + quote(safe_next(target), safe="/")
            return await self._respond(scope, send, 303, b"", "text/plain",
                                       [(b"location", location.encode())])
        await self._respond(scope, send, 401, json.dumps(
            {"error": {"code": "auth_required", "message": "Требуется вход.", "retryable": False}},
            ensure_ascii=False).encode(), "application/json")

    async def _read_form(self, receive) -> dict[str, str] | None:
        body = bytearray()
        while True:
            event = await receive()
            if event["type"] == "http.disconnect":
                return None
            body.extend(event.get("body", b""))
            if len(body) > LOGIN_BODY_LIMIT:
                return None
            if not event.get("more_body", False):
                break
        try:
            parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True, max_num_fields=8)
        except (UnicodeDecodeError, ValueError):
            return None
        return {key: values[-1] for key, values in parsed.items()}

    async def _login(self, scope, receive, send):
        config = self.config
        content_type = _headers(scope).get(b"content-type", b"").split(b";")[0].strip().lower()
        form = await self._read_form(receive) if content_type == b"application/x-www-form-urlencoded" else None
        if form is None:
            return await self._deny(scope, receive, send, 400, "invalid_request", "Некорректная форма входа.")
        username, password = form.get("username", "")[:200], form.get("password", "")[:512]
        target = safe_next(form.get("next"))
        key = client_key(scope)
        wait = max(self.throttle.retry_after(key), self.admin_throttle.retry_after("admin"))
        if wait:
            return await self._login_page(scope, send, 429, "Слишком много попыток. Повторите позже.",
                                          target, extra=[(b"retry-after", str(wait).encode())])
        result = authenticate(config, username, password)
        if result is None:
            self.throttle.failed(key)
            if config.admin_enabled and username.strip() == config.admin_username:
                self.admin_throttle.failed("admin")
            return await self._login_page(scope, send, 401, "Неверный логин или пароль.", target)
        self.throttle.succeeded(key)
        name, role = result
        if role == ROLE_ADMIN:
            self.admin_throttle.succeeded("admin")
        token, ttl = issue_token(config, name, role)
        await self._respond(scope, send, 303, b"", "text/plain", [
            (b"location", target.encode()), (b"set-cookie", session_cookie(config, scope, token, ttl))])

    async def _login_page(self, scope, send, status, message, target, extra=None):
        from navigator.main import render_login
        html = render_login(self.config, message=message, next_url=target)
        await self._respond(scope, send, status, html.encode(), "text/html; charset=utf-8", extra)

    async def _logout(self, scope, receive, send, session):
        if scope["method"] != "POST":
            return await self._respond(scope, send, 303, b"", "text/plain", [(b"location", b"/login")])
        if session and session.get("n"):
            if len(self.config.revoked) > 10_000:
                self.config.revoked.clear()
            self.config.revoked.add(session["n"])
        await self._respond(scope, send, 303, b"", "text/plain", [
            (b"location", b"/login"), *[(b"set-cookie", c) for c in clear_cookies(self.config)]])


# ---------------------------------------------------------------- endpoint-level check

def role_of(request) -> str | None:
    return request.scope.get(ROLE_KEY)


def is_admin_request(request) -> bool:
    config = getattr(request.app.state, "auth", None)
    return config is None or not config.required or role_of(request) == ROLE_ADMIN


def admin_denied(request):
    """JSON 403 for review/approval endpoints unless the caller is the administrator.

    This is the route-level half of the check (the middleware is the other): the endpoint
    stays closed even if it is ever mounted behind different routing. Applications without any
    authentication configuration keep the local-operator behaviour.
    """
    if is_admin_request(request):
        return None
    from fastapi.responses import JSONResponse

    return JSONResponse({"error": {"code": "admin_required", "retryable": False,
                                   "message": "Проверка и подтверждение распознавания доступны только администратору."}},
                        status_code=403)
