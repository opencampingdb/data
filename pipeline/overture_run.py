"""The monthly Overture workflow's steps around the importer.

The workflow (.github/workflows/sync-overture.yml) lists the releases in
Overture's public bucket, runs the newest one unless it was already applied,
and opens one PR. This module does the parts that need no network:

    python -m pipeline.overture_run check --listing listing.xml --root . [--release R] [--output FILE]
    python -m pipeline.overture_run carry --decisions FILE --do-not-match FILE --root .
    python -m pipeline.overture_run applied <release> --root .
    python -m pipeline.overture_run body --report overture-report.md --summary overture-summary.json \\
        [--decided decided.json] --out pr-body.md [--output FILE]

check reads the bucket listing (an S3 ListObjectsV2 answer for the prefix
release/ with the delimiter /) and writes release, changelog and skip as
step outputs. carry brings the reviewer's decisions and rejections from the
last overture-sync branch into the rebuilt one. applied records the release
in overture/release.json, which the PR commits. body writes the PR body
(whether the PR merges itself, any decision lines skipped for a person,
then the import report) and the auto_merge
output. See pipeline/README.md "Monthly Overture workflow".
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from .common import write_text
from .sync import MAX_REPORT_CHARS

APPLIED_FILE = Path("overture") / "release.json"
DECISIONS_FILE = Path("overture") / "decisions.ndjson"
DO_NOT_MATCH_FILE = Path("overture") / "do-not-match.json"
RELEASE_PATTERN = re.compile(r"^(\d{4}-\d{2}-\d{2})\.(\d+)$")
# The PR body's size budget. The body is one line saying whether the PR merges
# itself, then the import report, which pipeline.overture cuts to
# REPORT_MAX_CHARS. The whole body stays within MAX_REPORT_CHARS.
REPORT_MAX_CHARS = MAX_REPORT_CHARS - 1_000


def release_key(release: str) -> tuple[str, int]:
    date, patch = RELEASE_PATTERN.match(release).groups()
    return date, int(patch)


def releases_in_listing(listing: str) -> list[str]:
    """Every release named in the bucket listing, oldest first."""
    root = ET.fromstring(listing)
    found = set()
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] != "Prefix" or not element.text:
            continue
        parts = element.text.strip("/").split("/")
        if len(parts) == 2 and parts[0] == "release" and RELEASE_PATTERN.match(parts[1]):
            found.add(parts[1])
    return sorted(found, key=release_key)


def plan_run(listing: str, applied: str | None, wanted: str | None = None) -> dict:
    """What this run does: the release, whether a changelog run is enough, and why it skips (or None).

    The release is the newest in the listing, or the one asked for by hand,
    which must be listed. A release already applied is skipped, unless it
    was asked for by hand. A changelog run needs the applied release to be
    the one just before: the changelog holds only the changes since then.
    """
    releases = releases_in_listing(listing)
    if not releases:
        raise ValueError("the bucket listing names no Overture release")
    if wanted and wanted not in releases:
        raise ValueError(f"Overture release {wanted!r} is not in the bucket")
    release = wanted or releases[-1]
    index = releases.index(release)
    changelog = applied is not None and index > 0 and releases[index - 1] == applied
    skip = None
    if release == applied and not wanted:
        skip = f"Overture {release} was already applied"
    return {"release": release, "changelog": changelog, "skip": skip}


def applied_release(root: Path) -> str | None:
    """The Overture release the data on this branch was last brought up to, or None."""
    path = Path(root) / APPLIED_FILE
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))["release"]


def write_applied(root: Path, release: str) -> None:
    if not RELEASE_PATTERN.match(release):
        raise ValueError(f"not an Overture release: {release!r}")
    write_text(Path(root) / APPLIED_FILE, json.dumps({"release": release}) + "\n")


def should_auto_merge(summary: dict, published: int = 0, skipped: list | None = None) -> bool:
    """Only gap fills under the limit merge themselves (the import summary's
    auto_merge). A candidate published in this run, or a decision line
    pipeline.overture_decide skipped, waits for a person too."""
    return bool(summary["auto_merge"]) and not published and not skipped


def skipped_section(skipped: list) -> str:
    """The PR body's table of decision lines pipeline.overture_decide skipped."""
    from .overture_report import md_cell  # overture_report imports this module

    rows = [
        f"| {s['line']} | {md_cell(s.get('gers') or '')} | {md_cell(s['problem'])} |" for s in skipped
    ]
    return (
        "## Decision lines that need a person\n\n"
        "pipeline.overture_decide skipped these lines of overture/decisions.ndjson and did nothing for them.\n\n"
        "| Line | GERS id | Problem |\n|---|---|---|\n" + "\n".join(rows) + "\n\n"
    )


def pr_body(report: str, summary: dict, published: int = 0, skipped: list | None = None) -> str:
    """The PR body: whether the PR merges itself and why not, any skipped
    decision lines, then the import report."""
    skipped = list(skipped or [])
    if should_auto_merge(summary, published, skipped):
        decision = "Only gap fills, under the limit: the PR merges itself once CI passes."
    else:
        reasons = list(summary.get("wait_reasons") or [])
        if published:
            reasons.append(f"{published} candidate" + ("" if published == 1 else "s") + " published in this run")
        if skipped:
            reasons.append(f"{len(skipped)} decision line" + ("" if len(skipped) == 1 else "s") + " skipped")
        decision = "The PR waits for a person: " + ", ".join(reasons) + "."
    body = decision + "\n\n" + (skipped_section(skipped) if skipped else "") + report
    if len(body) > MAX_REPORT_CHARS:
        cut = "\n\nThe report is cut here. The full report is in the workflow run's log.\n"
        body = body[: MAX_REPORT_CHARS - len(cut)] + cut
    return body


def carry(root: Path, decisions: Path, do_not_match: Path) -> None:
    """Bring the reviewer's work from the last overture-sync branch into the rebuilt one.

    decisions and do_not_match are that branch's overture/decisions.ndjson and
    overture/do-not-match.json, either of which may be missing. Its decisions
    file replaces this one. Its do-not-match list is joined to this one, so a
    pair or GERS id added on main since is kept too.
    """
    root = Path(root)
    if decisions.exists():
        write_text(root / DECISIONS_FILE, decisions.read_text(encoding="utf-8"))
    if not do_not_match.exists():
        return
    theirs = json.loads(do_not_match.read_text(encoding="utf-8"))
    path = root / DO_NOT_MATCH_FILE
    ours = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"pairs": []}
    pairs = list(ours.get("pairs", []))
    pairs += [pair for pair in theirs.get("pairs", []) if pair not in pairs]
    joined = dict(ours, pairs=pairs, gers=sorted(set(ours.get("gers", [])) | set(theirs.get("gers", []))))
    if joined != ours:
        write_text(path, json.dumps(joined, indent=2) + "\n")


def write_outputs(path: Path | None, values: dict) -> None:
    """Append key=value lines to a GitHub Actions output file, or print them."""
    text = "".join(f"{key}={value}\n" for key, value in values.items())
    if path is None:
        sys.stdout.write(text)
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="choose the release to run")
    check.add_argument("--listing", type=Path, required=True, help="the bucket listing for release/, as XML")
    check.add_argument("--root", type=Path, default=Path("."))
    check.add_argument("--release", default="", help="run this release instead of the newest")
    check.add_argument("--output", type=Path, help="GitHub Actions output file (default: print)")
    carried = commands.add_parser("carry", help="carry the reviewer's files over from the last overture-sync branch")
    carried.add_argument("--decisions", type=Path, required=True, help="that branch's overture/decisions.ndjson")
    carried.add_argument("--do-not-match", type=Path, required=True, help="that branch's overture/do-not-match.json")
    carried.add_argument("--root", type=Path, default=Path("."))
    applied = commands.add_parser("applied", help="record the release in overture/release.json")
    applied.add_argument("release")
    applied.add_argument("--root", type=Path, default=Path("."))
    body = commands.add_parser("body", help="write the PR body and the auto_merge output")
    body.add_argument("--report", type=Path, required=True)
    body.add_argument("--summary", type=Path, required=True)
    body.add_argument("--decided", type=Path, help="written by pipeline.overture_decide --summary")
    body.add_argument("--out", type=Path, required=True)
    body.add_argument("--output", type=Path, help="GitHub Actions output file (default: print)")
    args = parser.parse_args(argv)

    if args.command == "check":
        try:
            plan = plan_run(args.listing.read_text(encoding="utf-8"), applied_release(args.root), args.release or None)
        except ValueError as exc:
            print(f"::error::{exc}", file=sys.stderr)
            return 1
        if plan["skip"]:
            print(plan["skip"], file=sys.stderr)
        write_outputs(args.output, {
            "release": plan["release"],
            "changelog": str(plan["changelog"]).lower(),
            "skip": str(plan["skip"] is not None).lower(),
        })
        return 0
    if args.command == "carry":
        carry(args.root, args.decisions, args.do_not_match)
        return 0
    if args.command == "applied":
        write_applied(args.root, args.release)
        return 0
    summary = json.loads(args.summary.read_text(encoding="utf-8"))
    decided = json.loads(args.decided.read_text(encoding="utf-8")) if args.decided else {}
    published, skipped = decided.get("published", 0), decided.get("skipped_lines", [])
    write_text(args.out, pr_body(args.report.read_text(encoding="utf-8"), summary, published, skipped))
    write_outputs(args.output, {"auto_merge": str(should_auto_merge(summary, published, skipped)).lower()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
