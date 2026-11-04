"""Match places to stay with freely licensed photos on Wikimedia Commons.

Usage:
    python -m pipeline.commons --root . [--max-places N] [--stale-days 30] [--today YYYY-MM-DD]

Writes commons/matches.ndjson: one line per place in the overnight group,
sorted by place id, {"place", "checked", "files"}. files lists up to
MAX_FILES Commons files that are likely to show the place, best first, each
with the credit and licence a reader must show beside it. A place with no
match has an empty list, so the next run knows it was checked.

Each run checks the places never checked and those checked more than
--stale-days days ago, oldest first, at most --max-places of them, and keeps
every other line as it was. A place that is no longer in the overnight group,
or was merged, loses its line.

One Commons API request per place: geotagged files within SEARCH_RADIUS_M of
the place, with their licence, size, categories and distance. A file is kept
only when its licence is on the allowlist, it is a photo of at least
MIN_WIDTH by MIN_HEIGHT, it carries no personality rights warning, and it
matches the place by one of these, in this order of trust:

    name     the place's distinctive name words and a camping word appear in
             the file's title or categories, within NAME_RADIUS_M
    camping  a camping word appears, within CAMPING_RADIUS_M
    area     the distinctive name words appear without a camping word, within
             AREA_RADIUS_M: a view of the named place's surroundings

The requests follow the Wikimedia User-Agent policy and the API etiquette:
one at a time, maxlag=5, a pause between requests, and a back-off when the
API asks for one.
"""

import argparse
import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

from .common import dump_line, load_places, write_text

API = "https://commons.wikimedia.org/w/api.php"
USER_AGENT = "OpenCampingDB-pipeline/1.0 (+https://github.com/opencampingdb/data)"
MATCHES_FILE = Path("commons") / "matches.ndjson"
GROUP = "overnight"

MAX_FILES = 5
SEARCH_RADIUS_M = 2000
NAME_RADIUS_M = 2000
CAMPING_RADIUS_M = 250
AREA_RADIUS_M = 500
MIN_WIDTH = 800
MIN_HEIGHT = 500
# Standard thumbnail widths Wikimedia renders ahead of time (Robot policy: "Only use
# originals or one of our standard thumbnail sizes").
THUMB_WIDTH = 960
FULL_WIDTH = 1920
PAUSE_S = 0.25
MATCH_ORDER = ("name", "camping", "area")

# API errors that mean "try again shortly".
RETRY_CODES = {"maxlag", "cirrussearch-too-busy-error", "ratelimited"}

PHOTO_MIMES = {"image/jpeg", "image/png", "image/webp", "image/tiff"}

# Licences that let anyone use the photo for any purpose with credit. GFDL alone
# is left out: it asks for the whole licence text to travel with the photo.
LICENCE_RE = re.compile(
    r"^(cc0( 1\.0)?|cc zero|public domain|pd|no restrictions|"
    r"cc[ -]by(-sa)? [1-4]\.[05]( [a-z]{2,3})?)$"
)

CAMPING_RE = re.compile(
    r"\b(camp|camps|camping|campground|campgrounds|campsite|campsites|caravan|caravans|"
    r"caravanning|tent|tents|holiday park|tourist park|rest area|camping ground|bush camp)\b"
)

# Words that say what kind of place it is or where, not which one. A name made
# only of these has no distinctive words and matches only by camping words.
GENERIC_WORDS = {
    "and", "the", "of", "at", "on", "in", "by", "to", "for",
    "camp", "camps", "camping", "campground", "campgrounds", "campsite", "campsites",
    "caravan", "caravans", "park", "parks", "holiday", "holidays", "tourist", "tourism",
    "national", "state", "forest", "reserve", "conservation", "area", "areas", "rest",
    "free", "bush", "site", "sites", "ground", "grounds", "village", "resort", "resorts",
    "cabin", "cabins", "motel", "motor", "inn", "lodge", "hotel", "hostel", "backpackers",
    "backpacker", "apartments", "apartment", "accommodation", "retreat", "stay", "farm",
    "station", "showground", "showgrounds", "big4", "discovery", "nrma", "ingenia",
    "top", "reflections", "lake", "lakes", "river", "creek", "beach", "bay",
    "point", "mount", "mt", "north", "south", "east", "west", "upper", "lower",
    "little", "big", "new", "old", "day", "use", "road", "track", "dam", "spring",
    "springs", "falls", "waterhole", "island", "head", "heads", "gorge", "valley",
    "tent", "tents", "rv", "pet", "friendly", "pty", "ltd", "co",
}

TAG_RE = re.compile(r"<[^>]+>")
WORD_RE = re.compile(r"[a-z0-9]+")


def words(text: str) -> list[str]:
    """Lower-case words of letters and digits, accents kept as they are."""
    return WORD_RE.findall(text.lower())


def distinctive_words(name: str | None) -> set[str]:
    """The words of a place name that say which place it is."""
    return {w for w in words(name or "") if len(w) >= 3 and not w.isdigit() and w not in GENERIC_WORDS}


def plain_text(value: str | None, limit: int = 200) -> str:
    """Text of an extmetadata HTML value, tags removed and spaces collapsed."""
    text = html.unescape(TAG_RE.sub(" ", value or ""))
    text = " ".join(text.split())
    return text[:limit].rstrip()


def meta(info: dict, key: str) -> str:
    return (info.get("extmetadata", {}).get(key) or {}).get("value") or ""


def allowed_licence(short_name: str) -> bool:
    return bool(LICENCE_RE.match(" ".join(short_name.lower().split())))


def full_url(info: dict) -> str:
    """The FULL_WIDTH standard thumbnail, or the original when it is no wider than that."""
    thumb = info.get("thumburl", "")
    if info.get("width", 0) > FULL_WIDTH and f"/{THUMB_WIDTH}px-" in thumb:
        return thumb.replace(f"/{THUMB_WIDTH}px-", f"/{FULL_WIDTH}px-")
    return info.get("url", "")


def match_kind(name_words: set[str], text: str, distance_m: float) -> str | None:
    """How a file matches a place: "name", "camping", "area", or None."""
    file_words = set(words(text))
    named = bool(name_words) and name_words <= file_words
    camping = bool(CAMPING_RE.search(text.lower()))
    if named and camping and distance_m <= NAME_RADIUS_M:
        return "name"
    if camping and distance_m <= CAMPING_RADIUS_M:
        return "camping"
    if named and distance_m <= AREA_RADIUS_M:
        return "area"
    return None


def file_entry(page: dict, name_words: set[str]) -> dict | None:
    """The matches.ndjson entry for one API page, or None when the file is not kept."""
    infos = page.get("imageinfo") or []
    coords = page.get("coordinates") or []
    if not infos or not coords:
        return None
    info = infos[0]
    if info.get("mime") not in PHOTO_MIMES:
        return None
    width, height = info.get("width", 0), info.get("height", 0)
    if max(width, height) < MIN_WIDTH or min(width, height) < MIN_HEIGHT:
        return None
    licence = plain_text(meta(info, "LicenseShortName"), 60)
    if not allowed_licence(licence):
        return None
    if "personality" in meta(info, "Restrictions").lower():
        return None
    credit = plain_text(meta(info, "Attribution")) or plain_text(meta(info, "Artist"))
    if not credit and not licence.lower().startswith(("cc0", "cc zero", "public domain", "pd", "no restrictions")):
        return None
    title = page.get("title", "")
    if not title.startswith("File:"):
        return None
    categories = " ".join(c.get("title", "").removeprefix("Category:") for c in page.get("categories") or [])
    text = " ".join([title.removeprefix("File:").rsplit(".", 1)[0].replace("_", " "), plain_text(meta(info, "ObjectName")), categories])
    distance_m = float(coords[0].get("dist", SEARCH_RADIUS_M + 1))
    kind = match_kind(name_words, text, distance_m)
    if not kind:
        return None
    entry = {
        "file": title.removeprefix("File:"),
        "width": width,
        "height": height,
        "thumb": info.get("thumburl") or info.get("url", ""),
        "full": full_url(info),
        "page": info.get("descriptionurl") or f"https://commons.wikimedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}",
        "licence": licence,
        "match": kind,
        "distance_m": round(distance_m),
    }
    licence_url = meta(info, "LicenseUrl")
    if licence_url.startswith("https://") or licence_url.startswith("http://"):
        entry["licence_url"] = licence_url.replace("http://", "https://", 1)
    if credit:
        entry["credit"] = credit
    return entry


def rank(entries: list[dict]) -> list[dict]:
    """Best first: by match kind, then nearest, then file name; at most MAX_FILES."""
    entries = sorted(entries, key=lambda e: (MATCH_ORDER.index(e["match"]), e["distance_m"], e["file"]))
    return entries[:MAX_FILES]


def query_url(lat: float, lon: float) -> str:
    params = {
        "action": "query",
        "format": "json",
        "formatversion": "2",
        "maxlag": "5",
        "generator": "geosearch",
        "ggscoord": f"{lat}|{lon}",
        "ggsradius": str(SEARCH_RADIUS_M),
        "ggsnamespace": "6",
        "ggslimit": "50",
        "prop": "imageinfo|categories|coordinates",
        "iiprop": "url|size|mime|extmetadata",
        "iiurlwidth": str(THUMB_WIDTH),
        "iiextmetadatafilter": "LicenseShortName|LicenseUrl|Artist|Attribution|Restrictions|ObjectName",
        "cllimit": "max",
        "clshow": "!hidden",
        "colimit": "max",
        "codistancefrompoint": f"{lat}|{lon}",
    }
    return f"{API}?{urllib.parse.urlencode(params)}"


def fetch_json(url: str, attempts: int = 6) -> dict:
    """GET a Commons API URL, waiting and trying again on maxlag, 429 and 5xx answers."""
    wait = 5.0
    for attempt in range(attempts):
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = json.load(response)
            if body.get("error", {}).get("code") in RETRY_CODES:
                time.sleep(wait)
                wait *= 2
                continue
            if "error" in body:
                raise RuntimeError(f"Commons API error: {body['error']}")
            return body
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504) or attempt == attempts - 1:
                raise
            retry_after = e.headers.get("Retry-After", "")
            time.sleep(float(retry_after) if retry_after.isdigit() else wait)
            wait *= 2
        except (urllib.error.URLError, TimeoutError):
            if attempt == attempts - 1:
                raise
            time.sleep(wait)
            wait *= 2
    raise RuntimeError(f"Commons API kept asking to wait: {url}")


def pages_for(body: dict) -> list[dict]:
    return body.get("query", {}).get("pages", [])


def match_place(place: dict, fetch=fetch_json) -> list[dict]:
    """The ranked Commons files for one place record."""
    location = place["location"]
    body = fetch(query_url(location["lat"], location["lon"]))
    name_words = distinctive_words(place.get("name"))
    entries = [e for e in (file_entry(p, name_words) for p in pages_for(body)) if e]
    return rank(entries)


def load_matches(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    lines = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                record = json.loads(line)
                lines[record["place"]] = record
    return lines


def places_to_match(places: dict[str, dict]) -> dict[str, dict]:
    """Places in the overnight group that a release carries."""
    return {
        place_id: record
        for place_id, record in places.items()
        if record.get("group") == GROUP
        and record.get("status") != "merged"
        and record.get("standing") not in {"held", "hidden"}
    }


def due(matches: dict[str, dict], candidates: dict[str, dict], today: date, stale_days: int) -> list[str]:
    """Place ids to check, never checked first, then the oldest checks."""
    cutoff = (today - timedelta(days=stale_days)).isoformat()
    unchecked = sorted(p for p in candidates if p not in matches)
    stale = sorted((matches[p]["checked"], p) for p in candidates if p in matches and matches[p]["checked"] <= cutoff)
    return unchecked + [p for _, p in stale]


def write_matches(path: Path, matches: dict[str, dict]) -> None:
    write_text(path, "".join(dump_line(matches[p]) + "\n" for p in sorted(matches)))


def run(root: Path, today: date, max_places: int | None, stale_days: int, fetch=fetch_json, pause: float = PAUSE_S, log=print) -> dict:
    path = root / MATCHES_FILE
    candidates = places_to_match(load_places(root))
    matches = {p: m for p, m in load_matches(path).items() if p in candidates}
    todo = due(matches, candidates, today, stale_days)
    if max_places is not None:
        todo = todo[:max_places]
    checked = matched = 0
    for i, place_id in enumerate(todo):
        files = match_place(candidates[place_id], fetch)
        matches[place_id] = {"place": place_id, "checked": today.isoformat(), "files": files}
        checked += 1
        matched += bool(files)
        # Saved as it goes, so a stopped run keeps what it found.
        if (i + 1) % 200 == 0:
            write_matches(path, matches)
            log(f"Checked {i + 1} of {len(todo)} places, {matched} with a photo.")
        if pause:
            time.sleep(pause)
    write_matches(path, matches)
    with_photo = sum(1 for m in matches.values() if m["files"])
    summary = {"checked": checked, "matched": matched, "places": len(candidates), "with_photo": with_photo, "lines": len(matches)}
    log(json.dumps(summary))
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--max-places", type=int, help="check at most this many places this run")
    parser.add_argument("--stale-days", type=int, default=30, help="check a place again after this many days (default 30)")
    parser.add_argument("--today", help="the check date, YYYY-MM-DD (default: today, UTC)")
    args = parser.parse_args(argv)
    today = date.fromisoformat(args.today) if args.today else datetime_today()
    run(args.root, today, args.max_places, args.stale_days)
    return 0


def datetime_today() -> date:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).date()


if __name__ == "__main__":
    sys.exit(main())
