from pathlib import Path

import httpx
import pytest

from osm_family_data.parking import (
    fetch_parking,
    from_osm,
    is_car_park,
    parking_queries,
    replace_area,
    squares,
    write_parking,
)
from osm_family_data.places import OverpassError, chosen_areas, load_places_config, read_places
from osm_family_data.storage import LocalObjectStore

CONFIG = load_places_config(Path("config/places.yaml"))
NO_SLEEP = lambda _: None  # noqa: E731
RHINELAND = chosen_areas(CONFIG.towns, ["Köln"])[0]


def test_which_car_parks():
    assert is_car_park({"amenity": "parking"})
    assert is_car_park({"amenity": "parking", "parking": "multi-storey", "access": "customers"})
    assert not is_car_park({"amenity": "parking", "access": "private"})
    assert not is_car_park({"amenity": "parking", "parking": "street_side"})
    assert not is_car_park({"amenity": "parking", "capacity": "3"})
    assert is_car_park({"amenity": "parking", "capacity": "lots"})
    assert not is_car_park({"amenity": "bicycle_parking"})


def test_big_boxes_are_cut_into_small_squares():
    assert squares((50.0, 6.0, 50.2, 6.25)) == [(50.0, 6.0, 50.2, 6.25)]
    cut = squares((50.0, 6.0, 51.0, 7.0))
    assert (
        len(cut) == 16
        and cut[0] == (50.0, 6.0, 50.25, 6.25)
        and cut[-1] == (50.75, 6.75, 51.0, 7.0)
    )
    for s, w, n, e in squares((50.6379, 5.925, 51.5541, 7.374)):
        assert n - s <= 0.3 and e - w <= 0.3


def test_queries_leave_out_lanes_and_private_lots():
    queries = parking_queries(CONFIG.towns, CONFIG.radius_m)
    assert 20 < len(queries) < 60
    square, query = queries[0]
    assert query.startswith(f"[out:json][timeout:90][bbox:{square[0]:g},")
    assert '["amenity"="parking"]' in query and '["access"!~"^(' in query and "street_side" in query
    assert query.endswith("out center tags;")


def test_a_car_park_row():
    element = {
        "type": "way",
        "id": 42,
        "center": {"lat": 50.9383, "lon": 6.9599},
        "tags": {
            "amenity": "parking",
            "parking": "underground",
            "name": "Parkhaus Dom",
            "capacity": "600",
            "capacity:parent": "12",
            "capacity:disabled": "yes",
            "fee": "yes",
            "maxheight": "2,00 m",
            "opening_hours": "24/7",
            "operator": "Contipark",
        },
    }
    assert from_osm(element, CONFIG.towns, CONFIG.radius_m) == {
        "osm": "way/42",
        "town": "Köln",
        "lat": 50.9383,
        "lon": 6.9599,
        "name": "Parkhaus Dom",
        "type": "underground",
        "capacity": 600,
        "parent": 12,
        "disabled": True,
        "free": False,
        "maxheight": 2.0,
        "hours": "24/7",
        "operator": "Contipark",
    }
    far = {"type": "node", "id": 1, "lat": 52.0, "lon": 9.0, "tags": {"amenity": "parking"}}
    assert from_osm(far, CONFIG.towns, CONFIG.radius_m) is None


class Answers:
    """Overpass stand-in: one answer per call, in order; an exception fails that query."""

    def __init__(self, answers):
        self.answers = list(answers)

    def post(self, url, data, timeout):
        answer = self.answers.pop(0) if self.answers else []
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(200, json={"elements": answer}, request=httpx.Request("POST", url))


def lot(n, lat, lon):
    return {"type": "node", "id": n, "lat": lat, "lon": lon, "tags": {"amenity": "parking"}}


def test_a_failed_square_keeps_its_last_rows():
    config = CONFIG.model_copy(update={"towns": RHINELAND})
    queries = parking_queries(RHINELAND, CONFIG.radius_m)
    first, second = queries[0][0], queries[1][0]
    old = {
        "osm": "node/9",
        "town": "Köln",
        "lat": (first[0] + first[2]) / 2,
        "lon": (first[1] + first[3]) / 2,
    }
    gone = {
        "osm": "node/8",
        "town": "Köln",
        "lat": (second[0] + second[2]) / 2,
        "lon": (second[1] + second[3]) / 2,
    }
    boom = httpx.ConnectError("down")
    answers = [boom, boom] + [[lot(1, 50.94, 6.96)]] * (len(queries) - 1)
    rows, failed, elements = fetch_parking(
        config, [old, gone], Answers(answers), NO_SLEEP, ["https://o"]
    )
    assert failed == 1 and elements == len(queries) - 1
    assert {r["osm"] for r in rows} == {"node/1", "node/9"}


def test_nothing_at_all_keeps_the_previous_file():
    config = CONFIG.model_copy(update={"towns": RHINELAND})
    with pytest.raises(OverpassError):
        fetch_parking(config, [], Answers([]), NO_SLEEP, ["https://o"])


def test_an_area_replaces_only_its_own_rows(tmp_path):
    berlin = {"osm": "node/2", "town": "Berlin", "lat": 52.5, "lon": 13.4}
    old = {"osm": "node/3", "town": "Köln", "lat": 50.9, "lon": 6.9}
    new = {"osm": "node/4", "town": "Köln", "lat": 50.95, "lon": 6.95, "parent": 4, "free": True}
    area = chosen_areas(CONFIG.towns, ["Köln"])[0]
    rows = replace_area([berlin, old], area, [new])
    assert [r["osm"] for r in rows] == ["node/2", "node/4"]
    store = LocalObjectStore(tmp_path)
    write_parking(rows, store, CONFIG.parking_key)
    assert read_places(store, CONFIG.parking_key) == rows
    manifest = (tmp_path / "places/parking.json").read_text()
    assert '"with_parent_spaces": 1' in manifest and "OpenStreetMap" in manifest
