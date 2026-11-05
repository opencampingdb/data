# Pipeline

Four Python 3.12 commands turn OpenStreetMap data into the published dataset.
Run them from the repository root after `python -m pip install -r requirements.txt`.

| Step | Command | Reads | Writes |
|---|---|---|---|
| Extract | `python -m pipeline.extract FILE --out extract.ndjson [--geometry geometry.ndjson]` | `.osm.pbf` or `.osm` XML | one line per place, sorted by OSM type and id, and with `--geometry` the outlines, signals and major roads the duplicate rules need (see "Opposite sides of a road") |
| Sync | `python -m pipeline.sync extract.ndjson --root . --report sync-report.md --summary sync-summary.json [--geometry geometry.ndjson]` | the extract and `places/` | changed files in `places/`, the report, a JSON summary |
| Validate | `python -m pipeline.validate --root .` | `places/`, `reviews/`, `fees/`, `moderation/`, `commons/`, `schema/` | a list of problems, exit code 1 if there are any |
| Release | `python -m pipeline.release --root . --out dist` | `places/`, `reviews/`, `fees/`, `moderation/` | `dist/places.ndjson.gz`, `dist/reviews.ndjson.gz`, `dist/fees.ndjson.gz`, `dist/merged.ndjson.gz`, `dist/manifest.json`, `dist/place-index.ndjson.gz` |
| Place index | `INTAKE_PLACES_TOKEN=... python -m pipeline.place_index --url <intake>` | `dist/place-index.ndjson.gz` | the changed map cells, sent to a feedback intake (the release workflow runs this) |
| Commons | `python -m pipeline.commons --root . [--max-places N] [--stale-days 30]` | `places/` and the Wikimedia Commons API | `commons/matches.ndjson`, freely licensed Commons photos matched to places to stay (the nightly Commons photos workflow runs it). See the module docstring |
| Duplicates | `python -m pipeline.duplicates --root . [--geometry geometry.ndjson]` | `places/`, `moderation/distinct_places.json`, the geometry sidecar | likely duplicate pairs, one JSON object per line (sync runs this too) |
| Split | `python -m pipeline.split extract.ndjson --geometry geometry.ndjson --root .` | the extract and its sidecar, `places/` | records split where merged OSM objects are separate places, new pairs on the distinct list, `moderation/opposite-side-splits.csv`. Run by a person only |
| Merge | `python -m pipeline.merge <keep_id> <drop_id> --root . [--part]` | `places/`, `reviews/`, `fees/` | the kept record, a tombstone, moved reviews and fees. Run by a person only |
| Moderate | `python -m pipeline.moderate hide\|show <review id>` or `block <contributor> --reason <reason>\|unblock <contributor>` | `reviews/`, `moderation/`, `places/` | the review line or the blocked list, and the ratings it changes. Run by a person only |
| MapRoulette | `python -m pipeline.maproulette extract.ndjson --root . --geometry geometry.ndjson --inbox <inbox> --report sync-report.md` | the extract, `places/` and the inbox's `applied/` folder | `maproulette/double_mapping.geojson`, `maproulette/traveller_reports.geojson`, a section of the sync report (sync runs this too). Values with the source `community_report`, and places whose name or location has it, are left out. Report tasks need the evidence in `--inbox`, and without it the report file has no tasks. See [maproulette/README.md](../maproulette/README.md) |

Tests: `python -m pytest`.

## Extract

Keeps nodes, closed ways and multipolygon relations that `pipeline.kinds`
classifies as a place (see "Kind" below). A node is placed at its own
position. A closed way or a multipolygon relation is placed at the centroid of
its area, with holes taken out, so `osm.type` is `node`, `way` or `relation`.
pyosmium's area handler assembles the multipolygons. A `type=site` relation
with a kind tag is a place too (`osm.type` is `relation`). It is placed at the
mean of its members' positions: a member node's location, a member way's
centroid (the area centroid of a closed way, the mean of the nodes of an open
one) and the area centroid of a member multipolygon. Nested site relations are
not followed. Relations of any other type, ways that are not closed, and
objects with missing node locations are skipped, with one exception: an open
way that is a `boat_ramp` or an `attraction` (slipways and walls are often
drawn as lines) is placed at its middle vertex, node number `len(nodes) // 2`
counting from 0. Coordinates are rounded to 6 decimal places.

The centroid treats degrees as flat x and y, which is close enough over the
size of a camp ground. A shape with no area falls back to the mean of its
outer ring points.

The file is read three times. A first, quick pass reads only relations and
finds the site relations that are places, with their members. The next pass
reads only ways and relations, without
node locations. It notes the closed ways that are a place or a container (see
"Containers" below), and the member ways of multipolygon and boundary
relations that are a place or a container, and the member ways and member
multipolygons of those site relations. Every other relation is dropped before
the area builder sees it. The second pass reads everything with a node
location cache, and a way id filter in front of the area builder lets through
only the ways noted in the first pass. Buildings and other untagged closed
ways are never assembled into areas. (pyosmium's `FileProcessor.with_areas()`
filters only relations, so it would assemble an area for every closed way in
Australia.) The log line "Handed N ways to the area builder" shows how many
got through.

### Kind

`kind` is one of the codes in `schema/kinds.json`: the 22 site types of the
OTWD (Open Touring and Waypoint Database) vocabulary, version 1.0.0, under
Apache-2.0, plus three Open Camping DB extensions, `campground`,
`picnic_site` and `farm_stay`. A `farm_stay` is a station or farm stay: an OSM
camp or caravan site outside a park, forest or showground whose name says so
("Mount Gipps Station", "Bonus Downs Farmstay", "Olive Hill Farm", but not a
pump station, Farm Beach or a wind farm; `kinds.is_farm_stay`), or private land
listed by its owner. Each kind belongs to a group, and every record carries its
`group`.

| Code | Label | Group | Origin |
|---|---|---|---|
| `free_camp` | Free camp | overnight | OTWD |
| `paid_campground` | Paid campground | overnight | OTWD |
| `caravan_park` | Caravan park | overnight | OTWD |
| `national_park_campsite` | National park campsite | overnight | OTWD |
| `state_forest_campsite` | State forest campsite | overnight | OTWD |
| `showground` | Showground | overnight | OTWD |
| `campground` | Campground | overnight | Open Camping DB: a camp site whose fee is not known |
| `farm_stay` | Farm stay | overnight | Open Camping DB: a station or farm stay, named so in OSM or listed by its owner |
| `rest_area` | Rest area | utility | OTWD |
| `dump_point` | Dump point | utility | OTWD |
| `drinking_water_point` | Drinking water point | utility | OTWD |
| `public_toilet` | Public toilet | utility | OTWD |
| `public_shower` | Public shower | utility | OTWD |
| `laundromat` | Laundromat | utility | OTWD |
| `picnic_site` | Picnic area | utility | Open Camping DB |
| `information_centre` | Information centre | services | OTWD |
| `fuel` | Fuel | services | OTWD |
| `lpg_refill_swap` | LPG refill or swap | services | OTWD |
| `mechanic` | Mechanic | services | OTWD |
| `attraction` | Attraction | attraction | OTWD |
| `lookout` | Lookout | attraction | OTWD |
| `swimming_spot` | Swimming spot | attraction | OTWD |
| `boat_ramp` | Boat ramp | attraction | OTWD |
| `hospital_emergency` | Hospital (emergency) | emergency | OTWD |
| `ferry_terminal` | Ferry terminal | transport | OTWD |

#### Precedence

The rules are tried in this order, and the first that matches wins. An object
with several tags gets the kind of the earliest rule it matches: a camp site
that is also a rest area is a camp site, a viewpoint that is also an
attraction is a lookout, and a picnic site with `amenity=toilets` is a public
toilet.

| Order | OSM tags | `kind` | OTWD match | Notes |
|---|---|---|---|---|
| 1a | `tourism=camp_site`, inside a park | `national_park_campsite` | broader | Park: see "Containers" |
| 1b | `tourism=camp_site`, inside a state forest | `state_forest_campsite` | broader | |
| 1c | `tourism=camp_site`, inside a showground | `showground` | related | |
| 1d | `tourism=camp_site` + `fee=no` | `free_camp` | exact | Not for a commercial park (see "Facts") |
| 1e | `tourism=camp_site` + `fee=yes` or a price | `paid_campground` | exact | A price is a value with a digit or `$` |
| 1f | `tourism=camp_site`, any other fee or none | `campground` | | Open Camping DB extension. `fee=donation` lands here |
| 2a | `tourism=caravan_site` + `fee=no` | `free_camp` | exact | OTWD: for areas that suit self-contained vehicles. Not for a commercial park (see "Facts") |
| 2b | `tourism=caravan_site` | `caravan_park` | exact | Containers do not apply |
| 2c | `tourism=hostel` | `backpacker_hostel` | | Open Camping DB extension |
| 2d | `tourism=motel` or `tourism=chalet` | `roofed_accommodation` | | Open Camping DB extension. `tourism=hotel` and `guest_house` are left out: there are too many, and a park's cabins are the `cabins` fact |
| 3 | `highway=rest_area` | `rest_area` | exact | |
| 3a | `amenity=parking` + `motorhome`, `caravan` or `hgv=designated` | `long_vehicle_parking` | | Open Camping DB extension. Other car parks are left out |
| 4 | `amenity=sanitary_dump_station` | `dump_point` | exact | |
| 5 | `amenity=water_point` or `amenity=drinking_water` | `drinking_water_point` | exact, narrower | Fact `water_type`: `tank_fill` for water_point, `fountain` for drinking_water |
| 6 | `amenity=toilets` | `public_toilet` | exact | |
| 7 | `amenity=shower` | `public_shower` | exact | |
| 8 | `shop=laundry` + `self_service=yes` | `laundromat` | exact | A laundry without `self_service=yes` is left out |
| 9 | `amenity=hospital` + `emergency=yes` | `hospital_emergency` | exact | A hospital without `emergency=yes` is left out |
| 10 | `amenity=ferry_terminal` | `ferry_terminal` | exact | |
| 11 | `amenity=fuel` | `fuel` | exact | |
| 12 | `shop=gas` | `lpg_refill_swap` | exact | |
| 13 | `shop=car_repair` | `mechanic` | exact | |
| 13a | `amenity=pub` | `food_and_drink` | | Open Camping DB extension. Cafes and restaurants are left out: there are too many |
| 14 | `tourism=information` + `information=office` or `visitor_centre` | `information_centre` | exact, related | Other information values (boards, maps, guideposts) are left out |
| 15 | `tourism=viewpoint` | `lookout` | exact | |
| 16 | `leisure=swimming_area` or `leisure=bathing_place` | `swimming_spot` | narrower | |
| 17 | `leisure=swimming_pool` + `access=yes` or `access=public` | `swimming_spot` | related | Public pools only. Pools without public access are left out |
| 18 | `leisure=slipway` | `boat_ramp` | exact | An open way is placed at its middle vertex |
| 19 | `tourism=picnic_site` | `picnic_site` | | Open Camping DB extension |
| 19a | `tourism=museum` or `tourism=gallery` | `heritage` | | Open Camping DB extension |
| 20 | `tourism=attraction` | `attraction` | exact | Last, because OTWD uses it only when no more specific type fits. An open way is placed at its middle vertex |

OTWD mappings not used, and why:

| OTWD mapping | Match | Why not |
|---|---|---|
| `tourism=camp_site` + `caravans=yes` as `caravan_park` | related | Most free camps and campgrounds that allow caravans carry this tag. It would turn them into caravan parks |
| `tourism=camp_site` + `maxstay=*` as `rest_area` | related | Free camps and paid campgrounds often have a stay limit. `maxstay` is kept as the `max_stay` fact instead |
| `highway=services` as `rest_area` | related | Service centres are fuel and food stops. Their fuel stations are mapped as `amenity=fuel` |
| `landuse=recreation_ground` + `recreation_ground=showground` as a place | narrower | A showground is a place to stay only where it takes campers, and the tags do not say so. The ground is used as a container for camp sites inside it (rule 1c) |
| `natural=beach` as `swimming_spot` | related | Far too many beach polygons, most of them not swimming spots |
| `amenity=fuel` + `fuel:lpg=yes` as `lpg_refill_swap` | related | Autogas for vehicles is not a bottle refill. These stay `fuel` |

#### Containers

Only camp sites use containers. A camp site's position (its node, or the
centroid of its way or relation) is tested against every container with an
STRtree (shapely), and a point on a container's edge counts as inside.
Containers are closed ways and multipolygon or boundary relations. An area is
one container type at most, checked in this order:

| Order | Container | Tags |
|---|---|---|
| 1 | state forest | `landuse=forest` or `boundary=protected_area` whose `name`, `official_name` or `protection_title` contains "State Forest" |
| 2 | state forest | `landuse=forest` with an `operator` that is a forestry agency or a government department (Forestry Corporation, VicForests, Forest Products Commission, Sustainable Timber Tasmania, or containing "department", "government" or "crown"). Private plantation companies do not count |
| 3 | park | `boundary=national_park` |
| 4 | park | `boundary=protected_area` with `protect_class` 1a, 1b or 1 to 6 (the IUCN categories), or with an `operator` that is a park agency (a national parks service, Parks Victoria, Parks Australia, Parks Tasmania, Parks SA, a parks and wildlife service, NPWS, QPWS or DBCA) |
| 5 | showground | `landuse=recreation_ground` + `recreation_ground=showground`, or `leisure=showground` |

State forests are checked first because a state forest tagged as a protected
area often carries `protect_class=6`, which would otherwise make it a park.

`name` is the `name` tag, or `null` when there is none.

`osm` is `{"type", "id", "version", "timestamp"}`. `timestamp` is when that
version of the object was saved in OSM, the last time anyone edited it, in
UTC (`2025-03-04T07:59:37Z`). A multipolygon place takes its relation's
time and a site relation its own, not its members'. Geofabrik extracts and
Overpass `out meta` carry timestamps. A file without them (pyosmium then
reports the Unix epoch) gives no `timestamp`.

### Facts

A fact is only written when the tag has a value we can read. Any other value
leaves the fact out, which means "not known". Values are compared without
regard to case. Where OSM lists several values separated by `;`, the first is used.

| OSM tag | Fact | Mapping |
|---|---|---|
| `fee` | `fee` | `no` is `false`. `yes`, or any value with a digit or `$` (a price), is `true`. `donation` is `"donation"`. Others are left out. `fee=no` on a commercial park is left out too (see below). |
| `power_supply` | `power` | `yes` is `true`, `no` is `false` |
| `dog` | `pets` | `yes`, `leashed` or `no`, copied as is |
| `dog:conditional` | `pets`, `pets_months` | `yes @ (May-Sep)`, with `dog=no` or no `dog` tag, makes `pets` `seasonal` and `pets_months` the months as written in OSM's month range syntax (`May-Sep`, `Dec-Feb`, `Apr-May,Sep-Oct`). Only whole months count: a condition with times, weekdays or a second rule is left out, and `pets` keeps the `dog` value. When a traveller or the owner has set `pets` to anything but `seasonal`, sync drops OSM's months |
| `tents` | `tents` | `yes` / `no` |
| `caravans` | `caravans` | `yes` / `no` |
| `toilets` | `toilets` | `yes` / `no` |
| `shower` | `showers` | `yes`, `hot` or `cold` is `true`, `no` is `false` |
| `drinking_water` | `drinking_water` | `yes` / `no` |
| `sanitary_dump_station` | `dump_station` | `yes` / `no` |
| `wheelchair` | `wheelchair` | `yes` / `no`. `limited` is left out |
| `motorhome` | `motorhomes` | `yes` / `no` |
| `washing_machine` | `laundry` | `yes` / `no` |
| `kitchen` | `kitchen` | `yes` / `no` |
| `bbq` | `bbq` | `yes` / `no` |
| `picnic_table` | `picnic_tables` | `yes` / `no` |
| `openfire` | `campfires` | `yes` / `no` |
| `cabins` | `cabins` | `yes` / `no` |
| `playground` | `playground` | `yes` / `no` |
| `internet_access` | `wifi` | `wlan`, `wifi` or `yes` is `true`, `no` is `false` |
| `nudism` | `nudist` | `yes`, `designated` or `customary` is `true`, `no` is `false` |
| `min_age` | `over_50s` | `50`, `55` or `60` is `true`. No other value is written |
| `brand` | `brand` | A park chain or club we list (BIG4 is `big4`, Discovery Parks is `discovery`, and so on: `BRANDS` in `pipeline/kinds.py`). Other brands are left out |
| `addr:unit`, `addr:housenumber`, `addr:street`, `addr:place`, `addr:suburb` (then `addr:city`), `addr:state`, `addr:postcode` | `address` | an object with `unit`, `housenumber`, `street`, `place`, `suburb`, `state` and `postcode`. Written only with a street or a place. State becomes its abbreviation (`Victoria` is `VIC`); a state we can't read, a postcode that isn't four digits and values over 100 characters are left out |
| `email`, then `contact:email` | `email` | kept only if it looks like an email address |
| `contact:facebook`, `contact:instagram`, `contact:tiktok`, `contact:twitter` (then the bare keys) | `facebook`, `instagram`, `tiktok`, `x` | kept only if it starts with `https://`. A bare handle is left out |
| `maxstay` | `max_stay` | A number and a unit is shortened: hours to `h`, days or nights to `d`, weeks to `w`, months to `mo` (`48 hours` becomes `48h`). Anything else is kept as written. |
| `opening_hours` | `opening_hours` | copied as is |
| `website`, then `contact:website` | `website` | kept only if it starts with `http://` or `https://` |
| `phone`, then `contact:phone` | `phone` | copied as is |
| `4wd_only` | `access` | `yes` is `4wd`, `recommended` is `awd`, `no` is `2wd`. No tag, no fact. `unknown` is never written by the extract. |
| `amenity` | `water_type` | For a `drinking_water_point` only: `water_point` is `tank_fill`, `drinking_water` is `fountain` |
| `access` | `entry` | `yes`, `permissive` or `public` is `public`. `customers`, `permit` and `private` are copied. `no` is `private`. Others, such as `destination`, are left out. |

A commercial park is a `tourism=camp_site` or `tourism=caravan_site` whose
name says holiday park, tourist park, caravan park, holiday village, caravan
village or resort, or names a park chain (Discovery Parks, BIG4, NRMA Parks,
Ingenia Holidays, G'day Parks), or whose `brand` is a park chain, or whose website's domain does
(`lighthousebeachholidayvillage.com.au`, `discoveryholidayparks.com.au`).
`fee=no` on one is taken as a mapping slip and the `fee` fact is left out, so
the place is not a `free_camp` (rules 1d and 2a). A name that says free
("Barbour Park Free Caravan Park") keeps `fee=no`. Only the website's domain
is read, because council sites file free camps under paths such as
`/Caravan-Parks-and-Campgrounds/`. Other objects, such as the toilets in a
tourist park, keep `fee=no`.

`access` (the fact) is about the road and the vehicle. `entry` is about who
may stay, and comes from the OSM `access` tag.

The extract never infers a fact from the kind: a `caravan_park` does not
get `caravans: true` unless the `caravans` tag says so. `water_type` is the
one fact the extract takes from the kind. The release adds a few facts by
rule, never stored in `places/` (see "Rules" under "Release").

## Sync

- Matches each extract line to a record by `osm.type` and `osm.id`.
- A new OSM object gets a new record with the id `ocdb_<ULID>`, `status: open`,
  `rating: null` and every fact sourced from `osm`.
- For a matched record, `name`, `kind`, `group`, `location`, `osm` and the
  OSM-sourced facts are replaced with what OSM says now. A fact whose
  `fact_sources` entry is `feedback` or `contributed` is kept. An OSM-sourced
  fact whose tag has gone is removed. `status` and `rating` are never
  touched, and a `status` entry in `fact_sources` is kept as it is. A fact
  whose source is `owner` or `community_report` is kept too, with its source:
  sync never turns a community report value into an OSM value. A kind
  change counts as an update.
- A record without an OSM link (`osm: null`: a contributed place, a new
  place from feedback or one from community reports) is never touched,
  flagged or deleted.
- A record that still has one of the eight kinds used before the OTWD
  vocabulary keeps its id and moves to a new kind. A record matched to the
  extract takes the extract's kind. A record gone from OSM is moved by
  `LEGACY_KINDS` in `pipeline/sync.py`: `camp_site`
  becomes `free_camp` when its `fee` fact is false, `paid_campground` when it
  is true and `campground` otherwise. `caravan_site` becomes `caravan_park`,
  `dump_station` becomes `dump_point`, `drinking_water` becomes
  `drinking_water_point`, `toilets` becomes `public_toilet`, `shower` becomes
  `public_shower`, and `rest_area` and `picnic_site` stay as they are. The
  first sync after this change updates every record, so its PR waits for a
  person. A record without an OSM link that still has an old kind is left
  for a person to fix: validation reports it.
- `fact_sources` may also hold `name`, `location` and `status`. When `name`
  or `location` is `feedback` or `contributed`, sync keeps the record's own
  value. When it is missing or `osm`, the value comes from OSM. Sync never
  adds these entries itself.
- `osm.timestamp` comes from the extract with the rest of `osm`. When the
  extract has no timestamp for an object, the record keeps its own as long
  as the version is the same. A record that gains a timestamp it did not
  have (the first sync after timestamps were added) is written, and counts
  as updated in the report, but its `updated` does not move: the place did
  not change.
- `fact_dates` says when each value that is not from OSM was accepted: one
  entry, in the same UTC form as `updated`, for every `fact_sources` entry
  that is `feedback`, `contributed`, `owner` or `community_report`. Whoever
  sets such a value sets its date, the feedback engine for `feedback` and
  `owner` and the person opening the PR for `contributed`. A
  `community_report` value is dated the first day of the month the report
  was made. Sync keeps the dates of the
  values it keeps and never adds one. Values from OSM are as fresh as
  `osm.timestamp`.
- `last_confirmed` is kept as it is. Only the feedback engine sets it, when
  a traveller's check finds an open place still there.
- `updated` is set to the time of the run only when the record's content
  changed. Running the same extract twice changes no file.
- A record whose OSM object is no longer in the extract is never deleted. It
  is listed under "Gone from OSM" in the report until a person sets its
  `status` to `closed` or removes it.
- Change percentage is the number of records that are new, updated or gone
  (each record counted once), divided by the number of records after the sync. The nightly PR merges itself only below 2%.

Place files are written in one canonical form: sorted keys, 2-space indent,
UTF-8 characters as they are, LF line endings and one trailing newline. They
sit at `places/<last 2 characters of id, lower case>/<id>.json`, which spreads
them over up to 1,024 folders. Anything else that writes to `places/` (such
as the feedback workflow) must use the same form, or validation fails. In
Python that is
`json.dumps(record, sort_keys=True, indent=2, ensure_ascii=False) + "\n"`.

Values from a place's verified owner have the source `owner`. Sync never
overwrites them, and never touches `description`, `owner_verified`,
`reopens` or `status`, none of which come from OSM.

Review lines are compact JSON with sorted keys, one per line, sorted by
`id`, with LF line endings and a trailing newline. In Python that is
`json.dumps(review, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`.
`rating.mean` is the mean of the stars rounded to 1 decimal place, half up
(4.25 becomes 4.3). Python's `round()` rounds half to even, so use
`decimal.Decimal` with `ROUND_HALF_UP`, as `pipeline.common.rating_mean` does.

Fee lines in `fees/<place id>.ndjson` take the same form. The release keeps
the fees paid in the 24 months before the release time, leaving out fees
from blocked contributors.

## Duplicates

`pipeline.duplicates` finds pairs of places that are likely the same place.
Sync runs it after every merge into `places/` and lists the pairs in the
report under "Possible duplicates". It only flags. A pair is likely when:

- both places are in the overnight group, whatever their kinds (a caravan
  park and a paid campground can be the same place), or both have the same
  kind (every other group);
- they are within 200 m;
- their names are similar: a `difflib` ratio of 0.8 or more once the names
  are lower-cased, stripped of punctuation and of the words campground,
  campgrounds, camping, camp, camps, area, reserve, park, caravan, holiday
  and the. Or one of them has no name and they are within 50 m.

Never paired: two utility places that both have no name, two overnight
places that both have no name (often parts or pitches of one park), two
places whose names differ only in their numbers unless the numbers are equal
("Site 20" and "Site 26" are separate pitches), tombstones, and pairs on the
distinct list, and pairs the rules below find to be separate places. Places
are bucketed in a grid of 0.0025 by 0.004 degree cells (at least 200 m
across everywhere in Australia), so only neighbouring cells are compared.

### Opposite sides of a road

Rest areas, truck bays and fuel stations often come in pairs, one for each
direction of travel, a few tens of metres apart with the same name. They are
two places. `pipeline.geometry` decides, for two OSM objects, whether they
are one place or two:

1. **Same place by area.** When one is an area (a closed way or a
   multipolygon) of the other's group, and the other lies inside it, on its
   edge, or within 20 m of its edge with no major road between, they are one
   place. A rest area pin on the car park inside the rest area's outline is
   one place with it. Rules 2 and 3 are not asked.
2. **A major road between.** Two objects within 200 m are separate places
   when the straight line between them (from the nearest point of an area's
   outline) crosses a major road: `highway` motorway, trunk, primary or
   secondary, or one of their `_link` roads, whatever its `oneway`, or a
   tertiary road with `oneway=yes` or `oneway=-1`. Touching the road at an
   end does not count.
3. **Never-merge signals.** Different names (the finder's name test),
   different directions (northbound, south bound and so on in the name,
   `description` or `direction`), different operators (allowing for
   spacing, a missing "the", a typo or a longer form) or different `access`
   keep two objects apart. They never make two places one.

The rules need the sidecar `pipeline.extract --geometry` writes beside the
extract, from the same read of the OSM file: the simplified outline of every
place that is an area, the operator and directions of every place that has
them, and the pieces of every major road within about a kilometre of a
place. Roads are bucketed into a grid of 0.01 degree cells when the sidecar
is loaded, so a pair only tests the roads in the cells its line touches. The
sidecar never goes into place records or a release. Without it, only names,
the direction in a name and `entry` count.

Sync uses the rules for "Possible duplicates", `pipeline.maproulette` to
leave separate places out of the double mapping challenge, and
`pipeline.split` to undo earlier merges. `pipeline.split` checks every
record that holds more than one OSM object (parts aside) and splits off each
object the rules find to be a separate place. The object's tombstone comes
back to life with its old id (or, without one, it gets a new record), the
pair goes on the distinct list with reason `direction` (a road or a
direction) or `other`, and the split is added to
`moderation/opposite-side-splits.csv` for a person to review: the old and
new ids and names, the OSM objects each keeps, the distance and the road
crossed. A person runs it and opens a PR with the result.

### Distinct list

`moderation/distinct_places.json`
([schema](../schema/distinct_places.schema.json)) lists flagged pairs that a
person checked and found to be separate places, and the pairs
`pipeline.split` split apart. The finder and sync leave
them out from then on. Each entry has the two ids (`a` sorts before `b`), the
day it was `added`, a `reason` and an optional `note`:

| `reason` | Meaning |
|---|---|
| `pitches` | Separate pitches, access points or camp spots |
| `sections` | Parts of one ground with different rules, such as a caravan area and a tent area |
| `direction` | One for each side of a road |
| `gender` | Separate facilities for men and women |
| `other` | Anything else, said in `note` |

Add an entry by hand in the PR that sorts the pairs, keeping the list sorted
by `a`, then `b`.

## Merge

`python -m pipeline.merge <keep_id> <drop_id>` merges two records of one
place. A person runs it and opens a PR with the result:

- The kept record gains `merged_osm`, the dropped record's OSM link and any
  links already merged into it, sorted. Sync matches these objects to the
  kept record and never creates them again.
- With `--part`, the dropped record's own link gets `"part": true`: OSM
  draws the two as parts of one place on purpose, such as separate camping
  areas of one ground. The MapRoulette double mapping challenge leaves parts
  out. Part flags already on either record are kept.
- A pair on the distinct list is refused, unless `--part` says they are
  parts of one place, such as two sections of a caravan park on either side
  of a street.
- Facts the dropped record got from `feedback`, `contributed` or `community_report` move to the
  kept record where it has no value, with their `fact_dates`. OSM facts do
  not move.
- The kept record takes the newer `last_confirmed` of the two.
- The dropped record's reviews move to the kept record, and the kept record's
  rating is recalculated from its visible reviews.
- The dropped record's fees move to the kept record. Where one contributor
  has a fee for the same site type at both, only the newer is kept.
- The dropped record stays as a tombstone: `status: "merged"` and
  `merged_into: <keep_id>`, nothing else changed. Older tombstones that
  pointed at the dropped record now point at the kept one.

It refuses to merge a record into itself, an unknown id, or a record that is
already merged.

## Moderate

A person decides on reported reviews from the `feedback/review` PR, where
each report is listed. Three reports from different contributors have
already hidden a review (`hidden: "reported"`) on the automatic branch.

- `hide <review id>` sets `hidden: "moderator"`. `show <review id>` removes
  `hidden`, whether a person or reports set it.
- `block <contributor> --reason <reason>` adds the contributor to
  `moderation/blocked_contributors.json` with today's date. The reason is
  `spam_ad`, `offensive`, `off_topic`, `fake`, `personal_details` or
  `abuse`. The feedback engine then refuses everything they send, and their
  reviews are left out of the release and the rating. `unblock` removes them.

Each command recalculates the rating of every place it touches and sets
that place's `updated`, so validation passes. Commit the result on a branch
and open a PR.

## Validate

Checks every place file against `schema/place.schema.json` and every review
line against `schema/review.schema.json`. Neither schema allows a key it does
not list. On top of the schemas it checks that `group` is the group of the
record's `kind`, the canonical form, the file path,
6 decimal places, that `fact_sources` has an entry for every fact and no
other fact (plus optional `name`, `location` and `status`, each `osm`,
`feedback`, `contributed`, `owner` or `community_report`), that no file holds a
`rule` source or `fact_rules` (only the release sets those), that `fact_dates` has a date for exactly the
`fact_sources` entries that are not `osm` and none later than `updated`,
that `last_confirmed` is not in the future (it may be later than `updated`),
that no two records share an
id or an OSM object (counting `merged_osm`, leaving tombstones out), that a
tombstone has a `merged_into` naming a record that is not itself merged and
no reviews, the owner rules (a value with the source `owner` needs
`owner_verified: true`; a `farm_stay` its owner lists (`osm: null`) has `entry: private`,
`booking_required: true` and a `booking_url` or `phone`; a `description` is
checked like review text; `reopens` is a real date and only goes with
`status: temporarily_closed`), that review lines are canonical and sorted by id, that
each review's place exists, that each `rating` has the exact count and
rounded mean of its visible reviews (no `hidden`, no blocked `contributor`),
that `moderation/blocked_contributors.json` matches its schema and is sorted
by contributor with no repeats, that `moderation/distinct_places.json`
matches its schema, is sorted, lists each pair once with `a` before `b` and
names places that exist, and that review text has no email
address, phone number, link, social media handle or obvious personal name.
The name check catches phrases such as "my name is" and sign-offs such as
"Cheers, Sam". It cannot catch every name, so the feedback moderation step
stays the main safeguard. ISO dates (`2026-10-30`, `2026-10`) are blanked out
before the checks run, so a date is never taken for a phone number. The
feedback engine's moderation does the same.

## Release

`places.ndjson.gz` holds every record except tombstones on one line each,
sorted by `id`. `merged.ndjson.gz` holds one `{"id","merged_into"}` line per
tombstone, sorted by `id`.
`reviews.ndjson.gz` holds every visible review, grouped by place in id
order: lines with `hidden` and lines from a contributor in
`moderation/blocked_contributors.json` stay in the repository and are left
out. Lines
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

### Rules

[schema/fact_rules.json](../schema/fact_rules.json) lists facts the release
sets from a place's kind, where no source gives a value:

| Rule | Kind | Fact |
|---|---|---|
| `kind:public_toilet` | `public_toilet` | `toilets: true` |
| `kind:drinking_water_point` | `drinking_water_point` | `drinking_water: true` |
| `kind:public_shower` | `public_shower` | `showers: true` |
| `kind:dump_point` | `dump_point` | `dump_station: true` |
| `national_park` | `national_park_campsite` | `pets: "no"` |

A value from OSM, feedback, a contributor, the owner or a community report
always wins, including `false`. A rule fact has the source `rule` in
`fact_sources`, the rule's name in `fact_rules` and no `fact_dates` entry.
`pipeline.common.with_rule_facts` applies them to released records and to
the place index only: files in `places/` never hold them (validation
refuses one that does), so the OSM sync, merges and the MapRoulette
challenges never see them, and nothing set by a rule reaches OSM.

`place-index.ndjson.gz` is for feedback intakes, not apps: it is not a release
file and the manifest leaves it out. It holds one line for each place
`places.ndjson.gz` holds, sorted by `id`: `{"cell", "fact_dates",
"fact_sources", "facts", "id"}`, where `cell` is the whole-degree map cell
`[floor(lon), floor(lat)]` and `fact_dates` is `{}` when the record has none.
Its facts and sources include the rule facts, as the released record does.
An intake decides feedback against it at send time.

`python -m pipeline.place_index --url <intake>` loads it into an intake with
the token in `INTAKE_PLACES_TOKEN` (the intake's `PLACES_TOKEN`). It asks the
intake for the hash of each cell it holds, `GET /v1/place-index/cells`, and
sends only the cells whose lines hash differently, or that no longer hold a
place, with `PUT /v1/place-index`, about 500 kB of lines per call. A release that
changes a few places sends only their cells, and a call that fails is sent
again by the next release. The release workflow runs it after publishing when
the `INTAKE_URL` variable and the `INTAKE_PLACES_TOKEN` secret are set, and
skips it otherwise.
