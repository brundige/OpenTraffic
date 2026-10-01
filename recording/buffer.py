from __future__ import annotations

import json
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np


CLIP_VERSION = 1


# Coordinates are held as centimetres in an int16. The OS-1 tops out
# at 120 m so +-327 m is ample headroom, and 1 cm quantisation sits
# well inside the sensor's own +-3 cm range accuracy. It halves the
# cost of a point against float32, which is the difference between
# holding the full 30 s on a Jetson and holding 21 s of it.
CM = 100.0
CM_LIMIT = 32767


@dataclass
class Snapshot:
    """
    One frame, compacted for retention.

    The pipeline hands us xyz as float64 over every pixel, which is
    3.1 MB a frame -- 944 MB for 30 s. Points with no return sit at
    the origin and carry nothing, so they are dropped, and what is
    left is quantised to centimetres.
    """

    frame_number: int
    timestamp: float
    received: float
    xyz_cm: np.ndarray
    intensity: Optional[np.ndarray]

    @property
    def xyz(self) -> np.ndarray:
        """
        Points as float32 metres.
        """
        return self.xyz_cm.astype(np.float32) / CM

    @property
    def points(self) -> int:
        return self.xyz_cm.shape[0]

    @property
    def nbytes(self) -> int:
        return self.xyz_cm.nbytes + (
            self.intensity.nbytes if self.intensity is not None else 0
        )


def compact(frame) -> Snapshot:

    xyz = np.asarray(frame.flat_xyz, dtype=np.float32)

    valid = np.any(xyz != 0.0, axis=1)

    intensity = None

    if frame.intensity is not None:
        flat = np.asarray(frame.intensity).reshape(-1)[valid]
        intensity = flat.astype(np.uint16, copy=False)

    # Rounded, not truncated, so the error is +-0.5 cm rather than
    # biased toward the sensor.
    centimetres = np.rint(xyz[valid] * CM)
    np.clip(centimetres, -CM_LIMIT, CM_LIMIT, out=centimetres)

    return Snapshot(
        frame_number=frame.frame_number,
        timestamp=frame.timestamp,
        received=time.monotonic(),
        xyz_cm=np.ascontiguousarray(centimetres.astype(np.int16)),
        intensity=intensity,
    )


class RollingBuffer:
    """
    The last `seconds` of frames, kept in memory.

    Retention is by arrival time rather than sensor timestamp, so a
    sensor clock that is unset or wraps cannot empty the buffer.

    A dense scene costs more per frame than a sparse one, so the
    window is also capped in bytes; when the cap bites, the buffer
    holds less than `seconds` and says so in stats().
    """

    def __init__(self, seconds: float = 30.0, max_bytes: int = 256 << 20):
        self.seconds = float(seconds)
        self.max_bytes = int(max_bytes)

        self._lock = threading.Lock()
        self._frames: deque = deque()
        self._bytes = 0
        self._published = 0
        self._evicted_by_cap = 0

    # ------------------------------------------------------------ writing

    def publish(self, frame) -> Snapshot:

        snapshot = compact(frame)

        with self._lock:

            self._frames.append(snapshot)
            self._bytes += snapshot.nbytes
            self._published += 1

            cutoff = snapshot.received - self.seconds

            while len(self._frames) > 1 and self._frames[0].received < cutoff:
                self._bytes -= self._frames.popleft().nbytes

            while len(self._frames) > 1 and self._bytes > self.max_bytes:
                self._bytes -= self._frames.popleft().nbytes
                self._evicted_by_cap += 1

        return snapshot

    # ------------------------------------------------------------ reading

    def latest(self) -> Optional[Snapshot]:

        with self._lock:
            return self._frames[-1] if self._frames else None

    def snapshots(self) -> List[Snapshot]:

        with self._lock:
            return list(self._frames)

    def stats(self) -> Dict[str, Any]:
        """
        What the buffer is actually holding, as opposed to what it
        was asked to hold.
        """

        with self._lock:

            frames = len(self._frames)

            span = 0.0

            if frames > 1:
                span = self._frames[-1].received - self._frames[0].received

            return {
                "frames": frames,
                "seconds": round(span, 2),
                "requested_seconds": self.seconds,
                "megabytes": round(self._bytes / 1e6, 1),
                "max_megabytes": round(self.max_bytes / 1e6, 1),
                "capped": self._evicted_by_cap > 0,
                "published": self._published,
                "points": self._frames[-1].points if frames else 0,
            }

    # ------------------------------------------------------------ writing out

    def save(self, path: Path, sensor: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Write everything held to a .npz clip, replayable with
        --source <clip>.

        Frames are variable length, so points are concatenated and
        indexed by an offsets array rather than padded to a
        rectangle.
        """

        frames = self.snapshots()

        if not frames:
            raise ValueError("nothing buffered yet")

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        counts = np.array([f.points for f in frames], dtype=np.int64)

        offsets = np.zeros(len(frames) + 1, dtype=np.int64)
        np.cumsum(counts, out=offsets[1:])

        xyz_cm = np.concatenate([f.xyz_cm for f in frames])

        has_intensity = all(f.intensity is not None for f in frames)

        intensity = (
            np.concatenate([f.intensity for f in frames])
            if has_intensity
            else np.zeros(0, dtype=np.uint16)
        )

        meta = {
            "version": CLIP_VERSION,
            "saved": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "sensor": sensor or {},
            "frames": len(frames),
            "seconds": round(frames[-1].received - frames[0].received, 2),
            "points": int(counts.sum()),
            "units": "centimetres",
        }

        # Uncompressed: a 150 MB clip is written from the inspector
        # thread while the detector keeps ingesting, and zlib on that
        # much float32 would stall for seconds for little gain.
        np.savez(
            path,
            meta=np.array(json.dumps(meta)),
            frame_numbers=np.array([f.frame_number for f in frames], dtype=np.int64),
            timestamps=np.array([f.timestamp for f in frames], dtype=np.float64),
            offsets=offsets,
            xyz_cm=xyz_cm,
            intensity=intensity,
        )

        meta["path"] = str(path)
        meta["megabytes"] = round(path.stat().st_size / 1e6, 1)

        return meta


def load_clip(path: Path) -> Dict[str, Any]:
    """
    Read a clip back into its metadata and per-frame arrays.
    """

    with np.load(Path(path), allow_pickle=False) as data:

        meta = json.loads(str(data["meta"]))

        offsets = data["offsets"]
        xyz = data["xyz_cm"].astype(np.float32) / CM
        intensity = data["intensity"]

        frames = []

        for index in range(len(offsets) - 1):

            start, stop = int(offsets[index]), int(offsets[index + 1])

            frames.append(
                {
                    "frame_number": int(data["frame_numbers"][index]),
                    "timestamp": float(data["timestamps"][index]),
                    "xyz": xyz[start:stop],
                    "intensity": intensity[start:stop] if intensity.size else None,
                }
            )

    return {"meta": meta, "frames": frames}
