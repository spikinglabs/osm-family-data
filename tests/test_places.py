import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from osm_family_data.cli import app
from osm_family_data.places import (
    OverpassError,
    add_reverse,
    chosen_areas,
    fetch_places,
    from_nominatim,
    from_osm,
    kind_of,
    load_places_config,
    named_areas,
    overpass_queries,
    read_places,
    read_refreshed,
    replace_area,
    reverse_key,
    write_places,
)
from osm_family_data.storage import LocalObjectStore

CONFIG = load_places_config(Path("config/places.yaml"))
NO_SLEEP = lambda _: None  # noqa: E731


def test_kinds_follow_the_most_specific_tag():
    assert kind_of({"leisure": "playground", "indoor": "yes"}) == "indoor"
    assert kind_of({"leisure": "playground"}) == "playground"
    assert kind_of({"zoo": "petting_zoo"}) == "zoo"
    assert kind_of({"tourism": "museum", "name": "Kunstmuseum"}) is None
    assert kind_of({"tourism": "museum", "name": "Odysseum"}) == "museum"
    assert kind_of({"amenity": "cafe", "name": "Café Central"}) is None
    assert kind_of({"amenity": "cafe", "name": "Kindercafé Casino"}) is None
    assert kind_of({"amenity": "cafe", "name": "Café", "kids_area": "yes"}) == "cafe"
    assert kind_of({"leisure": "park", "name": "Rosenplatz", "wikidata": "Q1"}) is None
    assert kind_of({"leisure": "ice_rink", "name": "Eisstadion"}) == "winter"
    assert kind_of({"piste:type": "sled", "name": "Rodelbahn"}) == "winter"


def test_one_small_query_per_tag_and_area():
    queries = overpass_queries(CONFIG.towns, CONFIG.radius_m)
    assert len(queries) == 130
    # The NRW box is padded by its widest town radius (Düsseldorf, 10 km) plus 1 km.
    assert queries[0][1].startswith("[out:json][timeout:90][bbox:50.6379,5.925,51.5541,7.374];")
    # Berlin is its own box with its own radius, not one box from Aachen to Berlin.
    assert queries[26][1].startswith(
        "[out:json][timeout:90][bbox:52.3759,13.1681,52.6641,13.6419];"
    )
    # München too: 500 km from Berlin and NRW, so a third box.
    assert queries[52][1].startswith(
        "[out:json][timeout:90][bbox:48.0199,11.3995,48.2541,11.7505];"
    )
    # Frankfurt (125 km from Bonn) and Hamburg get boxes of their own as well.
    assert queries[78][1].startswith("[out:json][timeout:90][bbox:50.0119,8.5275,50.2101,8.8365];")
    assert queries[104][1].startswith(
        "[out:json][timeout:90][bbox:53.4159,9.7655,53.6861,10.2205];"
    )
    assert all(query.endswith("out center tags;") for _, query in queries)


def test_a_place_gets_its_nearest_town_and_playground_equipment():
    element = {
        "type": "way",
        "id": 7,
        "center": {"lat": 50.9401234567, "lon": 6.9601},
        "tags": {
            "leisure": "playground",
            "name": "Spielplatz am Dom",
            "playground:swing": "yes",
            "max_age": "3",
            "website": "https://example.org",
        },
    }
    row = from_osm(element, CONFIG.towns, CONFIG.radius_m)
    assert row == {
        "osm": "way/7",
        "kind": "playground",
        "name": "Spielplatz am Dom",
        "town": "Köln",
        "lat": 50.940123,
        "lon": 6.9601,
        "website": "https://example.org/",
        "free": True,
        "facilities": ["swings", "toddler_area"],
    }
    far = {**element, "center": {"lat": 51.6, "lon": 9.0}}
    assert from_osm(far, CONFIG.towns, CONFIG.radius_m) is None
    # Berlin's own 15 km radius keeps a playground 10 km out, which 6 km would drop.
    spandau = {**element, "center": {"lat": 52.535, "lon": 13.26}}
    assert from_osm(spandau, CONFIG.towns, CONFIG.radius_m)["town"] == "Berlin"


def overpass_client(failing: set[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        query = request.content.decode()
        if any(f"%22{value}%22" in query for value in failing):
            return httpx.Response(504)
        if "playground" in query:
            element = {
                "type": "node",
                "id": 1,
                "lat": 50.94,
                "lon": 6.96,
                "tags": {"leisure": "playground", "name": "Neu"},
            }
            return httpx.Response(200, json={"elements": [element]})
        return httpx.Response(200, json={"elements": []})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_a_failed_kind_keeps_last_weeks_rows():
    previous = [
        {"osm": "node/9", "kind": "zoo", "name": "Zoo", "town": "Köln", "lat": 50.9, "lon": 6.9},
        {"osm": "node/8", "kind": "playground", "name": "Weg", "town": "Köln", "lat": 1, "lon": 1},
    ]
    with overpass_client({"zoo"}) as client:
        places, failed, elements = fetch_places(
            CONFIG, previous, client, sleep=NO_SLEEP, servers=["https://overpass.test"]
        )
    assert failed == {"zoo"}
    assert elements == 5  # the playground query runs once per area (NRW, Berlin, München, …)
    assert [row["osm"] for row in places] == ["node/1", "node/9"]


def test_queries_are_spread_over_the_servers():
    hosts = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        return httpx.Response(200, json={"elements": []})

    servers = ["https://a.test", "https://b.test", "https://c.test"]
    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(OverpassError, match="no places"),
    ):
        fetch_places(CONFIG, [], client, sleep=NO_SLEEP, servers=servers)
    assert len(hosts) == 130
    assert Counter(hosts) == {"a.test": 44, "b.test": 43, "c.test": 43}


def test_no_places_at_all_keeps_the_previous_file():
    with (
        overpass_client({"playground"}) as client,
        pytest.raises(OverpassError, match="no places"),
    ):
        fetch_places(CONFIG, [], client, sleep=NO_SLEEP, servers=["https://overpass.test"])


def test_streets_are_looked_up_once_and_again_when_a_place_moves():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params["lat"])
        address = {"road": "Domkloster", "suburb": "Altstadt-Nord", "postcode": "50667"}
        return httpx.Response(200, json={"address": address})

    kept = {"osm": "node/1", "lat": 50.94, "lon": 6.96}
    moved = {"osm": "node/2", "lat": 50.95, "lon": 6.96}
    previous = [
        {**kept, "nearby": {"road": "Alt"}},
        {"osm": "node/2", "lat": 50.90, "lon": 6.96, "nearby": {"road": "Alt"}},
    ]
    places = [dict(kept), dict(moved), {"osm": "node/3", "lat": 50.96, "lon": 6.96}]
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        looked_up, failed = add_reverse(places, previous, client, max_lookups=1, sleep=NO_SLEEP)
    assert (looked_up, failed) == (1, 0)
    assert calls == ["50.95"]
    assert places[0]["nearby"] == {"road": "Alt"}
    assert places[1]["nearby"] == {
        "road": "Domkloster",
        "district": "Altstadt-Nord",
        "postcode": "50667",
    }
    assert "nearby" not in places[2]  # over the limit: next run
    assert reverse_key(kept) == "node/1@50.9400,6.9600"


def test_nominatim_answer_without_empty_fields():
    assert from_nominatim({"address": {"footway": "Pfad", "village": "Dorf"}}) == {
        "road": "Pfad",
        "district": "Dorf",
    }
    assert from_nominatim({}) == {}


def test_places_round_trip_with_a_manifest(tmp_path):
    store = LocalObjectStore(tmp_path)
    assert read_places(store, "places/places.jsonl") == []
    rows = [{"osm": "node/1", "kind": "zoo", "name": "Zoo", "town": "Köln"}]
    write_places(rows, store, "places/places.jsonl")
    assert read_places(store, "places/places.jsonl") == rows
    manifest = json.loads((tmp_path / "places/places.json").read_text())
    assert manifest["place_count"] == 1
    assert manifest["kinds"] == {"zoo": 1}


def test_a_run_can_refresh_only_some_areas():
    every = chosen_areas(CONFIG.towns)
    assert [area[0].name for area in every] == [
        "Velbert",
        "Berlin",
        "München",
        "Frankfurt am Main",
        "Hamburg",
    ]
    some = chosen_areas(CONFIG.towns, ["hamburg", "Köln"])
    assert [area[0].name for area in some] == ["Velbert", "Hamburg"]
    with pytest.raises(ValueError, match="Atlantis"):
        chosen_areas(CONFIG.towns, ["Atlantis"])


def test_an_area_replaces_only_its_own_rows():
    hamburg = chosen_areas(CONFIG.towns, ["Hamburg"])[0]
    places = [
        {"osm": "node/1", "kind": "zoo", "name": "Zoo", "town": "Köln"},
        {"osm": "node/2", "kind": "zoo", "name": "Alt", "town": "Hamburg"},
    ]
    fresh = [{"osm": "node/3", "kind": "zoo", "name": "Hagenbeck", "town": "Hamburg"}]
    assert [row["osm"] for row in replace_area(places, hamburg, fresh)] == ["node/3", "node/1"]


def test_an_unknown_town_is_refused_before_any_fetch(tmp_path):
    result = CliRunner().invoke(app, ["places", "--town", "Atlantis"])
    assert result.exit_code != 0
    assert "Atlantis" in result.output


def test_places_only_towns_are_areas_of_their_own():
    groups = named_areas(CONFIG.more_towns)
    ruhr = next(g for g in groups if g[0].area == "Ruhr")
    assert {"Dortmund", "Bochum", "Duisburg"} <= {t.name for t in ruhr}
    assert [t.name for t in next(g for g in groups if g[0].name == "Dresden")] == ["Dresden"]
    # Not part of a plain run of the main towns, but --town finds them.
    assert all(t.name != "Dortmund" for area in chosen_areas(CONFIG.towns) for t in area)
    found = chosen_areas(CONFIG.towns, ["dortmund"], CONFIG.more_towns)
    assert [area[0].area for area in found] == ["Ruhr"]
    names = [t.name for t in CONFIG.every_town]
    assert len(names) == len(set(names))


def test_a_place_goes_to_the_nearest_town_whose_radius_reaches_it():
    def playground(lat, lon):
        tags = {"leisure": "playground", "name": "Spielplatz"}
        return {"type": "node", "id": 1, "lat": lat, "lon": lon, "tags": tags}

    everyone = CONFIG.every_town
    # Nearer Neuss's centre than Düsseldorf's: Neuss.
    assert from_osm(playground(51.2, 6.70), everyone, CONFIG.radius_m)["town"] == "Neuss"
    # Nearer Neuss too, but past Neuss's 6 km and inside Düsseldorf's 10 km: Düsseldorf.
    far_west = playground(51.29, 6.72)
    assert from_osm(far_west, everyone, CONFIG.radius_m)["town"] == "Düsseldorf"


def test_a_place_is_in_the_file_once_when_boxes_overlap():
    niederrhein = chosen_areas(CONFIG.towns, ["Neuss"], CONFIG.more_towns)[0]
    places = [{"osm": "node/1", "kind": "park", "name": "Park", "town": "Düsseldorf"}]
    fresh = [{"osm": "node/1", "kind": "park", "name": "Park", "town": "Neuss"}]
    assert replace_area(places, niederrhein, fresh) == fresh


def test_part_splits_the_areas_between_jobs():
    result = CliRunner().invoke(app, ["places", "--more", "--part", "7/6"])
    assert result.exit_code != 0
    assert "K/N" in result.output


def test_a_restarted_run_skips_the_areas_it_already_fetched(tmp_path):
    store = LocalObjectStore(tmp_path)
    write_places([], store, "places/places.jsonl", {"Duisburg": datetime.now(UTC).isoformat()})
    assert read_refreshed(store, "places/places.jsonl").keys() == {"Duisburg"}
    area = named_areas(load_places_config(Path("config/places.yaml")).more_towns)[0]
    assert area[0].name == "Duisburg"
    args = ["places", "--more", "--part", "1/41", "--local-root", str(tmp_path)]
    result = CliRunner().invoke(app, [*args, "--skip-days", "20"])
    assert result.exit_code == 0, result.output
    assert "skipped, fetched" in result.output
    assert "updated nothing" in result.output


def test_the_areas_command_lists_one_job_per_area():
    result = CliRunner().invoke(app, ["areas"])
    rows = json.loads(result.output)
    assert rows[0] == {"n": 1, "of": len(rows), "name": "Ruhr"}
    assert len(rows) == 41
