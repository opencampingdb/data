"""Tests for pipeline.roads: nearest_major_road, the distance to the nearest major road."""

from pathlib import Path

from pipeline.extract import extract_file

ROADS = Path(__file__).parent / "fixtures" / "roads.osm"


def by_id(records):
    return {r["osm"]["id"]: r for r in records}


def test_place_gets_the_distance_to_the_nearest_major_road():
    camp = by_id(extract_file(ROADS))[1]
    # The trunk road 193 m east wins over the secondary road 481 m west.
    assert camp["facts"]["nearest_major_road"] == {"distance_m": 190, "name": "Fixture Highway"}


def test_a_road_with_no_name_is_named_by_its_ref_and_minor_roads_do_not_count():
    rest_area = by_id(extract_file(ROADS))[2]
    # The residential street 56 m south is not a major road. The primary road
    # 334 m south has only a ref.
    assert rest_area["facts"]["nearest_major_road"] == {"distance_m": 330, "name": "B52"}


def test_a_one_way_tertiary_road_counts_and_a_two_way_one_does_not():
    camp = by_id(extract_file(ROADS))[3]
    # The two-way tertiary 22 m north is passed over. The one-way tertiary
    # 100 m south is unnamed, so the fact has no name.
    assert camp["facts"]["nearest_major_road"] == {"distance_m": 100}


def test_no_fact_beyond_the_radius():
    camp = by_id(extract_file(ROADS))[4]
    # The motorway is 512 m north, past the 500 m radius.
    assert "nearest_major_road" not in camp["facts"]


def test_major_road_count_is_in_the_stats():
    stats = {}
    extract_file(ROADS, stats)
    # Ways 501, 502, 512, 522 and 531.
    assert stats["major_roads"] == 5
