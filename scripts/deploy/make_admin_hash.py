"""Create the secret administrator credentials for the public deployment.

The web login has two accounts: the public demo account (admin / qwerty, never an administrator)
and one separate administrator. Only a PBKDF2 hash of the administrator password is stored.

    python scripts/deploy/make_admin_hash.py --user ops-root

Prints ADMIN_USERNAME / ADMIN_PASSWORD_HASH lines for the private env file (outside Git), and a
fresh SESSION_SECRET. The username must differ from the public demo username.
"""
import argparse
import getpass
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from navigator.auth import hash_password  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", required=True, help="administrator login (not 'admin')")
    parser.add_argument("--password-file", type=Path,
                        help="read the password from this file instead of prompting")
    args = parser.parse_args()
    if args.user.strip().lower() in {"admin", "administrator", "root", "demo"}:
        parser.error("Choose a non-obvious administrator login: 'admin' is the public demo account")
    if args.password_file:
        password = args.password_file.read_text(encoding="utf-8").rstrip("\r\n")
    else:
        password = getpass.getpass("Administrator password: ")
        if password != getpass.getpass("Repeat: "):
            parser.error("Passwords differ")
    if len(password) < 12 or password.lower() == "qwerty":
        parser.error("Use at least 12 characters; the demo password is public")
    print(f"ADMIN_USERNAME={args.user.strip()}")
    print(f"ADMIN_PASSWORD_HASH={hash_password(password)}")
    print(f"SESSION_SECRET={secrets.token_hex(32)}")


if __name__ == "__main__":
    main()
