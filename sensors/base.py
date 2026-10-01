from abc import ABC, abstractmethod
from typing import Iterator

from .types import PointCloudFrame


class Lidar(ABC):
    """
    Abstract interface for all LiDAR sensors.

    Anything downstream of this class should not need to know
    which manufacturer produced the point cloud.
    """

    @abstractmethod
    def frames(self) -> Iterator[PointCloudFrame]:
        """
        Yield standardized point-cloud frames.
        """
        raise NotImplementedError

    def close(self) -> None:
        """
        Release sensor resources.
        """
        pass