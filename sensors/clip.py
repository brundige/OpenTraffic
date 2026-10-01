from __future__ import annotations

import time
from pathlib import Path
from typing import Iterator

from recording import load_clip

from .base import Lidar
from .types import PointCloudFrame


class ClipLidar(Lidar):
    """
    Replays a .npz clip written by the rolling buffer.

    Same interface as the live sensor, so an incident recorded in the
    field runs back through the detector unchanged.
    """

    def __init__(self, source: str, realtime: bool = False):
        self.source_url = str(source)
        self.realtime = realtime

        print(f"Reading clip: {source}")

        clip = load_clip(Path(source))

        self.meta = clip["meta"]
        self._frames = clip["frames"]

        self.sensor_name = (self.meta.get("sensor") or {}).get(
            "prod_line", "clip"
        )

        print(
            f"Clip: {self.meta['frames']} frames, "
            f"{self.meta['seconds']} s, "
            f"recorded {self.meta['saved']}"
        )

    def describe(self) -> dict:

        sensor = dict(self.meta.get("sensor") or {})
        sensor["source"] = self.source_url

        return sensor

    def frames(self) -> Iterator[PointCloudFrame]:

        previous = None

        for index, frame in enumerate(self._frames):

            if self.realtime and previous is not None:
                delay = frame["timestamp"] - previous
                if 0 < delay < 5:
                    time.sleep(delay)

            previous = frame["timestamp"]

            yield PointCloudFrame(
                xyz=frame["xyz"],
                timestamp=frame["timestamp"],
                intensity=frame["intensity"],
                sensor_name=self.sensor_name,
                frame_number=frame["frame_number"],
            )

    def close(self) -> None:
        self._frames = []
