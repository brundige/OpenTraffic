"""
Finding the Luxcom EM-HDLC on the cabinet network.

Two kinds of evidence, collected together over a couple of seconds:

  * Asked: the EMH_BIU "discover" command (0x01) is broadcast on every
    interface, and anything that answers with the LXBIU header is an
    adapter. NOT YET CONFIRMED on hardware: Luxcom's sample sends
    discover to a known address, and an EM-HDLC may answer only to the
    command IP/port set in its web interface rather than to whoever
    asked. Answers that arrive there are caught too, by listening on
    the command port.

  * Heard: an EM-HDLC whose forward address is already this unit sends
    every SDLC frame from the bus to the forward port. Any sender of
    SDLC-shaped frames there is an adapter, and one that is already
    set up for this unit -- which is what the signal state needs.

  * Overheard: an EM-HDLC whose command/forward IP is some other
    machine's -- the laptop it was set up with -- keeps ARPing for that
    address and nobody answers. Listening to ARP on the wired ports
    finds it even on a subnet this unit has no address on, and says
    which address it wants; the inspector can then have this unit take
    that address (controllers/network.py), with no change to the
    adapter. Seen on the bench: 192.168.1.124 asking ~20 times a second
    for 192.168.1.139.

If the link to an adapter is already running it owns both ports, so
what it has heard is passed in as `running` instead.
"""

from __future__ import annotations

import ipaddress
import select
import socket
import time
from typing import Any, Dict, List, Optional

import psutil

from .luxcom import CMD_DISCOVER, MAGIC, UI_CONTROL, command
from .network import ArpListener


# Evidence from the running link counts only if this fresh (s).
RECENT_S = 10.0

SKIP_INTERFACES = ("lo", "docker", "br-", "veth", "virbr", "l4tbr", "tailscale", "wg")


def _networks() -> List[Dict[str, Any]]:

    stats = psutil.net_if_stats()
    out = []

    for name, addresses in sorted(psutil.net_if_addrs().items()):

        if name.startswith(SKIP_INTERFACES) or not (stats.get(name) and stats[name].isup):
            continue

        for a in addresses:

            if a.family != socket.AF_INET or not a.netmask:
                continue

            network = ipaddress.ip_interface(f"{a.address}/{a.netmask}").network

            out.append({
                "interface": name,
                "address": a.address,
                "network": network,
                "broadcast": a.broadcast or str(network.broadcast_address),
            })

    return out


def _interface_for(address: str, networks: List[Dict[str, Any]]) -> Optional[str]:

    ip = ipaddress.ip_address(address)

    for n in networks:
        if ip in n["network"]:
            return n["interface"]

    return None


def _bind(port: int) -> Optional[socket.socket]:

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    try:
        sock.bind(("0.0.0.0", port))
    except OSError:
        sock.close()
        return None

    return sock


def find_adapters(
    port: int = 10001,
    listen_port: int = 10001,
    forward_port: int = 10002,
    wait: float = 2.0,
    running: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Adapters seen, each with how it was seen:
      {"adapters": [{"address", "interface", "evidence": [...],
                     "looking_for"?, "mac"?, "reachable"}],
       "searched": [interface, ...], "notes": [...]}

    "looking_for" is set for an adapter overheard asking for an address
    nobody has; "reachable" is whether this unit already has an address
    on the adapter's subnet.
    """

    networks = _networks()
    found: Dict[str, set] = {}
    notes: List[str] = []

    def saw(address: str, how: str) -> None:
        found.setdefault(address, set()).add(how)

    # What the running link has heard lately -- not what it remembers
    # from an adapter that has since gone quiet.
    if running:
        now = time.time()
        if running.get("reply_from") and now - (running.get("last_reply") or 0) < RECENT_S:
            saw(running["reply_from"], "answering this unit's link")
        for frame in running.get("forwarded_recent") or []:
            if now - frame["time"] < RECENT_S:
                saw(frame["from"], "forwarding SDLC frames to this unit")

    # Ask on the command port, or from any port if the link holds it.
    asker = _bind(listen_port)

    if asker is None:
        asker = _bind(0)
        notes.append(
            f"port {listen_port} is held by the running link; "
            f"answers sent there are counted from the link"
        )

    asker.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

    targets = {n["broadcast"] for n in networks} | {"255.255.255.255"}

    for target in sorted(targets):
        try:
            asker.sendto(command(CMD_DISCOVER), (target, port))
        except OSError:
            continue

    listener = _bind(forward_port)

    if listener is None and not running:
        notes.append(f"port {forward_port} is in use; forwarded frames not checked")

    arp = ArpListener(ports=(listen_port, forward_port))

    if not arp.available:
        notes.append("cannot listen to ARP (needs CAP_NET_RAW); adapters on other subnets not seen")

    sockets = [s for s in (asker, listener) if s is not None] + list(arp.sockets)
    own = {n["address"] for n in networks}

    deadline = time.monotonic() + wait

    try:

        while True:

            remaining = deadline - time.monotonic()

            if remaining <= 0:
                break

            ready, _, _ = select.select(sockets, [], [], remaining)

            for sock in ready:

                if sock in arp.sockets:
                    arp.read(sock)
                    continue

                try:
                    data, (address, _) = sock.recvfrom(2048)
                except OSError:
                    continue

                if address in own:
                    continue

                if sock is asker and data.startswith(MAGIC):
                    saw(address, "answered discover")

                elif sock is listener and len(data) >= 3 and data[1] == UI_CONTROL:
                    saw(address, "forwarding SDLC frames to this unit")

    finally:
        for sock in sockets:
            sock.close()

    extra: Dict[str, Dict[str, Any]] = {}

    for device in arp.stranded(own):
        saw(device["address"], f"trying to reach {device['looking_for']}, which no device here has")
        extra.setdefault(device["address"], {
            "looking_for": device["looking_for"],
            "mac": device["mac"],
            "arp_interface": device["interface"],
        })

    adapters = []

    for address, how in sorted(found.items(), key=lambda item: ipaddress.ip_address(item[0])):

        interface = _interface_for(address, networks)
        more = extra.get(address, {})

        adapters.append({
            "address": address,
            "interface": interface or more.get("arp_interface"),
            "evidence": sorted(how),
            "reachable": interface is not None,
            **{k: v for k, v in more.items() if k != "arp_interface"},
        })

    return {
        "adapters": adapters,
        "searched": sorted({n["interface"] for n in networks}),
        "notes": notes,
    }
