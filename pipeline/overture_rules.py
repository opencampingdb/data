"""The Overture match and name source rules, and the files they read.

The kept lists in overture/, the Overture extract as read for one run, the
link file, and the rules that pick the Overture place a record takes values
from (its Overture match or its name source) and the values it gives. See
pipeline/README.md "Overture import".
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from shapely.geometry import Point
from shapely.ops import nearest_points

from .common import KIND_GROUP, OVERTURE, dump_line, load_taken, write_text
from .duplicates import distance_m
from .geometry import Geometry, osm_ref
from .overture_socials import (
    SOCIAL_FACTS,
    SOCIAL_HOST_LIST,
    all_social_pages,
    host_of,
    on_host,
    page_key,
    social_pages,
)


# The kinds the importer fills. Every other kind is left alone.
OVERTURE_KINDS = tuple(sorted(
    [kind for kind, group in KIND_GROUP.items() if group == "overnight"]
    + [
        "information_centre", "fuel", "lpg_refill_swap", "mechanic", "food_and_drink",
        "laundromat", "hospital_emergency", "attraction", "heritage", "experience",
    ]
))


# The facts an Overture match or a name source fills, besides a name for an unnamed place.
FILLED_FACTS = ("website", "phone", "address", *SOCIAL_FACTS)

# Changelog entries that mean an Overture place has not changed (None: the
# changelog does not list it). A changelog run does not check a place linked
# to one of these again.
UNCHANGED = ("unchanged", None)
# Changelog entries a changelog run matches places with no link against.
FRESH = ("added", "data_changed")

# A website or social page Overture gives for more than this many places in
# one run is a chain's or an agency's shared page (Shell's Facebook page on
# hundreds of stations), not any one place's: it is taken for none of them
# and never compared in "Overture differs".
SHARED_PAGE_LIMIT = 5

# The Overture match (the strong match), for a named place.
MATCH_DISTANCE_M = 100
MATCH_NAME_SIMILARITY = 0.85
MATCH_CONFIDENCE = 0.7
# The name source, for an unnamed place.
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
    taxonomy primary) that fit it. directory_hosts: hosts whose websites are
    never taken or compared (a subdomain counts too). Social hosts are never
    the website either (SOCIAL_HOSTS). do_not_match: (place id, GERS id) pairs never matched.
    candidate_kinds: each camping category that makes an Overture candidate,
    and the kind it publishes as. chains: chain-only names, normalised.
    rejected: GERS ids of rejected candidates, never candidates again.
    taken: (place id, fact) to the Overture value a person or the reviewer
    chose over OSM's in "Overture differs" (overture/taken.json).
    """

    categories: dict[str, set[str]]
    directory_hosts: tuple[str, ...]
    do_not_match: frozenset[tuple[str, str]] = frozenset()
    candidate_kinds: dict[str, str] = field(default_factory=dict)
    chains: tuple[str, ...] = ()
    rejected: frozenset[str] = frozenset()
    taken: dict[tuple[str, str], object] = field(default_factory=dict)


def load_kept_lists(root: Path) -> KeptLists:
    """Read the kept lists from the repository's overture/ folder.

    categories.json, directory-hosts.json, and when present do-not-match.json,
    chains.json and taken.json.
    """
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
        taken=load_taken(root),
    )


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


@dataclass
class Overture:
    """The Overture places that could fit a kind, filed by grid cell."""

    release: str
    places: dict[str, dict]
    grid: dict[tuple[int, int], list[dict]]
    changes: dict[str, str | None]


def cell_of(lat: float, lon: float) -> tuple[int, int]:
    return math.floor(lat / CELL_LAT), math.floor(lon / CELL_LON)


def cells_within(lat: float, lon: float, metres: float) -> list[tuple[int, int]]:
    """The grid cells that hold every point within metres of a point."""
    row, col = cell_of(lat, lon)
    reach_lat = math.ceil(metres / 111_000 / CELL_LAT)
    reach_lon = math.ceil(metres / (111_000 * max(math.cos(math.radians(lat)), 0.2)) / CELL_LON)
    return [
        (r, c)
        for r in range(row - reach_lat, row + reach_lat + 1)
        for c in range(col - reach_lon, col + reach_lon + 1)
    ]


def at(lat: float, lon: float) -> dict:
    """A point in the form duplicates.distance_m takes."""
    return {"location": {"lat": lat, "lon": lon}}


def metres_apart(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres (duplicates.distance_m)."""
    return distance_m(at(lat1, lon1), at(lat2, lon2))


def phones_of(row: dict) -> list[str]:
    """An Overture place's phones, trimmed, leaving out empty ones."""
    return [p.strip() for p in row.get("phones") or [] if p and p.strip()]


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
    for cell in cells_within(lat, lon, metres):
        for candidate in overture.grid.get(cell, ()):
            d = metres_apart(lat, lon, candidate["lat"], candidate["lon"])
            if d <= metres:
                out.append((d, candidate))
    return out


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


def strong_match(record: dict, overture: Overture, lists: KeptLists, fresh_only: bool = False) -> dict | None:
    """A named place's Overture match: the one Overture place that strongly matches it, or None.

    With fresh_only, only Overture places the changelog lists as added or changed count.
    """
    name = normalised_name(record["name"])
    if not name:
        return None  # a name with nothing to compare (no letters a-z or digits) matches nothing
    loc = record["location"]
    passing = [
        row
        for _, row in near(overture, loc["lat"], loc["lon"], MATCH_DISTANCE_M)
        if (not fresh_only or row.get("change") in FRESH)
        and fits(row, record["kind"], lists.categories)
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
        if metres_apart(row["lat"], row["lon"], nearest.y, nearest.x) <= metres:
            out.append(row)
    return out


def name_source(
    record: dict, overture: Overture, lists: KeptLists, geometry: Geometry, fresh_only: bool = False
) -> dict | None:
    """An unnamed place's name source: the one confident Overture place that names it, or None.

    With fresh_only, only Overture places the changelog lists as added or changed count.
    """
    area = geometry.areas.get(osm_ref(record))
    if area is not None:
        nearby = near_outline(overture, area, NAME_DISTANCE_M)
    else:
        loc = record["location"]
        nearby = [row for _, row in near(overture, loc["lat"], loc["lon"], NAME_DISTANCE_M)]
    passing = [
        row
        for row in nearby
        if (not fresh_only or row.get("change") in FRESH)
        and (row.get("name") or "").strip()
        and fits(row, record["kind"], lists.categories)
        and (row.get("confidence") or 0) >= NAME_CONFIDENCE
        and (record["id"], row["id"]) not in lists.do_not_match
    ]
    return passing[0] if len(passing) == 1 else None


def overture_place_for(
    record: dict, overture: Overture, lists: KeptLists, geometry: Geometry, fresh_only: bool = False
) -> dict | None:
    """The Overture place a record takes values from: its Overture match when it
    has a name, its name source when it has none."""
    if record.get("name"):
        return strong_match(record, overture, lists, fresh_only)
    return name_source(record, overture, lists, geometry, fresh_only)


def offered_pages(row: dict, lists: KeptLists) -> set[str]:
    """Every website and social page an Overture place gives, as page keys."""
    return {page_key(url) for url in own_websites(row, lists)} | {page_key(page) for _, page in all_social_pages(row)}


def values_from(row: dict, base: dict, lists: KeptLists, shared: frozenset[str] = frozenset()) -> dict:
    """The values an Overture place (an Overture match or a name source) gives where the base record has none."""
    out = {}
    facts = base.get("facts", {})
    name = (row.get("name") or "").strip()
    if not base.get("name") and name and len(name) <= 300:
        out["name"] = name
    if "website" not in facts:
        websites = [url for url in own_websites(row, lists, shared) if len(url) <= 500]
        if websites:
            out["website"] = websites[0]
    if "phone" not in facts:
        phones = phones_of(row)
        if phones and len(phones[0]) <= 100:
            out["phone"] = phones[0]
    if "address" not in facts:
        address = overture_address(row.get("addresses") or [])
        if address:
            out["address"] = address
    for key, page in social_pages(row, shared).items():
        if key not in facts:
            out[key] = page
    return out


def own_websites(row: dict, lists: KeptLists, shared: frozenset[str] = frozenset()) -> list[str]:
    """Overture's websites, leaving out directory hosts, social hosts and shared pages."""
    return [
        url.strip()
        for url in row.get("websites") or []
        if url
        and re.match(r"^https?://", url.strip())
        and not on_host(host_of(url), lists.directory_hosts)
        and not on_host(host_of(url), SOCIAL_HOST_LIST)
        and page_key(url) not in shared
    ]


def differences(base: dict, row: dict, lists: KeptLists, shared: frozenset[str] = frozenset()) -> list[dict]:
    """Where Overture's website or phone disagrees with an OSM value.

    Websites are compared by host, phones as Australian numbers. They agree
    when any Overture value matches any of OSM's. A shared page is not compared.
    """
    facts, sources = base.get("facts", {}), base.get("fact_sources", {})
    out = []
    phones = phones_of(row)
    if sources.get("phone") == "osm" and phones:
        if not {phone_key(p) for p in osm_phones(facts["phone"])} & {phone_key(p) for p in phones}:
            out.append(("phone", facts["phone"], phones[0]))
    websites = own_websites(row, lists, shared)
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


def taken_values(base: dict, lists: KeptLists) -> dict:
    """The values a person took from Overture for this place, where no source but OSM has one."""
    sources = base.get("fact_sources", {})
    return {
        fact: value
        for (place_id, fact), value in lists.taken.items()
        if place_id == base["id"] and sources.get(fact, "osm") == "osm"
    }


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


def read_json_lines(path: Path):
    """A JSON-lines file's lines (an extract, candidates or decisions), one at a time, skipping blank lines."""
    with open(path, encoding="utf-8") as f:
        for text in f:
            if text.strip():
                yield json.loads(text)
