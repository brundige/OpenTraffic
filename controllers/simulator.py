import time
from typing import Any, Dict, List, Tuple

from controllers.base import Controller


# A plain dual-ring fixed-time plan, NEMA numbering: the main street
# on 2+6, the side street on 4+8, with its left turns ahead of each.
SEQUENCE: List[Tuple[int, int]] = [(1, 5), (2, 6), (3, 7), (4, 8)]

GREEN = {1: 6.0, 2: 20.0, 3: 6.0, 4: 14.0}
YELLOW = 3.0
RED_CLEAR = 1.0

PHASES = list(range(1, 9))


class SimulatorController(Controller):
    """
    Stands in for a controller when there is none on the bench.

    Every call "goes out" -- there is no wire to fail -- and the
    phases cycle through a fixed-time plan, so the inspector has
    something to show. It does not respond to calls.
    """

    kind = "simulator"

    def __init__(self):
        super().__init__()
        self._start = time.monotonic()

    def describe(self) -> Dict[str, Any]:
        return {"kind": self.kind, "name": "Simulated controller"}

    def _send(self, channel: int, occupied: bool) -> None:
        pass

    def phases(self) -> Dict[str, Any]:

        cycle = sum(GREEN[pair[0]] + YELLOW + RED_CLEAR for pair in SEQUENCE)

        t = (time.monotonic() - self._start) % cycle

        for pair in SEQUENCE:

            green = GREEN[pair[0]]

            if t < green:
                return _state(green=pair, remaining=green - t)

            t -= green

            if t < YELLOW:
                return _state(yellow=pair, remaining=YELLOW - t)

            t -= YELLOW

            if t < RED_CLEAR:
                return _state(remaining=RED_CLEAR - t)

            t -= RED_CLEAR

        return _state()


def _state(green=(), yellow=(), remaining=0.0) -> Dict[str, Any]:
    return {
        "unit": "phase",
        "numbers": PHASES,
        "green": list(green),
        "yellow": list(yellow),
        "red": [p for p in PHASES if p not in green and p not in yellow],
        "remaining": round(remaining, 1),
    }
