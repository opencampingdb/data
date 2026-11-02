"""Hide or show a review, or block or unblock a contributor. Run by a person.

Usage:
    python -m pipeline.moderate hide <review id> --root .
    python -m pipeline.moderate show <review id> --root .
    python -m pipeline.moderate block <contributor> --reason <reason> --root .
    python -m pipeline.moderate unblock <contributor> --root .

hide sets hidden "moderator" on the review line, show removes hidden (also
a "reported" hide from three reports). block adds the contributor to
moderation/blocked_contributors.json with today's date; reason is one of
spam_ad, offensive, off_topic, fake, personal_details or abuse. Hidden
reviews and reviews from blocked contributors stay in reviews/ but are left
out of the release and the rating. Each command recalculates the rating of
every place it touches and sets that place's updated, so validation passes.
"""

import argparse
import json
import sys
from pathlib import Path

from .common import (
    blocked_path,
    dump_line,
    dump_record,
    is_visible,
    iter_review_files,
    load_blocked,
    place_path,
    rating_mean,
    utc_now,
    write_text,
)

REASONS = ("spam_ad", "offensive", "off_topic", "fake", "personal_details", "abuse")


class ModerateError(Exception):
    """The command was refused. Nothing was written."""


def read_lines(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def rerate(root: Path, place_ids: set[str], now: str) -> None:
    """Recalculate the rating of each place from its visible reviews."""
    blocked = load_blocked(root)
    for place_id in sorted(place_ids):
        path = place_path(root, place_id)
        record = json.loads(path.read_text(encoding="utf-8"))
        review_file = Path(root) / "reviews" / f"{place_id}.ndjson"
        lines = read_lines(review_file) if review_file.exists() else []
        stars = [r["stars"] for r in lines if is_visible(r, blocked)]
        rating = {"count": len(stars), "mean": rating_mean(stars)} if stars else None
        if record.get("rating") != rating:
            record["rating"] = rating
            record["updated"] = now
            write_text(path, dump_record(record))


def set_hidden(root: Path, review_id: str, hidden: bool, now: str | None = None) -> str:
    """Hide or show one review. Returns its place id."""
    for path in iter_review_files(root):
        lines = read_lines(path)
        for line in lines:
            if line["id"] != review_id:
                continue
            if hidden:
                line["hidden"] = "moderator"
            else:
                line.pop("hidden", None)
            write_text(path, "".join(dump_line(r) + "\n" for r in lines))
            rerate(root, {path.stem}, now or utc_now())
            return path.stem
    raise ModerateError(f"no review {review_id}")


def set_blocked(root: Path, contributor: str, reason: str | None, now: str | None = None) -> set[str]:
    """Block (reason given) or unblock (reason None) a contributor. Returns the places re-rated."""
    now = now or utc_now()
    path = blocked_path(root)
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"contributors": []}
    entries = [e for e in data["contributors"] if e["contributor"] != contributor]
    if reason is not None:
        if reason not in REASONS:
            raise ModerateError(f"reason must be one of {', '.join(REASONS)}")
        if len(entries) != len(data["contributors"]):
            raise ModerateError(f"{contributor} is already blocked")
        entries.append({"contributor": contributor, "added": now[:10], "reason": reason})
    elif len(entries) == len(data["contributors"]):
        raise ModerateError(f"{contributor} is not blocked")
    write_text(path, dump_record({"contributors": sorted(entries, key=lambda e: e["contributor"])}))
    touched = {p.stem for p in iter_review_files(root) if any(r.get("contributor") == contributor for r in read_lines(p))}
    rerate(root, touched, now)
    return touched


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["hide", "show", "block", "unblock"])
    parser.add_argument("target", help="review id (hide, show) or contributor (block, unblock)")
    parser.add_argument("--reason", choices=REASONS, help="why the contributor is blocked")
    parser.add_argument("--root", type=Path, default=Path("."), help="data repository root")
    args = parser.parse_args(argv)
    try:
        if args.command in ("hide", "show"):
            place_id = set_hidden(args.root, args.target, args.command == "hide")
            print(f"{args.command}: {args.target} on {place_id}", file=sys.stderr)
        else:
            if args.command == "block" and not args.reason:
                raise ModerateError("block needs --reason")
            touched = set_blocked(args.root, args.target, args.reason if args.command == "block" else None)
            print(f"{args.command}: {args.target}, {len(touched)} place(s) re-rated", file=sys.stderr)
    except ModerateError as exc:
        print(f"Not changed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
