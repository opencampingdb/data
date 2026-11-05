# MapRoulette challenges

Open Camping DB passes what OpenStreetMap (OSM) should hear about to OSM's mappers as [MapRoulette](https://maproulette.org) challenges. A challenge is a list of small tasks on a map. Mappers choose to take a task, check the place and edit OSM themselves if they agree. Open Camping DB never edits OSM.

| File | Challenge | One task per |
|---|---|---|
| `double_mapping.geojson` | Camp sites mapped twice | place that OSM still maps as two or more objects, after its records were merged, leaving out objects that are a separate place (see below), and leaving out motels, cabins and hostels |
| `traveller_reports.geojson` | What travellers report | place where a value that at least two travellers or the verified owner reported still differs from OSM |
| `challenges.json` | | the MapRoulette project and challenge ids, used for the rebuild links |

`python -m pipeline.maproulette` writes both files during the nightly OSM sync. Community reports (`community_report` in `fact_sources`) never become tasks, and neither does a place whose name or location came from them. Facts the release sets by rule (`rule` in `fact_sources`, such as `toilets` on a public toilet) are never in `places/`, so they never become tasks either. [pipeline/maproulette.py](../pipeline/maproulette.py) has the rules. Tasks go away by themselves: a double mapping task once OSM keeps one object, a report task once OSM shows the reported value.

### What a report task needs

A value in Open Camping DB is not enough on its own, because the feedback engine can accept a single attested report. A report task is built only from the evidence in the feedback inbox's `applied/` folder, which the sync reads with `--inbox`:

- A value from travellers (`feedback`) needs at least 2 independent sources (a distinct `origin`, or `device` when there is no origin) that reported that same value for the place in the last 90 days, in items the engine applied.
- Those confirming sources must also outnumber the sources that doubted the fact (said it is wrong without giving a value) in the same 90 days by at least 2. Three confirmations and one doubt make a task, two and one do not. A doubt counts whether the engine applied it or held it for a reviewer, and stops counting if it was refused. Each source counts once on each side, and doubts never become tasks.
- A value from the owner (`owner`) needs the verified owner's applied update. One is enough, and doubts do not change that.
- A name that only describes the place is never sent: every word is a generic place word (rest area, heavy vehicle, truck, parking, camp, camping, caravan park, dump point, toilets, picnic area, reserve, lookout, free camp, roadside stop and similar), or the name is a kind's label. "Heavy Vehicle Rest Area" is one.
- Without `--inbox` (a local run, a fork) the file is written with no tasks, and the log and sync report say why.

Each line of a task says how many travellers reported the value and when the latest did, or when the owner set it, and how to check it: a survey or street-level photos (Mapillary, Panoramax) of the access road for road access; signage or a survey for a name; the operator's own sign or website for fees, opening hours and contacts; a survey, street-level photos or recent imagery for toilets, water, showers and dump points. Only counts and dates leave the inbox. Device ids, origins and travellers' words never reach a task.

## Approving changes

1. **The sync PR.** Each night's OSM sync PR lists, under "MapRoulette", the tasks each file gains and loses. Merging the PR approves them. Small syncs merge on their own, which is fine: every task comes from a change already approved, a merge a person made, a batch of automatic merges a person reviewed and merged (each listed in `moderation/automatic-merges.csv`), or feedback the apply rules accepted.
2. **The rebuild issue.** When a merge changes the files on main, the MapRoulette workflow opens an issue called "Rebuild the MapRoulette challenges" with a link to each challenge. MapRoulette has no API for a workflow to rebuild, so a person presses the button.
3. **Rebuild.** Sign in to MapRoulette with your OSM account, open the link, press **Rebuild Tasks**, tick **First remove incomplete tasks** and press **Proceed**. That removes the tasks nobody has finished and builds them again from the file, so a task that left the file goes. Finished tasks, "Not an Issue" among them, stay. Tick the box in the issue, and close it when every box is ticked.

   A file with no tasks can't be built, so the issue ticks it for you. Tasks already in that challenge stay until the file has tasks again.

Anyone who is an admin of the MapRoulette project can rebuild. Add people under the project's **Manage** page.

### Not double mapping

Two OSM objects close together are not always one place mapped twice. Rest areas, truck bays and fuel stations often come in pairs, one for each direction of travel, with the same name either side of a highway. The rules in [pipeline/geometry.py](../pipeline/geometry.py) (described in [pipeline/README.md](../pipeline/README.md), "Opposite sides of a road") decide which:

- An object inside an area of its place, on its edge, or within 20 m of the edge with no major road between, is the same place as the area. Those stay tasks.
- Two objects within 200 m with a major road between them are separate places: motorway, trunk, primary or secondary roads and their links, and one way tertiary roads. They never become a task, and are never merged.
- Different names, directions (northbound and southbound), operators or access keep two objects apart too. These signals only ever keep places apart.

A task leaves out any object the rules find to be a separate place, and goes when one object is left.

The challenge is about camp sites. A record whose kind is `roofed_accommodation` or `backpacker_hostel` (motels, cabins, hostels) is never a task, and an automatic merge of a motel or hostel into the caravan park it stands in marks the motel as a part, since OSM maps the two as separate features on purpose.

To keep a pair out of the double mapping challenge, merge it with `python -m pipeline.merge <keep> <drop> --part`, which marks the dropped object as a part drawn apart on purpose (`"part": true` in `merged_osm`). To keep two places from being merged at all, add them to `moderation/distinct_places.json`. `pipeline.merge` refuses a pair on that list unless `--part` says they are parts of one place, such as two sections of a caravan park on either side of a street.

## Setting up a challenge

Both challenges live in the MapRoulette project "Open Camping DB". Each one is built from a remote GeoJSON URL on main:

- `https://raw.githubusercontent.com/opencampingdb/data/main/maproulette/double_mapping.geojson`
- `https://raw.githubusercontent.com/opencampingdb/data/main/maproulette/traveller_reports.geojson`

Every feature carries `id` (the Open Camping DB id, which matches a task across rebuilds), `@id` (the OSM object, such as `node/123`), `name` and `record` (the place file). Double mapping tasks add `objects`, and report tasks add `changes` and `ocdb_id` (the Open Camping DB id again, for mappers to cite). Challenge instructions use them as `{{name}}` and so on.

**Camp sites mapped twice**, instructions:

```text
{{name}} is mapped in OpenStreetMap more than once: {{objects}}. Open Camping DB merged these into one place: {{record}}

Check the imagery and any survey you have. If they are the same place, merge them into one object and keep the tags from both. If they really are separate places, mark the task "Not an Issue".
```

**What travellers report**, instructions:

```text
Open Camping DB, an open list of places to camp and stop in Australia, has these reports about {{name}} that OpenStreetMap does not show yet. Each comes from at least two travellers who reported the same thing in the last 90 days, or from the place's verified owner:

{{changes}}

Open Camping DB record {{ocdb_id}}: {{record}}

Check each report with the method listed under it before you edit. If you cannot confirm a report, skip the task. If Open Camping DB is wrong, mark the task "Not an Issue". If you edit, you can mention Open Camping DB {{ocdb_id}} in your changeset comment, with your own check as the source. Open Camping DB does not edit OpenStreetMap itself.
```

After creating a challenge, put its id in `challenges.json`, along with the project id. The project is "Open Camping DB" (66368). Its challenges are "Camp sites mapped twice" (57405) and "What travellers report" (57406). Both stay out of MapRoulette's listings until the project is made discoverable.
