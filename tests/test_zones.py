import numpy as np
import pytest
import yaml

from controllers.base import MAX_CHANNEL
from perception.ground import Plane
from zones import SCHEMA_VERSION, Zone, ZoneStore
from zones.zones import ZoneError, parse_geo

SQUARE = [[0, 0], [4, 0], [4, 4], [0, 4]]


def roi(**fields):
    return {"type": "roi", "polygon": SQUARE, **fields}


# ------------------------------------------------------------ from_dict


def test_defaults():
    zone = Zone.from_dict(roi(), index=2)

    assert zone.type == "roi"
    assert zone.name == "roi 3"
    assert zone.height_min == 0.15 and zone.height_max == 5.0
    assert zone.enabled is True
    assert zone.channel is None
    assert len(zone.id) == 8


def test_type_is_normalized():
    assert Zone.from_dict(roi(type="  ROI ")).type == "roi"


def test_points_are_rounded_to_millimetres():
    zone = Zone.from_dict(roi(polygon=[[0.12345, 1], [2, 0], ["3", "4.0006"]]))

    assert zone.polygon == [[0.123, 1.0], [2.0, 0.0], [3.0, 4.001]]


def test_v1_z_band_carries_over():
    zone = Zone.from_dict(roi(z_min=0.5, z_max=2.5))

    assert (zone.height_min, zone.height_max) == (0.5, 2.5)


@pytest.mark.parametrize("channel", [1, MAX_CHANNEL, "7"])
def test_valid_channel(channel):
    assert Zone.from_dict(roi(channel=channel)).channel == int(channel)


@pytest.mark.parametrize("channel", [None, ""])
def test_blank_channel_means_no_call(channel):
    assert Zone.from_dict(roi(channel=channel)).channel is None


@pytest.mark.parametrize(
    "raw, message",
    [
        ("not a mapping", "not a mapping"),
        (roi(type="lane"), "has type 'lane'"),
        (roi(polygon=[[0, 0], [1, 1]]), "at least 3 points"),
        (roi(polygon=None), "at least 3 points"),
        (roi(polygon=5), "at least 3 points"),
        (roi(polygon=[[0, 0], [1, 1], [2]]), "not [x, y]"),
        (roi(polygon=[[0, 0], [1, 1], ["a", 2]]), "non-numeric point"),
        (roi(height_min="low"), "non-numeric height"),
        (roi(height_min=2, height_max=2), "height_max <= height_min"),
        (roi(channel="x"), "non-numeric channel"),
        (roi(channel=0), "expected 1-"),
        (roi(channel=MAX_CHANNEL + 1), "expected 1-"),
        (roi(type="exclusion", channel=3), "cannot call"),
    ],
)
def test_invalid_zone(raw, message):
    with pytest.raises(ZoneError, match=message.replace("[", r"\[").replace("]", r"\]")):
        Zone.from_dict(raw)


def test_round_trip():
    zone = Zone.from_dict(roi(id="abc", name="NB left", channel=5, enabled=False))

    assert Zone.from_dict(zone.to_dict()) == zone


# ------------------------------------------------------------- contains


def test_contains_2d():
    zone = Zone.from_dict(roi())
    points = np.array([[2, 2], [-1, 2], [5, 2], [2, -0.1], [2, 3.9]])

    assert zone.contains(points).tolist() == [True, False, False, False, True]


def test_contains_concave_polygon():
    # An L: the notch at (3, 3) is outside.
    zone = Zone.from_dict(roi(polygon=[[0, 0], [4, 0], [4, 2], [2, 2], [2, 4], [0, 4]]))
    points = np.array([[1, 1], [3, 1], [1, 3], [3, 3]])

    assert zone.contains(points).tolist() == [True, True, True, False]


def test_height_band_without_roadway_is_sensor_z():
    zone = Zone.from_dict(roi(height_min=0.5, height_max=2.0))
    points = np.array([[2, 2, 0.1], [2, 2, 0.5], [2, 2, 1.0], [2, 2, 2.0], [2, 2, 2.5]])

    assert zone.contains(points).tolist() == [False, True, True, True, False]


def test_height_is_measured_from_the_roadway():
    # Sensor 6 m above the road: a car's roof is at z = -4.5.
    road = Plane.horizontal(z=-6.0)
    zone = Zone.from_dict(roi(height_min=0.15, height_max=3.0))
    points = np.array([[2, 2, -4.5], [2, 2, -6.0], [2, 2, 1.0]])

    assert zone.contains(points, road).tolist() == [True, False, False]
    assert zone.contains(points).tolist() == [False, False, True]


@pytest.mark.parametrize("shape", [(3,), (3, 4), (2, 1)])
def test_contains_rejects_bad_shapes(shape):
    with pytest.raises(ZoneError):
        Zone.from_dict(roi()).contains(np.zeros(shape))


# ------------------------------------------------------------------ geo


def test_geo():
    assert parse_geo(None) is None
    assert parse_geo({}) is None
    assert parse_geo({"lat": "35.0456", "lon": -85.3097, "heading": -90}) == {
        "lat": 35.0456,
        "lon": -85.3097,
        "heading": 270.0,
    }


@pytest.mark.parametrize(
    "raw", [[1, 2], {"lat": 1}, {"lat": "x", "lon": 0}, {"lat": 86, "lon": 0}, {"lat": 0, "lon": 181}]
)
def test_invalid_geo(raw):
    with pytest.raises(ZoneError):
        parse_geo(raw)


# ---------------------------------------------------------------- store


def test_missing_file_loads_empty(tmp_path):
    document = ZoneStore(tmp_path / "zones.yaml").load()

    assert document["version"] == SCHEMA_VERSION
    assert document["zones"] == []
    assert document["roadway"] is None


def test_save_and_load(tmp_path):
    store = ZoneStore(tmp_path / "site" / "zones.yaml")
    road = Plane.horizontal(z=-6.0).to_dict()

    saved = store.save(
        {
            "zones": [roi(id="a", channel=1), roi(id="b", type="exclusion")],
            "roadway": {**road, "fit": {"inliers": 0.9}},
            "geo": {"lat": 35, "lon": -85},
        },
        sensor={"serial": "123"},
    )
    loaded = store.load()

    assert not store.path.with_suffix(".yaml.tmp").exists()
    assert [z["id"] for z in loaded["zones"]] == ["a", "b"]
    assert loaded["zones"] == saved["zones"]
    assert loaded["sensor"] == {"serial": "123"}
    assert loaded["roadway"]["sensor_height"] == 6.0
    assert loaded["roadway"]["fit"] == {"inliers": 0.9}
    assert loaded["geo"] == {"lat": 35.0, "lon": -85.0, "heading": 0.0}


def test_polygons_are_written_inline(tmp_path):
    store = ZoneStore(tmp_path / "zones.yaml")
    store.save({"zones": [roi(id="a")]})

    assert "- [0.0, 0.0]" in store.path.read_text()


@pytest.mark.parametrize(
    "document, message",
    [
        ([], "must be a mapping"),
        ({}, "needs a 'zones' list"),
        ({"zones": [roi(id="a"), roi(id="a")]}, "duplicate zone id"),
        ({"zones": [roi(id="a", channel=2), roi(id="b", channel=2)]}, "channel 2 is used by more"),
        ({"zones": [], "roadway": {"normal": [0, 0, 0]}}, "degenerate"),
        ({"zones": [], "roadway": {"normal": [0, 1]}}, "normal must be"),
        ({"zones": [roi(polygon=[])]}, "at least 3 points"),
    ],
)
def test_save_rejects(tmp_path, document, message):
    store = ZoneStore(tmp_path / "zones.yaml")

    with pytest.raises(ZoneError, match=message):
        store.save(document)

    assert not store.path.exists()


def test_failed_save_keeps_the_previous_file(tmp_path):
    store = ZoneStore(tmp_path / "zones.yaml")
    store.save({"zones": [roi(id="a")]})

    with pytest.raises(ZoneError):
        store.save({"zones": [roi(id="b"), roi(id="b")]})

    assert [z["id"] for z in store.load()["zones"]] == ["a"]


def test_load_falls_back_to_v1_json(tmp_path):
    store = ZoneStore(tmp_path / "zones.yaml")
    store.legacy_path.write_text('{"zones": [{"type": "roi", "polygon": %s, "z_min": 1, "z_max": 2}]}' % SQUARE)

    document = store.load()

    assert document["migrated_from"] == str(store.legacy_path)
    assert (document["zones"][0]["height_min"], document["zones"][0]["height_max"]) == (1.0, 2.0)


def test_yaml_wins_over_legacy_json(tmp_path):
    store = ZoneStore(tmp_path / "zones.yaml")
    store.legacy_path.write_text('{"zones": []}')
    store.save({"zones": [roi(id="new")]})

    document = store.load()

    assert "migrated_from" not in document
    assert [z["id"] for z in document["zones"]] == ["new"]


@pytest.mark.parametrize("text, message", [(": : :\n  - [", "not valid YAML"), ("- just\n- a list\n", "mapping")])
def test_load_rejects_bad_files(tmp_path, text, message):
    store = ZoneStore(tmp_path / "zones.yaml")
    store.path.write_text(text)

    with pytest.raises(ZoneError, match=message):
        store.load()


def test_saved_file_is_plain_yaml(tmp_path):
    store = ZoneStore(tmp_path / "zones.yaml")
    store.save({"zones": [roi(id="a", channel=3)]})

    raw = yaml.safe_load(store.path.read_text())

    assert raw["version"] == SCHEMA_VERSION
    assert raw["zones"][0]["channel"] == 3
