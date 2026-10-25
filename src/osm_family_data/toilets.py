"""Public toilets, changing tables and drinking water from OpenStreetMap, around the same towns
as the places.

Parents with small children ask "is there a toilet?" before almost anything else, and the
places themselves rarely say (OSM tags toilets=yes on few playgrounds or parks). So this keeps
every public toilet, every drinking-water tap, and every other spot that tags a changing table
(drugstores, cafés, town halls), one row each, and the site finds the ones near a place.

No Nominatim lookup: there are thousands of them and a pin is all a parent needs.
Data © OpenStreetMap contributors, ODbL.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any

import httpx

from osm_family_data.places import (
    OVERPASS_SERVERS,
    OverpassError,
    PlacesConfig,
    Town,
    _url,
    areas,
    bbox,
    km,
    overpass,
)
from osm_family_data.storage import ObjectStore

# access= values that mean a parent cannot just walk in.
CLOSED = {"private", "no", "permit", "delivery"}
# Where in a building the changing table is, as OSM's changing_table:location says it.
TABLE_LOCATIONS = {
    "wheelchair_toilet",
    "female_toilet",
    "male_toilet",
    "unisex_toilet",
    "dedicated_room",
    "room",
    "sales_area",
}


def kind_of(tags: dict[str, str]) -> str | None:
    """toilets, water or changing_table; a toilet with a changing table stays a toilet."""
    if tags.get("access") in CLOSED:
        return None
    if tags.get("amenity") == "toilets":
        return "toilets"
    if tags.get("amenity") == "drinking_water" or (
        tags.get("drinking_water") == "yes"
        and (tags.get("man_made") == "water_tap" or tags.get("amenity") == "fountain")
    ):
        return "water"
    if tags.get("changing_table") == "yes" or tags.get("diaper") == "yes":
        return "changing_table"
    return None


def toilet_queries(towns: list[Town], radius_m: int) -> list[tuple[str, str]]:
    """One small query per tag and area, like the places: exact tag values only."""
    filters = [
        ("toilets", '["amenity"="toilets"]'),
        ("water", '["amenity"="drinking_water"]'),
        ("water", '["man_made"="water_tap"]["drinking_water"="yes"]'),
        ("water", '["amenity"="fountain"]["drinking_water"="yes"]'),
        ("changing_table", '["changing_table"="yes"]'),
        ("changing_table", '["diaper"="yes"]'),
    ]
    boxes = [",".join(f"{value:g}" for value in bbox(area, radius_m)) for area in areas(towns)]
    return [
        (kind, f"[out:json][timeout:90][bbox:{box}];nwr{tag_filter};out center tags;")
        for box in boxes
        for kind, tag_filter in filters
    ]


def _flag(value: str | None) -> bool | None:
    if value in {"yes", "designated"}:
        return True
    if value == "no":
        return False
    return None


def from_osm(element: dict[str, Any], towns: list[Town], radius_m: int) -> dict[str, Any] | None:
    """One Overpass element → one compact row, or None. Its town is the nearest one."""
    tags = element.get("tags") or {}
    kind = kind_of(tags)
    center = element.get("center") or {}
    lat, lon = element.get("lat", center.get("lat")), element.get("lon", center.get("lon"))
    if not kind or not isinstance(lat, int | float) or not isinstance(lon, int | float):
        return None
    distance, town, radius = min(
        (km((t.lat, t.lon), (lat, lon)), t.name, t.radius_m or radius_m) for t in towns
    )
    if distance > radius / 1000 + 1:
        return None
    table = (
        True
        if kind == "changing_table"
        else _flag(tags.get("changing_table") or tags.get("diaper"))
    )
    location = tags.get("changing_table:location")
    fee = tags.get("fee")
    row = {
        "osm": f"{element['type']}/{element['id']}",
        "kind": kind,
        "town": town,
        "lat": round(lat, 6),
        "lon": round(lon, 6),
        "name": tags.get("name"),
        # What a changing-table spot is (a chemist, a café …), from its main OSM tag.
        "at": next(
            (tags[key] for key in ("shop", "amenity", "tourism", "leisure") if tags.get(key)),
            None,
        )
        if kind == "changing_table"
        else None,
        "changing_table": table,
        "changing_table_location": location if table and location in TABLE_LOCATIONS else None,
        "wheelchair": tags.get("wheelchair")
        if tags.get("wheelchair") in {"yes", "limited", "no"}
        else None,
        "free": True if fee == "no" else False if fee == "yes" else None,
        # A café's or a shop's toilet: fine to ask, but buy something.
        "customers_only": True if tags.get("access") == "customers" else None,
        "hours": tags.get("opening_hours"),
        "operator": tags.get("operator"),
        "website": _url(tags.get("website") or tags.get("contact:website")),
    }
    return {key: value for key, value in row.items() if value not in (None, "")}


def _order(row: dict[str, Any]) -> tuple[str, str, float, float]:
    return (row["town"].casefold(), row["kind"], row["lat"], row["lon"])


def fetch_toilets(
    config: PlacesConfig,
    previous: list[dict[str, Any]],
    client: httpx.Client,
    sleep: Callable[[float], None] = time.sleep,
    servers: Iterable[str] = OVERPASS_SERVERS,
) -> tuple[list[dict[str, Any]], set[str], int]:
    """This run's rows; a kind whose query failed keeps the previous run's rows."""
    elements: list[dict[str, Any]] = []
    failed: set[str] = set()
    for kind, query in toilet_queries(config.towns, config.radius_m):
        try:
            elements.extend(overpass(client, query, servers, sleep))
        except OverpassError as error:
            failed.add(kind)
            print(error)
        sleep(2)
    if not elements:
        raise OverpassError("Overpass returned no toilets; keeping the previous list")
    rows = {row["osm"]: row for row in previous if row["kind"] in failed}
    for element in elements:
        row = from_osm(element, config.towns, config.radius_m)
        if row:
            rows[row["osm"]] = row
    return sorted(rows.values(), key=_order), failed, len(elements)


def replace_area(
    rows: list[dict[str, Any]], area: list[Town], fresh: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Every other area's rows as they were, this area's from this run."""
    names = {t.name for t in area}
    return sorted([row for row in rows if row["town"] not in names] + fresh, key=_order)


def write_toilets(rows: list[dict[str, Any]], store: ObjectStore, key: str) -> str:
    """The rows, one per line, plus a small manifest with counts per town."""
    body = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
    store.put(key, body.encode(), "application/x-ndjson")
    towns: dict[str, Counter[str]] = {}
    for row in rows:
        counts = towns.setdefault(row["town"], Counter())
        counts[row["kind"]] += 1
        if row["kind"] == "toilets" and row.get("changing_table"):
            counts["toilets_with_changing_table"] += 1
    manifest = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "count": len(rows),
        "kinds": dict(sorted(Counter(row["kind"] for row in rows).items())),
        "towns": {town: dict(sorted(c.items())) for town, c in sorted(towns.items())},
        "object_key": key,
        "attribution": "© OpenStreetMap contributors, ODbL",
    }
    store.put(
        key.rsplit(".", 1)[0] + ".json",
        json.dumps(manifest, ensure_ascii=False, indent=2).encode(),
        "application/json",
    )
    return key
