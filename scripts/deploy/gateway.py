"""Fail-closed public reader surface; authentication belongs to the VPS proxy.

Never expose this listener directly: bind loopback and use an SSH tunnel or
Tailscale Serve restricted to the VPS. Administration uses another private app.
"""
import asyncio
import json
import os
import re
import time


class ReaderGateway:
    def __init__(self, app, origin, body_timeout=10):
        if not re.fullmatch(r"https://[a-z0-9.-]+(?::[0-9]+)?", origin):
            raise ValueError("PUBLIC_ORIGIN must be an explicit HTTPS origin")
        self.app, self.origin = app, origin
        self.busy = False
        self.next_question = 0.0
        self.body_timeout = body_timeout

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
        read = path == "/" or re.fullmatch(r"/static/[a-zA-Z0-9_./-]+", path) or re.fullmatch(
            r"/api/(sources/[a-zA-Z0-9_-]+|documents/[a-zA-Z0-9_-]+/original)", path)
        ask = path == "/api/ask" and method == "POST"
        if not ((read and method in {"GET", "HEAD"}) or ask):
            return await reject(403, "Reader access only")
        if not ask:
            return await self.app(scope, receive, send)
        pairs = scope.get("headers", [])
        # Multiple Origin headers are invalid, including identical ones.
        origins = [v.decode("latin1") for k, v in pairs if k.lower() == b"origin"]
        headers = {k.lower(): v for k, v in pairs}
        if origins != [self.origin] or headers.get(b"content-type", b"").split(b";")[0].strip().lower() != b"application/json":
            return await reject(403, "Same-origin JSON required")
        if self.busy or time.monotonic() < self.next_question:
            return await reject(429, "Another question is in progress")
        self.busy = True
        self.next_question = time.monotonic() + 2
        try:
            body = bytearray()
            deadline = time.monotonic() + self.body_timeout
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
            await self.app(scope, replay, send)
        finally:
            self.busy = False


def create_app():
    from navigator.main import create_app as backend
    return ReaderGateway(backend(synthetic_fallback=False), os.environ["PUBLIC_ORIGIN"])
