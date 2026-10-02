import os

import numpy as np
import pytest

from perception.ground import Plane
from perception.presence import PresenceDetector
from zones import ZoneStore

NB = {"id": "nb", "name": "NB", "type": "roi", "channel": 1,
      "polygon": [[0, 0], [4, 0], [4, 4], [0, 4]]}
SB = {"id": "sb", "name": "SB", "type": "roi", "channel": 2,
      "polygon": [[10, 0], [14, 0], [14, 4], [10, 4]]}


def car(n, x=2.0, y=2.0, z=1.0):
    """n returns clustered around (x, y) at height z."""
    rng = np.random.default_rng(n)
    offsets = rng.uniform(-0.5, 0.5, size=(n, 2))
    return np.column_stack([x + offsets[:, 0], y + offsets[:, 1], np.full(n, z)])


EMPTY = np.empty((0, 3))


class Site:
    """A zone file and a detector watching it."""

    def __init__(self, tmp_path, zones, roadway=None, **options):
        self.store = ZoneStore(tmp_path / "zones.yaml")
        self.mtime = 1_000_000_000
        self.save(zones, roadway)
        self.detector = PresenceDetector(self.store, **options)

    def save(self, zones, roadway=None):
        self.store.save({"zones": zones, "roadway": roadway})
        # Step the mtime explicitly so a reload is never missed to
        # filesystem timestamp resolution.
        self.mtime += 10
        os.utime(self.store.path, (self.mtime, self.mtime))

    def update(self, points, now):
        return self.detector.update(points, now=now)


@pytest.fixture
def site(tmp_path):
    return Site(tmp_path, [NB], min_points=10, call_delay=3.0)


def test_empty_zone_does_not_call(site):
    assert site.update(EMPTY, 0.0) == [(1, False, "NB")]
    assert site.update(EMPTY, 1.0) == []


def test_call_waits_for_the_delay(site):
    site.update(EMPTY, 0.0)

    assert site.update(car(10), 10.0) == []
    assert site.update(car(10), 12.9) == []
    assert site.update(car(10), 13.0) == [(1, True, "NB")]
    assert site.update(car(10), 20.0) == []


def test_call_drops_as_soon_as_the_zone_clears(site):
    site.update(car(10), 0.0)
    site.update(car(10), 3.0)

    assert site.update(EMPTY, 3.1) == [(1, False, "NB")]


def test_passing_vehicle_does_not_call(site):
    site.update(EMPTY, 0.0)

    for t in (1.0, 2.0, 3.5):
        assert site.update(car(10), t) == []
    assert site.update(EMPTY, 3.9) == []


def test_a_break_restarts_the_delay(site):
    site.update(car(10), 0.0)
    site.update(EMPTY, 2.0)
    site.update(car(10), 2.5)

    assert site.update(car(10), 5.0) == []
    assert site.update(car(10), 5.5) == [(1, True, "NB")]


def test_below_min_points_is_not_present(site):
    site.update(car(9), 0.0)

    assert site.update(car(9), 10.0) == []
    assert site.detector.status()[0]["present"] is False


def test_hysteresis_holds_until_half(site):
    site.update(car(10), 0.0)
    site.update(car(10), 3.0)

    # Still present down to min_points / 2...
    assert site.update(car(5), 4.0) == []
    assert site.detector.status()[0]["calling"] is True
    # ...and clears below it.
    assert site.update(car(4), 5.0) == [(1, False, "NB")]
    # Re-entering needs the full min_points again.
    assert site.update(car(9), 6.0) == []
    assert site.detector.status()[0]["present"] is False


def test_points_outside_the_zone_or_height_band_do_not_count(site):
    site.update(car(10, x=6.0), 0.0)
    site.update(car(10, z=0.05), 0.1)
    site.update(car(10, z=6.0), 0.2)

    assert site.update(car(10, x=6.0), 10.0) == []
    assert site.detector.status()[0]["present"] is False


def test_heights_follow_the_roadway(tmp_path):
    site = Site(tmp_path, [NB], roadway=Plane.horizontal(z=-6.0).to_dict(),
                min_points=10, call_delay=0.0)

    # 1 m above sensor z is 7 m above this road: out of the band.
    assert site.update(car(10, z=1.0), 0.0) == [(1, False, "NB")]
    assert site.update(car(10, z=-5.0), 1.0) == [(1, True, "NB")]


def test_exclusion_zone_removes_points(tmp_path):
    pole = {"id": "pole", "type": "exclusion",
            "polygon": [[1, 1], [3, 1], [3, 3], [1, 3]]}
    site = Site(tmp_path, [NB, pole], min_points=10, call_delay=0.0)

    # Everything inside the exclusion: nothing left to count.
    assert site.update(car(30), 0.0) == [(1, False, "NB")]
    # The same returns at a corner of NB outside the exclusion do count.
    assert site.update(car(30, x=3.6, y=3.6), 1.0) == [(1, True, "NB")]


def test_disabled_or_unchanneled_zones_never_call(tmp_path):
    zones = [{**NB, "enabled": False}, {**SB, "channel": None}]
    site = Site(tmp_path, zones, min_points=10, call_delay=0.0)

    assert site.update(car(10), 0.0) == []
    assert site.update(car(10, x=12.0), 1.0) == []
    assert site.detector.status() == []


def test_channels_are_independent(tmp_path):
    site = Site(tmp_path, [NB, SB], min_points=10, call_delay=0.0)
    site.update(EMPTY, 0.0)

    assert site.update(car(10, x=12.0), 1.0) == [(2, True, "SB")]
    both = np.vstack([car(10), car(10, x=12.0)])
    assert site.update(both, 2.0) == [(1, True, "NB")]
    assert site.update(car(10), 3.0) == [(2, False, "SB")]


def test_zone_edits_are_picked_up_without_a_restart(tmp_path):
    site = Site(tmp_path, [NB], min_points=10, call_delay=0.0)
    site.update(EMPTY, 0.0)

    site.save([NB, SB])

    assert site.update(car(10, x=12.0), 1.0) == [(2, True, "SB")]


def test_removing_a_calling_zone_drops_its_call(tmp_path):
    site = Site(tmp_path, [NB, SB], min_points=10, call_delay=0.0)
    site.update(np.vstack([car(10), car(10, x=12.0)]), 0.0)

    site.save([NB])

    assert site.update(car(10), 1.0) == [(2, False, "")]
    assert [z["channel"] for z in site.detector.status()] == [1]


def test_removing_the_last_zone_drops_its_call(tmp_path):
    site = Site(tmp_path, [NB], min_points=10, call_delay=0.0)
    site.update(car(10), 0.0)

    site.save([])

    assert site.update(car(10), 1.0) == [(1, False, "")]
    assert site.update(car(10), 2.0) == []


def test_status_reports_the_wait(site, monkeypatch):
    monkeypatch.setattr("perception.presence.time.monotonic", lambda: 101.25)

    site.update(car(10), 100.0)
    [status] = site.detector.status()

    assert status == {"channel": 1, "name": "NB", "present": True,
                      "waited": 1.2, "delay": 3.0, "calling": False}


def test_options_are_clamped(tmp_path):
    site = Site(tmp_path, [NB], min_points=0, call_delay=-5)

    assert site.detector.min_points == 1
    assert site.detector.call_delay == 0.0
