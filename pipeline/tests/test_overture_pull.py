"""Tests for the Overture pull tool's extract lines and extract format.

The pull itself reads the public bucket and never runs here.
"""

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).parent / "fixtures" / "overture-extract.ndjson"


def load_pull():
    spec = importlib.util.spec_from_file_location("overture_pull", ROOT / "tools" / "overture-pull" / "pull.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pull = load_pull()


def overture_row(**over):
    """One row as the DuckDB query returns it, with fields the extract must drop."""
    row = {
        "id": "08f8c3a0e2b1c0d103a4d1e5f6a7b8c9",
        "names": {"primary": "Lakeside Holiday Park", "common": None, "rules": None},
        "basic_category": "campground",
        "taxonomy": {"primary": "rv_park", "hierarchy": ["travel", "rv_park"], "alternates": ["campground"]},
        "confidence": 0.93,
        "websites": ["https://lakeside.example.au/"],
        "phones": ["+61 2 5550 1234"],
        "emails": ["bookings@lakeside.example.au"],
        "socials": ["https://www.facebook.com/lakeside"],
        "brand": {"names": {"primary": "Big Chain"}},
        "operating_status": "open",
        "addresses": [
            {"freeform": "1 Shore Rd", "locality": "Lakeside", "postcode": "2000", "region": "NSW", "country": "AU"}
        ],
        "sources": [
            {"property": "", "dataset": "meta", "license": "CDLA-Permissive-2.0", "record_id": "123",
             "update_time": "2026-09-01T00:00:00Z", "confidence": 0.9, "between": None},
        ],
        "bbox": {"xmin": 151.12345678, "xmax": 151.12345678, "ymin": -33.87654321, "ymax": -33.87654321},
    }
    row.update(over)
    return row


def test_a_row_keeps_only_the_fields_the_importer_uses():
    line = json.loads(pull.place_line(overture_row(), change="data_changed"))
    assert line == {
        "id": "08f8c3a0e2b1c0d103a4d1e5f6a7b8c9",
        "change": "data_changed",
        "name": "Lakeside Holiday Park",
        "basic_category": "campground",
        "taxonomy_primary": "rv_park",
        "confidence": 0.93,
        "websites": ["https://lakeside.example.au/"],
        "phones": ["+61 2 5550 1234"],
        "addresses": [
            {"freeform": "1 Shore Rd", "locality": "Lakeside", "postcode": "2000", "region": "NSW", "country": "AU"}
        ],
        "sources": [{"dataset": "meta", "record_id": "123", "update_time": "2026-09-01T00:00:00Z"}],
        "lon": 151.1234568,
        "lat": -33.8765432,
    }


def test_an_extract_has_a_header_then_every_place_and_removal_sorted_by_id(tmp_path):
    out = tmp_path / "extract.ndjson"
    rows = [overture_row(id="c3"), overture_row(id="a1")]
    changes = {"a1": "added", "c3": "unchanged", "b2": "removed", "zz": "data_changed"}
    pull.write_extract(out, release="2026-10-21.0", rows=rows, changes=changes)

    lines = [json.loads(text) for text in out.read_text(encoding="utf-8").splitlines()]
    assert lines[0] == {
        "extract": "overture-places",
        "version": 1,
        "release": "2026-10-21.0",
        "bbox": [112.0, -44.5, 154.5, -9.0],
        "places": 2,
        "removed": 1,
    }
    assert [(line["id"], line["change"]) for line in lines[1:]] == [
        ("a1", "added"),
        ("b2", "removed"),
        ("c3", "unchanged"),
    ]
    assert lines[2] == {"id": "b2", "change": "removed"}


def test_a_place_the_changelog_does_not_list_has_no_change():
    out_line = json.loads(pull.place_line(overture_row(), change=None))
    assert out_line["change"] is None


def test_a_row_with_no_name_or_contacts_keeps_every_key():
    row = overture_row(names=None, taxonomy=None, websites=None, phones=None, addresses=None, sources=None)
    line = json.loads(pull.place_line(row, change="added"))
    assert line["name"] is None
    assert line["taxonomy_primary"] is None
    assert (line["websites"], line["phones"], line["addresses"], line["sources"]) == ([], [], [], [])


def test_the_fixture_follows_the_extract_format():
    assert pull.check_extract(FIXTURE.read_text(encoding="utf-8").splitlines()) == []


def test_a_written_extract_follows_the_extract_format(tmp_path):
    out = tmp_path / "extract.ndjson"
    pull.write_extract(out, "2026-10-21.0", [overture_row(id="a1")], {"b2": "removed"})
    assert pull.check_extract(out.read_text(encoding="utf-8").splitlines()) == []


def test_the_format_check_finds_disorder_and_fields_never_taken():
    header = json.dumps({"extract": "overture-places", "version": 1, "release": "2026-10-21.0",
                         "bbox": [112.0, -44.5, 154.5, -9.0], "places": 1, "removed": 1})
    place = json.loads(pull.place_line(overture_row(id="b2"), change=None))
    place["emails"] = ["a@b.example"]
    problems = pull.check_extract([header, json.dumps(place), pull.removed_line("a1")])
    assert any("emails" in problem for problem in problems)
    assert any("order" in problem for problem in problems)
