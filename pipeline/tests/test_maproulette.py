"""Tests for pipeline.maproulette (the MapRoulette challenge files) and merge --part."""

import json

import pytest
from datetime import datetime, timezone
from pathlib import Path

from pipeline.common import dump_record, load_places, place_path
from pipeline.maproulette import (
    DOUBLE_MAPPING,
    RS,
    TRAVELLER_REPORTS,
    descriptive_name,
    main,
    osm_tag,
    render_report,
    write_challenges,
)
from pipeline.merge import merge
from pipeline.validate import validate_repo

T1 = "2026-10-01T16:00:00Z"
NOW = datetime(2026, 11, 5, 6, 0, tzinfo=timezone.utc)


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


def fix(repo):
    (task,) = tasks(repo, DOUBLE_MAPPING)
    return task["features"][0]["properties"]["fix"]


def test_a_point_and_an_outline_go_into_the_outline(repo):
    write(repo, record(A, 1, merged_osm=[{"type": "way", "id": 2}]))
    write_challenges(repo, [osm_place(1), osm_place(2, osm_type="way", lat=-35.0001)])
    assert fix(repo) == (
        "Likely fix: keep the outline way/2, copy across any tags only node/1 has, then delete the point. "
        "In iD, select them together and use Merge (C), which does both."
    )


def test_two_points_keep_one(repo):
    write(repo, record(A, 1, merged_osm=[{"type": "node", "id": 2}]))
    write_challenges(repo, [osm_place(1), osm_place(2, lat=-35.0001)])
    assert fix(repo).startswith("Likely fix: keep one point, usually the one with the longer history")
    assert "Merge Nodes (M)" in fix(repo)


def test_two_outlines_keep_the_better_boundary(repo):
    write(repo, record(A, 1, osm_type="way", merged_osm=[{"type": "relation", "id": 2}]))
    write_challenges(repo, [osm_place(1, osm_type="way"), osm_place(2, osm_type="relation", lat=-35.0001)])
    assert fix(repo).startswith("Likely fix: keep the outline that best follows the site's boundary")


def test_disagreeing_kinds_and_tags_are_named(repo):
    write(repo, record(A, 1, merged_osm=[{"type": "way", "id": 2}]))
    caravan_park = {**osm_place(2, osm_type="way", lat=-35.0001, fee=True, toilets=True), "kind": "caravan_park"}
    write_challenges(repo, [osm_place(1, fee=False, toilets=True, address={"street": "x"}), caravan_park])
    conflicts = fix(repo).split("\n\n")[1]
    assert conflicts == (
        "They are tagged as different kinds of place: Free camp on node/1, Caravan park on way/2. "
        "They disagree on fee=no on node/1, fee=yes on way/2. Check which is right before you keep one value."
    )


def test_task_goes_once_osm_keeps_one_object(repo):
    write(repo, record(A, 1, merged_osm=[{"type": "way", "id": 2}]))
    write_challenges(repo, [osm_place(1)])
    assert tasks(repo, DOUBLE_MAPPING) == []


@pytest.mark.parametrize("kind", ["roofed_accommodation", "backpacker_hostel"])
def test_motels_and_hostels_mapped_twice_are_not_camp_site_tasks(repo, kind):
    write(repo, record(A, 1, kind=kind, merged_osm=[{"type": "way", "id": 2}]))
    write_challenges(repo, [osm_place(1), osm_place(2, osm_type="way", lat=-35.0001)])
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

_items = iter(range(10_000))


def fact(place, field, value, source="o1", received="2026-11-04T00:50:54Z", result="applied", **extra):
    """An applied fact report as the inbox stores it."""
    item = {
        "id": f"fb_{next(_items):026d}",
        "received": received,
        "type": "report",
        "place": place,
        "device": f"dev-{source}",
        "origin": source,
        "contributor": f"ctr-{source}",
        "attested": "app_attest",
        "report": {"kind": "fact", "fact": field, "value": value, "note": "secret words from a traveller"},
        "outcome": {"result": result, "reason": "test", "run": received},
    }
    item.update(extra)
    return item


def name_report(place, name, source):
    item = fact(place, None, None, source=source)
    item["report"] = {"kind": "name", "value": name}
    return item


def owner_update(place, received="2026-11-03T02:00:00Z", **update):
    return {
        "id": f"fb_{next(_items):026d}",
        "received": received,
        "type": "owner_update",
        "place": place,
        "device": "dev-owner",
        "token_sha256": "0" * 64,
        "update": update,
        "outcome": {"result": "applied", "reason": "test", "run": received},
    }


def inbox(tmp_path, *items):
    root = tmp_path / "inbox"
    for item in items:
        path = root / "applied" / item["received"][:10] / f"{item['id']}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(item), encoding="utf-8")
    (root / "applied").mkdir(parents=True, exist_ok=True)
    return root


def changes(repo):
    (task,) = tasks(repo, TRAVELLER_REPORTS)
    return task["features"][0]["properties"]["changes"]


def test_one_traveller_is_not_enough(repo, tmp_path):
    write(repo, record(A, 1, facts={"access": "2wd"}, fact_sources={"access": "feedback"}))
    write_challenges(repo, [osm_place(1)], inbox=inbox(tmp_path, fact(A, "access", "2wd")), now=NOW)
    assert tasks(repo, TRAVELLER_REPORTS) == []


def test_one_source_on_two_devices_is_one_traveller(repo, tmp_path):
    write(repo, record(A, 1, facts={"access": "2wd"}, fact_sources={"access": "feedback"}))
    items = [fact(A, "access", "2wd", device="d1"), fact(A, "access", "2wd", device="d2")]
    write_challenges(repo, [osm_place(1)], inbox=inbox(tmp_path, *items), now=NOW)
    assert tasks(repo, TRAVELLER_REPORTS) == []


def test_two_travellers_make_a_task_with_count_date_and_check(repo, tmp_path):
    write(repo, record(A, 1, facts={"access": "2wd"}, fact_sources={"access": "feedback"}))
    items = [
        fact(A, "access", "2wd", source="o1", received="2026-10-20T03:00:00Z"),
        fact(A, "access", "2wd", source="o2", received="2026-11-04T00:50:54Z"),
    ]
    write_challenges(repo, [osm_place(1)], inbox=inbox(tmp_path, *items), now=NOW)
    (task,) = tasks(repo, TRAVELLER_REPORTS)
    properties = task["features"][0]["properties"]
    assert properties["changes"] == (
        "- 2 travellers reported the road is fine for 2WD (4wd_only=no), most recently on 4 November 2026.\n"
        "  - Check it with a survey, or street-level photos (Mapillary or Panoramax) of the access road.\n"
        "  - If it checks out, set 4wd_only=no. OSM wiki: https://wiki.openstreetmap.org/wiki/Key:4wd_only"
    )
    assert properties["ocdb_id"] == A
    assert properties["record"].endswith(f"/places/18/{A}.json")


def test_only_reports_of_the_same_value_recent_and_applied_count(repo, tmp_path):
    write(repo, record(A, 1, facts={"access": "2wd"}, fact_sources={"access": "feedback"}))
    items = [
        fact(A, "access", "2wd", source="o1"),
        fact(A, "access", "4wd", source="o2"),
        fact(A, "access", "2wd", source="o3", received="2026-07-01T00:00:00Z"),
        fact(A, "access", "2wd", source="o4", result="refused"),
        fact(B, "access", "2wd", source="o5"),
    ]
    write_challenges(repo, [osm_place(1)], inbox=inbox(tmp_path, *items), now=NOW)
    assert tasks(repo, TRAVELLER_REPORTS) == []


def test_reports_on_a_merged_record_count_for_the_kept_one(repo, tmp_path):
    write(
        repo,
        record(A, 1, facts={"fee": False}, fact_sources={"fee": "feedback"}),
        record(B, 2, status="merged", merged_into=A),
    )
    items = [fact(A, "fee", False, source="o1"), fact(B, "fee", False, source="o2")]
    write_challenges(repo, [osm_place(1, fee=True)], inbox=inbox(tmp_path, *items), now=NOW)
    assert changes(repo).startswith("- 2 travellers reported there is no fee to stay (fee=no), most recently on 4 November 2026.")
    assert "the operator's own sign or website" in changes(repo)


def test_verified_owner_is_enough(repo, tmp_path):
    rec = record(
        A, 1, owner_verified=True,
        facts={"pets": "leashed"}, fact_sources={"pets": "owner"}, fact_dates={"pets": "2026-11-03T02:10:00Z"},
    )
    write(repo, rec)
    write_challenges(repo, [osm_place(1)], inbox=inbox(tmp_path, owner_update(A, facts={"pets": "leashed"})), now=NOW)
    assert changes(repo) == (
        "- The verified owner says dogs are allowed on a lead (dog=leashed), set on 3 November 2026.\n"
        "  - Check it with a survey, or the operator's own sign or website.\n"
        "  - If it checks out, set dog=leashed. OSM wiki: https://wiki.openstreetmap.org/wiki/Key:dog"
    )


def test_owner_value_without_its_update_in_the_inbox_is_not_sent(repo, tmp_path):
    write(repo, record(A, 1, owner_verified=True, facts={"pets": "leashed"}, fact_sources={"pets": "owner"}))
    write_challenges(repo, [osm_place(1)], inbox=inbox(tmp_path), now=NOW)
    assert tasks(repo, TRAVELLER_REPORTS) == []


def test_descriptive_name_is_never_sent(repo, tmp_path):
    name = "Heavy Vehicle Rest Area"
    write(repo, record(A, 1, name=name, kind="rest_area", fact_sources={"name": "feedback"}))
    items = [name_report(A, name, "o1"), name_report(A, name, "o2")]
    write_challenges(repo, [osm_place(1, name=None)], inbox=inbox(tmp_path, *items), now=NOW)
    assert tasks(repo, TRAVELLER_REPORTS) == []


def test_descriptive_names():
    assert descriptive_name("Heavy Vehicle Rest Area")
    assert descriptive_name("heavy vehicle rest area")
    assert descriptive_name("Truck Parking")
    assert descriptive_name("Free Camp")
    assert descriptive_name("Caravan Park & Camping Area")
    assert descriptive_name("Information centre", "information_centre")
    assert not descriptive_name("Lake Bend Camp")
    assert not descriptive_name("Gundagai Rest Area")


def test_a_real_name_from_two_travellers_is_sent(repo, tmp_path):
    write(repo, record(A, 1, name="Lake Bend Camp", fact_sources={"name": "feedback"}))
    items = [name_report(A, "Lake Bend Camp", "o1"), name_report(A, "Lake Bend Camp", "o2")]
    write_challenges(repo, [osm_place(1, name="Lake Camp")], inbox=inbox(tmp_path, *items), now=NOW)
    assert changes(repo) == (
        "- 2 travellers reported the name is Lake Bend Camp (name=Lake Bend Camp), most recently on 4 November 2026.\n"
        "  - Check it with the signage at the place, or a survey.\n"
        "  - If it checks out, set name=Lake Bend Camp. The name tag holds the name on the sign, never a description. "
        "OSM wiki: https://wiki.openstreetmap.org/wiki/Key:name"
    )


def test_closure_from_two_travellers(repo, tmp_path):
    write(repo, record(A, 1, status="closed", fact_sources={"status": "feedback"}))
    closed = fact(A, None, None, source="o1")
    closed["report"] = {"kind": "closed"}
    gone = {**fact(A, None, None, source="o2"), "type": "check", "check": {"visited": "2026-11-03", "still_there": False}}
    del gone["report"]
    write_challenges(repo, [osm_place(1)], inbox=inbox(tmp_path, closed, gone), now=NOW)
    assert changes(repo).startswith("- 2 travellers reported it has closed for good")
    assert changes(repo).endswith(
        "such as disused:tourism=camp_site. Once nothing is left on the ground, delete the object. "
        "OSM wiki: https://wiki.openstreetmap.org/wiki/Lifecycle_prefix"
    )


def doubt(place, field, source, received="2026-11-03T00:00:00Z", result="needs_review"):
    """A doubt as the inbox stores it: the fact is wrong, with no value."""
    item = fact(place, field, None, source=source, received=received, result=result)
    item["report"] = {"kind": "doubt", "fact": field, "note": "secret words from a traveller"}
    return item


def confirmed(*sources):
    return [fact(A, "access", "2wd", source=source) for source in sources]


def access_task(repo, tmp_path, *items):
    write(repo, record(A, 1, facts={"access": "2wd"}, fact_sources={"access": "feedback"}))
    write_challenges(repo, [osm_place(1)], inbox=inbox(tmp_path, *items), now=NOW)
    return tasks(repo, TRAVELLER_REPORTS)


def test_confirmations_leading_doubts_by_two_make_a_task(repo, tmp_path):
    (task,) = access_task(repo, tmp_path, *confirmed("o1", "o2", "o3"), doubt(A, "access", "o4"))
    assert task["features"][0]["properties"]["changes"].startswith("- 3 travellers reported the road is fine for 2WD")


def test_a_close_case_stays_out(repo, tmp_path):
    assert access_task(repo, tmp_path, *confirmed("o1", "o2"), doubt(A, "access", "o3")) == []


def test_a_doubt_counts_whatever_its_outcome_unless_refused(repo, tmp_path):
    for result in ("needs_review", "applied"):
        assert access_task(repo, tmp_path / result, *confirmed("o1", "o2", "o3"), doubt(A, "access", "o4", result=result),
                           doubt(A, "access", "o5", result=result)) == []
    refused = [doubt(A, "access", "o4", result="refused"), doubt(A, "access", "o5", result="refused")]
    assert len(access_task(repo, tmp_path / "refused", *confirmed("o1", "o2", "o3"), *refused)) == 1


def test_doubts_older_than_90_days_stop_counting(repo, tmp_path):
    old = [doubt(A, "access", "o3", received="2026-07-01T00:00:00Z"), doubt(A, "access", "o4", received="2026-07-01T00:00:00Z")]
    assert len(access_task(repo, tmp_path, *confirmed("o1", "o2"), *old)) == 1


def test_one_source_doubting_twice_is_one_doubt(repo, tmp_path):
    twice = [doubt(A, "access", "o4"), doubt(A, "access", "o4", received="2026-11-04T00:00:00Z")]
    assert len(access_task(repo, tmp_path, *confirmed("o1", "o2", "o3"), *twice)) == 1


def test_doubts_about_another_fact_or_place_do_not_count(repo, tmp_path):
    others = [doubt(A, "fee", "o3"), doubt(B, "access", "o4")]
    assert len(access_task(repo, tmp_path, *confirmed("o1", "o2"), *others)) == 1


def test_doubts_never_hold_back_a_verified_owner(repo, tmp_path):
    write(repo, record(A, 1, owner_verified=True, facts={"pets": "leashed"}, fact_sources={"pets": "owner"},
                       fact_dates={"pets": "2026-11-03T02:00:00Z"}))
    items = [owner_update(A, facts={"pets": "leashed"}), doubt(A, "pets", "o1"), doubt(A, "pets", "o2")]
    write_challenges(repo, [osm_place(1)], inbox=inbox(tmp_path, *items), now=NOW)
    assert "The verified owner says" in changes(repo)


def test_doubts_never_become_tasks_or_leak_a_note(repo, tmp_path):
    items = [doubt(A, "access", source) for source in ("o1", "o2", "o3")]
    assert access_task(repo, tmp_path, *items) == []
    text = (repo / "maproulette" / TRAVELLER_REPORTS).read_text(encoding="utf-8")
    assert "secret" not in text


def test_no_inbox_writes_an_empty_report_file_and_says_why(repo, capsys):
    write(repo, record(A, 1, facts={"fee": False}, fact_sources={"fee": "feedback"}))
    extract = repo / "extract.ndjson"
    extract.write_text(json.dumps(osm_place(1, fee=True)) + "\n", encoding="utf-8")
    report = repo / "report.md"
    assert main([str(extract), "--root", str(repo), "--report", str(report)]) == 0
    assert (repo / "maproulette" / TRAVELLER_REPORTS).read_text(encoding="utf-8") == ""
    assert "no --inbox given" in capsys.readouterr().err
    assert "Not built: no --inbox given" in report.read_text(encoding="utf-8")


def test_nothing_personal_reaches_a_task(repo, tmp_path):
    rec = record(
        A, 1, name="Lake Bend Camp",
        facts={"access": "2wd", "toilets": True}, fact_sources={"name": "feedback", "access": "feedback", "toilets": "feedback"},
    )
    write(repo, rec)
    items = [
        fact(A, "access", "2wd", source="origin-aaaa"), fact(A, "access", "2wd", source="origin-bbbb"),
        fact(A, "toilets", True, source="origin-aaaa"), fact(A, "toilets", True, source="origin-bbbb"),
        name_report(A, "Lake Bend Camp", "origin-aaaa"), name_report(A, "Lake Bend Camp", "origin-bbbb"),
    ]
    write_challenges(repo, [osm_place(1)], inbox=inbox(tmp_path, *items), now=NOW)
    text = (repo / "maproulette" / TRAVELLER_REPORTS).read_text(encoding="utf-8")
    assert text.count("travellers reported") == 3
    for secret in ("origin-", "dev-", "ctr-", "app_attest", "secret words", "fb_"):
        assert secret not in text


def test_task_goes_once_osm_agrees(repo, tmp_path):
    write(repo, record(A, 1, facts={"fee": False}, fact_sources={"fee": "feedback"}))
    items = [fact(A, "fee", False, source="o1"), fact(A, "fee", False, source="o2")]
    write_challenges(repo, [osm_place(1, fee=False)], inbox=inbox(tmp_path, *items), now=NOW)
    assert tasks(repo, TRAVELLER_REPORTS) == []


def test_contributed_values_and_places_outside_osm_are_never_sent(repo, tmp_path):
    write(
        repo,
        record(A, 1, facts={"fee": False}, fact_sources={"fee": "contributed"}),
        record(B, 2, osm=None, facts={"fee": False}, fact_sources={"fee": "feedback"}),
    )
    items = [fact(p, "fee", False, source=s) for p in (A, B) for s in ("o1", "o2")]
    write_challenges(repo, [osm_place(1), osm_place(2)], inbox=inbox(tmp_path, *items), now=NOW)
    assert tasks(repo, TRAVELLER_REPORTS) == []


def test_contributed_facts_stay_out_of_a_task_with_feedback(repo, tmp_path):
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
    items = [fact(A, f, True, source=s) for f in ("dump_station", "drinking_water", "toilets") for s in ("o1", "o2")]
    write_challenges(repo, [osm_place(1, drinking_water=False)], inbox=inbox(tmp_path, *items), now=NOW)
    assert changes(repo) == (
        "- 2 travellers reported there are toilets (toilets=yes), most recently on 4 November 2026.\n"
        "  - Check it with a survey, street-level photos or recent imagery.\n"
        "  - If it checks out, set toilets=yes. OSM wiki: https://wiki.openstreetmap.org/wiki/Key:toilets"
    )
    assert tasks(repo, DOUBLE_MAPPING) == []


def test_untagged_facts_and_contact_details_are_left_out(repo, tmp_path):
    rec = record(
        A, 1, name="Call 0412 345 678",
        facts={"booking_url": "https://example.com", "opening_hours": "ring 0412 345 678", "website": "https://lake.example"},
        fact_sources={"name": "feedback", "booking_url": "feedback", "opening_hours": "feedback", "website": "feedback"},
    )
    write(repo, rec)
    items = [name_report(A, "Call 0412 345 678", s) for s in ("o1", "o2")]
    for field, value in rec["facts"].items():
        items += [fact(A, field, value, source=s) for s in ("o1", "o2")]
    write_challenges(repo, [osm_place(1)], inbox=inbox(tmp_path, *items), now=NOW)
    assert changes(repo).splitlines()[0] == (
        "- 2 travellers reported the website is https://lake.example (website=https://lake.example), most recently on 4 November 2026."
    )
    assert len(changes(repo).splitlines()) == 3


def test_osm_tags_follow_the_extract():
    assert osm_tag("showers", True) == "shower=yes"
    assert osm_tag("wifi", False) == "internet_access=no"
    assert osm_tag("fee", "donation") == "fee=donation"
    assert osm_tag("pets", "seasonal") is None
    assert osm_tag("caravans", "offroad") is None
    assert osm_tag("access", "4wd") == "4wd_only=yes"
    assert osm_tag("booking_required", True) == "reservation=required"
    assert osm_tag("toilet_type", "flush") == "toilets:disposal=flush"
    assert osm_tag("toilet_type", "chemical") == "toilets:disposal=chemical"
    assert osm_tag("toilet_type", "dry") == "toilets:disposal=dry_toilet"
    assert osm_tag("hot_water", True) == "hot_water=yes"
    assert osm_tag("booking_url", "https://book.example") is None


def test_files_are_stable(repo, tmp_path):
    write(repo, record(A, 1, facts={"fee": False}, fact_sources={"fee": "feedback"}))
    box = inbox(tmp_path, fact(A, "fee", False, source="o1"), fact(A, "fee", False, source="o2"))
    extract = [osm_place(1)]
    write_challenges(repo, extract, inbox=box, now=NOW)
    first = (repo / "maproulette" / TRAVELLER_REPORTS).read_bytes()
    assert first
    write_challenges(repo, extract, inbox=box, now=NOW)
    assert (repo / "maproulette" / TRAVELLER_REPORTS).read_bytes() == first
    assert load_places(repo)[A]["updated"] == T1


def test_report_names_new_and_gone_tasks(repo, tmp_path):
    write(repo, record(A, 1, facts={"fee": False}, fact_sources={"fee": "feedback"}))
    box = inbox(tmp_path, fact(A, "fee", False, source="o1"), fact(A, "fee", False, source="o2"))
    first = write_challenges(repo, [osm_place(1)], inbox=box, now=NOW)
    assert first[TRAVELLER_REPORTS]["added"] == [(A, "Lake Camp")]
    second = write_challenges(repo, [osm_place(1, fee=False)], inbox=box, now=NOW)
    assert second[TRAVELLER_REPORTS] == {"count": 0, "added": [], "removed": [(A, "Lake Camp")]}
    assert f"  - Gone: Lake Camp ({A})" in render_report(second)


# ------------------------------------------------- audit run-2: OCDB-06, OCDB-09

NASTY = "Camp @example-org/example-team | `x` [verify](https://example.invalid/)"


def test_report_escapes_osm_names_of_new_and_gone_tasks():
    # An OSM editor chooses the name; the sync PR body must not mention, break out or link.
    result = {TRAVELLER_REPORTS: {"count": 1, "added": [(A, NASTY)], "removed": [(B, NASTY)]}}
    report = render_report(result)
    for word, place in (("New", A), ("Gone", B)):
        (line,) = [l for l in report.splitlines() if l.startswith(f"  - {word}:")]
        assert line.endswith(f"({place})")
        assert "@" not in line
        assert "`" not in line
        assert "[verify](" not in line
        assert "example-org/example-team" in line


def test_crowd_opening_hours_with_a_comment_is_never_a_task(repo, tmp_path):
    value = 'Mo-Su 08:00-17:00 "click here: [verify listing](//evil.example/x)"'
    write(repo, record(A, 1, facts={"opening_hours": value}, fact_sources={"opening_hours": "feedback"}))
    items = [fact(A, "opening_hours", value, source=s) for s in ("o1", "o2", "o3")]
    write_challenges(repo, [osm_place(1)], inbox=inbox(tmp_path, *items), now=NOW)
    assert tasks(repo, TRAVELLER_REPORTS) == []


def test_crowd_opening_hours_without_a_comment_is_still_sent(repo, tmp_path):
    value = "Mo-Su 08:00-17:00"
    write(repo, record(A, 1, facts={"opening_hours": value}, fact_sources={"opening_hours": "feedback"}))
    items = [fact(A, "opening_hours", value, source=s) for s in ("o1", "o2")]
    write_challenges(repo, [osm_place(1)], inbox=inbox(tmp_path, *items), now=NOW)
    assert "(opening_hours=Mo-Su 08:00-17:00)" in changes(repo)


def test_verified_owner_opening_hours_comment_is_still_sent(repo, tmp_path):
    value = 'Mo-Su 08:00-17:00 "office closes at noon on Sundays"'
    write(repo, record(A, 1, owner_verified=True, facts={"opening_hours": value}, fact_sources={"opening_hours": "owner"}))
    box = inbox(tmp_path, owner_update(A, facts={"opening_hours": value}))
    write_challenges(repo, [osm_place(1)], inbox=box, now=NOW)
    assert "office closes at noon on Sundays" in changes(repo)


# ------------------------------------------------------- newly mapped facts


def test_travellers_bins_differing_from_osm_make_a_task(repo, tmp_path):
    write(repo, record(A, 1, facts={"bins": True}, fact_sources={"bins": "feedback"}))
    items = [fact(A, "bins", True, source=s) for s in ("o1", "o2")]
    write_challenges(repo, [osm_place(1, bins=False)], inbox=inbox(tmp_path, *items), now=NOW)
    assert changes(repo) == (
        "- 2 travellers reported there are bins (bin=yes), most recently on 4 November 2026.\n"
        "  - Check it with a survey, street-level photos or recent imagery.\n"
        "  - If it checks out, set bin=yes. OSM wiki: https://wiki.openstreetmap.org/wiki/Key:bin"
    )


def test_booking_required_is_sent_and_the_booking_link_never_is(repo, tmp_path):
    facts = {"booking_required": False, "booking_url": "https://book.example/lake"}
    write(repo, record(A, 1, facts=facts, fact_sources={field: "feedback" for field in facts}))
    items = [fact(A, field, value, source=s) for field, value in facts.items() for s in ("o1", "o2")]
    write_challenges(repo, [osm_place(1, booking_required=True)], inbox=inbox(tmp_path, *items), now=NOW)
    assert changes(repo) == (
        "- 2 travellers reported no booking is needed (reservation=no), most recently on 4 November 2026.\n"
        "  - Check it with the operator's own sign or website.\n"
        "  - If it checks out, set reservation=no. OSM wiki: https://wiki.openstreetmap.org/wiki/Key:reservation"
    )


def test_a_booking_link_alone_never_makes_a_task(repo, tmp_path):
    write(repo, record(A, 1, facts={"booking_url": "https://book.example/lake"}, fact_sources={"booking_url": "owner"},
                       owner_verified=True))
    box = inbox(tmp_path, owner_update(A, facts={"booking_url": "https://book.example/lake"}))
    write_challenges(repo, [osm_place(1)], inbox=box, now=NOW)
    assert tasks(repo, TRAVELLER_REPORTS) == []


def test_travellers_toilet_type_differing_from_osm_make_a_task(repo, tmp_path):
    write(repo, record(A, 1, facts={"toilet_type": "pit"}, fact_sources={"toilet_type": "feedback"}))
    items = [fact(A, "toilet_type", "pit", source=s) for s in ("o1", "o2")]
    write_challenges(repo, [osm_place(1, toilet_type="flush")], inbox=inbox(tmp_path, *items), now=NOW)
    assert changes(repo) == (
        "- 2 travellers reported the toilets are pit toilets (toilets:disposal=pitlatrine), most recently on 4 November 2026.\n"
        "  - Check it with a survey, or the operator's own sign or website.\n"
        "  - If it checks out, set toilets:disposal=pitlatrine. OSM wiki: https://wiki.openstreetmap.org/wiki/Key:toilets:disposal"
    )


def test_owner_fishing_and_hot_water_differing_from_osm_make_a_task(repo, tmp_path):
    facts = {"fishing": True, "hot_water": False}
    write(repo, record(A, 1, owner_verified=True, facts=facts, fact_sources={field: "owner" for field in facts},
                       fact_dates={field: "2026-11-03T02:10:00Z" for field in facts}))
    box = inbox(tmp_path, owner_update(A, facts=facts))
    write_challenges(repo, [osm_place(1, fishing=False, hot_water=True)], inbox=box, now=NOW)
    lines = changes(repo).splitlines()
    assert lines[0] == "- The verified owner says fishing is allowed (fishing=yes), set on 3 November 2026."
    assert lines[2] == "  - If it checks out, set fishing=yes. OSM wiki: https://wiki.openstreetmap.org/wiki/Key:fishing"
    assert lines[3] == "- The verified owner says there is no hot water (hot_water=no), set on 3 November 2026."
    assert lines[4] == "  - Check it with a survey, or the operator's own sign or website."
    assert lines[5] == "  - If it checks out, set hot_water=no. OSM wiki: https://wiki.openstreetmap.org/wiki/Key:hot_water"


def test_owner_second_phone_joins_the_place_phone_tag(repo, tmp_path):
    facts = {"phone": "+61 2 5550 0100", "phone_2": "+61 2 5550 0199"}
    write(repo, record(A, 1, owner_verified=True, facts=facts, fact_sources={field: "owner" for field in facts},
                       fact_dates={field: "2026-11-03T02:10:00Z" for field in facts}))
    box = inbox(tmp_path, owner_update(A, facts=facts))
    write_challenges(repo, [osm_place(1, phone="+61 2 5550 0100")], inbox=box, now=NOW)
    assert changes(repo) == (
        "- The verified owner says the second phone number is +61 2 5550 0199 (phone=+61 2 5550 0100;+61 2 5550 0199),"
        " set on 3 November 2026.\n"
        "  - Check it with the operator's own sign or website.\n"
        "  - If it checks out, set phone=+61 2 5550 0100;+61 2 5550 0199. OSM wiki: https://wiki.openstreetmap.org/wiki/Key:phone"
    )


def test_a_second_phone_follows_the_osm_place_first_phone(repo, tmp_path):
    # A traveller's first number may differ from OSM's, so the tag keeps OSM's own first number.
    facts = {"phone": "+61 2 5550 0222", "phone_2": "+61 2 5550 0199"}
    write(repo, record(A, 1, owner_verified=True, facts=facts, fact_sources={field: "owner" for field in facts},
                       fact_dates={field: "2026-11-03T02:10:00Z" for field in facts}))
    box = inbox(tmp_path, owner_update(A, facts=facts))
    write_challenges(repo, [osm_place(1, phone="+61 2 5550 0100")], inbox=box, now=NOW)
    assert "(phone=+61 2 5550 0100;+61 2 5550 0199)" in changes(repo)
    assert "0222;" not in changes(repo)


def test_a_second_phone_is_never_sent_when_osm_has_no_first(repo, tmp_path):
    facts = {"phone": "+61 2 5550 0222", "phone_2": "+61 2 5550 0199"}
    write(repo, record(A, 1, owner_verified=True, facts=facts, fact_sources={field: "owner" for field in facts},
                       fact_dates={field: "2026-11-03T02:10:00Z" for field in facts}))
    box = inbox(tmp_path, owner_update(A, facts=facts))
    write_challenges(repo, [osm_place(1)], inbox=box, now=NOW)
    assert "second phone" not in changes(repo)


def test_a_second_phone_without_a_first_is_never_sent(repo, tmp_path):
    facts = {"phone_2": "+61 2 5550 0199"}
    write(repo, record(A, 1, owner_verified=True, facts=facts, fact_sources={"phone_2": "owner"},
                       fact_dates={"phone_2": "2026-11-03T02:10:00Z"}))
    write_challenges(repo, [osm_place(1)], inbox=inbox(tmp_path, owner_update(A, facts=facts)), now=NOW)
    assert tasks(repo, TRAVELLER_REPORTS) == []


@pytest.mark.parametrize("field, value", [
    ("pub", True), ("fuel", True), ("car_wash", True), ("crocodiles", True),
    ("nearest_beach", {"distance_m": 300}), ("nearest_boat_ramp", {"distance_m": 900}),
])
def test_a_nearby_fact_never_makes_a_task(repo, tmp_path, field, value):
    # These come from other OSM objects, so no tag on the place's own object would show them.
    write(repo, record(A, 1, facts={field: value}, fact_sources={field: "feedback"}))
    items = [fact(A, field, value, source=s) for s in ("o1", "o2")]
    write_challenges(repo, [osm_place(1)], inbox=inbox(tmp_path, *items), now=NOW)
    assert tasks(repo, TRAVELLER_REPORTS) == []
