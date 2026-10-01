from __future__ import annotations

import http.client
import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

from auth import AuthStore


STATIC_DIR = Path(__file__).parent / "static"

MAX_BODY_BYTES = 1 << 20

COOKIE = "ot_session"

# Too many wrong passwords in a row locks logins out for a while. Behind
# the Jetson's socket proxy every client looks like 127.0.0.1, so this
# is effectively one counter for the whole unit -- which is what slows
# down guessing; the cost is that a guesser can lock the operator out
# for a few minutes.
FAIL_LIMIT = 8
LOCKOUT_S = 300

# The page is ours, the map tiles are OpenStreetMap's, nothing else.
CSP = (
    "default-src 'self'; "
    "img-src 'self' data: https://tile.openstreetmap.org; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "form-action 'self'"
)

# Headers worth passing back from the detector API.
PASS_HEADERS = ("Content-Type", "X-Frame-Number", "X-Layer")


class Sessions:
    """
    Logged-in browsers. In memory on purpose: when the inspector is
    stopped for being idle, everyone is logged out with it.
    """

    def __init__(self, hours: float):
        self.ttl = hours * 3600.0
        self._lock = threading.Lock()
        self._sessions: Dict[str, Dict[str, Any]] = {}

    def create(self, user: str) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[token] = {"user": user, "expires": time.time() + self.ttl}
        return token

    def get(self, token: Optional[str]) -> Optional[str]:
        if not token:
            return None
        with self._lock:
            entry = self._sessions.get(token)
            if entry is None:
                return None
            if entry["expires"] < time.time():
                del self._sessions[token]
                return None
            return entry["user"]

    def drop(self, token: Optional[str]) -> None:
        with self._lock:
            self._sessions.pop(token or "", None)


def _make_handler(auth: AuthStore, sessions: Sessions, upstream: Tuple[str, int], secure: bool):

    failures = {"count": 0, "locked_until": 0.0}
    fail_lock = threading.Lock()

    class InspectorHandler(BaseHTTPRequestHandler):

        server_version = "OpenTrafficInspector/1.0"
        protocol_version = "HTTP/1.1"

        def log_message(self, *args) -> None:
            pass

        # ---------------------------------------------------------- routing

        def do_GET(self) -> None:
            self._route("GET")

        def do_POST(self) -> None:
            self._route("POST")

        def do_PUT(self) -> None:
            self._route("PUT")

        def _route(self, method: str) -> None:

            path = urlparse(self.path).path

            try:

                if path == "/login" and method == "GET":
                    return self._static("login.html")

                if path == "/api/login" and method == "POST":
                    return self._login()

                if path == "/api/logout" and method == "POST":
                    sessions.drop(self._cookie())
                    return self._json({"ok": True}, cookie="")

                user = sessions.get(self._cookie())

                if path in ("/", "/index.html") and method == "GET":
                    if user is None:
                        return self._redirect("/login")
                    return self._static("index.html")

                if user is None:
                    return self._json({"error": "login required"}, 401)

                if path == "/api/session" and method == "GET":
                    return self._json({"user": user})

                if path.startswith("/api/"):
                    # Changes need a header a cross-site form cannot set,
                    # on top of the SameSite=Strict cookie.
                    if method != "GET" and self.headers.get("X-OpenTraffic") != "1":
                        return self._json({"error": "missing X-OpenTraffic header"}, 403)
                    return self._proxy(method)

            except (ValueError, json.JSONDecodeError) as exc:
                return self._json({"error": str(exc)}, 400)

            except Exception as exc:  # noqa: BLE001 - report, do not die
                return self._json({"error": str(exc)}, 500)

            self._json({"error": "not found"}, 404)

        # ------------------------------------------------------------ login

        def _login(self) -> None:

            with fail_lock:
                wait = failures["locked_until"] - time.time()

            if wait > 0:
                return self._json(
                    {"error": f"too many failed logins; try again in {wait:.0f} s"}, 429
                )

            if not auth.configured:
                return self._json(
                    {"error": "no login is set on this unit: run "
                              "'python auth.py set-password' on the device"},
                    503,
                )

            body = self._body()
            username = str(body.get("username", ""))
            password = str(body.get("password", ""))

            if not auth.verify(username, password):
                with fail_lock:
                    failures["count"] += 1
                    if failures["count"] >= FAIL_LIMIT:
                        failures["locked_until"] = time.time() + LOCKOUT_S
                        failures["count"] = 0
                print(f"Inspector: failed login for {username!r}")
                return self._json({"error": "wrong username or password"}, 401)

            with fail_lock:
                failures["count"] = 0

            print(f"Inspector: {username} logged in")
            self._json({"user": username}, cookie=sessions.create(username))

        def _cookie(self) -> Optional[str]:
            for part in self.headers.get("Cookie", "").split(";"):
                name, _, value = part.strip().partition("=")
                if name == COOKIE:
                    return value
            return None

        # ------------------------------------------------------------ proxy

        def _proxy(self, method: str) -> None:

            body = None
            if method != "GET":
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY_BYTES:
                    return self._json({"error": "request body too large"}, 413)
                body = self.rfile.read(length) if length else None

            connection = http.client.HTTPConnection(*upstream, timeout=10)

            try:
                headers = {"Content-Type": self.headers.get("Content-Type", "application/json")}
                connection.request(method, self.path, body=body, headers=headers)
                response = connection.getresponse()
                payload = response.read()
            except OSError as exc:
                return self._json({"error": f"detector unreachable: {exc}"}, 502)
            finally:
                connection.close()

            self.send_response(response.status)
            for key in PASS_HEADERS:
                value = response.getheader(key)
                if value:
                    self.send_header(key, value)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self._security_headers()
            self.end_headers()
            self.wfile.write(payload)

        # ---------------------------------------------------------- helpers

        def _body(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY_BYTES:
                raise ValueError("bad request body")
            return json.loads(self.rfile.read(length))

        def _security_headers(self) -> None:
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")

        def _json(self, payload, status: int = 200, cookie: Optional[str] = None) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            if cookie is not None:
                attributes = "HttpOnly; SameSite=Strict; Path=/" + ("; Secure" if secure else "")
                if cookie:
                    self.send_header("Set-Cookie", f"{COOKIE}={cookie}; Max-Age={int(sessions.ttl)}; {attributes}")
                else:
                    self.send_header("Set-Cookie", f"{COOKIE}=; Max-Age=0; {attributes}")
            self._security_headers()
            self.end_headers()
            self.wfile.write(data)

        def _redirect(self, location: str) -> None:
            self.send_response(303)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _static(self, name: str) -> None:
            # Only the files we ship, looked up by exact name.
            body = (STATIC_DIR / name).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self._security_headers()
            self.end_headers()
            self.wfile.write(body)

    return InspectorHandler


class InspectorServer:
    """
    The operator's web page: login, setup, the point-cloud inspector.

    It holds no detector state of its own. Every /api/ request from a
    logged-in browser is passed to the detector's loopback API, so the
    inspector can be stopped and started at will without touching
    detection -- on the Jetson, systemd starts it when someone connects
    and stops it once they have gone.
    """

    def __init__(
        self,
        auth_file: Path,
        upstream: Tuple[str, int] = ("127.0.0.1", 8081),
        host: str = "127.0.0.1",
        port: int = 8080,
        session_hours: float = 8.0,
        secure_cookie: bool = False,
    ):
        self.host = host
        self.port = port

        handler = _make_handler(
            AuthStore(auth_file), Sessions(session_hours), upstream, secure_cookie
        )

        self._server = ThreadingHTTPServer((host, port), handler)
        self._server.daemon_threads = True
        self._thread: Optional[threading.Thread] = None

    @property
    def url(self) -> str:
        host = "localhost" if self.host in ("0.0.0.0", "") else self.host
        return f"http://{host}:{self.port}/"

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="inspector", daemon=True
        )
        self._thread.start()

    def serve_forever(self) -> None:
        self._server.serve_forever()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
