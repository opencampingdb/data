"""Tests for pipeline.states: which state or territory a point is in, from OSM's outlines."""

import json

import pytest

from pipeline.states import BOUNDARIES, NO_REF_CODES, StateLookup, code_of, from_overpass


@pytest.fixture(scope="module")
def lookup():
    return StateLookup()


@pytest.mark.parametrize(
    ("lon", "lat", "state"),
    [
        (151.2153, -33.8568, "NSW"),  # Sydney Harbour
        (150.6721, -35.0389, "NSW"),  # Huskisson, on Jervis Bay
        (159.0800, -31.5500, "NSW"),  # Lord Howe Island
        (144.9631, -37.8136, "VIC"),  # Melbourne
        (153.0251, -27.4698, "QLD"),  # Brisbane
        (138.6007, -34.9285, "SA"),  # Adelaide
        (115.8605, -31.9505, "WA"),  # Perth
        (147.3272, -42.8821, "TAS"),  # Hobart
        (130.8456, -12.4634, "NT"),  # Darwin
        (149.1244, -35.3081, "ACT"),  # Canberra
        (150.7290, -35.1456, "JBT"),  # Green Patch, Booderee National Park
        (105.6275, -10.4907, "CX"),  # Flying Fish Cove, Christmas Island
        (96.8256, -12.1872, "CC"),  # West Island, Cocos (Keeling) Islands
        (73.5000, -53.1000, "HIMI"),  # Heard Island
        (167.9547, -29.0408, "NF"),  # Kingston, Norfolk Island
        (149.9650, -16.2880, "CR"),  # Willis Island, Coral Sea Islands
        (123.5500, -12.5300, "AT"),  # Cartier Island
    ],
)
def test_known_points(lookup, lon, lat, state):
    assert lookup.state_of(lon, lat) == state


def test_coastal_waters_are_inside_the_outlines(lookup):
    # OSM's outlines take in the coastal waters: about 2 km east of the
    # cliffs at the entrance to Sydney Harbour is in NSW, with no snapping.
    assert lookup.state_of(151.31, -33.835) == "NSW"


@pytest.mark.parametrize(
    ("lon", "lat"),
    [
        (145.808595, -16.005679),  # Agincourt Reef, outer Great Barrier Reef
        (145.663465, -14.663675),  # the Cod Hole
        (151.40, -33.85),  # about 10 km off Sydney
        (155.0, -35.0),  # the Tasman Sea
        (174.7633, -36.8485),  # Auckland, not Australia
    ],
)
def test_outside_every_outline_is_in_no_state(lookup, lon, lat):
    assert lookup.state_of(lon, lat) is None


def test_the_file_has_one_outline_per_code_with_no_ot():
    features = json.loads(BOUNDARIES.read_text(encoding="utf-8"))["features"]
    codes = sorted(feature["properties"]["state"] for feature in features)
    assert codes == sorted([
        "NSW", "VIC", "QLD", "SA", "WA", "TAS", "NT", "ACT", "JBT", "CX", "CC", "HIMI", "NF", "CR", "AT",
    ])
    assert {feature["properties"]["osm_relation"] for feature in features} >= {2357330, *NO_REF_CODES}


def test_codes_come_from_the_ref_tag_and_the_table_only_where_osm_has_none():
    assert code_of({"id": 2357330, "tags": {"ref": "JBT"}}) == "JBT"
    assert code_of({"id": 2574988, "tags": {"name": "Norfolk Island"}}) == "NF"
    assert code_of({"id": 1, "tags": {"name": "Somewhere"}}) is None


def test_from_overpass_builds_outlines_with_holes(tmp_path):
    square = [(0, 0), (4, 0), (4, 4), (0, 4), (0, 0)]
    hole = [(1, 1), (2, 1), (2, 2), (1, 2), (1, 1)]

    def way(coords, role):
        return {"type": "way", "role": role, "geometry": [{"lon": x, "lat": y} for x, y in coords]}

    response = {"elements": [{
        "type": "relation", "id": 7, "tags": {"ref": "XX", "name": "Test"},
        "members": [way(square[:3], "outer"), way(square[2:], "outer"), way(hole, "inner")],
    }]}
    collection = from_overpass(response)
    path = tmp_path / "outlines.geojson"
    path.write_text(json.dumps(collection), encoding="utf-8")
    lookup = StateLookup(path)
    assert lookup.state_of(3, 3) == "XX"
    assert lookup.state_of(1.5, 1.5) is None


def test_states_of_matches_state_of(lookup):
    points = [(151.2153, -33.8568), (150.7290, -35.1456), (145.808595, -16.005679), (149.1244, -35.3081)]
    assert lookup.states_of(points) == [lookup.state_of(lon, lat) for lon, lat in points] == ["NSW", "JBT", None, "ACT"]
    assert lookup.states_of([]) == []
