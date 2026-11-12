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

charge and charge:conditional
    The reference is the OSM wiki's Key:charge syntax,
    "<amount> <currency>[/<unit>][/<time unit>]": an amount with at most 2
    decimal places, a space, an ISO 4217 code (three capital letters), then
    optionally what it is for (a word, or a quantity and a word, such as
    person or 1.5 litre) and the time it covers, written out in full (minute,
    hour, day, night, week, month or year). Spaces may stand around a slash.
    charge:conditional is one or more "<charge> @ <condition>" pairs
    separated by ";" (Conditional_restrictions), a condition holding ";" in
    parentheses. The feedback SDK's parseCharge and chargeConditions read the
    same grammar, so a value the SDK accepts is never out of spec here.
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


CHARGE_TIME_UNITS = frozenset({"minute", "hour", "day", "night", "week", "month", "year"})
CHARGE_AMOUNT_RE = re.compile(r"(?:0|[1-9][0-9]{0,6})(?:\.[0-9]{1,2})?")
CHARGE_CURRENCY_RE = re.compile(r"[A-Z]{3}")
CHARGE_UNIT_RE = re.compile(r"(?:[0-9]+(?:\.[0-9]+)? )?[A-Za-z][A-Za-z0-9_]*")
MAX_TAG_LENGTH = 255


def charge_ok(text: str) -> bool:
    """True for a charge in the wiki's syntax, as the SDK's parseCharge reads it."""
    if not text or len(text) > MAX_TAG_LENGTH or text != text.strip():
        return False
    parts = re.split(r" ?/ ?", text)
    if len(parts) > 3:
        return False
    head = parts[0].split(" ")
    if len(head) != 2 or not CHARGE_AMOUNT_RE.fullmatch(head[0]) or not CHARGE_CURRENCY_RE.fullmatch(head[1]):
        return False
    rest = parts[1:]
    if any(not part for part in rest):
        return False
    if rest and rest[-1] in CHARGE_TIME_UNITS:
        rest = rest[:-1]
    if len(rest) > 1:
        return False
    return not rest or (bool(CHARGE_UNIT_RE.fullmatch(rest[0])) and rest[0] not in CHARGE_TIME_UNITS)


def _split_outside_parentheses(text: str) -> list[str] | None:
    parts, depth, start = [], 0, 0
    for i, c in enumerate(text):
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth < 0:
                return None
        elif c == ";" and depth == 0:
            parts.append(text[start:i])
            start = i + 1
    if depth != 0:
        return None
    parts.append(text[start:])
    return parts


def charge_conditional_ok(text: str) -> bool:
    """True for "<charge> @ <condition>[; ...]", as the SDK's chargeConditions reads it."""
    if not text or len(text) > MAX_TAG_LENGTH or text != text.strip():
        return False
    pairs = _split_outside_parentheses(text)
    if pairs is None:
        return False
    for pair in pairs:
        if pair.count("@") != 1:
            return False
        charge, condition = (side.strip() for side in pair.split("@"))
        wrapped = condition.startswith("(") and condition.endswith(")") and _split_outside_parentheses(condition[1:-1]) is not None
        if wrapped:
            condition = condition[1:-1].strip()
        elif "(" in condition or ")" in condition or ";" in condition:
            return False
        if not charge_ok(charge) or not condition:
            return False
    return True
