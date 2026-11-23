"""Tests for the dates the release gives values from OSM (fact_dates from osm.timestamp)."""

import gzip
import json

from pipeline.common import dump_record, place_path
from pipeline.common import dump_line
from pipeline.release import build_release
from pipeline.validate import main as validate_main, validate_release

VERSION = "20261030T160000Z-abc1234"
GENERATED = "2026-10-30T16:00:00Z"
PLACE_ID = "ocdb_01M66CKP2X55A8CVGR95ZFJ170"
EDITED = "2025-04-12T03:15:00Z"


def place(osm=None, facts=None, sources=None, dates=None, kind="free_camp", group="overnight"):
    facts = facts if facts is not None else {"fee": False, "toilets": True}
    record = {
        "id": PLACE_ID,
        "name": "Example Camp",
        "kind": kind,
        "group": group,
        "location": {"lat": -37.107745, "lon": 142.423232},
        "osm": osm if osm is not None else {"type": "node", "id": 316599210, "version": 5, "timestamp": EDITED},
        "facts": facts,
        "fact_sources": sources if sources is not None else {k: "osm" for k in facts},
        "status": "open",
        "rating": None,
        "updated": "2026-10-30T16:00:00Z",
    }
    if dates:
        record["fact_dates"] = dates
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


def test_a_released_osm_value_is_dated_when_its_object_was_last_edited(repo, tmp_path):
    write(repo, place())
    [record] = released(repo, tmp_path)
    assert record["fact_dates"] == {"fee": EDITED, "toilets": EDITED}


def test_an_osm_value_is_undated_when_the_extract_had_no_timestamp(repo, tmp_path):
    write(repo, place(osm={"type": "node", "id": 316599210, "version": 5}))
    [record] = released(repo, tmp_path)
    assert "fact_dates" not in record


def test_values_from_other_sources_keep_their_own_dates_and_rules_get_none(repo, tmp_path):
    write(repo, place(
        kind="public_toilet",
        group="utility",
        facts={"fee": False, "website": "https://example.org"},
        sources={"fee": "osm", "website": "feedback"},
        dates={"website": "2026-09-01T10:00:00Z"},
    ))
    [record] = released(repo, tmp_path)
    assert record["fact_sources"]["toilets"] == "rule"
    assert record["fact_dates"] == {"fee": EDITED, "website": "2026-09-01T10:00:00Z"}


def test_a_name_entry_from_osm_is_dated_too(repo, tmp_path):
    write(repo, place(facts={"fee": False}, sources={"fee": "osm", "name": "osm"}))
    [record] = released(repo, tmp_path)
    assert record["fact_dates"] == {"fee": EDITED, "name": EDITED}


def test_the_place_index_and_the_repository_file_hold_no_osm_dates(repo, tmp_path):
    record = place()
    write(repo, record)
    [line] = released(repo, tmp_path, name="place-index.ndjson.gz")
    assert line["fact_dates"] == {}
    assert place_path(repo, PLACE_ID).read_text(encoding="utf-8") == dump_record(record)


def write_places_file(out, records):
    out.mkdir(parents=True, exist_ok=True)
    raw = "".join(dump_line(record) + "\n" for record in records).encode("utf-8")
    (out / "places.ndjson.gz").write_bytes(gzip.compress(raw))


def test_a_built_release_passes_release_validation(repo, tmp_path):
    write(repo, place())
    out = tmp_path / "dist"
    build_release(repo, out, data_version=VERSION, generated=GENERATED)
    assert validate_release(out) == []
    assert validate_main(["--root", str(repo), "--release-dir", str(out)]) == 0


def test_release_validation_refuses_an_undated_osm_value(tmp_path):
    record = place(dates={"toilets": EDITED})
    write_places_file(tmp_path / "dist", [record])
    [error] = validate_release(tmp_path / "dist")
    assert PLACE_ID in error and "['fee']" in error


def test_release_validation_refuses_an_osm_date_other_than_the_edit_time(tmp_path):
    record = place(dates={"fee": EDITED, "toilets": "2026-01-01T00:00:00Z"})
    write_places_file(tmp_path / "dist", [record])
    [error] = validate_release(tmp_path / "dist")
    assert PLACE_ID in error and "osm.timestamp" in error and "['toilets']" in error


def test_release_validation_refuses_an_osm_date_without_an_edit_time(tmp_path):
    record = place(osm={"type": "node", "id": 316599210, "version": 5}, dates={"fee": EDITED})
    write_places_file(tmp_path / "dist", [record])
    [error] = validate_release(tmp_path / "dist")
    assert "['fee']" in error


def test_release_validation_checks_the_schema(tmp_path):
    record = place(dates={"fee": "last week", "toilets": EDITED})
    write_places_file(tmp_path / "dist", [record])
    errors = validate_release(tmp_path / "dist")
    assert any("last week" in error for error in errors)


def test_the_manifest_is_format_13_4(repo, tmp_path):
    manifest = build_release(repo, tmp_path / "dist", data_version=VERSION, generated=GENERATED)
    assert (manifest["format_version"], manifest["format_minor"]) == (13, 5)
