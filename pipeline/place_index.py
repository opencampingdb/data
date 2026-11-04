"""Load the place index that pipeline.release writes into a feedback intake.

Usage:
    INTAKE_PLACES_TOKEN=... python -m pipeline.place_index --file dist/place-index.ndjson.gz --url https://intake.example

It reads the token from the INTAKE_PLACES_TOKEN environment variable. It
asks the intake for the hash of every cell it holds (GET /v1/place-index/cells)
and sends only the cells whose hash differs (PUT /v1/place-index), a few cells
per call, so a release that changes a few places writes only their cells.
"""

import argparse
import gzip
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

from .common import dump_line
from .release import PLACE_INDEX_FILE, gzip_bytes

# Uncompressed bytes of place lines in one call, so each call parses within a
# free plan Worker's CPU time. The intake takes up to 4 MiB, and a cell bigger
# than PART_BYTES goes in a call of its own.
PART_BYTES = 500_000
MAX_CALL_BYTES = 4 * 1024 * 1024
MAX_CALL_CELLS = 2000
TOKEN_ENV = "INTAKE_PLACES_TOKEN"

Cell = tuple[int, int]
# (method, url, headers, body) -> (status, response body)
Send = Callable[[str, str, dict[str, str], bytes | None], tuple[int, bytes]]


def lines_by_cell(text: str) -> dict[Cell, list[str]]:
    """The index lines grouped by cell, each with its newline, in file order."""
    cells: dict[Cell, list[str]] = {}
    for line in text.splitlines():
        if line.strip():
            lon, lat = json.loads(line)["cell"]
            cells.setdefault((lon, lat), []).append(line + "\n")
    return cells


def cell_hash(lines: list[str]) -> str:
    return hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()


def changed_parts(cells: dict[Cell, list[str]], held: dict[Cell, str]) -> list[bytes]:
    """The call bodies that bring an intake holding `held` (cell to hash) up to `cells`.

    Each body is NDJSON: a line naming the cells it replaces, then their places.
    A cell the intake holds that no longer has a place is sent with a null hash.
    """
    changed = []
    for cell in sorted(set(cells) | set(held)):
        lines = cells.get(cell, [])
        local = cell_hash(lines) if lines else None
        if local != held.get(cell):
            changed.append((cell, local, lines))

    parts, group, size = [], [], 0
    for item in changed:
        cell_bytes = sum(len(line.encode("utf-8")) for line in item[2])
        if cell_bytes > MAX_CALL_BYTES - 64 * 1024:
            raise ValueError(f"cell {item[0]} holds {cell_bytes} bytes, more than one intake call takes")
        # A cell with no places left adds nothing, so it joins whatever call is open.
        if group and ((cell_bytes and size + cell_bytes > PART_BYTES) or len(group) == MAX_CALL_CELLS):
            parts.append(group)
            group, size = [], 0
        group.append(item)
        size += cell_bytes

    if group:
        parts.append(group)
    bodies = []
    for group in parts:
        head = {"cells": [{"cell": list(cell), "hash": local} for cell, local, _ in group]}
        text = dump_line(head) + "\n" + "".join(line for _, _, lines in group for line in lines)
        bodies.append(text.encode("utf-8"))
    return bodies


def urllib_send(method: str, url: str, headers: dict[str, str], body: bytes | None) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def upload(path: Path, url: str, token: str, send: Send = urllib_send, log=print) -> int:
    """Send the cells of the index file at `path` that the intake at `url` lacks. Returns the cells sent."""
    base = url.rstrip("/")
    auth = {"authorization": f"Bearer {token}", "user-agent": "opencampingdb-release"}
    status, body = send("GET", f"{base}/v1/place-index/cells", auth, None)
    if status != 200:
        raise RuntimeError(f"GET /v1/place-index/cells answered {status}: {body[:500]!r}")
    held = {(c["cell"][0], c["cell"][1]): c["hash"] for c in json.loads(body)["cells"]}

    cells = lines_by_cell(gzip.decompress(Path(path).read_bytes()).decode("utf-8"))
    parts = changed_parts(cells, held)
    log(f"The intake holds {len(held)} cells. {len(cells)} cells hold places; sending {len(parts)} calls.")
    sent = 0
    for number, part in enumerate(parts, 1):
        headers = {**auth, "content-type": "application/x-ndjson"}
        # Gzipped as a whole file, not with content-encoding, so the intake gunzips it itself.
        status, answer = send("PUT", f"{base}/v1/place-index", headers, gzip_bytes(part))
        if status != 200:
            raise RuntimeError(f"call {number} of {len(parts)}: PUT /v1/place-index answered {status}: {answer[:500]!r}")
        sent += json.loads(answer)["cells"]
    log(f"Sent {sent} cells.")
    return sent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--file", type=Path, default=Path("dist") / PLACE_INDEX_FILE)
    parser.add_argument("--url", required=True, help="the intake's base URL")
    args = parser.parse_args(argv)

    token = os.environ.get(TOKEN_ENV, "")
    if not token:
        print(f"{TOKEN_ENV} is not set.", file=sys.stderr)
        return 2
    try:
        upload(args.file, args.url, token)
    except (RuntimeError, ValueError, OSError) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
