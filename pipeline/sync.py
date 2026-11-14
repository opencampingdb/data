"""Merge an extract into places/ and write a sync report.

Usage:
    python -m pipeline.sync extract.ndjson --root . \
        --report sync-report.md --summary sync-summary.json

Rules (SPEC, "OSM sync"):
- A record is matched to an OSM object by osm.type + osm.id.
- An OSM object with no record gets a new record and a new id, ocdb_<ULID>.
- OSM-sourced facts, name and location are refreshed. Any of them whose
  fact_sources entry is not "osm" (feedback, contributed, owner or
  community_report) is kept as it is, and keeps its source: a value from
  community reports never becomes an OSM value. Sync never overwrites a value whose source is owner. A record with no
  fact_sources entry for name or location takes both from OSM.
- A record whose OSM object has gone is never deleted. It is listed in the
  report as "gone from OSM" for a person to decide. Records a person has
  already set to "closed" are no longer listed.
- status, reopens, description, owner_verified and rating never come from
  OSM, so sync leaves them alone, with their fact_sources entries.
- A record without an OSM link (osm is null) is never touched, flagged or
  deleted. Sync does not even read it beyond its id.
- A merged record (a tombstone, status "merged") is never touched or
  revived. An OSM object listed in a record's merged_osm belongs to that
  record: it is never made into a new record, and it does not change the
  record (which follows its own osm link).
- After the merge into places/, likely duplicates are listed in the report
  (pipeline.duplicates), leaving out pairs on the distinct list and pairs
  the rules in pipeline.geometry find to be separate places (with
  --geometry, the sidecar pipeline.extract wrote). Sync never merges them.
- kind is refreshed from the extract, and group follows kind. A kind change
  counts as an update.
- Records still carrying a kind from before the OTWD vocabulary (camp_site,
  toilets and so on) are moved to the new kind even when their OSM object is
  gone, so that every record validates. See LEGACY_KINDS.
- osm.timestamp is the time of the OSM object's current version. An extract
  without timestamps keeps the record's timestamp while the version is the
  same. Adding a timestamp to a record that had none does not move
  "updated", since the place itself did not change.
- fact_dates follows fact_sources: a date is kept for every value that is
  not from OSM. Sync never adds one, because it never adds such a value.
- last_confirmed is kept as it is. Only the feedback engine sets it.
- "updated" moves only when the record's content changes, so running the
  same extract twice changes nothing.
- A new or refreshed record that schema/place.schema.json refuses is not
  written. A new one gets no file, and a refreshed one keeps its file as it
  was. The report lists it under "Skipped" with the reasons, so one OSM value
  costs one record and the rest of the sync goes ahead.
"""

import argparse
import copy
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ulid import ULID

from .common import (
    EXTRA_SOURCE_KEYS,
    KIND_GROUP,
    dump_record,
    load_distinct,
    load_places,
    place_path,
    utc_now,
    write_text,
)
from .duplicates import find_duplicates
from .geometry import Geometry, load_geometry
from .validate import load_validator

# Lists in the report are cut at this length. When four full lists would
# still pass MAX_REPORT_CHARS (a sync that touches nearly every record),
# render_report cuts them shorter, so the PR body stays under GitHub's 65,536
# character limit.
REPORT_LIST_LIMIT = 200
MAX_REPORT_CHARS = 60000

# The sync PR merges itself only below this change percentage (SPEC).
AUTO_MERGE_BELOW_PERCENT = 2.0

# Kinds used before the OTWD vocabulary, and the kind each one becomes when
# the record is not refreshed from OSM. camp_site is decided by the fee fact.
LEGACY_KINDS = {
    "camp_site": None,
    "caravan_site": "caravan_park",
    "rest_area": "rest_area",
    "dump_station": "dump_point",
    "drinking_water": "drinking_water_point",
    "toilets": "public_toilet",
    "shower": "public_shower",
    "picnic_site": "picnic_site",
}


@dataclass
class SyncResult:
    """What one sync run did."""

    new: list[dict] = field(default_factory=list)
    updated: list[dict] = field(default_factory=list)
    gone: list[dict] = field(default_factory=list)
    duplicates: list[dict] = field(default_factory=list)
    # Records the place schema refuses, each {"record": ..., "errors": [...]}.
    skipped: list[dict] = field(default_factory=list)
    unchanged: int = 0
    records_before: int = 0
    records_after: int = 0

    @property
    def change_percent(self) -> float:
        """New, updated and gone records as a percentage of all records after the sync.

        A record that is both updated and gone (an old record migrated to the
        new kinds) counts once.
        """
        changed = len({r["id"] for r in self.new + self.updated + self.gone})
        if changed == 0:
            return 0.0
        return round(100.0 * changed / max(self.records_after, 1), 2)


def new_place_id() -> str:
    return f"ocdb_{ULID()}"


def osm_key(osm: dict) -> tuple[str, int]:
    return osm["type"], osm["id"]


def merge_facts(record: dict, osm_facts: dict) -> tuple[dict, dict]:
    """Combine a record's feedback facts with fresh OSM facts.

    Returns (facts, fact_sources). Facts from feedback or contributions win. Every other
    fact is whatever OSM says now, so a tag removed in OSM removes its fact.
    """
    old_facts = record.get("facts", {})
    old_sources = record.get("fact_sources", {})

    facts, sources = {}, {}
    for key, source in old_sources.items():
        if key in EXTRA_SOURCE_KEYS:
            continue  # name and location are handled in refreshed_record
        if source != "osm" and key in old_facts:
            facts[key] = old_facts[key]
            sources[key] = source
    for key, value in osm_facts.items():
        if key not in sources:
            facts[key] = value
            sources[key] = "osm"
    # OSM's dog months belong to OSM's seasonal pets. When another source set
    # pets to something else, the months no longer apply.
    if facts.get("pets") != "seasonal" and sources.get("pets_months") == "osm":
        del facts["pets_months"], sources["pets_months"]
    return facts, sources


def migrated_kind(record: dict) -> str:
    """The new kind for a record that still has a kind from before the OTWD vocabulary."""
    kind = LEGACY_KINDS[record["kind"]]
    if kind is not None:
        return kind
    fee = record.get("facts", {}).get("fee")
    if fee is False:
        return "free_camp"
    if fee is True:
        return "paid_campground"
    return "campground"


def migrated_record(record: dict, now: str) -> dict:
    """The record moved to the new kinds, with its group. Unchanged if already current."""
    fresh = copy.deepcopy(record)
    if fresh["kind"] in LEGACY_KINDS and fresh["kind"] not in KIND_GROUP:
        fresh["kind"] = migrated_kind(fresh)
    fresh["group"] = KIND_GROUP[fresh["kind"]]
    if fresh != record:
        fresh["updated"] = now
    return fresh


def new_record(place: dict, now: str, make_id) -> dict:
    """A brand new record for an OSM object we have not seen before.

    Every value is from OSM, so it has no fact_dates.
    """
    return {
        "id": make_id(),
        "name": place["name"],
        "kind": place["kind"],
        "group": KIND_GROUP[place["kind"]],
        "location": place["location"],
        "osm": place["osm"],
        "facts": dict(place["facts"]),
        "status": "open",
        "rating": None,
        "fact_sources": {key: "osm" for key in place["facts"]},
        "updated": now,
    }


def without_osm_timestamp(record: dict) -> dict:
    """The record without osm.timestamp, to tell a timestamp backfill from a change."""
    if not record.get("osm") or "timestamp" not in record["osm"]:
        return record
    bare = dict(record, osm=dict(record["osm"]))
    del bare["osm"]["timestamp"]
    return bare


def refreshed_osm(old: dict | None, new: dict) -> dict:
    """The extract's osm link, keeping the old timestamp if the extract has none for this version."""
    fresh = dict(new)
    if (
        "timestamp" not in fresh
        and old
        and "timestamp" in old
        and old.get("version") == fresh["version"]
    ):
        fresh["timestamp"] = old["timestamp"]
    return fresh


def refreshed_record(record: dict, place: dict, now: str) -> dict:
    """The record with OSM data refreshed. "updated" moves only if something changed.

    A timestamp added to an osm link that had none is not a change to the
    place, so it alone does not move "updated".
    """
    fresh = copy.deepcopy(record)
    old_sources = record.get("fact_sources", {})
    # A kind set by feedback, a contribution or the owner wins over OSM's.
    if old_sources.get("kind", "osm") == "osm":
        fresh["kind"] = place["kind"]
        fresh["group"] = KIND_GROUP[place["kind"]]
    fresh["osm"] = refreshed_osm(record.get("osm"), place["osm"])
    fresh["facts"], fresh["fact_sources"] = merge_facts(record, place["facts"])
    # Source entries for name, location, kind and status are carried over as
    # they were. name, location and kind follow OSM unless their source is
    # something else (feedback, contributed, owner or community_report). status never comes
    # from OSM.
    for key in EXTRA_SOURCE_KEYS:
        if key in old_sources:
            fresh["fact_sources"][key] = old_sources[key]
    for key in ("name", "location"):
        if old_sources.get(key, "osm") == "osm":
            fresh[key] = place[key]
    dates = {
        key: when
        for key, when in record.get("fact_dates", {}).items()
        if fresh["fact_sources"].get(key, "osm") != "osm"
    }
    fresh.pop("fact_dates", None)
    if dates:
        fresh["fact_dates"] = dates
    if without_osm_timestamp(fresh) != without_osm_timestamp(record):
        fresh["updated"] = now
    return fresh


# Words for the schema rules a refused record breaks. The report names the
# field and the rule, never the value, since the value is OSM text.
SCHEMA_RULE_WORDS = {
    "maxLength": "too long",
    "minLength": "too short",
    "pattern": "not in the form the schema allows",
    "enum": "not one of the allowed values",
    "type": "of the wrong type",
    "additionalProperties": "has a field the schema does not allow",
    "required": "is missing a required field",
}


def schema_reasons(validator, record: dict) -> list[str]:
    """Why the place schema refuses record, one line per error, empty when it passes."""
    reasons = set()
    for error in validator.iter_errors(record):
        path = "/".join(str(p) for p in error.absolute_path) or "(top level)"
        reasons.add(f"{path} {SCHEMA_RULE_WORDS.get(error.validator, 'refused by the schema')}")
    return sorted(reasons)


def sync(
    root: Path, extract: list[dict], now: str | None = None, make_id=new_place_id, geometry: Geometry | None = None
) -> SyncResult:
    """Merge extract records into root/places. Returns what changed."""
    now = now or utc_now()
    places = load_places(root)
    result = SyncResult(records_before=len(places))
    validator = load_validator("place.schema.json")

    live = [r for r in places.values() if r.get("status") != "merged"]
    by_osm = {osm_key(r["osm"]): r for r in live if r.get("osm")}
    merged_osm = {
        (m["type"], m["id"]): r["id"] for r in live for m in r.get("merged_osm", [])
    }
    seen = set()

    for place in extract:
        key = osm_key(place["osm"])
        seen.add(key)
        if key in merged_osm and key not in by_osm:
            continue  # merged into another record by a person
        existing = by_osm.get(key)
        if existing is None:
            record = new_record(place, now, make_id)
        else:
            record = refreshed_record(existing, place, now)
            if record == existing:
                result.unchanged += 1
                continue
        errors = schema_reasons(validator, record)
        if errors:
            result.skipped.append({"record": record, "errors": errors})
            continue
        (result.new if existing is None else result.updated).append(record)
        write_text(place_path(root, record["id"]), dump_record(record))

    # Records gone from OSM keep everything except an old kind, which moves
    # to the new vocabulary. Records without an OSM link are left alone.
    updated_ids = {r["id"] for r in result.updated}
    for record in sorted(live, key=lambda r: r["id"]):
        osm = record.get("osm")
        if not osm or osm_key(osm) in seen:
            continue
        current = migrated_record(record, now)
        if current != record and current["id"] not in updated_ids:
            write_text(place_path(root, current["id"]), dump_record(current))
            result.updated.append(current)
        if current.get("status") != "closed":
            result.gone.append(current)
    result.gone.sort(key=lambda r: osm_key(r["osm"]))

    result.records_after = result.records_before + len(result.new)
    result.duplicates = find_duplicates(list(load_places(root).values()), load_distinct(root), geometry)
    return result


def escape_cell(text: str | None) -> str:
    """Make OSM text safe to show in a Markdown table cell in a PR body.

    Pipes would break the table, backticks the code span, an @ could
    notify a GitHub user and square brackets could make a link, so all four
    are neutralised.
    """
    if text is None:
        return "(unnamed)"
    text = text.replace("\\", "\\\\").replace("|", "\\|").replace("`", "'")
    text = text.replace("[", "\\[").replace("]", "\\]")
    text = text.replace("@", "(at)")
    return text.replace("\n", " ").replace("\r", " ")


def osm_link(osm: dict | None) -> str:
    if not osm:
        return "(not from OSM)"
    ref = f"{osm['type']}/{osm['id']}"
    return f"[{ref}](https://www.openstreetmap.org/{ref})"


def table(records: list[dict], limit: int = REPORT_LIST_LIMIT) -> list[str]:
    lines = ["| Record | Kind | Name | OSM object |", "|---|---|---|---|"]
    for record in records[:limit]:
        lines.append(
            f"| `{record['id']}` | {record['kind']} | {escape_cell(record['name'])} "
            f"| {osm_link(record.get('osm'))} |"
        )
    if len(records) > limit:
        lines.append("")
        lines.append(f"And {len(records) - limit} more, not listed here.")
    return lines


def skipped_table(skipped: list[dict], limit: int = REPORT_LIST_LIMIT) -> list[str]:
    lines = [
        "The place schema refuses these OSM objects as extracted, so their records "
        "were not written. A record that already existed is left as it was. Fix the "
        "value in OSM or the extract.",
        "",
        "| OSM object | Name | Reasons |",
        "|---|---|---|",
    ]
    for item in skipped[:limit]:
        record = item["record"]
        reasons = "; ".join(item["errors"])
        if len(reasons) > 300:
            reasons = reasons[:300] + "..."
        lines.append(f"| {osm_link(record.get('osm'))} | {escape_cell(record['name'])} | {escape_cell(reasons)} |")
    if len(skipped) > limit:
        lines.append("")
        lines.append(f"And {len(skipped) - limit} more, not listed here.")
    return lines


def duplicates_table(pairs: list[dict], limit: int = REPORT_LIST_LIMIT) -> list[str]:
    lines = [
        "Pairs that are likely the same place. Nothing was merged. To merge, keep one "
        "and run `python -m pipeline.merge <keep_id> <drop_id>` in a separate PR.",
        "",
        "| Record A | Name A | Kind A | Record B | Name B | Kind B | Distance |",
        "|---|---|---|---|---|---|---:|",
    ]
    for pair in pairs[:limit]:
        a, b = pair["a"], pair["b"]
        lines.append(
            f"| `{a['id']}` | {escape_cell(a['name'])} | {a['kind']} "
            f"| `{b['id']}` | {escape_cell(b['name'])} | {b['kind']} | {pair['distance_m']:.0f} m |"
        )
    if len(pairs) > limit:
        lines.append("")
        lines.append(f"And {len(pairs) - limit} more, not listed here.")
    return lines


def render_report(result: SyncResult, source: str | None = None) -> str:
    """The sync report as Markdown, used as the PR body, at most MAX_REPORT_CHARS long.

    Lists are cut at REPORT_LIST_LIMIT rows, and shorter when the report
    would still be too long.
    """
    limit = REPORT_LIST_LIMIT
    while True:
        report = _render_report(result, source, limit)
        if len(report) <= MAX_REPORT_CHARS or limit == 0:
            return report
        limit //= 2


def _render_report(result: SyncResult, source: str | None, limit: int) -> str:
    auto = result.change_percent < AUTO_MERGE_BELOW_PERCENT
    lines = ["# OSM sync report", ""]
    if source:
        lines += [f"Source: {escape_cell(source)}", ""]
    lines += [
        "| | Records |",
        "|---|---:|",
        f"| Before | {result.records_before} |",
        f"| New | {len(result.new)} |",
        f"| Updated | {len(result.updated)} |",
        f"| Unchanged | {result.unchanged} |",
        f"| Gone from OSM | {len(result.gone)} |",
        f"| Skipped | {len(result.skipped)} |",
        f"| After | {result.records_after} |",
        "",
        f"Change: {result.change_percent:.2f}% of records "
        f"(new, updated and gone, out of all records after the sync).",
        "",
        (
            f"This is under {AUTO_MERGE_BELOW_PERCENT:g}%, so the PR merges itself once CI passes."
            if auto
            else f"This is {AUTO_MERGE_BELOW_PERCENT:g}% or more, so the PR waits for a person."
        ),
        "",
        "## Gone from OSM",
        "",
    ]
    if result.gone:
        lines += [
            "These records are kept. A person decides for each one: set `status` to "
            "`closed`, fix it in OSM, or remove the file in a separate PR.",
            "",
            *table(result.gone, limit),
        ]
    else:
        lines.append("None.")
    lines += ["", "## Skipped", ""]
    lines += skipped_table(result.skipped, limit) if result.skipped else ["None."]
    lines += ["", "## Possible duplicates", ""]
    lines += duplicates_table(result.duplicates, limit) if result.duplicates else ["None."]
    lines += ["", "## New", ""]
    lines += table(result.new, limit) if result.new else ["None."]
    lines += ["", "## Updated", ""]
    lines += table(result.updated, limit) if result.updated else ["None."]
    return "\n".join(lines) + "\n"


def load_extract(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("extract", type=Path, help="NDJSON written by pipeline.extract")
    parser.add_argument("--root", type=Path, default=Path("."), help="data repository root")
    parser.add_argument("--report", type=Path, default=Path("sync-report.md"))
    parser.add_argument("--summary", type=Path, help="also write counts as JSON here")
    parser.add_argument("--source", help="describes the OSM file, shown in the report")
    parser.add_argument("--geometry", type=Path, help="geometry sidecar written by pipeline.extract --geometry, for the duplicate rules")
    args = parser.parse_args(argv)

    result = sync(args.root, load_extract(args.extract), geometry=load_geometry(args.geometry))
    write_text(args.report, render_report(result, args.source))

    summary = {
        "new": len(result.new),
        "updated": len(result.updated),
        "gone": len(result.gone),
        "skipped": len(result.skipped),
        "unchanged": result.unchanged,
        "records_before": result.records_before,
        "records_after": result.records_after,
        "change_percent": result.change_percent,
        "auto_merge": result.change_percent < AUTO_MERGE_BELOW_PERCENT,
        "possible_duplicates": len(result.duplicates),
    }
    if args.summary:
        write_text(args.summary, json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, sort_keys=True), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
