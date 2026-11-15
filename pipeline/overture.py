"""The Overture importer: gap fills from Overture Maps places.

One run takes the place records, one Overture extract (tools/overture-pull,
format in pipeline/README.md "Overture extract"), the link file and the kept
lists, and returns the changed records, the gap fills, the values taken off,
the "Overture differs" list, the new link file and the Overture candidates
by tier. It does no network work. import_overture is the entry point. The
rules are in pipeline.overture_rules, social pages in
pipeline.overture_socials, candidates and decisions in
pipeline.overture_candidates (pipeline.overture_decide acts on reviewed
candidates), and the report in pipeline.overture_report.

    python -m pipeline.overture overture.ndjson --root . --geometry geometry.ndjson \\
        [--changelog] [--report overture-report.md] [--summary overture-summary.json] \\
        [--candidates overture-candidates.ndjson]

See pipeline/README.md "Overture import" for the rules.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .common import dump_line, dump_record, load_places, place_path, write_text
from .geometry import Geometry, load_geometry
from .overture_candidates import TIERS, find_candidates, without_shared_pages
from .overture_report import COUNTED, render_report
from .overture_rules import (
    OVERTURE_KINDS,
    SHARED_PAGE_LIMIT,
    UNCHANGED,
    KeptLists,
    differences,
    is_candidate,
    load_kept_lists,
    load_links,
    offered_pages,
    overture_place_for,
    overture_values,
    own_websites,
    page_key,
    read_json_lines,
    read_overture,
    strip_overture,
    taken_values,
    value_of,
    values_from,
    with_values,
    write_links,
)


from .overture_socials import all_social_pages
from .sync import AUTO_MERGE_BELOW_PERCENT


@dataclass
class ImportResult:
    """What one run found.

    records: every changed record, by id. fills: one entry per value taken or
    changed ({place, key, value, previous}). removals: one per Overture value
    taken off ({place, key, value}). differs: places with an Overture match
    or a name source where Overture's website or phone disagrees with OSM's.
    links: the new link file, place id to {gers, release}. candidates: Overture candidates by tier (TIERS).
    dropped: the camping places each junk rule dropped, by rule. shared: each
    shared page left out (SHARED_PAGE_LIMIT) and how many places it was given for.
    matches and name_sources: the places that got an Overture match or a name
    source in this run.
    """

    release: str
    records: dict[str, dict] = field(default_factory=dict)
    fills: list[dict] = field(default_factory=list)
    removals: list[dict] = field(default_factory=list)
    differs: list[dict] = field(default_factory=list)
    links: dict[str, dict] = field(default_factory=dict)
    candidates: dict[str, list[dict]] = field(default_factory=dict)
    dropped: dict[str, list[dict]] = field(default_factory=dict)
    shared: dict[str, int] = field(default_factory=dict)
    checked: int = 0
    matches: int = 0
    name_sources: int = 0


def import_overture(
    places: dict[str, dict],
    extract: Iterable[dict],
    links: dict[str, dict],
    lists: KeptLists,
    *,
    now: str,
    geometry: Geometry | None = None,
    changelog_only: bool = False,
) -> ImportResult:
    """Match places to Overture places and work out the gap fills.

    places: every record by id. extract: the Overture extract's lines, the
    header first. links: the link file as read, place id to {gers, release}.
    now: the run time, used for fact dates and updated. With changelog_only
    (spec decision 13), a linked place is checked again only when the
    changelog lists its Overture place as changed or removed: otherwise it
    keeps its values and link, unless overture/taken.json lists a value for
    it. A place with no link is matched only against
    Overture places the changelog lists as added or changed.
    """
    geometry = geometry or Geometry()
    overture = read_overture(
        extract, lists.categories, {link["gers"] for link in links.values()}, lists.candidate_kinds
    )
    result = ImportResult(release=overture.release)
    checks = []
    # The Overture places of linked places a changelog run does not check
    # again. Their pages still count towards SHARED_PAGE_LIMIT.
    unchecked = []
    # Places with a value taken in "Overture differs", always checked so a
    # value taken since the last run is written.
    with_taken = {place_id for place_id, _ in lists.taken}
    for place_id in sorted(places):
        record = places[place_id]
        if record.get("status") == "merged":
            continue
        link = links.get(place_id)
        if (
            changelog_only
            and link
            and record.get("kind") in OVERTURE_KINDS
            and overture.changes.get(link["gers"]) in UNCHANGED
            and place_id not in with_taken
        ):
            # The changelog says the Overture place has not changed: keep the
            # place as it is and note the link as seen in this release.
            result.links[place_id] = {"gers": link["gers"], "release": overture.release}
            if link["gers"] in overture.places:
                unchecked.append(overture.places[link["gers"]])
            continue
        base = strip_overture(record)
        row = None
        if is_candidate(record):
            # A published candidate takes values from the Overture place it came from.
            row = overture.places.get(link["gers"]) if link else None
            if link and not row:
                result.links[place_id] = link
        elif record.get("kind") in OVERTURE_KINDS:
            row = overture_place_for(base, overture, lists, geometry, fresh_only=changelog_only and not link)
            if row and base.get("name"):
                result.matches += 1
            elif row:
                result.name_sources += 1
        result.checked += 1
        checks.append((place_id, record, base, row))
    # The Overture places linked to a place are never candidates. A
    # candidate's pages count towards SHARED_PAGE_LIMIT too, so publishing it
    # never makes a page shared that was not, and a shared page is not its own.
    linked = frozenset({row["id"] for *_, row in checks if row} | {link["gers"] for link in result.links.values()})
    found, result.dropped = find_candidates(places, overture, lists, linked)
    candidate_rows = [entry["row"] for tier in TIERS for entry in found[tier]]
    shared = shared_pages([row for *_, row in checks] + unchecked + candidate_rows, lists, result)
    result.candidates = without_shared_pages(found, shared, overture.release, lists)
    for place_id, record, base, row in checks:
        values = values_from(row, base, lists, shared) if row else {}
        values.update(taken_values(base, lists))
        if row:
            result.differs.extend(
                d for d in differences(base, row, lists, shared) if (place_id, d["key"]) not in lists.taken
            )
            result.links[place_id] = {"gers": row["id"], "release": overture.release}
        new = with_values(base, values, record, now)
        before_values, after_values = overture_values(record), overture_values(new)
        for key, value in after_values.items():
            if before_values.get(key, None) != value or key not in before_values:
                result.fills.append({"place": place_id, "key": key, "value": value, "previous": before_values.get(key)})
        for key, value in before_values.items():
            if key not in after_values:
                result.removals.append({"place": place_id, "key": key, "value": value})
        if new != record:
            # The run's time is the day at 00:00, so a record changed later
            # that day keeps its later time.
            new["updated"] = max(now, record.get("updated") or now)
            result.records[place_id] = new
    return result


def shared_pages(rows: list[dict | None], lists: KeptLists, result: ImportResult) -> frozenset[str]:
    """The page keys Overture gives for more than SHARED_PAGE_LIMIT of the places.

    rows: the Overture place of each place (None for a place with none).
    Notes each one in result.shared, by the page as first given, with its count.
    """
    counts, shown = {}, {}
    for row in rows:
        if not row:
            continue
        for key in offered_pages(row, lists):
            counts[key] = counts.get(key, 0) + 1
        for url in [*own_websites(row, lists), *(page for _, page in all_social_pages(row))]:
            shown.setdefault(page_key(url), url)
    shared = {key for key, count in counts.items() if count > SHARED_PAGE_LIMIT}
    result.shared = {shown[key]: counts[key] for key in sorted(shared, key=lambda k: (-counts[k], k))}
    return frozenset(shared)


def kind_counts(places: dict[str, dict]) -> dict[str, dict]:
    """Per kind filled: the places, and how many have a name, website, phone, address and each social page."""
    out = {}
    for record in places.values():
        if record.get("status") == "merged" or record.get("kind") not in OVERTURE_KINDS:
            continue
        entry = out.setdefault(record["kind"], {"places": 0, **{key: 0 for key in COUNTED}})
        entry["places"] += 1
        for key in COUNTED:
            entry[key] += value_of(record, key) is not None
    return out


def summary_of(result: ImportResult, places: dict[str, dict]) -> dict:
    """Counts for the workflow and the report: totals and each kind before and after."""
    before = kind_counts(places)
    after = kind_counts({**places, **result.records})
    live = sum(1 for record in places.values() if record.get("status") != "merged")
    percent = 100.0 * len(result.records) / live if live else 0.0
    # Only gap fills under the limit merge themselves. Overture differs needs a
    # person, and so does a change of the limit or more. Candidates found are
    # only listed, so they hold nothing. A candidate published in this run
    # holds the PR (pipeline.overture_run).
    wait_reasons = []
    if percent >= AUTO_MERGE_BELOW_PERCENT:
        wait_reasons.append(f"{percent:.2f}% of records changed, {AUTO_MERGE_BELOW_PERCENT:g}% or more")
    if result.differs:
        wait_reasons.append(f"{len(result.differs)} Overture differs")
    return {
        "release": result.release,
        "places": live,
        "checked": result.checked,
        "changed": len(result.records),
        "change_percent": round(percent, 2),
        "auto_merge": not wait_reasons,
        "wait_reasons": wait_reasons,
        "fills": len(result.fills),
        "removals": len(result.removals),
        "differs": len(result.differs),
        "shared_pages": len(result.shared),
        "matches": result.matches,
        "name_sources": result.name_sources,
        "links": len(result.links),
        "candidates": {tier: len(rows) for tier, rows in result.candidates.items()},
        "dropped": {rule: len(rows) for rule, rows in sorted(result.dropped.items())},
        "kinds": {
            kind: {
                "places": after[kind]["places"],
                "before": {key: before[kind][key] for key in COUNTED},
                "after": {key: after[kind][key] for key in COUNTED},
            }
            for kind in sorted(after)
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("extract", type=Path, help="Overture extract written by tools/overture-pull/pull.py")
    parser.add_argument("--root", type=Path, default=Path("."), help="data repository root")
    parser.add_argument("--geometry", type=Path, help="geometry sidecar written by pipeline.extract --geometry, for outlines")
    parser.add_argument("--changelog", action="store_true", help="check again only the linked places the changelog lists as changed or removed, "
                        "and match the rest only against Overture places it lists as added or changed")
    parser.add_argument("--report", type=Path, default=Path("overture-report.md"))
    parser.add_argument("--summary", type=Path, help="also write counts as JSON here")
    parser.add_argument("--candidates", type=Path, help="also write the Overture candidates here, one line each, best tier first")
    parser.add_argument("--now", help="the run time, UTC (default: today at 00:00:00Z)")
    args = parser.parse_args(argv)

    started = time.monotonic()
    # A pull that found no places is broken. A run on it would take every
    # Overture value off, so it stops before anything is written.
    header = next(read_json_lines(args.extract), None)
    if not header or not header.get("places"):
        raise SystemExit(f"{args.extract}: the extract has no places")
    now = args.now or time.strftime("%Y-%m-%dT00:00:00Z", time.gmtime())
    places = load_places(args.root)
    result = import_overture(
        places,
        read_json_lines(args.extract),
        load_links(args.root),
        load_kept_lists(args.root),
        now=now,
        geometry=load_geometry(args.geometry),
        changelog_only=args.changelog,
    )
    for place_id, record in result.records.items():
        write_text(place_path(args.root, place_id), dump_record(record))
    write_links(args.root, result.links)
    summary = summary_of(result, places)
    write_text(args.report, render_report(result, summary))
    if args.summary:
        write_text(args.summary, json.dumps(summary, indent=2, sort_keys=True) + "\n")
    if args.candidates:
        write_text(args.candidates, "".join(
            dump_line(entry) + "\n" for tier in TIERS for entry in result.candidates.get(tier, [])
        ))
    print(
        f"Overture {result.release}: {summary['changed']} records changed, {summary['fills']} values filled, "
        f"{summary['removals']} taken off, {summary['differs']} differ, {summary['links']} links "
        f"in {time.monotonic() - started:.0f} s",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
