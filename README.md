# OpenTraffic

[![CI](https://github.com/brundige/OpenTraffic/actions/workflows/ci.yml/badge.svg)](https://github.com/brundige/OpenTraffic/actions/workflows/ci.yml)
[![Discord](https://img.shields.io/badge/Discord-join-5865F2?logo=discord&logoColor=white)](https://discord.gg/QGDazpAuNh)

Sensor-agnostic vehicle detection for signalised intersections. A
sensor on a pole watches the approaches; a Jetson in the signal
cabinet finds vehicles in operator-drawn zones and places detector calls
on the traffic controller over the cabinet's SDLC bus, through a Luxcom
EM-HDLC. The same link reads the controller's signal state back.

**Sensors.** Everything after the sensor — background, roadway fit,
zones, presence, calls and the inspector — works on frames of 3D points
and does not know which sensor produced them. A sensor adapter in
`sensors/` supplies those frames. The first adapter, written for bench
testing, is for an Ouster OS-1 LiDAR, alongside clip replay for developing
without hardware. The goal of the project is to expand the codebase to
work with any type of sensor — other LiDARs, radar, cameras and computer
vision — and new adapters are very welcome.

**Status.** Detector calls confirmed on a Siemens M60 (SEPAC 5.7.0.31)
over NTCIP, through a Luxcom EM-HDLC, with the OS-1 adapter. Field
testing at an intersection is next.

Deeper notes on the OS-1 adapter, the roadway fit, zones and the rolling
buffer are in [info.md](info.md).

> **Safety.** OpenTraffic places calls on a live traffic signal
> controller. It is provided without warranty (see [License](#license)).
> Test on a bench first, keep the cabinet's existing detection as a
> fallback, and deploy only with the agreement of the agency that owns
> the signal.

---

## Contents

1. [How it fits together](#how-it-fits-together)
2. [Production workflow](#production-workflow)
3. [Quick start on a Mac](#quick-start-on-a-mac)
4. [Setting up an intersection](#setting-up-an-intersection)
5. [Detection](#detection)
6. [The traffic controller link](#the-traffic-controller-link)
7. [Health monitoring](#health-monitoring)
8. [Deploying on the Jetson](#deploying-on-the-jetson)
9. [Reaching a unit over the city VPN](#reaching-a-unit-over-the-city-vpn)
10. [Security](#security)
11. [Configuration reference](#configuration-reference)
12. [Troubleshooting](#troubleshooting)
13. [Repository layout](#repository-layout)
14. [Testing](#testing)
15. [Contributing](#contributing)
16. [License](#license)

---

## How it fits together

```
 OS-1 LiDAR ──UDP 7502──►┌─────────────────────── Jetson ───────────────────────┐
  (pole top)             │                                                      │
                         │  detector  (always on)                               │
                         │   ingest → background → zones → 3 s presence → calls │
                         │   API      127.0.0.1:8081   loopback, no login      │
                         │   health   0.0.0.0:8090     /healthz, /health       │
                         │        │                                             │
                         │        │ UDP 10001 commands / 10002 forwarded SDLC   │
                         │        ▼                                             │
                         │  inspector (on demand)  127.0.0.1:8088               │
                         │   login, setup guide, point-cloud page               │
                         │        ▲                                             │
                         │  systemd socket 0.0.0.0:8080 ─ starts it on connect  │
                         └────────┼─────────────────────────────────────────────┘
                                  │
          Luxcom EM-HDLC ◄────────┘ Ethernet       operator browser ──► :8080
            │ SDLC (TS2 Port 1)                    monitoring       ──► :8090
            ▼
          M60 controller
```

| Port | Bound to | What | Login |
|---|---|---|---|
| 8080 | network | Inspector (systemd starts it on connect) | operator password |
| 8088 | loopback | Inspector process itself | — |
| 8081 | loopback | Detector API | none, so never exposed |
| 8090 | network | Health: `/healthz` open, `/health` token | bearer token for `/health` |
| 10001, 10002 | network (cabinet LAN) | EM-HDLC commands / forwarded SDLC | — |

**Why a sidecar.** The detector is the part that must never stop. The
inspector is only needed while someone sets up or checks a unit, so it
runs separately: systemd holds port 8080 open, starts the inspector on
the first connection, and stops it after 15 idle minutes. When nobody is
connected the GUI uses nothing, and a fault in it cannot touch detection.
(Measured on the bench, an idle in-process GUI was already close to free;
the sidecar's main gain is isolation and a closed attack surface.)

---

## Production workflow

1. **Set up.** Connect to `http://<unit>:8080/`, log in, and follow the
   setup guide (skippable; reopen with **Setup**). It walks through:
   sensor streaming, learning the background, fitting the roadway,
   placing the map, drawing detection zones with detector channels,
   exclusion zones, saving, connecting the controller (**Find** searches
   the cabinet network for the EM-HDLC), checking the link, and testing
   a call. Each step ticks itself off from the unit's live state. The
   sensor is found on its own; nothing is configured by hand.
2. **Leave it.** Close the browser. After 15 minutes the inspector stops;
   detection, calls and health carry on.
3. **Monitor.** Poll `http://<unit>:8090/healthz` from monitoring; pull
   `/health` with the token for detail.
4. **Come back.** Browse to `:8080` again; systemd starts the inspector
   in a few seconds and you log in as before.

---

## Quick start on a Mac

Dev runs natively (never in Docker on macOS: Docker Desktop cannot see
the Mac's interfaces, so sensor packets never reach a container).

```sh
conda activate OpenTraffic           # Python 3.11
pip install -r requirements.txt

python auth.py set-password          # once; username "operator"
python main.py                       # dev profile: everything in one process
```

Open <http://127.0.0.1:8080/> and log in. In dev the inspector runs
inside the detector and every listener is loopback-only.

**No sensor?** Replay a recording:

```sh
python main.py --source data/clips/<clip>.npz --realtime
```

**Bench network.** The OS-1 uses link-local addressing and is found
automatically. The EM-HDLC and controller have fixed addresses, so give
the Mac's wired port an address on each subnet (lost on reboot/unplug):

```sh
sudo ifconfig en0 alias 192.168.1.139 255.255.255.0   # EM-HDLC 192.168.1.124 forwards here
sudo ifconfig en0 alias 10.227.3.139 255.255.0.0      # M60 at 10.227.3.77 (NTCIP)
```

**Bench with a Jetson instead.** Nothing to alias. On the bench the
OS-1 and the EM-HDLC share the LiDAR port through a switch; open the
inspector, **Controller connection → Find**, and the EM-HDLC shows as
*set to send to 192.168.1.139, which no device has*. **Use it** and the
unit takes 192.168.1.139 itself (kept in `data/site.yaml`, re-added at
every start). Do not add that address to the port's NetworkManager
profile as well: then two things own it, and choosing the simulator
cannot give it back. Unplug the Mac or drop its alias first — two
machines answering as .139 would fight over the adapter's traffic.

---

## Setting up an intersection

The setup guide covers this in the page; the reasoning behind each step:

| Step | Why |
|---|---|
| Learn background | Defines the empty scene. Only points outside it count. Do it with the approach clear: anything still for most of the 20 s becomes background, people included. |
| Fit roadway | The road is a tilted plane in sensor coordinates; zone heights are measured from it. |
| Place map | Lat/lon of the sensor and the compass heading of its +x axis. Makes zones easy to draw against kerbs and lanes. Needs internet on the browser for OpenStreetMap tiles. |
| Detection zones | One ROI per detector, with the controller's detector channel (1–16). |
| Exclusion zones | Things that move but are not traffic. |
| Check controller | Adapter replying, controller polling, live signal channels. |
| Test a call | Stand in a zone: *waiting* → *CALL* after 3 s, and the controller shows the detector. |

Zones, roadway and map location live in `data/zones.yaml`; the
background in `data/background.npz`. Both are valid for this mounting
only — redo them if the sensor is moved. Both are stamped with the
sensor's serial number: if a different sensor is connected later,
health turns *degraded* with "sensor changed" until the background is
relearned and the zones are checked and saved again.

---

## Detection

**Background.** For a fixed sensor the static scene returns from the
same places every frame. While learning (20 s), points are binned into
0.2 m voxels; voxels occupied in ≥ 80 % of frames are background,
widened by one voxel so range noise does not show as movement. After
that, every point outside the background is *moving*.

It deliberately does **not** keep adapting. A model that slowly absorbs
whatever stands still would absorb a car waiting at a red light and drop
its call. Relearn instead (button, or `POST /api/background`) when the
scene really changes.

Measured: classifying a frame costs ~0.5 ms; a static room of 131 k
points per frame leaves ~10–16 moving points.

**Presence and calls.** An enabled ROI with a channel is *present* when
≥ `detector_min_points` moving points lie inside it, above the roadway
and outside every exclusion zone (it clears below half that, to avoid
chatter). The call goes out after `detector_call_delay` (3 s) of
continuous presence and drops as soon as the zone clears. Until the
first background is learned no calls are placed at all.

`detector_min_points` counts moving points; distant vehicles return
fewer, so far zones may need a lower value.

---

## The traffic controller link

The EM-HDLC (EMH_BIU firmware) emulates up to four NEMA TS2 detector
BIUs on the controller's SDLC bus. The detector talks to it over UDP.

**Commands** (port 10001), from Luxcom's published sample:

```
"LXBIU" 0x01 <command> <length> <payload>
  0x01 discover   0x02 set timeout (s)   0x03 enable BIU mask   0x0A update call data
reply: same header, command | 0x80, length 1, status (0x00 = ok)
```

The detector resends timeout / enable / call data every 0.5 s against a
5 s timeout. If the detector dies, the BIU times out and the controller
falls back to its own detector-failure behaviour — **check what yours
does (e.g. constant call) before deploying.**

**Call data** — a TS2 Type 148 response for BIU 1: `08 83 94` + 36 bytes.
Worked out on the bench M60 by setting bits and reading the result back
over NTCIP (`vehicleDetectorStatusGroupActive`):

| Byte | Meaning |
|---|---|
| 0–31 | no effect on calls (presumably per-detector timestamps); sent as 0 |
| 32 | detectors 1–8, one bit each, detector 1 = bit 0 |
| 33 | detectors 9–16 |
| 34–35 | sent as 0 |

**This is one
controller's reading of the frame, not the TS2 text** — confirm against
the spec or a real detector BIU before relying on it in the field.
BIUs 2–4 (detectors 17–64) are refused until verified the same way.

**Signal state (SPaT)** arrives as raw SDLC frames forwarded to port
10002. The load-switch frame (`13 83 00` + 13 bytes, every 100 ms) is
read as three 4-byte groups — green, yellow, red — with 2 bits per
channel, channel 1 lowest. Inferred from 4 minutes of live traffic: no
channel ever in two colours, every change in green → yellow → red order,
constant yellow times. These are **channels**; the channel→phase mapping
is the controller's programming.

---

## Health monitoring

The detector always serves health on port 8090, whether or not the
inspector is running.

```sh
curl http://<unit>:8090/healthz
# {"status": "ok", "time": "2026-10-01T12:53:02Z"}    HTTP 200 (503 when down)

python auth.py health-token          # on the unit; prints a token once
curl -H "Authorization: Bearer <token>" http://<unit>:8090/health
```

**Status**

| | Meaning |
|---|---|
| `down` | no LiDAR frame for 5 s — the unit is not detecting. The problem says what the sensor link is doing: searching, or why it could not connect |
| `degraded` | detecting, but: adapter not replying, controller not polling, no signal state, background not learned, memory or disk > 90 %, board ≥ 85 °C |
| `ok` | none of the above |

`problems` lists the reasons in plain words.

**`/health` sections:** `device` (hostname, profile, version, uptime),
`sensor` (fps, last frame age, points), `detection` (background, zones
with presence timers, rolling buffer), `controller` (calls sent/failed,
active calls, adapter reply age, controller poll age, signals live),
`system` (CPU, load, memory, disk, temperatures), `process` (detector CPU,
RSS, threads), `network` (interfaces with link state, speed, addresses,
byte/error/drop counters, and the kernel's UDP drop counters —
`RcvbufErrors` climbing means LiDAR packets are being lost).

The inspector's **Device** panel shows the same report.

---

## Deploying on the Jetson

**Hardware.** Parts list and compute options (Orin NX 16GB in a wide-
temperature fanless box): see the *OpenTraffic Cabinet Hardware Parts
List* document. It's worth noting, that I am developing this on an Jetson - in production, a commercial edge compute device is probably the way ! Given that the impact of device failure is somewhat low, its probably worth seeing how long it takes a $250 unit to fail -- since the commerical units are in the 5k range.


**Finding the sensor.** The prod profile has `source: auto`: the
detector asks for `_ouster-lidar._tcp` over mDNS on every interface,
checks each answer against the sensor's HTTP API, and connects to the
one it finds, so any OS-1 works without configuring its address. If
the sensor is missing, unplugged later or still booting, the detector
keeps running (health, inspector, controller keep-alive) and retries
every few seconds; `/health` and the Device panel say what it is
doing. If more than one sensor answers, it picks the one the unit was
set up with (by serial), or set `OPENTRAFFIC_SENSOR_INTERFACE` to the
LiDAR port. Set `OPENTRAFFIC_SOURCE` to an address to skip the search.

**JetPack.** Prefer JetPack 6 (Ubuntu 22.04): its systemd supports the
inspector's idle shutdown, and the Ouster SDK installs directly. The
current image is Debian-based to support JetPack 4 hosts; on JetPack 6 it
can move to an L4T base if GPU work is added later.

Root is needed once per unit, to prepare the host; everything after
that runs as the unit's user, with no sudo.

```sh
# 1. Code, owned by the user who runs it (any path works)
git clone https://github.com/brundige/OpenTraffic.git ~/opentraffic
cd ~/opentraffic

# 2. Once per unit, on the bench: Docker access and lingering for this
#    user, the UDP receive buffer, the LiDAR port's addresses, hostname
sudo LIDAR_IF=enP8p1s0 UNIT_NAME=ot-main-and-5th deploy/provision-host.sh
#    (log out and in if it just added you to the docker group)

# 3. Build, set the operator password, start the detector, install the
#    on-demand inspector on :8080 -- no sudo
deploy/install.sh

# 4. Optional: a token for the full /health report
docker compose run --rm detector python auth.py health-token
```

**Update:** `git pull && deploy/install.sh`. Zones, background, site
settings and credentials in `data/` survive.

**What runs where.** The detector is a Docker container that Docker
restarts at boot. The inspector is three systemd **user** units in
`~/.config/systemd/user` (port 8080 needs no root): a socket that holds
the port, a proxy started on the first connection, and the inspector
container, stopped again after 15 idle minutes. If the inspector dies,
the proxy goes with it and the next connection starts both again.
Lingering keeps them running with nobody logged in.

On JetPack 5 (systemd 245) remove `--exit-idle-time=15min` from
`deploy/systemd-user/opentraffic-inspector-proxy.service`; the inspector
then stays up after first use until
`systemctl --user stop opentraffic-inspector-proxy opentraffic-inspector`.

**Moving from the old system-wide units** (`deploy/install-inspector.sh`,
removed): `deploy/install.sh` prints the one `sudo` command that takes
them out, since they hold port 8080.

---

## Reaching a unit over the city VPN

If your trying to test this on your municipal infrastructure, you need each unit's address on the city network. Ask your city IT for:

1. **A fixed address per unit** — a DHCP reservation for the Jetson's
   cabinet-network port, or a static IP. A **DNS name** per unit
   (e.g. `ot-main-and-5th.signals.city.gov`) is better still.
2. **Firewall rules** allowing the VPN subnet to reach the units on TCP
   8080 (inspector), 8090 (health) and 22 (SSH, for maintenance) — and
   nothing from outside the city network.
3. If the cabinet network is separate from where the VPN lands, the route
   between them.

Keep an inventory of intersection → address → hostname. `/health`
reports the unit's hostname, profile and software version, which helps
confirm you are talking to the unit you think you are.

**Two network ports on the Jetson.** One dedicated to the LiDAR
(link-local, ~130 Mbit/s), one for the cabinet/city network (EM-HDLC,
VPN access). Do not bridge them. If you only have ethernet port on the jetson - I had success locally by connecting the SDLC, Lidar and Jetson to a "dumb" nwetwork switch. I used a Netgear GS3116PP which has the added advantage of POE.

---

## Security

- **Login.** One operator account per unit, set on the unit. Password:
  salted PBKDF2-SHA256 (600 000 iterations), minimum 10 characters.
  Sessions are random tokens in an HttpOnly, SameSite=Strict cookie,
  held in memory (8 h) — stopping the inspector logs everyone out.
  8 wrong passwords lock logins for 5 minutes.
- **Changes** need an `X-OpenTraffic: 1` header as well as the cookie,
  so a page on another site cannot make the browser change zones.
- **Headers.** Strict Content-Security-Policy (page, OpenStreetMap
  tiles, nothing else), no framing, no MIME sniffing.
- **Detector API** has no login and refuses to bind anything but
  loopback.
- **Health token** is stored only as a SHA-256 hash and shown once.
- **No TLS yet.** Traffic is plain HTTP, protected by the VPN. Before any
  exposure beyond the VPN, put TLS in front (e.g. a reverse proxy with a
  city certificate) and set the session cookie to `Secure`.
- `data/` is git-ignored: credentials, zones and recordings never go into
  the repository.

---

## Configuration reference

All settings live in `config/profiles.yaml` (`dev`, `prod`). Any of them
can be overridden with `OPENTRAFFIC_<NAME>`, e.g.
`OPENTRAFFIC_SOURCE=rec.npz`. Selected:

| Setting | dev | prod | |
|---|---|---|---|
| `source` | 169.254.151.172 | auto | sensor address, `auto`, or a recording |
| `sensor_interface` | — | "" | limit the `auto` search to one interface (the LiDAR port) |
| `controller` | luxcom | simulator* | `simulator` or `luxcom` |
| `controller_host` | 192.168.1.124 | — | EM-HDLC address |
| `site_file` | data/site.yaml | /app/data/site.yaml | controller and address chosen in the inspector |
| `detector_min_points` | 20 | 20 | moving points to count as present |
| `detector_call_delay` | 3.0 | 3.0 | seconds of presence before a call |
| `background_learn_seconds` | 20 | 20 | |
| `background_voxel` | 0.2 | 0.2 | metres |
| `inspector_embedded` | true | false | inspector inside the detector |
| `inspector_port` | 8080 | 8088 | (prod: behind systemd's 8080) |
| `health_host` | 127.0.0.1 | 0.0.0.0 | |
| `retain_seconds` / `retain_max_mb` | 30 / 256 | 30 / 256 | rolling buffer for clips |

\* Per site, choose the controller in the inspector's **Controller
connection** panel (or the setup guide): it is applied at once and saved
in `site_file`, which overrides the profile. An `OPENTRAFFIC_CONTROLLER`
or `OPENTRAFFIC_CONTROLLER_HOST` in the environment overrides both, and
the panel then shows the setting as fixed.

If the chosen controller cannot start (its port is taken, say), the
detector carries on with the simulator and health reports
`controller: … could not start`, so it can be put right from the
inspector.

**An adapter set up for another machine.** An EM-HDLC sends its replies
and forwarded SDLC frames to the command/forward IP in its web page —
often the laptop it was configured from. It then keeps trying to reach
that address, by ARP or (with the answer cached) by sending to it, even
on a subnet this unit has no address on. **Find** overhears this on the
wired ports and shows *"192.168.1.124 is set to send to 192.168.1.139,
which no device has"*. **Use it** checks the address is free (RFC 5227
ARP probe), adds it to that port, connects, and keeps it only if the
adapter answers within 5 s; nothing changes on the adapter. The address
is saved in `site_file` and re-added at every start; choosing the
simulator gives it back. This needs the detector container's
`NET_RAW` and `NET_ADMIN` capabilities (`docker-compose.yml`).

---

## Troubleshooting

| Symptom | Look at |
|---|---|
| `/healthz` says down | `problems` in `/health` (or the Device panel) says why: no sensor answering (power, cable, boot takes ~1 min), port 7502 used by another program, or no packets arriving. Then `RcvbufErrors` (raise `rmem_max`) |
| No calls | Background learned? Zone enabled, has a channel, saved? Sidebar row shows *waiting*? |
| Calls show *not sent* | Adapter unreachable — address, cable; check **Controller connection** |
| **Find** shows no adapter | EM-HDLC powered and cabled to a wired port of this unit (Find does not look on Wi-Fi)? Type its address instead. Once its forward address is this unit (port 10002) it is always found |
| **Use it** says the address is in use | Another device has the adapter's command/forward IP: set that IP to this unit in the EM-HDLC's web page instead |
| Zone always occupied | Something static in it was not in the background: relearn with the scene clear |
| Moving points where nothing moves | Something left the scene after learning, revealing what was behind it: relearn |
| Signal channels say stale | EM-HDLC forward address must be this unit, port 10002 |
| Inspector will not load on :8080 | `systemctl --user status opentraffic-inspector-proxy.socket opentraffic-inspector`, `journalctl --user -u opentraffic-inspector` |
| Login says no login is set | `docker compose run --rm detector python auth.py set-password` |

---

## Repository layout

```
main.py              detector entry point (ingest loop)
settings.py          profiles + env overrides
auth.py              credential store and CLI
health.py            health report
api/                 detector API (loopback) + health listener
inspector/           sidecar: login, setup guide, page, proxy
controllers/         controller adapters: luxcom (EM-HDLC), simulator
perception/          background model, presence/calls, roadway fit
zones/               zone/roadway/map storage and validation
recording/           rolling buffer and clips
sensors/             Ouster and clip sources
config/profiles.yaml dev and prod settings
deploy/              provision-host.sh (root, once), install.sh, user units
docker/, docker-compose.yml   Jetson image and services
data/                per-unit state (git-ignored)
tests/               unit tests (pytest)
```

---

## Testing

Unit tests cover zone validation and storage and the presence logic
(thresholds, call delay, exclusions, live zone reloads). They need no
sensor or controller:

```sh
pip install -r requirements-dev.txt
pytest
```

CI runs them on every push and pull request, alongside lint, an import
check of every module, shellcheck and a build of the Jetson image.

---

## Contributing

Issues and pull requests are welcome; see
[CONTRIBUTING.md](CONTRIBUTING.md). Report security problems privately,
as described in [SECURITY.md](SECURITY.md).

Questions, field results and adapter ideas: join the
[OpenTraffic Discord](https://discord.gg/QGDazpAuNh).

## License

Copyright (C) 2026 Chris Brundige.

OpenTraffic is free software: you can redistribute it and/or modify it
under the terms of the GNU General Public License as published by the
Free Software Foundation, either version 3 of the License, or (at your
option) any later version. It is distributed WITHOUT ANY WARRANTY; see
[LICENSE](LICENSE) for the full terms.

Ouster, Siemens, Luxcom, NVIDIA, Jetson and other product names are
trademarks of their respective owners. OpenTraffic is an independent
project, not affiliated with or endorsed by them.
