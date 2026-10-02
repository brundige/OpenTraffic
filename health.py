"""
Device health: what an operator or a monitoring system needs to know
about a field unit without opening the inspector.

The report has one overall status -- ok, degraded or down -- and the
reasons behind it, so a monitor can alert on a single field and a
person can see why.

  down       no LiDAR frames: the unit is not detecting anything (the
             problem says whether it is still looking for the sensor)
  degraded   detecting, but something needs attention: the controller
             link is quiet, the background is not learned, the sensor
             is not the one the unit was set up with, the disk or
             memory is nearly full, the board is hot, packets are being
             dropped
  ok         everything above is fine
"""

from __future__ import annotations

import os
import platform
import shutil
import socket
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import psutil


# Thresholds for "degraded".
FRAME_STALE_S = 5.0
CONTROLLER_STALE_S = 10.0
DISK_PCT = 90.0
MEMORY_PCT = 90.0
TEMP_C = 85.0


class FrameStats:
    """
    Updated by the ingest loop every frame; read by the health report.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.started = time.time()
        self.frames = 0
        self.last_frame: Optional[float] = None       # monotonic
        self.last_points = 0
        self._window: List[float] = []

    def frame(self, points: int) -> None:
        now = time.monotonic()
        with self._lock:
            self.frames += 1
            self.last_frame = now
            self.last_points = points
            self._window.append(now)
            while self._window and self._window[0] < now - 5.0:
                self._window.pop(0)

    def snapshot(self) -> Dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            # Only frames from the last 5 s: the window is trimmed as
            # frames arrive, so with none arriving it would go stale.
            window = [t for t in self._window if t >= now - 5.0]
            age = now - self.last_frame if self.last_frame is not None else None
            return {
                "frames": self.frames,
                "fps": round(len(window) / 5.0, 1) if window else 0.0,
                "last_frame_age_s": round(age, 2) if age is not None else None,
                "points_per_frame": self.last_points,
            }


def _udp_errors() -> Optional[Dict[str, int]]:
    """
    Kernel UDP drop counters (Linux). RcvbufErrors climbing means LiDAR
    packets are being dropped because the receive buffer is too small.
    """

    try:
        lines = Path("/proc/net/snmp").read_text().splitlines()
    except OSError:
        return None

    rows = [line.split() for line in lines if line.startswith("Udp:")]

    if len(rows) < 2:
        return None

    fields = dict(zip(rows[0][1:], (int(v) for v in rows[1][1:])))

    return {
        key: fields[key]
        for key in ("InDatagrams", "InErrors", "RcvbufErrors", "NoPorts")
        if key in fields
    }


def _temperatures() -> Dict[str, float]:
    """
    Board temperatures, hottest reading per sensor (Linux / Jetson).
    """

    out: Dict[str, float] = {}

    try:
        readings = psutil.sensors_temperatures()
    except (AttributeError, OSError):
        readings = {}

    for name, entries in (readings or {}).items():
        values = [e.current for e in entries if e.current is not None]
        if values:
            out[name] = round(max(values), 1)

    # Jetson thermal zones, if psutil did not find them.
    if not out:
        for zone in sorted(Path("/sys/devices/virtual/thermal").glob("thermal_zone*")):
            try:
                label = (zone / "type").read_text().strip()
                out[label] = round(int((zone / "temp").read_text()) / 1000.0, 1)
            except (OSError, ValueError):
                continue

    return out


def _network() -> Dict[str, Any]:

    stats = psutil.net_if_stats()
    addresses = psutil.net_if_addrs()
    counters = psutil.net_io_counters(pernic=True)

    interfaces = []

    for name, st in sorted(stats.items()):

        if name.startswith(("lo", "utun", "awdl", "llw", "anpi", "gif", "stf", "bridge", "ap")):
            continue

        io = counters.get(name)

        interfaces.append({
            "name": name,
            "up": st.isup,
            "speed_mbps": st.speed or None,
            "mtu": st.mtu,
            "ipv4": [a.address for a in addresses.get(name, []) if a.family == socket.AF_INET],
            "rx_bytes": io.bytes_recv if io else None,
            "tx_bytes": io.bytes_sent if io else None,
            "rx_errors": io.errin if io else None,
            "rx_dropped": io.dropin if io else None,
        })

    return {
        "hostname": socket.gethostname(),
        "interfaces": interfaces,
        "udp": _udp_errors(),
    }


def detect_version() -> str:
    """
    What this unit is running: the image's build stamp if there is one,
    else the git commit of the checkout.
    """

    stamp = os.environ.get("OPENTRAFFIC_VERSION")

    if stamp:
        return stamp

    try:
        import subprocess
        return subprocess.run(
            ["git", "describe", "--always", "--dirty", "--tags"],
            cwd=Path(__file__).parent, capture_output=True, text=True, timeout=2,
        ).stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


class Health:
    """
    Builds the health report from the detector's live parts.
    """

    def __init__(
        self,
        settings,
        frames: FrameStats,
        buffer=None,
        background=None,
        controller=None,
        presence=None,
        link=None,
        store=None,
        version: str = "",
    ):
        self.settings = settings
        self.frames = frames
        self.buffer = buffer
        self.background = background
        self.controller = controller
        self.presence = presence
        self.link = link
        self.store = store
        self.version = version
        self.process = psutil.Process()
        self.process.cpu_percent(None)       # prime the CPU meter

    def zones_serial(self) -> Optional[str]:
        """
        The serial of the sensor the zones were saved with, if any.
        """

        if self.store is None:
            return None

        try:
            serial = (self.store.load().get("sensor") or {}).get("serial")
        except Exception:  # noqa: BLE001 - a bad zone file is reported elsewhere
            return None

        return str(serial) if serial else None

    def liveness(self) -> Dict[str, Any]:
        """
        The short form, safe to show without logging in.
        """
        report = self.report()
        return {"status": report["status"], "time": report["time"]}

    def report(self) -> Dict[str, Any]:

        problems: List[str] = []
        status = "ok"

        frames = self.frames.snapshot()
        age = frames["last_frame_age_s"]
        sensor_link = self.link.status() if self.link else None

        if age is None or age > FRAME_STALE_S:
            status = "down"
            problem = (
                "no LiDAR frames yet" if age is None
                else f"no LiDAR frame for {age:.0f} s"
            )
            if sensor_link and sensor_link["state"] != "streaming":
                problem += f": {sensor_link['message']}"
            problems.append(problem)

        def degrade(reason: str) -> None:
            nonlocal status
            if status == "ok":
                status = "degraded"
            problems.append(reason)

        # ---- detection
        background = self.background.status() if self.background else None

        if background is not None and not background["learned"]:
            degrade("background not learned: no calls are placed")

        # ---- the same sensor the unit was set up with?
        serial = sensor_link.get("serial") if sensor_link else None

        if serial:

            learned_with = (background or {}).get("sensor_serial")

            if background and background["learned"] and learned_with and learned_with != serial:
                degrade(
                    f"sensor changed: the background was learned with sensor "
                    f"{learned_with}, this is {serial} -- relearn it"
                )

            drawn_for = self.zones_serial()

            if drawn_for and drawn_for != serial:
                degrade(
                    f"sensor changed: the zones were drawn for sensor "
                    f"{drawn_for}, this is {serial} -- check them against "
                    f"the cloud and save"
                )

        # ---- controller link
        controller = None

        if self.controller is not None:

            st = self.controller.status()
            link = st.get("link") or {}
            now = time.time()

            reply_age = now - link["last_reply"] if link.get("last_reply") else None
            poll_age = now - link["last_poll"] if link.get("last_poll") else None
            phases = st.get("phases") or {}

            controller = {
                "kind": st["controller"]["kind"],
                "target": st["controller"].get("target"),
                "calls_sent": st["sent"],
                "calls_failed": st["failed"],
                "active_calls": [c["channel"] for c in st["calls"] if c["occupied"]],
                "link_reply_age_s": round(reply_age, 1) if reply_age is not None else None,
                "controller_poll_age_s": round(poll_age, 1) if poll_age is not None else None,
                "biu_enabled": link.get("biu_enabled"),
                "signals_live": bool(phases) and not phases.get("stale", False),
            }

            if st.get("setup_error"):
                degrade(f"controller: {st['setup_error']}")

            if st["controller"]["kind"] != "simulator":

                if reply_age is None or reply_age > CONTROLLER_STALE_S:
                    degrade("no reply from the SDLC adapter")

                if poll_age is None or poll_age > CONTROLLER_STALE_S:
                    degrade("controller is not polling the detector BIU")

                if not controller["signals_live"]:
                    degrade("no signal state from the controller")

        # ---- system
        memory = psutil.virtual_memory()
        disk_path = Path(self.settings.data_dir)
        disk = shutil.disk_usage(disk_path if disk_path.exists() else ".")
        disk_pct = 100.0 * disk.used / disk.total
        temps = _temperatures()

        if memory.percent > MEMORY_PCT:
            degrade(f"memory {memory.percent:.0f}% used")

        if disk_pct > DISK_PCT:
            degrade(f"disk {disk_pct:.0f}% used")

        hot = {k: v for k, v in temps.items() if v >= TEMP_C}
        if hot:
            degrade("hot: " + ", ".join(f"{k} {v} C" for k, v in hot.items()))

        network = _network()

        with self.process.oneshot():
            process = {
                "pid": self.process.pid,
                "cpu_percent": round(self.process.cpu_percent(None), 1),
                "rss_mb": round(self.process.memory_info().rss / 2**20, 1),
                "threads": self.process.num_threads(),
            }

        try:
            load = [round(v, 2) for v in os.getloadavg()]
        except OSError:
            load = None

        return {
            "status": status,
            "problems": problems,
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "device": {
                "hostname": network["hostname"],
                "profile": self.settings.profile,
                "version": self.version,
                "platform": platform.platform(),
                "uptime_s": round(time.time() - psutil.boot_time()),
                "detector_uptime_s": round(time.time() - self.frames.started),
            },
            "sensor": {
                "source": self.settings.source,
                **frames,
                "link": sensor_link,
            },
            "detection": {
                "background": background,
                "zones": self.presence.status() if self.presence else [],
                "buffer": self.buffer.stats() if self.buffer else None,
            },
            "controller": controller,
            "system": {
                "cpu_percent": psutil.cpu_percent(None),
                "cpu_count": psutil.cpu_count(),
                "load_average": load,
                "memory": {
                    "total_mb": round(memory.total / 2**20),
                    "available_mb": round(memory.available / 2**20),
                    "percent": memory.percent,
                },
                "disk": {
                    "path": str(disk_path),
                    "total_gb": round(disk.total / 2**30, 1),
                    "free_gb": round(disk.free / 2**30, 1),
                    "percent": round(disk_pct, 1),
                },
                "temperatures_c": temps,
            },
            "process": process,
            "network": network,
        }
