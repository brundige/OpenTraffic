from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np


# Voxel keys pack three signed indices of 21 bits each; at 0.2 m that is
# +-209 km, far past anything the sensor returns.
_BIAS = 1 << 20
_MASK21 = (1 << 21) - 1

# Background lookups go through a bit table indexed by a hash of the
# key. A collision can only make a point look like background, never
# invent movement, and at 2^24 slots it is rare for a scene's ~10^5
# voxels.
_TABLE_BITS = 24
_HASH_MUL = np.uint64(0x9E3779B97F4A7C15)

# The 27 neighbour offsets, for widening the background by one voxel.
_NEIGHBOURS = np.array(
    [(dx, dy, dz) for dx in (-1, 0, 1) for dy in (-1, 0, 1) for dz in (-1, 0, 1)],
    dtype=np.int64,
)


def _keys(indices: np.ndarray) -> np.ndarray:
    i = indices.astype(np.int64) + _BIAS
    return (i[:, 0] << 42) | (i[:, 1] << 21) | i[:, 2]


def _unpack(keys: np.ndarray) -> np.ndarray:
    return np.stack(
        [(keys >> 42) & _MASK21, (keys >> 21) & _MASK21, keys & _MASK21], axis=1
    ) - _BIAS


def _slots(keys: np.ndarray) -> np.ndarray:
    return ((keys.astype(np.uint64) * _HASH_MUL) >> np.uint64(64 - _TABLE_BITS)).astype(np.int64)


def _table(static: np.ndarray) -> np.ndarray:
    """
    Lookup table for the static voxels widened by one voxel in every
    direction.
    """

    widened = np.unique(
        _keys((_unpack(static)[:, None, :] + _NEIGHBOURS[None, :, :]).reshape(-1, 3))
    )

    table = np.zeros(1 << _TABLE_BITS, dtype=bool)
    table[_slots(widened)] = True

    return table


class BackgroundModel:
    """
    The static scene, learned, so everything else counts as movement.

    For a sensor on a pole the road, kerbs, buildings and poles return
    from the same places frame after frame. While learning, every frame's
    points are binned into voxels; a voxel occupied in at least
    `occupancy` of the learning frames is background. Anything seen in
    a voxel that is not background is new: a vehicle, a person, a bin
    that was not there before.

    The background is widened by one voxel, so range noise on a static
    surface does not flicker through as movement. The price is that
    points within about one voxel of a learned surface -- the bottom of
    a tyre on the road -- are hidden too.

    It does NOT keep adapting. A model that slowly absorbs whatever
    stands still would absorb a car waiting at a red light and drop its
    call, which is the one thing a stop-bar detector must never do.
    Relearn instead when the scene really changes (a parked trailer, the
    sensor re-aimed), with the view as clear of traffic as possible.
    """

    def __init__(
        self,
        path: Optional[Path] = None,
        voxel: float = 0.2,
        learn_seconds: float = 20.0,
        occupancy: float = 0.8,
        sensor_serial: Optional[Callable[[], Optional[str]]] = None,
    ):
        self.path = Path(path) if path else None
        self.voxel = float(voxel)
        self.learn_seconds = float(learn_seconds)
        self.occupancy = float(occupancy)

        # The serial of the sensor in use, stamped on what is learned:
        # a background only fits the sensor (and mounting) it came from.
        self._current_serial = sensor_serial or (lambda: None)
        self._sensor_serial: Optional[str] = None

        self._lock = threading.Lock()
        self._table: Optional[np.ndarray] = None
        self._voxels = 0
        self._learned_at: Optional[str] = None
        self._learning: Optional[Dict[str, Any]] = None
        # Newest frame's number and mask; the mask is None when no
        # background had been learned yet for that frame.
        self._latest: Tuple[int, Optional[np.ndarray]] = (-1, None)

        if self.path and self.path.exists():
            self._load()

    # ------------------------------------------------------------ learning

    def learn(self) -> None:
        """
        Start (or restart) learning from the next frames.
        """
        with self._lock:
            self._learning = {"started": None, "frames": [], "count": 0}

    @property
    def learned(self) -> bool:
        return self._table is not None

    def _finish(self, frames: List[np.ndarray], learning: Dict[str, Any]) -> None:
        """
        Build the background from the learning frames. Runs on its own
        thread: it takes a second or two, and the frame loop must not
        stall while the sensor keeps streaming.
        """

        keys, counts = np.unique(np.concatenate(frames), return_counts=True)

        static = keys[counts >= self.occupancy * len(frames)]

        table = _table(static)

        learned_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        serial = self._current_serial()

        with self._lock:

            # A newer learn() started meanwhile; this result is stale.
            if self._learning is not learning:
                return

            self._table = table
            self._voxels = int(static.size)
            self._learned_at = learned_at
            self._sensor_serial = serial
            self._learning = None

        if self.path:
            self._save(static, learned_at, serial)

    def _save(self, static: np.ndarray, learned_at: str, serial: Optional[str]) -> None:

        self.path.parent.mkdir(parents=True, exist_ok=True)

        temporary = self.path.with_name(self.path.stem + ".tmp.npz")

        np.savez_compressed(
            temporary,
            keys=static,
            voxel=np.float64(self.voxel),
            learned_at=np.array(learned_at),
            sensor_serial=np.array(serial or ""),
        )

        os.replace(temporary, self.path)

    def _load(self) -> None:

        with np.load(self.path) as data:

            if abs(float(data["voxel"]) - self.voxel) > 1e-9:
                # Learned at another voxel size; it cannot be reused.
                return

            static = data["keys"]
            self._learned_at = str(data["learned_at"])

            if "sensor_serial" in data.files:
                self._sensor_serial = str(data["sensor_serial"]) or None

        self._table = _table(static)
        self._voxels = int(static.size)

    # -------------------------------------------------------- per frame

    def update(self, xyz: np.ndarray, frame_number: int, now: float) -> np.ndarray:
        """
        Feed one frame's (N, 3) points.

        Returns the foreground mask: True for points that are not
        background. While nothing has been learned yet, every point is
        foreground.
        """

        keys = _keys(np.floor(np.asarray(xyz) / self.voxel))

        with self._lock:

            learning = self._learning

            if learning is not None and not learning.get("finishing"):

                if learning["started"] is None:
                    learning["started"] = now

                learning["frames"].append(np.unique(keys))
                learning["count"] += 1

                if now - learning["started"] >= self.learn_seconds and learning["count"] >= 5:
                    learning["finishing"] = True
                    threading.Thread(
                        target=self._finish,
                        args=(learning.pop("frames"), learning),
                        name="background-learn",
                        daemon=True,
                    ).start()

            if self._table is None:
                mask = np.ones(len(keys), dtype=bool)
                self._latest = (frame_number, None)
            else:
                mask = ~self._table[_slots(keys)]
                self._latest = (frame_number, mask)

        return mask

    def latest(self) -> Tuple[int, Optional[np.ndarray]]:
        """
        The newest classified frame's number and foreground mask, or a
        None mask if that frame came before any background was learned.
        """
        return self._latest

    def status(self) -> Dict[str, Any]:

        with self._lock:

            learning = self._learning
            progress = None

            if learning is not None:
                elapsed = (
                    time.monotonic() - learning["started"]
                    if learning["started"] is not None else 0.0
                )
                progress = {
                    "seconds": round(min(elapsed, self.learn_seconds), 1),
                    "of": self.learn_seconds,
                    "frames": learning["count"],
                    "building": bool(learning.get("finishing")),
                }

            return {
                "learned": self._table is not None,
                "voxels": self._voxels,
                "voxel": self.voxel,
                "learned_at": self._learned_at,
                "sensor_serial": self._sensor_serial,
                "learning": progress,
                "path": str(self.path) if self.path else None,
            }
