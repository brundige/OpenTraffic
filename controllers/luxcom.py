"""
Detector calls to an M60 through a Luxcom EM-HDLC running the EMH_BIU
firmware, which emulates NEMA TS2 detector BIUs on the cabinet's SDLC
bus (TS2 Port 1).

What is here is what Luxcom's own sample confirms
(github.com/luxcom/EM_HDLC-BIU-Python-Samples):

  * Commands are UDP to the EM-HDLC, port 10001 by default:
        b"LXBIU" 0x01 <command> <length> <payload>
    01 discover, 02 set timeout (s), 03 enable BIU mask,
    0a update call data.
  * Call data is a whole TS2 Type 148 response -- BIU 1's address
    0x08, control 0x83, frame type 0x94, then 36 bytes -- which the
    EM-HDLC answers the controller's polls with.
  * The sample resends timeout, enable and call data every 0.5 s
    against a 5 s timeout, so the host keeps the BIU alive.
  * Every frame on the SDLC bus is forwarded by UDP to the address set
    in the EM-HDLC web interface, port 10002 in the sample. That is
    where SPaT comes from.

Forwarded frames arrive raw: address, control 0x83, frame type, data.
Seen from an M60 on the bench (2026-09-30):

  * 0x13 83 00 + 13 bytes, every 100 ms: the load switch drivers,
    which is SPaT. decode_signal_frame() reads it as three 4-byte
    groups -- green, yellow, red -- with 2 bits per channel, channel
    1 lowest. That layout is INFERRED, not from the spec: it was
    checked against 4 minutes of live traffic (2,401 frames, no
    channel ever in two colours, all 44 changes in green -> yellow ->
    red order, constant yellow times per channel). Only the value 3
    was ever seen in a 2-bit field; what 1 or 2 mean (flash or dim,
    presumably) is unknown, so any non-zero value counts as lit.
  * 0x08 83 14, every 100 ms: the controller polling detector BIU 1
    for call data -- what the Type 148 update answers.
  * 0x08 83 18 00, every second: another request to BIU 1.
  * 0xff 83 09, every second: a broadcast whose data decodes as the
    controller's date and time.

The call-data layout was worked out on the same bench M60 (SEPAC
5.7.0.31) by setting bits in the Type 148 data and reading back the
controller's NTCIP 1202 vehicleDetectorStatusGroupActive:

  * byte 32 carries detectors 1-8 and byte 33 detectors 9-16, one bit
    each, detector 1 in the lowest bit. All 16 bits and a combination
    read back exactly.
  * No other byte places a call. Bytes 0-31 are presumably per-
    detector timestamps (2 bytes x 16); they are sent as zero, which
    the controller accepts. Bytes 34-35 are sent as zero too.
  * The EM-HDLC acknowledges each command with the command code
    + 0x80 and a status byte, 00 for success.

That is one controller's reading of the frame, not the TS2 text; check
it against the spec, or against a real detector BIU, before relying
on it at an intersection.
"""

import socket
import threading
import time
from collections import deque
from typing import Any, Dict, List, Optional

from controllers.base import Controller


MAGIC = b"LXBIU"
VERSION = 0x01

CMD_DISCOVER = 0x01
CMD_SET_TIMEOUT = 0x02
CMD_ENABLE_MASK = 0x03
CMD_UPDATE_CALL_DATA = 0x0A

# TS2 Type 148: detector BIU 1's call data response.
BIU1_ADDRESS = 0x08
UI_CONTROL = 0x83
FRAME_TYPE_148 = 0x94
CALL_DATA_BYTES = 36

# Detector call bits within the call data: see the module docstring.
CALL_BYTES_OFFSET = 32

# Only BIU 1 is confirmed by the sample; BIUs 2-4 would carry
# channels 17-64 but their addresses and frame types are unverified.
CONFIRMED_CHANNELS = 16

RESEND_INTERVAL = 0.5
DISCOVER_INTERVAL = 5.0

FORWARD_LOG = 20

SIGNAL_FRAME_TYPE = 0x00
CALL_DATA_REQUEST = 0x14
SIGNAL_CHANNELS = 16

# A signal state older than this is not shown as current.
SIGNAL_STALE = 2.0


def command(code: int, payload: bytes = b"") -> bytes:
    """
    Frame one EMH_BIU command.
    """

    if len(payload) > 255:
        raise ValueError("EMH_BIU payload longer than 255 bytes")

    return MAGIC + bytes([VERSION, code, len(payload)]) + payload


def type148_frame(call_data: bytes) -> bytes:
    """
    Wrap BIU 1's call data as the TS2 Type 148 response the EM-HDLC
    answers the controller with.
    """

    if len(call_data) != CALL_DATA_BYTES:
        raise ValueError(
            f"Type 148 call data is {CALL_DATA_BYTES} bytes, got {len(call_data)}"
        )

    return bytes([BIU1_ADDRESS, UI_CONTROL, FRAME_TYPE_148]) + call_data


def encode_call_data(calls: Dict[int, bool]) -> bytes:
    """
    The 36 call-data bytes of a Type 148 response for channels 1-16.
    """

    data = bytearray(CALL_DATA_BYTES)

    for channel, occupied in calls.items():

        if not 1 <= channel <= CONFIRMED_CHANNELS:
            raise ValueError(f"channel {channel} is not on BIU 1")

        if occupied:
            data[CALL_BYTES_OFFSET + (channel - 1) // 8] |= 1 << ((channel - 1) % 8)

    return bytes(data)


def decode_signal_frame(frame: bytes) -> Optional[Dict[str, List[int]]]:
    """
    Signal channels lit in a forwarded load switch driver frame, or
    None if the frame is not one. See the module docstring for how
    the layout was established.
    """

    if len(frame) < 15 or frame[1] != UI_CONTROL or frame[2] != SIGNAL_FRAME_TYPE:
        return None

    data = frame[3:]

    lit: Dict[str, List[int]] = {}

    for colour, offset in (("green", 0), ("yellow", 4), ("red", 8)):

        bits = int.from_bytes(data[offset:offset + 4], "little")

        lit[colour] = [
            channel + 1
            for channel in range(SIGNAL_CHANNELS)
            if (bits >> (2 * channel)) & 0b11
        ]

    return lit


class LuxcomEMHDLC(Controller):

    kind = "luxcom"

    def __init__(
        self,
        ip: str,
        port: int = 10001,
        listen_port: int = 10001,
        forward_port: int = 10002,
        timeout: int = 5,
    ):
        super().__init__()

        self.ip = ip
        self.port = port
        self.listen_port = listen_port
        self.forward_port = forward_port
        self.timeout = timeout

        self._state: Dict[int, bool] = {}
        self._enabled = False

        # Replies from the EM-HDLC come back to the port its web
        # interface has as the command port, so send from that too.
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind(("0.0.0.0", listen_port))
        self.socket.settimeout(0.5)

        self.forward = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.forward.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.forward.bind(("0.0.0.0", forward_port))
        self.forward.settimeout(0.5)

        self._link_lock = threading.Lock()
        self._last_reply: Optional[float] = None
        self._last_reply_hex = ""
        self._replies = 0
        self._last_error: Optional[str] = None
        self._forwarded = 0
        self._forward_log: deque = deque(maxlen=FORWARD_LOG)
        self._signals: Optional[Dict[str, List[int]]] = None
        self._signals_at: Optional[float] = None
        self._channels_seen: set = set()
        self._polls = 0
        self._last_poll: Optional[float] = None

        self._running = True

        self._threads = [
            threading.Thread(target=loop, name=name, daemon=True)
            for name, loop in (
                ("luxcom-keepalive", self._keepalive),
                ("luxcom-replies", self._receive_replies),
                ("luxcom-forward", self._receive_forwarded),
            )
        ]

        for thread in self._threads:
            thread.start()

    # ----------------------------------------------------------- wire

    def _command(self, code: int, payload: bytes = b"") -> None:
        self.socket.sendto(command(code, payload), (self.ip, self.port))

    def _update_frame(self) -> bytes:
        return type148_frame(encode_call_data(dict(self._state)))

    def _send(self, channel: int, occupied: bool) -> None:

        if channel > CONFIRMED_CHANNELS:
            raise ValueError(
                f"channel {channel} is on BIU {(channel - 1) // 16 + 1}; "
                f"only BIU 1 (channels 1-16) is confirmed"
            )

        state = {**self._state, channel: occupied}

        frame = type148_frame(encode_call_data(state))

        self._command(CMD_UPDATE_CALL_DATA, frame)

        self._state = state

    def _keepalive(self) -> None:

        last_discover = 0.0

        while self._running:

            try:

                now = time.monotonic()

                if now - last_discover >= DISCOVER_INTERVAL:
                    self._command(CMD_DISCOVER)
                    last_discover = now

                # Only claim BIU 1 on the bus once we can say what its
                # detectors see; otherwise the controller would poll a
                # BIU answering with nothing we chose.
                try:
                    frame = self._update_frame()
                except NotImplementedError:
                    frame = None

                if frame is not None:
                    self._command(CMD_SET_TIMEOUT, bytes([self.timeout]))
                    self._command(CMD_ENABLE_MASK, bytes([0x01]))
                    self._command(CMD_UPDATE_CALL_DATA, frame)

                self._enabled = frame is not None

            except OSError as exc:
                with self._link_lock:
                    self._last_error = f"{type(exc).__name__}: {exc}"

            time.sleep(RESEND_INTERVAL)

    def _receive_replies(self) -> None:

        while self._running:

            try:
                data, _ = self.socket.recvfrom(1024)
            except socket.timeout:
                continue
            except OSError:
                if not self._running:
                    return
                continue

            with self._link_lock:
                self._last_reply = time.time()
                self._last_reply_hex = data.hex(" ")
                self._replies += 1
                self._last_error = None

    def _receive_forwarded(self) -> None:

        while self._running:

            try:
                data, addr = self.forward.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                if not self._running:
                    return
                continue

            now = time.time()

            signals = decode_signal_frame(data)

            with self._link_lock:

                self._forwarded += 1

                if signals is not None:
                    self._signals = signals
                    self._signals_at = now
                    for channels in signals.values():
                        self._channels_seen.update(channels)

                if data[:3] == bytes([BIU1_ADDRESS, UI_CONTROL, CALL_DATA_REQUEST]):
                    self._polls += 1
                    self._last_poll = now

                self._forward_log.appendleft({
                    "time": time.time(),
                    "from": addr[0],
                    "length": len(data),
                    "hex": data.hex(" "),
                })

    # --------------------------------------------------------- status

    def describe(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "name": "M60 via Luxcom EM-HDLC",
            "target": f"{self.ip}:{self.port}",
        }

    def phases(self) -> Optional[Dict[str, Any]]:

        with self._link_lock:

            if self._signals is None:
                return None

            age = time.time() - self._signals_at

            return {
                "unit": "channel",
                "numbers": sorted(self._channels_seen),
                **(
                    {"green": [], "yellow": [], "red": []}
                    if age > SIGNAL_STALE else self._signals
                ),
                "stale": age > SIGNAL_STALE,
                "age": round(age, 1),
                "source": "SDLC load switch drivers (layout inferred)",
            }

    def link(self) -> Dict[str, Any]:

        with self._link_lock:

            return {
                "replies": self._replies,
                "last_reply": self._last_reply,
                "last_reply_hex": self._last_reply_hex,
                "error": self._last_error,
                "biu_enabled": self._enabled,
                "forward_port": self.forward_port,
                "forwarded": self._forwarded,
                "polls": self._polls,
                "last_poll": self._last_poll,
                "forwarded_recent": list(self._forward_log),
            }

    def close(self) -> None:

        self._running = False

        for thread in self._threads:
            thread.join(timeout=1.0)

        self.socket.close()
        self.forward.close()
