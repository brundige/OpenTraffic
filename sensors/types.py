from dataclasses import dataclass
from typing import Optional

import numpy as np


@dataclass
class PointCloudFrame:
    xyz: np.ndarray
    timestamp: float
    intensity: Optional[np.ndarray] = None
    sensor_name: str = ""
    frame_number: int = 0

    @property
    def point_count(self) -> int:
        # (H, W, 3) from a live sensor, (N, 3) from a replayed clip.
        return int(np.prod(self.xyz.shape[:-1]))

    @property
    def flat_xyz(self) -> np.ndarray:
        return self.xyz.reshape(-1, 3)