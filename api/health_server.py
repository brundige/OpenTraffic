from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import urlparse


class HealthServer:
    """
    The detector's one network-facing listener, up whether or not the
    inspector is running.

        GET /healthz   {"status": "ok" | "degraded" | "down", "time": ...}
                       no login, so a load balancer or uptime monitor can
                       poll it; HTTP 200 unless the status is "down" (503)
        GET /health    the full report: device, sensor, detection,
                       controller link, system, network. Needs
                       Authorization: Bearer <token> (python auth.py
                       health-token).
    """

    def __init__(self, health, auth, host: str = "0.0.0.0", port: int = 8090):

        self.host = host
        self.port = port

        class Handler(BaseHTTPRequestHandler):

            server_version = "OpenTrafficHealth/1.0"

            def log_message(self, *args) -> None:
                pass

            def do_GET(self) -> None:

                path = urlparse(self.path).path

                try:

                    if path == "/healthz":
                        short = health.liveness()
                        return self._json(short, 503 if short["status"] == "down" else 200)

                    if path == "/health":
                        header = self.headers.get("Authorization", "")
                        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
                        if not auth.check_health_token(token):
                            return self._json(
                                {"error": "needs Authorization: Bearer <health token>"},
                                401,
                                {"WWW-Authenticate": 'Bearer realm="opentraffic"'},
                            )
                        return self._json(health.report())

                except Exception as exc:  # noqa: BLE001 - report, do not die
                    return self._json({"status": "unknown", "error": str(exc)}, 500)

                self._json({"error": "not found"}, 404)

            def _json(self, payload, status: int = 200, headers: Optional[dict] = None) -> None:
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer((host, port), Handler)
        self._server.daemon_threads = True
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="health", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
