"""Match places to stay with freely licensed photos on Wikimedia Commons.

Usage:
    python -m pipeline.commons --root . [--max-places N] [--stale-days 30] [--today YYYY-MM-DD]

Writes commons/matches.ndjson: one line per place in the overnight group,
sorted by place id, {"place", "checked", "rules", "files"}. files lists up to
MAX_FILES Commons files that are likely to show the place, best first, each
with the credit and licence a reader must show beside it. A place with no
match has an empty list, so the next run knows it was checked.

Each run checks, at most --max-places of them: first the places checked under
older rules (rules below RULES), then the places never checked, then those
checked more than --stale-days days ago, oldest first. It keeps every other
line as it was. A place that is no longer in the overnight group,
or was merged, loses its line.

One Commons API request per place: geotagged files within SEARCH_RADIUS_M of
the place, with their licence, size, categories and distance. A file is kept
only when its licence is on the allowlist, it is a photo of at least
MIN_WIDTH by MIN_HEIGHT, it carries no personality rights warning, it is not
a photo of a plant or animal (a title that starts with a species name that is
also one of its categories), and it matches the place by one of these, in
this order of trust:

    name     all the place's distinctive name words appear in the file's
             title, with a camping word or the place's whole name (of two
             words or more), within NAME_RADIUS_M
    camping  a camping word appears in the title, within CAMPING_RADIUS_M, for
             a place whose name has no distinctive words (such as "Campsite
             10"); a named place matches by its name, never by a camping word
             alone, which is as often another park nearby
    area     the distinctive name words appear in the title without a camping
             word, within AREA_RADIUS_M: a view of the named place's
             surroundings

Everything counts in the title only: a category such as a town's name, or
"Campsites in Victoria", is shared by every photo there, birds on the beach
included. Categories are read only to spot a species photo. Words are compared without a plural s, and words in
brackets in a place's name, such as "(GOW)", are left out. "Camp" alone is not
a camping word: Camp Creek, a police camp and a workers' camp are not places
to stay. Historic photos (a year before 1960 in the title, or a portrait) are
left out too: they show people and the past, not the place today.

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
# The matching rules each line was checked under. Raise it when the rules
# change, and the next runs check every older line again first.
RULES = 2

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
    r"\b(camping|campground|campgrounds|campsite|campsites|camp site|camp sites|camp ground|"
    r"caravan|caravans|caravanning|holiday park|tourist park|rest area|camping ground)\b"
)
# A title that starts with a two-word species name, such as "Eopsaltria australis - ...".
SPECIES_RE = re.compile(r"^([A-Z][a-z]+ [a-z]{3,})\b")
# A historic photo: a year before 1960 in the title, or a portrait.
HISTORIC_RE = re.compile(r"\b(1[0-8][0-9]{2}|19[0-5][0-9])\b|^portrait\b", re.IGNORECASE)
BRACKETS_RE = re.compile(r"\([^)]*\)")

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
    "tent", "tents", "rv", "pet", "friendly", "pty", "ltd", "co", "flats", "coast",
}

TAG_RE = re.compile(r"<[^>]+>")
WORD_RE = re.compile(r"[a-z0-9]+")


def words(text: str) -> list[str]:
    """Lower-case words of letters and digits, accents kept as they are."""
    return WORD_RE.findall(text.lower())


def singular(word: str) -> str:
    """The word without a plural s, so Goldfields matches Goldfield."""
    return word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word


def distinctive_words(name: str | None) -> set[str]:
    """The words of a place name that say which place it is, without a plural s. Words in brackets are left out."""
    return {
        singular(w)
        for w in words(BRACKETS_RE.sub(" ", name or ""))
        if len(w) >= 3 and not w.isdigit() and w not in GENERIC_WORDS
    }


def plain_text(value: str | None, limit: int = 200) -> str:
    """Text of an extmetadata HTML value, tags removed and spaces collapsed."""
    text = html.unescape(TAG_RE.sub(" ", value or ""))
    text = " ".join(text.split())
    return text[:limit].rstrip()


def meta(info: dict, key: str) -> str:
    return (info.get("extmetadata", {}).get(key) or {}).get("value") or ""


def allowed_licence(short_name: str) -> bool:
    return bool(LICENCE_RE.match(" ".join(short_name.lower().split())))


IMAGE_HOSTS = ("https://upload.wikimedia.org/", "https://thumb.wikimedia.org/")


def image_url(url: str) -> str:
    """An image URL on a Wikimedia image host without its query (the API adds utm tracking), or "" when not."""
    url = (url or "").split("?", 1)[0]
    return url if url.startswith(IMAGE_HOSTS) else ""


def full_url(info: dict) -> str:
    """The FULL_WIDTH standard thumbnail, or the original when it is no wider than that."""
    thumb = image_url(info.get("thumburl", ""))
    if info.get("width", 0) > FULL_WIDTH and f"/{THUMB_WIDTH}px-" in thumb:
        return thumb.replace(f"/{THUMB_WIDTH}px-", f"/{FULL_WIDTH}px-")
    return image_url(info.get("url", ""))


def match_kind(name_words: set[str], title: str, distance_m: float, place_name: str | None = None) -> str | None:
    """How a file matches a place, from its title: "name", "camping", "area", or None."""
    title_words = words(title)
    named = bool(name_words) and name_words <= {singular(w) for w in title_words}
    camping = bool(CAMPING_RE.search(title.lower()))
    whole_name = " ".join(words(BRACKETS_RE.sub(" ", place_name or "")))
    # A one-word name such as "Johanna" is in every title about the beach, so the whole name counts from two words.
    says_name = len(whole_name.split()) >= 2 and f" {whole_name} " in f" {' '.join(title_words)} "
    if named and (camping or says_name) and distance_m <= NAME_RADIUS_M:
        return "name"
    if not name_words and camping and distance_m <= CAMPING_RADIUS_M:
        return "camping"
    if named and distance_m <= AREA_RADIUS_M:
        return "area"
    return None


def is_species_photo(title_text: str, categories: list[str]) -> bool:
    """A photo of a plant or animal: its title starts with a species name that is also a category."""
    m = SPECIES_RE.match(title_text)
    return bool(m) and any(c == m.group(1) or c.startswith(m.group(1) + " (") for c in categories)


def file_entry(page: dict, name_words: set[str], place_name: str | None = None) -> dict | None:
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
    category_list = [c.get("title", "").removeprefix("Category:") for c in page.get("categories") or []]
    title_text = title.removeprefix("File:").rsplit(".", 1)[0].replace("_", " ")
    if is_species_photo(title_text, category_list) or HISTORIC_RE.search(title_text):
        return None
    distance_m = float(coords[0].get("dist", SEARCH_RADIUS_M + 1))
    kind = match_kind(name_words, title_text, distance_m, place_name)
    thumb = image_url(info.get("thumburl", "")) or image_url(info.get("url", ""))
    full = full_url(info)
    if not kind or not thumb or not full:
        return None
    entry = {
        "file": title.removeprefix("File:"),
        "width": width,
        "height": height,
        "thumb": thumb,
        "full": full,
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
        "iiextmetadatafilter": "LicenseShortName|LicenseUrl|Artist|Attribution|Restrictions",
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
    entries = [e for e in (file_entry(p, name_words, place.get("name")) for p in pages_for(body)) if e]
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
    """Place ids to check: those checked under older rules, then never checked, then the oldest checks."""
    cutoff = (today - timedelta(days=stale_days)).isoformat()
    checked = [p for p in candidates if p in matches]
    older_rules = sorted((matches[p]["checked"], p) for p in checked if matches[p].get("rules", 1) < RULES)
    unchecked = sorted(p for p in candidates if p not in matches)
    stale = sorted(
        (matches[p]["checked"], p) for p in checked if matches[p].get("rules", 1) >= RULES and matches[p]["checked"] <= cutoff
    )
    return [p for _, p in older_rules] + unchecked + [p for _, p in stale]


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
        matches[place_id] = {"place": place_id, "checked": today.isoformat(), "rules": RULES, "files": files}
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
