"""Tests for the out of spec challenge file, through the challenge builder (pipeline.maproulette)."""

import json
import os
import shutil

import pytest

from pipeline.maproulette import OUT_OF_SPEC, OH_CHECK_DIR, render_report, write_challenges
from pipeline.maproulette import RS
from pipeline.tests.test_maproulette import A, B, NOW, osm_place, record, tasks, write

# The out of spec file is written only when opening_hours.js can run through
# node. Without node or `npm ci` in tools/oh-check, these tests skip locally. CI sets REQUIRE_NODE=1,
# which makes that a failure instead.
CHECKER_READY = bool(shutil.which("node")) and (OH_CHECK_DIR / "node_modules" / "opening_hours").is_dir()
if not CHECKER_READY and os.environ.get("REQUIRE_NODE") == "1":
    raise RuntimeError("REQUIRE_NODE=1 but node or tools/oh-check/node_modules is missing: run npm ci --ignore-scripts there")
needs_node = pytest.mark.skipif(
    not CHECKER_READY, reason="needs node and `npm ci --ignore-scripts` in tools/oh-check (opening_hours.js)"
)


def osm_record(place_id, osm_id, **facts):
    return record(place_id, osm_id, facts=facts, fact_sources={field: "osm" for field in facts})


def build(repo, *pairs):
    """Write each (record, OSM place) pair and run the challenge builder. Returns its result."""
    write(repo, *(rec for rec, _ in pairs))
    return write_challenges(repo, [place for _, place in pairs], now=NOW)


@needs_node
def test_opening_hours_the_reference_refuses_make_a_task(repo):
    value = "Mo-Fr 25:00-26:00"
    build(repo, (osm_record(A, 1, opening_hours=value), osm_place(1, opening_hours=value)))
    (task,) = tasks(repo, OUT_OF_SPEC)
    (feature,) = task["features"]
    properties = feature["properties"]
    assert properties["id"] == f"{A}:opening_hours"
    assert properties["@id"] == "node/1"
    assert properties["ocdb_id"] == A
    assert properties["tag"] == "opening_hours"
    assert properties["value"] == value
    assert feature["geometry"] == {"type": "Point", "coordinates": [145.0, -35.0]}
    check = properties["check"]
    assert "opening_hours=Mo-Fr 25:00-26:00" in check
    assert "opening_hours.js" in check
    assert "https://wiki.openstreetmap.org/wiki/Key:opening_hours" in check
    assert "skip the task" in check


@needs_node
def test_opening_hours_with_only_a_style_warning_make_no_task(repo):
    # The trailing ';' draws a warning from opening_hours.js, but it reads the value.
    value = "Mo-Fr 08:00-17:00;"
    build(repo, (osm_record(A, 1, opening_hours=value), osm_place(1, opening_hours=value)))
    assert tasks(repo, OUT_OF_SPEC) == []


@needs_node
def test_school_holidays_the_reference_has_no_australian_data_for_make_no_task(repo):
    for place_id, osm_id, value in ((A, 1, "Mo-Su 08:00-17:00; SH off"), (B, 2, "Mo-Fr 09:00-17:00; PH off")):
        write(repo, osm_record(place_id, osm_id, opening_hours=value))
    write_challenges(repo, [
        osm_place(1, opening_hours="Mo-Su 08:00-17:00; SH off"),
        osm_place(2, opening_hours="Mo-Fr 09:00-17:00; PH off"),
    ], now=NOW)
    assert tasks(repo, OUT_OF_SPEC) == []


@needs_node
def test_an_error_behind_school_holidays_still_makes_a_task(repo):
    value = "SH off; Mo-Fr 25:00-26:00"
    build(repo, (osm_record(A, 1, opening_hours=value), osm_place(1, opening_hours=value)))
    assert [t["features"][0]["properties"]["id"] for t in tasks(repo, OUT_OF_SPEC)] == [f"{A}:opening_hours"]


@needs_node
def test_a_local_format_phone_makes_a_task(repo):
    build(repo, (osm_record(A, 1, phone="02 5550 0100"), osm_place(1, phone="02 5550 0100")))
    (task,) = tasks(repo, OUT_OF_SPEC)
    properties = task["features"][0]["properties"]
    assert properties["id"] == f"{A}:phone"
    assert properties["tag"] == "phone"
    assert properties["value"] == "02 5550 0100"
    check = properties["check"]
    assert "phone=02 5550 0100" in check
    assert "+61" in check
    assert "https://wiki.openstreetmap.org/wiki/Key:phone" in check
    assert "skip the task" in check


@needs_node
@pytest.mark.parametrize("phone", ["(02) 5550 0100", "+61 (2) 5550 0100", "61 2 5550 0100", "+61 2 5550 0100 ext 2"])
def test_a_phone_with_other_characters_or_no_country_code_makes_a_task(repo, phone):
    build(repo, (osm_record(A, 1, phone=phone), osm_place(1, phone=phone)))
    assert len(tasks(repo, OUT_OF_SPEC)) == 1


@needs_node
def test_an_international_phone_makes_no_task(repo):
    facts = {"phone": "+61 2 5550 0100", "phone_2": "+61-400-555-010"}
    build(repo, (osm_record(A, 1, **facts), osm_place(1, **facts)))
    assert tasks(repo, OUT_OF_SPEC) == []


@needs_node
def test_a_local_second_number_makes_one_phone_task_naming_both(repo):
    facts = {"phone": "+61 2 5550 0100", "phone_2": "0400 555 010"}
    build(repo, (osm_record(A, 1, **facts), osm_place(1, **facts)))
    (task,) = tasks(repo, OUT_OF_SPEC)
    assert task["features"][0]["properties"]["value"] == "+61 2 5550 0100;0400 555 010"


@needs_node
def test_a_value_not_from_osm_is_never_checked(repo):
    facts = {"phone": "02 5550 0100", "opening_hours": "Mo-Fr 25:00-26:00"}
    rec = record(A, 1, facts=facts, fact_sources={"phone": "feedback", "opening_hours": "contributed"})
    build(repo, (rec, osm_place(1, **facts)))
    assert tasks(repo, OUT_OF_SPEC) == []


def test_without_node_the_file_is_left_alone_and_the_report_says_why(repo, monkeypatch):
    path = repo / "maproulette" / OUT_OF_SPEC
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = RS + json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": None, "properties": {"id": f"{B}:phone", "name": "Old Camp"}},
    ]}) + "\n"
    path.write_text(previous, encoding="utf-8")
    monkeypatch.setenv("OH_CHECK_NODE", "no-such-node-binary")
    result = build(repo, (osm_record(A, 1, phone="02 5550 0100"), osm_place(1, phone="02 5550 0100")))
    assert path.read_text(encoding="utf-8") == previous
    assert result[OUT_OF_SPEC]["count"] == 1
    assert "node" in result[OUT_OF_SPEC]["skipped"]
    assert "Not built:" in render_report(result)
