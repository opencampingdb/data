# Open Camping DB data

Open Camping DB is a free, open dataset of the places travellers camp and
stop at in Australia: free camps, campgrounds, national park, state forest
and reserve campsites, caravan parks, showgrounds, farm stays, backpacker
hostels and motels; rest areas, dump points, drinking water, toilets,
showers, laundromats, picnic areas and long vehicle parking; information
centres, fuel, LPG, mechanics, pubs, emergency hospitals and ferry terminals;
lookouts, swimming spots, boat ramps, museums and galleries, recreation areas,
experiences and attractions. Its kinds of place are the 22 site types of the
OTWD (Open Touring and Waypoint Database) vocabulary, plus 11 of its own.
Each place can carry its site features (who can get in, what is there, what
to do, phone and internet signal, warnings), what it charges, its park chain
and its contacts. It is built from OpenStreetMap every night and improved by
feedback from the apps that use it. Any app may use the data and send
feedback.

This repository holds the records, the pipeline that builds them and the
workflows that publish them. App developers should start with
[CONNECTING.md](CONNECTING.md).

## Download

Each change to `main` is published as a new GitHub Release within minutes.
Start with the manifest:

```
https://github.com/opencampingdb/data/releases/latest/download/manifest.json
```

The manifest names its release, its `data_version`, the `format_version` and
`format_minor` of the records and the `layout_version` of the database. It
lists the four NDJSON files (`places`, `reviews`, `merged` and `commons`),
the SQLite database (`database`) and the `changesets` that bring an earlier
database up to date, each with an absolute `url`, its SHA-256 and its size.
Each `url` points at the file in the release that made it, for example:

```
https://github.com/opencampingdb/data/releases/download/data-20261030T160000Z/places.ndjson.gz
```

Releases are tagged `data-<YYYYMMDDTHHMMSSZ>`, the time of the commit they
were built from. The newest is marked as the repository's Latest release,
which is what the stable URL above serves. The newest 60 releases are kept
and older ones are deleted.

Read [CONNECTING.md](CONNECTING.md) before downloading anything. It covers
checking the files, staying current, the record format and its changelog,
the attribution and sending feedback.

## What a record looks like

`places.ndjson.gz` has one JSON record per line, sorted by `id`:

```json
{
  "id": "ocdb_01M66CKP2X55A8CVGR95ZFJ170",
  "name": "Smiths Mill Campground",
  "kind": "national_park_campsite",
  "group": "overnight",
  "location": {"lat": -37.107745, "lon": 142.423232},
  "osm": {"type": "node", "id": 316599210, "version": 5, "timestamp": "2021-06-05T03:07:04Z"},
  "facts": {"fee": true, "tents": true, "caravans": true},
  "fact_sources": {"fee": "osm", "tents": "osm", "caravans": "osm"},
  "status": "open",
  "rating": null,
  "updated": "2026-10-30T16:00:00Z"
}
```

A place can carry many more facts than this one: site features such as
`campfires`, `telstra_signal` and `crocodiles`, a `charge` such as
`57 AUD/night`, a park chain in `brand`, and contacts. Section 4 of
[CONNECTING.md](CONNECTING.md) describes them, and section 1 has the format
changelog. The current format is 13.

The full contract is [schema/place.schema.json](schema/place.schema.json)
and [schema/review.schema.json](schema/review.schema.json). The kinds and
their groups are in [schema/kinds.json](schema/kinds.json).

In the repository:

| Path | Holds |
|---|---|
| `places/<last two characters of id, lower case>/<id>.json` | one place |
| `reviews/<place id>.ndjson` | the reviews of one place |
| `commons/matches.ndjson` | Wikimedia Commons photos matched to places to stay |
| `moderation/blocked_contributors.json` ([schema](schema/blocked_contributors.schema.json)) | contributors a person has blocked |
| `moderation/removed_reviews.json` ([schema](schema/removed_reviews.schema.json)) | reviews a person removed, which never come back |
| `moderation/distinct_places.json` ([schema](schema/distinct_places.schema.json)) | pairs a person found to be separate places, which the duplicate finder skips |
| `moderation/commons-do-not-match.json` ([schema](schema/commons_do_not_match.schema.json)) | Commons photos travellers reported as not of their place, never matched again |
| [`maproulette/`](maproulette/README.md) | what OSM's mappers should check, as MapRoulette challenge files |
| [`pipeline/`](pipeline/README.md) | the pipeline, its tag mapping and its rules |

## Licence

The database is licensed under the [Open Database License (ODbL) 1.0](LICENSE).
Any rights in its individual contents are licensed under the
[Database Contents License (DbCL) 1.0](LICENSE-CONTENTS). It contains
OpenStreetMap data, which is also under the ODbL. See [NOTICE](NOTICE).

The kind vocabulary in [schema/kinds.json](schema/kinds.json) comes from the
OTWD site types and is under the Apache License 2.0
([LICENSE-APACHE-2.0](LICENSE-APACHE-2.0)).

In short: you may use, share and adapt the data, including commercially, if
you show the attribution below, and if you publicly use or share an adapted
database you offer it under the ODbL too. The licence text is what binds.

## Attribution

Anything that shows this data must show:

```
© OpenStreetMap contributors, Open Camping DB
```

## How it is built

1. **Nightly OSM sync** (`.github/workflows/sync-osm.yml`, 16:00 UTC, or
   started by hand). Downloads the Geofabrik extract for Australia, keeps the
   tags that map to a kind of place, classifies camp sites by the park, state
   forest, reserve or showground they lie in, merges the result into
   `places/`, rewrites the MapRoulette files and opens or updates one pull
   request from the `osm-sync` branch. The PR body lists what is new, what
   changed and what has gone from OSM. Records that have gone from OSM are
   never deleted: a person decides. The PR merges itself once CI passes if
   fewer than 2% of records changed, and waits for a person otherwise. A
   change to the tag mapping reaches the data only through this sync, so
   after one merges, run the sync before expecting a release to carry it.
2. **Feedback.** Apps send reports, reviews and checks to the intake run by
   the [feedback-engine](https://github.com/opencampingdb/feedback-engine). A
   workflow in the private `inbox` repository applies them every hour and
   keeps at most two pull requests open here: `feedback/auto` for changes
   the automatic rules made, which merges itself once CI passes, and
   `feedback/review` for anything a person must check. Facts, names,
   positions, kinds and descriptions set by feedback are marked `"feedback"`
   in `fact_sources`, and the OSM sync leaves them alone. A check that finds
   an open place still there sets `last_confirmed`.
3. **Contributed places and closures.** Places that are not in OSM, and
   closures of existing places, can be contributed directly under the ODbL
   by pull request. A contributed place has `osm: null` and `"contributed"`
   in `fact_sources` for its name and location. A contributed closure sets
   `status: "closed"` with `fact_sources.status` set to `"contributed"`.
   Every contributed value also gets the time it was accepted in
   `fact_dates`, as feedback and owner values do. The OSM sync never changes
   a place without an OSM link, and keeps a contributed status.
4. **Community reports.** A person imports anonymous community reports from
   public camping groups by pull request. Only facts are imported, never the
   reports' own words. Each value has `"community_report"` in `fact_sources`
   and, in `fact_dates`, the first day of the month the report was made. A
   report that matches one place adds facts to it without replacing any
   value the place already has. A report that matches none becomes a new
   place only where it could be located at a specific feature, with
   `osm: null` and `"community_report"` for its name and location. Community
   report values never go to OSM: the MapRoulette challenges leave them out,
   and leave out places named or located by them. The OSM sync keeps them as
   they are.
5. **Owners.** A place's owner can claim it through the owner page at
   `https://opencampingdb.github.io/owners/`. A person approves every claim
   and every private property listing before anything is published. After
   that the owner's updates (facts, booking details, a short description, a
   temporary closure) are marked `"owner"` in `fact_sources`, and the OSM
   sync never overwrites them. Private land that OSM does not map appears
   only as a `farm_stay` its owner lists, and apps show it as "Private
   property. Book with the owner first."
6. **Duplicates.** After every OSM sync, likely duplicates (for example a
   campground mapped as both a node and a way) are listed in the PR body
   under "Possible duplicates". Sync never merges them. A person merges a
   pair with `python -m pipeline.merge <keep_id> <drop_id>` in a separate
   PR, or merges every pair the automatic merge rule accepts (the same name
   once tidied, the same group, under 50 m apart and no major road between)
   with `python -m pipeline.merge --auto`, which lists each merge in
   `moderation/automatic-merges.csv` for review. Either way the dropped
   record stays as a tombstone that points at the kept one, and releases
   list it in `merged.ndjson.gz` so apps can redirect old ids.
7. **Commons photos** (`.github/workflows/commons.yml`, 18:00 UTC, or started
   by hand). Matches places to stay with freely licensed Wikimedia Commons
   photos, up to 2,000 places a run, each place again after 30 days, and
   opens or updates a pull request from the `commons-sync` branch that merges
   itself once CI passes.
8. **Validation** (`.github/workflows/validate.yml`) runs on every pull
   request: the tests, the schema and content checks, and a trial build of
   the release and its database. The **Neutral** check
   (`.github/workflows/neutral.yml`) runs on every push and pull request and
   fails when a file or commit message names an app that uses the data.
9. **Release** (`.github/workflows/release.yml`) runs on every push to
   `main`, every hour as a backstop, and on demand. When `main` has changed
   since the newest release, it publishes the files described above as a new
   release, prunes the oldest and loads the place index into the feedback
   intake.
10. **MapRoulette** (`.github/workflows/maproulette.yml`). When a merge
    changes a challenge file on `main`, it opens or updates the issue
    "Rebuild the MapRoulette challenges" (see
    [maproulette/README.md](maproulette/README.md)).

To fix a wrong name, position or tag, the best place is OpenStreetMap
itself: the next nightly sync picks it up.

## Contact

Data problems and corrections: open an issue in this repository. Takedown
requests: the **Takedown** issue form, or **Report a vulnerability** on the
Security tab for anything that must stay private. Security problems and abuse
of the dataset or the feedback intake: **Report a vulnerability**.

App keys and help connecting an app: the **App key** issue form in
[feedback-engine](https://github.com/opencampingdb/feedback-engine/issues/new/choose).

## Running it locally

You need Python 3.12 and, for the opening hours check, Node 24
(`tools/oh-check/.node-version`). Small `.osm.pbf` extracts are available
from [Geofabrik](https://download.geofabrik.de/australia-oceania.html).

```sh
python -m pip install -r requirements.txt
npm ci --ignore-scripts --prefix tools/oh-check
python -m pytest
python -m pipeline.extract some-area.osm.pbf --out extract.ndjson --geometry geometry.ndjson
python -m pipeline.sync extract.ndjson --root . --geometry geometry.ndjson
python -m pipeline.validate --root .
python -m pipeline.release --root . --out dist
python -m pipeline.database --release-dir dist
```

[pipeline/README.md](pipeline/README.md) lists every command.

## Repository setup

The workflows need the following before they work as described.

### GitHub Apps

Open Camping DB uses two GitHub Apps.

- **`opencampingdb-bot`** is installed on this repository with read and
  write access to contents and pull requests, and on `inbox` with read and
  write access to contents. Its key lives only in Actions secrets.
  - The nightly OSM sync and the Commons photos workflow push their
    branches (`osm-sync`, `commons-sync`), open or update their PRs and turn
    on auto-merge with this App's token.
  - The nightly OSM sync also makes a read-only token for `inbox` and checks
    out its `applied/` folder, the evidence MapRoulette report tasks need
    (see [maproulette/README.md](maproulette/README.md)). Nothing from the
    inbox is committed.
  - The apply-feedback workflow in `inbox` uses it to push the two fixed
    branches here, `feedback/auto` and `feedback/review` (labelled
    `needs-review`), and to keep at most one open PR from each.

  PRs and merges are never made with the default `GITHUB_TOKEN`: events it
  causes do not start other workflows, so its PRs would never get the
  Validate check.
- **`opencampingdb-intake`** is installed on `inbox` with write access to
  contents. The intake Worker holds its key and writes raw submissions
  there. It is installed on this repository too only when the feedback
  engine's AI reviewer is on (see the feedback-engine README).

The Release and MapRoulette workflows only create releases and issues in
this repository, so they use the default `GITHUB_TOKEN`.

### Variables and secrets

| Name | Kind | Used by |
|---|---|---|
| `OCDB_BOT_CLIENT_ID` | variable | OSM sync, Commons photos: the `opencampingdb-bot` client ID |
| `OCDB_BOT_PRIVATE_KEY` | secret | OSM sync, Commons photos: the `opencampingdb-bot` private key |
| `INTAKE_URL` | variable | Release: the intake to load the place index into. Without it the step is skipped |
| `INTAKE_PLACES_TOKEN` | secret | Release: the intake's `PLACES_TOKEN`. Without it the step is skipped |
| `NEUTRAL_TERMS` | organisation secret | Neutral: the names to look for, as an extended regular expression. Without it the check warns and passes |

### Settings

- **Allow auto-merge** in the repository settings.
- **Branch protection on `main`**: changes arrive by pull request only, and
  the status check `Tests, validate and build` is required. Without a
  required check, auto-merge has nothing to wait for.
- The labels `needs-review` (for the `feedback/review` PRs) and
  `format-change` (for the announcements in CONNECTING.md, section 1).
- The first OSM sync changes every record (100%), so its PR waits for a
  maintainer to merge it.
