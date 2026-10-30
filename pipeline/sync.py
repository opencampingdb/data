"""Merge an extract into places/ and write a sync report.

Usage:
    python -m pipeline.sync extract.ndjson --root . \
        --report sync-report.md --summary sync-summary.json

Rules (SPEC, "OSM sync"):
- A record is matched to an OSM object by osm.type + osm.id.
- An OSM object with no record gets a new record and a new id, ocdb_<ULID>.
- OSM-sourced facts, name and location are refreshed. Any of them whose
  fact_sources entry is "feedback" is kept as it is. A record with no
  fact_sources entry for name or location takes both from OSM.
- A record whose OSM object has gone is never deleted. It is listed in the
  report as "gone from OSM" for a person to decide. Records a person has
  already set to "closed" are no longer listed.
- status and rating come from feedback, never from OSM, so sync leaves them alone.
- kind is refreshed from the extract, and group follows kind. A kind change
  counts as an update.
- Records still carrying a kind from before the OTWD vocabulary (camp_site,
  toilets and so on) are moved to the new kind even when their OSM object is
  gone, so that every record validates. See LEGACY_KINDS.
- "updated" moves only when the record's content changes, so running the
  same extract twice changes nothing.
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
    load_places,
    place_path,
    utc_now,
    write_text,
)

# Lists in the report are cut at this length so the PR body stays under
# GitHub's 65,536 character limit.
REPORT_LIST_LIMIT = 200

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

    Returns (facts, fact_sources). Feedback-sourced facts win. Every other
    fact is whatever OSM says now, so a tag removed in OSM removes its fact.
    """
    old_facts = record.get("facts", {})
    old_sources = record.get("fact_sources", {})

    facts, sources = {}, {}
    for key, source in old_sources.items():
        if key in EXTRA_SOURCE_KEYS:
            continue  # name and location are handled in refreshed_record
        if source == "feedback" and key in old_facts:
            facts[key] = old_facts[key]
            sources[key] = "feedback"
    for key, value in osm_facts.items():
        if key not in sources:
            facts[key] = value
            sources[key] = "osm"
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
    """A brand new record for an OSM object we have not seen before."""
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


def refreshed_record(record: dict, place: dict, now: str) -> dict:
    """The record with OSM data refreshed. "updated" moves only if something changed."""
    fresh = copy.deepcopy(record)
    old_sources = record.get("fact_sources", {})
    fresh["kind"] = place["kind"]
    fresh["group"] = KIND_GROUP[place["kind"]]
    fresh["osm"] = place["osm"]
    fresh["facts"], fresh["fact_sources"] = merge_facts(record, place["facts"])
    # name and location follow OSM unless feedback set them. An explicit
    # "osm" or "feedback" entry is carried over as it was.
    for key in EXTRA_SOURCE_KEYS:
        if key in old_sources:
            fresh["fact_sources"][key] = old_sources[key]
        if old_sources.get(key) != "feedback":
            fresh[key] = place[key]
    if fresh != record:
        fresh["updated"] = now
    return fresh


def sync(root: Path, extract: list[dict], now: str | None = None, make_id=new_place_id) -> SyncResult:
    """Merge extract records into root/places. Returns what changed."""
    now = now or utc_now()
    places = load_places(root)
    result = SyncResult(records_before=len(places))

    by_osm = {osm_key(r["osm"]): r for r in places.values() if r.get("osm")}
    seen = set()

    for place in extract:
        key = osm_key(place["osm"])
        seen.add(key)
        existing = by_osm.get(key)
        if existing is None:
            record = new_record(place, now, make_id)
            result.new.append(record)
        else:
            record = refreshed_record(existing, place, now)
            if record == existing:
                result.unchanged += 1
                continue
            result.updated.append(record)
        write_text(place_path(root, record["id"]), dump_record(record))

    # Records not refreshed from OSM: those gone from OSM and those that never
    # came from it. They keep everything except an old kind, which moves to
    # the new vocabulary.
    updated_ids = {r["id"] for r in result.updated}
    for record in sorted(places.values(), key=lambda r: r["id"]):
        osm = record.get("osm")
        if osm and osm_key(osm) in seen:
            continue
        current = migrated_record(record, now)
        if current != record and current["id"] not in updated_ids:
            write_text(place_path(root, current["id"]), dump_record(current))
            result.updated.append(current)
        if osm and current.get("status") != "closed":
            result.gone.append(current)
    result.gone.sort(key=lambda r: osm_key(r["osm"]))

    result.records_after = result.records_before + len(result.new)
    return result


def escape_cell(text: str | None) -> str:
    """Make OSM text safe to show in a Markdown table cell in a PR body.

    Pipes would break the table, backticks the code span, and an @ could
    notify a GitHub user, so all three are neutralised.
    """
    if text is None:
        return "(unnamed)"
    text = text.replace("\\", "\\\\").replace("|", "\\|").replace("`", "'")
    text = text.replace("@", "(at)")
    return text.replace("\n", " ").replace("\r", " ")


def osm_link(osm: dict | None) -> str:
    if not osm:
        return "(not from OSM)"
    ref = f"{osm['type']}/{osm['id']}"
    return f"[{ref}](https://www.openstreetmap.org/{ref})"


def table(records: list[dict]) -> list[str]:
    lines = ["| Record | Kind | Name | OSM object |", "|---|---|---|---|"]
    for record in records[:REPORT_LIST_LIMIT]:
        lines.append(
            f"| `{record['id']}` | {record['kind']} | {escape_cell(record['name'])} "
            f"| {osm_link(record.get('osm'))} |"
        )
    if len(records) > REPORT_LIST_LIMIT:
        lines.append("")
        lines.append(f"And {len(records) - REPORT_LIST_LIMIT} more, not listed here.")
    return lines


def render_report(result: SyncResult, source: str | None = None) -> str:
    """The sync report as Markdown, used as the PR body."""
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
            *table(result.gone),
        ]
    else:
        lines.append("None.")
    lines += ["", "## New", ""]
    lines += table(result.new) if result.new else ["None."]
    lines += ["", "## Updated", ""]
    lines += table(result.updated) if result.updated else ["None."]
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
    args = parser.parse_args(argv)

    result = sync(args.root, load_extract(args.extract))
    write_text(args.report, render_report(result, args.source))

    summary = {
        "new": len(result.new),
        "updated": len(result.updated),
        "gone": len(result.gone),
        "unchanged": result.unchanged,
        "records_before": result.records_before,
        "records_after": result.records_after,
        "change_percent": result.change_percent,
        "auto_merge": result.change_percent < AUTO_MERGE_BELOW_PERCENT,
    }
    if args.summary:
        write_text(args.summary, json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, sort_keys=True), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
