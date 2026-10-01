import os
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Optional

import yaml


PROFILES_PATH = Path(__file__).parent / "config" / "profiles.yaml"

ENV_PREFIX = "OPENTRAFFIC_"


@dataclass
class Settings:
    """
    Resolved runtime configuration for one profile.
    """

    profile: str
    source: str
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
    detector_min_points: int = 20
    detector_call_delay: float = 3.0
    background_file: Path = Path("data/background.npz")
    background_voxel: float = 0.2
    background_learn_seconds: float = 20.0

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
        field.name: field.type
        for field in fields(Settings)
        if field.name != "profile"
    }

    unknown = set(values) - set(types)

    if unknown:
        raise ValueError(
            f"Profile {name!r} has unknown settings: "
            f"{', '.join(sorted(unknown))}"
        )

    for key in types:

        from_env = os.environ.get(ENV_PREFIX + key.upper())

        if from_env:
            values[key] = from_env

    values.update(
        {key: value for key, value in overrides.items() if value is not None}
    )

    if not values.get("source"):
        raise ValueError(
            f"Profile {name!r} does not define a source. "
            f"Set it in {PROFILES_PATH}, pass --source, "
            f"or set {ENV_PREFIX}SOURCE."
        )

    return Settings(
        profile=name,
        **{key: _coerce(value, types[key]) for key, value in values.items()},
    )
