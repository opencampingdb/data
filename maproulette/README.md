# MapRoulette challenges

Open Camping DB passes what OpenStreetMap (OSM) should hear about to OSM's mappers as [MapRoulette](https://maproulette.org) challenges. A challenge is a list of small tasks on a map. Mappers choose to take a task, check the place and edit OSM themselves if they agree. Open Camping DB never edits OSM.

| File | Challenge | One task per |
|---|---|---|
| `double_mapping.geojson` | Camp sites mapped twice | place that OSM still maps as two or more objects, after a person merged its records |
| `traveller_reports.geojson` | What travellers report | place where a value from travellers or the owner still differs from OSM |
| `challenges.json` | | the MapRoulette project and challenge ids, used for the rebuild links |

`python -m pipeline.maproulette` writes both files during the nightly OSM sync. [pipeline/maproulette.py](../pipeline/maproulette.py) has the rules. Tasks go away by themselves: a double mapping task once OSM keeps one object, a report task once OSM shows the reported value.

## Approving changes

1. **The sync PR.** Each night's OSM sync PR lists, under "MapRoulette", the tasks each file gains and loses. Merging the PR approves them. Small syncs merge on their own, which is fine: every task comes from a change already approved, a merge a person made or feedback the apply rules accepted.
2. **The rebuild issue.** When a merge changes the files on main, the MapRoulette workflow opens an issue called "Rebuild the MapRoulette challenges" with a link to each challenge. MapRoulette has no API for a workflow to rebuild, so a person presses the button.
3. **Rebuild.** Sign in to MapRoulette with your OSM account, open the link, press **Rebuild Tasks**, tick **First remove incomplete tasks** and press **Proceed**. That removes the tasks nobody has finished and builds them again from the file, so a task that left the file goes. Finished tasks, "Not an Issue" among them, stay. Tick the box in the issue, and close it when every box is ticked.

   A file with no tasks can't be built, so the issue ticks it for you. Tasks already in that challenge stay until the file has tasks again.

Anyone who is an admin of the MapRoulette project can rebuild. Add people under the project's **Manage** page.

To keep a pair out of the double mapping challenge, merge it with `python -m pipeline.merge <keep> <drop> --part`, which marks the dropped object as a part drawn apart on purpose (`"part": true` in `merged_osm`). To keep two places from being merged at all, add them to `moderation/distinct_places.json`.

## Setting up a challenge

Both challenges live in the MapRoulette project "Open Camping DB". Each one is built from a remote GeoJSON URL on main:

- `https://raw.githubusercontent.com/opencampingdb/data/main/maproulette/double_mapping.geojson`
- `https://raw.githubusercontent.com/opencampingdb/data/main/maproulette/traveller_reports.geojson`

Every feature carries `id` (the Open Camping DB id, which matches a task across rebuilds), `@id` (the OSM object, such as `node/123`), `name` and `record` (the place file). Double mapping tasks add `objects`, and report tasks add `changes`. Challenge instructions use them as `{{name}}` and so on.

**Camp sites mapped twice**, instructions:

```text
{{name}} is mapped in OpenStreetMap more than once: {{objects}}. Open Camping DB merged these into one place: {{record}}

Check the imagery and any survey you have. If they are the same place, merge them into one object and keep the tags from both. If they really are separate places, mark the task "Not an Issue".
```

**What travellers report**, instructions:

```text
Open Camping DB, an open list of places to camp and stop in Australia, has accepted these reports about {{name}} that OpenStreetMap does not show yet:

{{changes}}

Open Camping DB record: {{record}}

Please check before editing. If you cannot confirm a report, skip the task. Open Camping DB does not edit OpenStreetMap itself.
```

After creating a challenge, put its id in `challenges.json`, along with the project id. The project is "Open Camping DB" (66368). Its challenges are "Camp sites mapped twice" (57405) and "What travellers report" (57406). Both stay out of MapRoulette's listings until the project is made discoverable.
