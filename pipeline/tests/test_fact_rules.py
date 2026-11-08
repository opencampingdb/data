"""Tests for facts set by a documented rule (source "rule") in the release."""

import gzip
import json

import pytest
from jsonschema import Draft202012Validator

from pipeline.common import SCHEMA_DIR, dump_record, place_path
from pipeline.release import build_release
from pipeline.validate import validate_repo

VERSION = "20261030T160000Z-abc1234"
GENERATED = "2026-10-30T16:00:00Z"
PLACE_ID = "ocdb_01M66CKP2X55A8CVGR95ZFJ170"


def place(kind, group="utility", facts=None, sources=None, dates=None, **changes):
    facts = facts or {}
    record = {
        "id": PLACE_ID,
        "name": "Example Place",
        "kind": kind,
        "group": group,
        "location": {"lat": -37.107745, "lon": 142.423232},
        "osm": {"type": "node", "id": 316599210, "version": 5},
        "facts": facts,
        "fact_sources": sources if sources is not None else {k: "osm" for k in facts},
        "status": "open",
        "rating": None,
        "updated": "2026-10-30T16:00:00Z",
    }
    if dates:
        record["fact_dates"] = dates
    record.update(changes)
    return record


def write(repo, record):
    path = place_path(repo, record["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_record(record), encoding="utf-8", newline="\n")


def released(repo, tmp_path, name="places.ndjson.gz"):
    out = tmp_path / "dist"
    build_release(repo, out, data_version=VERSION, generated=GENERATED)
    lines = gzip.decompress((out / name).read_bytes()).decode("utf-8").splitlines()
    return [json.loads(line) for line in lines]


@pytest.mark.parametrize("kind,fact", [
    ("public_toilet", "toilets"),
    ("drinking_water_point", "drinking_water"),
    ("public_shower", "showers"),
    ("dump_point", "dump_station"),
])
def test_a_kind_records_its_own_feature_by_rule(repo, tmp_path, kind, fact):
    write(repo, place(kind))
    [record] = released(repo, tmp_path)
    assert record["facts"][fact] is True
    assert record["fact_sources"][fact] == "rule"
    assert record["fact_rules"] == {fact: f"kind:{kind}"}
    assert "fact_dates" not in record


def test_a_national_park_campsite_has_no_dogs_by_rule(repo, tmp_path):
    write(repo, place("national_park_campsite", group="overnight", facts={"toilets": True}))
    [record] = released(repo, tmp_path)
    assert record["facts"] == {"toilets": True, "pets": "no"}
    assert record["fact_sources"] == {"toilets": "osm", "pets": "rule"}
    assert record["fact_rules"] == {"pets": "national_park"}


@pytest.mark.parametrize("kind", ["state_forest_campsite", "free_camp", "campground"])
def test_other_places_to_stay_get_no_dog_rule(repo, tmp_path, kind):
    write(repo, place(kind, group="overnight"))
    [record] = released(repo, tmp_path)
    assert "pets" not in record["facts"]
    assert "fact_rules" not in record


def test_an_osm_value_wins_over_a_rule(repo, tmp_path):
    write(repo, place("public_toilet", facts={"toilets": False}))
    [record] = released(repo, tmp_path)
    assert record["facts"]["toilets"] is False
    assert record["fact_sources"]["toilets"] == "osm"
    assert "fact_rules" not in record


def test_a_reported_value_wins_over_a_rule(repo, tmp_path):
    write(repo, place(
        "national_park_campsite", group="overnight",
        facts={"pets": "leashed"}, sources={"pets": "feedback"},
        dates={"pets": "2026-10-01T00:00:00Z"},
    ))
    [record] = released(repo, tmp_path)
    assert record["facts"]["pets"] == "leashed"
    assert record["fact_sources"]["pets"] == "feedback"
    assert "fact_rules" not in record


def test_the_place_index_carries_rule_facts(repo, tmp_path):
    write(repo, place("dump_point"))
    [line] = released(repo, tmp_path, "place-index.ndjson.gz")
    assert line["facts"] == {"dump_station": True}
    assert line["fact_sources"] == {"dump_station": "rule"}
    assert line["fact_dates"] == {}


def test_the_repository_record_is_not_changed(repo, tmp_path):
    record = place("public_toilet")
    write(repo, record)
    released(repo, tmp_path)
    assert json.loads(place_path(repo, PLACE_ID).read_text(encoding="utf-8")) == record


def test_a_released_record_with_rules_matches_the_schema(repo, tmp_path):
    write(repo, place("national_park_campsite", group="overnight"))
    [record] = released(repo, tmp_path)
    schema = json.loads((SCHEMA_DIR / "place.schema.json").read_text(encoding="utf-8"))
    assert list(Draft202012Validator(schema).iter_errors(record)) == []


def test_the_manifest_is_format_12(repo, tmp_path):
    out = tmp_path / "dist"
    manifest = build_release(repo, out, data_version=VERSION, generated=GENERATED)
    assert (manifest["format_version"], manifest["format_minor"]) == (12, 3)


def test_a_rule_fact_in_a_repository_file_is_refused(repo):
    write(repo, place(
        "public_toilet", facts={"toilets": True}, sources={"toilets": "rule"},
        fact_rules={"toilets": "kind:public_toilet"},
    ))
    errors = validate_repo(repo, now="2026-11-01T00:00:00Z")
    assert any("rule" in e and "release" in e for e in errors), errors


def test_the_rules_file_lists_every_rule():
    rules = json.loads((SCHEMA_DIR / "fact_rules.json").read_text(encoding="utf-8"))["rules"]
    assert {(r["rule"], r["kind"], r["fact"], json.dumps(r["value"])) for r in rules} == {
        ("kind:public_toilet", "public_toilet", "toilets", "true"),
        ("kind:drinking_water_point", "drinking_water_point", "drinking_water", "true"),
        ("kind:public_shower", "public_shower", "showers", "true"),
        ("kind:dump_point", "dump_point", "dump_station", "true"),
        ("national_park", "national_park_campsite", "pets", '"no"'),
    }


def test_the_schema_names_the_same_rules_as_the_rules_file():
    schema = json.loads((SCHEMA_DIR / "place.schema.json").read_text(encoding="utf-8"))
    rules = json.loads((SCHEMA_DIR / "fact_rules.json").read_text(encoding="utf-8"))["rules"]
    [(pattern, allowed)] = schema["properties"]["fact_rules"]["patternProperties"].items()
    assert sorted(allowed["enum"]) == sorted(r["rule"] for r in rules)
    assert set(pattern.strip("^$()").split("|")) == {r["fact"] for r in rules}

