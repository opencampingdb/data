"""The Overture importer: gap fills from Overture Maps places.

One run takes the place records, one Overture extract (tools/overture-pull,
format in pipeline/README.md "Overture extract"), the link file and the kept
lists, and returns the changed records, the gap fills, the values taken off,
the "Overture differs" list, the new link file and the Overture candidates
by tier. It does no network work. pipeline.overture_decide acts on reviewed
candidates.

    python -m pipeline.overture overture.ndjson --root . --geometry geometry.ndjson \\
        [--changelog] [--report overture-report.md] [--summary overture-summary.json] \\
        [--candidates overture-candidates.ndjson]

See pipeline/README.md "Overture import" for the rules.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from shapely.geometry import Point
from shapely.ops import nearest_points

from .common import (
    KIND_GROUP,
    OVERTURE,
    dump_line,
    dump_record,
    load_places,
    place_path,
    write_text,
)
from .geometry import Geometry, load_geometry, osm_ref
from .sync import AUTO_MERGE_BELOW_PERCENT, REPORT_LIST_LIMIT, new_place_id

# The kinds the importer fills. Every other kind is left alone.
OVERTURE_KINDS = tuple(sorted(
    [kind for kind, group in KIND_GROUP.items() if group == "overnight"]
    + [
        "information_centre", "fuel", "lpg_refill_swap", "mechanic", "food_and_drink",
        "laundromat", "hospital_emergency", "attraction", "heritage", "experience",
    ]
))

# The facts a match fills, besides a name for an unnamed place.
FILLED_FACTS = ("website", "phone", "address")

# Changelog entries that mean an Overture place has not changed. A changelog
# run does not check a place linked to one of these again.
UNCHANGED = ("unchanged", None)

# The strong match.
MATCH_DISTANCE_M = 100
MATCH_NAME_SIMILARITY = 0.85
MATCH_CONFIDENCE = 0.7
# The name for an unnamed place.
NAME_DISTANCE_M = 100
NAME_CONFIDENCE = 0.8

CATEGORIES_FILE = Path("overture") / "categories.json"
DIRECTORY_HOSTS_FILE = Path("overture") / "directory-hosts.json"
LINKS_FILE = Path("overture") / "links.ndjson"
DO_NOT_MATCH_FILE = Path("overture") / "do-not-match.json"
CHAINS_FILE = Path("overture") / "chains.json"

# The grid the Overture places are filed in: about 220 m by 230 m, so every
# place within 100 m of a point is in its cell or a neighbouring one.
CELL_LAT = 0.002
CELL_LON = 0.0025

STATES = {
    "NSW": "NSW", "NEW SOUTH WALES": "NSW",
    "VIC": "VIC", "VICTORIA": "VIC",
    "QLD": "QLD", "QUEENSLAND": "QLD",
    "SA": "SA", "SOUTH AUSTRALIA": "SA",
    "WA": "WA", "WESTERN AUSTRALIA": "WA",
    "TAS": "TAS", "TASMANIA": "TAS",
    "NT": "NT", "NORTHERN TERRITORY": "NT",
    "ACT": "ACT", "AUSTRALIAN CAPITAL TERRITORY": "ACT",
}


@dataclass(frozen=True)
class KeptLists:
    """The lists kept in the repository.

    categories: each kind and the Overture categories (basic_category or
    taxonomy primary) that fit it. directory_hosts: directory, aggregator and
    social hosts, whose websites are never taken or compared (a subdomain
    counts too). do_not_match: (place id, GERS id) pairs never matched.
    candidate_kinds: each camping category that makes an Overture candidate,
    and the kind it publishes as. chains: chain-only names, normalised.
    rejected: GERS ids of rejected candidates, never candidates again.
    """

    categories: dict[str, set[str]]
    directory_hosts: tuple[str, ...]
    do_not_match: frozenset[tuple[str, str]] = frozenset()
    candidate_kinds: dict[str, str] = field(default_factory=dict)
    chains: tuple[str, ...] = ()
    rejected: frozenset[str] = frozenset()


@dataclass
class ImportResult:
    """What one run found.

    records: every changed record, by id. fills: one entry per value taken or
    changed ({place, key, value, previous}). removals: one per Overture value
    taken off ({place, key, value}). differs: matched places where Overture's
    website or phone disagrees with OSM's. links: the new link file, place id
    to {gers, release}. candidates: Overture candidates by tier, filled by the
    candidate step (opencampingdb/data#192), empty until then.
    """

    release: str
    records: dict[str, dict] = field(default_factory=dict)
    fills: list[dict] = field(default_factory=list)
    removals: list[dict] = field(default_factory=list)
    differs: list[dict] = field(default_factory=list)
    links: dict[str, dict] = field(default_factory=dict)
    candidates: dict[str, list[dict]] = field(default_factory=dict)
    dropped: dict[str, list[dict]] = field(default_factory=dict)
    checked: int = 0


def load_kept_lists(root: Path) -> KeptLists:
    """Read the category map, the directory hosts and the do-not-match list from the repository."""
    root = Path(root)
    category_file = json.loads((root / CATEGORIES_FILE).read_text(encoding="utf-8"))
    hosts = json.loads((root / DIRECTORY_HOSTS_FILE).read_text(encoding="utf-8"))["hosts"]
    pairs, rejected = set(), set()
    path = root / DO_NOT_MATCH_FILE
    if path.exists():
        do_not_match = json.loads(path.read_text(encoding="utf-8"))
        for entry in do_not_match.get("pairs", []):
            pairs.add((entry["place"], entry["gers"]))
        rejected.update(do_not_match.get("gers", []))
    chains = []
    path = root / CHAINS_FILE
    if path.exists():
        chains = [normalised_name(name) for name in json.loads(path.read_text(encoding="utf-8"))["names"]]
    return KeptLists(
        {kind: set(cats) for kind, cats in category_file["categories"].items()},
        tuple(host.lower() for host in hosts),
        frozenset(pairs),
        candidate_kinds=dict(category_file.get("candidates", {})),
        chains=tuple(chains),
        rejected=frozenset(rejected),
    )


# ------------------------------------------------------------------ helpers


def normalised_name(name: str | None) -> str:
    """Lower case, every run of characters but a-z and 0-9 one space, trimmed."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).split())


def jaro_winkler(a: str, b: str) -> float:
    """Jaro-Winkler similarity, prefix scale 0.1 over at most 4 characters."""
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    window = max(max(len(a), len(b)) // 2 - 1, 0)
    a_hit = [False] * len(a)
    b_hit = [False] * len(b)
    matches = 0
    for i, ch in enumerate(a):
        for j in range(max(0, i - window), min(len(b), i + window + 1)):
            if not b_hit[j] and b[j] == ch:
                a_hit[i] = b_hit[j] = True
                matches += 1
                break
    if matches == 0:
        return 0.0
    a_chars = [ch for ch, hit in zip(a, a_hit) if hit]
    b_chars = [ch for ch, hit in zip(b, b_hit) if hit]
    transpositions = sum(x != y for x, y in zip(a_chars, b_chars)) / 2
    jaro = (matches / len(a) + matches / len(b) + (matches - transpositions) / matches) / 3
    prefix = 0
    for x, y in zip(a[:4], b[:4]):
        if x != y:
            break
        prefix += 1
    return jaro + prefix * 0.1 * (1 - jaro)


def metres_between(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6_371_008.8 * math.asin(math.sqrt(h))


def host_of(url: str) -> str:
    """The host of a URL, lower case, without a leading www."""
    match = re.match(r"^[a-z][a-z0-9+.-]*://([^/?#:]+)", url.strip().lower())
    host = match.group(1) if match else url.strip().lower()
    return host[4:] if host.startswith("www.") else host


def on_host(host: str, hosts: Iterable[str]) -> bool:
    return any(host == h or host.endswith("." + h) for h in hosts)


def phone_key(phone: str) -> str:
    """An Australian phone number as its digits in the national form (0 2 5550 0101)."""
    digits = re.sub(r"\D", "", phone)
    if digits.startswith("0011"):
        digits = digits[4:]
    if digits.startswith("61") and len(digits) >= 10:
        digits = digits[2:]
        if digits.startswith("0"):
            digits = digits[1:]  # +61 (0)2 ...
        digits = "0" + digits
    return digits


def osm_phones(value: str) -> list[str]:
    """The numbers in an OSM phone value (several are separated by ;)."""
    return [part.strip() for part in value.split(";") if part.strip()]


# A street Overture gives for a road with no name, and a locality that is a
# council area rather than a suburb.
UNNAMED_STREET = re.compile(r"^unnamed\s+(rd|road)$", re.IGNORECASE)
COUNCIL_AREA = re.compile(r"^(shire|city|municipality|town|council|district|region) of\s", re.IGNORECASE)

HOUSENUMBER = re.compile(r"^((?:lot\s+)?[0-9][0-9a-z]*(?:[/-][0-9][0-9a-z]*)*)\s+(.+)$", re.IGNORECASE)


def overture_address(addresses: list[dict]) -> dict | None:
    """The first Overture address that makes a valid address fact, or None.

    The street part is the last comma-separated piece of freeform, split into
    a house number (with "Lot") and the street. locality becomes the suburb
    unless it is a council area, region the state, and a four digit postcode
    is kept. A street Overture calls "Unnamed Rd" is no street.
    """
    for address in addresses:
        if address.get("country") not in (None, "AU"):
            continue
        freeform = (address.get("freeform") or "").strip()
        if not freeform:
            continue
        street_part = freeform.split(",")[-1].strip()
        if not street_part or not re.search(r"[A-Za-z]", street_part) or UNNAMED_STREET.match(street_part):
            continue
        out = {}
        match = HOUSENUMBER.match(street_part)
        if match:
            out["housenumber"], out["street"] = match.group(1), match.group(2)
        else:
            out["street"] = street_part
        locality = (address.get("locality") or "").strip()
        if locality and not COUNCIL_AREA.match(locality):
            out["suburb"] = locality
        state = STATES.get((address.get("region") or "").strip().upper())
        if state:
            out["state"] = state
        postcode = (address.get("postcode") or "").strip()
        if re.fullmatch(r"\d{4}", postcode):
            out["postcode"] = postcode
        if all(0 < len(v) <= 100 for v in out.values()):
            return out
    return None


# ------------------------------------------------------------- the extract


@dataclass
class Overture:
    """The Overture places that could fit a kind, filed by grid cell."""

    release: str
    places: dict[str, dict]
    grid: dict[tuple[int, int], list[dict]]
    changes: dict[str, str | None]


def cell_of(lat: float, lon: float) -> tuple[int, int]:
    return math.floor(lat / CELL_LAT), math.floor(lon / CELL_LON)


def read_overture(
    lines: Iterable[dict], categories: dict[str, set[str]], wanted_ids: set[str], candidate_categories: Iterable[str] = ()
) -> Overture:
    """Keep the extract's places whose category fits some kind or makes a candidate, at confidence 0.7 or more.

    Also notes the changelog entry of every id in wanted_ids (the linked ones),
    kept or not.
    """
    fitting = set().union(*categories.values(), candidate_categories)
    rows = iter(lines)
    header = next(rows)
    places, grid, changes = {}, {}, {}
    for row in rows:
        gers = row["id"]
        if gers in wanted_ids:
            changes[gers] = row.get("change")
        if row.get("change") == "removed":
            continue
        if (row.get("confidence") or 0) < MATCH_CONFIDENCE:
            continue
        if row.get("basic_category") not in fitting and row.get("taxonomy_primary") not in fitting:
            continue
        places[gers] = row
        grid.setdefault(cell_of(row["lat"], row["lon"]), []).append(row)
    return Overture(header["release"], places, grid, changes)


def fits(row: dict, kind: str, categories: dict[str, set[str]]) -> bool:
    wanted = categories.get(kind, ())
    return row.get("basic_category") in wanted or row.get("taxonomy_primary") in wanted


def near(overture: Overture, lat: float, lon: float, metres: float) -> list[tuple[float, dict]]:
    """Overture places within metres of a point, with their distances."""
    out = []
    row, col = cell_of(lat, lon)
    reach_lat = math.ceil(metres / 111_000 / CELL_LAT)
    reach_lon = math.ceil(metres / (111_000 * max(math.cos(math.radians(lat)), 0.2)) / CELL_LON)
    for r in range(row - reach_lat, row + reach_lat + 1):
        for c in range(col - reach_lon, col + reach_lon + 1):
            for candidate in overture.grid.get((r, c), ()):
                d = metres_between(lat, lon, candidate["lat"], candidate["lon"])
                if d <= metres:
                    out.append((d, candidate))
    return out


# --------------------------------------------------------------- the rules


def is_candidate(record: dict) -> bool:
    """True for a published Overture candidate: no OSM link, its location from Overture."""
    return record.get("osm") is None and record.get("fact_sources", {}).get("location") == OVERTURE


def strip_overture(record: dict) -> dict:
    """The record without its Overture values: what other sources give.

    A published candidate keeps its Overture name and location: they are what
    the place is, not gap fills.
    """
    sources = record.get("fact_sources", {})
    if OVERTURE not in sources.values():
        return record
    kept = ("name", "location") if is_candidate(record) else ()
    gone = {k for k, v in sources.items() if v == OVERTURE and k not in kept}
    base = dict(record)
    base["facts"] = {k: v for k, v in record.get("facts", {}).items() if k not in gone}
    base["fact_sources"] = {k: v for k, v in sources.items() if k not in gone}
    dates = {k: v for k, v in record.get("fact_dates", {}).items() if k not in gone}
    base.pop("fact_dates", None)
    if dates:
        base["fact_dates"] = dates
    if "name" in gone:
        base["name"] = None
    return base


def strong_match(record: dict, overture: Overture, lists: KeptLists) -> dict | None:
    """The one Overture place that strongly matches a named place, or None."""
    name = normalised_name(record["name"])
    loc = record["location"]
    passing = [
        row
        for _, row in near(overture, loc["lat"], loc["lon"], MATCH_DISTANCE_M)
        if fits(row, record["kind"], lists.categories)
        and (row.get("confidence") or 0) >= MATCH_CONFIDENCE
        and jaro_winkler(name, normalised_name(row.get("name"))) >= MATCH_NAME_SIMILARITY
        and (record["id"], row["id"]) not in lists.do_not_match
    ]
    return passing[0] if len(passing) == 1 else None


def in_box(overture: Overture, west: float, south: float, east: float, north: float) -> list[dict]:
    """Overture places inside a box of degrees."""
    r0, c0 = cell_of(south, west)
    r1, c1 = cell_of(north, east)
    if (r1 - r0 + 1) * (c1 - c0 + 1) > len(overture.grid):
        cells = [cell for cell in overture.grid if r0 <= cell[0] <= r1 and c0 <= cell[1] <= c1]
    else:
        cells = [(r, c) for r in range(r0, r1 + 1) for c in range(c0, c1 + 1)]
    return [
        row
        for cell in cells
        for row in overture.grid.get(cell, ())
        if west <= row["lon"] <= east and south <= row["lat"] <= north
    ]


def near_outline(overture: Overture, area, metres: float) -> list[dict]:
    """Overture places inside an outline or within metres of it."""
    west, south, east, north = area.bounds
    pad_lat = metres / 111_000
    pad_lon = metres / (111_000 * max(math.cos(math.radians(north)), 0.2))
    out = []
    for row in in_box(overture, west - pad_lon, south - pad_lat, east + pad_lon, north + pad_lat):
        point = Point(row["lon"], row["lat"])
        if area.covers(point):
            out.append(row)
            continue
        nearest = nearest_points(area, point)[0]
        if metres_between(row["lat"], row["lon"], nearest.y, nearest.x) <= metres:
            out.append(row)
    return out


def name_match(record: dict, overture: Overture, lists: KeptLists, geometry: Geometry) -> dict | None:
    """The one confident Overture place that names an unnamed place, or None."""
    area = geometry.areas.get(osm_ref(record))
    if area is not None:
        nearby = near_outline(overture, area, NAME_DISTANCE_M)
    else:
        loc = record["location"]
        nearby = [row for _, row in near(overture, loc["lat"], loc["lon"], NAME_DISTANCE_M)]
    passing = [
        row
        for row in nearby
        if row.get("name")
        and fits(row, record["kind"], lists.categories)
        and (row.get("confidence") or 0) >= NAME_CONFIDENCE
        and (record["id"], row["id"]) not in lists.do_not_match
    ]
    return passing[0] if len(passing) == 1 else None


def match_for(record: dict, overture: Overture, lists: KeptLists, geometry: Geometry) -> dict | None:
    """The record's Overture match: the strong match for a named place, the name rule for an unnamed one."""
    if record.get("name"):
        return strong_match(record, overture, lists)
    return name_match(record, overture, lists, geometry)


def values_from(row: dict, base: dict, lists: KeptLists) -> dict:
    """The values a matched Overture place gives where the base record has none."""
    out = {}
    facts = base.get("facts", {})
    if not base.get("name") and row.get("name") and len(row["name"]) <= 300:
        out["name"] = row["name"].strip()
    if "website" not in facts:
        websites = [url for url in own_websites(row, lists) if len(url) <= 500]
        if websites:
            out["website"] = websites[0]
    if "phone" not in facts:
        phones = [p.strip() for p in row.get("phones") or [] if p and p.strip()]
        if phones and len(phones[0]) <= 100:
            out["phone"] = phones[0]
    if "address" not in facts:
        address = overture_address(row.get("addresses") or [])
        if address:
            out["address"] = address
    return out


def own_websites(row: dict, lists: KeptLists) -> list[str]:
    """Overture's websites, leaving out directory hosts."""
    return [
        url.strip()
        for url in row.get("websites") or []
        if url and re.match(r"^https?://", url.strip()) and not on_host(host_of(url), lists.directory_hosts)
    ]


def differences(base: dict, row: dict, lists: KeptLists) -> list[dict]:
    """Where Overture's website or phone disagrees with an OSM value.

    Websites are compared by host, phones as Australian numbers. They agree
    when any Overture value matches any of OSM's.
    """
    facts, sources = base.get("facts", {}), base.get("fact_sources", {})
    out = []
    phones = [p.strip() for p in row.get("phones") or [] if p and p.strip()]
    if sources.get("phone") == "osm" and phones:
        if not {phone_key(p) for p in osm_phones(facts["phone"])} & {phone_key(p) for p in phones}:
            out.append(("phone", facts["phone"], phones[0]))
    websites = own_websites(row, lists)
    if sources.get("website") == "osm" and websites:
        if host_of(facts["website"]) not in {host_of(url) for url in websites}:
            out.append(("website", facts["website"], websites[0]))
    return [
        {
            "place": base["id"],
            "name": base.get("name"),
            "kind": base["kind"],
            "location": base["location"],
            "key": key,
            "osm": ours,
            "overture": theirs,
            "gers": row["id"],
            "overture_name": row.get("name"),
            "overture_location": {"lat": row["lat"], "lon": row["lon"]},
        }
        for key, ours, theirs in out
    ]


def with_values(base: dict, values: dict, before: dict, now: str) -> dict:
    """The base record with the Overture values set, keeping the date of a value that has not changed."""
    record = dict(base)
    record["facts"] = dict(base.get("facts", {}))
    record["fact_sources"] = dict(base.get("fact_sources", {}))
    dates = dict(base.get("fact_dates", {}))
    old_sources = before.get("fact_sources", {})
    old_dates = before.get("fact_dates", {})
    for key, value in values.items():
        if key == "name":
            record["name"] = value
            unchanged = old_sources.get("name") == OVERTURE and before.get("name") == value
        else:
            record["facts"][key] = value
            unchanged = old_sources.get(key) == OVERTURE and before.get("facts", {}).get(key) == value
        record["fact_sources"][key] = OVERTURE
        dates[key] = old_dates[key] if unchanged and key in old_dates else now
    record.pop("fact_dates", None)
    if dates:
        record["fact_dates"] = dates
    return record


def value_of(record: dict, key: str):
    return record.get("name") if key == "name" else record.get("facts", {}).get(key)


def overture_values(record: dict) -> dict:
    return {k: value_of(record, k) for k, v in record.get("fact_sources", {}).items() if v == OVERTURE}


# ------------------------------------------------------------------ the run


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
    now: the run time, used for fact dates and updated. With changelog_only,
    a linked place whose Overture place the changelog lists as unchanged (or
    does not list) is not checked again: it keeps its values and link.
    """
    geometry = geometry or Geometry()
    overture = read_overture(
        extract, lists.categories, {link["gers"] for link in links.values()}, lists.candidate_kinds
    )
    result = ImportResult(release=overture.release)
    for place_id in sorted(places):
        record = places[place_id]
        if record.get("status") == "merged":
            continue
        link = links.get(place_id)
        if (
            changelog_only
            and link
            and record.get("kind") in OVERTURE_KINDS
            and link["gers"] in overture.changes
            and overture.changes[link["gers"]] in UNCHANGED
        ):
            # The changelog says the Overture place has not changed: keep the
            # place as it is and note the link as seen in this release.
            result.links[place_id] = {"gers": link["gers"], "release": overture.release}
            continue
        base = strip_overture(record)
        row = None
        if is_candidate(record):
            # A published candidate's match is the Overture place it came from.
            row = overture.places.get(link["gers"]) if link else None
            if link and not row:
                result.links[place_id] = link
        elif record.get("kind") in OVERTURE_KINDS:
            row = match_for(base, overture, lists, geometry)
        result.checked += 1
        values = values_from(row, base, lists) if row else {}
        if row:
            result.differs.extend(differences(base, row, lists))
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
            new["updated"] = now
            result.records[place_id] = new
    result.candidates, result.dropped = find_candidates(places, overture, lists)
    return result


# --------------------------------------------------------------- candidates


# The tiers, best first. strong: confidence 0.9 or more, a camping
# basic_category (not only a camping taxonomy entry) and a website of its own
# (not on a directory host). weak: every other candidate.
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
    phones = [p.strip() for p in row.get("phones") or [] if p and p.strip()]
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
    row, col = cell_of(lat, lon)
    reach_lat = math.ceil(metres / 111_000 / CELL_LAT)
    reach_lon = math.ceil(metres / (111_000 * max(math.cos(math.radians(lat)), 0.2)) / CELL_LON)
    return any(
        metres_between(lat, lon, plat, plon) <= metres
        for r in range(row - reach_lat, row + reach_lat + 1)
        for c in range(col - reach_lon, col + reach_lon + 1)
        for plat, plon in grid.get((r, c), ())
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


# ---------------------------------------------------------------- decisions


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


def apply_decisions(root: Path, decisions: Iterable[dict], *, now: str, publish: bool = False, make_id=new_place_id) -> dict:
    """Act on reviewed candidates: candidate entries, each with decision approve, reject or flag.

    A rejected candidate's GERS id goes on overture/do-not-match.json, so it is
    never a candidate again. A flagged one waits for a person. An approved one
    is published (written to places/ and linked in the link file) only when
    publish is true; otherwise it is held. A candidate whose GERS id is
    already linked to a place is not published again, and one within 250 m
    of a place (including one published in the same run, such as an
    Overture duplicate) waits for a person.

    Returns the place ids published, and the GERS ids held, rejected,
    flagged, already linked and near a place.
    """
    root = Path(root)
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
        elif not publish:
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


# --------------------------------------------------------------- the files


def load_links(root: Path) -> dict[str, dict]:
    """The link file: place id to {gers, release}. Empty when the file is missing."""
    path = Path(root) / LINKS_FILE
    if not path.exists():
        return {}
    links = {}
    with open(path, encoding="utf-8") as f:
        for text in f:
            if text.strip():
                line = json.loads(text)
                links[line["place"]] = {"gers": line["gers"], "release": line["release"]}
    return links


def write_links(root: Path, links: dict[str, dict]) -> None:
    """Write the link file, one compact line per place, sorted by place id."""
    text = "".join(dump_line({"place": place_id, **links[place_id]}) + "\n" for place_id in sorted(links))
    write_text(Path(root) / LINKS_FILE, text)


def read_extract(path: Path):
    """The extract's lines, one at a time."""
    with open(path, encoding="utf-8") as f:
        for text in f:
            if text.strip():
                yield json.loads(text)


COUNTED = ("name", "website", "phone", "address")


def kind_counts(places: dict[str, dict]) -> dict[str, dict]:
    """Per kind filled: the places, and how many have a name, website, phone and address."""
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
    return {
        "release": result.release,
        "places": live,
        "checked": result.checked,
        "changed": len(result.records),
        "change_percent": round(percent, 2),
        "auto_merge": percent < AUTO_MERGE_BELOW_PERCENT,
        "fills": len(result.fills),
        "removals": len(result.removals),
        "differs": len(result.differs),
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


def cell(text) -> str:
    return str(text if text is not None else "").replace("|", "\\|").replace("\n", " ")


def render_report(result: ImportResult, summary: dict, limit: int = REPORT_LIST_LIMIT) -> str:
    """The run's report as Markdown, used as the PR body."""
    lines = [
        "# Overture import report",
        "",
        f"Overture release {result.release}.",
        "",
        "| | Count |",
        "|---|---:|",
        f"| Places | {summary['places']} |",
        f"| Checked | {summary['checked']} |",
        f"| Matched (links) | {summary['links']} |",
        f"| Records changed | {summary['changed']} |",
        f"| Values filled or changed | {summary['fills']} |",
        f"| Values taken off | {summary['removals']} |",
        f"| Overture differs | {summary['differs']} |",
        "",
        f"Change: {summary['change_percent']:.2f}% of records.",
        "",
        "## Filled values by kind",
        "",
        "| Kind | Places | Names | Websites | Phones | Addresses |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for kind, entry in summary["kinds"].items():
        cells = [f"{entry['before'][key]} to {entry['after'][key]}" for key in COUNTED]
        lines.append(f"| {kind} | {entry['places']} | " + " | ".join(cells) + " |")
    lines += [
        "",
        "## Overture differs",
        "",
    ]
    if result.differs:
        lines += [
            "OSM's value stays unless a person takes Overture's, which then becomes `overture`-sourced.",
            "",
            "| Place | Kind | Value | OSM | Overture | Overture place |",
            "|---|---|---|---|---|---|",
        ]
        for row in result.differs[:limit]:
            loc, oloc = row["location"], row["overture_location"]
            lines.append(
                f"| {cell(row['name'])} (`{row['place']}`, {loc['lat']},{loc['lon']}) | {row['kind']} | {row['key']} "
                f"| {cell(row['osm'])} | {cell(row['overture'])} "
                f"| {cell(row['overture_name'])} (`{row['gers']}`, {oloc['lat']},{oloc['lon']}) |"
            )
        if len(result.differs) > limit:
            lines.append(f"| and {len(result.differs) - limit} more | | | | | |")
    else:
        lines.append("None.")
    lines += ["", "## Overture candidates", ""]
    lines += [
        "Camping places Overture lists with no place of ours within 250 m. None is published until a reviewer",
        "approves it and publishing is switched on (`python -m pipeline.overture_decide --publish-candidates`).",
        "",
        "| Tier or rule | Count |",
        "|---|---:|",
    ]
    lines += [f"| {tier} | {len(result.candidates.get(tier, []))} |" for tier in TIERS]
    lines += [f"| dropped: {rule} | {len(rows)} |" for rule, rows in sorted(result.dropped.items())]
    for tier in TIERS:
        rows = result.candidates.get(tier, [])
        if not rows:
            continue
        lines += ["", f"### {tier.capitalize()} ({len(rows)})", "",
                  "| Name | Category | Confidence | State | Website | Overture place |", "|---|---|---:|---|---|---|"]
        for c in rows[:limit]:
            loc = c["location"]
            lines.append(
                f"| {cell(c['name'])} | {c['category']} | {c['confidence']:.2f} | {cell(c['state'])} "
                f"| {cell(c['website'])} | `{c['gers']}`, {loc['lat']},{loc['lon']} |"
            )
        if len(rows) > limit:
            lines.append(f"| and {len(rows) - limit} more | | | | | |")
    lines += ["", "## Taken off", ""]
    if result.removals:
        lines += ["| Place | Value | Was |", "|---|---|---|"]
        lines += [f"| `{r['place']}` | {r['key']} | {cell(r['value'])} |" for r in result.removals[:limit]]
        if len(result.removals) > limit:
            lines.append(f"| and {len(result.removals) - limit} more | | |")
    else:
        lines.append("None.")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("extract", type=Path, help="Overture extract written by tools/overture-pull/pull.py")
    parser.add_argument("--root", type=Path, default=Path("."), help="data repository root")
    parser.add_argument("--geometry", type=Path, help="geometry sidecar written by pipeline.extract --geometry, for outlines")
    parser.add_argument("--changelog", action="store_true", help="check again only the linked places the changelog lists as changed or removed")
    parser.add_argument("--report", type=Path, default=Path("overture-report.md"))
    parser.add_argument("--summary", type=Path, help="also write counts as JSON here")
    parser.add_argument("--candidates", type=Path, help="also write the Overture candidates here, one line each, best tier first")
    parser.add_argument("--now", help="the run time, UTC (default: today at 00:00:00Z)")
    args = parser.parse_args(argv)

    started = time.monotonic()
    now = args.now or time.strftime("%Y-%m-%dT00:00:00Z", time.gmtime())
    places = load_places(args.root)
    result = import_overture(
        places,
        read_extract(args.extract),
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
