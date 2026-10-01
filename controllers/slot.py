"""
The controller the detector places calls on, changeable while it runs.

Choosing the controller used to mean editing the profile or the
environment and restarting. The slot lets the inspector's setup guide
do it instead: it closes the current controller, builds the new one
from the changed settings, and puts back every call that is still
active, so a zone that is calling keeps calling across the change.
"""

from __future__ import annotations

import dataclasses
import threading
from typing import Any, Dict, Optional, Tuple

from .base import Controller
from .simulator import SimulatorController


class ControllerSlot:

    def __init__(self, settings, make):
        self._settings = settings
        self._make = make
        self._lock = threading.Lock()
        self._error: Optional[str] = None
        # The last state the detector asked for on each channel.
        self._wanted: Dict[int, Tuple[bool, str]] = {}
        self._current: Controller = self._build(settings)

    def _build(self, settings) -> Controller:
        """
        The controller for these settings, or -- if it cannot start, say
        its port is taken -- the simulator, with the reason kept for the
        health report. Detection, health and the inspector stay up
        either way, so the setting can be put right from the inspector.
        """

        try:
            controller = self._make(settings)
            self._error = None
            return controller
        except Exception as exc:  # noqa: BLE001
            self._error = f"{settings.controller} could not start: {exc}"
            print(f"Controller: {self._error}")
            return SimulatorController()

    @property
    def settings(self):
        return self._settings

    # -------------------------------------------------- Controller API

    def set_detector(self, channel: int, occupied: bool, label: str = "") -> bool:
        with self._lock:
            self._wanted[channel] = (occupied, label)
            return self._current.set_detector(channel, occupied, label)

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {**self._current.status(), "setup_error": self._error}

    @property
    def error(self) -> Optional[str]:
        return self._error

    def describe(self) -> Dict[str, Any]:
        with self._lock:
            return self._current.describe()

    def phases(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._current.phases()

    def link(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._current.link()

    def close(self) -> None:
        with self._lock:
            self._current.close()

    # ---------------------------------------------------------- change

    def reconfigure(self, **changes: Any) -> None:
        """
        Switch to the controller these settings describe.

        The old controller is closed first -- the EM-HDLC link binds
        fixed UDP ports, so the new one could not open alongside it. If
        the new one cannot be built, the old settings are put back and
        the error raised.
        """

        new_settings = dataclasses.replace(self._settings, **changes)

        with self._lock:

            self._current.close()

            try:
                self._current = self._make(new_settings)
                self._error = None
            except Exception:
                self._current = self._build(self._settings)
                raise
            finally:
                for channel, (occupied, label) in sorted(self._wanted.items()):
                    if occupied:
                        self._current.set_detector(channel, True, label)

            self._settings = new_settings

    def current(self) -> Controller:
        with self._lock:
            return self._current
