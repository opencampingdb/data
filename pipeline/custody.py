"""Follow a place when its OSM object leaves the extract.

A record follows one OSM object (osm). When that object is deleted, loses
its tags or is tagged as something the extract does not take, the place is
often still there, mapped as another object: a mapper merged a point into
the park's outline, redrew it, or fixed a place mapped twice. OSM keeps the
whole chain of custody: every version of an object names the changeset that
made it, and a changeset lists every object it touched. resolve() reads it,
so a record keeps its id, and everything that hangs off the id, whatever the
mapper did.

For each live record whose object is not in the extract, in this order:

1. Its own links. When an object in the record's merged_osm is still in the
   extract (Open Camping DB merged the two records, then a mapper removed
   the other object in OSM), the record follows it. With more than one, the
   history decides.
2. The history (with an OsmHistory). The object's latest version and the
   changeset that made it:
   - tags under a lifecycle prefix (disused:, abandoned:, was:, demolished:,
     razed:, removed:): closed in OSM, for a person, with the changeset;
   - other objects in that changeset that took over (a way or relation that
     now holds the old node, or an object given the same name and main tag),
     when exactly one of them is in the extract: the record follows it. When
     that object has a record of its own, or another record follows it now,
     the two are merged (pipeline.merge) and the older id is kept, the one
     most likely to carry what travellers sent, unless either has an owner;
   - still there with tags the extract does not take: retagged, for a person;
   - deleted, or its tags taken off, with no object that took over: for a
     person, with the changeset.
3. Nearby (drawn_again): exactly one object with no record, in the same
   group, with a matching name, whose outline holds the old point or lies
   within MAX_DISTANCE_M of it, with nothing in pipeline.geometry keeping them
   apart, and claimed by no other such record. This covers a place redrawn in
   a later changeset than the one that removed it.

Records a person has closed are left as they are. An object two records could
follow is followed by neither. A record that follows another object keeps the
old one in merged_osm, so the old object never becomes a record of its own if
it comes back. The sync checks the followed record against the schema before
it merges or writes anything, and a record it refuses stays listed as gone.

What the history says about each object is kept in osm/history.ndjson, one
line per object, so each is looked up once while it stays gone, and again
after RECHECK_DAYS. The file
holds object ids, versions, changeset ids, the main tags and the objects that
took over, and nothing about the mapper.
"""

import http.client
import json
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

from .common import KIND_GROUP, write_text
from .duplicates import MAX_DISTANCE_M, NAME_RATIO, differ_only_in_numbers, normalise_name, tidy_name
from .geometry import Geometry, judge

HISTORY_FILE = Path("osm") / "history.ndjson"
API = "https://api.openstreetmap.org/api/0.6"
USER_AGENT = "OpenCampingDB-pipeline (+https://github.com/opencampingdb/data)"
# The OSM API usage policy asks for moderate use: one request at a time, with a pause.
PAUSE_S = 1.0
# A verdict on an object still gone is checked again after this many days, in case a mapper changed it since.
RECHECK_DAYS = 30
# Errors reading the OSM API: the record stays gone, and the next run tries again.
READ_ERRORS = (OSError, http.client.HTTPException, ValueError, ET.ParseError, KeyError)
# A changeset larger than this is a bulk edit: it is not downloaded, and the nearby rule decides.
MAX_CHANGESET_CHANGES = 2000
LIFECYCLE_PREFIXES = ("disused:", "abandoned:", "was:", "demolished:", "razed:", "removed:")
# Tags that say what an object is. The extract's kinds come from these.
MAIN_KEYS = ("tourism", "amenity", "leisure", "highway", "shop", "historic", "natural", "landuse", "man_made", "boundary")


def key_of(osm: dict) -> tuple[str, int]:
    return osm["type"], osm["id"]


def ref(key: tuple[str, int]) -> str:
    return f"{key[0]}/{key[1]}"


# ------------------------------------------------------------- the history


def main_tags(tags: dict) -> dict:
    """The tags that say what an object is, its name, and any of them under a lifecycle prefix."""
    out = {}
    for k, v in tags.items():
        bare = next((k[len(p):] for p in LIFECYCLE_PREFIXES if k.startswith(p)), k)
        if bare in MAIN_KEYS or bare == "name":
            out[k] = v
    return out


def successors(old: tuple[str, int], old_tags: dict, change: ET.Element) -> list[tuple[str, int]]:
    """Objects in a changeset (osmChange) that took over from old: they hold the old node, or got its name and main tag."""
    name = old_tags.get("name")
    main = {(k, v) for k, v in old_tags.items() if k in MAIN_KEYS}
    found = []
    for action in change:
        if action.tag not in ("create", "modify"):
            continue
        for el in action:
            key = (el.tag, int(el.get("id")))
            if key == old:
                continue
            tags = {t.get("k"): t.get("v") for t in el.findall("tag")}
            holds = old[0] == "node" and any(int(n.get("ref")) == old[1] for n in el.findall("nd"))
            holds = holds or any(
                m.get("type") == old[0] and int(m.get("ref")) == old[1] for m in el.findall("member")
            )
            same = name is not None and tidy_name(tags.get("name")) == tidy_name(name) and bool(main & set(tags.items()))
            shares = bool(main & set(tags.items())) or (name is not None and tidy_name(tags.get("name")) == tidy_name(name))
            if (holds and shares) or same:
                found.append(key)
    return sorted(set(found))


def read_history(text: str) -> dict:
    """The latest version of an object, from its /history.json, and the main tags of the last version that had any."""
    versions = json.loads(text)["elements"]
    last = versions[-1]
    tagged = next((v for v in reversed(versions) if main_tags(v.get("tags") or {})), None)
    return {
        "version": last["version"],
        "changeset": last["changeset"],
        "visible": last.get("visible", True),
        "tags": main_tags(last.get("tags") or {}),
        "had": main_tags(tagged.get("tags") or {}) if tagged else {},
    }


def default_fetch(url: str) -> str | None:
    """GET url from the OSM API. None for 404 or 410 (an object or changeset that is not there).

    Tries three times, waiting as long as a 429 or 503 asks (Retry-After), and
    raises one of READ_ERRORS when the API cannot be read.
    """
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(3):
        wait = PAUSE_S * 10
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                return None
            if attempt == 2:
                raise
            retry_after = e.headers.get("Retry-After") if e.headers else None
            if retry_after and retry_after.isdigit():
                wait = min(int(retry_after), 120)
        except READ_ERRORS:
            if attempt == 2:
                raise
        time.sleep(wait)
    return None


@dataclass
class OsmHistory:
    """What OSM's history says about objects, read through fetch and kept in a cache file."""

    path: Path | None = None
    fetch: object = default_fetch
    pause_s: float = PAUSE_S
    entries: dict[str, dict] = field(default_factory=dict)
    looked_up: int = 0
    failed: int = 0

    def __post_init__(self):
        if self.path is not None and self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    entry = json.loads(line)
                    self.entries[entry["object"]] = entry

    def lookup(self, key: tuple[str, int], now: str | None = None) -> dict | None:
        """The entry for key: its latest version, changeset and successors. None when OSM could not be read.

        A cached entry older than RECHECK_DAYS (by its checked date, against now) is read again.
        """
        cached = self.entries.get(ref(key))
        if cached is not None and not _stale(cached, now):
            return cached
        try:
            text = self._get(f"{API}/{key[0]}/{key[1]}/history.json")
            if text is None:
                return None
            entry = {"object": ref(key), **read_history(text)}
            entry["successors"] = []
            if not entry["tags"] or not entry["visible"]:
                meta = self._get(f"{API}/changeset/{entry['changeset']}.json")
                count = json.loads(meta)["changeset"].get("changes_count", 0) if meta else 0
                if meta and count <= MAX_CHANGESET_CHANGES:
                    change = self._get(f"{API}/changeset/{entry['changeset']}/download")
                    if change:
                        entry["successors"] = [ref(k) for k in successors(key, entry["had"], ET.fromstring(change))]
                else:
                    entry["bulk"] = True
        except READ_ERRORS:
            self.failed += 1
            return cached
        if now:
            entry["checked"] = now[:10]
        self.entries[ref(key)] = entry
        self.looked_up += 1
        return entry

    def _get(self, url: str) -> str | None:
        if self.pause_s:
            time.sleep(self.pause_s)
        return self.fetch(url)

    def save(self, keep: set[str]) -> None:
        """Write the cache, keeping only the objects in keep (those still gone)."""
        if self.path is None:
            return
        lines = [json.dumps(self.entries[k], sort_keys=True, ensure_ascii=False) for k in sorted(keep) if k in self.entries]
        write_text(self.path, "".join(line + "\n" for line in lines))


def _stale(entry: dict, now: str | None) -> bool:
    if not now or "checked" not in entry:
        return False
    from datetime import date

    return (date.fromisoformat(now[:10]) - date.fromisoformat(entry["checked"])).days >= RECHECK_DAYS


# -------------------------------------------------------------- nearby rule


def names_match(a: str | None, b: str | None) -> bool:
    """True when two names say the same place: both named, the same once tidied or close once generic words go."""
    if not tidy_name(a) or not tidy_name(b):
        return False
    if tidy_name(a) == tidy_name(b):
        return True
    name_a, name_b = normalise_name(a), normalise_name(b)
    if not name_a or not name_b or differ_only_in_numbers(name_a, name_b):
        return False
    return SequenceMatcher(None, name_a, name_b).ratio() >= NAME_RATIO


def drawn_again(record: dict, place: dict, geometry: Geometry) -> bool:
    """True when place, an extract object, is record's place drawn again in OSM (rule 3 above)."""
    # A record from before the OTWD vocabulary may have no group: it never moves, and stays for a person.
    if record.get("group") is None or KIND_GROUP.get(place["kind"]) != record["group"]:
        return False
    if not names_match(record.get("name"), place.get("name")):
        return False
    verdict = judge(record, place, geometry, MAX_DISTANCE_M)
    # Within MAX_DISTANCE_M, judge has looked for a major road between them.
    return not verdict.separate and verdict.gap_m <= MAX_DISTANCE_M


# Cells for the nearby rule, about 2 km on a side: an outline's location can sit well inside a large park.
CELL = 0.02


def _cell(record: dict) -> tuple[int, int]:
    return int(record["location"]["lat"] // CELL), int(record["location"]["lon"] // CELL)


def nearby_moves(gone: list[dict], unclaimed: list[dict], geometry: Geometry) -> dict[str, tuple[str, int]]:
    """Record id to the one unclaimed object it follows by the nearby rule, claimed by no other gone record."""
    cells: dict[tuple[int, int], list[dict]] = {}
    for place in unclaimed:
        cells.setdefault(_cell(place), []).append(place)
    matches: dict[str, list[tuple[str, int]]] = {}
    claimants: dict[tuple[str, int], list[str]] = {}
    for record in gone:
        lat, lon = _cell(record)
        near = [p for dy in (-1, 0, 1) for dx in (-1, 0, 1) for p in cells.get((lat + dy, lon + dx), [])]
        found = [key_of(p["osm"]) for p in near if drawn_again(record, p, geometry)]
        matches[record["id"]] = found
        for key in found:
            claimants.setdefault(key, []).append(record["id"])
    return {rid: found[0] for rid, found in matches.items() if len(found) == 1 and claimants[found[0]] == [rid]}


# ----------------------------------------------------------------- resolve


@dataclass
class Resolution:
    """What resolve() found for one record whose object left the extract.

    outcome: "own" (follows an object in its merged_osm), "history" (follows
    the object that took over in the changeset), "merged" (that object had a
    record, merged into this one), "nearby" (rule 3), or for a person
    "closed_in_osm", "retagged", "deleted" (deleted or its tags taken off,
    with no object that took over), "unclear" (more than one candidate, or one a rule may not merge) or
    "not_looked_up" (no history, or OSM could not be read). The sync sets
    "refused" when the place schema refuses the record it would follow new as.
    """

    record_id: str
    old: tuple[str, int]
    outcome: str
    new: tuple[str, int] | None = None
    changeset: int | None = None
    merged_id: str | None = None
    tags: dict | None = None
    # On "merged", once the sync has merged them: the record kept (the older id).
    kept_id: str | None = None

    @property
    def follows(self) -> bool:
        return self.new is not None and self.outcome != "refused"


def relink(record: dict, new: tuple[str, int]) -> dict:
    """A copy of record pointed at new, keeping its old object, and leaving new out, in merged_osm."""
    record = dict(record)
    old = key_of(record["osm"])
    links = {key_of(m): m for m in record.get("merged_osm", [])}
    links.pop(new, None)
    links.setdefault(old, {"type": old[0], "id": old[1]})
    record["merged_osm"] = [links[k] for k in sorted(links)]
    # No version, so the sync takes the new object's timestamp rather than the old one's.
    record["osm"] = {"type": new[0], "id": new[1]}
    return record


def mergeable(record: dict, other: dict) -> bool:
    """Whether a rule may merge other into record: never an owner's listing or a record with a value the owner set."""
    for r in (record, other):
        if r.get("owner_verified") or "owner" in r.get("fact_sources", {}).values():
            return False
    return True


def resolve(
    places: dict[str, dict],
    extract: list[dict],
    geometry: Geometry | None = None,
    history: OsmHistory | None = None,
    now: str | None = None,
) -> list[Resolution]:
    """What to do with every open record whose object left the extract. Changes nothing.

    A record a person has closed is left as it is. An object two records could
    follow is followed by neither: both are "unclear", for a person.
    """
    geometry = geometry or Geometry()
    in_extract = {key_of(p["osm"]): p for p in extract}
    live = [r for r in places.values() if r.get("status") != "merged"]
    owner: dict[tuple[str, int], str] = {}
    for r in live:
        if r.get("osm"):
            owner[key_of(r["osm"])] = r["id"]
    for r in live:
        for m in r.get("merged_osm", []):
            owner.setdefault(key_of(m), r["id"])
    gone = sorted(
        (r for r in live if r.get("osm") and r.get("status") != "closed" and key_of(r["osm"]) not in in_extract),
        key=lambda r: r["id"],
    )

    # First what each record could follow, then each object claimed by exactly one record.
    proposed: dict[str, Resolution] = {}
    out: list[Resolution] = []
    unresolved: list[dict] = []
    said: dict[str, tuple[str, int | None]] = {}
    # Records whose heir is another record's merged object: merged when that record follows it too.
    pending: list[tuple[dict, tuple[str, int], str, int]] = []
    for record in gone:
        old = key_of(record["osm"])
        parts = {key_of(m) for m in record.get("merged_osm", []) if m.get("part")}
        # A part drawn apart on purpose is not the place itself, so a record never follows one.
        own = sorted(
            key_of(m)
            for m in record.get("merged_osm", [])
            if key_of(m) not in parts and key_of(m) in in_extract and owner.get(key_of(m)) == record["id"]
        )
        if len(own) == 1:
            proposed[record["id"]] = Resolution(record["id"], old, "own", new=own[0])
            continue
        entry = history.lookup(old, now) if history is not None else None
        if entry is None:
            if own:
                out.append(Resolution(record["id"], old, "unclear"))
            else:
                unresolved.append(record)
            continue
        changeset = entry["changeset"]
        lifecycle = {k: v for k, v in entry["tags"].items() if k.startswith(LIFECYCLE_PREFIXES)}
        if entry["visible"] and lifecycle:
            out.append(Resolution(record["id"], old, "closed_in_osm", changeset=changeset, tags=lifecycle))
            continue
        if entry["visible"] and entry["tags"]:
            out.append(Resolution(record["id"], old, "retagged", changeset=changeset, tags=entry["tags"]))
            continue
        heirs = [k for k in (tuple_of(s) for s in entry["successors"]) if k in in_extract]
        if not heirs:
            # Deleted or its tags taken off, with no object in the extract that took over.
            unresolved.append(record)
            said[record["id"]] = ("deleted", changeset)
            continue
        heir = heirs[0]
        holder = owner.get(heir)
        if len(heirs) > 1 or heir in parts:
            out.append(Resolution(record["id"], old, "unclear", changeset=changeset))
        elif holder is None or holder == record["id"]:
            proposed[record["id"]] = Resolution(record["id"], old, "history", new=heir, changeset=changeset)
        elif (
            places[holder].get("osm")
            and key_of(places[holder]["osm"]) == heir
            and places[holder].get("status") != "closed"
            and mergeable(record, places[holder])
        ):
            proposed[record["id"]] = Resolution(record["id"], old, "merged", new=heir, changeset=changeset, merged_id=holder)
        elif (
            places[holder].get("status") != "closed"
            and any(key_of(m) == heir and not m.get("part") for m in places[holder].get("merged_osm", []))
            and mergeable(record, places[holder])
        ):
            pending.append((record, heir, holder, changeset))
        else:
            out.append(Resolution(record["id"], old, "unclear", changeset=changeset))
    for record, heir, holder, changeset in pending:
        if holder in proposed and proposed[holder].new == heir:
            proposed[record["id"]] = Resolution(record["id"], key_of(record["osm"]), "merged", new=heir, changeset=changeset, merged_id=holder)
        else:
            out.append(Resolution(record["id"], key_of(record["osm"]), "unclear", changeset=changeset))

    claimed = {k for r in proposed.values() for k in [r.new]} | set(owner)
    unclaimed = [p for k, p in in_extract.items() if k not in claimed]
    for rid, new in nearby_moves(unresolved, unclaimed, geometry).items():
        record = places[rid]
        proposed[rid] = Resolution(rid, key_of(record["osm"]), "nearby", new=new, changeset=said.get(rid, (None, None))[1])
    for record in unresolved:
        if record["id"] not in proposed:
            outcome, changeset = said.get(record["id"], ("not_looked_up", None))
            out.append(Resolution(record["id"], key_of(record["osm"]), outcome, changeset=changeset))

    # An object is followed by one record. A merge claims the record it merges with, and only once.
    claims: dict[object, list[str]] = {}

    def claim(res: Resolution):
        return ("record", res.merged_id) if res.outcome == "merged" else res.new

    for res in proposed.values():
        claims.setdefault(claim(res), []).append(res.record_id)
    for res in proposed.values():
        if len(claims[claim(res)]) == 1:
            out.append(res)
        else:
            out.append(Resolution(res.record_id, res.old, "unclear", changeset=res.changeset))
    return sorted(out, key=lambda r: r.old)


def tuple_of(text: str) -> tuple[str, int]:
    kind, number = text.split("/")
    return kind, int(number)
