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
from .overture_socials import page_key, social_page
from .overture_rules import (
    DO_NOT_MATCH_FILE,
    STATES,
    WIDE_MATCH_DISTANCE_M,
    WIDE_MATCH_KINDS,
    KeptLists,
    Overture,
    cell_of,
    cells_within,
    load_kept_lists,
    load_links,
    metres_apart,
    names_agree,
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


def camping_grid(places: dict[str, dict]) -> dict[tuple[int, int], list[dict]]:
    """Every live named camping place (WIDE_MATCH_KINDS), filed by grid cell."""
    grid = {}
    for record in places.values():
        if record.get("status") == "merged" or not record.get("location") or record.get("kind") not in WIDE_MATCH_KINDS:
            continue
        if not normalised_name(record.get("name")):
            continue
        loc = record["location"]
        grid.setdefault(cell_of(loc["lat"], loc["lon"]), []).append(record)
    return grid


def same_place_within(camping: dict, name: str | None, lat: float, lon: float, lists: KeptLists) -> bool:
    """True when a camping place within WIDE_MATCH_DISTANCE_M has a name that
    agrees with this one (names_agree): it is that place, under a fuller or
    chain-branded name."""
    return any(
        names_agree(record["name"], name, lists.chains)
        and metres_apart(lat, lon, record["location"]["lat"], record["location"]["lon"]) <= WIDE_MATCH_DISTANCE_M
        for cell in cells_within(lat, lon, WIDE_MATCH_DISTANCE_M)
        for record in camping.get(cell, ())
    )


def junk_rule(row: dict, grid: dict, lists: KeptLists, camping: dict | None = None) -> str | None:
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
    if camping and same_place_within(camping, row.get("name"), row["lat"], row["lon"], lists):
        return "same_place"
    return None


def find_candidates(
    places: dict[str, dict], overture: Overture, lists: KeptLists, linked: frozenset[str] = frozenset()
) -> tuple[dict, dict]:
    """Overture camping places we lack, by tier, and the rows each junk rule dropped.

    linked: GERS ids linked to a place (an Overture match, a name source or a
    published candidate). They are places we have, so never candidates.
    """
    found = {tier: [] for tier in TIERS}
    dropped = {}
    grid = places_grid(places)
    camping = camping_grid(places)
    for gers in sorted(overture.places):
        row = overture.places[gers]
        kind = candidate_kind(row, lists)
        if kind is None:
            continue
        entry = candidate_entry(row, kind, overture.release, lists)
        rule = junk_rule(row, grid, lists, camping)
        if rule:
            dropped.setdefault(rule, []).append(entry)
        elif gers not in linked:
            entry["tier"] = tier_of(entry, lists)
            found[entry["tier"]].append(entry)
    return found, dropped


def without_shared_pages(found: dict, shared: frozenset[str], release: str, lists: KeptLists) -> dict:
    """The candidates by tier again, their Overture rows without the shared pages (SHARED_PAGE_LIMIT).

    A shared page is no candidate's own website, so its tier can change. The
    row is what a published candidate takes its values from.
    """
    if not shared:
        return found
    def is_shared(url) -> bool:
        if not isinstance(url, str):
            return False
        page = social_page(url)
        return page_key(url) in shared or (page is not None and page_key(page[1]) in shared)

    out = {tier: [] for tier in TIERS}
    entries = sorted((entry for tier in TIERS for entry in found[tier]), key=lambda entry: entry["gers"])
    for entry in entries:
        row = dict(entry["row"])
        for key in ("websites", "socials"):
            row[key] = [url for url in row.get(key) or [] if not is_shared(url)]
        fresh = candidate_entry(row, entry["kind"], release, lists)
        fresh["tier"] = tier_of(fresh, lists)
        out[fresh["tier"]].append(fresh)
    return out


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


def decision_problem(entry) -> str | None:
    """Why a decision line cannot be acted on, or None when it can.

    Rejections and flags need only a GERS id. An approval also needs what its
    place record is made from: a tier, a kind, a name, a location, the
    release and the Overture row.
    """
    if not isinstance(entry, dict):
        return "not a JSON object: " + str(entry).strip()[:80]
    if not isinstance(entry.get("gers"), str) or not entry["gers"].strip():
        return "no GERS id"
    decision = entry.get("decision")
    if decision not in DECISIONS:
        return f"unknown decision {decision!r}"
    if decision != "approve":
        return None
    location = entry.get("location")
    if not (
        isinstance(location, dict)
        and all(isinstance(location.get(k), (int, float)) and not isinstance(location.get(k), bool) for k in ("lat", "lon"))
        and -90 <= location["lat"] <= 90
        and -180 <= location["lon"] <= 180
    ):
        return "an approval with no usable location"
    if not isinstance(entry.get("name"), str) or not entry["name"].strip():
        return "an approval with no name"
    if entry.get("kind") not in KIND_GROUP:
        return f"an approval with an unknown kind {entry.get('kind')!r}"
    if entry.get("tier") not in TIERS:
        return f"an approval with an unknown tier {entry.get('tier')!r}"
    if not isinstance(entry.get("release"), str) or not entry["release"]:
        return "an approval with no release"
    if not isinstance(entry.get("row"), dict):
        return "an approval with no Overture row"
    return None


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
    Overture duplicate), or within 500 m of a camping place whose name
    agrees with its own (same_place_within), waits for a person. A new place never takes the id of
    a place already in places/.

    A line that is not a JSON object (the decide command passes such a line
    as its text), has no GERS id, has an unknown decision, or is an approval
    that cannot make a place is skipped: nothing is done for it and it is
    listed for a person. Every other line is still acted on.

    Returns the place ids published, the GERS ids held, rejected, flagged,
    already linked and near a place, and under "skipped" each skipped line
    as {line, gers, problem}, line counting the decision lines from 1.
    """
    root = Path(root)
    allowed = set(TIERS if tiers is None else tiers)
    lists = load_kept_lists(root)
    links = load_links(root)
    linked = {link["gers"] for link in links.values()}
    places = load_places(root) if publish else {}
    grid = places_grid(places)
    camping = camping_grid(places)
    used_ids = set(places)
    outcome = {"published": [], "held": [], "rejected": [], "flagged": [], "already": [], "near": [], "skipped": []}
    for line, entry in enumerate(decisions, start=1):
        problem = decision_problem(entry)
        if problem:
            gers = entry.get("gers") if isinstance(entry, dict) and isinstance(entry.get("gers"), str) else None
            outcome["skipped"].append({"line": line, "gers": gers, "problem": problem})
            continue
        decision, gers = entry["decision"], entry["gers"]
        if decision == "reject":
            outcome["rejected"].append(gers)
        elif decision == "flag":
            outcome["flagged"].append(gers)
        elif gers in linked:
            outcome["already"].append(gers)
        elif not publish or entry.get("tier") not in allowed:
            outcome["held"].append(gers)
        elif place_within(grid, entry["location"]["lat"], entry["location"]["lon"], CANDIDATE_CLEARANCE_M) or (
            same_place_within(camping, entry["name"], entry["location"]["lat"], entry["location"]["lon"], lists)
        ):
            outcome["near"].append(gers)
        else:
            place_id = make_id()
            while place_id in used_ids:  # never write over a place already there
                place_id = make_id()
            used_ids.add(place_id)
            try:
                record = candidate_record(entry, lists, now, place_id)
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                outcome["skipped"].append({"line": line, "gers": gers, "problem": f"an approval that makes no place ({exc!r})"})
                continue
            write_text(place_path(root, record["id"]), dump_record(record))
            links[record["id"]] = {"gers": gers, "release": entry["release"]}
            linked.add(gers)
            grid.setdefault(cell_of(record["location"]["lat"], record["location"]["lon"]), []).append(
                (record["location"]["lat"], record["location"]["lon"])
            )
            if record["kind"] in WIDE_MATCH_KINDS:
                camping.setdefault(cell_of(record["location"]["lat"], record["location"]["lon"]), []).append(record)
            outcome["published"].append(record["id"])
    if outcome["rejected"]:
        path = root / DO_NOT_MATCH_FILE
        saved = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"pairs": []}
        saved["gers"] = sorted(set(saved.get("gers", [])) | set(outcome["rejected"]))
        write_text(path, json.dumps(saved, indent=2) + "\n")
    if outcome["published"]:
        write_links(root, links)
    return outcome
