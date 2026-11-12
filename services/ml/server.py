"""Small local BGE HTTP service: python -m services.ml.server."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

from pydantic import ValidationError

from navigator.config import Settings
from navigator.interfaces import ComponentError
from navigator.rag.clients import strict_json
from navigator.schemas import (
    EmbeddingRequest, EmbeddingResponse, ErrorDetail, ErrorResponse, RerankRequest, RerankResponse,
)


class MLServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, backend):
        self.backend = backend
        self.inference_lock = threading.Lock()
        super().__init__(address, MLHandler)


class MLHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Do not log source texts or HTTP bodies.

    def _send(self, status, body):
        data = json.dumps(body, allow_nan=False).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _error(self, status, code, message, retryable=False):
        self._send(status, ErrorResponse(error=ErrorDetail(
            code=code, message=message, retryable=retryable)).model_dump(mode="json"))

    def do_POST(self):
        self.connection.settimeout(10)
        routes = {"/embed": (EmbeddingRequest, EmbeddingResponse, self.server.backend.embed),
                  "/rerank": (RerankRequest, RerankResponse, self.server.backend.rerank)}
        if self.path not in routes:
            self._error(404, "not_found", "Неизвестный ML endpoint.")
            return
        request_type, response_type, method = routes[self.path]
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 2 * 1024 * 1024 or self.headers.get("Transfer-Encoding"):
                raise ValueError("Invalid body size")
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("Incomplete body")
            request = request_type.model_validate(strict_json(raw))
            if isinstance(request, RerankRequest):
                ids = [fragment.id for fragment in request.fragments]
                if len(ids) != len(set(ids)):
                    raise ValueError("Duplicate IDs")
        except (ValueError, ValidationError, UnicodeError, OSError):
            self._error(422, "invalid_request", "Некорректный ML-запрос.")
            return
        if not self.server.inference_lock.acquire(blocking=False):
            self._error(503, "busy", "ML-сервис занят.", True)
            return
        try:
            response = response_type.model_validate(method(request))
            self._send(200, response.model_dump(mode="json"))
        except ValidationError:
            self._error(502, "invalid_model_output", "Некорректный ответ ML-модели.")
        except ComponentError as exc:
            if exc.detail.code == "context_exceeded":
                self._error(422, "context_exceeded", "Вход превышает токенный лимит BGE.")
            else:
                self._error(503, "model_unavailable", "ML-инференс недоступен.", True)
        except ValueError:
            self._error(502, "invalid_model_output", "Некорректный ответ ML-модели.")
        except Exception:
            self._error(503, "model_unavailable", "ML-инференс недоступен.", True)
        finally:
            self.server.inference_lock.release()


def main():
    parser = argparse.ArgumentParser(description="Serve locally cached pinned BGE models")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args()
    settings = Settings()
    if not settings.ml_enabled:
        parser.error("Set ML_ENABLED=true and pin RERANKER_REVISION before starting")
    from services.ml.backend import BGEBackend
    try:
        backend = BGEBackend(settings)
    except Exception as exc:
        raise SystemExit("BGE startup failed: check runtime dependencies, local pinned weights and device "
                         f"({type(exc).__name__}). No weights were downloaded.") from None
    with MLServer((args.host, args.port), backend) as server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
