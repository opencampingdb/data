"""Tests for pipeline.surroundings: the protected area, waterway and feature that can describe an unnamed place."""

from pathlib import Path

from pipeline.extract import extract_file

SURROUNDINGS = Path(__file__).parent / "fixtures" / "surroundings.osm"


def by_osm(records):
    return {(r["osm"]["type"], r["osm"]["id"]): r for r in records}


def facts_of(node_id):
    return by_osm(extract_file(SURROUNDINGS))[("node", node_id)]["facts"]


def test_protected_area_is_the_smallest_named_area_around_the_place():
    # Place A lies in a nature reserve, inside a national park, around an
    # unnamed protected area that does not count.
    assert facts_of(201)["protected_area"] == {
        "name": "Fixture Nature Reserve",
        "osm_id": 3001,
        "osm_type": "way",
    }


def test_a_forest_counts_only_with_state_forest_in_its_name():
    # The plantation around place B is smaller, but only the state forest counts.
    assert facts_of(202)["protected_area"] == {
        "name": "Fixture State Forest",
        "osm_id": 3020,
        "osm_type": "way",
    }


def test_a_national_park_drawn_as_a_boundary_relation():
    assert facts_of(205)["protected_area"] == {
        "name": "Fixture National Park",
        "osm_id": 5001,
        "osm_type": "relation",
    }


def test_a_place_outside_every_protected_area_has_none():
    assert "protected_area" not in facts_of(203)


def test_nearest_waterway_is_the_nearest_named_river_or_creek_within_200_m():
    # The creek is 93 m east of place A. A nearer unnamed stream and a named
    # drain do not count, and the river 280 m east is too far.
    assert facts_of(201)["nearest_waterway"] == {
        "name": "Black Bobs Creek",
        "osm_id": 3010,
        "distance_m": 90,
    }


def test_a_place_with_no_named_waterway_within_200_m_has_none():
    assert "nearest_waterway" not in facts_of(202)


def test_nearest_feature_is_a_named_natural_feature_within_300_m():
    # The peak is 222 m south of place A. The hamlet 111 m north comes only
    # when there is no natural feature, and the spring is 389 m away.
    assert facts_of(201)["nearest_feature"] == {
        "name": "Fixture Hill",
        "type": "peak",
        "osm_id": 301,
        "osm_type": "node",
        "distance_m": 220,
    }


def test_a_feature_area_around_the_place_is_0_m_away():
    # Place B lies in a lake, ahead of a peak 111 m away.
    assert facts_of(202)["nearest_feature"] == {
        "name": "Fixture Lake",
        "type": "water",
        "osm_id": 3022,
        "osm_type": "way",
        "distance_m": 0,
    }


def test_with_no_natural_feature_the_nearest_named_locality_within_1000_m():
    # The cave is 445 m from place C, too far, and an unnamed locality is nearer.
    assert facts_of(203)["nearest_feature"] == {
        "name": "Fixture Locality",
        "type": "locality",
        "osm_id": 305,
        "osm_type": "node",
        "distance_m": 560,
    }


def test_a_place_with_nothing_near_gets_none_of_the_facts():
    # The hamlet is 1,056 m from place D.
    assert not {"protected_area", "nearest_waterway", "nearest_feature"} & set(facts_of(204))


def test_the_facts_match_the_schema():
    import json

    from jsonschema import Draft202012Validator

    schema = json.loads((Path(__file__).parents[2] / "schema" / "place.schema.json").read_text(encoding="utf-8"))
    facts = schema["properties"]["facts"]["properties"]
    seen = 0
    for record in extract_file(SURROUNDINGS):
        for name in ("protected_area", "nearest_waterway", "nearest_feature"):
            if name in record["facts"]:
                assert list(Draft202012Validator(facts[name]).iter_errors(record["facts"][name])) == []
                seen += 1
    assert seen == 10


def test_counts_of_named_surroundings():
    stats = {}
    extract_file(SURROUNDINGS, stats)
    # Unnamed objects, the plantation and the drain are not counted.
    assert stats["surroundings"] == {
        "protected_areas": 3,
        "waterways": 2,
        "features": 5,
        "localities_and_hamlets": 3,
    }


def test_places_outside_the_overnight_group_get_the_facts_too():
    facts = facts_of(206)
    assert facts["protected_area"]["osm_id"] == 3001
    # The creek is 75 m east of the toilet, rounded half up.
    assert facts["nearest_waterway"] == {"name": "Black Bobs Creek", "osm_id": 3010, "distance_m": 80}
    assert facts["nearest_feature"]["osm_id"] == 301
