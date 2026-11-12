# Connecting an app

Any app may use Open Camping DB and send feedback to it. This page covers
downloading the data, keeping it current, showing the attribution and
sending feedback.

## 1. Download

Fetch the manifest from the stable URL. GitHub answers with a redirect, so
follow redirects.

```
GET https://github.com/opencampingdb/data/releases/latest/download/manifest.json
```

```json
{
  "format_version": 12,
  "format_minor": 5,
  "layout_version": 1,
  "data_version": "20261030T160000Z-abc1234",
  "release": "data-20261030T160000Z",
  "generated": "2026-10-30T16:00:00Z",
  "places": {
    "file": "places.ndjson.gz",
    "url": "https://github.com/opencampingdb/data/releases/download/data-20261030T160000Z/places.ndjson.gz",
    "sha256": "...", "bytes": 1234567, "count": 54321
  },
  "reviews": {
    "file": "reviews.ndjson.gz",
    "url": "https://github.com/opencampingdb/data/releases/download/data-20261030T160000Z/reviews.ndjson.gz",
    "sha256": "...", "bytes": 2345, "count": 12
  },
  "fees": {
    "file": "fees.ndjson.gz",
    "url": "https://github.com/opencampingdb/data/releases/download/data-20261030T160000Z/fees.ndjson.gz",
    "sha256": "...", "bytes": 456, "count": 7
  },
  "merged": {
    "file": "merged.ndjson.gz",
    "url": "https://github.com/opencampingdb/data/releases/download/data-20261030T160000Z/merged.ndjson.gz",
    "sha256": "...", "bytes": 20, "count": 0
  },
  "commons": {
    "file": "commons.ndjson.gz",
    "url": "https://github.com/opencampingdb/data/releases/download/data-20261030T160000Z/commons.ndjson.gz",
    "sha256": "...", "bytes": 3456, "count": 25
  },
  "database": {
    "file": "ocdb.sqlite.gz",
    "url": "https://github.com/opencampingdb/data/releases/download/data-20261030T160000Z/ocdb.sqlite.gz",
    "sha256": "...", "bytes": 12227557
  },
  "changesets": [
    {
      "file": "changeset-20261030T090000Z-0a1b2c3.bin.gz",
      "url": "https://github.com/opencampingdb/data/releases/download/data-20261030T160000Z/changeset-20261030T090000Z-0a1b2c3.bin.gz",
      "sha256": "...", "bytes": 10948,
      "from": "20261030T090000Z-0a1b2c3", "to": "20261030T160000Z-abc1234", "day": false
    },
    {
      "file": "changeset-day-2026-10-29.bin.gz",
      "url": "https://github.com/opencampingdb/data/releases/download/data-20261030T160000Z/changeset-day-2026-10-29.bin.gz",
      "sha256": "...", "bytes": 104957,
      "from": "20261029T231500Z-9f8e7d6", "to": "20261030T160000Z-abc1234", "day": true
    }
  ],
  "licence": "ODbL-1.0",
  "attribution": "© OpenStreetMap contributors, Open Camping DB"
}
```

If `format_version` is higher than the one your app understands, keep the
data you have and stop: the record format has changed in a way your app
cannot read. Read any release whose `format_version` is the one your app
knows or lower, whatever its `format_minor`.

`format_version` rises only when a reader of the previous format would
misread the records: a field removed or renamed, a field whose meaning
changes, or a new value in an existing field (such as `fee` gaining
`"donation"` in format 7). Each rise is announced in an issue labelled
`format-change` on this repository at least 7 days before the first release
that carries it, so apps can ship a reader first. Watch the repository's
issues to hear about it.

`format_minor` rises for a change a reader of the same `format_version` can
read without updating: a new optional field, a new file in the manifest,
records left out of a release, or a new kind (see section 4). It goes back to
0 when `format_version` rises. To stay readable through these changes, ignore
fields, facts and manifest entries your app does not know. The schemas in
[schema/](schema/) reject unknown fields, which is right for checking this
repository but wrong for reading a release.

Format 1 had eight kinds (`camp_site`, `caravan_site`, `rest_area`,
`dump_station`, `drinking_water`, `toilets`, `shower`, `picnic_site`) and no
`group`. Format 2 uses the kinds listed in section 4 and adds `group`. Every
place kept its `id` across the change. Format 3 adds `osm.timestamp` and
`fact_dates` (see section 4). Records without them read the same as in
format 2. Format 4 adds `last_confirmed` (see section 4). Records without it
read the same as in format 3. Format 5 changes reviews: every line carries
`contributor` and may carry `visited_nearby`, and reviews a person or three
reports have hidden are left out (see section 4). Format 6 adds
`fees.ndjson.gz`, what travellers paid per night (see section 4). Places and
reviews read the same as in format 5. Format 7 widens three facts: `fee` may
be `"donation"`, `caravans` may be `"offroad"` and `pets` may be `"seasonal"`.
It adds 51 facts for site features and contacts (section 4), seven kinds
(`backpacker_hostel`, `roofed_accommodation`, `long_vehicle_parking`,
`food_and_drink`, `recreation_area`, `heritage` and `experience`) and a
`kind` entry in `fact_sources` for a kind set by feedback or the owner. An
app that reads `fee` as a plain boolean must handle `"donation"`. Format 9
drops `photos.ndjson.gz`, which format 8 had: Open Camping DB holds no photos.
Everything else reads the same as in format 7. Format 10 adds `standing` to
places travellers added (see section 4) and leaves out those that are `held`
or `hidden`. Records without it read the same as in format 9. Format 11 adds
the source `community_report` to `fact_sources` (see section 4): an app that
expects only the sources of format 10 must handle it. Everything else reads
the same as in format 10. Formats 3, 4, 6, 9 and 10 were additive and would
each be a `format_minor` rise under the rule above. `format_minor` starts at 0
in format 10, and is 0 again in format 11. Format 11.1 adds
`commons.ndjson.gz`, Wikimedia Commons photos matched to places to stay (see
section 4). Everything else reads the same as in 11.0. Format 12 adds the
source `rule` to `fact_sources`, with `fact_rules` naming the rule (see
section 4): a fact a documented rule sets from the place's kind, such as
`toilets: true` on a `public_toilet`, where no source says otherwise. An app
that expects only the sources of format 11 must handle it, and should show a
rule fact as what is usual, not as something anyone reported. Format 12
also adds the facts `pets_months`, the months a seasonal place allows dogs,
and `vehicle_access`, for places only heavy vehicles may use (section 4).
Everything else reads the same as in 11.1. Format 12.2 adds
`ocdb.sqlite.gz`, the release as an SQLite database, the `changesets` that
update the databases of the last 50 releases to it, and `layout_version`
(section 3).
The other files read the same as in 12.1. Format 12.3 adds the facts
`nearest_beach`, `sharks` and `marine_stingers` (section 4). Format 12.4
adds the facts `fuels`, `air`, `hot_water`, `toilet_type` and `operator`,
from OSM, and fills `bins` and `booking_required` from OSM too (section 4).
It also fills `fishing`, `phone_2` and `booking_url` from OSM, and sets
`dump_station` on a place to stay whose dump station is for guests only.
It adds `nearest_boat_ramp`, and fills `pub`, `fuel` and `car_wash` from
OSM objects inside a place (section 4). Format 12.5 adds the fact
`nearest_major_road`, the distance to the nearest major OSM road within
500 m (section 4). The traveller fact `near_road` reads the same as before.
It also fills `motorbikes` from OSM, where it means motorbikes may enter,
and retires `seasonal_closure`: its existing values stay, and no new ones
are taken (section 4).

Then fetch each file from its `url` field. Each `url` points at the file in
the release the manifest belongs to, and a release's files never change, so
the files always match the manifest even if a newer release is published
while you download. Do not build file URLs yourself from the stable
`latest/download/` address.

## 2. Check the files

Before using a file, check that its length equals `bytes` and that the
SHA-256 of the downloaded (still compressed) bytes, as lowercase hex, equals
`sha256`. If either differs, throw the download away, keep the data you
already have and try again later. Because the files come from the `url`
fields, a mismatch means a damaged download, not a newer release, so a
mismatch that repeats should be reported (see section 6).

Then decompress with gzip and read one JSON object per line, as UTF-8.

```sh
curl -fsSL -o manifest.json https://github.com/opencampingdb/data/releases/latest/download/manifest.json
curl -fsSL -o places.ndjson.gz "$(python3 -c 'import json; print(json.load(open("manifest.json"))["places"]["url"])')"
sha256sum places.ndjson.gz   # compare with places.sha256 in manifest.json
```

## 3. Stay current

Store the `data_version` of the data you have. To update, fetch the manifest
and compare its `data_version` with yours. If they are equal, you are current
and need nothing else. If they differ, download and check the files as above
and swap them in only once both checks pass, so a failed update never leaves
you with half a dataset.

### Redirect merged places

Sometimes one place is recorded twice, and the two records are merged into
one, by a person or by a documented rule (the same name, under 50 m apart
and no major road between) whose merges a person reviews. The record that is dropped leaves `places.ndjson.gz`, and its id is
listed in `merged.ndjson.gz` (the `merged` entry in the manifest), one line
per merged id:

```json
{"id":"ocdb_01M66H48SBTFGJFH6HPDK5WAF8","merged_into":"ocdb_01M66H48SBTFGJFH6HPDK5WAF7"}
```

If your app stored an id (a favourite, a note, a trip) that no longer appears
in `places.ndjson.gz`, look it up in `merged.ndjson.gz` and move what you
stored to `merged_into`. A redirect always takes one step: `merged_into` is
never itself merged. The kept place also carries the merged place's reviews
and rating. An id found in neither file has been removed, which only a
person does, for example on a takedown request.

Treat `data_version` as an opaque string and compare it only for equality.
Checking once a day is plenty: the OSM sync runs once a night and a new
release is published at most once an hour. Do not poll more often than once
an hour. Only the newest 60 releases are kept, so do not store file URLs for
later: always start again from the manifest.

### The SQLite database

Each release also comes as an SQLite database, `ocdb.sqlite.gz` (the
`database` entry in the manifest), so an app can query the release directly
instead of importing the NDJSON files. It holds the same release as those
files, plus each place's state. Check its `bytes` and `sha256` as in section
2, then decompress it with gzip to get an SQLite 3 database file (page size
4096, rollback journal).

Every table has a declared primary key, and each holds what one NDJSON file
holds. A `record` column holds the line exactly as it is in the NDJSON file,
so a field without a column of its own is still there to read.

`release`, one row:

| Column | Type | Value |
|---|---|---|
| `id` | INTEGER, primary key | always 1 |
| `data_version` | TEXT | as in the manifest |
| `release` | TEXT | the release tag, as in the manifest |
| `generated` | TEXT | as in the manifest |
| `format_version` | INTEGER | as in the manifest |
| `format_minor` | INTEGER | as in the manifest |
| `layout_version` | INTEGER | the version of these tables (below) |
| `licence` | TEXT | as in the manifest |
| `attribution` | TEXT | as in the manifest, to show as section 5 says |

`places`, one row per line of `places.ndjson.gz`:

| Column | Type | Value |
|---|---|---|
| `id` | TEXT, primary key | `id` |
| `name` | TEXT | `name`, or NULL |
| `kind` | TEXT | `kind` |
| `group` | TEXT | `group` (quote the name in SQL: `"group"`) |
| `lat` | REAL | `location.lat` |
| `lon` | REAL | `location.lon` |
| `status` | TEXT | `status` |
| `state` | TEXT | the Australian state or territory the place is in, or NULL (below) |
| `standing` | TEXT | `standing`, or NULL |
| `rating_count` | INTEGER | `rating.count`, or NULL when `rating` is null |
| `rating_mean` | REAL | `rating.mean`, or NULL when `rating` is null |
| `updated` | TEXT | `updated` |
| `record` | TEXT | the whole record as JSON |

`state` is the state or territory the place is in, from OpenStreetMap's
`boundary=administrative`, `admin_level=4` outlines in Australia, or NULL
for a place outside all of them. The code is the outline's `ref` tag in OSM:

| Code | State or territory |
|---|---|
| `NSW` | New South Wales (with Lord Howe Island) |
| `VIC` | Victoria |
| `QLD` | Queensland |
| `SA` | South Australia |
| `WA` | Western Australia |
| `TAS` | Tasmania |
| `NT` | Northern Territory |
| `ACT` | Australian Capital Territory |
| `JBT` | Jervis Bay Territory |
| `CX` | Christmas Island |
| `CC` | Cocos (Keeling) Islands |
| `HIMI` | Heard Island and McDonald Islands |
| `NF` | Norfolk Island (no `ref` in OSM: its `ISO3166-1` tag) |
| `CR` | Coral Sea Islands (no `ref` or ISO code in OSM: its FIPS 10-4 code) |
| `AT` | Ashmore and Cartier Islands (no `ref` or ISO code in OSM: its FIPS 10-4 code) |

OSM's outlines take in the coastal waters, so places on beaches, jetties and
near-shore islands have their state. A place out on a reef, such as a
pontoon on the outer Great Barrier Reef, is outside every outline and has
NULL.

`merged`, one row per line of `merged.ndjson.gz`: `id` (TEXT, primary key)
and `merged_into` (TEXT).

`reviews`, one row per line of `reviews.ndjson.gz`: `id` (TEXT, primary key),
`place` (TEXT), `stars` (INTEGER), `visited` (TEXT), `created` (TEXT) and
`record` (TEXT, the whole review as JSON).

`fees`, one row per line of `fees.ndjson.gz`: `id` (TEXT, primary key),
`place` (TEXT), `amount` (REAL), `site` (TEXT), `paid` (TEXT) and `record`
(TEXT, the whole fee as JSON).

`commons`, one row per line of `commons.ndjson.gz`: `place` (TEXT, primary
key) and `files` (TEXT, the `files` array as JSON).

The indexes are `places_kind` on `places (kind)`, `places_group` on
`places ("group")`, `places_state` on `places (state)`, `places_lat_lon` on
`places (lat, lon)`, `reviews_place` on `reviews (place)` and `fees_place` on
`fees (place)`. For a map area, filter on `lat BETWEEN ? AND ? AND lon BETWEEN
? AND ?`. An app that needs an R*Tree can build one from `places` in a table
of its own.

`layout_version` (in the manifest, in the `release` table and as `PRAGMA
user_version`) is the version of these tables, columns and indexes. It starts
at 1 and rises whenever any of them changes. If the manifest's `layout_version` is one
your app cannot read, keep the database you have.

Treat the release tables as read only: a changeset (below) expects them to
be exactly as the release left them. Keep your app's own data, such as places
a traveller added or edits laid over released places, in tables of your own
or in an attached database, and read both together through a view. Add no
triggers to the release tables.

### Changesets

The `changesets` entry in the manifest lists every changeset into each of
the newest 50 releases, ordered by `from`, so the newest manifest alone gives
the path from any of those releases to the newest one. Each entry has:

- `from` and `to`, the `data_version` of the release it starts from and of
  the release it gives.
- `file`, `url`, `sha256` and `bytes`. The `url` points at the release that
  made the changeset (the one tagged for `to`), so it stays valid for as long
  as that release is kept.
- `day`, `true` for a daily changeset (below).

Each file is a gzipped changeset in SQLite's standard session extension
format, which `sqlite3changeset_apply` applies in any SQLite built with the
session extension (`SQLITE_ENABLE_SESSION` and
`SQLITE_ENABLE_PREUPDATE_HOOK`). Applied to the database of its `from`
release, a changeset gives exactly the rows of its `to` release in every
table. The release build applies each one and compares every table before
publishing it.

Each release makes at most two changesets into itself:

- `changeset-<data_version>.bin.gz`, from the release just before.
- `changeset-day-<YYYY-MM-DD>.bin.gz`, with `day: true`, from the last
  release of the previous UTC day (the date in the name is that day), when
  that is not the release just before. A phone that was current yesterday
  catches up with one file.

The list starts again, empty, when there is no earlier database to start
from: the first release with a database, and the first release after a
`layout_version` rise.

To update the database you have:

1. Read your `data_version` from the `release` table, and fetch the newest
   manifest (section 1). If the data versions are equal, stop.
2. If the manifest's `layout_version` is not the one your database has,
   download the whole database, if your app can read that layout.
3. Find a path in `changesets` from your `data_version` to the manifest's:
   start from the entries whose `from` is yours and follow `to` into `from`.
   Prefer the path with the fewest files, which uses the daily changesets.
   If there is none, because your release is older than the list, download
   the whole database.
4. Download each changeset on the path, check its `bytes` and `sha256`, and
   apply them in order with `sqlite3changeset_apply`, aborting on any
   conflict, in one transaction.

A database updated by changesets holds the same rows as the newest
`ocdb.sqlite.gz`, but not the same bytes, so check it by its `data_version`,
not by a file hash. If a download, a check or the apply fails, keep the
database you had (the transaction leaves it unchanged) and try again later.

In JavaScript, TypeScript or React Native, the feedback SDK's
`createReleaseUpdater` does these steps for you: it finds the path, checks
every file, applies the chain in one transaction through four calls you write
over your SQLite library, falls back to the whole database, and says why when
an update fails. See
[Keeping the data current](https://github.com/opencampingdb/feedback-engine/blob/main/docs/integration.md#10-keeping-the-data-current)
in the integration guide.

## 4. Use the records

The schemas in [schema/](schema/) are the contract. Some points to keep in mind:

- `id` is stable. Use it to link your own data (favourites, notes) to a place.
  It is never reused.
- Every fact is optional. A missing fact means "not known", not "no".
- `status` is `open`, `reported_closed` (at least one user said it has
  closed, not yet confirmed), `temporarily_closed` (set by the owner, often
  with a `reopens` date, YYYY-MM-DD) or `closed`. (`merged` exists only in the
  repository, on tombstones, and never appears in `places.ndjson.gz`.) Show `reported_closed` as a warning
  rather than hiding the place.
- `rating` is `null` until a place has accepted reviews. It counts only the
  reviews in `reviews.ndjson.gz`.
- Each line of `reviews.ndjson.gz` has a stable `id` (`rev_<ulid>`), the
  `place`, `stars`, `text`, `visited` (YYYY-MM), `created`, the `app` that
  sent it and `contributor`, an opaque id (`ctr_` and 16 hex characters) of
  the account or attested install that wrote it. It is never a name or email,
  and it is the same on every review from one contributor through one app, so
  your app can let a traveller hide everything from one contributor on their
  own phone. `visited_nearby: true` means the app saw the phone near the
  place in the 60 days before the review. One contributor has at most one
  review per place: a newer one replaces the older. Reviews hidden after
  reports and reviews from blocked contributors are left out of the release.
  Let travellers report a review (see section 6).
- Each line of `fees.ndjson.gz` is what a traveller paid for one night: a
  stable `id` (`fee_<ulid>`), the `place`, `amount` (Australian dollars per
  night, at most 2 decimal places), `site` (`unpowered`, `powered` or
  `cabin`), `paid` (the month, YYYY-MM), `created`, the `app` that sent it
  and `contributor`, as on reviews. One contributor has at most one fee per
  place per site type: a newer one replaces the older. The release holds
  fees paid in the 24 months before it was built, and none from blocked
  contributors. Show recent fees with their month, since prices change.
- Each line of `commons.ndjson.gz` links one place to stay (group
  `overnight`) with up to 5 photos on Wikimedia Commons, best first:
  `{"place", "files"}`. Open Camping DB holds no photos: the files stay on
  Commons, each under its own licence. Each file has `file` (its Commons name,
  without `File:`), `width` and `height` of the original, `thumb` (a 960 px
  wide standard thumbnail) and `full` (1920 px, or the original when it is
  smaller), `page` (the file's Commons page), `licence` (for example
  `CC BY-SA 4.0` or `CC0`), `licence_url` when the licence has one, `credit`
  (the author, plain text; absent only for public domain and CC0 files),
  `match` and `distance_m` (from the place). `match` says how sure the match
  is, from the file's title: `name` (the place's name with a camping word, or
  its whole name, within 2 km), `camping` (a camping word within 250 m, only
  for a place with no distinctive name, such as "Campsite 10") or `area` (the
  place's name without a camping word, within 500 m: often a view of the
  surroundings rather than the place). Show `area` photos as the
  surroundings, or not at all. Photos of plants and animals and historic
  photos are left out. A photo travellers reported as not of the place is
  listed in `moderation/commons-do-not-match.json` and never matched to it
  again. Only licences that allow any use with credit
  are kept (CC0, public domain, CC BY and CC BY-SA). Photos are matched again
  every 30 days, so a file deleted from Commons drops out. Show the credit
  beside each photo (section 5) and load only `thumb` and `full`, which
  Wikimedia's robot policy asks for. Set a descriptive `User-Agent` on the
  requests, as the Wikimedia User-Agent policy asks.
- `osm` is `null` for places that did not come from OpenStreetMap: places
  contributed directly to Open Camping DB, and new places from feedback.
- `standing` is only on a place a traveller added in an app. `shown` means
  nobody has checked it yet: show it with a "not checked yet" label and let
  travellers leave such places out. `confirmed` means two travellers near it
  have found it there. The release leaves out new places that are `held` for
  a person or `hidden` after reports, and a place its adder withdrew is
  deleted. A place without `standing` is from OpenStreetMap, contributed or
  checked by a person. New places reach a release some time after they are added; to
  show them sooner, read the recent places list (section 6).
- `moderator` is only on a place a traveller added: a person's `restore` or
  `hide`, with the time, which wins over the reports and checks before it.
  Apps need not read it: `standing` already reflects it.
- `fact_sources` says where each fact came from: `osm`, `feedback` (from apps),
  `contributed` (given directly to Open Camping DB under the ODbL), `owner`
  (the place's verified owner), `community_report` (anonymous community
  reports from public camping groups, imported by a person; see below) or
  `rule` (set by a documented rule, not reported by anyone; see below). It may
  also have `name`, `location`, `kind` and `status` entries. When `name`,
  `location` or `kind` is missing, the name, position and kind come from OSM. A place with
  `osm: null` and `contributed` name and location is a contributed place, and
  one with `community_report` name and location was found only in community
  reports. A `status` entry says who closed or reopened the place.
- `community_report` values come from what travellers wrote in public
  camping groups, without their names. Only facts are kept. They are second
  hand and may be old, so show their date: in `fact_dates` it is the first
  day of the month the report was made (a report from "around March 2026"
  is `2026-03-01T00:00:00Z`), not when it was imported. A place
  found only in community reports is located at a specific mapped feature,
  never a town centre. These values are never passed to OpenStreetMap.
- `rule` values are what a place of that kind usually has, set by the
  release where no source gives a value. `fact_rules` names the rule for
  each one, and [schema/fact_rules.json](schema/fact_rules.json) lists them:
  `kind:public_toilet`, `kind:drinking_water_point`, `kind:public_shower` and
  `kind:dump_point` set the place's own feature (`toilets`,
  `drinking_water`, `showers` and `dump_station`, all `true`), and
  `national_park` sets `pets: "no"` on a `national_park_campsite`, since
  most Australian national parks allow no dogs. A `reserve_campsite` has no
  such rule: regional parks, game reserves and river reserves often allow
  dogs. Any value from OSM, a
  traveller, a contributor or the owner wins over a rule, `false` included,
  so a single park that allows dogs says so. Present a rule fact as
  "usually": for example "Usually no dogs (national park)", never as a firm
  fact. A rule value has no `fact_dates` entry, and is never passed to
  OpenStreetMap. A report that confirms or corrects it is welcome (section 6).
- How fresh a place's information is: `osm.timestamp` is when its OSM object
  was last edited by anyone, in UTC. `fact_dates` gives, for each value that
  did not come from OSM (each `fact_sources` entry that is `feedback`,
  `contributed`, `owner` or `community_report`), when it was accepted, or for
  `community_report` when it was reported. The newest of these is
  when someone last said something about the place. `updated` is when the
  record's content last changed in Open Camping DB, which is not the same:
  it moves when OSM data is refreshed, whatever its age. Older records may
  lack `osm.timestamp` until the next OSM sync.
- `last_confirmed` is when a traveller last checked the place in an app and
  found it there, in UTC. It is the time the check was accepted, usually
  within the hour after it was sent. One traveller's check is enough to set it, and it
  is only set while the place is open: if `status` changed later, the date
  in `fact_dates.status` is newer. It does not move `updated`, so it can be
  later than `updated`. Places nobody has checked have no `last_confirmed`.
  Show it as "Confirmed by a traveller" with its date, apart from the
  freshness of the information, because it says the place exists, not that
  its facts are right.
- Site features are true or false facts: who can get in (`wheelchair`,
  `motorhomes`, `camper_trailers`, `motorbikes`, `big_rigs`, `generators`),
  what is there (`non_drinking_water`, `laundry`, `kitchen`, `bbq`,
  `drive_through`, `ensuite_sites`, `bins`, `picnic_tables`, `campfires`,
  `cabins`, `pub`), what to do (`swimming`, `fishing`, `walking_tracks`,
  `scenery`, `wildlife`, `playground`, `games_room`, `shade`,
  `cycling_tracks`, `offroad_tracks`, `boat_ramp_nearby`, `near_road`),
  services (`telstra_signal`, `mobile_signal`, `wifi`, `tv_reception`,
  `pet_sitting`, `fuel`, `lpg`, `car_wash`, `vehicle_storage`) and warnings
  (`self_contained_only`, `crocodiles`, `sharks`, `marine_stingers`,
  `seasonal_closure` (retired), `dangerous_when_wet`, `over_50s`, `nudist`). `brand` is the park chain or
  club, `operator` is who runs the place (a council, a parks agency, a
  company), `fuels` lists the fuels sold, `air` is air for tyres,
  `hot_water` is hot water in the showers or basins, `toilet_type` is
  `flush`, `pit`, `chemical` or `dry`, `address` is the street address (an object with `unit`,
  `housenumber`, `street` or a rural `place`, `suburb`, `state` and
  `postcode`, each there only when known), `email` and `phone_2` are contacts, and `facebook`, `instagram`,
  `tiktok` and `x` are https links. [schema/place.schema.json](schema/place.schema.json)
  describes each one.
- `vehicle_access` says who may use a place when only some vehicles may:
  `heavy_vehicles_only` means only heavy vehicles (trucks) may stop there,
  and others can be fined. Show it plainly on the place, and consider
  leaving such places out for travellers in cars, vans and caravans. It
  comes from OSM tags (`hgv=designated` with `motorcar=no`, or `access=no`
  with `hgv=yes`), or from a fact report a person or reviewer accepts. More
  values may be added later, so treat an unknown value as a restriction you
  can't explain rather than ignoring it.
- `dump_station: true` on a place to stay can be a dump station for its
  guests only (OSM `sanitary_dump_station=customers`). A dump point or any
  other place whose dump station is for customers only has no
  `dump_station` from OSM, since it is not open to everyone.
- `fuels` lists the fuels a place sells, in this order: `diesel`,
  `unleaded_91`, `e10`, `unleaded_95`, `unleaded_98`, `e85`, `adblue` and
  `lpg`. Its `lpg` is LPG at the pump (OSM `fuel:lpg`). The `lpg` fact is
  different: it says gas bottles are refilled or swapped there, and comes
  only from travellers and owners. `big_rigs` also comes only from
  travellers and owners: it means a rig 10 m or longer can get in and turn
  around, which OSM's `hgv` access tag does not say.
- `motorbikes` says whether motorbikes may enter: a permission, not whether
  the way in suits one. It comes from OSM `motorcycle` (`yes`, `designated`
  or `permissive` is `true`, `no` is `false`), or from `motor_vehicle` the
  same way when `motorcycle` is absent, so a walk-in site with
  `motor_vehicle=no` has `false`.
- `seasonal_closure` is retired: the open months in `opening_hours` replace
  it. Values already in the data stay valid and can still be read, but no
  new ones are taken. Read `opening_hours` for when a place is open.
- `nearest_beach` is the nearest OSM beach (`natural=beach`) within 5 km of
  the place: `distance_m`, from the place's location to the nearest point of
  the beach, 0 inside it and rounded to 10 m, and the beach's `name` when it
  has one. It is worked out from OSM at each sync, so its source is always
  `osm` and fact reports cannot set it. A place with no beach within 5 km
  has none. Swimming holes and swimming areas in rivers, creeks and lakes are
  places of their own (kind `swimming_spot`): find the ones near a place by
  their location.
- `nearest_boat_ramp` is the nearest OSM boat ramp (`leisure=slipway`)
  within 5 km of the place, read and rounded the same way as
  `nearest_beach`: `distance_m` to the nearest point of the ramp, and the
  ramp's `name` when it has one. A boat ramp is a place too (kind
  `boat_ramp`), and its own `nearest_boat_ramp` is the next ramp, never
  itself. Its source is always `osm` and fact reports cannot set it. The
  traveller fact `boat_ramp_nearby` is separate and unchanged.
- `nearest_major_road` is the nearest major OSM road within 500 m of the
  place: a `highway=motorway`, `trunk`, `primary` or `secondary` road, their
  `_link` roads, or a one-way `tertiary` road (one carriageway of a divided
  road). `distance_m` is to the nearest point of the road's line, rounded to
  10 m, and `name` is the road's name, or its `ref` (such as `A1`) when it
  has no name. A place with no major road within 500 m has none. Its source
  is always `osm` and fact reports cannot set it. The traveller fact
  `near_road`, that road noise carries, is separate and unchanged: a road
  can be close and quiet, or further away and still heard.
- `pub`, `fuel` and `car_wash` are also `true` from OSM when an
  `amenity=pub`, `amenity=fuel` or `amenity=car_wash` lies inside the
  place's area, such as a pub inside a caravan park. A place mapped as a
  single point has no area, so it gets these only from fact reports. OSM
  never sets them to `false`.
- `crocodiles`, `sharks` and `marine_stingers` warn about dangerous animals
  in the water nearby. `marine_stingers` covers box jellyfish, Irukandji and
  other dangerous jellyfish, which come in season in tropical waters.
  `crocodiles` also comes from OSM when the place itself, or a hazard sign
  within 1 km, carries `hazard=crocodile` (or `crocodiles`) or
  `hazard:animal=crocodile`. OSM has no tag in use for sharks or jellyfish,
  so those two come only from fact reports. A missing warning does not mean
  the water is safe: show these as what someone reported, never their
  absence as an all clear.
- `pets_months` goes only with `pets: "seasonal"`: the months dogs are
  allowed, as one OpenStreetMap month range of two different months, such
  as `May-Sep` or `Dec-Feb` (a range may wrap the year end). A single month
  or a list of ranges is not used. It is OSM's
  `dog:conditional=yes @ (May-Sep)`. A seasonal place without it allows dogs
  in some months that nobody has given yet. A fact report may set it, with a
  value in the same form, or clear it.
- `access` is the vehicle needed on the road in (`2wd`, `awd`, `4wd`).
  `entry` is who may stay (`public`, `customers`, `permit`, `private`).
  Consider leaving `private` places out of what you show.
- Records gone from OpenStreetMap are kept until a person reviews them, so a
  place may linger for a while after it has really closed.

### Owners and private property

A place's owner can claim it through the owner page at
`https://opencampingdb.github.io/owners/`. Once a person has approved the
claim, the place has `owner_verified: true`, and the owner can update its
facts, phone, website, `booking_url`, a short `description` and a temporary
closure directly. Those values have the source `owner` in `fact_sources`.
An app can offer a "Do you run this place?" button that opens the owner page
with the place's id.

- `description` is plain text from the owner, at most 500 characters,
  moderated like reviews. Show it as the owner's words.
- `booking_url` (always https) and `booking_required` say how and whether to
  book. Either can also come from OSM (`booking_url` from `website:booking`,
  then `reservation:website`), with the source `osm`. An owner's value
  always wins: sync never replaces a value whose source is `owner`.
- `farm_stay` is private land that its owner has listed. Open Camping DB
  never lists private land unless its owner adds it. A farm stay always has
  `osm: null`, `entry: "private"`, `booking_required: true`, and a
  `booking_url` or a `phone`. Show it as private property and tell people to
  book first, in these words or close to them: "Private property. Book with
  the owner first." Do not show a farm stay as a free camp or a public
  place, and do not offer directions without the booking link or phone next
  to them.
- Any place with `entry: "private"` is private property. Label it the same
  way.

### Kinds and groups

`kind` says what a place is and `group` says which family it belongs to:
`overnight`, `utility`, `services`, `attraction`, `emergency` or `transport`.
Filter on `group` to show, for example, only places to stay. The kinds are the
site types of the OTWD (Open Touring and Waypoint Database) vocabulary, plus
the Open Camping DB extensions at the end of the table. [schema/kinds.json](schema/kinds.json) holds
the same list in machine-readable form, and new kinds may be added in later
versions, so show an unknown kind under its group rather than dropping it.

| `kind` | Label | `group` |
|---|---|---|
| `free_camp` | Free camp | overnight |
| `paid_campground` | Paid campground | overnight |
| `caravan_park` | Caravan park | overnight |
| `national_park_campsite` | National park campsite | overnight |
| `state_forest_campsite` | State forest campsite | overnight |
| `reserve_campsite` | Reserve campsite (protected area, not a national park) | overnight |
| `showground` | Showground | overnight |
| `campground` | Campground (fee not known) | overnight |
| `farm_stay` | Farm stay (private, book first) | overnight |
| `backpacker_hostel` | Backpacker hostel | overnight |
| `roofed_accommodation` | Roofed accommodation (cabins, motels) | overnight |
| `rest_area` | Rest area | utility |
| `dump_point` | Dump point | utility |
| `drinking_water_point` | Drinking water point | utility |
| `public_toilet` | Public toilet | utility |
| `public_shower` | Public shower | utility |
| `laundromat` | Laundromat | utility |
| `picnic_site` | Picnic area | utility |
| `long_vehicle_parking` | Long vehicle parking | utility |
| `information_centre` | Information centre | services |
| `fuel` | Fuel | services |
| `lpg_refill_swap` | LPG refill or swap | services |
| `mechanic` | Mechanic | services |
| `food_and_drink` | Food and drink | services |
| `attraction` | Attraction | attraction |
| `lookout` | Lookout | attraction |
| `swimming_spot` | Swimming spot | attraction |
| `boat_ramp` | Boat ramp | attraction |
| `recreation_area` | Recreation area (day use) | attraction |
| `heritage` | Arts, history and culture | attraction |
| `experience` | Experience (tours, tastings) | attraction |
| `hospital_emergency` | Hospital (emergency) | emergency |
| `ferry_terminal` | Ferry terminal | transport |

How each kind is derived from OpenStreetMap is described in
[pipeline/README.md](pipeline/README.md). A `drinking_water_point` may carry
the fact `water_type`: `tank_fill` for a tap suited to filling tanks, or
`fountain` for a drinking fountain, which often is not.

### Mapped facts and extensions

Every fact in [schema/place.schema.json](schema/place.schema.json) carries
`x-ocdb-class`, so your app can label it honestly:

- `mapped`: the dataset can fill it from OpenStreetMap. `x-osm` names the
  tag, or the OSM objects a nearby fact comes from (`nearest_beach` from
  `natural=beach`, `pub` from an `amenity=pub` inside the place's area). A
  mapped fact can still come from a traveller for one place: `fact_sources`
  says where each value came from.
- `proposable`: an extension a mapper could check true or false at the
  place. OSM has no tag for it yet, so the value only comes from reports.
  `x-osm` is the form a tag could take, a suggestion for whoever proposes
  one, not a tag in use.
- `ocdb_only`: an opinion or a personal account, which OSM can never hold.
- `retired`: no new values are taken, and values already in the data stay
  valid and readable. `x-osm` names where the information now lives, and
  the fact also carries JSON Schema's `"deprecated": true`.
  `seasonal_closure` is retired in favour of the open months in
  `opening_hours`.

A value of a mapped fact that OSM has no value for is listed under
`x-ocdb-extension-values` on that fact, with its own class. JSON Schema
validators ignore all three keys.

Proposable extensions, with the OSM form each could take:

| Fact | OSM form |
|---|---|
| `generators` | `generators=yes/no` |
| `camper_trailers` | `camper_trailer=yes/no` |
| `big_rigs` | `big_rigs=yes/no` |
| `non_drinking_water` | `non_drinking_water=yes/no` |
| `drive_through` | `drive_through_sites=yes/no` |
| `ensuite_sites` | `ensuite=yes/no` |
| `swimming` | `swimming=yes/no` |
| `walking_tracks` | `walking_tracks=yes/no` |
| `games_room` | `games_room=yes/no` |
| `shade` | `shade=yes/no` |
| `cycling_tracks` | `cycling_tracks=yes/no` |
| `offroad_tracks` | `4wd_tracks=yes/no` |
| `boat_ramp_nearby` | `boat_ramp_nearby=yes/no` |
| `telstra_signal` | `mobile_signal:telstra=yes/no` |
| `mobile_signal` | `mobile_signal=yes/no` |
| `tv_reception` | `tv_reception=yes/no` |
| `pet_sitting` | `pet_sitting=yes/no` |
| `lpg` | `lpg_bottles=yes/no` |
| `vehicle_storage` | `vehicle_storage=yes/no` |
| `self_contained_only` | `self_contained_vehicles=only` |
| `sharks` | `hazard=shark` on a warning sign or beach, read within 1 km |
| `marine_stingers` | `hazard=jellyfish` on a warning sign or beach, read within 1 km |
| `dangerous_when_wet` | `dangerous_when_wet=yes/no` |
| `caravans: "offroad"` (a value) | `caravans=offroad` |

Open Camping DB only: `scenery`, `wildlife`, `rating` and reviews, and the
`access` value `unknown`.

`pets: "seasonal"` is the dataset's own value, but it is mapped: it is read
from and written as `dog:conditional=yes @ (<pets_months>)`.

No tag is proposed to OpenStreetMap by the pipeline. A person picks an
extension, checks that no tag in use covers it, and decides whether to
document or propose it.

## 5. Show the attribution

Every screen that shows this data, including a map, a list or a place page,
must show:

```
© OpenStreetMap contributors, Open Camping DB
```

It must be visible without the user going looking for it, for example in the
map corner or at the foot of a place page, and it should link to
`https://www.openstreetmap.org/copyright` and
`https://github.com/opencampingdb/data`.

The data is under the ODbL 1.0 with contents under the DbCL 1.0 (see
[LICENSE](LICENSE), [LICENSE-CONTENTS](LICENSE-CONTENTS) and
[NOTICE](NOTICE)). If you publicly use or share a database adapted from this
one, for example merged with your own places, that database must also be
offered under the ODbL.

A Commons photo from `commons.ndjson.gz` carries its own credit as well:
show `credit` and `licence` beside or on the photo, link `licence_url` and
`page`, and say if you changed the photo beyond resizing it, for example
`Photo: Jane Doe, CC BY-SA 4.0, via Wikimedia Commons`. A CC BY-SA photo you
crop or edit stays under CC BY-SA. Don't use Wikimedia's logos or suggest
that Wikimedia endorses your app.

## 6. Send feedback

Apps send what their users report to the Open Camping DB intake. The
[feedback-engine](https://github.com/opencampingdb/feedback-engine)
repository holds a client SDK for this and the intake itself. For an app
key, the intake host, or help connecting your app, open an issue with the
**App key** form in
[feedback-engine](https://github.com/opencampingdb/feedback-engine/issues/new/choose).

```
POST https://<intake host>/v1/feedback
X-App-Key: <your app key>
Content-Type: application/json
```

```json
{
  "type": "report",
  "place": "ocdb_01M66CKP2X55A8CVGR95ZFJ170",
  "device": "6f1c2c1e-6c4b-4f0e-9a39-2a1f5d6c2b7e",
  "app_version": "1.0.0",
  "consent": true,
  "report": {"kind": "fact", "fact": "power", "value": true, "note": "Powered sites near the gate"}
}
```

- `type` is `report`, `review`, `check`, `review_report`, `fee` or
  `withdraw`, with a matching `report`, `review`, `check`, `review_report` or
  `fee` object (`withdraw` has none).
- `place` is the record `id`, or `null` for a new place, in which case
  `location` (`{"lat": ..., "lon": ...}`) is required and `report.kind` is
  `new_place`.
- `report.kind` is `closed`, `reopened`, `fact`, `wrong_location`,
  `new_place`, `other`, `name`, `description`, `kind` or `remove`. A `fact` report also
  carries `fact` (a key from the place schema, except `booking_url`) and
  `value`. A `name` report carries the new name in `value` (1 to 300
  characters), a `description` report the text in `value` (at most 500), and
  a `kind` report a code from [schema/kinds.json](schema/kinds.json) in
  `place_kind`. Names and descriptions must not hold an email address, phone
  number or link. A `new_place` report may carry `name` (1 to 300
  characters, checked as a `name` report's value is), `place_kind` and
  `contacts`, with any of `phone`, `website` and `email`, kept out of the
  note. A `remove` report says a new place should come down, with `reason`
  `not_there`, `fake`, `offensive`, `private_land` or `duplicate`.
- A new place's id is known once its report is accepted: `ocdb_` and the
  `fb_<ulid>` the intake answers with, without its `fb_`. A `withdraw` with
  that id as `place`, from the same install, takes the place back within 24
  hours. After 24 hours it counts as one more `remove` report.
  The id comes back only in the answer to the send that carried the report.
  A report queued offline learns it when the queue sends it, so keep what
  depends on the new place (edits, photos) until that answer arrives. With the
  SDK, give the report a `ref` of your own and read the id from `onAnswered`.
- Submissions are normalised before they are queued or sent: a location is
  rounded to 6 decimals, and names and text lose the spaces at their ends.
  An app that keeps its own copy of what it sent must compare against the
  normalised values, never against what it passed in. The SDK exports the
  rounding as `sdkCoordinate`, and `ref` with `onAnswered` connects an answer
  to its submission without comparing content at all.
- `review` is `{"stars": 1-5, "text": "...", "visited": "YYYY-MM"}`, with
  `"visited_nearby": true` when the app saw the phone near the place in the
  last 60 days (never send the location itself). Review text is published,
  so it must not hold names, email addresses, phone numbers, links or
  advertising. Reviews that do are refused.
- `review_report` is `{"review": "rev_<ulid>", "reason": "..."}`, a
  traveller's report of a published review. `reason` is `spam_ad`,
  `offensive`, `off_topic`, `fake` or `personal_details`. `place` is the
  review's place.
- `fee` is `{"amount": 32.5, "site": "powered", "paid": "YYYY-MM"}`, what
  the traveller paid per night in Australian dollars (more than 0, at most
  1000, at most 2 decimal places), for an `unpowered` or `powered` site or a
  `cabin`, in a month that is not in the future and not more than 24 months
  ago.
- `contributor` is an opaque id for the traveller's account (a keyed hash,
  never a name or email), 16 to 128 characters of letters, digits, `_` and
  `-`. Reviews, review reports and fees need it, unless the app key proves the
  install with App Attest instead. The intake hashes it again before storing
  it. The feedback engine's
  [integration guide](https://github.com/opencampingdb/feedback-engine/blob/main/docs/integration.md)
  covers it, and the App Attest and Play Integrity headers an app key may
  require.
- `check` is `{"visited": "YYYY-MM-DD", "still_there": true}`, a confirmation
  that a place is as described, with `"nearby": true` when the app saw the
  phone near the place (never send the location itself).
- `device` is a random UUID your app creates once per install. Never send a
  hardware ID, advertising ID, account ID, name or email. The intake hashes
  `device` before storing it, and nothing that identifies a person is ever
  written to this repository.
- `consent` must be `true`, and only after the user has agreed that their
  contribution is licensed under the ODbL 1.0 and DbCL 1.0. Ask once, in plain
  words, before the first submission.

Responses:

| Status | Meaning |
|---|---|
| `202 {"id": "fb_<ulid>"}` | received |
| `400` | the body does not match the schema, or `consent` is not `true` |
| `401` | missing or unknown app key |
| `429` | rate limit reached (20 a day per device, and a limit per app key). Try again later. |

### Recent places

New places are served before they are in a release:

```
GET https://<intake host>/v1/places/recent?bbox=<west>,<south>,<east>,<north>
```

```json
{"places": [{"id": "ocdb_01JC...", "name": "Gravel Pit Rest Area", "location": {"lat": -35.1, "lon": 149.2}, "kind": "free_camp",
  "standing": "shown", "added": "2026-10-30T01:00:00Z", "contributor": "ctr_0123456789abcdef"}]}
```

- No app key. `bbox` is optional. Answers are cached for 30 seconds, so
  there is no need to ask more often.
- Only places that are `shown` or `confirmed` are listed. A place leaves the
  list when it is hidden or withdrawn, and once it is in the data, so read
  the release and this list together and join them by `id`, the release copy
  winning.
- `contributor` is the same opaque id as on reviews, so an app can hide
  every place from a contributor a traveller has hidden. The list never
  carries a note, contacts or a device id.
- Kind codes are those of [schema/kinds.json](schema/kinds.json), which
  gives each kind's group.

### What happens to feedback

Feedback is applied by a scheduled job, usually within the hour, and reaches
apps with the next release.

- A review is published if it passes moderation, and the place's rating is
  recalculated. A contributor's first review goes to a person; once one is
  accepted, later ones are published straight away. A newer review from the
  same contributor replaces their older one for that place.
- A fee holds no free text, so it is published at the next apply run. A
  newer fee from the same contributor for the same site type replaces their
  older one for that place.
- Every `review_report` goes to a person. Reports from 3 different
  contributors hide the review until a person decides. A person can hide a
  review or block a contributor (`moderation/blocked_contributors.json`).
  Everything from a blocked contributor is refused, and their reviews and
  fees are left out of the release and their reviews out of the rating.
- A `closed` report marks the place `reported_closed` at once. It becomes
  `closed` after reports from 2 different devices within 90 days, or 1 report
  and a person's approval. A `reopened` report or a check with
  `still_there: true` clears it. A check with `still_there: false` counts as
  a `closed` report.
- Reopening a place that is already `closed` needs `reopened` reports from 2
  different devices within 90 days.
- A `fact` report changes the fact once 2 different devices agree within 90
  days. Changes to contacts (`website`, `phone`, `phone_2`, `email`,
  `facebook`, `instagram`, `tiktok` and `x`) always go to a person.
- A `new_place` is written into the data at the next apply run, with
  `standing` `shown`, when the install proved itself with App Attest or Play
  Integrity, the report has a contributor and its note passes the text
  checks, with the `name` it carries. Checks with `still_there: true` and
  `nearby: true` from 2 other devices make it `confirmed`. `remove` reports, or checks with `still_there: false`, from
  2 different devices hide it, or 3 once it is confirmed. A blocked
  contributor's places are hidden. A new place with the same name as a
  place in the same group less than 50 m away, and no major road between,
  is merged into that place (see "Redirect merged places"), and a person
  can undo it. Any other new place within 150 m of a place in the
  same group, one hidden before it was published and one that fails the
  checks above go to a person as `held`. Contacts sent with a new place
  always go to a person before they are published.
- `wrong_location`, `name`, `description`, `kind` and `other` reports, and
  `remove` reports on a place that is not new, go to a person. An accepted
  kind, name or description is kept through later OSM syncs.
- A report that has not found agreement within 90 days expires.
- Checks never change facts on their own.

Corrections to names, positions and OSM tags are best made in OpenStreetMap
itself, where every map benefits. The nightly sync picks them up.
