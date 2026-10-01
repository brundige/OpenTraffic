"""
The cabinet network, at the level an EM-HDLC is found and reached.

An EM-HDLC sends its replies and forwarded SDLC traffic to the
"command" and "forward" IP set in its web page. In a cabinet that
address is usually some other machine's: the laptop it was set up
with, a previous detector. The adapter then ARPs for that address,
over and over, and nobody answers -- which is both how to find it and
the way to fix it without touching the adapter: this unit can take
the address it is looking for, once it is sure nobody else has it.

This module listens to ARP on the wired ports, probes whether an
address is free (RFC 5227), and adds or removes this unit's extra
address. Raw ARP needs CAP_NET_RAW (Docker's default for a root
container); changing addresses needs CAP_NET_ADMIN (docker-compose.yml)
and the `ip` tool (the image installs iproute2).
"""

from __future__ import annotations

import ctypes
import ipaddress
import select
import socket
import struct
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ETH_P_ARP = 0x0806
ETH_P_IP = 0x0800
SO_ATTACH_FILTER = 26
BROADCAST_MAC = b"\xff" * 6

# Interfaces a cabinet adapter is never on.
SKIP_INTERFACES = ("lo", "docker", "br-", "veth", "virbr", "l4tbr", "tailscale", "wg", "wl")

# Asking this many times in a listening window, with no answer, is a
# device that cannot reach the address it was set up with.
STRANDED_REQUESTS = 3


def wired_interfaces() -> List[str]:
    """
    Up, non-virtual, non-wireless interfaces: where a cabinet device is.
    """

    out = []

    for path in sorted(Path("/sys/class/net").iterdir()):

        name = path.name

        if name.startswith(SKIP_INTERFACES) or (path / "wireless").exists():
            continue

        try:
            if (path / "operstate").read_text().strip() != "up":
                continue
        except OSError:
            continue

        out.append(name)

    return out


def _mac(interface: str) -> bytes:
    text = Path(f"/sys/class/net/{interface}/address").read_text().strip()
    return bytes.fromhex(text.replace(":", ""))


def _arp_socket(interface: str) -> Optional[socket.socket]:

    try:
        sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ARP))
        sock.bind((interface, ETH_P_ARP))
        return sock
    except (OSError, AttributeError):
        return None


def _parse(frame: bytes) -> Optional[Tuple[int, str, str, str]]:
    """
    (opcode, sender MAC, sender IP, target IP) of an Ethernet ARP frame.
    """

    if len(frame) < 42 or frame[12:14] != b"\x08\x06":
        return None

    opcode = struct.unpack("!H", frame[20:22])[0]

    return (
        opcode,
        frame[22:28].hex(":"),
        socket.inet_ntoa(frame[28:32]),
        socket.inet_ntoa(frame[38:42]),
    )


def _udp_port_filter(ports: Tuple[int, ...]) -> bytes:
    """
    Classic BPF: IPv4, UDP, not a fragment, destination port in
    `ports` (up to 2). Keeps the LiDAR stream out of userspace.
    """

    a, b = (tuple(ports) + tuple(ports))[:2]

    program = [
        (0x28, 0, 0, 12),            # ldh [12]          ethertype
        (0x15, 0, 8, ETH_P_IP),      # != IPv4 -> drop
        (0x30, 0, 0, 23),            # ldb [23]          protocol
        (0x15, 0, 6, 17),            # != UDP -> drop
        (0x28, 0, 0, 20),            # ldh [20]          fragment
        (0x45, 4, 0, 0x1FFF),        # fragment -> drop
        (0xB1, 0, 0, 14),            # x = IP header length
        (0x48, 0, 0, 16),            # ldh [x + 16]      dst port
        (0x15, 2, 0, a),             # == a -> keep
        (0x15, 1, 0, b),             # == b -> keep
        (0x06, 0, 0, 0),             # drop
        (0x06, 0, 0, 0x40000),       # keep
    ]

    return b"".join(struct.pack("HBBI", *insn) for insn in program)


def _udp_socket(interface: str, ports: Tuple[int, ...]) -> Optional[socket.socket]:

    try:
        sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_IP))
        code = _udp_port_filter(ports)
        buffer = ctypes.create_string_buffer(code)
        program = struct.pack("HL", len(code) // 8, ctypes.addressof(buffer))
        # The kernel copies the program; the buffer need not outlive this.
        sock.setsockopt(socket.SOL_SOCKET, SO_ATTACH_FILTER, program)
        sock.bind((interface, ETH_P_IP))
        return sock
    except (OSError, AttributeError):
        return None


class ArpListener:
    """
    Opened before a search and read alongside it; afterwards, says who
    on the wired ports kept trying to reach an address that is not
    there: asking for it by ARP with nobody answering, or -- with the
    answer still cached -- sending to it on the adapter ports.
    """

    def __init__(self, interfaces: Optional[List[str]] = None, ports: Tuple[int, ...] = ()):

        self.sockets: Dict[socket.socket, str] = {}
        self._udp: set = set()

        for name in interfaces if interfaces is not None else wired_interfaces():

            sock = _arp_socket(name)

            if sock is not None:
                self.sockets[sock] = name

            if ports:
                udp = _udp_socket(name, ports)
                if udp is not None:
                    self.sockets[udp] = name
                    self._udp.add(udp)

        self.available = bool(self.sockets)

        # (interface, sender ip) -> {"mac", "asks": {target: count}}
        self._askers: Dict[Tuple[str, str], Dict] = {}
        self._answered: set = set()

    def read(self, sock: socket.socket) -> None:

        try:
            frame = sock.recv(2048)
        except OSError:
            return

        if sock in self._udp:

            if len(frame) < 34:
                return

            sender = socket.inet_ntoa(frame[26:30])
            target = socket.inet_ntoa(frame[30:34])

            entry = self._askers.setdefault(
                (self.sockets[sock], sender), {"mac": frame[6:12].hex(":"), "asks": {}}
            )
            entry["asks"][target] = entry["asks"].get(target, 0) + 1
            return

        parsed = _parse(frame)

        if parsed is None:
            return

        opcode, mac, sender, target = parsed

        if sender != "0.0.0.0":
            # Anyone who sends ARP from an address has that address.
            self._answered.add(sender)

        if opcode != 1 or sender in ("0.0.0.0", target):
            return

        entry = self._askers.setdefault(
            (self.sockets[sock], sender), {"mac": mac, "asks": {}}
        )
        entry["asks"][target] = entry["asks"].get(target, 0) + 1

    def stranded(self, own: set) -> List[Dict]:
        """
        Devices that asked for an address again and again, which no one
        answered and which is not this unit's.
        """

        out = []

        for (interface, sender), entry in self._askers.items():

            if sender in own:
                continue

            for target, count in entry["asks"].items():

                if count < STRANDED_REQUESTS or target in self._answered or target in own:
                    continue

                out.append({
                    "address": sender,
                    "mac": entry["mac"],
                    "interface": interface,
                    "looking_for": target,
                    "requests": count,
                })

        return out

    def close(self) -> None:
        for sock in self.sockets:
            sock.close()


def address_in_use(interface: str, address: str, wait: float = 1.0) -> bool:
    """
    RFC 5227 probe: ask who has `address` from 0.0.0.0 and listen.
    True if anything answers -- or claims it -- within `wait`.
    """

    sock = _arp_socket(interface)

    if sock is None:
        raise RuntimeError(f"cannot send ARP on {interface}")

    try:

        mac = _mac(interface)
        target = socket.inet_aton(address)

        probe = (
            BROADCAST_MAC + mac + struct.pack("!H", ETH_P_ARP)
            + struct.pack("!HHBBH", 1, 0x0800, 6, 4, 1)
            + mac + b"\x00" * 4 + b"\x00" * 6 + target
        )

        deadline = time.monotonic() + wait
        sent = 0

        while True:

            remaining = deadline - time.monotonic()

            if remaining <= 0:
                return False

            if sent < 3 and remaining < wait * (1 - sent / 3):
                sock.send(probe)
                sent += 1

            ready, _, _ = select.select([sock], [], [], min(remaining, wait / 3))

            if not ready:
                continue

            parsed = _parse(sock.recv(2048))

            if parsed and parsed[2] == address and parsed[1] != mac.hex(":"):
                return True

    finally:
        sock.close()


# ------------------------------------------------------------ addresses

def subnet_for(address: str, peer: str) -> str:
    """
    The CIDR to take `address` with, so `peer` is on the link: a /24
    when they share one, else the narrowest prefix holding both (not
    wider than /16).
    """

    a = int(ipaddress.IPv4Address(address))
    b = int(ipaddress.IPv4Address(peer))

    prefix = 32 - (a ^ b).bit_length()
    prefix = min(prefix, 24)

    if prefix < 16:
        raise ValueError(f"{address} and {peer} are not on one plausible subnet")

    return f"{address}/{prefix}"


def has_address(interface: str, address: str) -> bool:

    result = subprocess.run(
        ["ip", "-4", "-o", "addr", "show", "dev", interface],
        capture_output=True, text=True, timeout=5,
    )

    return any(
        part.split("/")[0] == address
        for line in result.stdout.splitlines()
        for part in line.split()
        if "/" in part
    )


def _ip(*args: str) -> None:

    try:
        result = subprocess.run(["ip", *args], capture_output=True, text=True, timeout=5)
    except FileNotFoundError:
        raise RuntimeError("the `ip` tool is missing from the image")

    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip()
        if "Operation not permitted" in message:
            message += " (the detector container needs cap_add: NET_ADMIN)"
        raise RuntimeError(f"ip {' '.join(args)}: {message}")


def add_address(interface: str, cidr: str) -> None:
    _ip("addr", "replace", cidr, "dev", interface)


def remove_address(interface: str, cidr: str) -> None:
    if has_address(interface, cidr.split("/")[0]):
        _ip("addr", "del", cidr, "dev", interface)
