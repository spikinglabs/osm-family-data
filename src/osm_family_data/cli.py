"""`osm-family-data places` and `osm-family-data toilets`: one area at a time, saved as it goes."""

from __future__ import annotations

import os
import time
from collections import Counter
from pathlib import Path
from typing import Annotated

import httpx
import typer

from osm_family_data import toilets as toilet_data
from osm_family_data.places import (
    USER_AGENT,
    OverpassError,
    PlacesConfig,
    Town,
    add_reverse,
    area_label,
    areas,
    chosen_areas,
    fetch_places,
    load_places_config,
    named_areas,
    read_places,
    replace_area,
    write_places,
)
from osm_family_data.storage import LocalObjectStore, ObjectStore, R2ObjectStore

app = typer.Typer(no_args_is_help=True)

CONFIG = Path("config/places.yaml")
TownOption = Annotated[list[str], typer.Option("--town", help="Only the areas of these towns.")]


def _store(storage: str, local_root: Path) -> ObjectStore:
    if storage == "local":
        return LocalObjectStore(local_root)
    bucket = os.environ.get("R2_PLACES_BUCKET")
    if not bucket:
        raise typer.BadParameter("R2_PLACES_BUCKET is required for storage r2")
    return R2ObjectStore(bucket)


def _areas(settings: PlacesConfig, towns: list[str], more: list[Town] | None = None):
    try:
        return chosen_areas(settings.towns, towns, more)
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error


def _part(value: str) -> tuple[int, int]:
    """ "2/6" → (2, 6): the second of six shares of the areas."""
    try:
        k, n = (int(x) for x in value.split("/"))
    except ValueError as error:
        raise typer.BadParameter("--part is K/N, e.g. 2/6") from error
    if not 1 <= k <= n:
        raise typer.BadParameter("--part is K/N with 1 <= K <= N")
    return k, n


@app.command("places")
def places_command(
    storage: Annotated[str, typer.Option("--storage")] = "local",
    places_config: Annotated[Path, typer.Option("--places-config")] = CONFIG,
    local_root: Annotated[Path, typer.Option("--local-root")] = Path("data"),
    max_lookups: Annotated[int, typer.Option("--max-lookups")] = 2000,
    towns: TownOption = [],  # noqa: B006
    more: Annotated[
        bool, typer.Option("--more", help="The places-only areas (more_towns) instead.")
    ] = False,
    part: Annotated[
        str | None, typer.Option("--part", help="K/N: every Nth area from the Kth, one job each.")
    ] = None,
) -> None:
    """Fetch family places from OpenStreetMap into the public places bucket.

    Without --town the areas of `towns` in config/places.yaml are refreshed (with --more, those
    of `more_towns`); with it, only the areas holding those towns. The rest of the file stays
    as it was.
    """
    settings = load_places_config(places_config)
    if towns:
        chosen = _areas(settings, towns, settings.more_towns)
    else:
        chosen = named_areas(settings.more_towns) if more else areas(settings.towns)
    if part:
        k, n = _part(part)
        chosen = chosen[k - 1 :: n]
    everyone = settings.every_town
    store = _store(storage, local_root)
    places = read_places(store, settings.object_key)
    if not towns:  # a full run also drops towns taken out of the config
        configured = {t.name for t in everyone}
        places = [row for row in places if row["town"] in configured]
    updated, broken, spent = [], [], 0
    with httpx.Client(headers={"user-agent": USER_AGENT}, follow_redirects=True) as client:
        # One area at a time, saved as soon as it is done: a failing or slow area only loses
        # its own update, and the other areas keep what they had.
        for area in chosen:
            label = area_label(area)
            started = time.monotonic()
            names = {t.name for t in area}
            previous = [row for row in places if row["town"] in names]
            try:
                fresh, failed, elements = fetch_places(
                    settings.model_copy(update={"towns": area}),
                    previous,
                    client,
                    everyone=everyone,
                )
            except OverpassError as error:
                typer.echo(f"{label}: {error}", err=True)
                broken.append(label)
                continue
            # Streets are reused from the whole file: a place may have been under another
            # area's town before (Neuss's playgrounds were Düsseldorf's until Neuss was added).
            looked_up, lookup_failed = add_reverse(
                fresh, places, client, max(0, max_lookups - spent)
            )
            spent += looked_up + lookup_failed
            places = replace_area(places, area, fresh)
            write_places(places, store, settings.object_key)
            kinds = ", ".join(
                f"{kind} {n}" for kind, n in sorted(Counter(r["kind"] for r in fresh).items())
            )
            kept = (
                f"; kept last week's {', '.join(sorted(failed))} (query failed)" if failed else ""
            )
            missing = sum("nearby" not in row for row in fresh)
            line = (
                f"{label}: {elements} elements, {len(fresh)} places ({kinds}){kept}; streets: "
                f"{looked_up} looked up, {lookup_failed} failed, {missing} still without"
            )
            typer.echo(f"{line} ({(time.monotonic() - started) / 60:.0f} min)")
            updated.append(label)
    done = f"places: {len(places)} in all; updated {', '.join(updated) or 'nothing'}"
    if broken:
        typer.echo(f"{done}; Overpass failed for {', '.join(broken)} (kept their old rows)")
        raise typer.Exit(1)
    typer.echo(f"{done} -> {settings.object_key}")


@app.command("toilets")
def toilets_command(
    storage: Annotated[str, typer.Option("--storage")] = "local",
    places_config: Annotated[Path, typer.Option("--places-config")] = CONFIG,
    local_root: Annotated[Path, typer.Option("--local-root")] = Path("data"),
    towns: TownOption = [],  # noqa: B006
) -> None:
    """Fetch public toilets, changing tables and drinking water into the public places bucket.

    Same towns and areas as `places`; --town refreshes only the areas holding those towns.
    """
    settings = load_places_config(places_config)
    chosen = _areas(settings, towns)
    store = _store(storage, local_root)
    key = settings.toilets_key
    rows = read_places(store, key)
    if not towns:
        configured = {t.name for t in settings.towns}
        rows = [row for row in rows if row["town"] in configured]
    updated, broken = [], []
    with httpx.Client(headers={"user-agent": USER_AGENT}, follow_redirects=True) as client:
        for area in chosen:
            label = area_label(area)
            names = {t.name for t in area}
            previous = [row for row in rows if row["town"] in names]
            try:
                fresh, failed, elements = toilet_data.fetch_toilets(
                    settings.model_copy(update={"towns": area}), previous, client
                )
            except OverpassError as error:
                typer.echo(f"{label}: {error}", err=True)
                broken.append(label)
                continue
            rows = toilet_data.replace_area(rows, area, fresh)
            toilet_data.write_toilets(rows, store, key)
            kinds = Counter(r["kind"] for r in fresh)
            tables = sum(1 for r in fresh if r["kind"] == "toilets" and r.get("changing_table"))
            kept = f"; kept the last {', '.join(sorted(failed))} (query failed)" if failed else ""
            typer.echo(
                f"{label}: {elements} elements, {kinds['toilets']} toilets ({tables} with a "
                f"changing table), {kinds['changing_table']} other changing tables, "
                f"{kinds['water']} drinking water{kept}"
            )
            updated.append(label)
    done = f"toilets: {len(rows)} in all; updated {', '.join(updated) or 'nothing'}"
    if broken:
        typer.echo(f"{done}; Overpass failed for {', '.join(broken)} (kept their old rows)")
        raise typer.Exit(1)
    typer.echo(f"{done} -> {key}")
