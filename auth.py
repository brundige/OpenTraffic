"""
Credentials for a field unit: the one operator login for the inspector,
and the token a monitoring system uses for the full /health report.

Nothing is stored in the clear. The password is a salted PBKDF2-SHA256
hash; the health token is kept only as a SHA-256 hash and shown once,
when it is made. The file is written owner-only (0600).

Set them on the device:

    python auth.py set-password            # prompts; username defaults to "operator"
    python auth.py health-token            # prints a new token, once
    python auth.py show                    # what is configured, no secrets
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any, Dict, Optional


ITERATIONS = 600_000
MIN_PASSWORD = 10


class AuthStore:

    def __init__(self, path: Path):
        self.path = Path(path)

    def _read(self) -> Dict[str, Any]:
        try:
            return json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}

    def _write(self, data: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, indent=1)
        os.replace(temporary, self.path)

    # ------------------------------------------------------------ login

    @property
    def configured(self) -> bool:
        return "password" in self._read()

    @property
    def username(self) -> Optional[str]:
        return self._read().get("username")

    def set_password(self, username: str, password: str) -> None:

        if len(password) < MIN_PASSWORD:
            raise ValueError(f"password must be at least {MIN_PASSWORD} characters")

        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)

        data = self._read()
        data.update({
            "username": username,
            "password": {
                "algorithm": "pbkdf2_sha256",
                "iterations": ITERATIONS,
                "salt": salt.hex(),
                "hash": digest.hex(),
            },
        })
        self._write(data)

    def verify(self, username: str, password: str) -> bool:

        data = self._read()
        stored = data.get("password")

        if not stored:
            return False

        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode(),
            bytes.fromhex(stored["salt"]),
            int(stored["iterations"]),
        )

        # Both comparisons always run, so timing does not reveal which
        # of the two was wrong.
        user_ok = hmac.compare_digest(username.encode(), str(data.get("username", "")).encode())
        pass_ok = hmac.compare_digest(digest.hex(), stored["hash"])

        return user_ok and pass_ok

    # ----------------------------------------------------- health token

    def new_health_token(self) -> str:
        token = secrets.token_urlsafe(32)
        data = self._read()
        data["health_token_sha256"] = hashlib.sha256(token.encode()).hexdigest()
        self._write(data)
        return token

    def check_health_token(self, token: str) -> bool:
        stored = self._read().get("health_token_sha256")
        if not stored or not token:
            return False
        return hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), stored)


def main() -> int:

    from settings import load_settings

    parser = argparse.ArgumentParser(description="OpenTraffic credentials")
    parser.add_argument("--profile", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    pw = sub.add_parser("set-password", help="set the inspector login")
    pw.add_argument("--username", default="operator")

    sub.add_parser("health-token", help="make a new token for GET /health")
    sub.add_parser("show", help="what is configured (no secrets)")

    args = parser.parse_args()

    settings = load_settings(args.profile)
    store = AuthStore(settings.auth_file)

    if args.command == "set-password":
        first = getpass.getpass(f"New password for {args.username}: ")
        if getpass.getpass("Again: ") != first:
            print("Passwords do not match.", file=sys.stderr)
            return 1
        try:
            store.set_password(args.username, first)
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 1
        print(f"Login set for {args.username} in {store.path}")
        return 0

    if args.command == "health-token":
        token = store.new_health_token()
        print("Health token (shown once; any previous token stops working):")
        print(token)
        print(f"\nUse it as:  curl -H 'Authorization: Bearer {token[:6]}...' http://<unit>:{settings.health_port}/health")
        return 0

    print(f"Auth file:     {store.path}")
    print(f"Login:         {store.username or 'not set'}")
    print(f"Health token:  {'set' if store._read().get('health_token_sha256') else 'not set'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
