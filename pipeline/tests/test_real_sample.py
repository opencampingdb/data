"""End-to-end run on real OSM data.

grampians-overpass.osm is an Overpass API download of the SPEC tags in a box
around the Grampians, Victoria (-37.5,142.2,-36.9,142.7), with the nodes of
every way included. OSM data, © OpenStreetMap contributors, ODbL 1.0.
"""

from pathlib import Path

from pipeline.extract import extract_file
from pipeline.release import build_release
from pipeline.sync import sync
from pipeline.validate import validate_repo

SAMPLE = Path(__file__).parent / "fixtures" / "grampians-overpass.osm"


def test_real_sample_end_to_end(repo, tmp_path):
    records = extract_file(SAMPLE)
    assert len(records) > 100
    kinds = {r["kind"] for r in records}
    assert {"caravan_park", "public_toilet", "drinking_water_point"} <= kinds
    assert kinds & {"free_camp", "paid_campground", "campground", "national_park_campsite"}
    assert any(r["osm"]["type"] == "way" for r in records)

    first = sync(repo, records, now="2026-10-30T16:00:00Z")
    assert len(first.new) == len(records)
    assert validate_repo(repo) == []

    second = sync(repo, records, now="2026-10-31T16:00:00Z")
    assert second.change_percent == 0

    manifest = build_release(repo, tmp_path / "dist", "20261030T160000Z-test", "2026-10-30T16:00:00Z")
    assert manifest["places"]["count"] == len(records)
