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
  "format_version": 2,
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
  "licence": "ODbL-1.0",
  "attribution": "© OpenStreetMap contributors, Open Camping DB"
}
```

If `format_version` is higher than the one your app understands, keep the
data you have and stop: the record format has changed in a way your app
cannot read.

Format 1 had eight kinds (`camp_site`, `caravan_site`, `rest_area`,
`dump_station`, `drinking_water`, `toilets`, `shower`, `picnic_site`) and no
`group`. Format 2 uses the kinds listed in section 4 and adds `group`. Every
place kept its `id` across the change.

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

Treat `data_version` as an opaque string and compare it only for equality.
Checking once a day is plenty: the OSM sync runs once a night and a new
release is published at most once an hour. Do not poll more often than once
an hour. Only the newest 60 releases are kept, so do not store file URLs for
later: always start again from the manifest.

## 4. Use the records

The schemas in [schema/](schema/) are the contract. Some points to keep in mind:

- `id` is stable. Use it to link your own data (favourites, notes) to a place.
  It is never reused.
- Every fact is optional. A missing fact means "not known", not "no".
- `status` is `open`, `reported_closed` (at least one user said it has
  closed, not yet confirmed) or `closed`. Show `reported_closed` as a warning
  rather than hiding the place.
- `rating` is `null` until a place has accepted reviews.
- `osm` is `null` for places that did not come from OpenStreetMap: places
  contributed directly to Open Camping DB, and new places from feedback.
- `fact_sources` says where each fact came from: `osm`, `feedback` (from apps)
  or `contributed` (given directly to Open Camping DB under the ODbL). It may
  also have `name`, `location` and `status` entries. When `name` or
  `location` is missing, the name and position come from OSM. A place with
  `osm: null` and `contributed` name and location is a contributed place. A
  `status` entry says who closed or reopened the place.
- `access` is the vehicle needed on the road in (`2wd`, `awd`, `4wd`).
  `entry` is who may stay (`public`, `customers`, `permit`, `private`).
  Consider leaving `private` places out of what you show.
- Records gone from OpenStreetMap are kept until a person reviews them, so a
  place may linger for a while after it has really closed.

### Kinds and groups

`kind` says what a place is and `group` says which family it belongs to:
`overnight`, `utility`, `services`, `attraction`, `emergency` or `transport`.
Filter on `group` to show, for example, only places to stay. The kinds are the
site types of the OTWD (Open Touring and Waypoint Database) vocabulary, plus
`campground` and `picnic_site`. [schema/kinds.json](schema/kinds.json) holds
the same list in machine-readable form, and new kinds may be added in later
versions, so show an unknown kind under its group rather than dropping it.

| `kind` | Label | `group` |
|---|---|---|
| `free_camp` | Free camp | overnight |
| `paid_campground` | Paid campground | overnight |
| `caravan_park` | Caravan park | overnight |
| `national_park_campsite` | National park campsite | overnight |
| `state_forest_campsite` | State forest campsite | overnight |
| `showground` | Showground | overnight |
| `campground` | Campground (fee not known) | overnight |
| `rest_area` | Rest area | utility |
| `dump_point` | Dump point | utility |
| `drinking_water_point` | Drinking water point | utility |
| `public_toilet` | Public toilet | utility |
| `public_shower` | Public shower | utility |
| `laundromat` | Laundromat | utility |
| `picnic_site` | Picnic area | utility |
| `information_centre` | Information centre | services |
| `fuel` | Fuel | services |
| `lpg_refill_swap` | LPG refill or swap | services |
| `mechanic` | Mechanic | services |
| `attraction` | Attraction | attraction |
| `lookout` | Lookout | attraction |
| `swimming_spot` | Swimming spot | attraction |
| `boat_ramp` | Boat ramp | attraction |
| `hospital_emergency` | Hospital (emergency) | emergency |
| `ferry_terminal` | Ferry terminal | transport |

How each kind is derived from OpenStreetMap is described in
[pipeline/README.md](pipeline/README.md). A `drinking_water_point` may carry
the fact `water_type`: `tank_fill` for a tap suited to filling tanks, or
`fountain` for a drinking fountain, which often is not.

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

## 6. Send feedback

Apps send what their users report to the Open Camping DB intake. The
[feedback-engine](https://github.com/opencampingdb/feedback-engine)
repository holds a client SDK for this and the intake itself. For an app
key, the intake host, or help connecting your app, write to
dev@camporacle.com.au.

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

- `type` is `report`, `review` or `check`, with a matching `report`,
  `review` or `check` object.
- `place` is the record `id`, or `null` for a new place, in which case
  `location` (`{"lat": ..., "lon": ...}`) is required and `report.kind` is
  `new_place`.
- `report.kind` is `closed`, `reopened`, `fact`, `wrong_location`,
  `new_place` or `other`. A `fact` report also carries `fact` (a key from the
  place schema) and `value`.
- `review` is `{"stars": 1-5, "text": "...", "visited": "YYYY-MM"}`. Review
  text is published, so it must not hold names, email addresses, phone
  numbers or links. Reviews that do are refused.
- `check` is `{"visited": "YYYY-MM-DD", "still_there": true}`, a confirmation
  that a place is as described.
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

### What happens to feedback

Feedback is applied by a scheduled job, usually within the hour, and reaches
apps with the next release.

- A review is published if it passes moderation, and the place's rating is
  recalculated.
- A `closed` report marks the place `reported_closed` at once. It becomes
  `closed` after reports from 2 different devices within 90 days, or 1 report
  and a person's approval. A `reopened` report or a check with
  `still_there: true` clears it. A check with `still_there: false` counts as
  a `closed` report.
- Reopening a place that is already `closed` needs `reopened` reports from 2
  different devices within 90 days.
- A `fact` report changes the fact once 2 different devices agree within 90
  days. Changes to `website` and `phone` always go to a person.
- `new_place`, `wrong_location` and `other` reports go to a person.
- A report that has not found agreement within 90 days expires.
- Checks never change facts on their own.

Corrections to names, positions and OSM tags are best made in OpenStreetMap
itself, where every map benefits. The nightly sync picks them up.
