"""Check OSM values against the reference OpenStreetMap names for each tag (docs/adr/0002).

opening_hours
    The reference is opening_hours.js. tools/oh-check/check.mjs runs it
    through node, once per run for every value: a JSON array of values in on
    stdin, a JSON array of results out on stdout. Values are read as
    Australian. Style warnings are not out of spec, and neither is a value
    that fails only because the library has no Australian school holiday
    data. Install it with `npm ci --ignore-scripts` in tools/oh-check.

phone
    The reference is the E.164 international form the OSM wiki's Key:phone
    page asks for: "+", the country code, then the number, with only digits,
    spaces and hyphens. Several numbers are separated by ";". An extension
    may follow in a form the page lists (see E164_RE). Domestic-only keys
    such as phone:AU are not read by the extract, so never checked.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

OH_CHECK_DIR = Path(__file__).resolve().parent.parent / "tools" / "oh-check"
OH_CHECK_SCRIPT = OH_CHECK_DIR / "check.mjs"
# The node binary to run. Tests point it at a missing one.
NODE_ENV = "OH_CHECK_NODE"

NO_NODE = (
    "node or opening_hours.js is not installed (run npm ci --ignore-scripts in tools/oh-check), "
    "so the file was left as it was"
)

# Key:phone, "Extensions": "E.123 format: +1-978-750-1900 ext. 4309", and the informal
# "x3369" with or without a space before it. The DIN 5008 form ("-1234") is
# already digits and hyphens. The ";ext=" form is not accepted: the page says no
# software reads it, and ";" separates numbers.
E164_RE = re.compile(r"\+[1-9][0-9 -]*[0-9](?:(?: ?x| ext\. )[0-9]+)?")


def checker_ready() -> bool:
    """True when node and the opening_hours.js package are both there."""
    node = shutil.which(os.environ.get(NODE_ENV, "node"))
    return bool(node) and (OH_CHECK_DIR / "node_modules" / "opening_hours").is_dir()


def check_opening_hours(values: list[str]) -> dict[str, str | None]:
    """Value -> the reference's error when it is out of spec, else None. One node run for all values.

    Raises RuntimeError when the checker is not installed or fails.
    """
    unique = sorted(set(values))
    if not unique:
        return {}
    node = shutil.which(os.environ.get(NODE_ENV, "node"))
    if not checker_ready() or not node:
        raise RuntimeError(NO_NODE)
    run = subprocess.run(
        [node, str(OH_CHECK_SCRIPT)],
        input=json.dumps(unique), capture_output=True, text=True, encoding="utf-8", cwd=OH_CHECK_DIR, timeout=600,
    )
    if run.returncode != 0:
        raise RuntimeError(f"tools/oh-check failed: {run.stderr.strip()[:500]}")
    results = json.loads(run.stdout)
    if len(results) != len(unique):
        raise RuntimeError("tools/oh-check returned a different number of results")
    return {value: (result["error"] or "the reference cannot read it") if result["out_of_spec"] else None
            for value, result in zip(unique, results)}


def phone_out_of_spec(number: str) -> bool:
    """True for a number not in the international form: "+", country code, then digits, spaces and hyphens.

    An extension the OSM wiki lists (" ext. 12", " x12" or "x12") may follow.
    """
    return not E164_RE.fullmatch(number.strip())
