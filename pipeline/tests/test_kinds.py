"""Tests for pipeline.kinds: the kind vocabulary and how OSM tags map to it."""

import pytest

from pipeline.common import GROUPS, KIND_GROUP, KINDS
from pipeline.kinds import classify, container_type


def test_vocabulary_has_the_otwd_codes_and_our_two_extensions():
    otwd = [
        "free_camp", "paid_campground", "caravan_park", "national_park_campsite",
        "state_forest_campsite", "showground", "rest_area", "dump_point",
        "drinking_water_point", "public_toilet", "public_shower", "laundromat",
        "information_centre", "attraction", "lookout", "swimming_spot", "boat_ramp",
        "fuel", "lpg_refill_swap", "mechanic", "hospital_emergency", "ferry_terminal",
    ]
    assert list(KINDS) == otwd + ["campground", "picnic_site"]
    assert set(KIND_GROUP.values()) == set(GROUPS)
    assert KIND_GROUP["campground"] == "overnight"
    assert KIND_GROUP["picnic_site"] == "utility"
    assert KIND_GROUP["hospital_emergency"] == "emergency"
    assert KIND_GROUP["ferry_terminal"] == "transport"


CAMP = {"tourism": "camp_site"}


@pytest.mark.parametrize(
    "tags, inside, kind",
    [
        # Containment comes first, in the order park, forest, showground.
        (CAMP, {"park"}, "national_park_campsite"),
        ({**CAMP, "fee": "yes"}, {"park"}, "national_park_campsite"),
        (CAMP, {"forest"}, "state_forest_campsite"),
        (CAMP, {"showground"}, "showground"),
        (CAMP, {"park", "showground"}, "national_park_campsite"),
        (CAMP, {"park", "forest"}, "national_park_campsite"),
        (CAMP, {"forest", "showground"}, "state_forest_campsite"),
        # Then the fee.
        ({**CAMP, "fee": "no"}, set(), "free_camp"),
        ({**CAMP, "fee": "yes"}, set(), "paid_campground"),
        ({**CAMP, "fee": "$15 a night"}, set(), "paid_campground"),
        (CAMP, set(), "campground"),
        ({**CAMP, "fee": "donation"}, set(), "campground"),
        # caravans=yes does not make a camp site a caravan park.
        ({**CAMP, "caravans": "yes", "fee": "no"}, set(), "free_camp"),
        # A camp site that is also a rest area is classified as a camp site.
        ({**CAMP, "highway": "rest_area", "fee": "no"}, set(), "free_camp"),
        # Caravan sites ignore containment.
        ({"tourism": "caravan_site"}, {"park"}, "caravan_park"),
        ({"tourism": "caravan_site", "fee": "no"}, set(), "free_camp"),
        ({"tourism": "caravan_site", "fee": "yes"}, set(), "caravan_park"),
    ],
)
def test_overnight_precedence(tags, inside, kind):
    assert classify(tags, frozenset(inside)) == kind


@pytest.mark.parametrize(
    "tags, kind",
    [
        ({"highway": "rest_area"}, "rest_area"),
        ({"highway": "services"}, None),
        ({"amenity": "sanitary_dump_station"}, "dump_point"),
        ({"amenity": "water_point"}, "drinking_water_point"),
        ({"amenity": "drinking_water"}, "drinking_water_point"),
        ({"amenity": "toilets"}, "public_toilet"),
        ({"amenity": "shower"}, "public_shower"),
        ({"shop": "laundry", "self_service": "yes"}, "laundromat"),
        ({"shop": "laundry"}, None),
        ({"tourism": "information", "information": "office"}, "information_centre"),
        ({"tourism": "information", "information": "visitor_centre"}, "information_centre"),
        ({"tourism": "information", "information": "board"}, None),
        ({"tourism": "attraction"}, "attraction"),
        ({"tourism": "viewpoint"}, "lookout"),
        ({"tourism": "viewpoint", "attraction": "yes"}, "lookout"),
        ({"tourism": "attraction", "leisure": "slipway"}, "boat_ramp"),
        ({"leisure": "swimming_area"}, "swimming_spot"),
        ({"leisure": "bathing_place"}, "swimming_spot"),
        ({"leisure": "swimming_pool", "access": "yes"}, "swimming_spot"),
        ({"leisure": "swimming_pool", "access": "public"}, "swimming_spot"),
        ({"leisure": "swimming_pool"}, None),
        ({"leisure": "swimming_pool", "access": "private"}, None),
        ({"natural": "beach"}, None),
        ({"leisure": "slipway"}, "boat_ramp"),
        ({"amenity": "fuel"}, "fuel"),
        ({"amenity": "fuel", "fuel:lpg": "yes"}, "fuel"),
        ({"shop": "gas"}, "lpg_refill_swap"),
        ({"shop": "car_repair"}, "mechanic"),
        ({"amenity": "hospital", "emergency": "yes"}, "hospital_emergency"),
        ({"amenity": "hospital"}, None),
        ({"amenity": "hospital", "emergency": "no"}, None),
        ({"amenity": "ferry_terminal"}, "ferry_terminal"),
        ({"tourism": "picnic_site"}, "picnic_site"),
        ({"tourism": "picnic_site", "amenity": "toilets"}, "public_toilet"),
        ({"amenity": "bench"}, None),
        ({}, None),
    ],
)
def test_other_kinds(tags, kind):
    assert classify(tags) == kind


@pytest.mark.parametrize(
    "tags, expected",
    [
        ({"boundary": "national_park"}, "park"),
        ({"boundary": "protected_area", "protect_class": "2"}, "park"),
        ({"boundary": "protected_area", "protect_class": "1b"}, "park"),
        ({"boundary": "protected_area", "protect_class": "7"}, None),
        ({"boundary": "protected_area"}, None),
        ({"boundary": "protected_area", "operator": "Parks Victoria"}, "park"),
        ({"boundary": "protected_area", "operator": "NSW National Parks and Wildlife Service"}, "park"),
        ({"boundary": "protected_area", "protect_class": "7", "operator": "QPWS"}, "park"),
        ({"landuse": "forest", "operator": "Forestry Corporation of NSW"}, "forest"),
        ({"landuse": "forest", "operator": "VicForests"}, "forest"),
        ({"landuse": "forest", "operator": "Acme Timber Pty Ltd"}, None),
        ({"landuse": "forest"}, None),
        # A state forest tagged as a protected area is a forest, not a park.
        ({"boundary": "protected_area", "protect_class": "6", "protection_title": "State Forest"}, "forest"),
        ({"landuse": "forest", "name": "Pilliga State Forest"}, "forest"),
        ({"landuse": "recreation_ground", "recreation_ground": "showground"}, "showground"),
        ({"landuse": "recreation_ground"}, None),
        ({"leisure": "showground"}, "showground"),
        ({"natural": "wood"}, None),
    ],
)
def test_container_types(tags, expected):
    assert container_type(tags) == expected
