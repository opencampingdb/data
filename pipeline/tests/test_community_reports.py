"""Values and places from community reports (source community_report).

They validate like contributed values, survive the OSM sync without ever
becoming OSM values, move on a merge, and never reach OSM: not through either
MapRoulette challenge file.
"""

import json

import pytest

from pipeline.common import NOT_FOR_OSM, SOURCES, dump_record, load_places, place_path
from pipeline.extract import extract_file
from pipeline.maproulette import DOUBLE_MAPPING, REPORT_SOURCES, TRAVELLER_REPORTS, write_challenges
from pipeline.merge import MOVABLE_SOURCES
from pipeline.sync import sync
from pipeline.validate import validate_repo

T1 = "2026-10-01T16:00:00Z"
T2 = "2026-10-02T16:00:00Z"
REPORTED = "2026-03-01T00:00:00Z"
NEW_ID = "ocdb_01JBX0000000000000000000R1"


def write(repo, rec):
    path = place_path(repo, rec["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_record(rec), encoding="utf-8", newline="\n")
    return path


def community_place(**changes):
    """A new place built from community reports: no OSM link."""
    rec = {
        "id": NEW_ID,
        "name": "Reported Creek Camp",
        "kind": "free_camp",
        "group": "overnight",
        "location": {"lat": -30.0, "lon": 140.0},
        "osm": None,
        "facts": {"toilets": True},
        "status": "open",
        "rating": None,
        "fact_sources": {"name": "community_report", "location": "community_report", "toilets": "community_report"},
        "fact_dates": {"name": REPORTED, "location": REPORTED, "toilets": REPORTED},
        "updated": T1,
    }
    rec.update(changes)
    return rec


def osm_place(osm_id, name="Lake Camp", **facts):
    return {
        "osm": {"type": "node", "id": osm_id, "version": 1},
        "kind": "free_camp",
        "name": name,
        "location": {"lat": -35.0, "lon": 145.0},
        "facts": facts,
    }


def osm_record(place_id, osm_id, **changes):
    rec = {
        "id": place_id,
        "name": "Lake Camp",
        "kind": "free_camp",
        "group": "overnight",
        "location": {"lat": -35.0, "lon": 145.0},
        "osm": {"type": "node", "id": osm_id, "version": 1},
        "facts": {},
        "status": "open",
        "rating": None,
        "fact_sources": {},
        "updated": T1,
    }
    rec.update(changes)
    return rec


A = "ocdb_01M66ES9DS1AQB245SJJXW0J18"
B = "ocdb_01M66ES9WTB7KSKMPCWQGC6HXN"


@pytest.fixture
def synced(repo, v1_osm):
    sync(repo, extract_file(v1_osm), now=T1)
    return repo


def first_osm_record(repo):
    path = sorted((repo / "places").rglob("*.json"))[0]
    return path, json.loads(path.read_text(encoding="utf-8"))


# ------------------------------------------------------------------ sources


def test_community_report_is_a_source_that_never_goes_to_osm():
    assert "community_report" in SOURCES
    assert "community_report" in NOT_FOR_OSM
    assert not set(REPORT_SOURCES) & set(NOT_FOR_OSM)
    assert "community_report" in MOVABLE_SOURCES


# --------------------------------------------------------------- validation


def test_new_place_from_community_reports_is_valid(synced):
    write(synced, community_place())
    assert validate_repo(synced) == []


def test_community_report_fact_on_an_osm_place_is_valid(synced):
    path, rec = first_osm_record(synced)
    rec["facts"]["power"] = True
    rec["fact_sources"]["power"] = "community_report"
    rec["fact_dates"] = {"power": REPORTED}
    write(synced, rec)
    assert validate_repo(synced) == []


def test_community_report_value_needs_a_date(synced):
    rec = community_place()
    del rec["fact_dates"]["toilets"]
    write(synced, rec)
    assert any("fact_dates" in e and "toilets" in e for e in validate_repo(synced))


def test_community_report_temporary_closure_is_valid(synced):
    path, rec = first_osm_record(synced)
    rec["status"] = "temporarily_closed"
    rec["fact_sources"]["status"] = "community_report"
    rec["fact_dates"] = {"status": REPORTED}
    write(synced, rec)
    assert validate_repo(synced) == []


# --------------------------------------------------------------------- sync


def test_sync_never_touches_a_community_report_place(repo, v1_osm, v2_osm):
    path = write(repo, community_place())
    before = path.read_bytes()
    for osm_file, now in ((v1_osm, T1), (v2_osm, T2)):
        result = sync(repo, extract_file(osm_file), now=now)
        touched = {r["id"] for r in result.new + result.updated + result.gone}
        assert NEW_ID not in touched
        assert path.read_bytes() == before


def test_sync_keeps_community_report_facts_and_their_source(repo, v1_osm, v2_osm):
    sync(repo, extract_file(v1_osm), now=T1)
    path, rec = first_osm_record(repo)
    osm_ref = (rec["osm"]["type"], rec["osm"]["id"])
    rec["facts"]["power"] = True
    rec["fact_sources"]["power"] = "community_report"
    rec["facts"]["big_rigs"] = True
    rec["fact_sources"]["big_rigs"] = "community_report"
    rec["fact_dates"] = {"power": REPORTED, "big_rigs": REPORTED}
    write(repo, rec)

    sync(repo, extract_file(v2_osm), now=T2)
    after = {(r["osm"]["type"], r["osm"]["id"]): r for r in load_places(repo).values() if r["osm"]}[osm_ref]
    assert after["facts"]["power"] is True and after["facts"]["big_rigs"] is True
    assert after["fact_sources"]["power"] == "community_report"
    assert after["fact_sources"]["big_rigs"] == "community_report"
    assert after["fact_dates"] == {"power": REPORTED, "big_rigs": REPORTED}
    assert validate_repo(repo) == []


# --------------------------------------------------------------- MapRoulette


def test_community_report_facts_never_become_tasks(repo):
    write(repo, osm_record(A, 1, facts={"fee": False, "toilets": True},
                           fact_sources={"fee": "community_report", "toilets": "community_report"},
                           fact_dates={"fee": REPORTED, "toilets": REPORTED}))
    write_challenges(repo, [osm_place(1)])
    assert (repo / "maproulette" / TRAVELLER_REPORTS).read_text(encoding="utf-8") == ""


def test_feedback_still_goes_when_community_report_facts_sit_beside_it(repo):
    write(repo, osm_record(A, 1, facts={"fee": False, "toilets": True},
                           fact_sources={"fee": "feedback", "toilets": "community_report"},
                           fact_dates={"fee": T1, "toilets": REPORTED}))
    write_challenges(repo, [osm_place(1)])
    text = (repo / "maproulette" / TRAVELLER_REPORTS).read_text(encoding="utf-8")
    assert "fee=no" in text
    assert "toilets" not in text


def test_place_named_or_placed_by_community_reports_is_never_sent(repo):
    # Even with an OSM link and feedback on it, a place whose name or
    # location came from community reports stays out of both challenges.
    write(repo, osm_record(A, 1, name="Reported Name", facts={"fee": False},
                           fact_sources={"name": "community_report", "fee": "feedback"},
                           fact_dates={"name": REPORTED, "fee": T1},
                           merged_osm=[{"type": "node", "id": 2}]))
    write(repo, osm_record(B, 3, location={"lat": -35.1, "lon": 145.1}, facts={"fee": False},
                           fact_sources={"location": "community_report", "fee": "feedback"},
                           fact_dates={"location": REPORTED, "fee": T1}))
    write_challenges(repo, [osm_place(1), osm_place(2), osm_place(3)])
    assert (repo / "maproulette" / TRAVELLER_REPORTS).read_text(encoding="utf-8") == ""
    assert (repo / "maproulette" / DOUBLE_MAPPING).read_text(encoding="utf-8") == ""


def test_new_place_from_community_reports_is_never_sent(repo):
    write(repo, community_place())
    write_challenges(repo, [osm_place(1)])
    assert (repo / "maproulette" / TRAVELLER_REPORTS).read_text(encoding="utf-8") == ""
    assert (repo / "maproulette" / DOUBLE_MAPPING).read_text(encoding="utf-8") == ""
