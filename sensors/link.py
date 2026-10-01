"""
Keeps the detector attached to its LiDAR.

The detector used to connect to the sensor before anything else, and
exit if it could not -- so a unit whose sensor was unplugged, still
booting, or at an unexpected address had no inspector and no health
detail to say why. SensorLink makes the sensor something the detector
waits for instead: it finds the sensor (or uses the configured
address), streams from it, and when the stream stops it reconnects,
while everything else keeps running and reports what it is doing.

Recordings are the exception: they are opened once and end when they
end, as before.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from .base import Lidar
from .clip import ClipLidar
from .discovery import find_sensors
from .ouster import OusterLidar
from .types import PointCloudFrame


AUTO = "auto"

# Seconds between attempts: quick at first, then every 10 s.
RETRY_DELAYS = (2.0, 2.0, 5.0, 10.0)


def _explain(exc: BaseException) -> str:
    """
    The SDK's error, plus what usually causes it.
    """

    text = " ".join(str(exc).split())

    if "failed to obtain a UDP socket" in text or "udp bind" in text:
        return (text + " -- another program on this machine is using the "
                "LiDAR port; only one can receive the sensor's stream")

    if "No valid frames received" in text:
        return (text + " -- the sensor answered but no LiDAR packets arrived; "
                "check the sensor is wired to this machine and nothing "
                "filters UDP")

    return text


class SensorLink:

    def __init__(
        self,
        source: str,
        interface: str = "",
        lidar_port: int = 7502,
        imu_port: int = 7503,
        frame_timeout: float = 10.0,
        realtime: bool = False,
        expected_serial=None,
    ):
        self.source = source
        self.interface = interface
        self.lidar_port = lidar_port
        self.imu_port = imu_port
        self.frame_timeout = frame_timeout
        self.realtime = realtime

        # Called for the serial the unit was set up with, to choose
        # between several sensors found at once.
        self._expected_serial = expected_serial or (lambda: None)

        self.recording = source.endswith(".npz") or Path(source).exists()

        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._lidar: Optional[Lidar] = None
        self._sensor: Dict[str, Any] = {}

        self._state = "starting"
        self._address: Optional[str] = None
        self._error: Optional[str] = None
        self._since = time.time()
        self._attempts = 0
        self._connects = 0

    # ------------------------------------------------------------ state

    def _set(self, state: str, error: Optional[str] = None) -> None:
        with self._lock:
            if state != self._state:
                self._since = time.time()
            self._state = state
            self._error = error

    def describe(self) -> Dict[str, Any]:
        """
        The sensor in use, or the last one connected; {} before any.
        """
        with self._lock:
            return dict(self._sensor)

    def status(self) -> Dict[str, Any]:

        with self._lock:

            if self._state == "searching":
                where = self.interface or "all interfaces"
                message = f"searching for an Ouster sensor on {where}"
            elif self._state == "connecting":
                message = f"connecting to the sensor at {self._address}"
            elif self._state == "streaming":
                message = f"streaming from {self._address}"
            elif self._state == "retrying":
                message = f"no sensor: {self._error}"
            else:
                message = self._state

            return {
                "state": self._state,
                "message": message,
                "configured": self.source,
                "address": self._address,
                "interface": self.interface or None,
                "error": self._error,
                "since": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._since)),
                "attempts": self._attempts,
                "connects": self._connects,
                "serial": self._sensor.get("serial"),
                "prod_line": self._sensor.get("prod_line"),
            }

    # ---------------------------------------------------------- connect

    def _resolve(self) -> str:
        """
        The address to connect to: the configured one, or, for "auto",
        the sensor found on the network.
        """

        if self.source.strip().lower() != AUTO:
            return self.source

        self._set("searching")

        found = find_sensors(self.interface)

        if not found:
            where = f"interface {self.interface}" if self.interface else "any interface"
            raise RuntimeError(
                f"no Ouster sensor answered on {where}; check its power "
                f"and cable (it takes about a minute to boot)"
            )

        if len(found) > 1:

            expected = self._expected_serial()
            match = [s for s in found if s.serial == expected]

            if len(match) == 1:
                return match[0].address

            raise RuntimeError(
                "more than one sensor found ("
                + "; ".join(s.label() for s in found)
                + "): set sensor_interface to the LiDAR port, or source "
                "to one address"
            )

        sensor = found[0]

        print(f"Found {sensor.label()}")

        return sensor.address

    def _open(self) -> Lidar:

        if self.recording:
            lidar: Lidar = ClipLidar(self.source, realtime=self.realtime) \
                if self.source.endswith(".npz") else \
                OusterLidar(self.source, frame_timeout=self.frame_timeout)
            address = self.source
        else:
            address = self._resolve()
            with self._lock:
                self._address = address
            self._set("connecting")
            lidar = OusterLidar(
                address,
                lidar_port=self.lidar_port,
                imu_port=self.imu_port,
                frame_timeout=self.frame_timeout,
            )

        with self._lock:
            self._lidar = lidar
            self._address = address
            self._sensor = lidar.describe() if hasattr(lidar, "describe") else {}
            self._connects += 1

        return lidar

    # ----------------------------------------------------------- frames

    def frames(self) -> Iterator[PointCloudFrame]:
        """
        Frames for as long as the detector runs, reconnecting whenever
        the sensor goes away. A recording yields its frames once.
        """

        failures = 0

        while not self._stop.is_set():

            lidar = None

            try:

                self._attempts += 1

                lidar = self._open()

                for frame in lidar.frames():

                    if failures or self._state != "streaming":
                        failures = 0
                        self._set("streaming")

                    yield frame

                    if self._stop.is_set():
                        return

                if self.recording:
                    return

                raise RuntimeError("the sensor stream ended")

            except Exception as exc:  # noqa: BLE001 - retry, never die

                if self.recording:
                    raise

                reason = _explain(exc)

                print(f"Sensor: {reason}")

                self._set("retrying", reason)

            finally:

                if lidar is not None:
                    with self._lock:
                        self._lidar = None
                    try:
                        lidar.close()
                    except Exception:  # noqa: BLE001
                        pass

            delay = RETRY_DELAYS[min(failures, len(RETRY_DELAYS) - 1)]
            failures += 1

            self._stop.wait(delay)

    def stop(self) -> None:
        self._stop.set()

    def close(self) -> None:

        self.stop()

        with self._lock:
            lidar, self._lidar = self._lidar, None

        if lidar is not None:
            lidar.close()
