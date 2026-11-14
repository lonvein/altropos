"""Fail-closed public reader surface with open read/search/ask and admin protection.

Never expose the raw backend listener directly: bind loopback and use an SSH tunnel or
reverse proxy. Administration uses a separate private admin listener.
"""
import asyncio
import json
import os
import re
import time
from urllib.parse import urlparse


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


class ReaderGateway:
    def __init__(self, app, origin, body_timeout=10, ask_timeout=105):
        if not re.fullmatch(r"https://[a-z0-9.-]+(?::[0-9]+)?", origin):
            raise ValueError("PUBLIC_ORIGIN must be an explicit HTTPS origin")
        self.app, self.origin = app, origin
        self.allowed_origins = _allowed_origins(origin)
        self.busy = False
        self.body_timeout = body_timeout
        if not 0 < ask_timeout <= 110:
            raise ValueError("Public question deadline must be in (0, 110] seconds")
        self.ask_timeout = ask_timeout

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        async def reject(status, message):
            body = json.dumps({"error": message}).encode()
            await send({"type": "http.response.start", "status": status,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"cache-control", b"no-store")]})
            await send({"type": "http.response.body", "body": body})
        path, method = scope["path"], scope["method"]
        if path == "/health" and method == "GET":
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
            return await send({"type": "http.response.body", "body": json.dumps(payload).encode()})
        # Reject ambiguous encodings, traversal and all unlisted future routes.
        if "%" in path or "\\" in path or ".." in path:
            return await reject(403, "Forbidden")
        read = (
            path in {"/", "/documents", "/favicon.ico"}
            or path in {"/api/documents", "/api/ask/active"}
            or re.fullmatch(r"/static/[a-zA-Z0-9_./-]+", path) is not None
            or re.fullmatch(r"/api/sources/[a-zA-Z0-9_-]+(?:/highlight)?", path) is not None
            or re.fullmatch(r"/api/documents/[a-zA-Z0-9_-]+/(?:original|view|original-info)", path) is not None
            or re.fullmatch(r"/api/documents/[a-zA-Z0-9_-]+/original-page/[0-9]+", path) is not None
            or re.fullmatch(r"/api/ask/status/[a-zA-Z0-9_.:-]+", path) is not None
        )
        ask = path in {"/api/ask", "/api/search"} and method == "POST"
        if not ((read and method in {"GET", "HEAD"}) or ask):
            return await reject(403, "Reader access only")
        if not ask:
            return await self.app(scope, receive, send)
        pairs = scope.get("headers", [])
        # Multiple Origin headers are invalid, including identical ones.
        origins = [v.decode("latin1") for k, v in pairs if k.lower() == b"origin"]
        headers = {k.lower(): v for k, v in pairs}
        host_header = headers.get(b"host", b"").decode("latin1").strip()
        loopback_host = host_header.startswith(("127.0.0.1:", "localhost:")) or host_header in {"127.0.0.1", "localhost"}
        same_loopback_origin = loopback_host and origins == [f"http://{host_header}"]
        origin_ok = (len(origins) == 1 and origins[0] in self.allowed_origins) or same_loopback_origin
        if not origin_ok or headers.get(b"content-type", b"").split(b";")[0].strip().lower() != b"application/json":
            return await reject(403, "Same-origin JSON required")
        if self.busy:
            return await reject(429, "Another question is in progress")
        self.busy = True
        ask_deadline = time.monotonic() + self.ask_timeout
        try:
            body = bytearray()
            deadline = min(time.monotonic() + self.body_timeout, ask_deadline)
            while True:
                try:
                    event = await asyncio.wait_for(receive(), timeout=max(0, deadline - time.monotonic()))
                except TimeoutError:
                    return await reject(408, "Question upload timed out")
                if event["type"] == "http.disconnect":
                    return
                body.extend(event.get("body", b""))
                if len(body) > 32768:
                    return await reject(413, "Question too large")
                if not event.get("more_body", False):
                    break
            used = False
            async def replay():
                nonlocal used
                if not used:
                    used = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await receive()
            started = False
            async def output(event):
                nonlocal started
                if event["type"] == "http.response.start":
                    started = True
                await send(event)
            try:
                await asyncio.wait_for(self.app(scope, replay, output), timeout=max(0, ask_deadline - time.monotonic()))
            except TimeoutError:
                if not started:
                    return await reject(504, "Question deadline exceeded")
                # Status cannot be changed after headers; finish this response.
                await send({"type": "http.response.body", "body": b"", "more_body": False})
        finally:
            self.busy = False


def create_app():
    from navigator.main import create_app as backend
    return ReaderGateway(backend(synthetic_fallback=False), os.environ["PUBLIC_ORIGIN"],
                         ask_timeout=float(os.environ.get("PUBLIC_ASK_DEADLINE", "105")))
