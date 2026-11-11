"""Tests for pipeline.nearby: a pub, fuel or car wash inside a place, and the nearest boat ramp."""

from pathlib import Path

import shapely

from pipeline.extract import extract_file
from pipeline.nearby import InsideFeatures

NEARBY = Path(__file__).parent / "fixtures" / "nearby.osm"


def by_osm(records):
    return {(r["osm"]["type"], r["osm"]["id"]): r for r in records}


def test_caravan_park_area_gets_the_pub_fuel_and_car_wash_inside_it():
    park = by_osm(extract_file(NEARBY))[("way", 2001)]
    assert park["facts"]["pub"] is True
    assert park["facts"]["fuel"] is True
    assert park["facts"]["car_wash"] is True


def test_node_only_camp_site_gets_no_pub_fuel_or_car_wash():
    # It lies inside the caravan park beside them, but a node has no area.
    camp = by_osm(extract_file(NEARBY))[("node", 103)]
    assert not {"pub", "fuel", "car_wash"} & set(camp["facts"])


def test_an_object_never_counts_for_itself():
    # The fuel station's own centroid lies inside its own area.
    station = by_osm(extract_file(NEARBY))[("way", 2002)]
    assert "fuel" not in station["facts"]


def test_multipolygon_place_gets_a_car_wash_drawn_as_an_area():
    camp = by_osm(extract_file(NEARBY))[("relation", 4001)]
    assert camp["facts"]["car_wash"] is True
    assert "pub" not in camp["facts"]


def test_pub_outside_the_area_does_not_count():
    # Pub 104 lies 1.1 km north of the caravan park, so only the park has a pub.
    records = extract_file(NEARBY)
    assert [r["osm"]["id"] for r in records if r["facts"].get("pub") is True] == [2001]


def test_a_place_keeps_its_own_pub_fuel_and_car_wash_values():
    # No OSM tag on the place sets these, so the record carries them in from elsewhere.
    inside = InsideFeatures()
    inside.add("pub", -35.0005, 145.0005, ("node", 1))
    inside.add("fuel", -35.0005, 145.0005, ("node", 2))
    inside.add("car_wash", -35.0005, 145.0005, ("node", 3))
    record = {"facts": {"pub": False, "fuel": False, "car_wash": False}}
    inside.set_facts(record, shapely.box(145.0, -35.001, 145.001, -35.0), ("way", 9))
    assert record["facts"] == {"pub": False, "fuel": False, "car_wash": False}


def test_nearest_boat_ramp_within_five_kilometres():
    records = by_osm(extract_file(NEARBY))
    # Ramp 106 is 3,002 m south of the park's centroid. Ramp 2006 is 6 km away.
    assert records[("way", 2001)]["facts"]["nearest_boat_ramp"] == {
        "distance_m": 3000,
        "name": "Fixture Boat Ramp",
    }
    # The node camp is 93 m east of the centroid: 3,004 m, rounded to 3,000.
    assert records[("node", 103)]["facts"]["nearest_boat_ramp"] == {
        "distance_m": 3000,
        "name": "Fixture Boat Ramp",
    }


def test_place_with_no_boat_ramp_within_five_kilometres_has_none():
    camp = by_osm(extract_file(NEARBY))[("way", 2003)]
    assert "nearest_boat_ramp" not in camp["facts"]


def test_a_boat_ramp_does_not_count_for_itself():
    records = by_osm(extract_file(NEARBY))
    ramp, line = records[("node", 106)], records[("way", 2006)]
    assert ramp["kind"] == line["kind"] == "boat_ramp"
    # Each gets the other, 3,002 m away, never itself at 0. The line has no name.
    assert ramp["facts"]["nearest_boat_ramp"] == {"distance_m": 3000}
    assert line["facts"]["nearest_boat_ramp"] == {"distance_m": 3000, "name": "Fixture Boat Ramp"}


def test_counts_of_nearby_features():
    stats = {}
    extract_file(NEARBY, stats)
    # Pubs 101 and 104; fuel station 2002; car washes 102 and 2005.
    assert stats["inside"] == {"pub": 2, "fuel": 1, "car_wash": 2}
    assert stats["water"]["boat_ramp"] == 2
