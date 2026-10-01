# OpenTraffic

LiDAR vehicle detection for signalised intersections. A pole-mounted
Ouster OS-1 watches the approaches; a Jetson in the signal cabinet finds
vehicles in operator-drawn zones and places detector calls on the traffic
controller over the cabinet's SDLC bus, through a Luxcom EM-HDLC. The
same link reads the controller's signal state back.

**Status.** Proven on a bench: OS-1-128, Luxcom EM-HDLC, Siemens M60
(SEPAC 5.7.0.31). Calls placed by the detector were confirmed on the
controller over NTCIP. Not yet deployed at an intersection; the Jetson
deployment pieces (systemd on-demand inspector) are written but have not
been run on a Jetson yet.

Deeper notes on the sensor, the roadway fit, zones and the rolling
buffer are in [info.md](info.md).

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
   exclusion zones, saving, checking the controller link, and testing a
   call. Each step ticks itself off from the unit's live state.
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
only — redo them if the sensor is moved.

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

All 16 bits and combinations read back exactly. **This is one
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
| `down` | no LiDAR frame for 5 s — the unit is not detecting |
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
List* document.

**JetPack.** Prefer JetPack 6 (Ubuntu 22.04): its systemd supports the
inspector's idle shutdown, and the Ouster SDK installs directly. The
current image is Debian-based to support JetPack 4 hosts; on JetPack 6 it
can move to an L4T base if GPU work is added later.

```sh
# 1. Code
sudo git clone https://github.com/brundige/OpenTraffic.git /opt/opentraffic
cd /opt/opentraffic

# 2. Build, stamped with the version the health report shows
sudo OPENTRAFFIC_VERSION=$(git describe --always --dirty) docker compose build

# 3. Credentials (stored in ./data/auth.json, owner-only)
sudo docker compose run --rm detector python auth.py set-password
sudo docker compose run --rm detector python auth.py health-token

# 4. Detector, always on
sudo docker compose up -d detector

# 5. On-demand inspector on :8080
sudo deploy/install-inspector.sh

# 6. UDP receive buffer, or packets drop at ~127 Mbit/s (OS-1-128, 10 Hz)
echo 'net.core.rmem_max=8388608' | sudo tee /etc/sysctl.d/60-opentraffic.conf
sudo sysctl --system
```

On JetPack 5 (systemd 245) remove `--exit-idle-time=15min` from
`opentraffic-inspector-proxy.service`; the inspector then stays up after
first use until stopped with
`sudo systemctl stop opentraffic-inspector-proxy opentraffic-inspector`.

Update: `git pull`, rebuild as in step 2, `docker compose up -d detector`.
Zones, background and credentials in `data/` survive.

---

## Reaching a unit over the city VPN

Yes — you need each unit's address on the city network. Ask city IT for:

1. **A fixed address per unit** — a DHCP reservation for the Jetson's
   cabinet-network port, or a static IP. A **DNS name** per unit
   (e.g. `ot-main-and-5th.signals.city.gov`) is better still: nobody has
   to remember addresses, and a box swap does not change the name.
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
VPN access). Do not bridge them.

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
| `source` | 169.254.151.172 | same | sensor address, or a recording |
| `controller` | luxcom | simulator* | `simulator` or `luxcom` |
| `controller_host` | 192.168.1.124 | — | EM-HDLC address |
| `detector_min_points` | 20 | 20 | moving points to count as present |
| `detector_call_delay` | 3.0 | 3.0 | seconds of presence before a call |
| `background_learn_seconds` | 20 | 20 | |
| `background_voxel` | 0.2 | 0.2 | metres |
| `inspector_embedded` | true | false | inspector inside the detector |
| `inspector_port` | 8080 | 8088 | (prod: behind systemd's 8080) |
| `health_host` | 127.0.0.1 | 0.0.0.0 | |
| `retain_seconds` / `retain_max_mb` | 30 / 256 | 30 / 256 | rolling buffer for clips |

\* Set `controller: luxcom` and `controller_host` per site.

---

## Troubleshooting

| Symptom | Look at |
|---|---|
| `/healthz` says down | Sensor power/cable; `RcvbufErrors` in `/health` (raise `rmem_max`) |
| No calls | Background learned? Zone enabled, has a channel, saved? Sidebar row shows *waiting*? |
| Calls show *not sent* | Adapter unreachable — address, cable, `controller_host` |
| Zone always occupied | Something static in it was not in the background: relearn with the scene clear |
| Moving points where nothing moves | Something left the scene after learning, revealing what was behind it: relearn |
| Signal channels say stale | EM-HDLC forward address must be this unit, port 10002 |
| Inspector will not load on :8080 | `systemctl status opentraffic-inspector-proxy.socket`, `journalctl -u opentraffic-inspector` |
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
deploy/              systemd units for the on-demand inspector
docker/, docker-compose.yml   Jetson image and services
data/                per-unit state (git-ignored)
```
