OS-1
  │
  │ Ethernet
  ▼
Jetson
  │
  ▼
Ouster SDK
  │
  ▼
LidarFrame
  │
  ▼
PointCloudFrame
  │
  ▼
YOUR APPLICATION

PROFILES
========

Two environments, selected by profile (config/profiles.yaml):

  dev   MacBook, wired to the sensor, run natively
  prod  Jetson, wired to the sensor, run in Docker

Dev (macOS) -- native, never Docker:

    python main.py                      # dev is the default profile
    python main.py --source rec.osf     # replay a recording instead

Docker Desktop on macOS gives a container the Linux VM's network, not
the Mac's interfaces. The sensor's UDP stream lands on the Mac and the
container times out with "No valid frames received". There is no
container-side fix; run natively for dev.

Prod (Jetson) -- Docker with host networking:

    docker compose build
    docker compose up -d
    docker compose logs -f

network_mode: host is required so the container shares the interface
the sensor streams to. It behaves correctly on Linux.

The image carries its own glibc (Debian bookworm), so the SDK's
manylinux_2_28 aarch64 wheels install even on a JetPack 4.x host whose
Ubuntu 18.04 userland is too old for a direct pip install.

Raise the UDP receive buffer on the Jetson, or packets will drop at
~127 Mbit/s (OS-1-128 at 10 Hz). With host networking this is a host
setting, not a container one:

    echo 'net.core.rmem_max=8388608' | sudo tee /etc/sysctl.d/60-opentraffic.conf
    sudo sysctl --system

Overrides, without editing the profile:

    --profile / --source                 command line
    OPENTRAFFIC_PROFILE                  which profile to load
    OPENTRAFFIC_SOURCE, _LIDAR_PORT,     any single setting
    _IMU_PORT, _FRAME_TIMEOUT,
    _STATS_INTERVAL, _DATA_DIR


CLOUD INSPECTOR
===============

The inspector is a point-cloud page, behind a login, so the operator
can see the actual cloud and draw detection zones against it. In dev it
runs inside the detector; on the Jetson it is an on-demand sidecar.
See README.md for how it is deployed and secured.

    dev     http://127.0.0.1:8080/
    prod    http://<jetson-ip>:8080/

The camera is a movable orthographic orbit: top-down for laying out
zones, tilted to judge height and see what the sensor actually hits.
Orthographic, not perspective, so a metre stays a metre anywhere on
screen and the range rings keep meaning something.

    drag                 orbit          wheel        zoom
    shift-drag           pan            right-drag   pan
    Top / Tilt / Side    presets        Rotate/Tilt  sliders
    Fit                  frame cloud    Reset        top-down, origin

    + ROI / + Exclusion  start a zone   click        place a point
    Enter / double-click close a zone   Backspace    undo a point
    Esc                  cancel         Delete       remove selected
    drag a vertex        reshape a zone

Zone points always land on the GROUND Z plane, whatever the camera
angle: the click is unprojected onto that plane, so a zone drawn from
a tilted view is the same zone drawn from above. On a pole mount the
sensor is the origin and the road is several metres below it, so set
ground z to the road (the z min slider helps you find it) before
drawing. New zones get z_min = ground z and z_max = ground z + 5.

Tilted views draw each zone as a prism between z_min and z_max, which
is the only way to see what those numbers mean. Near vertical the
ground plane goes edge-on and point placement is refused rather than
being wildly wrong; the Side preset stops just short of that.

Colour by height, intensity or range; the z min / z max sliders slice
the cloud, which is how you find the ground plane and drop overhead
returns. "Live" re-fetches a frame every second, with the colour scale
held steady so the scene does not flicker between frames.

ROADWAY
=======

A pole-mounted sensor is never level with the road it watches, so the
roadway is a tilted plane in sensor XYZ, not a constant z. Over a 40 m
approach at 4 degrees that is 2.8 m of z: a fixed z band drawn at the
stop line sits underground at the far end of the same zone.

So the roadway is fitted and stored, and every zone height is measured
from it. "Fit roadway" in the inspector fits the newest frame; select
a zone first to fit only inside that outline, which is what you want
when the sensor sees more roof and kerb than tarmac. Same thing over
HTTP, on the unit itself (the detector API is loopback-only):

    curl -X POST http://127.0.0.1:8081/api/roadway
    curl -X POST -H 'Content-Type: application/json' \
         -d '{"polygon": [[5,-8],[40,-8],[40,8],[5,8]]}' \
         http://127.0.0.1:8081/api/roadway

RANSAC first, because a scene is mostly not road -- vehicles, kerbs,
poles and foliage would drag a plain least-squares fit off the
surface -- then least squares over the inliers. The fit reports its
inlier count and rms so a bad one is obvious; on a clean surface
expect rms near the sensor's own noise, a couple of centimetres.

Plane.height(points) gives signed metres above the road, which is what
perception should threshold on. Without a roadway the plane defaults
to the sensor's own horizontal through the origin, so height is just
z and nothing breaks before the first fit.


ZONES
=====

Zones are written to zones_file as YAML, in sensor-frame metres:

    version: 2
    updated: '2026-09-03T19:53:35Z'
    sensor:
      prod_line: OS-1-128
      serial: '122450000095'
    roadway:                        # the road surface in sensor XYZ,
      normal: [0.069758, -0.000017, 0.997564]   # normal . p + offset = 0
      offset: 6.7999                # so the sensor is 6.8 m above it
      sensor_height: 6.8
      tilt_deg: 4.0
      fit:
        inliers: 70163
        rms: 0.0205
        source: fitted 2026-09-03T19:53:35Z
    zones:
    - id: d738e016
      name: North approach
      type: roi                     # roi | exclusion
      polygon:
      - [5.0, -4.0]
      - [40.0, -4.0]
      - [40.0, 4.0]
      height_min: 0.2               # metres ABOVE THE ROADWAY,
      height_max: 4.5               # not sensor z
      enabled: true

The footprint stays sensor-frame x/y; only the vertical is measured
from the road. A v1 JSON document is still read if no YAML exists --
z_min/z_max carry over as height_min/height_max and the next save
writes YAML.

The server validates every document before writing it and writes
atomically, so a bad request or a power cut cannot leave the field
unit with a broken zone file. Zone.contains(points, roadway) returns
a mask for an (N, 3) array, for perception to filter on.

Because zones are sensor-frame, they are only valid for the mounting
they were drawn against. Re-draw them if the sensor is moved.

ROLLING BUFFER
==============

The last retain_seconds of frames are always held in memory, so the
30 s leading up to something interesting can be written out after the
fact rather than only after someone thinks to hit record.

    Save last 30 s        in the inspector
    POST /api/clip        same thing, scriptable

Clips land in clip_dir as .npz and replay through the whole pipeline:

    python main.py --source data/clips/clip_20260903_143910.npz
    python main.py --source <clip> --realtime    # at 10 Hz, watchable
                                                 # in the inspector

Memory is the constraint, not disk. The pipeline's own float64 cloud
is 3.1 MB a frame, so 30 s of it would be 944 MB. Points with no
return are dropped and the rest are quantised to centimetres in an
int16, which is 8 bytes a point:

    944 MB   xyz float64, every pixel      (what the pipeline holds)
    551 MB   xyz float32 + intensity, every pixel
    217 MB   int16 cm + intensity, returns only   <- what is kept

Quantisation costs at most 5 mm, against the sensor's own +-30 mm
range accuracy, and +-327 m of headroom is well past the OS-1's
120 m. Compacting costs ~4 ms a frame out of the 100 ms budget.

A dense scene costs more per frame than a sparse one, so the window
is capped in bytes as well (retain_max_mb, 256 MB). When the cap
bites, the buffer holds less than retain_seconds and says so, in the
inspector and in /api/status - it never silently keeps less than it
claims. Raise the cap or lower the seconds to suit the unit.

The inspector needs a login (python auth.py set-password); the
detector's own API has none and so binds to loopback only. See
README.md, Security.
