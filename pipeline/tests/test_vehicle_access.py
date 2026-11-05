"""Tests for vehicle_access: places only heavy vehicles may use."""

import csv

import pytest

from pipeline.common import dump_record, place_path
from pipeline.extract import facts_from_tags
from pipeline.validate import validate_repo
from pipeline.vehicle_access import CANDIDATES_FILE, name_candidates, write_candidates

NOW = "2026-11-01T00:00:00Z"


@pytest.mark.parametrize("tags", [
    {"highway": "rest_area", "hgv": "designated", "motorcar": "no"},
    {"amenity": "parking", "hgv": "designated", "motor_vehicle": "no"},
    {"highway": "rest_area", "access": "no", "hgv": "yes"},
    {"amenity": "parking", "access": "no", "hgv": "designated"},
])
def test_osm_tags_mark_a_place_for_heavy_vehicles_only(tags):
    assert facts_from_tags(tags)["vehicle_access"] == "heavy_vehicles_only"


def test_access_no_for_heavy_vehicles_only_is_not_private_entry():
    facts = facts_from_tags({"highway": "rest_area", "access": "no", "hgv": "designated"})
    assert "entry" not in facts


@pytest.mark.parametrize("tags", [
    {"highway": "rest_area", "hgv": "designated"},
    {"highway": "rest_area", "hgv": "yes", "motorcar": "no"},
    {"highway": "rest_area", "access": "no"},
    {"highway": "rest_area", "access": "yes", "hgv": "designated"},
    {"highway": "rest_area", "access": "no", "hgv": "no"},
    {"highway": "rest_area", "hgv": "only"},
])
def test_other_tags_set_no_vehicle_access(tags):
    assert "vehicle_access" not in facts_from_tags(tags)


def record(place_id, name, kind="rest_area", facts=None, **changes):
    facts = facts or {}
    rec = {
        "id": place_id,
        "name": name,
        "kind": kind,
        "group": "utility",
        "location": {"lat": -31.5, "lon": 150.25},
        "osm": {"type": "node", "id": int(place_id[-4:], 36), "version": 1},
        "facts": facts,
        "fact_sources": {k: "osm" for k in facts},
        "status": "open",
        "rating": None,
        "updated": "2026-10-30T16:00:00Z",
    }
    rec.update(changes)
    return rec


@pytest.mark.parametrize("name", [
    "Liverpool Range Truck Parking Bay",
    "Heavy Vehicle Rest Area",
    "Heavy Vehicle Inspection Rest Area",
    "Gunnedah HV Rest Area",
    "B-Double Parking Bay",
    "Goodooga Truck Parking Bay Eastbound",
])
def test_a_rest_area_named_for_heavy_vehicles_is_a_candidate(name):
    places = {"ocdb_01M66CKP2X55A8CVGR95ZFJ1A0": record("ocdb_01M66CKP2X55A8CVGR95ZFJ1A0", name)}
    assert [c["id"] for c in name_candidates(places)] == ["ocdb_01M66CKP2X55A8CVGR95ZFJ1A0"]


@pytest.mark.parametrize("rec", [
    record("ocdb_01M66CKP2X55A8CVGR95ZFJ1A0", "Truck Stop", kind="fuel", group="services"),
    record("ocdb_01M66CKP2X55A8CVGR95ZFJ1A0", "Shiver Rest Area"),
    record("ocdb_01M66CKP2X55A8CVGR95ZFJ1A0", "Truck Parking Bay", facts={"vehicle_access": "heavy_vehicles_only"}),
    record("ocdb_01M66CKP2X55A8CVGR95ZFJ1A0", "Truck Parking Bay", status="merged", merged_into="ocdb_01M66CKP2X55A8CVGR95ZFJ1B0"),
    record("ocdb_01M66CKP2X55A8CVGR95ZFJ1A0", None),
])
def test_other_places_are_not_candidates(rec):
    assert name_candidates({rec["id"]: rec}) == []


def write(repo, rec):
    path = place_path(repo, rec["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_record(rec), encoding="utf-8", newline="\n")


def test_the_candidates_file_lists_id_name_and_location(repo):
    write(repo, record("ocdb_01M66CKP2X55A8CVGR95ZFJ1B0", "Big Ben Creek Truck Parking Bay"))
    write(repo, record("ocdb_01M66CKP2X55A8CVGR95ZFJ1A0", "Heavy Vehicle Rest Area"))
    write(repo, record("ocdb_01M66CKP2X55A8CVGR95ZFJ1C0", "Shiver Rest Area"))
    assert write_candidates(repo) == 2
    with open(repo / CANDIDATES_FILE, encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))
    assert rows == [
        ["place", "name", "location"],
        ["ocdb_01M66CKP2X55A8CVGR95ZFJ1A0", "Heavy Vehicle Rest Area", "-31.5,150.25"],
        ["ocdb_01M66CKP2X55A8CVGR95ZFJ1B0", "Big Ben Creek Truck Parking Bay", "-31.5,150.25"],
    ]
    assert validate_repo(repo, now=NOW) == []


def test_vehicle_access_fact_is_valid(repo):
    write(repo, record("ocdb_01M66CKP2X55A8CVGR95ZFJ1A0", "Truck Bay", facts={"vehicle_access": "heavy_vehicles_only"}))
    assert validate_repo(repo, now=NOW) == []


def test_unknown_vehicle_access_value_is_refused(repo):
    write(repo, record("ocdb_01M66CKP2X55A8CVGR95ZFJ1A0", "Truck Bay", facts={"vehicle_access": "cars_only"}))
    assert any("vehicle_access" in e for e in validate_repo(repo, now=NOW))
