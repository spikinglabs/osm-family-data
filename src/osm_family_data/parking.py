"""Car parks from OpenStreetMap, around the same towns as the toilets.

"Where do we leave the car?" comes right after "is there a toilet?" for a family with a pram.
This keeps every car park a parent can drive into (surface lots, multi-storey and underground
garages, park and ride), one row each, with what OSM knows about price, size, parent-and-child
spaces and height limits. Street-side parking lanes, garages for residents and private lots
are left out: there are hundreds of thousands of them and they would bury the useful ones.

No Nominatim lookup: a pin and a name are all the map needs.
Data © OpenStreetMap contributors, ODbL.
"""

from __future__ import annotations

import json
import math
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

# access= values that mean a family cannot just drive in.
CLOSED = {"private", "no", "permit", "delivery", "residents", "employees"}
# parking= values that are not a car park: street lanes, laybys, rows of lock-up garages.
NOT_A_CAR_PARK = {
    "street_side",
    "lane",
    "on_kerb",
    "half_on_kerb",
    "layby",
    "carports",
    "garage_boxes",
    "sheds",
}
TYPES = {"surface", "multi-storey", "underground", "rooftop"}
# A big area's box is cut into squares of at most this many degrees, so each query stays small
# (car parks are far more numerous than toilets; the whole Rhineland box in one query times out).
SQUARE = 0.3
# Known room for fewer cars than this is somebody's driveway, not a car park.
MIN_CAPACITY = 5

FILTER = (
    '["amenity"="parking"]'
    f'["access"!~"^({"|".join(sorted(CLOSED))})$"]'
    f'["parking"!~"^({"|".join(sorted(NOT_A_CAR_PARK))})$"]'
)


def squares(
    box: tuple[float, float, float, float], size: float = SQUARE
) -> list[tuple[float, ...]]:
    """(south, west, north, east) cut into an even grid of squares no bigger than size."""
    s, w, n, e = box
    rows, cols = max(1, math.ceil((n - s) / size - 1e-9)), max(1, math.ceil((e - w) / size - 1e-9))
    dy, dx = (n - s) / rows, (e - w) / cols
    return [
        tuple(round(v, 4) for v in (s + r * dy, w + c * dx, s + (r + 1) * dy, w + (c + 1) * dx))
        for r in range(rows)
        for c in range(cols)
    ]


def parking_queries(towns: list[Town], radius_m: int) -> list[tuple[tuple[float, ...], str]]:
    """One query per square of each area's box, with its square."""
    return [
        (
            square,
            f"[out:json][timeout:90][bbox:{','.join(f'{v:g}' for v in square)}];"
            f"nwr{FILTER};out center tags;",
        )
        for area in areas(towns)
        for square in squares(bbox(area, radius_m))
    ]


def _count(value: str | None) -> int | None:
    """capacity=120 → 120; "yes", "2-3" or rubbish → None."""
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return n if 0 <= n < 100000 else None


def _metres(value: str | None) -> float | None:
    """maxheight=2.1 / "2.1 m" / "2,10" → 2.1; "default", "none" or feet → None."""
    if not value:
        return None
    text = value.strip().lower().removesuffix("m").strip().replace(",", ".")
    try:
        h = float(text)
    except ValueError:
        return None
    return round(h, 2) if 1.5 <= h <= 6 else None


def is_car_park(tags: dict[str, str]) -> bool:
    if tags.get("amenity") != "parking":
        return False
    if tags.get("access") in CLOSED or tags.get("parking") in NOT_A_CAR_PARK:
        return False
    capacity = _count(tags.get("capacity"))
    return capacity is None or capacity >= MIN_CAPACITY


def from_osm(element: dict[str, Any], towns: list[Town], radius_m: int) -> dict[str, Any] | None:
    """One Overpass element → one compact row, or None. Its town is the nearest one."""
    tags = element.get("tags") or {}
    center = element.get("center") or {}
    lat, lon = element.get("lat", center.get("lat")), element.get("lon", center.get("lon"))
    if (
        not is_car_park(tags)
        or not isinstance(lat, int | float)
        or not isinstance(lon, int | float)
    ):
        return None
    distance, town, radius = min(
        (km((t.lat, t.lon), (lat, lon)), t.name, t.radius_m or radius_m) for t in towns
    )
    if distance > radius / 1000 + 1:
        return None
    fee = tags.get("fee")
    disabled = tags.get("capacity:disabled")
    row = {
        "osm": f"{element['type']}/{element['id']}",
        "town": town,
        "lat": round(lat, 6),
        "lon": round(lon, 6),
        "name": tags.get("name"),
        "type": tags.get("parking") if tags.get("parking") in TYPES else None,
        "capacity": _count(tags.get("capacity")),
        # Wider parent-and-child spaces (Eltern-Kind-Parkplätze): a count, or True for "yes".
        "parent": _count(tags.get("capacity:parent"))
        or (True if tags.get("capacity:parent") == "yes" else None),
        "disabled": _count(disabled) or (True if disabled == "yes" else None),
        "free": True if fee == "no" else False if fee == "yes" else None,
        "park_ride": True if tags.get("park_ride") not in (None, "no") else None,
        # A supermarket's or a zoo's car park: fine while you are their customer.
        "customers_only": True if tags.get("access") == "customers" else None,
        "maxheight": _metres(tags.get("maxheight")),
        "maxstay": tags.get("maxstay"),
        "hours": tags.get("opening_hours"),
        "operator": tags.get("operator"),
        "website": _url(tags.get("website") or tags.get("contact:website")),
    }
    return {key: value for key, value in row.items() if value not in (None, "")}


def _order(row: dict[str, Any]) -> tuple[str, float, float]:
    return (row["town"].casefold(), row["lat"], row["lon"])


def fetch_parking(
    config: PlacesConfig,
    previous: list[dict[str, Any]],
    client: httpx.Client,
    sleep: Callable[[float], None] = time.sleep,
    servers: Iterable[str] = OVERPASS_SERVERS,
) -> tuple[list[dict[str, Any]], int, int]:
    """This run's rows, the number of squares that failed and the elements read.

    A failed square keeps the previous run's rows inside it; a run where every square failed
    raises, so the area keeps its old rows.
    """
    elements: list[dict[str, Any]] = []
    failed: list[tuple[float, ...]] = []
    for square, query in parking_queries(config.towns, config.radius_m):
        try:
            elements.extend(overpass(client, query, servers, sleep))
        except OverpassError as error:
            failed.append(square)
            print(error)
        sleep(2)
    if not elements:
        raise OverpassError("Overpass returned no car parks; keeping the previous list")

    def inside(row: dict[str, Any], square: tuple[float, ...]) -> bool:
        s, w, n, e = square
        return s <= row["lat"] <= n and w <= row["lon"] <= e

    rows = {row["osm"]: row for row in previous if any(inside(row, sq) for sq in failed)}
    for element in elements:
        row = from_osm(element, config.towns, config.radius_m)
        if row:
            rows[row["osm"]] = row
    return sorted(rows.values(), key=_order), len(failed), len(elements)


def replace_area(
    rows: list[dict[str, Any]], area: list[Town], fresh: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Every other area's rows as they were, this area's from this run."""
    names = {t.name for t in area}
    return sorted([row for row in rows if row["town"] not in names] + fresh, key=_order)


def write_parking(rows: list[dict[str, Any]], store: ObjectStore, key: str) -> str:
    """The rows, one per line, plus a small manifest with counts per town."""
    body = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
    store.put(key, body.encode(), "application/x-ndjson")
    towns: dict[str, Counter[str]] = {}
    for row in rows:
        counts = towns.setdefault(row["town"], Counter())
        counts["car_parks"] += 1
        if row.get("parent"):
            counts["with_parent_spaces"] += 1
        if row.get("free"):
            counts["free"] += 1
    manifest = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "count": len(rows),
        "types": dict(sorted(Counter(row.get("type", "unknown") for row in rows).items())),
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
