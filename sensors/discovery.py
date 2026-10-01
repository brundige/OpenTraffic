"""
Finding the LiDAR without being told its address.

Ouster sensors announce themselves over mDNS as _ouster-lidar._tcp,
with the serial number in the TXT record (seen from an OS-1-128 on
firmware 3.1.0):

    Ouster Sensor 122450000095._ouster-lidar._tcp.local
        hostname os-122450000095.local, port 80
        txt  path=/api/v1  sn=122450000095  pn=840-104682-05

find_sensors() asks for that service on each interface and collects
who answers. The query goes out from an ordinary port, which makes it
a "legacy unicast" query (RFC 6762 s6.7): responders answer straight
back to the asking socket, so no avahi and no port 5353 are needed --
the container has neither.

Every answer is then checked over the sensor's own HTTP API, which is
also what proves the address is reachable from here. A sensor keeps a
link-local address next to any static one, so a fresh sensor on a
link-local port is found as readily as one set up on the bench.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import struct
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import psutil


SERVICE = "_ouster-lidar._tcp.local"

MDNS_GROUP = ("224.0.0.251", 5353)

TYPE_A = 1
TYPE_PTR = 12
TYPE_TXT = 16
TYPE_SRV = 33

# Virtual and container interfaces never have the sensor on them.
SKIP_INTERFACES = ("lo", "docker", "br-", "veth", "virbr", "l4tbr", "tailscale", "wg")


@dataclass
class FoundSensor:
    address: str
    interface: str
    serial: str
    prod_line: str = ""
    addresses: List[str] = field(default_factory=list)

    def label(self) -> str:
        return f"{self.prod_line or 'Ouster'} {self.serial} at {self.address} on {self.interface}"


def search_interfaces(only: str = "") -> List[Tuple[str, str]]:
    """
    (interface, IPv4 address) pairs to search: every interface that is
    up and has an address, or just `only` if given.
    """

    stats = psutil.net_if_stats()

    pairs = []

    for name, addresses in sorted(psutil.net_if_addrs().items()):

        if only and name != only:
            continue

        if not only and name.startswith(SKIP_INTERFACES):
            continue

        if not (stats.get(name) and stats[name].isup):
            continue

        pairs.extend(
            (name, a.address) for a in addresses if a.family == socket.AF_INET
        )

    return pairs


# ------------------------------------------------------------ mDNS wire

def _query(name: str, qtype: int) -> bytes:

    labels = b"".join(
        bytes([len(part)]) + part.encode() for part in name.split(".")
    ) + b"\x00"

    # id 0, flags 0, one question; class IN with the unicast-response bit.
    return struct.pack("!6H", 0, 0, 1, 0, 0, 0) + labels + struct.pack("!2H", qtype, 0x8001)


def _name(data: bytes, offset: int) -> Tuple[str, int]:
    """
    A DNS name at offset, following compression pointers. Returns the
    name and the offset just past it in the original position.
    """

    labels = []
    end = None
    hops = 0

    while True:

        length = data[offset]

        if length & 0xC0 == 0xC0:
            if end is None:
                end = offset + 2
            offset = ((length & 0x3F) << 8) | data[offset + 1]
            hops += 1
            if hops > 32:
                raise ValueError("DNS name pointer loop")
            continue

        offset += 1

        if length == 0:
            break

        labels.append(data[offset:offset + length].decode(errors="replace"))
        offset += length

    return ".".join(labels), end if end is not None else offset


def parse_response(data: bytes) -> Dict[str, Dict]:
    """
    The A, SRV and TXT records in one mDNS response:
      {"a": {host: [ip]}, "srv": {instance: host}, "txt": {instance: {k: v}}}
    """

    records: Dict[str, Dict] = {"a": {}, "srv": {}, "txt": {}}

    _, flags, qd, an, ns, ar = struct.unpack("!6H", data[:12])

    offset = 12

    for _ in range(qd):
        _, offset = _name(data, offset)
        offset += 4

    for _ in range(an + ns + ar):

        name, offset = _name(data, offset)
        rtype, _, _, length = struct.unpack("!2HIH", data[offset:offset + 10])
        offset += 10
        rdata = offset
        offset += length

        if rtype == TYPE_A and length == 4:
            records["a"].setdefault(name.lower(), []).append(
                socket.inet_ntoa(data[rdata:rdata + 4])
            )

        elif rtype == TYPE_SRV:
            host, _ = _name(data, rdata + 6)
            records["srv"][name] = host.lower()

        elif rtype == TYPE_TXT:
            txt = {}
            position = rdata
            while position < rdata + length:
                size = data[position]
                entry = data[position + 1:position + 1 + size].decode(errors="replace")
                key, _, value = entry.partition("=")
                txt[key] = value
                position += 1 + size
            records["txt"][name] = txt

    return records


def _ask(interface_address: str, wait: float) -> List[Tuple[str, Dict]]:
    """
    Send the query out of one interface and collect (responder, records).
    """

    answers = []

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    try:
        sock.bind((interface_address, 0))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                        socket.inet_aton(interface_address))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        sock.sendto(_query(SERVICE, TYPE_PTR), MDNS_GROUP)

        deadline = time.monotonic() + wait

        while True:

            remaining = deadline - time.monotonic()

            if remaining <= 0:
                break

            sock.settimeout(remaining)

            try:
                data, (responder, _) = sock.recvfrom(9000)
            except socket.timeout:
                break

            try:
                answers.append((responder, parse_response(data)))
            except (ValueError, IndexError, struct.error):
                continue

    except OSError:
        pass

    finally:
        sock.close()

    return answers


# ------------------------------------------------------------- checking

def sensor_info(address: str, timeout: float = 2.0) -> Optional[Dict]:
    """
    The sensor's metadata from its HTTP API, or None if nothing that
    looks like an Ouster answers there.
    """

    url = f"http://{address}/api/v1/sensor/metadata/sensor_info"

    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            info = json.loads(response.read())
    except (OSError, ValueError):
        return None

    return info if isinstance(info, dict) and info.get("prod_sn") else None


def _ordered(addresses: List[str], interface_address: str) -> List[str]:
    """
    Addresses on the interface's own subnet first: those need no route.
    """

    try:
        here = ipaddress.ip_interface(
            interface_address + ("/16" if interface_address.startswith("169.254.") else "/24")
        ).network
    except ValueError:
        return addresses

    return sorted(addresses, key=lambda a: ipaddress.ip_address(a) not in here)


def find_sensors(interface: str = "", wait: float = 1.5) -> List[FoundSensor]:
    """
    Every Ouster sensor that answers on the searched interfaces and
    whose HTTP API can be reached from here.
    """

    found: Dict[str, FoundSensor] = {}

    for name, interface_address in search_interfaces(interface):

        for responder, records in _ask(interface_address, wait):

            instances = [i for i in records["txt"] if "_ouster-lidar." in i.lower()] or [None]

            for instance in instances:

                host = records["srv"].get(instance) if instance else None
                addresses = list(records["a"].get(host, [])) if host else []

                if responder not in addresses:
                    addresses.append(responder)

                for address in _ordered(addresses, interface_address):

                    info = sensor_info(address)

                    if info is None:
                        continue

                    serial = str(info["prod_sn"])

                    if serial not in found:
                        found[serial] = FoundSensor(
                            address=address,
                            interface=name,
                            serial=serial,
                            prod_line=str(info.get("prod_line", "")),
                            addresses=addresses,
                        )

                    break

    return list(found.values())
