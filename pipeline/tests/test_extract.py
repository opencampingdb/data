"""Tests for pipeline.extract: OSM file in, plain place records out."""

from pathlib import Path

from pipeline.extract import extract_file, facts_from_tags

FIXTURES = Path(__file__).parent / "fixtures"
MULTIPOLYGON = FIXTURES / "multipolygon.osm"


def by_osm(records):
    return {(r["osm"]["type"], r["osm"]["id"]): r for r in records}


def test_keeps_only_spec_tags_and_skips_relations_and_open_ways(v1_osm):
    records = extract_file(v1_osm)
    keys = sorted(by_osm(records))
    # node 1003 has no tags, node 1005 is a bench, way 2002 is not closed,
    # relation 4001 is a type=route relation. None of them belong in the extract.
    # The area pyosmium builds from closed way 2001 is not a second record.
    assert keys == [("node", 1001), ("node", 1002), ("way", 2001)]


def test_records_are_sorted_by_osm_type_then_id(v1_osm):
    records = extract_file(v1_osm)
    assert [(r["osm"]["type"], r["osm"]["id"]) for r in records] == [
        ("node", 1001),
        ("node", 1002),
        ("way", 2001),
    ]


def test_camp_site_node(v1_osm):
    camp = by_osm(extract_file(v1_osm))[("node", 1001)]
    assert camp == {
        "osm": {"type": "node", "id": 1001, "version": 1},
        "kind": "free_camp",
        "name": "Fixture Creek Camp",
        "location": {"lat": -37.15, "lon": 142.5},
        "facts": {
            "fee": False,
            "pets": "leashed",
            "toilets": True,
            "power": False,
            "drinking_water": True,
            "max_stay": "2d",
            "access": "4wd",
            "tents": True,
            "caravans": False,
            "website": "https://example.org/camp",
        },
    }


def test_caravan_site_closed_way_uses_centroid(v1_osm):
    park = by_osm(extract_file(v1_osm))[("way", 2001)]
    assert park["kind"] == "caravan_park"
    assert park["osm"] == {"type": "way", "id": 2001, "version": 4}
    # Centroid of the four corners; the closing node is not counted twice.
    assert park["location"] == {"lat": -37.005, "lon": 142.005}
    assert park["facts"] == {
        "fee": True,
        "power": True,
        "showers": True,
        "dump_station": True,
        "phone": "+61 3 5555 0000",
        "opening_hours": "Mo-Su 08:00-18:00",
        "pets": "no",
    }


def test_rest_area(v1_osm):
    rest = by_osm(extract_file(v1_osm))[("node", 1002)]
    assert rest["kind"] == "rest_area"
    assert rest["facts"] == {"toilets": False, "opening_hours": "24/7"}


def test_unnamed_place_has_null_name(v2_osm):
    water = by_osm(extract_file(v2_osm))[("node", 1004)]
    assert water["kind"] == "drinking_water_point"
    assert water["name"] is None
    assert water["facts"] == {"water_type": "fountain"}


def test_fact_value_parsing():
    assert facts_from_tags({"fee": "$10 per night"}) == {"fee": True}
    assert facts_from_tags({"fee": "donation"}) == {}
    assert facts_from_tags({"fee": "YES"}) == {"fee": True}
    assert facts_from_tags({"maxstay": "48 hours"}) == {"max_stay": "48h"}
    assert facts_from_tags({"maxstay": "1 week"}) == {"max_stay": "1w"}
    assert facts_from_tags({"maxstay": "until the creek floods"}) == {
        "max_stay": "until the creek floods"
    }
    assert facts_from_tags({"4wd_only": "no"}) == {"access": "2wd"}
    assert facts_from_tags({"4wd_only": "recommended"}) == {"access": "awd"}
    assert facts_from_tags({"website": "example.org"}) == {}
    assert facts_from_tags({"contact:website": "http://example.org"}) == {
        "website": "http://example.org"
    }
    assert facts_from_tags({"phone": "+61 3 1111 1111;+61 3 2222 2222"}) == {
        "phone": "+61 3 1111 1111"
    }
    assert facts_from_tags({"shower": "cold"}) == {"showers": True}
    assert facts_from_tags({"power_supply": "maybe"}) == {}


def test_multipolygon_relation_uses_centroid_of_its_area():
    records = extract_file(MULTIPOLYGON)
    # Relation 4002 is tagged camp_site but is a type=route relation: skipped.
    # The untagged member ways are not places either.
    assert [(r["osm"]["type"], r["osm"]["id"]) for r in records] == [("relation", 4001)]
    camp = records[0]
    assert camp["osm"] == {"type": "relation", "id": 4001, "version": 6}
    # No fee tag and not inside a park, forest or showground.
    assert camp["kind"] == "campground"
    assert camp["name"] == "Fixture Bush Camp"
    assert camp["facts"] == {"entry": "permit"}
    # Outer square 0.02 degrees wide, centre 0.01 in from the north-west
    # corner. The hole is 1/16 of its area, centre 0.0035 in. Centroid:
    # (4 * 0.01 - 0.25 * 0.0035) / 3.75 = 0.0104333 in from the corner.
    assert camp["location"] == {"lat": -37.110433, "lon": 142.110433}


def test_entry_from_access_tag():
    assert facts_from_tags({"access": "yes"}) == {"entry": "public"}
    assert facts_from_tags({"access": "permissive"}) == {"entry": "public"}
    assert facts_from_tags({"access": "public"}) == {"entry": "public"}
    assert facts_from_tags({"access": "customers"}) == {"entry": "customers"}
    assert facts_from_tags({"access": "permit"}) == {"entry": "permit"}
    assert facts_from_tags({"access": "private"}) == {"entry": "private"}
    assert facts_from_tags({"access": "no"}) == {"entry": "private"}
    assert facts_from_tags({"access": "destination"}) == {}


def test_area_builder_only_gets_ways_it_needs():
    # Only the three member ways of the tagged multipolygon reach the area
    # builder. The building (way 2201) and the untagged closed way (2202) do
    # not, so on the full Australia file no building is ever assembled.
    stats = {}
    records = extract_file(MULTIPOLYGON, stats)
    assert stats["ways_to_area_builder"] == 3
    assert [r["osm"]["id"] for r in records] == [4001]


def test_tagged_closed_way_also_passes_the_area_gate(v1_osm):
    # The camp site way 2001 must still reach the extract loop. It is the
    # only way let through: the open way 2002 is not closed and the type=route
    # relation has no multipolygon members.
    stats = {}
    records = extract_file(v1_osm, stats)
    assert stats["ways_to_area_builder"] == 1
    assert ("way", 2001) in {(r["osm"]["type"], r["osm"]["id"]) for r in records}


def kinds_by_id(path, stats=None):
    return {r["osm"]["id"]: r["kind"] for r in extract_file(path, stats)}


def test_overnight_kinds_and_containment():
    kinds = kinds_by_id(FIXTURES / "overnight.osm")
    assert kinds == {
        101: "national_park_campsite",  # inside boundary=national_park, fee does not matter
        102: "national_park_campsite",  # inside protected_area class 2
        103: "free_camp",  # inside protected_area class 7, which is not a park
        104: "national_park_campsite",  # inside a protected area run by Parks Victoria
        105: "state_forest_campsite",  # inside landuse=forest run by Forestry Corporation
        106: "paid_campground",  # inside a private forest, which is not a state forest
        107: "state_forest_campsite",  # inside a relation titled State Forest
        108: "showground",  # inside landuse=recreation_ground + showground
        109: "showground",  # inside leisure=showground
        110: "national_park_campsite",  # inside a showground inside a park: park wins
        111: "free_camp",
        112: "paid_campground",
        113: "campground",  # fee not known
        114: "campground",  # fee=donation is not fee=no
        115: "free_camp",  # camp site and rest area: classified as a camp site
        116: "caravan_park",
        117: "free_camp",  # caravan_site + fee=no
        118: "caravan_park",  # caravan sites ignore containment
        2301: "national_park_campsite",  # a camp site way, by its centroid
    }


def test_camp_site_outside_and_inside_a_park_polygon():
    kinds = kinds_by_id(FIXTURES / "overnight.osm")
    assert kinds[101] == "national_park_campsite"
    assert kinds[111] == "free_camp"


def test_containers_and_buildings_are_not_places():
    kinds = kinds_by_id(FIXTURES / "overnight.osm")
    for container in (9001, 9002, 9003, 9004, 9005, 9006, 9007, 9008, 9009, 9101):
        assert container not in kinds
    assert 2401 not in kinds


def test_overnight_area_builder_gets_containers_but_no_building():
    # Way 2301 (a camp site), the seven qualifying container ways (9001,
    # 9002, 9004, 9005, 9007, 9008, 9009) and the member way 9102 of the
    # State Forest relation. Not the class 7 area (9003), not the private
    # forest (9006) and never the building (2401).
    stats = {}
    extract_file(FIXTURES / "overnight.osm", stats)
    assert stats["ways_to_area_builder"] == 9
    assert stats["containers"] == {"park": 3, "forest": 2, "showground": 3}


def test_utility_kinds():
    records = by_osm(extract_file(FIXTURES / "utility.osm"))
    kinds = {key[1]: r["kind"] for key, r in records.items()}
    assert kinds == {
        201: "rest_area",
        202: "dump_point",
        203: "drinking_water_point",
        204: "drinking_water_point",
        205: "drinking_water_point",
        206: "public_toilet",
        207: "public_shower",
        208: "laundromat",
        210: "picnic_site",
        # 209 is a laundry without self_service=yes; 211 is highway=services.
    }
    assert records[("node", 203)]["facts"] == {"water_type": "tank_fill"}
    assert records[("node", 204)]["facts"] == {"water_type": "fountain"}
    assert records[("node", 205)]["facts"] == {"water_type": "tank_fill", "drinking_water": True}


def test_services_attraction_emergency_and_transport_kinds():
    kinds = kinds_by_id(FIXTURES / "services.osm")
    assert kinds == {
        301: "information_centre",
        302: "information_centre",
        304: "attraction",
        305: "lookout",
        306: "lookout",
        307: "boat_ramp",
        308: "swimming_spot",
        309: "swimming_spot",
        310: "swimming_spot",
        311: "swimming_spot",
        315: "boat_ramp",
        316: "fuel",
        317: "fuel",
        318: "lpg_refill_swap",
        319: "mechanic",
        320: "hospital_emergency",
        323: "ferry_terminal",
        3901: "boat_ramp",  # an open way, at its middle vertex
        3902: "attraction",  # an open way, at its middle vertex
        # Left out: 303 information board, 312 and 313 pools without public
        # access, 314 natural=beach, 321 and 322 hospitals without
        # emergency=yes, and 3903, an open viewpoint way.
    }


def test_open_way_boat_ramps_and_attractions_sit_at_their_middle_vertex():
    stats = {}
    records = by_osm(extract_file(FIXTURES / "services.osm", stats))
    # Three nodes: the middle one is the second (node 3912).
    assert records[("way", 3901)]["location"] == {"lat": -34.3001, "lon": 146.0002}
    # Four nodes: the middle vertex is the third (index 4 // 2, node 3923).
    assert records[("way", 3902)]["location"] == {"lat": -34.311, "lon": 146.001}
    assert ("way", 3903) not in records
    assert stats["open_way_places"] == 2
    assert stats["open_ways"] == 1


SITES = FIXTURES / "sites.osm"


def test_site_relations_with_a_kind_tag_are_places():
    records = by_osm(extract_file(SITES))
    assert sorted(records) == [("relation", 7001), ("relation", 7002)]

    camp = records[("relation", 7001)]
    assert camp["kind"] == "free_camp"
    assert camp["name"] == "Fixture Site Camp"
    assert camp["osm"] == {"type": "relation", "id": 7001, "version": 3}
    # Mean of the member positions: node 7101 (-36.0, 141.0), the centroid of
    # closed way 7201 (-36.015, 141.015) and the centroid of the area of
    # multipolygon 7301 (-36.035, 141.035).
    assert camp["location"] == {"lat": -36.016667, "lon": 141.016667}

    toilet = records[("relation", 7002)]
    assert toilet["kind"] == "public_toilet"
    # The only member is the open way 7203: the mean of its two nodes.
    assert toilet["location"] == {"lat": -36.105, "lon": 141.105}


def test_site_relation_members_are_not_places_themselves():
    records = by_osm(extract_file(SITES))
    for member in (("node", 7101), ("way", 7201), ("relation", 7301), ("way", 7203)):
        assert member not in records


def test_site_relations_without_a_kind_tag_and_other_types_are_skipped():
    ids = {r["osm"]["id"] for r in extract_file(SITES)}
    assert 7003 not in ids  # type=site with no kind tag
    assert 7004 not in ids  # type=route with tourism=camp_site


def test_site_relation_count_in_stats():
    stats = {}
    extract_file(SITES, stats)
    assert stats["site_relations"] == 2
