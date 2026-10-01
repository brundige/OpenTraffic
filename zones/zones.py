from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import yaml

from controllers.base import MAX_CHANNEL
from perception.ground import Plane


SCHEMA_VERSION = 2

# ROI zones say where to look for vehicles; exclusion zones carve
# permanent clutter (kerbs, buildings, the pole itself) back out.
ZONE_TYPES = ("roi", "exclusion")


class ZoneError(ValueError):
    """
    Raised when a zone document is malformed.
    """


class _Flow(list):
    """
    A list that YAML writes inline, so a polygon reads as a column of
    [x, y] pairs rather than 200 lines of one number each.
    """


yaml.add_representer(
    _Flow,
    lambda dumper, data: dumper.represent_sequence(
        "tag:yaml.org,2002:seq", data, flow_style=True
    ),
)


@dataclass
class Zone:
    """
    A polygon on the roadway, in metres.

    The footprint is sensor-frame x/y. The vertical extent is height
    ABOVE THE ROADWAY, not sensor z: the sensor looks down at an
    angle, so a fixed z band would drift off the road across an
    approach. A car is 0 to 2 m above the tarmac wherever it is.
    """

    id: str
    name: str
    type: str
    polygon: List[List[float]]
    height_min: float = 0.15
    height_max: float = 5.0
    enabled: bool = True
    # Detector channel this ROI places its call on, or None to watch
    # without calling. Exclusion zones never call.
    channel: Optional[int] = None

    @classmethod
    def from_dict(cls, raw: Dict[str, Any], index: int = 0) -> "Zone":

        where = f"zone {index}"

        if not isinstance(raw, dict):
            raise ZoneError(f"{where} is not a mapping")

        zone_type = str(raw.get("type", "")).strip().lower()

        if zone_type not in ZONE_TYPES:
            raise ZoneError(
                f"{where} has type {zone_type!r}, "
                f"expected one of {', '.join(ZONE_TYPES)}"
            )

        polygon = raw.get("polygon") or []

        if not isinstance(polygon, list) or len(polygon) < 3:
            raise ZoneError(
                f"{where} needs at least 3 points, got {len(polygon)}"
            )

        points: List[List[float]] = []

        for point in polygon:

            if not isinstance(point, (list, tuple)) or len(point) != 2:
                raise ZoneError(f"{where} has a point that is not [x, y]")

            try:
                points.append(
                    [round(float(point[0]), 3), round(float(point[1]), 3)]
                )
            except (TypeError, ValueError):
                raise ZoneError(f"{where} has a non-numeric point")

        # v1 documents carried z_min/z_max in sensor z. Without a
        # roadway they meant the same thing, so they carry over.
        low = raw.get("height_min", raw.get("z_min", 0.15))
        high = raw.get("height_max", raw.get("z_max", 5.0))

        try:
            height_min, height_max = float(low), float(high)
        except (TypeError, ValueError):
            raise ZoneError(f"{where} has a non-numeric height")

        if height_max <= height_min:
            raise ZoneError(f"{where} has height_max <= height_min")

        name = str(raw.get("name") or "").strip()

        channel = raw.get("channel")

        if channel in (None, ""):
            channel = None
        else:
            try:
                channel = int(channel)
            except (TypeError, ValueError):
                raise ZoneError(f"{where} has a non-numeric channel")

            if not 1 <= channel <= MAX_CHANNEL:
                raise ZoneError(
                    f"{where} has channel {channel}, expected 1-{MAX_CHANNEL}"
                )

            if zone_type != "roi":
                raise ZoneError(f"{where} is an exclusion zone and cannot call")

        return cls(
            id=str(raw.get("id") or uuid.uuid4().hex[:8]),
            name=name or f"{zone_type} {index + 1}",
            type=zone_type,
            polygon=points,
            height_min=height_min,
            height_max=height_max,
            enabled=bool(raw.get("enabled", True)),
            channel=channel,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "polygon": [_Flow(point) for point in self.polygon],
            "height_min": self.height_min,
            "height_max": self.height_max,
            "enabled": self.enabled,
            "channel": self.channel,
        }

    def contains(
        self,
        points: np.ndarray,
        roadway: Optional[Plane] = None,
    ) -> np.ndarray:
        """
        Mask of the points inside this zone.

        Args:
            points: (N, 3) or (N, 2) array of sensor-frame metres.
            roadway: the road surface heights are measured from.
                Defaults to the sensor's own horizontal through the
                origin, which makes height the same as z.

        Returns:
            (N,) boolean array.
        """

        points = np.asarray(points, dtype=float)

        if points.ndim != 2 or points.shape[1] not in (2, 3):
            raise ZoneError("contains() expects an (N, 2) or (N, 3) array")

        x = points[:, 0]
        y = points[:, 1]

        polygon = np.asarray(self.polygon, dtype=float)

        px = polygon[:, 0]
        py = polygon[:, 1]

        # Ray casting: count polygon edges crossed by a ray heading
        # in +x from each point.
        inside = np.zeros(x.shape, dtype=bool)

        qx = np.roll(px, -1)
        qy = np.roll(py, -1)

        for x1, y1, x2, y2 in zip(px, py, qx, qy):

            if y1 == y2:
                continue

            straddles = (y1 > y) != (y2 > y)

            with np.errstate(invalid="ignore"):
                crossing_x = x1 + (y - y1) * (x2 - x1) / (y2 - y1)

            inside ^= straddles & (x < crossing_x)

        if points.shape[1] == 3:
            plane = roadway or Plane.horizontal()
            height = plane.height(points)
            inside &= (height >= self.height_min) & (height <= self.height_max)

        return inside


def parse_geo(raw: Any) -> Optional[Dict[str, float]]:
    """
    Where the sensor is on the map: lat/lon of the sensor origin in
    degrees, and heading, the compass bearing (degrees clockwise from
    north) that the sensor's +x axis points along. None clears it.
    """

    if raw in (None, {}):
        return None

    if not isinstance(raw, dict):
        raise ZoneError("geo must be a mapping")

    try:
        lat = float(raw["lat"])
        lon = float(raw["lon"])
        heading = float(raw.get("heading", 0.0)) % 360.0
    except (KeyError, TypeError, ValueError):
        raise ZoneError("geo needs numeric lat and lon (and heading)")

    if not -85.0 <= lat <= 85.0:
        raise ZoneError(f"geo lat {lat} is outside -85..85")

    if not -180.0 <= lon <= 180.0:
        raise ZoneError(f"geo lon {lon} is outside -180..180")

    return {
        "lat": round(lat, 7),
        "lon": round(lon, 7),
        "heading": round(heading, 2),
    }


class ZoneStore:
    """
    Reads and writes the roadway and zones as YAML.
    """

    def __init__(self, path: Path):
        self.path = Path(path)

    @property
    def legacy_path(self) -> Path:
        """
        Where a pre-YAML document would have been written.
        """

        return self.path.with_suffix(".json")

    def load(self) -> Dict[str, Any]:

        source = self.path

        if not source.exists():

            # Fall back to a v1 JSON document so an existing site
            # keeps its zones; the next save writes YAML.
            if self.legacy_path.exists():
                source = self.legacy_path
            else:
                return self._document([])

        try:
            raw = yaml.safe_load(source.read_text()) or {}
        except yaml.YAMLError as exc:
            raise ZoneError(f"{source} is not valid YAML: {exc}")

        if not isinstance(raw, dict):
            raise ZoneError(f"{source} does not contain a mapping")

        zones = [
            Zone.from_dict(entry, index)
            for index, entry in enumerate(raw.get("zones") or [])
        ]

        roadway = raw.get("roadway")

        return self._document(
            zones,
            sensor=raw.get("sensor") or {},
            updated=raw.get("updated"),
            roadway=Plane.from_dict(roadway) if roadway else None,
            roadway_fit=(roadway or {}).get("fit"),
            geo=parse_geo(raw.get("geo")),
            migrated_from=str(source) if source != self.path else None,
        )

    def save(
        self,
        raw: Dict[str, Any],
        sensor: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Validate a document and write it atomically.

        Returns the normalized document that was written, so the
        caller never has to trust what the browser sent.
        """

        if not isinstance(raw, dict):
            raise ZoneError("document must be a mapping")

        entries = raw.get("zones")

        if not isinstance(entries, list):
            raise ZoneError("document needs a 'zones' list")

        zones = [
            Zone.from_dict(entry, index)
            for index, entry in enumerate(entries)
        ]

        seen = set()

        for zone in zones:

            if zone.id in seen:
                raise ZoneError(f"duplicate zone id {zone.id!r}")

            seen.add(zone.id)

        channels = [zone.channel for zone in zones if zone.channel]

        for channel in set(channels):
            if channels.count(channel) > 1:
                raise ZoneError(
                    f"detector channel {channel} is used by more than one zone"
                )

        roadway = raw.get("roadway")

        try:
            plane = Plane.from_dict(roadway) if roadway else None
        except ValueError as exc:
            raise ZoneError(str(exc))

        document = self._document(
            zones,
            sensor=sensor if sensor is not None else raw.get("sensor") or {},
            roadway=plane,
            roadway_fit=(roadway or {}).get("fit"),
            geo=parse_geo(raw.get("geo")),
        )

        self.path.parent.mkdir(parents=True, exist_ok=True)

        # Write beside the target and rename, so a crash mid-write
        # cannot leave the field unit with a half-written config.
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")

        temporary.write_text(
            yaml.dump(document, sort_keys=False, default_flow_style=False)
        )

        os.replace(temporary, self.path)

        return document

    def _document(
        self,
        zones: List[Zone],
        sensor: Optional[Dict[str, Any]] = None,
        updated: Optional[str] = None,
        roadway: Optional[Plane] = None,
        roadway_fit: Optional[Dict[str, Any]] = None,
        geo: Optional[Dict[str, float]] = None,
        migrated_from: Optional[str] = None,
    ) -> Dict[str, Any]:

        document: Dict[str, Any] = {
            "version": SCHEMA_VERSION,
            "updated": updated
            or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "sensor": sensor or {},
            "roadway": None,
            "geo": geo,
            "zones": [zone.to_dict() for zone in zones],
        }

        if roadway is not None:

            entry = roadway.to_dict()
            entry["normal"] = _Flow(entry["normal"])
            entry["sensor_height"] = round(roadway.sensor_height, 3)
            entry["tilt_deg"] = round(roadway.tilt_deg, 2)

            if roadway_fit:
                entry["fit"] = roadway_fit

            document["roadway"] = entry

        if migrated_from:
            document["migrated_from"] = migrated_from

        return document
