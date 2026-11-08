"""Tests for pipeline.water: the nearest beach and crocodile signs."""

from pathlib import Path

from shapely import LineString, Point, Polygon

from pipeline.extract import extract_file, facts_from_tags
from pipeline.water import WaterFeatures, crocodile_hazard, distance_m, water_kinds

WATER = Path(__file__).parent / "fixtures" / "water.osm"


def by_id(records):
    return {r["osm"]["id"]: r for r in records}


def test_water_kinds():
    assert water_kinds({"natural": "beach"}) == {"beach"}
    # Swim spots are places of their own, not water features.
    assert water_kinds({"leisure": "bathing_place"}) == set()
    assert water_kinds({"hazard": "crocodile"}) == {"crocodiles"}
    assert water_kinds({"hazard": "curves;crocodiles"}) == {"crocodiles"}
    assert water_kinds({"hazard:animal": "Crocodiles"}) == {"crocodiles"}
    # A pool is not a swim spot, and a kangaroo sign is not a crocodile.
    assert water_kinds({"leisure": "swimming_pool"}) == set()
    assert water_kinds({"hazard": "animal_crossing", "hazard:animal": "kangaroo"}) == set()


def test_crocodile_hazard():
    assert crocodile_hazard({"hazard": "crocodiles"})
    assert crocodile_hazard({"hazard": "dangerous_animal", "hazard:animal": "crocodile"})
    assert not crocodile_hazard({"hazard": "drowning"})
    assert not crocodile_hazard({})


def test_distance_to_a_point_a_line_and_an_area():
    # One thousandth of a degree of latitude is 111 m anywhere.
    assert distance_m(-16.5, 145.4, Point(145.4, -16.501)) == 111
    # Inside an area is 0.
    square = Polygon([(145.39, -16.51), (145.41, -16.51), (145.41, -16.49), (145.39, -16.49)])
    assert distance_m(-16.5, 145.4, square) == 0
    # A degree of longitude is shorter away from the equator: cos(16.5) of 111,195 m.
    line = LineString([(145.41, -16.6), (145.41, -16.4)])
    assert distance_m(-16.5, 145.4, line) == 1066


def test_nearest_picks_the_closest_within_the_radius():
    features = WaterFeatures()
    features.add({"beach"}, Point(145.4, -16.501), "Near")
    features.add({"beach"}, Point(145.4, -16.502), "Far")
    features.add({"crocodiles"}, Point(145.4, -16.6), None)
    assert features.nearest("beach", -16.5, 145.4, 5000) == (111, "Near")
    # 11 km away: none within 5 km.
    assert features.nearest("crocodiles", -16.5, 145.4, 5000) is None


def test_place_gets_its_nearest_beach():
    camp = by_id(extract_file(WATER))[1]
    # The beach area 320 m east wins over the open beach line 1,070 m west.
    assert camp["facts"]["nearest_beach"] == {"distance_m": 320, "name": "Fixture Main Beach"}


def test_distances_are_rounded_to_ten_metres():
    features = WaterFeatures()
    features.add({"beach"}, Point(145.4, -16.50225), None)  # 250.2 m
    place = {"location": {"lat": -16.5, "lon": 145.4}, "facts": {}}
    features.set_facts(place)
    assert place["facts"]["nearest_beach"] == {"distance_m": 250}


def test_water_feature_counts_include_the_open_beach_line():
    stats = {}
    extract_file(WATER, stats)
    # Beaches: way 201 (an area) and way 202 (an open line). Crocodile
    # hazards: nodes 131 and 132, and place 3, whose own tags carry one.
    assert stats["water"] == {"beach": 2, "crocodiles": 3}


def test_unnamed_feature_has_no_name():
    features = WaterFeatures()
    features.add({"beach"}, Point(145.4, -16.501), None)
    assert features.nearest("beach", -16.5, 145.4, 5000) == (111, None)
    place = {"location": {"lat": -16.5, "lon": 145.4}, "facts": {}}
    features.set_facts(place)
    assert place["facts"]["nearest_beach"] == {"distance_m": 110}


def test_place_with_nothing_near_has_no_water_facts():
    # The crocodile sign 1.5 km north is beyond a kilometre.
    rest = by_id(extract_file(WATER))[2]
    assert "nearest_beach" not in rest["facts"]
    assert "crocodiles" not in rest["facts"]


def test_crocodile_sign_within_a_kilometre_sets_crocodiles():
    records = by_id(extract_file(WATER))
    assert records[1]["facts"]["crocodiles"] is True


def test_place_own_hazard_tag_sets_crocodiles():
    assert facts_from_tags({"tourism": "camp_site", "hazard": "drowning;crocodiles"})["crocodiles"] is True
    assert by_id(extract_file(WATER))[3]["facts"]["crocodiles"] is True


def test_beaches_and_signs_are_not_places_and_swim_spots_are():
    records = by_id(extract_file(WATER))
    assert sorted(records) == [1, 2, 3, 121, 122]
    assert records[121]["kind"] == records[122]["kind"] == "swimming_spot"
