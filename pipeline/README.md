# Pipeline

Four Python 3.12 commands turn OpenStreetMap data into the published dataset.
Run them from the repository root after `python -m pip install -r requirements.txt`.

| Step | Command | Reads | Writes |
|---|---|---|---|
| Extract | `python -m pipeline.extract FILE --out extract.ndjson` | `.osm.pbf` or `.osm` XML | one line per place, sorted by OSM type and id |
| Sync | `python -m pipeline.sync extract.ndjson --root . --report sync-report.md --summary sync-summary.json` | the extract and `places/` | changed files in `places/`, the report, a JSON summary |
| Validate | `python -m pipeline.validate --root .` | `places/`, `reviews/`, `schema/` | a list of problems, exit code 1 if there are any |
| Release | `python -m pipeline.release --root . --out dist` | `places/`, `reviews/` | `dist/places.ndjson.gz`, `dist/reviews.ndjson.gz`, `dist/manifest.json` |

Tests: `python -m pytest`.

## Extract

Keeps nodes, closed ways and multipolygon relations tagged with one of the
tags below. A node is placed at its own position. A closed way or a
multipolygon relation is placed at the centroid of its area, with holes taken
out, so `osm.type` is `node`, `way` or `relation`. pyosmium's area handler
assembles the multipolygons. Relations of any other type (such as
`type=site`), ways that are not closed, and objects with missing node
locations are skipped. Coordinates are rounded to 6 decimal places.

The centroid treats degrees as flat x and y, which is close enough over the
size of a camp ground. A shape with no area falls back to the mean of its
outer ring points.

The file is read twice. The first pass reads only ways and relations, without
node locations, and notes the closed ways with a SPEC tag and the member ways
of multipolygon relations with a SPEC tag. The second pass reads everything
with a node location cache, and a way id filter in front of the area builder
lets through only the ways noted in the first pass. Buildings and other
untagged closed ways are never assembled into areas. (pyosmium's
`FileProcessor.with_areas()` filters only relations, so it would assemble an
area for every closed way in Australia.) The log line "Handed N ways to the
area builder" shows how many got through.

### Kind

When an object has more than one of these tags, the first row that matches wins.

| OSM tag | `kind` |
|---|---|
| `tourism=camp_site` | `camp_site` |
| `tourism=caravan_site` | `caravan_site` |
| `highway=rest_area` | `rest_area` |
| `amenity=sanitary_dump_station` | `dump_station` |
| `amenity=drinking_water` | `drinking_water` |
| `amenity=toilets` | `toilets` |
| `amenity=shower` | `shower` |
| `tourism=picnic_site` | `picnic_site` |

`name` is the `name` tag, or `null` when there is none.

### Facts

A fact is only written when the tag has a value we can read. Any other value
leaves the fact out, which means "not known". Values are compared without
regard to case. Where OSM lists several values separated by `;`, the first is used.

| OSM tag | Fact | Mapping |
|---|---|---|
| `fee` | `fee` | `no` is `false`. `yes`, or any value with a digit or `$` (a price), is `true`. Others, such as `donation`, are left out. |
| `power_supply` | `power` | `yes` is `true`, `no` is `false` |
| `dog` | `pets` | `yes`, `leashed` or `no`, copied as is |
| `tents` | `tents` | `yes` / `no` |
| `caravans` | `caravans` | `yes` / `no` |
| `toilets` | `toilets` | `yes` / `no` |
| `shower` | `showers` | `yes`, `hot` or `cold` is `true`, `no` is `false` |
| `drinking_water` | `drinking_water` | `yes` / `no` |
| `sanitary_dump_station` | `dump_station` | `yes` / `no` |
| `maxstay` | `max_stay` | A number and a unit is shortened: hours to `h`, days or nights to `d`, weeks to `w`, months to `mo` (`48 hours` becomes `48h`). Anything else is kept as written. |
| `opening_hours` | `opening_hours` | copied as is |
| `website`, then `contact:website` | `website` | kept only if it starts with `http://` or `https://` |
| `phone`, then `contact:phone` | `phone` | copied as is |
| `4wd_only` | `access` | `yes` is `4wd`, `recommended` is `awd`, `no` is `2wd`. No tag, no fact. `unknown` is never written by the extract. |
| `access` | `entry` | `yes`, `permissive` or `public` is `public`. `customers`, `permit` and `private` are copied. `no` is `private`. Others, such as `destination`, are left out. |

`access` (the fact) is about the road and the vehicle. `entry` is about who
may stay, and comes from the OSM `access` tag.

Facts are never inferred from the kind: a `caravan_site` does not get
`caravans: true` unless the `caravans` tag says so.

## Sync

- Matches each extract line to a record by `osm.type` and `osm.id`.
- A new OSM object gets a new record with the id `ocdb_<ULID>`, `status: open`,
  `rating: null` and every fact sourced from `osm`.
- For a matched record, `name`, `kind`, `location`, `osm` and the OSM-sourced
  facts are replaced with what OSM says now. A fact whose `fact_sources` entry
  is `feedback` is kept. An OSM-sourced fact whose tag has gone is removed.
  `status` and `rating` are never touched.
- `fact_sources` may also hold `name` and `location`. When either is
  `feedback`, sync keeps the record's own value. When it is missing or `osm`,
  the value comes from OSM. Sync never adds these two entries itself.
- `updated` is set to the time of the run only when the record's content
  changed. Running the same extract twice changes no file.
- A record whose OSM object is no longer in the extract is never deleted. It
  is listed under "Gone from OSM" in the report until a person sets its
  `status` to `closed` or removes it.
- Change percentage is new + updated + gone, divided by the number of records
  after the sync. The nightly PR merges itself only below 2%.

Place files are written in one canonical form: sorted keys, 2-space indent,
UTF-8 characters as they are, LF line endings and one trailing newline. They
sit at `places/<last 2 characters of id, lower case>/<id>.json`, which spreads
them over up to 1,024 folders. Anything else that writes to `places/` (such
as the feedback workflow) must use the same form, or validation fails. In
Python that is
`json.dumps(record, sort_keys=True, indent=2, ensure_ascii=False) + "\n"`.

Review lines are compact JSON with sorted keys, one per line, sorted by
`id`, with LF line endings and a trailing newline. In Python that is
`json.dumps(review, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`.
`rating.mean` is the mean of the stars rounded to 1 decimal place, half up
(4.25 becomes 4.3). Python's `round()` rounds half to even, so use
`decimal.Decimal` with `ROUND_HALF_UP`, as `pipeline.common.rating_mean` does.

## Validate

Checks every place file against `schema/place.schema.json` and every review
line against `schema/review.schema.json`. Neither schema allows a key it does
not list. On top of the schemas it checks the canonical form, the file path,
6 decimal places, that `fact_sources` has an entry for every fact and no
other fact (plus optional `name` and `location`), that no two records share an
id or an OSM object, that review lines are canonical and sorted by id, that
each review's place exists, that each `rating` has the exact count and
rounded mean of its reviews, and that review text has no email
address, phone number, link, social media handle or obvious personal name.
The name check catches phrases such as "my name is" and sign-offs such as
"Cheers, Sam". It cannot catch every name, so the feedback moderation step
stays the main safeguard. ISO dates (`2026-10-30`, `2026-10`) are blanked out
before the checks run, so a date is never taken for a phone number. The
feedback engine's moderation does the same.

## Release

`places.ndjson.gz` holds every record on one line each, sorted by `id`.
`reviews.ndjson.gz` holds every review, grouped by place in id order. Lines
use compact JSON with sorted keys. Gzip is written with no file name and a
zero timestamp.

`data_version` is `<commit time>-<short sha>`, for example
`20261030T160000Z-abc1234`, and `generated` is the commit time. Both come from
the checked-out commit, so building one commit twice gives byte-identical
files. Pass `--data-version` and `--generated` to set them by hand.

The manifest names its `release`, `data-<commit time>` (for example
`data-20261030T160000Z`), and gives each file an absolute `url` in that
release: `https://github.com/<repository>/releases/download/<release>/<file>`.
`--repository` sets the owner and name, `opencampingdb/data` by default.
