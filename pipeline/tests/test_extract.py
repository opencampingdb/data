"""Tests for pipeline.extract: OSM file in, plain place records out."""

from pathlib import Path

from pipeline.extract import extract_file, facts_from_tags, kind_from_tags

MULTIPOLYGON = Path(__file__).parent / "fixtures" / "multipolygon.osm"


def by_osm(records):
    return {(r["osm"]["type"], r["osm"]["id"]): r for r in records}


def test_keeps_only_spec_tags_and_skips_relations_and_open_ways(v1_osm):
    records = extract_file(v1_osm)
    keys = sorted(by_osm(records))
    # node 1003 has no tags, node 1005 is a bench, way 2002 is not closed,
    # relation 4001 is not a multipolygon. None of them belong in the extract.
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
        "kind": "camp_site",
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
    assert park["kind"] == "caravan_site"
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
    assert water["kind"] == "drinking_water"
    assert water["name"] is None
    assert water["facts"] == {}


def test_kind_priority_when_several_tags_match():
    # A camp site that is also tagged as toilets is a camp site.
    assert kind_from_tags({"amenity": "toilets", "tourism": "camp_site"}) == "camp_site"
    assert kind_from_tags({"amenity": "sanitary_dump_station"}) == "dump_station"
    assert kind_from_tags({"amenity": "shower"}) == "shower"
    assert kind_from_tags({"tourism": "picnic_site"}) == "picnic_site"
    assert kind_from_tags({"amenity": "bench"}) is None


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
    # Relation 4002 is tagged camp_site but is not a multipolygon: skipped.
    # The untagged member ways are not places either.
    assert [(r["osm"]["type"], r["osm"]["id"]) for r in records] == [("relation", 4001)]
    camp = records[0]
    assert camp["osm"] == {"type": "relation", "id": 4001, "version": 6}
    assert camp["kind"] == "camp_site"
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
    # only way let through: the open way 2002 is not closed and the type=site
    # relation has no multipolygon members.
    stats = {}
    records = extract_file(v1_osm, stats)
    assert stats["ways_to_area_builder"] == 1
    assert ("way", 2001) in {(r["osm"]["type"], r["osm"]["id"]) for r in records}
