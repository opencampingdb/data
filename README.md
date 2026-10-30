# Open Camping DB data

Open Camping DB is a free, open dataset of places to camp and stop in
Australia: camp sites, caravan parks, rest areas, dump points, drinking water,
toilets, showers and picnic areas. It is built from OpenStreetMap every night
and improved by feedback from the apps that use it. Any app may use the data
and send feedback. CampOracle is the first.

This repository holds the records, the pipeline that builds them and the
workflows that publish them.

## Download

Every hour in which `main` has changed, a new GitHub Release is published.
Start with the manifest:

```
https://github.com/opencampingdb/data/releases/latest/download/manifest.json
```

It names its release, a `data_version`, and for each of the two data files
an absolute `url`, its SHA-256, its size and its record count. Each `url`
points at the file in that same release, for example:

```
https://github.com/opencampingdb/data/releases/download/data-20261030T160000Z/places.ndjson.gz
```

Releases are tagged `data-<YYYYMMDDTHHMMSSZ>`, the time of the commit they
were built from. The newest is marked as the repository's Latest release, which
is what the stable URL above serves. The newest 60 releases are kept and older
ones are deleted.

Apps should read [CONNECTING.md](CONNECTING.md) before they download anything.

## What a record looks like

`places.ndjson.gz` has one JSON record per line, sorted by `id`:

```json
{
  "id": "ocdb_01M66CKP2X55A8CVGR95ZFJ170",
  "name": "Smiths Mill Campground",
  "kind": "camp_site",
  "location": {"lat": -37.107745, "lon": 142.423232},
  "osm": {"type": "node", "id": 316599210, "version": 5},
  "facts": {"fee": true, "tents": true, "caravans": true},
  "fact_sources": {"fee": "osm", "tents": "osm", "caravans": "osm"},
  "status": "open",
  "rating": null,
  "updated": "2026-10-30T16:00:00Z"
}
```

The full contract is in [schema/place.schema.json](schema/place.schema.json)
and [schema/review.schema.json](schema/review.schema.json). In the repository
each place is a file at `places/<last two characters of id, lower case>/<id>.json`, and the reviews
of a place are at `reviews/<place id>.ndjson`.

## Licence

The database is licensed under the [Open Database License (ODbL) 1.0](LICENSE).
Any rights in its individual contents are licensed under the
[Database Contents License (DbCL) 1.0](LICENSE-CONTENTS). It contains
OpenStreetMap data, which is also under the ODbL. See [NOTICE](NOTICE).

In short: you may use, share and adapt the data, including commercially, if
you show the attribution below, and if you publicly use or share an adapted
database you offer it under the ODbL too. The licence text is what binds.

## Attribution

Anything that shows this data must show:

```
© OpenStreetMap contributors, Open Camping DB
```

## How it is built

1. **Nightly OSM sync** (`.github/workflows/sync-osm.yml`, 16:00 UTC). Downloads
   the Geofabrik extract for Australia, keeps the camping tags, merges the result
   into `places/` and opens or updates one pull request from the `osm-sync`
   branch. The PR body lists what is new, what changed and what has gone from
   OSM. Records that have gone from OSM are never deleted: a person decides. The
   PR merges itself once CI passes if fewer than 2% of records changed, and
   waits for a person otherwise.
2. **Feedback.** Apps send reports, reviews and checks to the intake run by the
   [feedback-engine](https://github.com/opencampingdb/feedback-engine). A
   workflow in the private `inbox` repository applies them every hour and
   keeps at most two pull requests open here: `feedback/auto` for changes the
   automatic rules made, which merges itself once CI passes, and
   `feedback/review` for anything a person must check. Facts, names and
   positions set by feedback are marked `"feedback"` in `fact_sources` and the
   OSM sync leaves them alone.
3. **Validation** (`.github/workflows/validate.yml`) runs on every pull request:
   the tests, the schema and content checks, and a trial build of the release.
4. **Release** (`.github/workflows/release.yml`) runs every hour and on
   demand. When `main` has changed since the newest release, it publishes the
   files described above as a new release and prunes the oldest.

The pipeline, its tag mapping and its rules are described in
[pipeline/README.md](pipeline/README.md).

To fix a wrong name, position or tag, the best place is OpenStreetMap itself:
the next nightly sync picks it up.

## Contact

Data problems, takedown requests, security reports and abuse of the dataset
or the feedback intake: abuse@camporacle.com.au.

App keys and help connecting an app: dev@camporacle.com.au.

## Running it locally

```
python -m pip install -r requirements.txt
python -m pytest
python -m pipeline.extract some-area.osm.pbf --out extract.ndjson
python -m pipeline.sync extract.ndjson --root .
python -m pipeline.validate --root .
python -m pipeline.release --root . --out dist
```

Small `.osm.pbf` extracts are available from
[Geofabrik](https://download.geofabrik.de/australia-oceania.html).

## Repository setup

The workflows need the following before they work as described.

### GitHub Apps

Open Camping DB uses two GitHub Apps. Only one of them is installed here.

- **`opencampingdb-bot`** is installed on this repository with read and write
  access to contents and pull requests, and on `inbox` with read and write
  access to contents. In this repository, store its client ID in the Actions
  variable `OCDB_BOT_CLIENT_ID` and its private key in the Actions secret
  `OCDB_BOT_PRIVATE_KEY`. Its key lives only in Actions secrets.
  - The nightly OSM sync pushes the `osm-sync` branch, opens or updates its PR
    and turns on auto-merge with this App's token.
  - The apply-feedback workflow in `inbox` uses it to push the two fixed
    branches here, `feedback/auto` (automatic changes only, merges itself once
    CI passes) and `feedback/review` (changes a person must check, labelled
    `needs-review`), and to keep at most one open PR from each.

  PRs and merges are never made with the default `GITHUB_TOKEN`: events it
  causes do not start other workflows, so its PRs would never get the
  Validate check.
- **`opencampingdb-intake`** is installed on `inbox` only, with write access to
  contents. The intake Worker holds its key and writes raw submissions there.
  It has no access to this repository.

The Release workflow only creates releases in this repository, so it uses the
default `GITHUB_TOKEN` with `contents: write`.

### Settings

- **Allow auto-merge** in the repository settings.
- **Branch protection on `main`**: changes arrive by pull request only, and
  the status check `Tests, validate and build` is required. Without a required
  check, auto-merge has nothing to wait for.
- Create the label `needs-review` for the `feedback/review` PRs.
- The first OSM sync changes every record (100%), so its PR waits for a
  maintainer to merge it.
