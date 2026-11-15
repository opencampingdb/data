"""Tests for the extract's name fallback: official_name, then loc_name, then alt_name."""

from pathlib import Path

from pipeline.common import load_places
from pipeline.extract import extract_file
from pipeline.sync import sync

OTHER_NAMES = Path(__file__).parent / "fixtures" / "other_names.osm"


def names():
    return {r["osm"]["id"]: r["name"] for r in extract_file(OTHER_NAMES)}


def test_official_name_is_used_trimmed():
    assert names()[1] == "Fixture Official Camping Area"


def test_loc_name_is_used():
    assert names()[2] == "Bobs Bend"


def test_alt_name_is_used_and_only_its_first_name():
    assert names()[3] == "Fixture Flat"


def test_official_name_comes_before_loc_name_and_alt_name():
    assert names()[4] == "Fixture Official"


def test_loc_name_comes_before_alt_name():
    assert names()[5] == "Fixture Local"


def test_old_short_operator_brand_signed_and_language_names_are_never_used():
    assert names()[6] is None


def test_a_name_tag_wins():
    assert names()[7] == "Fixture Named Camp"


def test_an_empty_value_is_passed_over():
    assert names()[8] == "Fixture Alt"


def test_the_name_source_stays_osm(repo):
    sync(repo, extract_file(OTHER_NAMES), now="2026-11-15T00:00:00Z")
    [record] = [r for r in load_places(repo).values() if r["osm"]["id"] == 2]
    # A missing name entry in fact_sources means the name comes from OSM.
    assert record["name"] == "Bobs Bend"
    assert "name" not in record["fact_sources"]
