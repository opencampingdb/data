"""Act on reviewed Overture candidates.

Reads candidate lines (as pipeline.overture --candidates writes them), each
with "decision" set to approve, reject or flag. A rejected candidate's GERS id
goes on overture/do-not-match.json. An approved one is published, as a place
not checked yet with its GERS id in the link file, only with
--publish-candidates and only when its tier is one named with --tier (every
tier when none is named). Other approved candidates are held and nothing is
written to places/ for them. A line that is not JSON, has an unknown
decision or cannot make a place is skipped, listed on stderr and in the
summary as needing a person, and the other lines are still acted on.
--summary writes how many candidates each outcome got, and the skipped lines
under "skipped_lines", as JSON.

    python -m pipeline.overture_decide decisions.ndjson --root . [--publish-candidates] \\
        [--tier strong] [--summary decided.json]

See pipeline/README.md "Overture candidates".
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .common import write_text
from .overture_candidates import TIERS, apply_decisions


def read_decision_lines(path: Path):
    """The decisions file's lines, one at a time, skipping blank lines.

    A line that is JSON comes back parsed. One that is not comes back as its
    text, so apply_decisions skips it and lists it for a person.
    """
    with open(path, encoding="utf-8") as f:
        for text in f:
            if text.strip():
                try:
                    yield json.loads(text)
                except json.JSONDecodeError:
                    yield text.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("decisions", type=Path, help="candidate lines, each with a decision")
    parser.add_argument("--root", type=Path, default=Path("."), help="data repository root")
    parser.add_argument(
        "--publish-candidates", action="store_true",
        help="publish approved candidates (off by default: they are held)",
    )
    parser.add_argument(
        "--tier", action="append", choices=TIERS, dest="tiers",
        help="publish approved candidates of this tier only (repeat for more, default every tier)",
    )
    parser.add_argument("--summary", type=Path, help="also write the count of each outcome here, as JSON")
    parser.add_argument("--now", help="the run time, UTC (default: today at 00:00:00Z)")
    args = parser.parse_args(argv)

    now = args.now or time.strftime("%Y-%m-%dT00:00:00Z", time.gmtime())
    outcome = apply_decisions(
        args.root, read_decision_lines(args.decisions), now=now, publish=args.publish_candidates, tiers=args.tiers
    )
    counts = {key: len(ids) for key, ids in outcome.items()}
    if args.summary:
        write_text(args.summary, json.dumps(dict(counts, skipped_lines=outcome["skipped"]), sort_keys=True) + "\n")
    print(json.dumps(counts, sort_keys=True), file=sys.stderr)
    for skipped in outcome["skipped"]:
        print(f"Decision line {skipped['line']} ({skipped['gers'] or 'no GERS id'}) needs a person: "
              f"{skipped['problem']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
