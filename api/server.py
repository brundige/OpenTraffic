from __future__ import annotations

import ipaddress
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from urllib.parse import parse_qs, urlparse

import numpy as np

from controllers import CONTROLLER_KINDS, Controller, find_adapters
from controllers.network import (
    add_address, address_in_use, has_address, remove_address, subnet_for, wired_interfaces,
)
from perception.ground import fit_plane
from recording import RollingBuffer
from settings import SITE_KEYS, write_site
from zones import Zone, ZoneStore
from zones.zones import ZoneError


# How long an adopted adapter has to show it is talking to us.
ADOPT_WAIT_S = 5.0

# A zone document is a few kilobytes; anything larger is a mistake
# or an attack, and we are not going to buffer it.
MAX_BODY_BYTES = 1 << 20


def encode_cloud(snapshot, max_points: int = 0, mask=None) -> bytes:
    """
    Pack a snapshot as interleaved float32 [x, y, z, intensity].

    The buffer already dropped the no-return points, so this only has
    to thin the result down to what is worth sending over the wire.
    A mask keeps only the points it marks (the moving ones).
    """

    xyz = snapshot.xyz

    intensity = snapshot.intensity

    if mask is not None:
        xyz = xyz[mask]
        intensity = intensity[mask] if intensity is not None else None

    if max_points and len(xyz) > max_points:

        stride = int(np.ceil(len(xyz) / max_points))

        xyz = xyz[::stride]
        intensity = intensity[::stride] if intensity is not None else None

    packed = np.empty((len(xyz), 4), dtype=np.float32)

    packed[:, 0:3] = xyz
    packed[:, 3] = intensity if intensity is not None else 0.0

    return packed.tobytes()


def _make_handler(
    buffer: RollingBuffer,
    store: ZoneStore,
    describe: Callable[[], Dict[str, Any]],
    max_points: int,
    clip_dir: Path,
    controller: Optional[Controller],
    presence: Optional[Any],
    background: Optional[Any],
    health: Optional[Any],
):

    class ApiHandler(BaseHTTPRequestHandler):

        server_version = "OpenTrafficDetector/1.0"

        protocol_version = "HTTP/1.1"

        def log_message(self, *args) -> None:
            # The detector owns stdout; stay out of its way.
            pass

        # ---------------------------------------------------------- routes

        def do_GET(self) -> None:

            route = urlparse(self.path)

            try:

                if route.path == "/api/health":
                    if health is None:
                        return self._send_json({"error": "no health report"}, status=404)
                    return self._send_json(health.report())

                if route.path == "/api/status":
                    return self._send_json(self._status())

                if route.path == "/api/cloud":
                    return self._send_cloud(parse_qs(route.query))

                if route.path == "/api/zones":
                    return self._send_json(store.load())

                if route.path == "/api/controller/config":
                    return self._send_json(self._controller_config())

                if route.path == "/api/controller":
                    if controller is None:
                        return self._send_json({"controller": None})
                    payload = controller.status()
                    payload["presence"] = presence.status() if presence else []
                    return self._send_json(payload)

            except ZoneError as exc:
                return self._send_json({"error": str(exc)}, status=400)

            except Exception as exc:  # noqa: BLE001 - report, do not die
                return self._send_json({"error": str(exc)}, status=500)

            self._send_json({"error": "not found"}, status=404)

        def do_POST(self) -> None:

            route = urlparse(self.path)

            if route.path == "/api/roadway":
                return self._fit_roadway()

            if route.path == "/api/controller/discover":
                return self._discover_adapters()

            if route.path == "/api/controller/adopt":
                return self._adopt_adapter()

            if route.path == "/api/background":
                if background is None:
                    return self._send_json({"error": "no background model"}, status=404)
                background.learn()
                print("Inspector: relearning the background")
                return self._send_json(background.status())

            if route.path != "/api/clip":
                return self._send_json({"error": "not found"}, status=404)

            try:

                name = time.strftime("clip_%Y%m%d_%H%M%S.npz", time.localtime())

                meta = buffer.save(clip_dir / name, sensor=describe())

                print(
                    f"Inspector: saved {meta['seconds']} s "
                    f"({meta['frames']} frames, {meta['megabytes']} MB) "
                    f"to {meta['path']}"
                )

                return self._send_json(meta)

            except ValueError as exc:
                return self._send_json({"error": str(exc)}, status=409)

            except Exception as exc:  # noqa: BLE001
                return self._send_json({"error": str(exc)}, status=500)

        def do_PUT(self) -> None:

            route = urlparse(self.path)

            if route.path == "/api/controller/config":
                return self._configure_controller()

            if route.path != "/api/zones":
                return self._send_json({"error": "not found"}, status=404)

            try:

                # Before any sensor has connected there is nothing to
                # stamp; keep the document's own record.
                document = store.save(self._read_json(), sensor=describe() or None)

                print(
                    f"Inspector: saved {len(document['zones'])} zone(s) "
                    f"to {store.path}"
                )

                return self._send_json(document)

            except ZoneError as exc:
                return self._send_json({"error": str(exc)}, status=400)

            except Exception as exc:  # noqa: BLE001
                return self._send_json({"error": str(exc)}, status=500)

        # ------------------------------------------------------ controller

        def _controller_config(self) -> Dict[str, Any]:

            if not hasattr(controller, "reconfigure"):
                return {"configurable": False}

            settings = controller.settings

            return {
                "configurable": True,
                "kinds": list(CONTROLLER_KINDS),
                **{key: getattr(settings, key) for key in SITE_KEYS},
                "ports": {
                    "command": settings.controller_port,
                    "reply": settings.controller_listen_port,
                    "forward": settings.controller_forward_port,
                },
                # Fixed by the environment or command line: changing
                # them here would not survive a restart.
                "pinned": sorted(set(SITE_KEYS) & settings.pinned),
                "site_file": str(settings.site_file),
            }

        def _configure_controller(self) -> None:

            if not hasattr(controller, "reconfigure"):
                return self._send_json({"error": "controller is not configurable"}, status=404)

            try:

                body = self._read_json()

                changes = {key: body[key] for key in SITE_KEYS if key in body}

                kind = changes.get("controller", controller.settings.controller)

                if kind not in CONTROLLER_KINDS:
                    raise ZoneError(
                        f"controller must be one of {', '.join(CONTROLLER_KINDS)}"
                    )

                if "controller_host" in changes:
                    host = str(changes["controller_host"] or "").strip()
                    if host:
                        try:
                            ipaddress.IPv4Address(host)
                        except ValueError:
                            raise ZoneError(f"{host!r} is not an IPv4 address")
                    changes["controller_host"] = host

                if kind == "luxcom" and not changes.get(
                    "controller_host", controller.settings.controller_host
                ):
                    raise ZoneError("the EM-HDLC needs an address")

                # Back to the simulator: give up any address taken for
                # the adapter, so it does not linger on the cabinet LAN.
                settings = controller.settings

                if kind == "simulator" and settings.controller_local_address:
                    remove_address(settings.controller_interface, settings.controller_local_address)
                    changes["controller_local_address"] = ""
                    changes["controller_interface"] = ""

                pinned = set(changes) & controller.settings.pinned

                if pinned:
                    raise ZoneError(
                        f"{', '.join(sorted(pinned))} is set by the unit's "
                        f"environment and cannot be changed here"
                    )

                controller.reconfigure(**changes)

                write_site(controller.settings.site_file, changes)

                print(
                    f"Inspector: controller set to {controller.settings.controller}"
                    + (f" at {controller.settings.controller_host}"
                       if controller.settings.controller == "luxcom" else "")
                )

                return self._send_json(self._controller_config())

            except ZoneError as exc:
                return self._send_json({"error": str(exc)}, status=400)

            except Exception as exc:  # noqa: BLE001
                return self._send_json({"error": f"{type(exc).__name__}: {exc}"}, status=500)

        def _adopt_adapter(self) -> None:
            """
            Use an adapter whose command/forward IP is not ours: take
            that address on the port the adapter was heard on, point
            the link at the adapter, and keep it only if the adapter
            answers.
            """

            if not hasattr(controller, "reconfigure"):
                return self._send_json({"error": "controller is not configurable"}, status=404)

            try:

                body = self._read_json()

                try:
                    adapter = str(ipaddress.IPv4Address(str(body.get("adapter", "")).strip()))
                    wanted = str(ipaddress.IPv4Address(str(body.get("address", "")).strip()))
                except ValueError:
                    raise ZoneError("adapter and address must be IPv4 addresses")

                interface = str(body.get("interface") or "")

                if interface not in wired_interfaces():
                    raise ZoneError(f"{interface!r} is not a wired interface of this unit")

                pinned = {"controller", "controller_host"} & controller.settings.pinned

                if pinned:
                    raise ZoneError(
                        f"{', '.join(sorted(pinned))} is set by the unit's environment"
                    )

                cidr = subnet_for(wanted, adapter)
                previous = controller.settings
                taken = False

                if not has_address(interface, wanted):

                    if address_in_use(interface, wanted):
                        raise ZoneError(
                            f"{wanted} is in use by another device on {interface}; "
                            f"set the adapter's command and forward IP to this unit instead"
                        )

                    add_address(interface, cidr)
                    taken = True

                changes = {
                    "controller": "luxcom",
                    "controller_host": adapter,
                    "controller_local_address": cidr,
                    "controller_interface": interface,
                }

                try:
                    controller.reconfigure(**changes)
                except Exception:
                    if taken:
                        remove_address(interface, cidr)
                    raise

                # Keep it only if the adapter now answers or forwards.
                deadline = time.monotonic() + ADOPT_WAIT_S
                link = {}

                while time.monotonic() < deadline:
                    link = controller.link() or {}
                    if link.get("replies") or link.get("forwarded"):
                        break
                    time.sleep(0.25)

                if not (link.get("replies") or link.get("forwarded")):

                    controller.reconfigure(**{
                        key: getattr(previous, key) for key in changes
                    })

                    if taken:
                        remove_address(interface, cidr)

                    return self._send_json({
                        "error": (
                            f"took {wanted} on {interface}, but {adapter} sent nothing in "
                            f"{ADOPT_WAIT_S:.0f} s, so it was given back. Check the adapter's "
                            f"command and forward ports ({controller.settings.controller_listen_port}"
                            f" and {controller.settings.controller_forward_port})."
                        )
                    }, status=502)

                write_site(controller.settings.site_file, changes)

                print(
                    f"Inspector: took {cidr} on {interface} for the EM-HDLC at "
                    f"{adapter}; controller set to luxcom"
                )

                return self._send_json(self._controller_config())

            except ZoneError as exc:
                return self._send_json({"error": str(exc)}, status=400)

            except Exception as exc:  # noqa: BLE001
                return self._send_json({"error": f"{type(exc).__name__}: {exc}"}, status=500)

        def _discover_adapters(self) -> None:

            try:

                settings = getattr(controller, "settings", None)

                running = None

                if controller is not None and controller.describe().get("kind") == "luxcom":
                    running = controller.link()

                ports = {}

                if settings is not None:
                    ports = {
                        "port": settings.controller_port,
                        "listen_port": settings.controller_listen_port,
                        "forward_port": settings.controller_forward_port,
                    }

                result = find_adapters(running=running, **ports)

                print(
                    f"Inspector: adapter search found "
                    f"{len(result['adapters'])} on {', '.join(result['searched']) or 'no interface'}"
                )

                return self._send_json(result)

            except Exception as exc:  # noqa: BLE001
                return self._send_json({"error": f"{type(exc).__name__}: {exc}"}, status=500)

        # --------------------------------------------------------- helpers

        def _fit_roadway(self) -> None:
            """
            Fit the road surface to the newest frame.

            An optional polygon in the body restricts the fit to a
            patch of known road, which is what you want when the
            sensor can see more roof and kerb than tarmac.
            """

            snapshot = buffer.latest()

            if snapshot is None:
                return self._send_json(
                    {"error": "no frame received yet"}, status=503
                )

            try:

                body = self._read_json() if self.headers.get("Content-Length") else {}

                points = snapshot.xyz

                polygon = body.get("polygon")

                if polygon:

                    patch = Zone.from_dict(
                        {"type": "roi", "polygon": polygon,
                         "height_min": -1e6, "height_max": 1e6}
                    )

                    inside = patch.contains(points[:, :2])

                    if inside.sum() < 3:
                        raise ZoneError(
                            "fewer than 3 points inside that outline"
                        )

                    points = points[inside]

                plane, stats = fit_plane(points)

                stats["source"] = time.strftime(
                    "fitted %Y-%m-%dT%H:%M:%SZ", time.gmtime()
                )
                stats["scope"] = "outline" if polygon else "whole frame"

                print(
                    f"Inspector: roadway fitted from {stats['inliers']:,} of "
                    f"{stats['points']:,} points, "
                    f"sensor {stats['sensor_height']} m up, "
                    f"tilt {stats['tilt_deg']} deg, rms {stats['rms']} m"
                )

                return self._send_json(dict(plane.to_dict(), fit=stats))

            except (ZoneError, ValueError) as exc:
                return self._send_json({"error": str(exc)}, status=400)

            except Exception as exc:  # noqa: BLE001
                return self._send_json({"error": str(exc)}, status=500)

        def _status(self) -> Dict[str, Any]:

            snapshot = buffer.latest()

            status = {
                "sensor": describe(),
                "zones_path": str(store.path),
                "clip_dir": str(clip_dir),
                "max_points": max_points,
                "buffer": buffer.stats(),
                "background": background.status() if background else None,
                "frame": None,
            }

            if snapshot is not None:
                status["frame"] = {
                    "number": snapshot.frame_number,
                    "timestamp": snapshot.timestamp,
                    "points": snapshot.points,
                }

            return status

        def _send_cloud(self, query: Dict[str, Any]) -> None:

            snapshot = buffer.latest()

            if snapshot is None:
                return self._send_json(
                    {"error": "no frame received yet"}, status=503
                )

            limit = max_points

            if "max_points" in query:
                try:
                    limit = max(1000, min(int(query["max_points"][0]), 500000))
                except (TypeError, ValueError):
                    pass

            mask = None
            layer = "all"

            if query.get("layer", ["all"])[0] == "moving" and background is not None:

                # The buffer takes a frame just before it is classified,
                # so the newest snapshot can be one ahead of the newest
                # mask; serve the frame the mask belongs to.
                number, latest_mask = background.latest()

                if latest_mask is not None:

                    if snapshot.frame_number != number:
                        snapshot = next(
                            (s for s in reversed(buffer.snapshots())
                             if s.frame_number == number),
                            None,
                        )

                    if snapshot is not None and len(latest_mask) == snapshot.points:
                        mask = latest_mask
                        layer = "moving"
                    else:
                        snapshot = buffer.latest()

            payload = encode_cloud(snapshot, limit, mask)

            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Number", str(snapshot.frame_number))
            self.send_header("X-Layer", layer)
            self.end_headers()

            self.wfile.write(payload)

        def _read_json(self) -> Dict[str, Any]:

            length = int(self.headers.get("Content-Length") or 0)

            if length <= 0:
                raise ZoneError("empty request body")

            if length > MAX_BODY_BYTES:
                raise ZoneError("request body too large")

            try:
                return json.loads(self.rfile.read(length))
            except json.JSONDecodeError as exc:
                raise ZoneError(f"body is not valid JSON: {exc}")

        def _send_json(self, payload: Dict[str, Any], status: int = 200) -> None:

            body = json.dumps(payload).encode()

            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

            self.wfile.write(body)

    return ApiHandler


class DetectorApi:
    """
    The detector's own API: clouds, zones, roadway, background, calls,
    health. It has no authentication, so it binds to loopback only;
    people reach it through the inspector, which logs them in first.
    """

    def __init__(
        self,
        buffer: RollingBuffer,
        store: ZoneStore,
        host: str = "127.0.0.1",
        port: int = 8081,
        max_points: int = 60000,
        describe: Optional[Callable[[], Dict[str, Any]]] = None,
        clip_dir: Path = Path("data/clips"),
        controller: Optional[Controller] = None,
        presence: Optional[Any] = None,
        background: Optional[Any] = None,
        health: Optional[Any] = None,
    ):
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise ValueError(
                f"the detector API has no login and must bind to loopback, not {host!r}"
            )

        self.buffer = buffer
        self.store = store
        self.host = host
        self.port = port

        handler = _make_handler(
            buffer,
            store,
            describe or (lambda: {}),
            max_points,
            Path(clip_dir),
            controller,
            presence,
            background,
            health,
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
            target=self._server.serve_forever,
            name="detector-api",
            daemon=True,
        )

        self._thread.start()

    def stop(self) -> None:

        self._server.shutdown()
        self._server.server_close()

        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
