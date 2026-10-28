"""Family places from OpenStreetMap, for Little Outings and the future spikinglabs events app.

Overpass gives the places around each configured town; Nominatim's reverse lookup adds the
nearest road and the district, because most playgrounds, parks and pools carry no address
tags. One JSONL file goes to its own public bucket (R2_PLACES_BUCKET), so site builds only
download it and never wait on either service. Moved here from little-outings
(scripts/fetch-osm-places.mjs and scripts/osm-streets.mjs).

Each run reads the previous file back: a place's reverse lookup is kept until it moves, and a
kind whose query failed this week keeps last week's rows. Data © OpenStreetMap contributors,
ODbL.
"""

from __future__ import annotations

import json
import math
import re
import time
from collections import Counter
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
import yaml
from botocore.exceptions import ClientError
from pydantic import Field

from osm_family_data.storage import ObjectStore, StrictModel

USER_AGENT = "little-outings (spikinglabs) places import; https://outings.allo.land"
OVERPASS_SERVERS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
)
NOMINATIM_REVERSE = "https://nominatim.openstreetmap.org/reverse"

# Museums only when they are for children or hands-on; an art museum is rarely a family outing.
KID_MUSEUM = re.compile(
    r"kinder|kids|jugend|natur|technik|science|spiel|mitmach|zoo|tier|eisenbahn|bahn|bergbau"
    r"|feuerwehr|odysseum|schokolade|dino|planetar|sternwarte|bauernhof|freilicht|römer|roemer"
    r"|neanderthal|aquarium",
    re.IGNORECASE,
)
KID_MUSEUM_TYPE = {
    "children",
    "nature",
    "science",
    "technology",
    "railway",
    "transport",
    "open_air",
    "mining",
    "archaeological",
}
# Animal places besides a zoo: petting zoos, wildlife parks and the many small "Wildgehege".
ANIMALS = ("petting_zoo", "wildlife_park", "safari_park", "enclosure", "birds", "falconry")
# Cafés are only family cafés: a play corner tagged in OSM, or a name that says so.
FAMILY_CAFE = re.compile(r"kinder|spiele?caf|famil|eltern|baby|kids", re.IGNORECASE)
NOT_FAMILY = re.compile(r"casino|spielhalle|agentur", re.IGNORECASE)
# OSM tags a playground's equipment on the playground itself as playground:<equipment>=yes.
EQUIPMENT = {
    "sandpit": ("sandpit",),
    "water_play": ("water", "splash_pad", "pump", "water_stream", "water_channel"),
    "swings": ("swing", "basketswing", "baby_swing"),
    "slides": ("slide",),
    "climbing": ("climbingframe", "climbingwall", "climbing_net", "structure"),
}


class Town(StrictModel):
    name: str
    lat: float
    lon: float
    # Overrides the config's radius_m, e.g. for a big city like Berlin.
    radius_m: int | None = None
    # more_towns only: towns with the same area share one Overpass box (the Ruhr); a town
    # without one is an area of its own.
    area: str | None = None


class PlacesConfig(StrictModel):
    schema_version: Literal[1] = 1
    object_key: str = "places/places.jsonl"
    # Toilets, changing tables and drinking water around the same towns (toilets.py).
    toilets_key: str = "places/toilets.jsonl"
    radius_m: int = 6000
    towns: list[Town] = Field(min_length=1)
    # Places only: towns Little Outings has no events for yet. Their places still go in the same
    # file; a run refreshes them with --more, apart from the towns above (toilets skip them).
    more_towns: list[Town] = []

    @property
    def every_town(self) -> list[Town]:
        return [*self.towns, *self.more_towns]


def load_places_config(path: Path) -> PlacesConfig:
    with path.open(encoding="utf-8") as handle:
        return PlacesConfig.model_validate(yaml.safe_load(handle))


def kind_of(tags: dict[str, str]) -> str | None:
    """Which family place an OSM object is, or None. The most specific tag wins."""
    name = tags.get("name") or ""
    if tags.get("tourism") in {"zoo", "aquarium"} or tags.get("zoo") in ANIMALS:
        return "zoo"
    if (
        tags.get("shop") == "farm"
        or tags.get("tourism") == "farm"
        or tags.get("attraction") == "farm"
    ):
        return "farm"
    if tags.get("leisure") in {"indoor_play", "trampoline_park"} or (
        tags.get("leisure") == "playground" and tags.get("indoor") == "yes"
    ):
        return "indoor"
    if tags.get("leisure") == "playground":
        return "playground"
    # Ice rinks and sledging runs: winter outings (Little Outings' "Ice & snow" chip).
    if tags.get("leisure") == "ice_rink" or tags.get("piste:type") == "sled":
        return "winter"
    if (
        tags.get("leisure") in {"water_park", "swimming_area"}
        or tags.get("amenity") == "public_bath"
        or (tags.get("leisure") == "sports_centre" and "swimming" in (tags.get("sport") or ""))
    ):
        return "pool"
    if tags.get("tourism") == "museum" and (
        tags.get("museum") in KID_MUSEUM_TYPE or KID_MUSEUM.search(name)
    ):
        return "museum"
    if (
        tags.get("tourism") == "theme_park"
        or tags.get("sport") == "climbing_adventure"
        or tags.get("attraction") == "summer_toboggan"
    ):
        return "fun"
    kids_area = tags.get("kids_area")
    if (
        tags.get("amenity") == "cafe"
        and ((kids_area and kids_area != "no") or FAMILY_CAFE.search(name))
        and not NOT_FAMILY.search(name)
    ):
        return "cafe"
    # Parks well known enough to have a Wikidata entry, and botanical gardens.
    if (
        tags.get("leisure") == "park"
        and tags.get("wikidata")
        and not re.search(r"platz$", name, re.IGNORECASE)
    ) or (tags.get("leisure") == "garden" and tags.get("garden:type") == "botanical"):
        return "park"
    return None


# Towns closer than this share one Overpass box; farther ones (Berlin vs. NRW) get their own,
# so a box never spans the country between them.
AREA_KM = 100


def areas(towns: list[Town]) -> list[list[Town]]:
    """Towns grouped so that each one is within AREA_KM of another in its group."""
    groups: list[list[Town]] = []
    for town in towns:
        near = [
            g for g in groups if any(km((t.lat, t.lon), (town.lat, town.lon)) <= AREA_KM for t in g)
        ]
        groups = [g for g in groups if all(g is not n for n in near)]
        groups.append([t for g in near for t in g] + [town])
    return sorted(groups, key=lambda g: min(towns.index(t) for t in g))


def named_areas(towns: list[Town]) -> list[list[Town]]:
    """more_towns grouped by their area label, in the order of the config."""
    groups: dict[str, list[Town]] = {}
    for town in towns:
        groups.setdefault(town.area or town.name, []).append(town)
    return list(groups.values())


def chosen_areas(
    towns: list[Town], only: Iterable[str] = (), more: list[Town] | None = None
) -> list[list[Town]]:
    """The areas to refresh: all areas of towns, or those of towns or more holding a named town."""
    wanted = {name.casefold() for name in only}
    if not wanted:
        return areas(towns)
    groups = areas(towns) + named_areas(more or [])
    known = {t.name.casefold() for t in [*towns, *(more or [])]}
    unknown = [name for name in only if name.casefold() not in known]
    if unknown:
        raise ValueError(f"not in config/places.yaml: {', '.join(unknown)}")
    return [g for g in groups if any(t.name.casefold() in wanted for t in g)]


def area_label(area: list[Town]) -> str:
    return area[0].name if len(area) == 1 else f"{area[0].name} and {len(area) - 1} more towns"


def replace_area(
    places: list[dict[str, Any]], area: list[Town], fresh: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Every other area's rows as they were, this area's from this run.

    Boxes overlap (Neuss lies inside Düsseldorf's), so a fresh row also replaces the same place
    under another area's town: each place is in the file once, under its nearest town.
    """
    names = {t.name for t in area}
    ids = {row["osm"] for row in fresh}
    rows = [row for row in places if row["town"] not in names and row["osm"] not in ids] + fresh
    return sorted(rows, key=lambda r: (r["town"].casefold(), r["kind"], r["name"].casefold()))


def bbox(towns: list[Town], radius_m: int) -> tuple[float, float, float, float]:
    """One box around the towns plus their radius; from_osm drops what is not near a town."""
    # One km more than the radius, as from_osm keeps places up to a km past it.
    pad = (max(t.radius_m or radius_m for t in towns) + 1000) / 111000
    lats, lons = [t.lat for t in towns], [t.lon for t in towns]
    lon_pad = pad / math.cos(math.radians(max(abs(lat) for lat in lats)))
    return (
        round(min(lats) - pad, 4),
        round(min(lons) - lon_pad, 4),
        round(max(lats) + pad, 4),
        round(max(lons) + lon_pad, 4),
    )


def overpass_queries(towns: list[Town], radius_m: int) -> list[tuple[str, str]]:
    """One small query per tag, with its kind: the public servers answer 504 to one big union.

    Exact tag values only: a regex or a bare key makes Overpass scan the whole box.
    """
    filters = [
        ("playground", '["leisure"="playground"]["name"]'),
        ("indoor", '["leisure"="indoor_play"]["name"]'),
        ("indoor", '["leisure"="trampoline_park"]["name"]'),
        ("park", '["leisure"="park"]["name"]["wikidata"]'),
        ("park", '["leisure"="garden"]["garden:type"="botanical"]["name"]'),
        ("zoo", '["tourism"="zoo"]["name"]'),
        ("zoo", '["tourism"="aquarium"]["name"]'),
        *(("zoo", f'["zoo"="{value}"]["name"]') for value in ANIMALS),
        ("farm", '["shop"="farm"]["name"]'),
        ("farm", '["tourism"="farm"]["name"]'),
        ("pool", '["leisure"="water_park"]["name"]'),
        ("pool", '["amenity"="public_bath"]["name"]'),
        ("pool", '["leisure"="sports_centre"]["sport"="swimming"]["name"]'),
        ("pool", '["leisure"="swimming_area"]["name"]'),
        ("museum", '["tourism"="museum"]["name"]'),
        ("winter", '["leisure"="ice_rink"]["name"]'),
        ("winter", '["piste:type"="sled"]["name"]'),
        # Every named café (kind_of keeps the family ones): a name regex timed out on Overpass.
        ("cafe", '["amenity"="cafe"]["name"]'),
        ("fun", '["tourism"="theme_park"]["name"]'),
        ("fun", '["sport"="climbing_adventure"]["name"]'),
        ("fun", '["attraction"="summer_toboggan"]["name"]'),
    ]
    boxes = [",".join(f"{value:g}" for value in bbox(area, radius_m)) for area in areas(towns)]
    return [
        (kind, f"[out:json][timeout:90][bbox:{box}];nwr{tag_filter};out center tags;")
        for box in boxes
        for kind, tag_filter in filters
    ]


def km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    x = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 12742 * math.asin(math.sqrt(x))


def _url(value: str | None) -> str | None:
    if not value:
        return None
    parts = urlsplit(value.strip())
    if parts.scheme not in {"http", "https"} or not parts.netloc or " " in parts.netloc:
        return None
    return value.strip() if parts.path else value.strip() + "/"


def _yes(value: str | None) -> bool:
    return value is not None and value not in {"no", "0"}


def play_features(tags: dict[str, str]) -> list[str]:
    """Playground facilities from the playground's own tags."""
    out = [
        feature
        for feature, keys in EQUIPMENT.items()
        if any(_yes(tags.get(f"playground:{key}")) for key in keys)
    ]
    try:
        max_age = float(tags.get("max_age") or 0)
    except ValueError:
        max_age = 0
    if 0 < max_age <= 4:
        out.append("toddler_area")
    if _yes(tags.get("bench")):
        out.append("benches")
    if tags.get("fenced") == "yes" or tags.get("fence") == "yes" or tags.get("barrier") == "fence":
        out.append("fenced")
    if tags.get("shade") == "yes" or tags.get("covered") == "yes":
        out.append("shade")
    return out


def from_osm(element: dict[str, Any], towns: list[Town], radius_m: int) -> dict[str, Any] | None:
    """One Overpass element → one compact place row, or None.

    Its town is the nearest one whose radius reaches it, so a place both a big city's and a
    neighbour's radius reach goes to the nearer of the two from either town's area.
    """
    tags = element.get("tags") or {}
    kind = kind_of(tags)
    center = element.get("center") or {}
    lat, lon = element.get("lat", center.get("lat")), element.get("lon", center.get("lon"))
    if not kind or not tags.get("name") or not isinstance(lat, int | float):
        return None
    if not isinstance(lon, int | float):
        return None
    reached = [
        (distance, t.name)
        for t in towns
        if (distance := km((t.lat, t.lon), (lat, lon))) <= (t.radius_m or radius_m) / 1000 + 1
    ]
    if not reached:
        return None
    town = min(reached)[1]
    facilities = []
    if tags.get("changing_table") == "yes" or tags.get("diaper") == "yes":
        facilities.append("changing_table")
    if tags.get("toilets") == "yes":
        facilities.append("toilets")
    if kind in {"zoo", "farm"}:
        facilities.append("animals")
    if tags.get("shop") == "farm":
        facilities.append("farm_shop")
    if kind == "zoo" and tags.get("playground") == "yes":
        facilities.append("playground")
    if kind == "playground":
        facilities.extend(play_features(tags))
    kids_area = tags.get("kids_area")
    if kind == "cafe" and kids_area and kids_area != "no":
        facilities.append("playground" if kids_area == "outdoor" else "indoor_play")
    fee = tags.get("fee")
    row = {
        "osm": f"{element['type']}/{element['id']}",
        "kind": kind,
        "name": tags["name"],
        "town": town,
        "street": " ".join(filter(None, (tags.get("addr:street"), tags.get("addr:housenumber")))),
        "postcode": tags.get("addr:postcode"),
        "lat": round(lat, 6),
        "lon": round(lon, 6),
        "hours": tags.get("opening_hours"),
        "website": _url(tags.get("website") or tags.get("contact:website")),
        "indoor": True if kind in {"indoor", "museum"} or tags.get("indoor") == "yes" else None,
        "free": True
        if fee == "no"
        else False
        if fee == "yes"
        else True
        if kind == "playground"
        else None,
        "facilities": facilities,
    }
    return {key: value for key, value in row.items() if value not in (None, "")}


def reverse_key(row: dict[str, Any]) -> str:
    """The OSM id plus the rounded point, so a moved place is looked up again."""
    return f"{row['osm']}@{row['lat']:.4f},{row['lon']:.4f}"


def from_nominatim(answer: dict[str, Any]) -> dict[str, str]:
    """One reverse answer → {road, district, postcode}, without empty fields."""
    address = answer.get("address") or {}

    def first(*keys: str) -> str | None:
        return next((address[key] for key in keys if address.get(key)), None)

    row = {
        "road": first("road", "pedestrian", "footway", "path"),
        # The part of town parents know: Stadtteil first, then smaller or larger units.
        "district": first(
            "suburb", "city_district", "quarter", "neighbourhood", "village", "hamlet"
        ),
        "postcode": first("postcode"),
    }
    return {key: value for key, value in row.items() if value}


class OverpassError(RuntimeError):
    pass


def overpass(
    client: httpx.Client,
    query: str,
    servers: Iterable[str] = OVERPASS_SERVERS,
    sleep: Callable[[float], None] = time.sleep,
) -> list[dict[str, Any]]:
    """Runs one query, trying each server up to twice; returns its elements."""
    errors = []
    for attempt in range(2):
        for endpoint in servers:
            try:
                response = client.post(endpoint, data={"data": query}, timeout=120)
                response.raise_for_status()
                body = response.json()
                # Overpass answers 200 with a remark when the query timed out or ran out of memory.
                if body.get("remark"):
                    raise OverpassError(body["remark"])
                return body.get("elements") or []
            except (httpx.HTTPError, ValueError, OverpassError) as error:
                errors.append(f"{urlsplit(endpoint).netloc}: {error}"[:200])
                sleep(15 * (attempt + 1))
    raise OverpassError(f"Overpass failed for {query[query.index('nwr') :]} ({'; '.join(errors)})")


def fetch_places(
    config: PlacesConfig,
    previous: list[dict[str, Any]],
    client: httpx.Client,
    sleep: Callable[[float], None] = time.sleep,
    servers: Iterable[str] = OVERPASS_SERVERS,
    everyone: list[Town] | None = None,
) -> tuple[list[dict[str, Any]], set[str], int]:
    """This week's places; a kind whose query failed keeps last week's rows.

    Each place goes to its nearest town among everyone (all configured towns), not only the
    area's, so a place in two overlapping boxes gets the same town from either run.
    """
    servers = list(servers)

    def run(job: tuple[int, tuple[str, str]]) -> tuple[str, list[dict[str, Any]] | OverpassError]:
        # One query per server at a time, each starting on another one: a query takes the
        # public servers about a minute, so asking them one after another took 40 minutes an
        # area and more than a job's time limit for six or seven areas.
        i, (kind, query) = job
        mine = servers[i % len(servers) :] + servers[: i % len(servers)]
        try:
            return kind, overpass(client, query, mine, sleep)
        except OverpassError as error:
            return kind, error
        finally:
            sleep(2)

    elements: list[dict[str, Any]] = []
    failed: set[str] = set()
    jobs = list(enumerate(overpass_queries(config.towns, config.radius_m)))
    with ThreadPoolExecutor(max_workers=len(servers)) as pool:
        for kind, result in pool.map(run, jobs):
            if isinstance(result, OverpassError):
                failed.add(kind)
                print(result, flush=True)
            else:
                elements.extend(result)
    if not elements:
        raise OverpassError("Overpass returned no places; keeping the previous list")
    rows = {row["osm"]: row for row in previous if row["kind"] in failed}
    for element in elements:
        row = from_osm(element, everyone or config.towns, config.radius_m)
        if row:
            rows[row["osm"]] = row
    places = sorted(
        rows.values(),
        key=lambda r: (r["town"].casefold(), r["kind"], r["name"].casefold()),
    )
    return places, failed, len(elements)


def add_reverse(
    places: list[dict[str, Any]],
    previous: list[dict[str, Any]],
    client: httpx.Client,
    max_lookups: int,
    sleep: Callable[[float], None] = time.sleep,
    endpoint: str = NOMINATIM_REVERSE,
) -> tuple[int, int]:
    """Nearest road and district per place; reuses last week's answer unless the place moved.

    Nominatim's policy: at most one request a second, a real user agent, results kept.
    Returns (looked up, failed).
    """
    known = {reverse_key(row): row["nearby"] for row in previous if "nearby" in row}
    looked_up = failed = 0
    for row in places:
        key = reverse_key(row)
        if key in known:
            row["nearby"] = known[key]
            continue
        if looked_up + failed >= max_lookups or failed >= 20:
            continue  # blocked, down or over the limit: the next run carries on
        params = {
            "format": "jsonv2",
            "zoom": "17",
            "addressdetails": "1",
            "accept-language": "de",
            "lat": str(row["lat"]),
            "lon": str(row["lon"]),
        }
        try:
            response = client.get(endpoint, params=params, timeout=30)
            response.raise_for_status()
            row["nearby"] = from_nominatim(response.json())
            looked_up += 1
        except (httpx.HTTPError, ValueError) as error:
            failed += 1
            if failed == 1:
                print(f"nominatim: {error}")
        sleep(1.1)
    return looked_up, failed


def read_places(store: ObjectStore, key: str) -> list[dict[str, Any]]:
    try:
        body = store.get(key)
    except FileNotFoundError:  # first run: nothing there yet
        return []
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") in {"NoSuchKey", "404"}:
            return []
        raise
    return [json.loads(line) for line in body.decode().splitlines() if line.strip()]


def write_places(places: list[dict[str, Any]], store: ObjectStore, key: str) -> str:
    """The places, one per line, plus a small manifest next to them."""
    body = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in places)
    store.put(key, body.encode(), "application/x-ndjson")
    manifest = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "place_count": len(places),
        "kinds": dict(sorted(Counter(row["kind"] for row in places).items())),
        "object_key": key,
        "attribution": "© OpenStreetMap contributors, ODbL",
    }
    store.put(
        key.rsplit(".", 1)[0] + ".json",
        json.dumps(manifest, ensure_ascii=False, indent=2).encode(),
        "application/json",
    )
    return key
