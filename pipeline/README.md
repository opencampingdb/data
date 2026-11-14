# Pipeline

These Python 3.12 commands turn OpenStreetMap data into the published
dataset. Run them from the repository root after installing what they need:

```sh
python -m pip install -r requirements.txt
npm ci --ignore-scripts --prefix tools/oh-check   # opening_hours.js, for the out of spec check
```

The nightly sync runs Extract, Sync and MapRoulette in that order, and the
release workflow runs Validate, Release, Database and Place index.

| Step | Command | Reads | Writes |
|---|---|---|---|
| Extract | `python -m pipeline.extract FILE --out extract.ndjson [--geometry geometry.ndjson] [--localities localities/localities.ndjson]` | `.osm.pbf` or `.osm` XML | one line per place, sorted by OSM type and id, with `--geometry` the outlines, signals and major roads the duplicate rules need (see "Opposite sides of a road"), and with `--localities` every town and its council area (see "Towns and council areas") |
| Sync | `python -m pipeline.sync extract.ndjson --root . --report sync-report.md --summary sync-summary.json [--geometry geometry.ndjson]` | the extract and `places/` | changed files in `places/`, the report, a JSON summary |
| Validate | `python -m pipeline.validate --root .` | `places/`, `reviews/`, `moderation/`, `commons/`, `schema/` | a list of problems, exit code 1 if there are any |
| Release | `python -m pipeline.release --root . --out dist [--repository opencampingdb/data] [--data-version V] [--generated T]` | `places/`, `reviews/`, `moderation/`, `commons/` | `dist/places.ndjson.gz`, `dist/reviews.ndjson.gz`, `dist/merged.ndjson.gz`, `dist/commons.ndjson.gz`, `dist/manifest.json`, `dist/place-index.ndjson.gz` |
| Database | `python -m pipeline.database --release-dir dist [--previous <folder>] [--day <folder>] [--root .]` | `dist/`, `boundaries/au-states.geojson`, `localities/localities.ndjson`, `moderation/removed_reviews.json`, earlier releases' `manifest.json` and `ocdb.sqlite.gz` | `dist/ocdb.sqlite.gz`, `dist/changeset-*.bin.gz`, and their entries in `dist/manifest.json` |
| Place index | `INTAKE_PLACES_TOKEN=... python -m pipeline.place_index --url <intake> [--file dist/place-index.ndjson.gz]` | `dist/place-index.ndjson.gz` | the changed map cells, sent to a feedback intake (the release workflow runs this) |
| Commons | `python -m pipeline.commons --root . [--max-places N] [--stale-days 30]` | `places/` and the Wikimedia Commons API | `commons/matches.ndjson`, freely licensed Commons photos matched to places to stay (the nightly Commons photos workflow runs it), never a place and file pair on `moderation/commons-do-not-match.json` ([schema](../schema/commons_do_not_match.schema.json)). See the module docstring |
| Duplicates | `python -m pipeline.duplicates --root . [--geometry geometry.ndjson]` | `places/`, `moderation/distinct_places.json`, the geometry sidecar | likely duplicate pairs, one JSON object per line (sync runs this too) |
| Split | `python -m pipeline.split extract.ndjson --geometry geometry.ndjson --root .` | the extract and its sidecar, `places/` | records split where merged OSM objects are separate places, new pairs on the distinct list, `moderation/opposite-side-splits.csv`. Run by a person only |
| Merge | `python -m pipeline.merge <keep_id> <drop_id> --root . [--part]`, or `python -m pipeline.merge --auto --geometry geometry.ndjson --root .` | `places/`, `reviews/`, with `--auto` the geometry sidecar | the kept record, a tombstone and moved reviews; with `--auto` every pair the automatic merge rule accepts and `moderation/automatic-merges.csv`. Run by a person only |
| Vehicle access | `python -m pipeline.vehicle_access --root .` | `places/` | `moderation/heavy-vehicle-candidates.csv`: rest areas and long vehicle parking whose name alone says truck, heavy vehicle, HV or B-double, with columns `place` (the id), `name` and `location` (`lat,lon`), for a person to check and a mapper to tag in OSM. Run by a person |
| Moderate | `python -m pipeline.moderate hide\|show <review id>`, `remove <review id> --reason <reason>` or `block <contributor> --reason <reason>\|unblock <contributor>` | `reviews/`, `moderation/`, `places/` | the review line, the removed or blocked list, and the ratings it changes. Run by a person only |
| MapRoulette | `python -m pipeline.maproulette extract.ndjson --root . --geometry geometry.ndjson --inbox <inbox> --report sync-report.md` | the extract, `places/` and the inbox's `applied/` folder | `maproulette/double_mapping.geojson`, `maproulette/traveller_reports.geojson`, `maproulette/out_of_spec.geojson`, a section of the sync report (the nightly sync workflow runs this after Sync). Values with the source `community_report`, and places whose name or location has it, are left out. Report tasks need the evidence in `--inbox`, and without it the report file has no tasks. See [maproulette/README.md](../maproulette/README.md) |
| Out of spec check | runs inside the MapRoulette step, with `npm ci --ignore-scripts` in `tools/oh-check` first | facts in `places/` whose source is `osm`, while the extract still has the same value | `maproulette/out_of_spec.geojson`: one task per place and tag whose value the tag's reference refuses, `opening_hours` read by opening_hours.js (node, `tools/oh-check`) as Australian, `phone` and `phone_2` against the international form (`+61 ...`), `charge` and `charge_conditional` against the Key:charge syntax. Style warnings and values that fail only for missing school holiday data make no task. Without node the file is left as it was and the sync report says why. See [pipeline/out_of_spec.py](out_of_spec.py) |

Tests: `python -m pytest`. The out of spec tests skip without
`tools/oh-check` installed as above. CI installs it and sets `REQUIRE_NODE=1`
so they cannot skip there.

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

The file is read three times:

1. Relations only: the site relations that are places, with their members.
2. Ways and relations, without node locations: the closed ways that are a
   place or a container (see "Containers" below), the member ways of
   multipolygon and boundary relations that are a place or a container, and
   the member ways and member multipolygons of those site relations. Every
   other relation is dropped before the area builder sees it.
3. Everything, with a node location cache. A way id filter in front of the
   area builder lets through only the ways noted in pass 2, so buildings and
   other untagged closed ways are never assembled into areas. Every way is
   also checked for a major road (see "Nearby features").

The log line "Handed N ways to the area builder" shows how many ways got
through.

### Kind

`kind` is one of the 33 codes in `schema/kinds.json`: the 22 site types of
the OTWD (Open Touring and Waypoint Database) vocabulary, version 1.0.0,
under Apache-2.0, plus 11 Open Camping DB extensions. A `farm_stay` is a
station or farm stay: an OSM camp or caravan site outside a park, forest,
reserve or showground whose name says so ("Mount Gipps Station", "Bonus
Downs Farmstay", "Olive Hill Farm", but not a pump station, Farm Beach or a
wind farm; `kinds.is_farm_stay`), or private land listed by its owner. Each
kind belongs to a group, and every record carries its `group`.

| Code | Label | Group | Origin |
|---|---|---|---|
| `free_camp` | Free camp | overnight | OTWD |
| `paid_campground` | Paid campground | overnight | OTWD |
| `caravan_park` | Caravan park | overnight | OTWD |
| `national_park_campsite` | National park campsite | overnight | OTWD |
| `state_forest_campsite` | State forest campsite | overnight | OTWD |
| `reserve_campsite` | Reserve campsite | overnight | Open Camping DB: a camp site in a conservation park, nature reserve, regional park or other protected area that is not a national park. No dog rule: their rules on dogs differ too much |
| `showground` | Showground | overnight | OTWD |
| `campground` | Campground | overnight | Open Camping DB: a camp site whose fee is not known |
| `farm_stay` | Farm stay | overnight | Open Camping DB: a station or farm stay, named so in OSM or listed by its owner |
| `backpacker_hostel` | Backpacker hostel | overnight | Open Camping DB: `tourism=hostel` |
| `roofed_accommodation` | Roofed accommodation | overnight | Open Camping DB: cabins, motels and chalets that are not part of a campground |
| `rest_area` | Rest area | utility | OTWD |
| `dump_point` | Dump point | utility | OTWD |
| `drinking_water_point` | Drinking water point | utility | OTWD |
| `public_toilet` | Public toilet | utility | OTWD |
| `public_shower` | Public shower | utility | OTWD |
| `laundromat` | Laundromat | utility | OTWD |
| `picnic_site` | Picnic area | utility | Open Camping DB |
| `long_vehicle_parking` | Long vehicle parking | utility | Open Camping DB: parking marked for motorhomes, caravans or heavy vehicles |
| `information_centre` | Information centre | services | OTWD |
| `fuel` | Fuel | services | OTWD |
| `lpg_refill_swap` | LPG refill or swap | services | OTWD |
| `mechanic` | Mechanic | services | OTWD |
| `food_and_drink` | Food and drink | services | Open Camping DB: a pub from OSM, or a cafe, bakery or restaurant listed by travellers or its owner |
| `attraction` | Attraction | attraction | OTWD |
| `lookout` | Lookout | attraction | OTWD |
| `swimming_spot` | Swimming spot | attraction | OTWD |
| `boat_ramp` | Boat ramp | attraction | OTWD |
| `recreation_area` | Recreation area | attraction | Open Camping DB: day use only, listed by travellers or its owner |
| `heritage` | Arts, history and culture | attraction | Open Camping DB: a museum, gallery or heritage site |
| `experience` | Experience | attraction | Open Camping DB: a paid tour, tasting or activity, listed by travellers or its owner |
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
| 1c | `tourism=camp_site`, inside a reserve | `reserve_campsite` | | Open Camping DB extension. Reserve: see "Containers" |
| 1d | `tourism=camp_site`, inside a showground | `showground` | related | |
| 1e | `tourism=camp_site` whose name says station or farm stay | `farm_stay` | | Open Camping DB extension (`kinds.is_farm_stay`) |
| 1f | `tourism=camp_site` + `fee=no` | `free_camp` | exact | Not for a commercial park (see "Facts") |
| 1g | `tourism=camp_site` + `fee=yes` or a price | `paid_campground` | exact | A price is a value with a digit or `$` |
| 1h | `tourism=camp_site`, any other fee or none | `campground` | | Open Camping DB extension. `fee=donation` lands here |
| 2a | `tourism=caravan_site` whose name says station or farm stay | `farm_stay` | | Open Camping DB extension |
| 2b | `tourism=caravan_site` + `fee=no` | `free_camp` | exact | OTWD: for areas that suit self-contained vehicles. Not for a commercial park (see "Facts") |
| 2c | `tourism=caravan_site` | `caravan_park` | exact | Containers do not apply |
| 2d | `tourism=hostel` | `backpacker_hostel` | | Open Camping DB extension |
| 2e | `tourism=motel` or `tourism=chalet` | `roofed_accommodation` | | Open Camping DB extension. `tourism=hotel` and `guest_house` are left out: there are too many, and a park's cabins are the `cabins` fact |
| 3 | `highway=rest_area` | `rest_area` | exact | |
| 3a | `amenity=parking` + `motorhome=designated`, `caravan=designated` or `hgv=designated` | `long_vehicle_parking` | | Open Camping DB extension. Other car parks are left out |
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
| `landuse=recreation_ground` + `recreation_ground=showground` as a place | narrower | A showground is a place to stay only where it takes campers, and the tags do not say so. The ground is used as a container for camp sites inside it (rule 1d) |
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
| 4 | park | `boundary=protected_area` with `protect_class` 1a, 1b or 1 to 6 (the IUCN categories), or with an `operator` that is a park agency (a national parks service, Parks Victoria, Parks Australia, Parks Tasmania, Parks SA, a parks and wildlife service, NPWS, QPWS or DBCA), whose `name`, `official_name` or `protection_title` contains "National Park" |
| 4b | reserve | The same protected areas when none of those says "National Park": conservation parks, nature reserves, coastal reserves and the like. A park agency managing an area, or an IUCN class, does not make it a national park |
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
regard to case. For the contact keys, `reservation`,
`toilets:disposal`, `internet_access`, `nudism`, `min_age`, `access`,
`sanitary_dump_station` and the vehicle keys (`motorcycle`,
`motor_vehicle`, `hgv`, `motorcar`), only the first of several `;` values is
read. A plain `yes` / `no` key with several values is left out.

Each fact in `schema/place.schema.json` except `near_road` is marked
`mapped`, `proposable`, `ocdb_only` or `retired` (`x-ocdb-class`, with the
OSM tag, the suggested form, or for a retired fact where its information now
lives, in `x-osm`).
The facts below and the nearby facts are mapped. The extensions are listed
in "Mapped facts and extensions" in [CONNECTING.md](../CONNECTING.md).

| OSM tag | Fact | Mapping |
|---|---|---|
| `fee` | `fee` | `no` is `false`. `yes`, or any value with a digit or `$` (a price), is `true`. `donation` is `"donation"`. Others are left out. `fee=no` on a commercial park is left out too (see below). |
| `power_supply` | `power` | `yes` is `true`, `no` is `false` |
| `dog` | `pets` | `yes`, `leashed` or `no`, copied as is |
| `dog:conditional` | `pets`, `pets_months` | `yes @ (May-Sep)`, with `dog=no` or no `dog` tag, makes `pets` `seasonal` and `pets_months` the range: one OSM month range of two different months (`May-Sep`, or `Dec-Feb` across the year end). Anything else is left out: a single month, a list of ranges, times, weekdays or a second rule, and `pets` keeps the `dog` value. When a traveller or the owner has set `pets` to anything but `seasonal`, sync drops OSM's months |
| `tents` | `tents` | `yes` / `no` |
| `caravans` | `caravans` | `yes` / `no` |
| `toilets` | `toilets` | `yes` / `no` |
| `shower` | `showers` | `yes`, `hot` or `cold` is `true`, `no` is `false` |
| `drinking_water` | `drinking_water` | `yes` / `no` |
| `sanitary_dump_station` | `dump_station` | `yes` / `no`. `customers` is `true` on a place to stay (a kind in the overnight group), whose guests may use it, and left out on any other kind |
| `wheelchair` | `wheelchair` | `yes` / `no`. `limited` is left out |
| `motorhome` | `motorhomes` | `yes` / `no` |
| `motorcycle`, then `motor_vehicle` | `motorbikes` | Motorbikes may enter (a permission, not whether the way in suits one). `yes`, `designated` or `permissive` is `true`, `no` is `false`, others are left out. `motor_vehicle` is read the same way only when there is no `motorcycle` tag, so `motor_vehicle=no` on a walk-in site is `false` |
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
| `fuel:diesel`, `fuel:octane_91`, `fuel:e10`, `fuel:octane_95`, `fuel:octane_98`, `fuel:e85`, `fuel:adblue`, `fuel:lpg` | `fuels` | the fuels tagged `yes`, as `diesel`, `unleaded_91`, `e10`, `unleaded_95`, `unleaded_98`, `e85`, `adblue`, `lpg` in that order. No fuel tagged `yes`, no fact. `fuel:lpg` is LPG at the pump, so it never sets `lpg` (gas bottle refill or swap) |
| `compressed_air` | `air` | `yes` / `no` |
| `fishing` | `fishing` | `yes` / `no`. Other values, such as `permit`, are left out |
| `bin` | `bins` | `yes` / `no` |
| `hot_water`, `shower` | `hot_water` | `yes` / `no`. Without a `hot_water` value, `shower=hot` is `true` and `shower=cold` is `false` |
| `toilets:disposal` | `toilet_type` | `flush`, `pitlatrine` is `pit`, `chemical`, `dry_toilet` or `bucket` is `dry`. Others are left out |
| `reservation` | `booking_required` | `required` is `true`, `no` is `false`. `yes` and `recommended` are left out |
| `operator` | `operator` | trimmed, up to 200 characters. Longer values are left out |
| `addr:unit`, `addr:housenumber`, `addr:street`, `addr:place`, `addr:suburb` (then `addr:city`), `addr:state`, `addr:postcode` | `address` | an object with `unit`, `housenumber`, `street`, `place`, `suburb`, `state` and `postcode`. Written only with a street or a place. State becomes its abbreviation (`Victoria` is `VIC`); a state we can't read, a postcode that isn't four digits and values over 100 characters are left out |
| `email`, then `contact:email` | `email` | kept only if it looks like an email address, up to 254 characters |
| `contact:facebook`, `contact:instagram`, `contact:tiktok`, `contact:twitter` then `contact:x` (then the bare keys `facebook`, `instagram`, `tiktok`, `twitter`) | `facebook`, `instagram`, `tiktok`, `x` | kept only if it starts with `https://`, in lower case, and is up to 500 characters. A bare handle is left out |
| `maxstay` | `max_stay` | A number and a unit is shortened: hours to `h`, days or nights to `d`, weeks to `w`, months to `mo` (`48 hours` becomes `48h`). Anything else is kept as written, when it is up to 100 characters. A longer value is left out. |
| `opening_hours` | `opening_hours` | copied as is |
| `charge` | `charge` | copied as is, trimmed and cut at 255 characters, out of spec values too ([ADR 0002](../docs/adr/0002-follow-osm-formats-keep-extensions.md)). A `check_date:charge` in `YYYY-MM-DD` form goes to `osm.check_dates.charge` |
| `charge:conditional` | `charge_conditional` | the same. `check_date:charge:conditional` goes to `osm.check_dates.charge_conditional` |
| `website`, then `contact:website` | `website` | kept only if it starts with `http://` or `https://`, in lower case, and is up to 500 characters |
| `phone`, then `contact:phone` | `phone`, `phone_2` | the first `;` value is `phone`, copied as is when it is up to 100 characters. A longer first value leaves out both numbers. The second, from the same key, is `phone_2` when it differs from the first, up to 100 characters |
| `website:booking`, then `reservation:website` | `booking_url` | the first with a value, kept only if it starts with `https://`, in lower case, and is up to 500 characters. An owner's `booking_url` is never replaced (see `pipeline/sync.py`) |
| `4wd_only` | `access` | `yes` is `4wd`, `recommended` is `awd`, `no` is `2wd`. No tag, no fact. `unknown` is never written by the extract. |
| `amenity` | `water_type` | For a `drinking_water_point` only: `water_point` is `tank_fill`, `drinking_water` is `fountain` |
| `access` | `entry` | `yes`, `permissive` or `public` is `public`. `customers`, `permit` and `private` are copied. `no` is `private`, except on a place for heavy vehicles only (next row), where it says who may drive in and is left out. Others, such as `destination`, are left out. |
| `hgv`, `motorcar`, `motor_vehicle`, `access` | `vehicle_access` | `heavy_vehicles_only` for `hgv=designated` with `motorcar=no` or `motor_vehicle=no`, or `access=no` with `hgv=yes` or `designated`. Any kind of place. A name alone ("Truck Parking Bay") never sets it: `python -m pipeline.vehicle_access` lists those places for a reviewer |
| `hazard`, `hazard:animal` | `crocodiles` | `crocodile` or `crocodiles`, alone or in a `;` list, is `true`, on the place itself or on a hazard within 1 km (see "Nearby features"). Never `false`: OSM has no tag in use that says there are none |

A commercial park is a `tourism=camp_site` or `tourism=caravan_site` whose
name says holiday park, tourist park, caravan park, holiday village, caravan
village or resort, or names a park chain (Discovery Parks, BIG4, NRMA Parks,
Ingenia Holidays, G'day Parks), or whose `brand` is a park chain, or whose website's domain does
(`lighthousebeachholidayvillage.com.au`, `discoveryholidayparks.com.au`).
`fee=no` on one is taken as a mapping slip and the `fee` fact is left out, so
the place is not a `free_camp` (rules 1f and 2b). A name that says free
("Barbour Park Free Caravan Park") keeps `fee=no`. Only the website's domain
is read, because council sites file free camps under paths such as
`/Caravan-Parks-and-Campgrounds/`. Other objects, such as the toilets in a
tourist park, keep `fee=no`.

`access` (the fact) is about the road and the vehicle. `entry` is about who
may stay, and comes from the OSM `access` tag.

#### Nearby features

These facts come from other OSM objects inside or near the place,
collected in the same read (`pipeline/water.py`, `pipeline/nearby.py` and
`pipeline/roads.py`):

- `nearest_beach`: the nearest `natural=beach` within 5 km, as an area, a
  node or an open line, with `distance_m` from the place's location to the
  nearest point of the beach (0 inside it) and the beach's `name` when it
  has one of 100 characters or fewer. The distance is worked out on a flat
  projection around the place, with longitude scaled by the cosine of the
  latitude, and rounded to 10 m (half up) so that a small redraw of a beach
  changes no record. Ties go to the beach read first.
- `crocodiles: true` when an object within 1 km carries a crocodile hazard
  (`hazard=crocodile` or `crocodiles`, or `hazard:animal=crocodile` or
  `crocodiles`, as a whole value) and the place's own tags set no
  `crocodiles`. OSM's `hazard` key is for hazards declared by a sign or a
  government notice.
- `nearest_boat_ramp`: the nearest `leisure=slipway` within 5 km, as a node,
  an area or an open line, by the same rules as `nearest_beach` (distance to
  the nearest point, rounded to 10 m half up, `name` only when 100
  characters or fewer, ties to the ramp read first). A slipway is also a
  place of kind `boat_ramp`, and never counts for itself.
- `nearest_major_road`: the nearest major road within 500 m
  (`pipeline/roads.py`), with `distance_m` from the place's location to the
  nearest point of the road's line, worked out and rounded like
  `nearest_beach`, and the road's `name`, or its `ref` when it has no name
  (left out when it has neither, or when it is longer than 100 characters).
  A major road is the set the geometry sidecar uses
  (`pipeline.geometry.is_major_road`): `highway=motorway`, `trunk`,
  `primary` or `secondary`, their `_link` roads, and a `tertiary` road with
  `oneway=yes` or `-1`, one carriageway of a divided road. Every such way
  counts as a line through its nodes, closed or not. The roads come from the
  same read as the places, and the sidecar's road lines are written from
  them. Ties go to the road read first. It is separate from `near_road`, a
  traveller's report that road noise carries.
- `pub`, `fuel` and `car_wash: true` when an `amenity=pub`, `amenity=fuel`
  or `amenity=car_wash` lies inside the place's area (a closed way or a
  multipolygon) or on its edge. A node counts at its position, a closed way
  or multipolygon at the centroid of its area. A place drawn as a node has
  no area and gets none of these, and an object never counts for itself, so
  a fuel station drawn as an area does not get `fuel` from its own centroid.
  A place whose own tags set the fact keeps its own value. Never `false`.

#### Towns and council areas

`locality` and `lga` come from towns and council boundaries collected in
the same read (`pipeline/localities.py`):

- `locality`: the nearest node tagged `place=city`, `place=town` or
  `place=village` with a `name` of 100 characters or fewer, within 50 km of
  the place's location, as `name`, `osm_id` (the node), `type` (the `place`
  value) and `distance_m`. The distance is on the sphere (haversine, mean
  radius 6,371,008.8 m), to the node, rounded to 10 m (half up). Ties go to
  the lower node id. Hamlets, localities and suburbs do not count: there are
  too many, and a town is what a traveller names a place by.
- `lga`: the `boundary=administrative`, `admin_level=6` relation the place's
  location lies in (in Australia, a local government area: a council,
  shire, city or municipality, or an unincorporated area OSM maps at that
  level), as `name` and `osm_id` (the relation). A point inside more than
  one, where outlines overlap, takes the lower relation id.

Both always have the source `osm`, and a place with no town within 50 km or
outside every council area has none. With `--localities`, the extract also
writes every town it read to `localities/localities.ndjson`
([schema](../schema/locality.schema.json)), one line per town sorted by
node id: `osm_id`, `name`, `type`, `lat`, `lon`, `population` when the
node's `population` tag is a whole number (thousands separators allowed),
and the town's own `lga`. The nightly sync commits the file with
`places/`, and `pipeline.database` turns it into the `localities` table.

Swim spots (`leisure=bathing_place` and `leisure=swimming_area`) are places
of their own, of kind `swimming_spot`, so they get no fact. OSM has no tag
in use for sharks or dangerous jellyfish (taginfo, 12 Nov 2026: 0 uses), so
those facts (`sharks`, `marine_stingers`) come only from fact reports.

The extract never infers a fact from the kind: a `caravan_park` does not
get `caravans: true` unless the `caravans` tag says so. `water_type` is the
one fact the extract takes from the kind. The release adds a few facts by
rule, never stored in `places/` (see "Rules" under "Release").

## Sync

- Matches each extract line to a record by `osm.type` and `osm.id`.
- A new OSM object gets a new record with the id `ocdb_<ULID>`, `status: open`,
  `rating: null` and every fact sourced from `osm`.
- For a matched record, `name`, `kind`, `group`, `location`, `osm` and the
  OSM-sourced facts are replaced with what OSM says now. A fact whose source
  is anything but `osm` (`feedback`, `contributed`, `owner` or
  `community_report`) is kept, with its source: sync never turns a community
  report value into an OSM value. An OSM-sourced fact whose tag has gone is
  removed. `status` and `rating` are never touched. A kind change counts as
  an update.
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
  `public_shower`, and `rest_area` and `picnic_site` stay as they are. A
  record without an OSM link that still has an old kind is left for a
  person to fix: validation reports it.
- `fact_sources` may also hold `name`, `location`, `kind`, `status` and
  `description`. When `name`, `location` or `kind` has a source other than
  `osm`, sync keeps the record's own value. When it is missing or `osm`, the
  value comes from OSM. Sync never adds these entries itself.
- `osm.timestamp` comes from the extract with the rest of `osm`. When the
  extract has no timestamp for an object, the record keeps its own as long
  as the version is the same. A record that gains a timestamp it did not
  have is written, and counts as updated in the report, but its `updated`
  does not move: the place did not change.
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
- A new or refreshed record that `schema/place.schema.json` refuses is not
  written: a new one gets no file and an existing one keeps its file as it
  was. The report lists it under "Skipped" with the field and the rule it
  breaks, and the summary counts it as `skipped`. The rest of the sync goes
  ahead.
- Change percentage is the number of records that are new, updated or gone
  (each record counted once), divided by the number of records after the
  sync. The nightly PR merges itself only below 2%.

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

## Duplicates

`pipeline.duplicates` finds pairs of places that are likely the same place.
Sync runs it after every merge into `places/` and lists the pairs in the
report under "Possible duplicates". It only flags (merging is a separate
step, see "Merge"). A pair is likely when:

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
- The dropped record stays as a tombstone: `status: "merged"` and
  `merged_into: <keep_id>`, nothing else changed. Older tombstones that
  pointed at the dropped record now point at the kept one.

It refuses to merge a record into itself, an unknown id, or a record that is
already merged.

### Automatic merges

`pipeline.duplicates.automatic_merge(a, b, geometry, distinct)` is the
automatic merge rule. It returns `None` when two records may be merged
without anyone looking, and otherwise the reason they wait for a reviewer.
All of these must hold:

- neither is a tombstone or has a verified owner, and the pair is not on the
  distinct list;
- the same group, and outside the overnight group the same kind (as the
  duplicate finder compares them);
- the same name once tidied (`tidy_name`): lower case, punctuation
  removed, `RA`, `Ck` and `Mt` written as rest area, creek and mount, then
  `and` and the finder's generic words dropped, with something left;
- less than 50 m apart (between the records' locations);
- `pipeline.geometry.judge` does not find them separate: no major road
  between them, and no different direction, operator or entry.

`python -m pipeline.merge --auto --geometry geometry.ndjson --root . [--group overnight]`
runs it over the finder's pairs in the overnight group (or the groups
given). Pairs that share a record make one group, merged into the record
with an OSM link, then a camp site over a motel or hostel, then the most
facts, then a kind more specific than `campground`, then the oldest id. A
motel or hostel merged with a camp site is merged as a part (`--part`):
OSM maps the two as separate features of one place on purpose. Each merge
is added to `moderation/automatic-merges.csv` (merged date, both ids,
names, kinds and OSM objects, the distance and whether it was a part) for
the person who reviews the PR. The geometry sidecar is required, because
without roads the rule cannot tell opposite sides of a highway apart. The
feedback engine applies the same rule when a traveller adds a place next
to an existing one, and merges the new place into it. Its tombstone is the
new place's own record (`osm: null`, its `standing`, its feedback
`fact_sources` and `fact_dates`) with `status: "merged"` and `merged_into`,
and the release lists it in `merged.ndjson.gz` like any other.

A person reverses an automatic merge of a new place by setting its
`status` back to `open`, removing `merged_into`, and adding the pair to
`moderation/distinct_places.json` so neither the finder nor the rule
pairs them again. A batch merge of two OSM records is reversed the same
way, and the dropped object's link is taken out of the kept record's
`merged_osm` (`pipeline.split` does both when a road or direction splits
them).

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
- `remove <review id> --reason <reason>` is for personal details and
  takedown requests. A hidden review still sits in `reviews/`, which anyone
  can read, so `remove` deletes the line (and the place's review file when
  it was the last) and adds the review id, place, day and reason, never the
  text, to `moderation/removed_reviews.json`
  ([schema](../schema/removed_reviews.schema.json)). Validation refuses a
  review line whose id is on that list, so the review cannot come back
  through a later feedback run or by hand. A removal is not undone with
  `show`; to restore a review, a person takes it off the list and puts the
  line back in the same PR.

A removal reaches `main` and every release built after it. No file of a
later release holds the text: `pipeline.database` makes no changeset from a
database that holds the removed review (see Database and changesets), so
the next release and that UTC day's later ones ship none from it. It does not
reach git history or the releases already published: the text stays in
earlier commits and in the release assets until the newest 60 releases have
moved past it. When that is not enough (a court order, a person's safety), the
repository owners rewrite history and delete the affected releases, and ask
GitHub support to purge cached views; say so on the takedown request.

Each command recalculates the rating of every place it touches and sets
that place's `updated`, so validation passes. Commit the result on a branch
and open a PR.

## Validate

`python -m pipeline.validate --root .` prints one line per problem and exits
1 if there are any. It checks:

- every place file against `schema/place.schema.json` and every review line
  against `schema/review.schema.json`. Neither schema allows a key it does
  not list;
- that each place file is in the canonical form (see "Sync") at its path,
  that `group` is the group of the record's `kind`, and that coordinates
  have at most 6 decimal places;
- that `fact_sources` has an entry for every fact and no other fact, plus
  optional `name`, `location`, `kind`, `status` and `description` entries,
  each `osm`, `feedback`, `contributed`, `owner` or `community_report`. No
  file holds a `rule` source or `fact_rules`: only the release sets those;
- that the nearby facts `nearest_beach`, `nearest_boat_ramp`,
  `nearest_major_road`, `locality` and `lga` have the source `osm`;
- that `localities/localities.ndjson`, when present, matches its schema, in
  canonical form and sorted by `osm_id`, each town once;
- that `fact_dates` has a date for exactly the `fact_sources` entries that
  are not `osm`, none later than `updated`, and that `last_confirmed` is not
  in the future (it may be later than `updated`);
- that `standing` and `moderator` appear only on a place a traveller added
  (`osm: null`), and `pets_months` only with `pets: "seasonal"`;
- that no two records share an id or an OSM object (counting `merged_osm`,
  leaving tombstones out), and that a tombstone has a `merged_into` naming a
  record that is not itself merged, and no reviews;
- the owner rules: a value with the source `owner` needs
  `owner_verified: true`; a `farm_stay` with `osm: null` has
  `entry: private`, `booking_required: true` and a `booking_url` or `phone`;
  a `description` is checked like review text; `reopens` is a real date and
  only goes with `status: temporarily_closed`;
- that review lines are canonical and sorted by id, that each review's place
  exists, and that each `rating` has the exact count and rounded mean of its
  visible reviews (no `hidden`, no blocked `contributor`);
- that there is no `fees/` folder: what travellers paid is no longer
  collected;
- that each JSON file in `moderation/` matches its schema and is sorted
  with no repeats, that the pairs in `distinct_places.json` and
  `commons-do-not-match.json` name places that exist, that `moderation/`
  holds nothing but those files and the CSV files the pipeline writes, and
  that no review line has an id on `moderation/removed_reviews.json`;
- that review text has no email address, phone number, link, social media
  handle or obvious personal name.

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

### Database and changesets

`python -m pipeline.database --release-dir dist` builds `ocdb.sqlite.gz`, the
release as an SQLite database, from the files `pipeline.release` wrote, so it
holds exactly what they hold, plus each place's state and the `localities`
table, read from `localities/localities.ndjson` in `--root` with each town's
state worked out the same way. Each `--previous`
folder holds an earlier release's `manifest.json` and `ocdb.sqlite.gz`; for
each one, it writes a changeset that turns that database into this one. The
release workflow passes the release just before as `--previous` and the last
release of the previous UTC day as `--day`. The manifest's `changesets` list
carries the list of the release just before forward, with this release's new
changesets, trimmed to the changesets into the newest 50 releases
(`CHAIN_RELEASES`), so an app finds its whole path from the newest manifest.
Each entry's `url` stays on the release that made it, and the release
workflow keeps 60 releases, so every listed file is still there.

An earlier release is a download, so its list is checked before it is
carried: an entry stays only when its file is the name the module gives a
changeset from its `from`, its `url` is this repository's download url for
that file in the release its `to` names, and its `to` is not later than the
release just before. Anything else is left out and logged. The release
workflow also picks the earlier releases only from releases it published
itself (author `github-actions[bot]`, not a draft), made before this one,
whose tag is on `main`'s history, so a release or tag made another way is
never read. The layout, the changesets and the
manifest entries are described in [CONNECTING.md](../CONNECTING.md), section 3.

SQLite comes from apsw (in `requirements.txt`), whose wheels bundle SQLite
built with the session extension. A changeset is what
`sqlite3session_diff` gives for every table (apsw's `Session.diff`), in the
standard changeset format any SQLite with the session extension applies.
Before a changeset is written it is applied to a copy of the earlier
database, with any conflict aborting, and every table is compared row by row
with the new database. A difference fails the release.

A changeset holds the old values of every row it deletes, a review's text
among them, so a changeset from an earlier database is written only when
every review of it can go out again. A review still in this release (as it
was, or edited) can. A review this release dropped can only when the same
contributor replaced it: its id is not on `moderation/removed_reviews.json`
under `--root` (the data repository, `.` by default), and this release holds
a review by that contributor of that place that the earlier database does
not, made after the one that left. Any other review that left stops the
changeset: one removed with `remove`, hidden by a person or by reports, from
a blocked contributor, or erased, and one whose contributor's other review
of the place is older or was already there. A review on the removed list
stops it even when the contributor wrote a new one. Apps on that release find
no path in the list and download the whole database. The release still has
changesets from any other earlier database passed in.

The build is deterministic: rows go in in key order, indexes are made after
the rows, the page size is 4096 and the file is written with `VACUUM INTO`,
and the SQLite version is the one apsw pins. The same release gives a
byte-identical database, gzipped like the other files.

`LAYOUT_VERSION` in `pipeline/database.py` is the version of the tables. Raise
it by hand whenever a table, column or index changes, and say so in
CONNECTING.md. No changeset is made from a database with another layout, so
apps download the whole database once and can refuse a layout they cannot
read.

Each place's `state` comes from `pipeline.states`, over OpenStreetMap's
`boundary=administrative`, `admin_level=4` relations in Australia (relation
80500), in `boundaries/au-states.geojson`. Each code is the relation's `ref`
tag. The three relations with no `ref` take the codes in `NO_REF_CODES`:
`NF` for Norfolk Island (its `ISO3166-1` tag) and the FIPS 10-4 codes `CR` and
`AT` for the Coral Sea Islands and Ashmore and Cartier Islands, which have no
ISO code.

OSM's outlines take in the coastal waters, so only places well offshore,
such as reef pontoons, fall outside them. There is no snapping to the
nearest state: a place outside every outline has no state. The outlines are
not simplified, because the land borders follow rivers and simplifying them
moves places on the riverbanks into the other state.

`python -m pipeline.states --fetch` rebuilds the file from Overpass, and
`--overpass <response.json>` from a saved response.

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

The URL must be https with a host, and no user, query or fragment. It refuses
any other URL before sending anything, and it does not follow a redirect, so
the token reaches only the URL given. `--configured-url <url>` also requires
the host of that URL, and `--check` prints the checked base URL and sends
nothing. The release workflow checks the address this way before any step holds
the token: a manual run's `intake_url` input must be https on the host of the
`INTAKE_URL` variable.
