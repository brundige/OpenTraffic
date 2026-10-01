from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from perception.ground import Plane
from zones import Zone, ZoneStore


class PresenceDetector:
    """
    Turns zone occupancy into detector calls.

    An enabled ROI with a channel is occupied when at least
    min_points returns sit inside it, above the roadway and outside
    every exclusion zone. It clears only once the count falls below
    half that, so a vehicle on the threshold does not chatter the
    call on and off every frame.

    The call is placed only once the zone has been occupied for
    call_delay seconds without a break, so something passing through
    does not call the phase; it drops as soon as the zone clears.

    The zone file is re-read when it changes on disk, so zones saved
    in the inspector take effect without a restart.
    """

    def __init__(
        self,
        store: ZoneStore,
        min_points: int = 20,
        call_delay: float = 3.0,
    ):
        self.store = store
        self.min_points = max(1, int(min_points))
        self.call_delay = max(0.0, float(call_delay))

        self._lock = threading.Lock()
        self._stamp = None
        self._roadway = Plane.horizontal()
        self._calling: List[Zone] = []
        self._exclusions: List[Zone] = []
        self._present: Dict[int, bool] = {}
        self._present_since: Dict[int, float] = {}
        self._occupied: Dict[int, bool] = {}
        self._dropped: List[int] = []

    def status(self) -> List[Dict[str, Any]]:
        """
        Each calling zone's state, for the inspector: whether it is
        occupied, how long it has been, and whether the call is on.
        """

        now = time.monotonic()

        with self._lock:

            return [
                {
                    "channel": zone.channel,
                    "name": zone.name,
                    "present": self._present.get(zone.channel, False),
                    "waited": round(
                        now - self._present_since[zone.channel], 1
                    ) if self._present.get(zone.channel) else 0.0,
                    "delay": self.call_delay,
                    "calling": self._occupied.get(zone.channel, False),
                }
                for zone in sorted(self._calling, key=lambda z: z.channel)
            ]

    def _reload(self) -> None:

        stamp = tuple(
            path.stat().st_mtime if path.exists() else None
            for path in (self.store.path, self.store.legacy_path)
        )

        if stamp == self._stamp:
            return

        self._stamp = stamp

        document = self.store.load()

        roadway = document.get("roadway")

        self._roadway = Plane.from_dict(roadway) if roadway else Plane.horizontal()

        zones = [Zone.from_dict(raw, i) for i, raw in enumerate(document["zones"])]

        self._calling = [
            zone for zone in zones
            if zone.enabled and zone.type == "roi" and zone.channel
        ]

        self._exclusions = [
            zone for zone in zones
            if zone.enabled and zone.type == "exclusion"
        ]

        # A channel whose zone was removed or disabled drops its call.
        live = {zone.channel for zone in self._calling}

        for channel in list(self._occupied):
            if channel not in live:
                self._present.pop(channel, None)
                self._present_since.pop(channel, None)
                if self._occupied.pop(channel):
                    self._dropped.append(channel)

    def update(
        self,
        xyz: np.ndarray,
        now: Optional[float] = None,
    ) -> List[Tuple[int, bool, str]]:
        """
        Feed one frame's points; returns the calls that changed, as
        (channel, occupied, zone name).

        now is the frame's arrival time on a monotonic clock, which is
        what call_delay is measured on.
        """

        if now is None:
            now = time.monotonic()

        with self._lock:
            return self._update(xyz, now)

    def _update(self, xyz: np.ndarray, now: float) -> List[Tuple[int, bool, str]]:

        self._reload()

        changes = [(channel, False, "") for channel in self._dropped]
        self._dropped = []

        if not self._calling:
            return changes

        points = xyz

        for zone in self._exclusions:
            points = points[~zone.contains(points, self._roadway)]

        for zone in self._calling:

            channel = zone.channel

            count = int(zone.contains(points, self._roadway).sum())

            was_present = self._present.get(channel, False)

            present = (
                count >= self.min_points / 2 if was_present
                else count >= self.min_points
            )

            self._present[channel] = present

            if present and not was_present:
                self._present_since[channel] = now

            calling = (
                present
                and now - self._present_since[channel] >= self.call_delay
            )

            if calling != self._occupied.get(channel):
                changes.append((channel, calling, zone.name))

            self._occupied[channel] = calling

        return changes
