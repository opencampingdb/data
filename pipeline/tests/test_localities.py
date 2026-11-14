"""Tests for pipeline.localities: locality, lga and the localities file."""

from pathlib import Path

import pytest

from pipeline.extract import extract_file, main
from pipeline.localities import Localities, great_circle_m, load_localities, parse_population, round_10
from pipeline.validate import check_localities

LOCALITIES = Path(__file__).parent / "fixtures" / "localities.osm"


def by_id(records):
    return {r["osm"]["id"]: r for r in records}


def test_place_gets_the_nearest_town_and_its_council_area():
    camp = by_id(extract_file(LOCALITIES))[1]
    # Example Town is 0.05 degrees south and east: about 7,354 m. The hamlet
    # and the unnamed town beside the camp do not count.
    assert camp["facts"]["locality"] == {"name": "Example Town", "osm_id": 101, "type": "town", "distance_m": 7350}
    # The admin_level 7 ward over the camp, with a lower id, does not count.
    assert camp["facts"]["lga"] == {"name": "Example Shire", "osm_id": 900}


def test_no_town_beyond_50_km_and_no_council_area_outside_every_boundary():
    rest_area = by_id(extract_file(LOCALITIES))[2]
    # Example Town is 0.6 degrees north (about 66.7 km), Far City further.
    assert "locality" not in rest_area["facts"]
    assert "lga" not in rest_area["facts"]


def test_a_tie_goes_to_the_lower_osm_id():
    camp = by_id(extract_file(LOCALITIES))[3]
    assert camp["facts"]["locality"]["osm_id"] == 101


def test_counts_are_in_the_stats():
    stats = {}
    extract_file(LOCALITIES, stats)
    assert stats["localities"] == {"localities": 3, "lgas": 1}


def test_rows_list_every_town_with_its_council_area():
    localities = Localities()
    extract_file(LOCALITIES, localities=localities)
    assert localities.rows() == [
        {"osm_id": 101, "name": "Example Town", "type": "town", "lat": -30.2, "lon": 150.2, "population": 1234,
         "lga": {"name": "Example Shire", "osm_id": 900}},
        # "about 300" is not a number, so no population.
        {"osm_id": 102, "name": "Example Village", "type": "village", "lat": -30.2, "lon": 150.45,
         "lga": {"name": "Example Shire", "osm_id": 900}},
        {"osm_id": 105, "name": "Far City", "type": "city", "lat": -31.5, "lon": 151.0},
    ]


def test_the_extract_writes_a_valid_localities_file(tmp_path):
    root = tmp_path / "repo"
    out = root / "localities" / "localities.ndjson"
    assert main([str(LOCALITIES), "--out", str(tmp_path / "extract.ndjson"), "--localities", str(out)]) == 0
    assert [row["osm_id"] for row in load_localities(root)] == [101, 102, 105]
    assert check_localities(root) == []


def test_a_bad_localities_file_is_refused(tmp_path):
    path = tmp_path / "localities" / "localities.ndjson"
    path.parent.mkdir()
    path.write_text(
        '{"lat":-30.2,"lon":150.2,"name":"B","osm_id":5,"type":"town"}\n'
        '{"lat":-30.2,"lon":150.2,"name":"A","osm_id":4,"type":"hamlet"}\n'
        '{"lat":-30.2,"lon":150.2,"name":"C","osm_id":3,"type":"town"}\n',
        encoding="utf-8",
    )
    errors = check_localities(tmp_path)
    assert any("line 2" in e and "hamlet" in e for e in errors), errors
    assert any("line 3" in e and "sorted by osm_id" in e for e in errors), errors


def test_no_localities_file_is_fine(tmp_path):
    assert check_localities(tmp_path) == []
    assert load_localities(tmp_path) == []


@pytest.mark.parametrize("value, expected", [
    ("1234", 1234), ("1,234", 1234), ("12 345", 12345), (None, None), ("", None), ("about 300", None), ("1.5k", None),
])
def test_population(value, expected):
    assert parse_population(value) == expected


def test_distance_on_the_sphere():
    # One degree of latitude on the sphere of mean radius 6,371,008.8 m.
    assert round(great_circle_m(-30.0, 150.0, -31.0, 150.0)) == 111_195
    assert round_10(7354.4) == 7350
    assert round_10(7355) == 7360
