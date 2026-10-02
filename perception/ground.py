from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Tuple

import numpy as np


@dataclass
class Plane:
    """
    The road surface in sensor XYZ coordinates.

    Held as `normal . p + offset = 0` with a unit normal oriented so
    the sensor side is positive. A pole-mounted sensor is never level
    with the road it watches, so the roadway is a tilted plane in the
    sensor frame and a constant z is not good enough: over a 50 m
    approach a 3 degree tilt is 2.6 m of z.
    """

    normal: np.ndarray
    offset: float

    def __post_init__(self):
        self.normal = np.asarray(self.normal, dtype=float).reshape(3)

        length = float(np.linalg.norm(self.normal))

        if length < 1e-9:
            raise ValueError("roadway normal is degenerate")

        self.normal = self.normal / length
        self.offset = float(self.offset) / length

    @classmethod
    def horizontal(cls, z: float = 0.0) -> "Plane":
        return cls(normal=np.array([0.0, 0.0, 1.0]), offset=-float(z))

    def height(self, points: np.ndarray) -> np.ndarray:
        """
        Signed height above the road, in metres.
        """

        points = np.asarray(points, dtype=float)

        return points @ self.normal + self.offset

    def z_at(self, x, y):
        """
        Road surface z at a given x, y. Vertical only if the plane is
        not itself vertical.
        """

        nx, ny, nz = self.normal

        if abs(nz) < 1e-6:
            raise ValueError("roadway is vertical; no z for a given x, y")

        return -(nx * np.asarray(x) + ny * np.asarray(y) + self.offset) / nz

    @property
    def tilt_deg(self) -> float:
        """
        Angle between the road and the sensor's own horizontal.
        """

        return float(np.degrees(np.arccos(min(1.0, abs(self.normal[2])))))

    @property
    def sensor_height(self) -> float:
        """
        How far the sensor sits above the road: the height of the
        origin, which is just the offset.
        """

        return float(self.offset)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "normal": [round(float(v), 6) for v in self.normal],
            "offset": round(float(self.offset), 4),
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Plane":

        if not isinstance(raw, dict):
            raise ValueError("roadway must be a mapping")

        normal = raw.get("normal")

        if not isinstance(normal, (list, tuple)) or len(normal) != 3:
            raise ValueError("roadway normal must be [x, y, z]")

        try:
            return cls(
                normal=[float(v) for v in normal],
                offset=float(raw.get("offset", 0.0)),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"roadway is not numeric: {exc}")


def fit_plane(
    points: np.ndarray,
    threshold: float = 0.12,
    iterations: int = 80,
    seed: int = 0,
) -> Tuple[Plane, Dict[str, Any]]:
    """
    Fit the road surface to a point cloud.

    RANSAC first, because a scene is mostly not road -- vehicles,
    kerbs, poles and foliage would all drag a plain least-squares fit
    off the surface -- then least squares over the inliers for
    precision.

    Args:
        points: (N, 3) array in sensor metres.
        threshold: inlier distance, metres.
        iterations: RANSAC trials.

    Returns:
        The plane, and stats describing how well it fitted.
    """

    points = np.asarray(points, dtype=float)

    if points.ndim != 2 or points.shape[1] != 3:
        raise ValueError("fit_plane expects an (N, 3) array")

    if len(points) < 3:
        raise ValueError("need at least 3 points to fit a roadway")

    rng = np.random.default_rng(seed)

    # RANSAC on a subsample: a full frame is ~90k points and the
    # inlier count does not need all of them to rank a candidate.
    sample = points

    if len(points) > 20000:
        sample = points[rng.choice(len(points), 20000, replace=False)]

    best_inliers = None
    best_count = 0

    for _ in range(iterations):

        trio = sample[rng.choice(len(sample), 3, replace=False)]

        normal = np.cross(trio[1] - trio[0], trio[2] - trio[0])

        length = np.linalg.norm(normal)

        if length < 1e-9:
            continue

        normal = normal / length
        offset = -float(normal @ trio[0])

        inliers = np.abs(sample @ normal + offset) < threshold

        count = int(inliers.sum())

        if count > best_count:
            best_count = count
            best_inliers = (normal, offset)

    if best_inliers is None:
        raise ValueError("no plane found in this cloud")

    normal, offset = best_inliers

    # Refine on every inlier in the full cloud, not just the sample.
    inliers = points[np.abs(points @ normal + offset) < threshold]

    if len(inliers) >= 3:
        centroid = inliers.mean(axis=0)
        _, _, vt = np.linalg.svd(inliers - centroid, full_matrices=False)
        normal = vt[2]
        offset = -float(normal @ centroid)

    # Orient so the sensor, at the origin, is on the positive side.
    if offset < 0:
        normal, offset = -normal, -offset

    plane = Plane(normal=normal, offset=offset)

    residuals = plane.height(inliers) if len(inliers) else np.zeros(1)

    stats = {
        "points": int(len(points)),
        "inliers": int(len(inliers)),
        "rms": round(float(np.sqrt(np.mean(residuals ** 2))), 4),
        "threshold": threshold,
        "tilt_deg": round(plane.tilt_deg, 2),
        "sensor_height": round(plane.sensor_height, 3),
    }

    return plane, stats
