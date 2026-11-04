"""Tests for pipeline.maproulette (the MapRoulette challenge files) and merge --part."""

import json
from pathlib import Path

from pipeline.common import dump_record, load_places, place_path
from pipeline.maproulette import DOUBLE_MAPPING, RS, TRAVELLER_REPORTS, osm_tag, render_report, write_challenges
from pipeline.merge import merge
from pipeline.validate import validate_repo

T1 = "2026-10-01T16:00:00Z"


def record(place_id, osm_id, name="Lake Camp", osm_type="node", **changes):
    rec = {
        "id": place_id,
        "name": name,
        "kind": "free_camp",
        "group": "overnight",
        "location": {"lat": -35.0, "lon": 145.0},
        "osm": {"type": osm_type, "id": osm_id, "version": 1},
        "facts": {},
        "status": "open",
        "rating": None,
        "fact_sources": {},
        "updated": T1,
    }
    rec.update(changes)
    return rec


def osm_place(osm_id, name="Lake Camp", osm_type="node", lat=-35.0, lon=145.0, **facts):
    return {
        "osm": {"type": osm_type, "id": osm_id, "version": 1},
        "kind": "free_camp",
        "name": name,
        "location": {"lat": lat, "lon": lon},
        "facts": facts,
    }


def write(repo, *records):
    for rec in records:
        path = place_path(repo, rec["id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dump_record(rec), encoding="utf-8")


def tasks(repo, name):
    # Not splitlines(): it also splits on the record separator.
    lines = (repo / "maproulette" / name).read_text(encoding="utf-8").split("\n")[:-1]
    assert all(line.startswith(RS) for line in lines)
    return [json.loads(line[1:]) for line in lines]


A = "ocdb_01M66ES9DS1AQB245SJJXW0J18"
B = "ocdb_01M66ES9WTB7KSKMPCWQGC6HXN"


# ----------------------------------------------------------- double mapping


def test_merged_place_still_mapped_twice_is_a_task(repo):
    write(repo, record(A, 1, merged_osm=[{"type": "way", "id": 2}]))
    write_challenges(repo, [osm_place(1), osm_place(2, osm_type="way", lat=-35.001)])
    (task,) = tasks(repo, DOUBLE_MAPPING)
    assert [f["properties"]["@id"] for f in task["features"]] == ["node/1", "way/2"]
    assert {f["properties"]["id"] for f in task["features"]} == {A}
    assert task["features"][0]["properties"]["objects"] == "node/1 and way/2"
    assert task["features"][1]["geometry"]["coordinates"] == [145.0, -35.001]
    assert task["features"][0]["properties"]["record"].endswith(f"/places/18/{A}.json")


def test_task_goes_once_osm_keeps_one_object(repo):
    write(repo, record(A, 1, merged_osm=[{"type": "way", "id": 2}]))
    write_challenges(repo, [osm_place(1)])
    assert tasks(repo, DOUBLE_MAPPING) == []


def test_parts_are_not_double_mapping(repo):
    write(repo, record(A, 1, merged_osm=[{"type": "node", "id": 2, "part": True}]))
    write_challenges(repo, [osm_place(1), osm_place(2)])
    assert tasks(repo, DOUBLE_MAPPING) == []


def test_merge_part_marks_the_dropped_link_and_validates(repo):
    write(repo, record(A, 1), record(B, 2))
    kept = merge(repo, A, B, now=T1, part=True)
    assert kept["merged_osm"] == [{"id": 2, "part": True, "type": "node"}]
    assert validate_repo(repo) == []


def test_part_flags_survive_a_later_merge(repo):
    c = "ocdb_01M66ES9WTB7KSKMPCWQGC6HXP"
    write(repo, record(A, 1), record(B, 2), record(c, 3))
    merge(repo, B, c, now=T1, part=True)
    kept = merge(repo, A, B, now=T1)
    assert kept["merged_osm"] == [{"id": 2, "type": "node"}, {"id": 3, "part": True, "type": "node"}]


# --------------------------------------------------------- traveller reports


def test_feedback_that_osm_does_not_show_is_a_task(repo):
    rec = record(
        A, 1,
        facts={"fee": False, "toilets": True, "pets": "leashed"},
        fact_sources={"fee": "feedback", "toilets": "osm", "pets": "owner"},
    )
    write(repo, rec)
    write_challenges(repo, [osm_place(1, fee=True, toilets=True)])
    (task,) = tasks(repo, TRAVELLER_REPORTS)
    (feature,) = task["features"]
    assert feature["properties"]["@id"] == "node/1"
    assert feature["properties"]["changes"] == (
        "- Travellers report there is no fee to stay (fee=no).\n"
        "- The owner reports dogs are allowed on a lead (dog=leashed)."
    )


def test_task_goes_once_osm_agrees(repo):
    write(repo, record(A, 1, facts={"fee": False}, fact_sources={"fee": "feedback"}))
    write_challenges(repo, [osm_place(1, fee=False)])
    assert tasks(repo, TRAVELLER_REPORTS) == []


def test_contributed_values_and_places_outside_osm_are_never_sent(repo):
    write(
        repo,
        record(A, 1, facts={"fee": False}, fact_sources={"fee": "contributed"}),
        record(B, 2, osm=None, facts={"fee": False}, fact_sources={"fee": "feedback"}),
    )
    write_challenges(repo, [osm_place(1), osm_place(2)])
    assert tasks(repo, TRAVELLER_REPORTS) == []


def test_contributed_facts_stay_out_of_a_task_with_feedback(repo):
    # A contributed dump point list adds dump_station and drinking_water to an OSM place,
    # and a new dump point without an OSM link. Neither reaches MapRoulette.
    rec = record(
        A, 1,
        facts={"dump_station": True, "drinking_water": True, "toilets": True},
        fact_sources={"dump_station": "contributed", "drinking_water": "contributed", "toilets": "feedback"},
    )
    new = record(B, 2, osm=None, kind="dump_point", group="utility", facts={"fee": False},
                 fact_sources={"name": "contributed", "location": "contributed", "fee": "contributed"})
    write(repo, rec, new)
    write_challenges(repo, [osm_place(1, drinking_water=False)])
    (task,) = tasks(repo, TRAVELLER_REPORTS)
    assert task["features"][0]["properties"]["changes"] == "- Travellers report there are toilets (toilets=yes)."
    assert tasks(repo, DOUBLE_MAPPING) == []


def test_untagged_facts_and_contact_details_are_left_out(repo):
    rec = record(
        A, 1, name="Call 0412 345 678",
        facts={"booking_url": "https://example.com", "opening_hours": "ring 0412 345 678", "website": "https://lake.example"},
        fact_sources={"name": "feedback", "booking_url": "feedback", "opening_hours": "feedback", "website": "feedback"},
    )
    write(repo, rec)
    write_challenges(repo, [osm_place(1)])
    (task,) = tasks(repo, TRAVELLER_REPORTS)
    assert task["features"][0]["properties"]["changes"] == (
        "- Travellers report the website is https://lake.example (website=https://lake.example)."
    )


def test_name_and_closure_from_feedback(repo):
    rec = record(A, 1, name="Lake Bend Camp", status="closed", fact_sources={"name": "owner", "status": "feedback"})
    write(repo, rec)
    write_challenges(repo, [osm_place(1, name="Lake Camp")])
    lines = tasks(repo, TRAVELLER_REPORTS)[0]["features"][0]["properties"]["changes"].splitlines()
    assert lines[0] == "- The owner reports the name is Lake Bend Camp (name=Lake Bend Camp)."
    assert lines[1].startswith("- Travellers report it has closed for good.")


def test_osm_tags_follow_the_extract():
    assert osm_tag("showers", True) == "shower=yes"
    assert osm_tag("wifi", False) == "internet_access=no"
    assert osm_tag("fee", "donation") == "fee=donation"
    assert osm_tag("pets", "seasonal") is None
    assert osm_tag("caravans", "offroad") is None
    assert osm_tag("access", "4wd") == "4wd_only=yes"


def test_files_are_stable(repo):
    write(repo, record(A, 1, facts={"fee": False}, fact_sources={"fee": "feedback"}))
    extract = [osm_place(1)]
    write_challenges(repo, extract)
    first = (repo / "maproulette" / TRAVELLER_REPORTS).read_bytes()
    write_challenges(repo, extract)
    assert (repo / "maproulette" / TRAVELLER_REPORTS).read_bytes() == first
    assert load_places(repo)[A]["updated"] == T1


def test_report_names_new_and_gone_tasks(repo):
    write(repo, record(A, 1, facts={"fee": False}, fact_sources={"fee": "feedback"}))
    first = write_challenges(repo, [osm_place(1)])
    assert first[TRAVELLER_REPORTS]["added"] == [(A, "Lake Camp")]
    second = write_challenges(repo, [osm_place(1, fee=False)])
    assert second[TRAVELLER_REPORTS] == {"count": 0, "added": [], "removed": [(A, "Lake Camp")]}
    assert f"  - Gone: Lake Camp ({A})" in render_report(second)
