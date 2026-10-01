import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, Optional, Set

import yaml


PROFILES_PATH = Path(__file__).parent / "config" / "profiles.yaml"

ENV_PREFIX = "OPENTRAFFIC_"

# Settings that belong to the cabinet rather than the build: set from
# the inspector and kept in the unit's site file (data/site.yaml), so
# installing a unit needs no editing of files or environment.
SITE_KEYS = (
    "controller",
    "controller_host",
    "controller_local_address",
    "controller_interface",
)


@dataclass
class Settings:
    """
    Resolved runtime configuration for one profile.
    """

    profile: str
    # A sensor address, "auto" to find it on the network, or a recording.
    source: str
    # Limit the "auto" search to one interface (the LiDAR port); empty
    # searches every interface.
    sensor_interface: str = ""
    lidar_port: int = 7502
    imu_port: int = 7503
    frame_timeout: float = 10.0
    stats_interval: float = 1.0
    data_dir: Path = Path("data")
    zones_file: Path = Path("data/zones.yaml")
    clip_dir: Path = Path("data/clips")
    retain_seconds: float = 30.0
    retain_max_mb: int = 256
    # The detector's own API: no login, so loopback only.
    api_host: str = "127.0.0.1"
    api_port: int = 8081
    cloud_max_points: int = 60000
    # The one network-facing listener the detector always runs.
    health_host: str = "0.0.0.0"
    health_port: int = 8090
    # The inspector (login + page). Embedded runs it inside the detector,
    # for dev; otherwise it is the separate sidecar (python -m inspector).
    inspector_embedded: bool = False
    inspector_host: str = "127.0.0.1"
    inspector_port: int = 8080
    session_hours: float = 8.0
    auth_file: Path = Path("data/auth.json")
    controller: str = "simulator"
    controller_host: str = ""
    controller_port: int = 10001
    controller_listen_port: int = 10001
    controller_forward_port: int = 10002
    controller_timeout: int = 5
    # An address this unit takes for the adapter's sake -- the one its
    # command/forward IP points at -- as CIDR, on controller_interface.
    # Set by the inspector's "Use it"; re-applied at every start.
    controller_local_address: str = ""
    controller_interface: str = ""
    detector_min_points: int = 20
    detector_call_delay: float = 3.0
    background_file: Path = Path("data/background.npz")
    background_voxel: float = 0.2
    background_learn_seconds: float = 20.0
    site_file: Path = Path("data/site.yaml")
    # Which settings the environment or command line fixed, so the
    # inspector can say it cannot change them.
    pinned: Set[str] = field(default_factory=set)

    @property
    def source_is_recording(self) -> bool:
        """
        True when the source is a file on disk rather than a sensor.
        """
        return Path(self.source).exists()


def _coerce(value: Any, target: type) -> Any:

    if isinstance(value, target):
        return value

    if target is Path:
        return Path(value)

    if target is bool:
        # environment variables arrive as strings
        return str(value).strip().lower() in ("1", "true", "yes", "on")

    return target(value)


def load_settings(
    profile: Optional[str] = None,
    **overrides: Any,
) -> Settings:
    """
    Build the settings for a profile.

    Precedence, highest first:
      1. keyword overrides (the command line)
      2. OPENTRAFFIC_<FIELD> environment variables
      3. the profile block in config/profiles.yaml
      4. the defaults on Settings
    """

    document = yaml.safe_load(PROFILES_PATH.read_text()) or {}

    profiles = document.get("profiles") or {}

    name = (
        profile
        or os.environ.get(ENV_PREFIX + "PROFILE")
        or document.get("default_profile")
    )

    if name not in profiles:

        available = ", ".join(sorted(profiles)) or "none"

        raise ValueError(
            f"Unknown profile {name!r}. "
            f"Available profiles: {available} "
            f"(defined in {PROFILES_PATH})"
        )

    values = dict(profiles[name] or {})

    types = {
        item.name: item.type
        for item in fields(Settings)
        if item.name not in ("profile", "pinned")
    }

    unknown = set(values) - set(types)

    if unknown:
        raise ValueError(
            f"Profile {name!r} has unknown settings: "
            f"{', '.join(sorted(unknown))}"
        )

    site_file = Path(values.get("site_file") or Settings.site_file)

    values.update(read_site(site_file))

    pinned = set()

    for key in types:

        from_env = os.environ.get(ENV_PREFIX + key.upper())

        if from_env:
            values[key] = from_env
            pinned.add(key)

    given = {key: value for key, value in overrides.items() if value is not None}

    values.update(given)
    pinned.update(given)

    if not values.get("source"):
        raise ValueError(
            f"Profile {name!r} does not define a source. "
            f"Set it in {PROFILES_PATH}, pass --source, "
            f"or set {ENV_PREFIX}SOURCE."
        )

    return Settings(
        profile=name,
        pinned=pinned,
        **{key: _coerce(value, types[key]) for key, value in values.items()},
    )


def read_site(path: Path) -> Dict[str, Any]:
    """
    The unit's site settings, or {} if none have been saved yet.
    """

    try:
        document = yaml.safe_load(Path(path).read_text()) or {}
    except FileNotFoundError:
        return {}

    if not isinstance(document, dict):
        raise ValueError(f"{path} is not a mapping")

    unknown = set(document) - set(SITE_KEYS)

    if unknown:
        raise ValueError(f"{path} has unknown settings: {', '.join(sorted(unknown))}")

    return document


def write_site(path: Path, updates: Dict[str, Any]) -> Dict[str, Any]:
    """
    Merge updates into the site file, atomically. Returns the result.
    """

    path = Path(path)

    document = {**read_site(path), **{k: updates[k] for k in SITE_KEYS if k in updates}}

    path.parent.mkdir(parents=True, exist_ok=True)

    temporary = path.with_name(path.name + ".tmp")

    temporary.write_text(
        "# This unit's site settings, written by the inspector.\n"
        + yaml.safe_dump(document, sort_keys=False)
    )

    os.replace(temporary, path)

    return document
