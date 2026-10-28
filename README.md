# osm-family-data

Family places, public toilets, changing tables and drinking water from
[OpenStreetMap](https://www.openstreetmap.org), around the towns that
[Little Outings](https://outings.allo.land) covers (Cologne, Düsseldorf, Bonn, Aachen and the
Bergisches Land, Berlin, Munich, Frankfurt and Hamburg), plus places (not toilets) around every
other German city of 100,000 or more, from Dortmund to Dresden. GitHub Actions in this repo fetch them
from Overpass and write open JSONL files to a public Cloudflare R2 bucket, so sites and apps only
download a file and never wait on OpenStreetMap.

Data © OpenStreetMap contributors, [ODbL](https://opendatacommons.org/licenses/odbl/). Whoever
shows it must credit OpenStreetMap. The code is MIT.

## Files

| File | What | Refreshed |
| --- | --- | --- |
| [`places/places.jsonl`](https://places.allo.land/places/places.jsonl) | Playgrounds, parks, zoos, farms, pools, kids' museums, indoor play, family cafés; with nearest road and district | 1st of every month |
| [`places/toilets.jsonl`](https://places.allo.land/places/toilets.jsonl) | Public toilets, other spots with a changing table, drinking water | 15th of every month |

Next to each file is a small manifest (`places/places.json`, `places/toilets.json`) with the
time it was written and counts per kind (and per town for toilets).

One JSON object per line; fields without a value are left out.

**places.jsonl**: `osm` (e.g. `way/123`), `kind` (playground, indoor, park, zoo, farm, pool,
museum, cafe, fun, winter: ice rinks and sledging runs), `name`, `town`, `lat`, `lon`, and when known `street`, `postcode`, `hours`
(OSM `opening_hours`), `website`, `indoor`, `free`, `facilities` (e.g. `swings`, `toilets`,
`changing_table`) and `nearby` {`road`, `district`, `postcode`} from Nominatim.

**toilets.jsonl**: `osm`, `kind` (`toilets`, `changing_table` for a café, shop or office with
one, `water` for a drinking-water tap), `town`, `lat`, `lon`, and when known `name`, `at` (what a
changing-table spot is, e.g. `chemist`, `cafe`), `changing_table`, `changing_table_location`
(e.g. `wheelchair_toilet`), `wheelchair` (yes, limited, no), `free`, `customers_only`, `hours`,
`operator`, `website`. Toilets marked private are left out.

## Running

```sh
uv sync
uv run osm-family-data toilets --town Köln       # writes data/places/toilets.jsonl
uv run osm-family-data places --town Hamburg     # writes data/places/places.jsonl
uv run pytest
```

Towns are in [`config/places.yaml`](config/places.yaml); towns within 100 km of each other share
one Overpass area (all of NRW is one, each other city is its own). `more_towns` are the
places-only towns: towns with the same `area` share one box (the Ruhr), the others are one each.
`--more` refreshes those instead of the main towns, and `--part K/N` only every Nth of them
(the workflow runs one job per area, one after another, from `osm-family-data areas`).
`--skip-days N` skips areas fetched in the last N days (the manifest's `refreshed`), so a run
started again after a timeout carries on where it stopped. Boxes overlap, so each place goes to
the nearest town whose radius reaches it and is in the file once. `--town` (repeatable, or the
checkboxes on a manual workflow run) refreshes only the areas of those towns and keeps the other
areas' rows. Each area is saved as soon as it is done.

`--storage r2` writes to the bucket instead. The workflows need four repository secrets:
`R2_ENDPOINT_URL`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` (an R2 token that can only write
the places bucket) and `R2_PLACES_BUCKET`.

Overpass and Nominatim are free public services: queries stay small (one per tag and area), the
street lookup keeps to one request a second and reuses the last answer until a place moves.
