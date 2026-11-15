"""Act on reviewed Overture candidates.

Reads candidate lines (as pipeline.overture --candidates writes them), each
with "decision" set to approve, reject or flag. A rejected candidate's GERS id
goes on overture/do-not-match.json. An approved one is published, as a place
not checked yet with its GERS id in the link file, only with
--publish-candidates. Without it, approved candidates are held and nothing is
written to places/.

    python -m pipeline.overture_decide decisions.ndjson --root . [--publish-candidates]

See pipeline/README.md "Overture candidates".
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .overture import apply_decisions, read_extract


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("decisions", type=Path, help="candidate lines, each with a decision")
    parser.add_argument("--root", type=Path, default=Path("."), help="data repository root")
    parser.add_argument(
        "--publish-candidates", action="store_true",
        help="publish approved candidates (off by default: they are held)",
    )
    parser.add_argument("--now", help="the run time, UTC (default: today at 00:00:00Z)")
    args = parser.parse_args(argv)

    now = args.now or time.strftime("%Y-%m-%dT00:00:00Z", time.gmtime())
    outcome = apply_decisions(args.root, read_extract(args.decisions), now=now, publish=args.publish_candidates)
    print(json.dumps({key: len(ids) for key, ids in outcome.items()}, sort_keys=True), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
