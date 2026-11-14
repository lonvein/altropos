"""Start a private reader on a free loopback port with a NEW empty DATA_DIR."""
import argparse
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time


def main():
    from dotenv import dotenv_values
    import httpx
    parser = argparse.ArgumentParser()
    parser.add_argument("env", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    values = dotenv_values(args.env)
    with tempfile.TemporaryDirectory(prefix="altropos-private-smoke-") as temp:
        values["DATA_DIR"] = str(Path(temp) / "data").replace("\\", "/")
        values["REVIEW_DIR"] = str(Path(temp) / "review").replace("\\", "/")
        config = Path(temp) / "smoke.env"
        config.write_text("\n".join(f"{k}={v}" for k, v in values.items() if v is not None), encoding="utf-8")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        with (Path(temp) / "server.log").open("w+") as log:
            child = subprocess.Popen([sys.executable, str(root / "scripts/deploy/serve.py"),
                                      "--env", str(config), "--port", str(port)], cwd=root,
                                     stdout=log, stderr=log,
                                     creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            try:
                with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=5) as client:
                    for _ in range(60):
                        if child.poll() is not None:
                            log.seek(0)
                            raise RuntimeError(log.read())
                        try:
                            if client.get("/").status_code == 200:
                                break
                        except httpx.TransportError:
                            pass
                        time.sleep(0.5)
                    else:
                        raise RuntimeError("Server did not start")
                    origin = {"Origin": values["PUBLIC_ORIGIN"]}
                    # Anonymous: login page and a minimal health probe only.
                    assert client.get("/documents", headers={"Accept": "text/html"}).status_code == 303
                    assert client.get("/api/documents").status_code == 401
                    assert client.get("/login").status_code == 200
                    assert "profile" not in client.get("/health").json()
                    assert client.post("/api/documents").status_code == 403  # no same-origin proof
                    assert client.post("/login", data={"username": "admin", "password": "bad"},
                                       headers=origin).status_code == 401
                    # The public demo account signs in and has every function except OCR review.
                    demo = client.post("/login", data={"username": "admin", "password": "qwerty"},
                                       headers=origin)
                    assert demo.status_code == 303, demo.text
                    assert client.get("/documents").status_code == 200
                    assert client.get("/api/documents").status_code == 200
                    assert client.get("/api/ask/active").status_code == 200
                    assert client.get("/review", headers={"Accept": "text/html"}).status_code == 403
                    assert client.get("/api/documents/none/review").status_code == 403
                    assert client.patch("/api/documents/none/metadata", json={"reviewed": True},
                                        headers=origin).status_code == 403
                    assert client.post("/api/documents", headers=origin).status_code == 422
                    assert client.post("/api/ask", json={}).status_code == 403
                    assert client.post("/api/ask", json={}, headers=origin).status_code == 422
                    print("PASS: real localhost HTTP, web login, user/admin boundary, CSRF, schema validation")
            finally:
                child.terminate()
                child.wait(timeout=20)


if __name__ == "__main__":
    main()
