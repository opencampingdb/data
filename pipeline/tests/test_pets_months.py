"""Tests for pets_months, the months a seasonal place allows dogs."""

import json

import pytest

from pipeline.common import dump_record, place_path
from pipeline.extract import facts_from_tags
from pipeline.sync import sync
from pipeline.validate import validate_repo

NOW = "2026-11-01T00:00:00Z"


@pytest.mark.parametrize("conditional,months", [
    ("yes @ (May-Sep)", "May-Sep"),
    ("yes @ (Dec-Feb)", "Dec-Feb"),
    ("yes @ Jun", "Jun"),
    ("yes @ (Apr-May,Sep-Oct)", "Apr-May,Sep-Oct"),
    ("yes @ (may-sep)", "May-Sep"),
])
def test_dog_conditional_with_months_makes_a_seasonal_place(conditional, months):
    facts = facts_from_tags({"dog": "no", "dog:conditional": conditional})
    assert facts["pets"] == "seasonal"
    assert facts["pets_months"] == months


def test_dog_conditional_without_a_dog_tag_counts_too():
    assert facts_from_tags({"dog:conditional": "yes @ (May-Sep)"}) == {"pets": "seasonal", "pets_months": "May-Sep"}


@pytest.mark.parametrize("tags", [
    {"dog": "leashed", "dog:conditional": "unleashed @ (15:30-10:30)"},
    {"dog": "no", "dog:conditional": "yes @ (May-Sep 08:00-17:00)"},
    {"dog": "no", "dog:conditional": "yes @ (Apr Su[1]-Oct Su[1])"},
    {"dog": "no", "dog:conditional": "yes @ (May-Sep); leashed @ (Oct)"},
    {"dog": "yes", "dog:conditional": "yes @ (May-Sep)"},
    {"dog": "no", "dog:conditional": "no @ (Dec-Feb)"},
])
def test_a_condition_that_is_not_plain_months_is_left_as_it_was(tags):
    facts = facts_from_tags(tags)
    assert "pets_months" not in facts
    assert facts.get("pets") == ({"yes": "yes", "no": "no", "leashed": "leashed"}.get(tags.get("dog")))


def record(facts, sources):
    return {
        "id": "ocdb_01M66CKP2X55A8CVGR95ZFJ170",
        "name": "Example Park Campground",
        "kind": "national_park_campsite",
        "group": "overnight",
        "location": {"lat": -37.107745, "lon": 142.423232},
        "osm": {"type": "node", "id": 316599210, "version": 5},
        "facts": facts,
        "fact_sources": sources,
        "status": "open",
        "rating": None,
        "updated": "2026-10-30T16:00:00Z",
    }


def write(repo, rec):
    path = place_path(repo, rec["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_record(rec), encoding="utf-8", newline="\n")


def test_pets_months_with_seasonal_pets_is_valid(repo):
    write(repo, record({"pets": "seasonal", "pets_months": "Dec-Feb"}, {"pets": "osm", "pets_months": "osm"}))
    assert validate_repo(repo, now=NOW) == []


@pytest.mark.parametrize("value", ["May to Sep", "may-sep", "Sept", "May-", "May-Sep;Dec", ""])
def test_pets_months_must_be_osm_months(repo, value):
    write(repo, record({"pets": "seasonal", "pets_months": value}, {"pets": "osm", "pets_months": "osm"}))
    assert any("pets_months" in e for e in validate_repo(repo, now=NOW))


def test_pets_months_without_seasonal_pets_is_refused(repo):
    write(repo, record({"pets": "no", "pets_months": "May-Sep"}, {"pets": "osm", "pets_months": "osm"}))
    errors = validate_repo(repo, now=NOW)
    assert any("pets_months" in e and "seasonal" in e for e in errors), errors


def test_sync_drops_osm_months_when_a_traveller_set_other_pets(repo):
    write(repo, dict(
        record({"pets": "yes"}, {"pets": "feedback"}),
        fact_dates={"pets": "2026-10-01T00:00:00Z"},
    ))
    extract = [{
        "osm": {"type": "node", "id": 316599210, "version": 6},
        "name": "Example Park Campground",
        "kind": "national_park_campsite",
        "location": {"lat": -37.107745, "lon": 142.423232},
        "facts": {"pets": "seasonal", "pets_months": "May-Sep"},
    }]
    sync(repo, extract, now=NOW)
    synced = json.loads(place_path(repo, "ocdb_01M66CKP2X55A8CVGR95ZFJ170").read_text(encoding="utf-8"))
    assert synced["facts"] == {"pets": "yes"}
    assert validate_repo(repo, now=NOW) == []
