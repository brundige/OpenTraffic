from __future__ import annotations

import time
from pathlib import Path
from typing import Iterator

from ouster.sdk import core
from ouster.sdk import open_source

from .base import Lidar
from .types import PointCloudFrame


class OusterLidar(Lidar):

    def __init__(
        self,
        source: str,
        lidar_port: int = 7502,
        imu_port: int = 7503,
        frame_timeout: float = 10.0,
    ):
        self.source_url = source
        self.frame_number = 0

        # A source that exists on disk is a recording (.pcap / .osf /
        # .bag); anything else is a live sensor hostname or IP.
        is_recording = Path(source).exists()

        if is_recording:
            print(f"Reading recording: {source}")
        else:
            print(f"Connecting to Ouster: {source}")

        # ouster-sdk 1.x: open_source configures the sensor (including
        # the UDP destination) and starts streaming.
        # collate=False + sensor_idx=0 gives a single-sensor stream.
        options = {}

        if not is_recording:
            options = {
                "lidar_port": lidar_port,
                "imu_port": imu_port,
                "timeout": frame_timeout,
            }

        self.source = open_source(
            source,
            collate=False,
            sensor_idx=0,
            **options,
        )

        self.sensor_info = self.source.sensor_info[0]

        self.xyz_lut = core.XYZLut(self.sensor_info)

        self.sensor_name = self.sensor_info.prod_line

        print(f"Connected: {self.sensor_name}")
        print(f"Serial: {self.sensor_info.sn}")
        print(
            f"Resolution: "
            f"{self.sensor_info.h} x "
            f"{self.sensor_info.w}"
        )

    def describe(self) -> dict:
        """
        Sensor identity, for the inspector's status endpoint and for
        stamping the zone file.
        """

        return {
            "prod_line": self.sensor_name,
            "serial": str(self.sensor_info.sn),
            "height": self.sensor_info.h,
            "width": self.sensor_info.w,
            "source": self.source_url,
        }

    def frames(self) -> Iterator[PointCloudFrame]:

        if self.source is None:
            return

        for frame_set in self.source:

            for frame in frame_set:

                if frame is None:
                    continue

                # RANGE is the fundamental LiDAR measurement.
                range_field = frame.field(core.ChanField.RANGE)

                # Convert the range image to Cartesian XYZ.
                xyz = self.xyz_lut(range_field)

                intensity = None

                if frame.has_field(core.ChanField.REFLECTIVITY):
                    intensity = frame.field(core.ChanField.REFLECTIVITY)

                # Use the LiDAR timestamp when available; an empty
                # frame has no valid packet to take it from.
                try:
                    timestamp = (
                        float(frame.get_first_valid_packet_timestamp())
                        / 1_000_000_000.0
                    )
                except RuntimeError:
                    timestamp = time.time()

                yield PointCloudFrame(
                    xyz=xyz,
                    timestamp=timestamp,
                    intensity=intensity,
                    sensor_name=self.sensor_name,
                    frame_number=self.frame_number,
                )

                self.frame_number += 1

    def close(self) -> None:

        if self.source is not None:
            self.source.close()
            self.source = None
