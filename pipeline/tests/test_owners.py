"""Tests for owner fields: farm stays, descriptions, temporary closures and owner sources."""

import json

import pytest

from pipeline.common import KIND_GROUP, KINDS, dump_record, place_path
from pipeline.extract import extract_file
from pipeline.sync import sync
from pipeline.validate import validate_repo

T1 = "2026-10-01T16:00:00Z"
T2 = "2026-10-02T16:00:00Z"
FARM_ID = "ocdb_01JBX0000000000000000000F1"


def farm_stay(**changes):
    record = {
        "id": FARM_ID,
        "name": "Fixture Hill Farm Stay",
        "kind": "farm_stay",
        "group": "overnight",
        "location": {"lat": -36.5, "lon": 146.5},
        "osm": None,
        "facts": {
            "entry": "private",
            "booking_required": True,
            "booking_url": "https://example.org/book",
            "fee": True,
        },
        "status": "open",
        "rating": None,
        "fact_sources": {
            "entry": "owner",
            "booking_required": "owner",
            "booking_url": "owner",
            "fee": "owner",
            "name": "owner",
            "location": "owner",
        },
        "owner_verified": True,
        "description": "Sheep farm with room for six vans by the dam. Book ahead.",
        "updated": T1,
    }
    record.update(changes)
    return record


def write(repo, record):
    path = place_path(repo, record["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_record(record), encoding="utf-8", newline="\n")
    return path


def errors_for(repo, record):
    write(repo, record)
    return validate_repo(repo)


def test_farm_stay_is_an_overnight_extension():
    assert "farm_stay" in KINDS
    assert KIND_GROUP["farm_stay"] == "overnight"


def test_valid_farm_stay_passes(repo):
    assert errors_for(repo, farm_stay()) == []


def test_farm_stay_with_phone_instead_of_booking_url_passes(repo):
    record = farm_stay()
    del record["facts"]["booking_url"]
    del record["fact_sources"]["booking_url"]
    record["facts"]["phone"] = "+61 3 5555 0101"
    record["fact_sources"]["phone"] = "owner"
    assert errors_for(repo, record) == []


@pytest.mark.parametrize(
    "change, expected",
    [
        ({"osm": {"type": "node", "id": 1, "version": 1}}, "farm_stay"),
        ({"facts": {"entry": "public", "booking_required": True, "booking_url": "https://example.org/book", "fee": True}}, "private"),
        ({"facts": {"entry": "private", "booking_url": "https://example.org/book", "fee": True}}, "booking_required"),
        ({"facts": {"entry": "private", "booking_required": True, "fee": True}}, "booking_url"),
    ],
)
def test_farm_stay_rules(repo, change, expected):
    record = farm_stay(**change)
    record["fact_sources"] = {
        k: "owner" for k in list(record["facts"]) + ["name", "location"]
    }
    errors = errors_for(repo, record)
    assert any(expected in e for e in errors), errors


def test_booking_url_must_be_https(repo):
    record = farm_stay()
    record["facts"]["booking_url"] = "http://example.org/book"
    assert any("booking_url" in e for e in errors_for(repo, record))


def test_description_limits(repo):
    assert any("description" in e for e in errors_for(repo, farm_stay(description="x" * 501)))
    assert errors_for(repo, farm_stay(description="x" * 500)) == []


@pytest.mark.parametrize(
    "text",
    [
        "Email bookings@example.org to stay",
        "Ring 0412 345 678 before you arrive",
        "See https://example.org for prices",
    ],
)
def test_description_is_moderated_like_review_text(repo, text):
    errors = errors_for(repo, farm_stay(description=text))
    assert any("description" in e for e in errors), errors


def test_temporarily_closed_with_reopens(repo):
    record = farm_stay(status="temporarily_closed", reopens="2027-01-15")
    record["fact_sources"]["status"] = "owner"
    assert errors_for(repo, record) == []


def test_reopens_only_with_temporarily_closed(repo):
    assert any("reopens" in e for e in errors_for(repo, farm_stay(reopens="2027-01-15")))


def test_reopens_must_be_a_real_date(repo):
    record = farm_stay(status="temporarily_closed", reopens="2027-02-30")
    assert any("reopens" in e for e in errors_for(repo, record))


def test_owner_source_needs_a_verified_owner(repo):
    record = farm_stay(owner_verified=False)
    assert any("owner_verified" in e for e in errors_for(repo, record))
    record = farm_stay()
    del record["owner_verified"]
    assert any("owner_verified" in e for e in errors_for(repo, record))


def test_sync_never_overwrites_owner_values(repo, v1_osm, v2_osm):
    sync(repo, extract_file(v1_osm), now=T1)
    camp = next(
        json.loads(p.read_text(encoding="utf-8"))
        for p in (repo / "places").rglob("*.json")
        if json.loads(p.read_text(encoding="utf-8"))["osm"]["id"] == 1001
    )
    path = place_path(repo, camp["id"])
    camp["owner_verified"] = True
    camp["name"] = "Fixture Creek Camp (owner name)"
    camp["fact_sources"]["name"] = "owner"
    camp["facts"]["fee"] = False  # v2 says fee=yes
    camp["fact_sources"]["fee"] = "owner"
    camp["facts"]["booking_url"] = "https://example.org/camp/book"
    camp["fact_sources"]["booking_url"] = "owner"
    camp["status"] = "temporarily_closed"
    camp["reopens"] = "2027-03-01"
    camp["fact_sources"]["status"] = "owner"
    camp["description"] = "Creek-side camp. Closed while the bridge is rebuilt."
    camp["fact_sources"]["description"] = "owner"
    path.write_text(dump_record(camp), encoding="utf-8", newline="\n")
    assert validate_repo(repo) == []

    sync(repo, extract_file(v2_osm), now=T2)
    after = json.loads(path.read_text(encoding="utf-8"))
    for key in ("name", "status", "reopens", "description", "owner_verified"):
        assert after[key] == camp[key], key
    assert after["facts"]["fee"] is False
    assert after["fact_sources"]["fee"] == "owner"
    assert after["facts"]["booking_url"] == "https://example.org/camp/book"
    assert validate_repo(repo) == []
