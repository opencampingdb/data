"""The monthly Overture workflow's steps around the importer.

The workflow (.github/workflows/sync-overture.yml) lists the releases in
Overture's public bucket, runs the newest one unless it was already applied,
and opens one PR. This module does the parts that need no network:

    python -m pipeline.overture_run check --listing listing.xml --root . [--release R] [--output FILE]
    python -m pipeline.overture_run applied <release> --root .
    python -m pipeline.overture_run body --report overture-report.md --summary overture-summary.json \\
        --merges merges.json --out pr-body.md [--output FILE]

check reads the bucket listing (an S3 ListObjectsV2 answer for the prefix
release/ with the delimiter /) and writes release, changelog and skip as
step outputs. applied records the release in overture/release.json, which
the PR commits. body writes the PR body (the automatic merges, then the
import report) and the auto_merge output. See pipeline/README.md "Monthly
Overture workflow".
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from .common import write_text
from .merge import AUTO_MERGES_FILE
from .overture import cell
from .sync import MAX_REPORT_CHARS

APPLIED_FILE = Path("overture") / "release.json"
RELEASE_PATTERN = re.compile(r"^(\d{4}-\d{2}-\d{2})\.(\d+)$")
# Automatic merges listed in the PR body. The rest are in the review file.
MERGE_ROWS = 100


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


def should_auto_merge(summary: dict, merges: list[dict]) -> bool:
    """Only gap fills under the limit merge themselves (the import summary's
    auto_merge). The automatic merge rule's merges wait for a person too."""
    return bool(summary["auto_merge"]) and not merges


def pr_body(report: str, summary: dict, merges: list[dict]) -> str:
    """The PR body: what happens to the PR, the automatic merges, then the import report."""
    if should_auto_merge(summary, merges):
        decision = "Only gap fills, under the limit: the PR merges itself once CI passes."
    else:
        reasons = list(summary.get("wait_reasons") or [])
        if merges:
            reasons.insert(0, f"{len(merges)} automatic merge" + ("" if len(merges) == 1 else "s"))
        decision = "The PR waits for a person: " + ", ".join(reasons) + "."
    lines = [decision, "", "## Automatic merges", ""]
    if merges:
        lines += [
            f"The automatic merge rule ran first. Each merge is also in `{AUTO_MERGES_FILE.as_posix()}`.",
            "",
            "| Kept | Dropped | Metres | Part |",
            "|---|---|---:|---|",
        ]
        for m in merges[:MERGE_ROWS]:
            lines.append(
                f"| {cell(m.get('kept_name'))} (`{m['kept']}`) | {cell(m.get('dropped_name'))} (`{m['dropped']}`) "
                f"| {m['distance_m']} | {'yes' if m.get('part') else 'no'} |"
            )
        if len(merges) > MERGE_ROWS:
            lines.append(f"And {len(merges) - MERGE_ROWS} more, listed in `{AUTO_MERGES_FILE.as_posix()}`.")
    else:
        lines.append("None.")
    body = "\n".join(lines) + "\n\n" + report
    if len(body) > MAX_REPORT_CHARS:
        cut = "\n\nThe report is cut here. The full report is in the workflow run's log.\n"
        body = body[: MAX_REPORT_CHARS - len(cut)] + cut
    return body


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
    applied = commands.add_parser("applied", help="record the release in overture/release.json")
    applied.add_argument("release")
    applied.add_argument("--root", type=Path, default=Path("."))
    body = commands.add_parser("body", help="write the PR body and the auto_merge output")
    body.add_argument("--report", type=Path, required=True)
    body.add_argument("--summary", type=Path, required=True)
    body.add_argument("--merges", type=Path, required=True, help="written by pipeline.merge --auto --summary")
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
    if args.command == "applied":
        write_applied(args.root, args.release)
        return 0
    summary = json.loads(args.summary.read_text(encoding="utf-8"))
    merges = json.loads(args.merges.read_text(encoding="utf-8"))
    write_text(args.out, pr_body(args.report.read_text(encoding="utf-8"), summary, merges))
    write_outputs(args.output, {"auto_merge": str(should_auto_merge(summary, merges)).lower()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
