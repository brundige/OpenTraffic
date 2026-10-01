from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Dict, List, Optional


# Four TS2 detector BIUs of 16 inputs each.
MAX_CHANNEL = 64

LOG_LENGTH = 50


class Controller:
    """
    A traffic controller we place detector calls on.

    Everything upstream only ever says "channel N is occupied" or
    "channel N is clear"; which box is in the cabinet, and how the call
    reaches it, is the subclass's business. The base keeps the record
    the inspector shows: which channels are calling, every call change
    we tried to send and whether it actually went out, and the phase
    state when the subclass has a source for it.
    """

    kind = "controller"

    def __init__(self):
        self._lock = threading.Lock()
        self._calls: Dict[int, Dict[str, Any]] = {}
        self._log: deque = deque(maxlen=LOG_LENGTH)
        self._sent = 0
        self._failed = 0

    # ----------------------------------------------------------- calls

    def set_detector(
        self,
        channel: int,
        occupied: bool,
        label: str = "",
    ) -> bool:
        """
        Place or drop the call on a detector channel.

        Returns True if the change went out to the controller. A failed
        send is recorded, not raised: a flaky link must not take the
        detector down, and the operator needs to see that it failed.
        """

        if not 1 <= channel <= MAX_CHANNEL:
            raise ValueError(f"detector channel {channel} is outside 1-{MAX_CHANNEL}")

        error = None

        try:
            self._send(channel, occupied)
        except Exception as exc:  # noqa: BLE001 - record, do not die
            error = f"{type(exc).__name__}: {exc}"

        now = time.time()

        with self._lock:

            self._calls[channel] = {
                "channel": channel,
                "label": label,
                "occupied": occupied,
                "since": now,
                "sent": error is None,
            }

            self._log.appendleft({
                "time": now,
                "channel": channel,
                "label": label,
                "occupied": occupied,
                "sent": error is None,
                "error": error,
            })

            if error is None:
                self._sent += 1
            else:
                self._failed += 1

        return error is None

    def _send(self, channel: int, occupied: bool) -> None:
        raise NotImplementedError

    # ---------------------------------------------------------- status

    def phases(self) -> Optional[Dict[str, Any]]:
        """
        Current phase state, or None when this controller has no
        source for it. {"green": [...], "yellow": [...], "red": [...]}.
        """
        return None

    def describe(self) -> Dict[str, Any]:
        return {"kind": self.kind}

    def link(self) -> Optional[Dict[str, Any]]:
        """
        Health of the link to the controller, or None when there is no
        link to speak of.
        """
        return None

    def status(self) -> Dict[str, Any]:

        with self._lock:
            calls: List[Dict[str, Any]] = sorted(
                self._calls.values(), key=lambda call: call["channel"]
            )
            log = list(self._log)
            sent, failed = self._sent, self._failed

        return {
            "controller": self.describe(),
            "phases": self.phases(),
            "link": self.link(),
            "calls": calls,
            "log": log,
            "sent": sent,
            "failed": failed,
        }

    def close(self) -> None:
        pass
