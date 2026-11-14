"""Fail-closed public edge in front of the application.

The VPS proxy no longer asks for a password: the application has its own login page and signed
sessions (``navigator.auth``), and every function of the local version -- questions, documents,
uploads, imports, metadata -- is available after sign-in. OCR review/approval stays administrator
only; the application enforces that per request, and this edge additionally keeps those routes
closed unless a separate administrator account is configured.

What this layer still guarantees, independent of the application code:

* an explicit allowlist of routes and methods (unknown or future routes are 403);
* ambiguous encodings and traversal are rejected;
* every body is bounded and read under a deadline before the application sees it;
* state-changing requests must come from this site's own origin (CSRF);
* one question at a time (the model is single-flight) and a deadline below the proxy's.

Never expose the raw backend listener: bind loopback and use an SSH tunnel or reverse proxy.
"""
import asyncio
import json
import os
import re
import time
from urllib.parse import urlparse

_ID = r"[a-zA-Z0-9_.-]+"


def _allowed_origins(origin: str) -> set[str]:
    parsed = urlparse(origin)
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    origins = {origin}
    if host.startswith("www."):
        origins.add(f"{parsed.scheme}://{host[4:]}{port}")
    elif host and host not in {"127.0.0.1", "localhost"}:
        origins.add(f"{parsed.scheme}://www.{host}{port}")
    return origins


# (method, path regex, body limit key, kind). kind: "read" | "write" | "ask" | "admin".
def _routes(max_upload: int):
    small, ask = 64 * 1024, 32 * 1024
    return [
        ("GET", r"/", 0, "read"),
        ("GET", r"/documents", 0, "read"),
        ("GET", r"/login", 0, "read"),
        ("POST", r"/login", 4096, "write"),
        ("POST", r"/logout", 1024, "write"),
        ("GET", r"/favicon\.ico", 0, "read"),
        ("GET", r"/static/[a-zA-Z0-9_./-]+", 0, "read"),
        ("GET", r"/api/session", 0, "read"),
        ("GET", r"/api/documents", 0, "read"),
        ("POST", r"/api/documents", max_upload, "write"),
        ("GET", rf"/api/documents/{_ID}/(?:original|view|original-info)", 0, "read"),
        ("GET", rf"/api/documents/{_ID}/original-page/[0-9]+", 0, "read"),
        ("PATCH", rf"/api/documents/{_ID}/metadata", small, "write"),
        ("POST", rf"/api/documents/{_ID}/retry", 1024, "write"),
        ("GET", r"/api/jobs", 0, "read"),
        ("GET", rf"/api/jobs/{_ID}", 0, "read"),
        ("POST", r"/api/import-url", small, "write"),
        ("GET", rf"/api/sources/{_ID}(?:/highlight)?", 0, "read"),
        ("GET", rf"/api/ask/status/{_ID}", 0, "read"),
        ("GET", r"/api/ask/active", 0, "read"),
        ("POST", r"/api/ask", ask, "ask"),
        ("POST", r"/api/search", ask, "ask"),
        # Administrator-only (OCR review / approval); the app re-checks the role.
        ("GET", r"/review", 0, "admin"),
        ("GET", r"/review-files/[a-zA-Z0-9_./-]+", 0, "admin"),
        ("GET", rf"/api/documents/{_ID}/review", 0, "admin"),
        ("POST", rf"/api/documents/{_ID}/review", small, "admin"),
        ("POST", rf"/api/documents/{_ID}/reocr", small, "admin"),
    ]


class ReaderGateway:
    def __init__(self, app, origin, body_timeout=10, ask_timeout=105, *, max_upload_mb=None,
                 admin_routes=None, upload_timeout=600):
        if not re.fullmatch(r"https://[a-z0-9.-]+(?::[0-9]+)?", origin):
            raise ValueError("PUBLIC_ORIGIN must be an explicit HTTPS origin")
        self.app, self.origin = app, origin
        self.allowed_origins = _allowed_origins(origin)
        self.busy = False
        self.body_timeout = body_timeout
        self.upload_timeout = upload_timeout
        if not 0 < ask_timeout <= 110:
            raise ValueError("Public question deadline must be in (0, 110] seconds")
        self.ask_timeout = ask_timeout
        if max_upload_mb is None:
            max_upload_mb = int(os.environ.get("MAX_UPLOAD_MB", "40"))
        # Multipart framing adds a little to the file itself.
        self.max_upload = int(max_upload_mb) * 1024 * 1024 + 1024 * 1024
        if admin_routes is None:
            admin_routes = bool(os.environ.get("ADMIN_USERNAME") and os.environ.get("ADMIN_PASSWORD_HASH"))
        self.admin_routes = admin_routes
        self.routes = [(m, re.compile(p), limit, kind) for m, p, limit, kind in _routes(self.max_upload)]

    # ---------------------------------------------------------------- helpers
    @staticmethod
    async def _reject(send, status, message, *, code=None, extra=()):
        error = {"code": code, "message": message, "retryable": False} if code else message
        body = json.dumps({"error": error}).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"), (b"cache-control", b"no-store"), *extra]})
        await send({"type": "http.response.body", "body": body})

    def _match(self, method, path):
        head = "GET" if method == "HEAD" else method
        known = False
        for route_method, pattern, limit, kind in self.routes:
            if pattern.fullmatch(path):
                known = True
                if route_method == head:
                    return limit, kind, True
        return 0, None, known

    def _origin_ok(self, pairs):
        origins = [v.decode("latin1") for k, v in pairs if k.lower() == b"origin"]
        headers = {k.lower(): v for k, v in pairs}
        host = headers.get(b"host", b"").decode("latin1").strip()
        loopback = host.startswith(("127.0.0.1:", "localhost:")) or host in {"127.0.0.1", "localhost"}
        same_loopback = loopback and origins == [f"http://{host}"]
        # Same-origin form posts from pages with a strict referrer policy carry "Origin: null";
        # Sec-Fetch-Site is set by the browser itself and cannot be forged by a web page.
        if origins == ["null"] and headers.get(b"sec-fetch-site", b"").lower() == b"same-origin":
            return True
        # Multiple Origin headers are invalid, including identical ones.
        return (len(origins) == 1 and origins[0] in self.allowed_origins) or same_loopback

    async def _read_body(self, receive, limit, deadline):
        body = bytearray()
        while True:
            try:
                event = await asyncio.wait_for(receive(), timeout=max(0, deadline - time.monotonic()))
            except TimeoutError:
                return None, 408, "Request body upload timed out"
            if event["type"] == "http.disconnect":
                return None, None, None
            body.extend(event.get("body", b""))
            if len(body) > limit:
                return None, 413, "Request body too large"
            if not event.get("more_body", False):
                return bytes(body), None, None

    async def _health(self, scope, receive, send):
        events = []

        async def capture(event):
            events.append(event)

        await self.app(scope, receive, capture)
        try:
            raw = json.loads(b"".join(e.get("body", b"") for e in events))
            allowed = {"ok", "limited", "degraded", "disabled", "unavailable", "unknown"}

            def status(obj):
                value = obj.get("status") if isinstance(obj, dict) else None
                return value if value in allowed else "unknown"

            payload = {"status": status(raw), "application": {
                "status": status(raw.get("application")), "data_mode": "real"},
                "ml": {"status": status(raw.get("ml"))},
                "llm": {"status": status(raw.get("llm"))}, "reasons": []}
        except (ValueError, TypeError, AttributeError):
            payload = {"status": "degraded", "reasons": ["Service status unavailable"]}
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json"), (b"cache-control", b"no-store")]})
        await send({"type": "http.response.body", "body": json.dumps(payload).encode()})

    # ---------------------------------------------------------------- ASGI
    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path, method = scope["path"], scope["method"]
        if path == "/health" and method == "GET":
            return await self._health(scope, receive, send)
        # Reject ambiguous encodings, traversal and all unlisted future routes.
        if "%" in path or "\\" in path or ".." in path:
            return await self._reject(send, 403, "Forbidden")
        limit, kind, known = self._match(method, path)
        if kind is None:
            return await self._reject(send, 405 if known else 403,
                                      "Method not allowed" if known else "Forbidden")
        if kind == "admin" and not self.admin_routes:
            return await self._reject(
                send, 403, "Проверка распознавания доступна в административном интерфейсе.",
                code="admin_required")
        pairs = scope.get("headers", [])
        headers = {k.lower(): v for k, v in pairs}
        unsafe = method not in {"GET", "HEAD"}
        if unsafe and not self._origin_ok(pairs):
            return await self._reject(send, 403, "Same-origin request required")
        content_type = headers.get(b"content-type", b"").split(b";")[0].strip().lower()
        if kind == "ask" and content_type != b"application/json":
            return await self._reject(send, 403, "Same-origin JSON required")
        if not unsafe:
            return await self.app(scope, receive, send)

        declared = headers.get(b"content-length", b"")
        if declared.isdigit() and int(declared) > limit:
            return await self._reject(send, 413, "Request body too large")

        if kind == "ask":
            return await self._ask(scope, receive, send, limit)
        upload = limit == self.max_upload
        deadline = time.monotonic() + (self.upload_timeout if upload else self.body_timeout)
        body, status, message = await self._read_body(receive, limit, deadline)
        if body is None:
            return await self._reject(send, status, message) if status else None
        return await self.app(scope, self._replay(body, receive), send)

    @staticmethod
    def _replay(body, receive):
        used = False

        async def replay():
            nonlocal used
            if not used:
                used = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        return replay

    async def _ask(self, scope, receive, send, limit):
        if self.busy:
            return await self._reject(send, 429, "Another question is in progress")
        self.busy = True
        ask_deadline = time.monotonic() + self.ask_timeout
        try:
            deadline = min(time.monotonic() + self.body_timeout, ask_deadline)
            body, status, message = await self._read_body(receive, limit, deadline)
            if body is None:
                if status == 408:
                    return await self._reject(send, 408, "Question upload timed out")
                if status == 413:
                    return await self._reject(send, 413, "Question too large")
                return None
            started = False

            async def output(event):
                nonlocal started
                if event["type"] == "http.response.start":
                    started = True
                await send(event)

            try:
                await asyncio.wait_for(self.app(scope, self._replay(body, receive), output),
                                       timeout=max(0, ask_deadline - time.monotonic()))
            except TimeoutError:
                if not started:
                    return await self._reject(send, 504, "Question deadline exceeded")
                # Status cannot be changed after headers; finish this response.
                await send({"type": "http.response.body", "body": b"", "more_body": False})
        finally:
            self.busy = False


PublicGateway = ReaderGateway


def create_app():
    from navigator.main import create_app as backend
    return ReaderGateway(backend(synthetic_fallback=False, auth_required=True), os.environ["PUBLIC_ORIGIN"],
                         ask_timeout=float(os.environ.get("PUBLIC_ASK_DEADLINE", "105")))
