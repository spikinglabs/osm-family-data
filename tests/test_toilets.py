import json
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from osm_family_data.cli import app
from osm_family_data.places import OverpassError, chosen_areas, load_places_config, read_places
from osm_family_data.storage import LocalObjectStore
from osm_family_data.toilets import (
    fetch_toilets,
    from_osm,
    kind_of,
    replace_area,
    toilet_queries,
    write_toilets,
)

CONFIG = load_places_config(Path("config/places.yaml"))
NO_SLEEP = lambda _: None  # noqa: E731


def test_kinds():
    assert kind_of({"amenity": "toilets"}) == "toilets"
    assert kind_of({"amenity": "toilets", "changing_table": "yes"}) == "toilets"
    assert kind_of({"amenity": "toilets", "access": "private"}) is None
    assert kind_of({"amenity": "drinking_water"}) == "water"
    assert kind_of({"man_made": "water_tap", "drinking_water": "yes"}) == "water"
    assert kind_of({"amenity": "fountain"}) is None
    assert kind_of({"shop": "chemist", "changing_table": "yes"}) == "changing_table"
    assert kind_of({"shop": "chemist"}) is None


def test_one_small_query_per_tag_and_area():
    queries = toilet_queries(CONFIG.towns, CONFIG.radius_m)
    assert len(queries) == 30  # six tags, five areas
    assert queries[0] == (
        "toilets",
        '[out:json][timeout:90][bbox:50.6379,5.925,51.5541,7.374];nwr["amenity"="toilets"];'
        "out center tags;",
    )


def test_a_toilet_row():
    element = {
        "type": "node",
        "id": 5,
        "lat": 50.9412345678,
        "lon": 6.958,
        "tags": {
            "amenity": "toilets",
            "fee": "no",
            "wheelchair": "yes",
            "changing_table": "yes",
            "changing_table:location": "wheelchair_toilet",
            "opening_hours": "24/7",
            "operator": "Stadt Köln",
        },
    }
    assert from_osm(element, CONFIG.towns, CONFIG.radius_m) == {
        "osm": "node/5",
        "kind": "toilets",
        "town": "Köln",
        "lat": 50.941235,
        "lon": 6.958,
        "changing_table": True,
        "changing_table_location": "wheelchair_toilet",
        "wheelchair": "yes",
        "free": True,
        "hours": "24/7",
        "operator": "Stadt Köln",
    }
    assert from_osm({**element, "lat": 51.9}, CONFIG.towns, CONFIG.radius_m) is None


def test_a_shop_with_a_changing_table():
    element = {
        "type": "way",
        "id": 6,
        "center": {"lat": 53.55, "lon": 10.0},
        "tags": {"shop": "chemist", "name": "dm", "changing_table": "yes"},
    }
    assert from_osm(element, CONFIG.towns, CONFIG.radius_m) == {
        "osm": "way/6",
        "kind": "changing_table",
        "town": "Hamburg",
        "lat": 53.55,
        "lon": 10.0,
        "name": "dm",
        "at": "chemist",
        "changing_table": True,
    }


def overpass_client(failing: set[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        query = request.content.decode()
        if any(f"%22{value}%22" in query for value in failing):
            return httpx.Response(504)
        if "toilets" in query:
            element = {
                "type": "node",
                "id": 1,
                "lat": 50.94,
                "lon": 6.96,
                "tags": {"amenity": "toilets"},
            }
            return httpx.Response(200, json={"elements": [element]})
        return httpx.Response(200, json={"elements": []})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_a_failed_kind_keeps_the_last_rows():
    area = chosen_areas(CONFIG.towns, ["Köln"])[0]
    config = CONFIG.model_copy(update={"towns": area})
    previous = [{"osm": "node/9", "kind": "water", "town": "Köln", "lat": 50.9, "lon": 6.9}]
    with overpass_client({"drinking_water"}) as client:
        rows, failed, elements = fetch_toilets(
            config, previous, client, sleep=NO_SLEEP, servers=["https://overpass.test"]
        )
    assert failed == {"water"}
    assert elements == 1
    assert [row["osm"] for row in rows] == ["node/1", "node/9"]


def test_nothing_at_all_keeps_the_previous_file():
    with overpass_client({"toilets"}) as client, pytest.raises(OverpassError, match="no toilets"):
        fetch_toilets(CONFIG, [], client, sleep=NO_SLEEP, servers=["https://overpass.test"])


def test_an_area_replaces_only_its_own_rows():
    hamburg = chosen_areas(CONFIG.towns, ["Hamburg"])[0]
    rows = [
        {"osm": "node/1", "kind": "toilets", "town": "Köln", "lat": 1, "lon": 1},
        {"osm": "node/2", "kind": "toilets", "town": "Hamburg", "lat": 1, "lon": 1},
    ]
    fresh = [{"osm": "node/3", "kind": "water", "town": "Hamburg", "lat": 1, "lon": 1}]
    assert [row["osm"] for row in replace_area(rows, hamburg, fresh)] == ["node/3", "node/1"]


def test_round_trip_with_a_manifest(tmp_path):
    store = LocalObjectStore(tmp_path)
    rows = [
        {"osm": "node/1", "kind": "toilets", "town": "Köln", "changing_table": True},
        {"osm": "node/2", "kind": "water", "town": "Köln"},
    ]
    write_toilets(rows, store, "places/toilets.jsonl")
    assert read_places(store, "places/toilets.jsonl") == rows
    manifest = json.loads((tmp_path / "places/toilets.json").read_text())
    assert manifest["count"] == 2
    assert manifest["towns"] == {
        "Köln": {"toilets": 1, "toilets_with_changing_table": 1, "water": 1}
    }


def test_an_unknown_town_is_refused_before_any_fetch():
    result = CliRunner().invoke(app, ["toilets", "--town", "Atlantis"])
    assert result.exit_code != 0
    assert "Atlantis" in result.output
