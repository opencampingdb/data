"""Overture candidates and the decisions on them.

find_candidates sorts the camping places Overture lists that the dataset
lacks into tiers, after the junk rules. apply_decisions acts on reviewed
candidates: rejections go on the do-not-match list, and approved candidates
of the allowed tiers are published. See pipeline/README.md "Overture
candidates" and "Overture decisions".
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable

from .common import KIND_GROUP, OVERTURE, dump_record, load_places, place_path, write_text
from .overture_rules import (
    DO_NOT_MATCH_FILE,
    STATES,
    KeptLists,
    Overture,
    cell_of,
    cells_within,
    load_kept_lists,
    load_links,
    metres_apart,
    normalised_name,
    own_websites,
    phones_of,
    values_from,
    write_links,
)


from .sync import new_place_id

# The tiers, best first. strong: confidence 0.9 or more, a camping
# basic_category (not only a camping taxonomy entry) and a website of its own
# (not on a directory host or a social host). weak: every other candidate.
TIERS = ("strong", "weak")
STRONG_CONFIDENCE = 0.9


def tier_of(entry: dict, lists: KeptLists) -> str:
    """The tier of a candidate entry."""
    strong = (
        (entry["confidence"] or 0) >= STRONG_CONFIDENCE
        and entry["category"] in lists.candidate_kinds
        and entry["website"] is not None
    )
    return "strong" if strong else "weak"


def candidate_kind(row: dict, lists: KeptLists) -> str | None:
    """The kind a candidate would publish as: its basic_category's, else its taxonomy primary's."""
    return lists.candidate_kinds.get(row.get("basic_category")) or lists.candidate_kinds.get(row.get("taxonomy_primary"))


def candidate_entry(row: dict, kind: str, release: str, lists: KeptLists) -> dict:
    """What the reviewer and the report see of a candidate, plus the Overture row for publishing."""
    websites = own_websites(row, lists)
    phones = phones_of(row)
    region = next((a.get("region") for a in row.get("addresses") or [] if a.get("region")), None)
    return {
        "gers": row["id"],
        "name": row.get("name"),
        "kind": kind,
        "category": row.get("basic_category"),
        "taxonomy": row.get("taxonomy_primary"),
        "confidence": row.get("confidence"),
        "location": {"lat": round(row["lat"], 6), "lon": round(row["lon"], 6)},
        "state": STATES.get((region or "").strip().upper()),
        "website": websites[0] if websites else None,
        "phone": phones[0] if phones else None,
        "release": release,
        "row": row,
    }


# A candidate this close to a place of any kind is the same place, or too
# close to tell apart.
CANDIDATE_CLEARANCE_M = 250

# A scout, guide, school or church camp. "Old school" and "School Point" are
# place names, not schools.
GROUP_CAMP = re.compile(
    r"\b(scouts?|scouting|guides?|(?<!old )schools?(?! point)|college|church|christian|bible|baptist\w*"
    r"|anglican|adventist|uniting|catholic|ymca|ywca)\b"
)


def places_grid(places: dict[str, dict]) -> dict[tuple[int, int], list[tuple[float, float]]]:
    """The locations of every live place, filed by grid cell."""
    grid = {}
    for record in places.values():
        if record.get("status") == "merged" or not record.get("location"):
            continue
        loc = record["location"]
        grid.setdefault(cell_of(loc["lat"], loc["lon"]), []).append((loc["lat"], loc["lon"]))
    return grid


def place_within(grid: dict, lat: float, lon: float, metres: float) -> bool:
    """True when a place in the grid lies within metres of a point."""
    return any(
        metres_apart(lat, lon, plat, plon) <= metres
        for cell in cells_within(lat, lon, metres)
        for plat, plon in grid.get(cell, ())
    )


def junk_rule(row: dict, grid: dict, lists: KeptLists) -> str | None:
    """The first junk rule a camping place breaks, or None when it is a candidate."""
    name = normalised_name(row.get("name"))
    if row["id"] in lists.rejected:
        return "do_not_match"
    if not name:
        return "no_name"
    if name in lists.chains:
        return "chain_name"
    if GROUP_CAMP.search(name):
        return "scout_school_church"
    if place_within(grid, row["lat"], row["lon"], CANDIDATE_CLEARANCE_M):
        return "near_place"
    return None


def find_candidates(places: dict[str, dict], overture: Overture, lists: KeptLists) -> tuple[dict, dict]:
    """Overture camping places we lack, by tier, and the rows each junk rule dropped."""
    found = {tier: [] for tier in TIERS}
    dropped = {}
    grid = places_grid(places)
    for gers in sorted(overture.places):
        row = overture.places[gers]
        kind = candidate_kind(row, lists)
        if kind is None:
            continue
        entry = candidate_entry(row, kind, overture.release, lists)
        rule = junk_rule(row, grid, lists)
        if rule:
            dropped.setdefault(rule, []).append(entry)
        else:
            entry["tier"] = tier_of(entry, lists)
            found[entry["tier"]].append(entry)
    return found, dropped


DECISIONS = ("approve", "reject", "flag")


def candidate_record(entry: dict, lists: KeptLists, now: str, place_id: str) -> dict:
    """The place record an approved candidate publishes as.

    No OSM link, standing shown (not checked yet), and overture as the source
    of its name, its location and any website, phone and address Overture
    gives, each dated now.
    """
    values = {"name": entry["name"].strip()}
    values.update(values_from(entry["row"], {"name": values["name"], "facts": {}}, lists))
    facts = {k: v for k, v in values.items() if k != "name"}
    keys = ["name", "location", *facts]
    return {
        "id": place_id,
        "name": values["name"],
        "kind": entry["kind"],
        "group": KIND_GROUP[entry["kind"]],
        "location": dict(entry["location"]),
        "osm": None,
        "facts": facts,
        "status": "open",
        "rating": None,
        "standing": "shown",
        "fact_sources": {key: OVERTURE for key in keys},
        "fact_dates": {key: now for key in keys},
        "updated": now,
    }


def apply_decisions(
    root: Path,
    decisions: Iterable[dict],
    *,
    now: str,
    publish: bool = False,
    tiers: Iterable[str] | None = None,
    make_id=new_place_id,
) -> dict:
    """Act on reviewed candidates: candidate entries, each with decision approve, reject or flag.

    A rejected candidate's GERS id goes on overture/do-not-match.json, so it is
    never a candidate again. A flagged one waits for a person. An approved one
    is published (written to places/ and linked in the link file) only when
    publish is true and its tier is one of tiers (every tier when tiers is
    None); otherwise it is held. A candidate whose GERS id is
    already linked to a place is not published again, and one within 250 m
    of a place (including one published in the same run, such as an
    Overture duplicate) waits for a person.

    Returns the place ids published, and the GERS ids held, rejected,
    flagged, already linked and near a place.
    """
    root = Path(root)
    allowed = set(TIERS if tiers is None else tiers)
    lists = load_kept_lists(root)
    links = load_links(root)
    linked = {link["gers"] for link in links.values()}
    grid = places_grid(load_places(root)) if publish else {}
    outcome = {"published": [], "held": [], "rejected": [], "flagged": [], "already": [], "near": []}
    for entry in decisions:
        decision, gers = entry["decision"], entry["gers"]
        if decision not in DECISIONS:
            raise ValueError(f"{gers}: unknown decision {decision!r}")
        if decision == "reject":
            outcome["rejected"].append(gers)
        elif decision == "flag":
            outcome["flagged"].append(gers)
        elif gers in linked:
            outcome["already"].append(gers)
        elif not publish or entry.get("tier") not in allowed:
            outcome["held"].append(gers)
        elif place_within(grid, entry["location"]["lat"], entry["location"]["lon"], CANDIDATE_CLEARANCE_M):
            outcome["near"].append(gers)
        else:
            record = candidate_record(entry, lists, now, make_id())
            write_text(place_path(root, record["id"]), dump_record(record))
            links[record["id"]] = {"gers": gers, "release": entry["release"]}
            linked.add(gers)
            grid.setdefault(cell_of(record["location"]["lat"], record["location"]["lon"]), []).append(
                (record["location"]["lat"], record["location"]["lon"])
            )
            outcome["published"].append(record["id"])
    if outcome["rejected"]:
        path = root / DO_NOT_MATCH_FILE
        saved = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"pairs": []}
        saved["gers"] = sorted(set(saved.get("gers", [])) | set(outcome["rejected"]))
        write_text(path, json.dumps(saved, indent=2) + "\n")
    if outcome["published"]:
        write_links(root, links)
    return outcome
