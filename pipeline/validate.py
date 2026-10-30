"""Check every place and review file in the repository.

Usage:
    python -m pipeline.validate --root .

Exits 0 when everything is valid. Otherwise prints one line per problem and
exits 1. Checks:
- every place file against schema/place.schema.json, every review line
  against schema/review.schema.json (no key outside the schema is allowed)
- place files are in canonical form (sorted keys, 2-space indent, trailing
  newline) and sit at places/<last 2 characters of id, lower case>/<id>.json
- kind is one of schema/kinds.json and group is that kind's group
- coordinates have at most 6 decimal places
- fact_sources has every key of facts and no other fact, plus optional
  name, location and status entries
- no two records share an id or an OSM object (merged_osm included,
  tombstones left out)
- a tombstone (status merged) has merged_into, naming a record that is not
  itself merged, and has no reviews. No other record has merged_into
- review lines are compact JSON with sorted keys, sorted by id
- reviews belong to a place that exists, have unique ids, and the place's
  rating has the right count and the mean rounded to 1 decimal place, half up
- review text has no email addresses, phone numbers, links, social media
  handles or obvious personal names ("my name is", sign-offs like "Cheers, Sam")
"""

import argparse
import json
import re
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

from .common import (
    EXTRA_SOURCE_KEYS,
    KIND_GROUP,
    dump_line,
    dump_record,
    iter_place_files,
    iter_review_files,
    place_path,
    rating_mean,
)

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schema"

# ISO dates (2026-10-30 or 2026-10, with an optional time after a T). They are
# blanked out before the checks below, so that a date, or several dates in a
# row, is never read as a phone number. The feedback engine's moderation
# applies the same rule, so the two agree.
ISO_DATE = re.compile(r"(?<!\d)\d{4}-(?:0[1-9]|1[0-2])(?:-(?:0[1-9]|[12]\d|3[01]))?(?!\d)")

# Personal details that must never appear in review text. Each pattern is
# paired with the reason shown in the error.
TEXT_RULES = [
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "an email address"),
    # Eight or more digits with only spaces, dots, dashes or brackets between them.
    (re.compile(r"(?<![\w])\+?\d(?:[\s().-]{0,2}\d){7,}"), "a phone number"),
    (
        re.compile(r"(?i)https?://|\bwww\.|\b[a-z0-9-]+\.(?:com|net|org|au|io|me)\b"),
        "a link",
    ),
    (re.compile(r"(?<![\w.])@[A-Za-z0-9_]{2,}"), "a social media handle"),
    (re.compile(r"(?i)\bmy name(?:'s| is)\b"), "a personal name"),
    (re.compile(r"(?i)\b(?:call|text|ring|contact|email|message|dm) me\b"), "a request for contact"),
    # A sign-off with a capitalised first name at the very end: "Cheers, Sam".
    (
        re.compile(r"(?:^|[.!?\n]\s*)(?i:cheers|regards|thanks|thank you|love|from)[,!\s-]+[A-Z][a-z]+[.!]?\s*$"),
        "a personal name",
    ),
]


def load_validator(name: str) -> Draft202012Validator:
    schema = json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def schema_errors(validator: Draft202012Validator, obj, where: str) -> list[str]:
    errors = []
    for error in sorted(validator.iter_errors(obj), key=lambda e: list(e.absolute_path)):
        path = "/".join(str(p) for p in error.absolute_path) or "(top level)"
        errors.append(f"{where}: {path}: {error.message}")
    return errors


def text_problems(text: str) -> list[str]:
    """Reasons a review text must be refused, empty when it is fine."""
    without_dates = ISO_DATE.sub(" ", text)
    return [reason for pattern, reason in TEXT_RULES if pattern.search(without_dates)]


def has_at_most_places(value: float, places: int = 6) -> bool:
    return round(value, places) == value


def check_place_file(root: Path, path: Path, validator) -> tuple[dict | None, list[str]]:
    """Validate one place file. Returns the record (if it parsed) and the errors."""
    where = str(path.relative_to(root)).replace("\\", "/")
    try:
        text = path.read_bytes().decode("utf-8")
        record = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, [f"{where}: not valid UTF-8 JSON: {exc}"]

    errors = schema_errors(validator, record, where)
    if not isinstance(record, dict) or errors:
        return (record if isinstance(record, dict) else None), errors

    if text != dump_record(record):
        errors.append(
            f"{where}: not in canonical form (sorted keys, 2-space indent, "
            "UTF-8 characters as they are, LF line endings, one trailing newline)"
        )

    expected = place_path(root, record["id"])
    if path.resolve() != expected.resolve():
        errors.append(f"{where}: should be at {expected.relative_to(root).as_posix()}")

    if record["group"] != KIND_GROUP[record["kind"]]:
        errors.append(
            f"{where}: group {record['group']} does not match kind {record['kind']}, "
            f"which is in group {KIND_GROUP[record['kind']]}"
        )

    for axis in ("lat", "lon"):
        if not has_at_most_places(record["location"][axis]):
            errors.append(f"{where}: location/{axis} has more than 6 decimal places")

    source_keys = set(record["fact_sources"]) - set(EXTRA_SOURCE_KEYS)
    if set(record["facts"]) != source_keys:
        differ = sorted(set(record["facts"]) ^ source_keys)
        errors.append(
            f"{where}: fact_sources must have an entry for every fact and no other fact, "
            f"differ on {differ}"
        )

    return record, errors


def check_places(root: Path) -> tuple[dict[str, dict], list[str]]:
    validator = load_validator("place.schema.json")
    errors: list[str] = []
    places: dict[str, dict] = {}
    osm_owner: dict[tuple, str] = {}

    for path in sorted((root / "places").rglob("*")):
        if path.is_file() and path.suffix != ".json" and path.name != ".gitkeep":
            errors.append(f"{path.relative_to(root).as_posix()}: unexpected file in places/")

    for path in iter_place_files(root):
        record, file_errors = check_place_file(root, path, validator)
        errors += file_errors
        if record is None or "id" not in record:
            continue
        if record["id"] in places:
            errors.append(f"{path.relative_to(root).as_posix()}: id {record['id']} used twice")
        places[record["id"]] = record
        if record.get("status") == "merged":
            continue  # a tombstone's OSM links now belong to the record it was merged into
        osm = record.get("osm")
        keys = []
        if isinstance(osm, dict) and "type" in osm and "id" in osm:
            keys.append((osm["type"], osm["id"]))
        keys += [(m["type"], m["id"]) for m in record.get("merged_osm", []) if isinstance(m, dict)]
        for key in keys:
            if key in osm_owner:
                errors.append(
                    f"{record['id']}: same OSM object {key[0]}/{key[1]} as {osm_owner[key]}"
                )
            else:
                osm_owner[key] = record["id"]

    # Tombstones: status "merged" and merged_into go together, and
    # merged_into names a record that is not itself merged.
    for place_id, record in places.items():
        merged = record.get("status") == "merged"
        target = record.get("merged_into")
        if merged and target is None:
            errors.append(f"{place_id}: status is merged but merged_into is missing")
        elif not merged and target is not None:
            errors.append(f"{place_id}: merged_into is set but status is not merged")
        elif merged and target not in places:
            errors.append(f"{place_id}: merged_into {target} is not a place")
        elif merged and places[target].get("status") == "merged":
            errors.append(f"{place_id}: merged_into {target} is itself merged; point at the place that is left")
    return places, errors


def check_reviews(root: Path, places: dict[str, dict]) -> list[str]:
    validator = load_validator("review.schema.json")
    errors: list[str] = []
    seen_ids: set[str] = set()
    stars_by_place: dict[str, list[int]] = {}

    for path in sorted((root / "reviews").rglob("*")):
        if path.is_file() and path.suffix != ".ndjson" and path.name != ".gitkeep":
            errors.append(f"{path.relative_to(root).as_posix()}: unexpected file in reviews/")

    for path in iter_review_files(root):
        where = f"reviews/{path.name}"
        place_id = path.stem
        if place_id not in places:
            errors.append(f"{where}: no such place {place_id}")
        raw = path.read_bytes()
        if b"\r" in raw:
            errors.append(f"{where}: use LF line endings")
        if raw and not raw.endswith(b"\n"):
            errors.append(f"{where}: missing trailing newline")
        previous_id = None
        for number, line in enumerate(raw.decode("utf-8", errors="replace").split("\n"), 1):
            if not line.strip():
                continue
            here = f"{where} line {number}"
            try:
                review = json.loads(line)
            except json.JSONDecodeError as exc:
                errors.append(f"{here}: not valid JSON: {exc}")
                continue
            line_errors = schema_errors(validator, review, here)
            errors += line_errors
            if line_errors or not isinstance(review, dict):
                continue
            if line != dump_line(review):
                errors.append(f"{here}: not in canonical form (compact JSON, sorted keys)")
            if previous_id is not None and review["id"] <= previous_id:
                errors.append(f"{here}: lines must be sorted by id")
            previous_id = review["id"]
            if review["place"] != place_id:
                errors.append(f"{here}: place {review['place']} does not match file name")
            if review["id"] in seen_ids:
                errors.append(f"{here}: review id {review['id']} used twice")
            seen_ids.add(review["id"])
            for reason in text_problems(review["text"]):
                errors.append(f"{here}: text contains {reason}")
            stars_by_place.setdefault(place_id, []).append(review["stars"])

    # The rating on each place must agree with its reviews. A tombstone keeps
    # the rating it had, but its reviews have moved, so it may have none.
    for place_id, record in places.items():
        stars = stars_by_place.get(place_id, [])
        rating = record.get("rating")
        if record.get("status") == "merged":
            if stars:
                errors.append(f"{place_id}: is merged, its reviews belong to {record.get('merged_into')}")
            continue
        if not stars:
            if rating is not None:
                errors.append(f"{place_id}: rating is set but the place has no reviews")
            continue
        if rating is None:
            errors.append(f"{place_id}: has {len(stars)} reviews but rating is null")
            continue
        expected = {"count": len(stars), "mean": rating_mean(stars)}
        if rating != expected:
            errors.append(
                f"{place_id}: rating {rating} does not match its reviews, expected {expected}"
            )
    return errors


def validate_repo(root: Path) -> list[str]:
    """Every problem found in the repository, as readable lines. Empty means valid."""
    root = Path(root)
    places, errors = check_places(root)
    errors += check_reviews(root, places)
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path("."), help="data repository root")
    args = parser.parse_args(argv)

    errors = validate_repo(args.root)
    for error in errors:
        print(error)
    if errors:
        print(f"{len(errors)} problem(s) found", file=sys.stderr)
        return 1
    print("All place and review files are valid", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
