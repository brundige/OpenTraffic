import argparse
import signal
import sys
import time
import numpy as np
from api import DetectorApi, HealthServer
from auth import AuthStore
from controllers import ControllerSlot, make_controller
from health import FrameStats, Health, detect_version
from perception.background import BackgroundModel
from perception.presence import PresenceDetector
from recording import RollingBuffer
from sensors import SensorLink
from settings import load_settings
from zones import ZoneStore


running = True

lidar = None


def shutdown_handler(signum, frame):
    global running
    running = False

    # The sensor link may be waiting to retry rather than in the frame
    # loop; wake it so the detector stops promptly.
    if lidar is not None:
        lidar.stop()


def main():

    global lidar

    parser = argparse.ArgumentParser(
        description="OpenTraffic LiDAR detector"
    )

    parser.add_argument(
        "--profile",
        default=None,
        help=(
            "profile from config/profiles.yaml "
            "(default: $OPENTRAFFIC_PROFILE, else the file's "
            "default_profile)"
        ),
    )

    parser.add_argument(
        "--source",
        default=None,
        help=(
            "override the profile's source: an Ouster hostname or IP, "
            "'auto' to find the sensor on the network, "
            "or a path to a .npz / .pcap / .osf / .bag recording"
        ),
    )

    parser.add_argument(
        "--realtime",
        action="store_true",
        help=(
            "replay a clip at its recorded rate instead of as fast as "
            "possible, so it can be watched in the cloud inspector"
        ),
    )

    args = parser.parse_args()

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    servers = []
    controller = None
    total_frames = 0

    try:

        settings = load_settings(args.profile, source=args.source)

        print("=" * 60)
        print("OPENTRAFFIC")
        print("Live LiDAR ingestion")
        print(f"Profile: {settings.profile}")
        print("=" * 60)

        # Everything starts before the sensor, and keeps running while
        # there is none: if the LiDAR is unplugged, still booting or
        # somewhere unexpected, the health endpoint and the inspector
        # are how anyone finds out.
        frame_stats = FrameStats()
        health = Health(settings, frame_stats, version=detect_version())

        health_server = HealthServer(
            health,
            AuthStore(settings.auth_file),
            host=settings.health_host,
            port=settings.health_port,
        )
        health_server.start()
        servers.append(health_server)

        print(f"Health: http://{settings.health_host}:{settings.health_port}/healthz")

        recent = RollingBuffer(
            seconds=settings.retain_seconds,
            max_bytes=settings.retain_max_mb << 20,
        )

        store = ZoneStore(settings.zones_file)

        controller = ControllerSlot(settings, make_controller)

        presence = PresenceDetector(
            store,
            min_points=settings.detector_min_points,
            call_delay=settings.detector_call_delay,
        )

        background = BackgroundModel(
            settings.background_file,
            voxel=settings.background_voxel,
            learn_seconds=settings.background_learn_seconds,
            sensor_serial=lambda: lidar.describe().get("serial"),
        )

        # The sensor the unit was set up with, to pick it out if the
        # search finds more than one.
        lidar = SensorLink(
            settings.source,
            interface=settings.sensor_interface,
            lidar_port=settings.lidar_port,
            imu_port=settings.imu_port,
            frame_timeout=settings.frame_timeout,
            realtime=args.realtime,
            expected_serial=lambda: (
                background.status()["sensor_serial"] or health.zones_serial()
            ),
        )

        if not background.learned:
            # No calls until it has learned: a zone would otherwise
            # count the road it is drawn on.
            background.learn()

        empty = np.zeros((0, 3), dtype=np.float32)

        print()
        print(f"Controller: {controller.describe().get('name', settings.controller)}")

        if background.learned:
            print(f"Background: {settings.background_file} "
                  f"(learned {background.status()['learned_at']})")
        else:
            print(f"Background: learning for {settings.background_learn_seconds:.0f} s "
                  f"-- keep the view clear; no calls until it is done")

        print(
            f"Retaining last {settings.retain_seconds:.0f} s in memory "
            f"(cap {settings.retain_max_mb} MB)"
        )

        health.buffer = recent
        health.background = background
        health.controller = controller
        health.presence = presence
        health.link = lidar
        health.store = store

        api = DetectorApi(
            recent,
            store,
            host=settings.api_host,
            port=settings.api_port,
            max_points=settings.cloud_max_points,
            describe=lidar.describe,
            clip_dir=settings.clip_dir,
            controller=controller,
            presence=presence,
            background=background,
            health=health,
        )
        api.start()
        servers.append(api)

        print(f"Detector API: {api.url} (loopback only)")

        if settings.inspector_embedded:

            from inspector import InspectorServer

            inspector = InspectorServer(
                settings.auth_file,
                upstream=(settings.api_host, settings.api_port),
                host=settings.inspector_host,
                port=settings.inspector_port,
                session_hours=settings.session_hours,
            )
            inspector.start()
            servers.append(inspector)

            print(f"Inspector: {inspector.url}")

            if not AuthStore(settings.auth_file).configured:
                print("  No login set yet: run 'python auth.py set-password'")

        print(f"Zones: {settings.zones_file}")
        print(f"Clips: {settings.clip_dir}")

        print()
        print(f"Sensor: {settings.source}"
              + (f" on {settings.sensor_interface}" if settings.sensor_interface else ""))
        print()

        start_time = time.monotonic()
        frames = 0

        for frame in lidar.frames():

            if not running:
                break

            frames += 1
            total_frames += 1

            snapshot = recent.publish(frame)

            frame_stats.frame(snapshot.points)

            points = snapshot.xyz

            moving = background.update(
                points, snapshot.frame_number, snapshot.received
            )

            changes = presence.update(
                points[moving] if background.learned else empty,
                now=snapshot.received,
            )

            for channel, occupied, label in changes:
                controller.set_detector(channel, occupied, label)

            elapsed = time.monotonic() - start_time

            if elapsed >= settings.stats_interval:

                fps = frames / elapsed

                points = frame.flat_xyz

                valid = np.isfinite(points).all(axis=1)
                valid_points = points[valid]

                if valid_points.size:
                    xyz_min = valid_points.min(axis=0)
                    xyz_max = valid_points.max(axis=0)
                else:
                    xyz_min = np.array([np.nan, np.nan, np.nan])
                    xyz_max = np.array([np.nan, np.nan, np.nan])

                print(
                    f"frames={frames:4d} "
                    f"fps={fps:5.1f} "
                    f"points/frame={frame.point_count:7d} "
                    f"valid={valid_points.shape[0]:7d}"
                )

                print(
                    f"  X: {xyz_min[0]:8.2f} -> {xyz_max[0]:8.2f} m"
                )

                print(
                    f"  Y: {xyz_min[1]:8.2f} -> {xyz_max[1]:8.2f} m"
                )

                print(
                    f"  Z: {xyz_min[2]:8.2f} -> {xyz_max[2]:8.2f} m"
                )

                print()

                frames = 0
                start_time = time.monotonic()

    except Exception as exc:

        print()
        print("ERROR:")
        print(exc)

        return 1

    finally:

        for server in reversed(servers):
            server.stop()

        if lidar is not None:
            lidar.close()

        if controller is not None:
            controller.close()

        print()
        print(f"LiDAR stopped after {total_frames} frames.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
