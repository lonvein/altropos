"""One Windows process, loopback only. Stop it before backup or restore."""
import argparse
import os
import sys
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


@contextmanager
def offline_lock(data):
    import msvcrt
    data.mkdir(parents=True, exist_ok=True)
    with (data / ".deployment.lock").open("a+b") as handle:
        handle.seek(0)
        if not handle.read(1):
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18000)
    parser.add_argument("--admin", action="store_true", help="Private maintenance only; never tunnel this port to VPS")
    args = parser.parse_args()
    if args.admin and args.port == 18000:
        parser.error("Admin must use a separate private port (for example 18001), never the public tunnel port 18000")
    from dotenv import dotenv_values
    values = dotenv_values(args.env)
    if not values.get("DATA_DIR") or not Path(values["DATA_DIR"]).is_absolute():
        parser.error("Explicit absolute DATA_DIR required")
    for key, value in values.items():
        if value is not None:
            os.environ[key] = value
    os.environ["UI_SYNTHETIC_FALLBACK"] = "false"
    os.chdir(ROOT)
    import uvicorn
    with offline_lock(Path(values["DATA_DIR"])):
        uvicorn.run("navigator.main:create_app" if args.admin else "scripts.deploy.gateway:create_app",
                    factory=True, host="127.0.0.1", port=args.port, workers=1,
                    proxy_headers=False, access_log=False)


if __name__ == "__main__":
    main()
