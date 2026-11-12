"""Tests for pipeline.extract: OSM file in, plain place records out."""

import re
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
        "hot_water": True,
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
    assert facts_from_tags({"fee": "donation"}) == {"fee": "donation"}
    assert facts_from_tags({"fee": "by donation"}) == {}
    assert facts_from_tags({"fee": "YES"}) == {"fee": True}
    # fee=no on a camp or caravan site that is a commercial park is not known.
    assert facts_from_tags(
        {"tourism": "caravan_site", "name": "Lane Cove River Tourist Park", "fee": "no"}
    ) == {}
    assert facts_from_tags(
        {"tourism": "caravan_site", "name": "Lane Cove River Tourist Park", "fee": "yes"}
    ) == {"fee": True}
    assert facts_from_tags(
        {"tourism": "camp_site", "name": "Barbour Park Free Caravan Park", "fee": "no"}
    ) == {"fee": False}
    # Toilets in a tourist park can be free.
    assert facts_from_tags(
        {"amenity": "toilets", "name": "Armidale Tourist Park Customers", "fee": "no"}
    ) == {"fee": False}
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
        "phone": "+61 3 1111 1111",
        "phone_2": "+61 3 2222 2222",
    }
    assert facts_from_tags({"shower": "cold"}) == {"showers": True, "hot_water": False}
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
        102: "reserve_campsite",  # inside a class 2 coastal reserve, not a national park
        103: "free_camp",  # inside protected_area class 7, which is not a park
        104: "national_park_campsite",  # inside a national park run by Parks Victoria
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
    assert stats["containers"] == {"park": 2, "reserve": 1, "forest": 2, "showground": 3}


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


def test_osm_timestamp_from_overpass_meta():
    records = {(r["osm"]["type"], r["osm"]["id"]): r for r in extract_file(FIXTURES / "grampians-overpass.osm")}
    assert records[("node", 316596843)]["osm"] == {
        "type": "node",
        "id": 316596843,
        "version": 3,
        "timestamp": "2019-10-21T13:06:14Z",
    }
    assert all("timestamp" in r["osm"] for r in records.values())


def test_osm_timestamp_on_areas_and_site_relations(tmp_path):
    # Every way, multipolygon and site relation place takes the edit time of
    # its own OSM object.
    for name in ("multipolygon.osm", "sites.osm"):
        text = (FIXTURES / name).read_text(encoding="utf-8")
        text = re.sub(r'( version="\d+")', r'\1 timestamp="2024-05-06T07:08:09Z"', text)
        path = tmp_path / name
        path.write_text(text, encoding="utf-8")
        records = extract_file(path)
        assert records
        assert {r["osm"]["timestamp"] for r in records} == {"2024-05-06T07:08:09Z"}


def test_no_osm_timestamp_when_the_file_has_none(v1_osm):
    assert all("timestamp" not in r["osm"] for r in extract_file(v1_osm))


def test_site_feature_facts():
    assert facts_from_tags({
        "wheelchair": "yes", "motorhome": "no", "washing_machine": "yes", "kitchen": "yes",
        "bbq": "no", "picnic_table": "yes", "openfire": "yes", "cabins": "no", "playground": "yes",
    }) == {
        "wheelchair": True, "motorhomes": False, "laundry": True, "kitchen": True, "bbq": False,
        "picnic_tables": True, "campfires": True, "cabins": False, "playground": True,
    }
    assert facts_from_tags({"wheelchair": "limited"}) == {}
    assert facts_from_tags({"internet_access": "wlan"}) == {"wifi": True}
    assert facts_from_tags({"internet_access": "no"}) == {"wifi": False}
    assert facts_from_tags({"internet_access": "terminal"}) == {}
    assert facts_from_tags({"nudism": "designated"}) == {"nudist": True}
    assert facts_from_tags({"nudism": "no"}) == {"nudist": False}
    assert facts_from_tags({"min_age": "50"}) == {"over_50s": True}
    assert facts_from_tags({"min_age": "18"}) == {}


def test_brand_contacts_and_social_pages():
    assert facts_from_tags({"brand": "BIG4"}) == {"brand": "big4"}
    assert facts_from_tags({"brand": "Discovery Parks"}) == {"brand": "discovery"}
    assert facts_from_tags({"brand": "Some Local Park"}) == {}
    assert facts_from_tags({"contact:email": "stay@example.com.au"}) == {"email": "stay@example.com.au"}
    assert facts_from_tags({"email": "not an email"}) == {}
    assert facts_from_tags({
        "contact:facebook": "https://www.facebook.com/lakebend",
        "contact:instagram": "lakebend",
        "contact:twitter": "https://x.com/lakebend",
        "contact:tiktok": "http://tiktok.com/@lakebend",
    }) == {"facebook": "https://www.facebook.com/lakebend", "x": "https://x.com/lakebend"}


def test_street_address():
    assert facts_from_tags({
        "addr:housenumber": "12",
        "addr:street": "Lake Road",
        "addr:city": "Lakeside",
        "addr:state": "Victoria",
        "addr:postcode": "3999",
    }) == {"address": {"housenumber": "12", "street": "Lake Road", "suburb": "Lakeside", "state": "VIC", "postcode": "3999"}}
    # addr:suburb wins over addr:city, and a rural address can name a place instead of a street.
    assert facts_from_tags({"addr:place": "Lake Station", "addr:suburb": "Lakeside", "addr:city": "Shire Town"}) == {
        "address": {"place": "Lake Station", "suburb": "Lakeside"}
    }
    # A state or postcode we can't read is left out.
    assert facts_from_tags({"addr:street": "Lake Road", "addr:state": "Somewhere", "addr:postcode": "39"}) == {
        "address": {"street": "Lake Road"}
    }
    # No street and no place, no address.
    assert facts_from_tags({"addr:postcode": "3999", "addr:city": "Lakeside"}) == {}


def test_bins_from_bin():
    assert facts_from_tags({"bin": "yes"}) == {"bins": True}
    assert facts_from_tags({"bin": "no"}) == {"bins": False}
    assert facts_from_tags({"bin": "recycling"}) == {}


def test_air_from_compressed_air():
    assert facts_from_tags({"compressed_air": "yes"}) == {"air": True}
    assert facts_from_tags({"compressed_air": "no"}) == {"air": False}
    assert facts_from_tags({"compressed_air": "broken"}) == {}


def test_hot_water_from_hot_water_and_shower():
    assert facts_from_tags({"hot_water": "yes"}) == {"hot_water": True}
    assert facts_from_tags({"hot_water": "no"}) == {"hot_water": False}
    # Hot and cold showers are both showers.
    assert facts_from_tags({"shower": "hot"}) == {"showers": True, "hot_water": True}
    assert facts_from_tags({"shower": "cold"}) == {"showers": True, "hot_water": False}
    # hot_water wins over a cold shower: the basins can have hot water.
    assert facts_from_tags({"shower": "cold", "hot_water": "yes"}) == {"showers": True, "hot_water": True}
    # An explicit hot_water tag wins over a hot shower too.
    assert facts_from_tags({"shower": "hot", "hot_water": "no"}) == {"showers": True, "hot_water": False}
    assert facts_from_tags({"shower": "yes"}) == {"showers": True}


def test_fuels_from_fuel_tags():
    assert facts_from_tags({
        "fuel:lpg": "yes",
        "fuel:adblue": "yes",
        "fuel:octane_98": "no",
        "fuel:octane_91": "yes",
        "fuel:diesel": "yes",
    }) == {"fuels": ["diesel", "unleaded_91", "adblue", "lpg"]}
    # fuel:lpg is autogas at a pump. It never says the place refills or swaps gas bottles.
    assert facts_from_tags({"fuel:lpg": "yes"}) == {"fuels": ["lpg"]}
    assert facts_from_tags({"fuel:lpg": "no"}) == {}
    assert facts_from_tags({"fuel:diesel": "no", "fuel:e10": "unknown"}) == {}


def test_booking_required_from_reservation():
    assert facts_from_tags({"reservation": "required"}) == {"booking_required": True}
    assert facts_from_tags({"reservation": "no"}) == {"booking_required": False}
    # yes and recommended say bookings are taken, not that one is needed.
    assert facts_from_tags({"reservation": "yes"}) == {}
    assert facts_from_tags({"reservation": "recommended"}) == {}


def test_toilet_type_from_toilets_disposal():
    assert facts_from_tags({"toilets:disposal": "flush"}) == {"toilet_type": "flush"}
    assert facts_from_tags({"toilets:disposal": "pitlatrine"}) == {"toilet_type": "pit"}
    assert facts_from_tags({"toilets:disposal": "chemical"}) == {"toilet_type": "chemical"}
    assert facts_from_tags({"toilets:disposal": "dry_toilet"}) == {"toilet_type": "dry"}
    assert facts_from_tags({"toilets:disposal": "bucket"}) == {"toilet_type": "dry"}
    assert facts_from_tags({"toilets:disposal": "Flush;pitlatrine"}) == {"toilet_type": "flush"}
    assert facts_from_tags({"toilets:disposal": "outhouse"}) == {}


def test_operator():
    assert facts_from_tags({"operator": " Lakeside Shire Council "}) == {"operator": "Lakeside Shire Council"}
    assert facts_from_tags({"operator": "x" * 200}) == {"operator": "x" * 200}
    assert facts_from_tags({"operator": "x" * 201}) == {}
    assert facts_from_tags({"operator": "  "}) == {}


def test_fishing():
    assert facts_from_tags({"fishing": "yes"}) == {"fishing": True}
    assert facts_from_tags({"fishing": "no"}) == {"fishing": False}
    # Other values say something about the fishing, not whether it is allowed.
    assert facts_from_tags({"fishing": "permit"}) == {}
    assert facts_from_tags({"fishing": "catch_and_release"}) == {}


def test_booking_url_from_website_booking_then_reservation_website():
    assert facts_from_tags({"website:booking": "https://example.org/book"}) == {
        "booking_url": "https://example.org/book"
    }
    assert facts_from_tags({"reservation:website": "https://example.org/reserve"}) == {
        "booking_url": "https://example.org/reserve"
    }
    # website:booking wins.
    assert facts_from_tags({
        "website:booking": "https://example.org/book",
        "reservation:website": "https://example.org/reserve",
    }) == {"booking_url": "https://example.org/book"}
    # The first with a value wins, even when it is not https.
    assert facts_from_tags({
        "website:booking": "http://example.org/book",
        "reservation:website": "https://example.org/reserve",
    }) == {}
    # An empty website:booking is no value, so reservation:website is read.
    assert facts_from_tags({
        "website:booking": " ",
        "reservation:website": "https://example.org/reserve",
    }) == {"booking_url": "https://example.org/reserve"}
    assert facts_from_tags({"website:booking": "example.org/book"}) == {}
    assert facts_from_tags({"website:booking": "https://example.org/" + "x" * 500}) == {}


def test_phone_2_from_the_second_phone_value():
    assert facts_from_tags({"phone": "+61 3 1111 1111; +61 3 2222 2222"}) == {
        "phone": "+61 3 1111 1111",
        "phone_2": "+61 3 2222 2222",
    }
    # Both numbers come from the same key: phone wins over contact:phone.
    assert facts_from_tags({
        "phone": "+61 3 1111 1111",
        "contact:phone": "+61 3 3333 3333;+61 3 4444 4444",
    }) == {"phone": "+61 3 1111 1111"}
    assert facts_from_tags({"contact:phone": "+61 3 3333 3333;+61 3 4444 4444;+61 3 5555 5555"}) == {
        "phone": "+61 3 3333 3333",
        "phone_2": "+61 3 4444 4444",
    }
    assert facts_from_tags({"phone": "+61 3 1111 1111;"}) == {"phone": "+61 3 1111 1111"}
    # The same number twice is one number.
    assert facts_from_tags({"phone": " +61 3 1111 1111 ; +61 3 1111 1111 "}) == {"phone": "+61 3 1111 1111"}
    assert facts_from_tags({"phone": "+61 3 1111 1111;" + "1" * 101}) == {"phone": "+61 3 1111 1111"}


def test_guest_only_dump_station_counts_on_a_place_to_stay_only():
    records = by_osm(extract_file(FIXTURES / "dump_customers.osm"))
    park = records[("node", 7001)]
    assert park["kind"] == "caravan_park"
    # Guests may use it, and a place to stay is for guests.
    assert park["facts"] == {"dump_station": True}
    # A dump point or fuel station for customers only is not open to everyone.
    dump_point = records[("node", 7002)]
    assert dump_point["kind"] == "dump_point"
    assert "dump_station" not in dump_point["facts"]
    fuel = records[("node", 7003)]
    assert "dump_station" not in fuel["facts"]
    assert records[("node", 7004)]["facts"]["dump_station"] is True


def test_hgv_does_not_set_big_rigs():
    # hgv is road access permission for heavy goods vehicles, not room to turn a 10 m rig.
    assert facts_from_tags({"hgv": "yes"}) == {}
    assert facts_from_tags({"hgv": "designated"}) == {}
    assert facts_from_tags({"hgv": "no"}) == {}


def test_charges_are_copied_as_osm_has_them():
    facts = facts_from_tags({
        "tourism": "caravan_site",
        "charge": " 57 AUD/night ",
        "charge:conditional": "75 AUD/night @ (Dec-Jan)",
    })
    assert facts["charge"] == "57 AUD/night"
    assert facts["charge_conditional"] == "75 AUD/night @ (Dec-Jan)"
    # Out of the wiki's syntax, but kept as OSM has it (ADR 0002).
    assert facts_from_tags({"charge": "$14.50AUD"})["charge"] == "$14.50AUD"
    assert "charge" not in facts_from_tags({"charge": "  "})


def test_check_dates_come_from_check_date_tags_of_charges_osm_has():
    from pipeline.extract import osm_link

    tags = {
        "charge": "57 AUD/night",
        "check_date:charge": "2026-11-03",
        "check_date:charge:conditional": "2026-10-01",
        "check_date:opening_hours": "2026-01-01",
    }
    assert osm_link("way", 7, 3, None, tags) == {"type": "way", "id": 7, "version": 3, "check_dates": {"charge": "2026-11-03"}}
    assert "check_dates" not in osm_link("node", 1, 1, None, {"charge": "5 AUD", "check_date:charge": "Nov 2026"})
    assert "check_dates" not in osm_link("node", 1, 1, None)
