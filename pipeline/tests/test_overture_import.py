"""Tests for the Overture importer (pipeline.overture.import_overture), at its
one seam: place records, an Overture extract, the link file and the kept lists
in; changed records, gap fills, removals, "Overture differs" and the new link
file out."""

import json
from pathlib import Path

import pytest
from shapely.geometry import Polygon

from pipeline.common import KIND_GROUP, dump_record, place_path
from pipeline.geometry import Geometry
from pipeline.overture import import_overture, main
from pipeline.overture_rules import OVERTURE_KINDS, SHARED_PAGE_LIMIT, KeptLists, load_kept_lists
from pipeline.validate import validate_repo

REPO = Path(__file__).resolve().parents[2]

T0 = "2026-10-01T16:00:00Z"
RUN = "2026-11-15T00:00:00Z"
RELEASE = "2026-10-21.0"
M = 1 / 111_320  # about 1 m of latitude, in degrees
LAT, LON = -35.0, 145.0

A = "ocdb_01M66CKP2X55A8CVGR95ZFJ1A0"
B = "ocdb_01M66CKP2X55A8CVGR95ZFJ1B0"
G1 = "08f2b1a0c4d5e6f70321a1b2c3d4e501"
G2 = "08f2b1a0c4d5e6f70321a1b2c3d4e502"

LISTS = KeptLists(
    categories={
        "caravan_park": {"rv_park", "campground", "holiday_park"},
        "campground": {"campground", "rv_park", "holiday_park"},
        "fuel": {"gas_station"},
        "lookout": {"scenic_viewpoint"},
    },
    directory_hosts=("placestars.com",),
    do_not_match=frozenset(),
)


def place(place_id=A, kind="caravan_park", name="Riverbend Caravan Park", metres_north=0.0, facts=None, sources=None, **changes):
    facts = facts or {}
    record = {
        "id": place_id,
        "name": name,
        "kind": kind,
        "group": KIND_GROUP[kind],
        "location": {"lat": round(LAT + metres_north * M, 6), "lon": LON},
        "osm": {"type": "way", "id": 1000 + int(place_id[-2], 36), "version": 1},
        "facts": facts,
        "status": "open",
        "rating": None,
        "fact_sources": sources if sources is not None else {k: "osm" for k in facts},
        "updated": T0,
    }
    record.update(changes)
    return record


def ov(gers=G1, name="Riverbend Caravan Park", category="rv_park", metres_north=0.0, confidence=0.9,
       websites=None, phones=None, addresses=None, change="unchanged", socials=None):
    return {
        "id": gers,
        "change": change,
        "name": name,
        "basic_category": category,
        "taxonomy_primary": category,
        "confidence": confidence,
        "websites": websites if websites is not None else ["https://riverbend.example.au/"],
        "socials": socials if socials is not None else [],
        "phones": phones if phones is not None else ["+61 2 5550 0101"],
        "addresses": addresses if addresses is not None else [],
        "sources": [{"dataset": "meta", "record_id": "1", "update_time": "2026-08-14T03:21:00.000Z"}],
        "lon": LON,
        "lat": round(LAT + metres_north * M, 7),
    }


def extract(*rows, release=RELEASE):
    places = [r for r in rows if r.get("change") != "removed"]
    header = {"extract": "overture-places", "version": 1, "release": release,
              "bbox": [112.0, -44.5, 154.5, -9.0], "places": len(places), "removed": len(rows) - len(places)}
    return [header, *sorted(rows, key=lambda r: r["id"])]


def run(places, rows, links=None, lists=LISTS, geometry=None, changelog_only=False, release=RELEASE):
    return import_overture(
        {p["id"]: p for p in places},
        extract(*rows, release=release),
        links or {},
        lists,
        now=RUN,
        geometry=geometry or Geometry(),
        changelog_only=changelog_only,
    )


# ------------------------------------------------------------ strong match


def test_a_strong_match_fills_the_website_and_phone():
    result = run([place()], [ov(metres_north=40)])
    record = result.records[A]
    assert record["facts"]["website"] == "https://riverbend.example.au/"
    assert record["facts"]["phone"] == "+61 2 5550 0101"
    assert record["fact_sources"]["website"] == "overture"
    assert record["fact_sources"]["phone"] == "overture"
    assert record["fact_dates"] == {"website": RUN, "phone": RUN}
    assert record["updated"] == RUN
    assert {(f["place"], f["key"]) for f in result.fills} == {(A, "website"), (A, "phone")}
    assert result.links == {A: {"gers": G1, "release": RELEASE}}


def test_a_match_at_the_limits_still_counts():
    # 99 m away, confidence exactly 0.7, a name close enough after normalising.
    result = run([place(name="Riverbend Caravan Pk")], [ov(name="RIVERBEND CARAVAN PARK!", metres_north=99, confidence=0.7)])
    assert result.records[A]["facts"]["website"] == "https://riverbend.example.au/"


@pytest.mark.parametrize("why,places,rows", [
    ("category", [place()], [ov(category="hotel")]),
    ("distance", [place()], [ov(metres_north=101)]),
    ("name", [place(name="Lucky Bay Caravan Park")], [ov(name="Riverbend Caravan Park")]),
    ("confidence", [place()], [ov(confidence=0.69)]),
    ("two candidates", [place()], [ov(G1), ov(G2, metres_north=30)]),
    ("kind not filled", [place(kind="lookout", name="Riverbend Lookout")], [ov(name="Riverbend Lookout", category="scenic_viewpoint")]),
    ("no name on either side", [place(name="Riverbend")], [ov(name=None)]),
])
def test_anything_short_of_a_strong_match_takes_nothing(why, places, rows):
    result = run(places, rows)
    assert result.records == {}, why
    assert result.fills == [] and result.links == {}


def test_a_pair_on_the_do_not_match_list_is_never_matched():
    lists = KeptLists(LISTS.categories, LISTS.directory_hosts, frozenset({(A, G1)}))
    assert run([place()], [ov()], lists=lists).records == {}


def test_a_pair_on_the_do_not_match_list_leaves_the_other_candidate_to_match():
    lists = KeptLists(LISTS.categories, LISTS.directory_hosts, frozenset({(A, G1)}))
    result = run([place()], [ov(G1), ov(G2, metres_north=20)], lists=lists)
    assert result.links == {A: {"gers": G2, "release": RELEASE}}


def test_the_kinds_filled_are_the_ones_in_the_spec():
    assert OVERTURE_KINDS == tuple(sorted((
        "free_camp", "paid_campground", "caravan_park", "national_park_campsite", "state_forest_campsite",
        "showground", "campground", "farm_stay", "backpacker_hostel", "roofed_accommodation", "reserve_campsite",
        "information_centre", "fuel", "lpg_refill_swap", "mechanic", "food_and_drink", "laundromat",
        "hospital_emergency", "attraction", "heritage", "experience",
    )))


def test_the_kept_category_map_covers_every_kind_filled_and_no_other():
    lists = load_kept_lists(REPO)
    assert set(lists.categories) == set(OVERTURE_KINDS)
    assert {"rv_park", "campground", "holiday_park"} <= lists.categories["caravan_park"]
    assert all(lists.categories[kind] for kind in OVERTURE_KINDS)


def test_a_name_with_no_latin_letters_never_matches_an_overture_place_with_no_name():
    # Both names normalise to nothing. Nothing in common is no match.
    result = run([place(name="营地")], [ov(name=None, metres_north=10)])
    assert A not in result.records
    assert result.links == {}


# ------------------------------------------------------- name for unnamed


def outline(metres_south, metres_north, metres_wide=300):
    """A rectangle around the test point, as the geometry sidecar gives it."""
    w = metres_wide * M / 0.819  # degrees of longitude at 35 S
    s, n = LAT - metres_south * M, LAT + metres_north * M
    return Polygon([(LON - w, s), (LON + w, s), (LON + w, n), (LON - w, n), (LON - w, s)])


def unnamed_park():
    return place(name=None)


def test_an_unnamed_place_takes_the_name_of_the_one_confident_overture_place_inside_its_outline():
    geometry = Geometry(areas={"way/1010": outline(100, 600)})
    result = run([unnamed_park()], [ov(metres_north=450, confidence=0.8)], geometry=geometry)
    record = result.records[A]
    assert record["name"] == "Riverbend Caravan Park"
    assert record["fact_sources"]["name"] == "overture"
    assert record["fact_dates"]["name"] == RUN
    assert {"place": A, "key": "name", "value": "Riverbend Caravan Park", "previous": None} in result.fills
    # The same match fills the gaps too.
    assert record["facts"]["website"] == "https://riverbend.example.au/"
    assert result.links[A]["gers"] == G1


def test_an_unnamed_place_without_an_outline_takes_a_name_from_within_100_m():
    result = run([unnamed_park()], [ov(metres_north=95, confidence=0.85)])
    assert result.records[A]["name"] == "Riverbend Caravan Park"


def test_an_unnamed_place_takes_a_name_from_within_100_m_of_its_outline():
    geometry = Geometry(areas={"way/1010": outline(100, 200)})
    result = run([unnamed_park()], [ov(metres_north=290, confidence=0.85)], geometry=geometry)
    assert result.records[A]["name"] == "Riverbend Caravan Park"


@pytest.mark.parametrize("why,rows", [
    ("confidence under 0.8", [ov(metres_north=50, confidence=0.79)]),
    ("category", [ov(metres_north=50, category="hotel")]),
    ("too far", [ov(metres_north=320)]),
    ("two candidates", [ov(G1, metres_north=50), ov(G2, name="Bend Tourist Park", metres_north=150)]),
    ("no name", [ov(metres_north=50, name=None)]),
])
def test_an_unnamed_place_takes_no_name_short_of_the_rule(why, rows):
    geometry = Geometry(areas={"way/1010": outline(100, 200)})
    result = run([unnamed_park()], rows, geometry=geometry)
    assert result.records == {}, why


def test_a_name_of_only_spaces_never_names_an_unnamed_place():
    result = run([place(name=None)], [ov(name="   ", metres_north=10)])
    assert result.records.get(A, place(name=None))["name"] is None


# --------------------------------------------------------------- gap fills


def test_a_value_from_any_other_source_is_never_replaced():
    facts = {"website": "https://riverbend.example.au/", "phone": "02 5550 0101", "address": {"street": "River St"}}
    for source in ("osm", "feedback", "contributed", "owner", "community_report"):
        record = place(facts=facts, sources={k: source for k in facts},
                       fact_dates={k: T0 for k in facts} if source != "osm" else None)
        if source == "osm":
            del record["fact_dates"]
        result = run([record], [ov(addresses=[{"freeform": "12 River St", "locality": "Bend", "postcode": "2640", "region": "NSW", "country": "AU"}])])
        assert result.records == {} and result.fills == [], source
        assert result.links[A]["gers"] == G1


def test_an_address_fills_from_the_overture_address():
    rows = [ov(addresses=[{"freeform": "Lot 4 Main Rd", "locality": "Barrow Creek", "postcode": "0872", "region": "Northern Territory", "country": "AU"}])]
    record = run([place()], rows).records[A]
    assert record["facts"]["address"] == {
        "housenumber": "Lot 4", "street": "Main Rd", "suburb": "Barrow Creek", "postcode": "0872", "state": "NT",
    }
    assert record["fact_sources"]["address"] == "overture"


@pytest.mark.parametrize("address,expected", [
    ({"freeform": "Unit 2, 2/6 Council Ave", "locality": "Rockingham", "postcode": "6168", "region": "WA", "country": "AU"},
     {"housenumber": "2/6", "street": "Council Ave", "suburb": "Rockingham", "postcode": "6168", "state": "WA"}),
    ({"freeform": "Princes Highway", "locality": None, "postcode": "33", "region": "Somewhere", "country": "AU"},
     {"street": "Princes Highway"}),
    # A council area is not a suburb.
    ({"freeform": "State Route 3", "locality": "Shire of Manjimup", "postcode": "6398", "region": None, "country": "AU"},
     {"street": "State Route 3", "postcode": "6398"}),
])
def test_an_address_keeps_only_what_fits_the_address_fact(address, expected):
    assert run([place()], [ov(addresses=[address])]).records[A]["facts"]["address"] == expected


@pytest.mark.parametrize("address", [
    {"freeform": None, "locality": "Coral Bay", "postcode": "6701", "region": "WA", "country": "AU"},
    {"freeform": "12 Main St", "locality": "Auckland", "postcode": "1010", "region": None, "country": "NZ"},
    {"freeform": "Unnamed Rd", "locality": "Harvey", "postcode": "6233", "region": "WA", "country": "AU"},
])
def test_an_address_without_a_street_or_outside_australia_is_not_taken(address):
    record = run([place()], [ov(addresses=[address])]).records[A]
    assert "address" not in record["facts"]


def test_a_website_on_a_directory_host_is_never_taken():
    rows = [ov(websites=["https://www.placestars.com/riverbend", "http://au.placestars.com/x", "https://riverbend.example.au"])]
    assert run([place()], rows).records[A]["facts"]["website"] == "https://riverbend.example.au"
    rows = [ov(websites=["https://www.placestars.com/riverbend"])]
    assert "website" not in run([place()], rows).records[A]["facts"]


def test_the_kept_directory_host_list_starts_empty_so_listing_sites_are_taken():
    lists = load_kept_lists(REPO)
    assert lists.directory_hosts == ()
    for listing in ("https://www.placestars.com/riverbend", "https://www.hipcamp.com/en-AU/land/riverbend-1"):
        record = run([place()], [ov(websites=[listing])], lists=lists).records[A]
        assert record["facts"]["website"] == listing
    rows = [ov(websites=["https://m.facebook.com/riverbend", "https://riverbend.example.au"])]
    assert run([place()], rows, lists=lists).records[A]["facts"]["website"] == "https://riverbend.example.au"


def test_email_brand_operating_status_and_confidence_are_never_taken():
    row = ov(websites=[], phones=[])
    row.update({"emails": ["info@riverbend.example.au"],
                "brand": {"names": {"primary": "Big4"}}, "operating_status": "open"})
    result = run([place()], [row])
    assert result.records == {} and result.fills == []
    assert result.links[A]["gers"] == G1


# ------------------------------------------------------------- social pages


@pytest.mark.parametrize("given,key,page", [
    ("https://www.facebook.com/riverbendpark", "facebook", "https://www.facebook.com/riverbendpark"),
    ("http://m.facebook.com/riverbendpark/?ref=page", "facebook", "https://www.facebook.com/riverbendpark"),
    ("https://facebook.com/profile.php?id=100063511234567&sk=about", "facebook",
     "https://www.facebook.com/profile.php?id=100063511234567"),
    ("https://www.facebook.com/pages/Riverbend-Park/123456789", "facebook", "https://www.facebook.com/123456789"),
    ("https://www.facebook.com/people/Riverbend-Park/61576217580151/", "facebook",
     "https://www.facebook.com/profile.php?id=61576217580151"),
    ("https://www.facebook.com/p/Riverbend-Park-100054544415650/", "facebook",
     "https://www.facebook.com/profile.php?id=100054544415650"),
    ("https://instagram.com/riverbend.park/", "instagram", "https://www.instagram.com/riverbend.park/"),
    ("https://www.tiktok.com/@riverbendpark?lang=en", "tiktok", "https://www.tiktok.com/@riverbendpark"),
    ("https://twitter.com/RiverbendPark", "x", "https://x.com/RiverbendPark"),
    ("https://x.com/RiverbendPark/", "x", "https://x.com/RiverbendPark"),
])
def test_a_strong_match_fills_each_social_page_from_overtures_socials(given, key, page):
    result = run([place()], [ov(socials=[given])])
    record = result.records[A]
    assert record["facts"][key] == page
    assert record["fact_sources"][key] == "overture"
    assert record["fact_dates"][key] == RUN
    assert record["facts"]["website"] == "https://riverbend.example.au/"
    assert {"place": A, "key": key, "value": page, "previous": None} in result.fills


@pytest.mark.parametrize("website,key,page", [
    ("https://www.facebook.com/riverbendpark/", "facebook", "https://www.facebook.com/riverbendpark"),
    ("https://www.instagram.com/riverbendpark", "instagram", "https://www.instagram.com/riverbendpark/"),
    ("https://www.tiktok.com/@riverbendpark", "tiktok", "https://www.tiktok.com/@riverbendpark"),
    ("https://x.com/riverbendpark", "x", "https://x.com/riverbendpark"),
    ("https://mobile.twitter.com/riverbendpark", "x", "https://x.com/riverbendpark"),
])
def test_a_website_on_a_social_host_fills_that_social_page_and_never_the_website(website, key, page):
    # LISTS names no social host as a directory host: the rule holds without the kept list.
    record = run([place()], [ov(websites=[website], phones=[])]).records[A]
    assert record["facts"] == {key: page}
    rows = [ov(websites=[website, "https://riverbend.example.au/"])]
    record = run([place()], rows).records[A]
    assert record["facts"]["website"] == "https://riverbend.example.au/"
    assert record["facts"][key] == page


def test_a_social_url_is_never_compared_with_the_osm_website():
    facts = {"website": "https://www.riverbendpark.com.au/"}
    result = run([place(facts=facts)], [ov(websites=["https://www.facebook.com/riverbendpark"])])
    assert result.differs == []
    assert result.records[A]["facts"]["website"] == "https://www.riverbendpark.com.au/"


def test_overtures_socials_come_before_a_social_website():
    rows = [ov(socials=["https://www.facebook.com/riverbendpark"], websites=["https://www.facebook.com/otherpage"])]
    assert run([place()], rows).records[A]["facts"]["facebook"] == "https://www.facebook.com/riverbendpark"


@pytest.mark.parametrize("link", [
    "https://www.facebook.com/riverbendpark/posts/pfbid02abc",
    "https://www.facebook.com/photo/?fbid=123456",
    "https://www.facebook.com/photo.php?fbid=123456",
    "https://www.facebook.com/riverbendpark/photos/a.1/2/",
    "https://www.facebook.com/events/123456789/",
    "https://www.facebook.com/share/1AbCdEf/",
    "https://www.facebook.com/sharer/sharer.php?u=https://riverbend.example.au",
    "https://www.facebook.com/permalink.php?story_fbid=1&id=2",
    "https://www.facebook.com/groups/riverbendcampers",
    "https://www.facebook.com/profile.php",
    "https://www.instagram.com/p/C1a2B3c4D5e/",
    "https://www.instagram.com/reel/C1a2B3c4D5e/",
    "https://www.instagram.com/stories/riverbendpark/123/",
    "https://www.tiktok.com/@riverbendpark/video/7300000000000000000",
    "https://vm.tiktok.com/ZMabc123/",
    "https://www.tiktok.com/t/ZTabc123/",
    "https://x.com/riverbendpark/status/1700000000000000000",
    "https://twitter.com/intent/tweet?text=hi",
    "https://twitter.com/share?url=https://riverbend.example.au",
    "https://twitter.com/hashtag/camping",
    "ftp://facebook.com/riverbendpark",
    "facebook.com/riverbendpark",
])
def test_a_post_photo_event_or_share_link_is_no_social_page(link):
    result = run([place()], [ov(websites=[], phones=[], socials=[link])])
    assert result.records == {} and result.fills == []
    result = run([place()], [ov(websites=[link], phones=[])])
    assert result.records == {} and result.fills == []


def test_a_social_page_from_any_other_source_is_never_replaced():
    facts = {"facebook": "https://www.facebook.com/osmpage"}
    result = run([place(facts=facts)], [ov(socials=["https://www.facebook.com/riverbendpark"])])
    record = result.records[A]
    assert record["facts"]["facebook"] == "https://www.facebook.com/osmpage"
    assert record["fact_sources"]["facebook"] == "osm"


def test_an_overture_social_page_is_refreshed_and_comes_off_like_the_other_values():
    facts = {"website": "https://riverbend.example.au/", "phone": "+61 2 5550 0101",
             "facebook": "https://www.facebook.com/riverbendpark", "x": "https://x.com/riverbendpark"}
    sources = {key: "overture" for key in facts}
    dates = {key: EARLIER for key in facts}
    park = place(facts=facts, sources=sources, fact_dates=dates)
    rows = [ov(socials=["https://www.facebook.com/riverbendpark", "https://www.instagram.com/riverbendpark"])]
    result = run([park], rows, links=LINKED)
    record = result.records[A]
    assert record["fact_dates"]["facebook"] == EARLIER
    assert record["facts"]["instagram"] == "https://www.instagram.com/riverbendpark/"
    assert record["fact_dates"]["instagram"] == RUN
    assert "x" not in record["facts"] and "x" not in record["fact_sources"]
    assert result.removals == [{"place": A, "key": "x", "value": "https://x.com/riverbendpark"}]
    result = run([park], [{"id": G1, "change": "removed"}], links=LINKED)
    assert result.records[A]["facts"] == {}
    assert {r["key"] for r in result.removals} == {"website", "phone", "facebook", "x"}


# ---------------------------------------------------------- Overture differs


def test_overture_differs_lists_a_website_host_or_phone_that_disagrees_with_osm():
    facts = {"website": "https://www.riverbendpark.com.au/book", "phone": "+61 2 5550 9999"}
    result = run([place(facts=facts)], [ov()])
    assert result.records == {}
    assert result.differs == [
        {"place": A, "name": "Riverbend Caravan Park", "kind": "caravan_park", "location": {"lat": LAT, "lon": LON},
         "key": "phone", "osm": "+61 2 5550 9999", "overture": "+61 2 5550 0101",
         "gers": G1, "overture_name": "Riverbend Caravan Park", "overture_location": {"lat": LAT, "lon": LON}},
        {"place": A, "name": "Riverbend Caravan Park", "kind": "caravan_park", "location": {"lat": LAT, "lon": LON},
         "key": "website", "osm": "https://www.riverbendpark.com.au/book", "overture": "https://riverbend.example.au/",
         "gers": G1, "overture_name": "Riverbend Caravan Park", "overture_location": {"lat": LAT, "lon": LON}},
    ]


def test_the_same_host_and_the_same_australian_number_written_differently_agree():
    facts = {"website": "http://www.riverbend.example.au/contact", "phone": "(02) 5550 0101; 0400 000 000"}
    assert run([place(facts=facts)], [ov(phones=["+61 (0)2 5550-0101"])]).differs == []


def test_overture_differs_leaves_out_values_from_other_sources_and_directory_hosts():
    facts = {"website": "https://other.example.au/", "phone": "+61 2 5550 9999"}
    owner = place(facts=facts, sources={"website": "owner", "phone": "owner"}, fact_dates={"website": T0, "phone": T0})
    assert run([owner], [ov()]).differs == []
    osm = place(facts={"website": "https://other.example.au/"})
    assert run([osm], [ov(websites=["https://placestars.com/riverbend"])]).differs == []


def test_a_value_a_person_took_from_overture_differs_replaces_osms():
    osm = place(facts={"website": "https://www.riverbendpark.com.au/book", "phone": "+61 2 5550 9999"})
    lists = KeptLists(**{**LISTS.__dict__, "taken": {(A, "website"): "https://riverbend.example.au/"}})
    result = run([osm], [ov()], lists=lists)
    record = result.records[A]
    assert record["facts"]["website"] == "https://riverbend.example.au/"
    assert record["fact_sources"] == {"website": "overture", "phone": "osm"}
    assert record["fact_dates"] == {"website": RUN}
    # Only the phone is still listed.
    assert [d["key"] for d in result.differs] == ["phone"]


def test_a_taken_value_stays_on_later_runs_even_without_a_match():
    taken = place(facts={"website": "https://riverbend.example.au/"}, sources={"website": "overture"},
                  fact_dates={"website": T0})
    lists = KeptLists(**{**LISTS.__dict__, "taken": {(A, "website"): "https://riverbend.example.au/"}})
    result = run([taken], [], lists=lists)
    assert result.records == {} and result.removals == []


def test_a_traveller_can_still_take_down_a_taken_value():
    reported = place(facts={"website": "https://riverbend-new.example.au/"}, sources={"website": "feedback"},
                     fact_dates={"website": T0})
    lists = KeptLists(**{**LISTS.__dict__, "taken": {(A, "website"): "https://riverbend.example.au/"}})
    result = run([reported], [ov(phones=[])], lists=lists)
    assert result.records == {}


def test_the_kept_lists_read_the_values_people_took(tmp_path):
    for name in ("categories.json", "directory-hosts.json"):
        (tmp_path / "overture").mkdir(exist_ok=True)
        (tmp_path / "overture" / name).write_bytes((REPO / "overture" / name).read_bytes())
    (tmp_path / "overture" / "taken.json").write_text(
        json.dumps({"taken": [{"place": A, "fact": "phone", "value": "+61 2 5550 0101"}]}), encoding="utf-8")
    assert load_kept_lists(tmp_path).taken == {(A, "phone"): "+61 2 5550 0101"}
    assert (REPO / "overture" / "taken.json").exists()


# ------------------------------------------------------------ shared pages

SHELL_PAGE = "https://www.facebook.com/Shell"
SHELL_SITE = "https://www.shell.com.au/"


def stations(n, **ov_changes):
    """n fuel stations 1 km apart, each matched to its own Overture place that gives one chain's pages."""
    places = [place(f"ocdb_01M66CKP2X55A8CVGR95Z{i:05d}", kind="fuel", name="Shell Coles Express",
                    metres_north=i * 1000) for i in range(n)]
    rows = [ov(f"08f2b1a0c4d5e6f70321a1b2c{i:07d}", name="Shell Coles Express", category="gas_station",
               metres_north=i * 1000, websites=[SHELL_SITE], socials=[SHELL_PAGE], phones=[], **ov_changes)
            for i in range(n)]
    return places, rows


def test_a_page_shared_by_more_than_the_limit_of_places_is_taken_for_none():
    assert SHARED_PAGE_LIMIT == 5
    places, rows = stations(SHARED_PAGE_LIMIT + 1)
    result = run(places, rows)
    assert result.fills == []
    assert result.shared == {SHELL_PAGE: 6, SHELL_SITE: 6}


def test_a_page_given_for_up_to_the_limit_of_places_is_taken():
    places, rows = stations(SHARED_PAGE_LIMIT)
    result = run(places, rows)
    assert sorted({(f["key"], f["value"]) for f in result.fills}) == [("facebook", SHELL_PAGE), ("website", SHELL_SITE)]
    assert len(result.fills) == 2 * SHARED_PAGE_LIMIT
    assert result.shared == {}


def test_a_changelog_run_counts_the_pages_of_linked_places_it_does_not_check_again():
    # Six stations share the chain's pages. Five are linked and unchanged, so
    # a changelog run leaves them as they are. The sixth is new: the page is
    # still given for six places, so it is not taken for the new one.
    places, rows = stations(SHARED_PAGE_LIMIT + 1)
    links = {p["id"]: {"gers": r["id"], "release": "2026-09-17.0"} for p, r in zip(places[1:], rows[1:])}
    rows[0]["change"] = "added"
    result = run(places, rows, links=links, changelog_only=True)
    assert result.fills == []
    assert result.shared == {SHELL_PAGE: 6, SHELL_SITE: 6}


def test_a_shared_page_gives_way_to_the_next_page_overture_gives():
    places, rows = stations(SHARED_PAGE_LIMIT + 1)
    rows[0]["websites"] = [SHELL_SITE, "https://shellbright.example.au/"]
    result = run(places, rows)
    assert [(f["place"], f["key"], f["value"]) for f in result.fills] == [
        (places[0]["id"], "website", "https://shellbright.example.au/"),
    ]


def test_a_shared_page_is_never_compared_with_the_osm_website():
    places, rows = stations(SHARED_PAGE_LIMIT + 1)
    places[0]["facts"] = {"website": "https://shellbright.example.au/"}
    places[0]["fact_sources"] = {"website": "osm"}
    assert run(places, rows).differs == []


# ------------------------------------------------------------------ refresh

EARLIER = "2026-10-15T00:00:00Z"


def filled_park(**changes):
    """A park an earlier run filled from Overture place G1."""
    facts = {"website": "https://riverbend.example.au/", "phone": "+61 2 5550 0101", "toilets": True}
    sources = {"website": "overture", "phone": "overture", "toilets": "osm"}
    return place(facts=facts, sources=sources, fact_dates={"website": EARLIER, "phone": EARLIER}, **changes)


LINKED = {A: {"gers": G1, "release": "2026-09-24.0"}}


def test_an_unchanged_overture_value_keeps_its_date_and_the_record_is_not_touched():
    result = run([filled_park()], [ov()], links=LINKED)
    assert result.records == {} and result.fills == [] and result.removals == []
    assert result.links == {A: {"gers": G1, "release": RELEASE}}


def test_an_overture_value_follows_overtures_change():
    result = run([filled_park()], [ov(phones=["+61 2 5550 0202"])], links=LINKED)
    record = result.records[A]
    assert record["facts"]["phone"] == "+61 2 5550 0202"
    assert record["fact_dates"] == {"website": EARLIER, "phone": RUN}
    assert result.fills == [{"place": A, "key": "phone", "value": "+61 2 5550 0202", "previous": "+61 2 5550 0101"}]


def test_a_value_overture_no_longer_gives_comes_off():
    result = run([filled_park()], [ov(phones=[])], links=LINKED)
    record = result.records[A]
    assert "phone" not in record["facts"] and "phone" not in record["fact_sources"]
    assert record["fact_dates"] == {"website": EARLIER}
    assert result.removals == [{"place": A, "key": "phone", "value": "+61 2 5550 0101"}]


@pytest.mark.parametrize("why,rows", [
    ("removed from Overture", [{"id": G1, "change": "removed"}]),
    ("no longer a strong match", [ov(confidence=0.6, change="data_changed")]),
    ("a second candidate", [ov(G1), ov(G2, metres_north=10)]),
])
def test_every_overture_value_comes_off_when_the_match_is_lost(why, rows):
    result = run([filled_park()], rows, links=LINKED)
    record = result.records[A]
    assert record["facts"] == {"toilets": True}, why
    assert record["fact_sources"] == {"toilets": "osm"}
    assert "fact_dates" not in record
    assert record["updated"] == RUN
    assert {r["key"] for r in result.removals} == {"website", "phone"}
    assert result.links == {}


def test_an_overture_name_stays_while_the_name_rule_holds_and_comes_off_when_it_fails():
    named = place(name="Riverbend Caravan Park", sources={"name": "overture"}, fact_dates={"name": EARLIER},
                  facts={}, )
    named["facts"], named["fact_sources"] = {"website": "https://riverbend.example.au/", "phone": "+61 2 5550 0101"}, \
        {"name": "overture", "website": "overture", "phone": "overture"}
    named["fact_dates"] = {"name": EARLIER, "website": EARLIER, "phone": EARLIER}
    assert run([named], [ov(confidence=0.85)], links=LINKED).records == {}
    # 0.75 is enough for a strong match, but the name came from Overture, so the
    # place counts as unnamed and a name source needs 0.8.
    record = run([named], [ov(confidence=0.75)], links=LINKED).records[A]
    assert record["name"] is None and "name" not in record["fact_sources"]
    assert record["facts"] == {} and "fact_dates" not in record


def test_a_value_another_source_has_since_replaced_is_left_alone():
    record = filled_park()
    record["facts"]["phone"] = "+61 2 5550 7777"
    record["fact_sources"]["phone"] = "feedback"
    record["fact_dates"]["phone"] = "2026-11-01T00:00:00Z"
    result = run([record], [ov(phones=["+61 2 5550 0202"])], links=LINKED)
    assert result.records == {} and result.removals == []


def test_a_place_whose_kind_is_no_longer_filled_loses_its_overture_values():
    record = filled_park(kind="lookout", name="Riverbend Caravan Park")
    record["group"] = KIND_GROUP["lookout"]
    result = run([record], [ov()], links=LINKED)
    assert result.records[A]["facts"] == {"toilets": True}
    assert result.links == {}


def test_a_merged_record_is_left_alone():
    record = place(status="merged", merged_into=B)
    assert run([record], [ov()]).records == {}


# ---------------------------------------------------------------- link file


def test_the_link_file_lists_each_matched_place_with_its_gers_id_and_the_release():
    other = place(B, kind="fuel", name="Main Road Roadhouse", metres_north=5000)
    rows = [ov(), ov(G2, name="Main Road Roadhouse", category="gas_station", metres_north=5030)]
    result = run([place(), other], rows, links={"ocdb_gone": {"gers": G2, "release": "2026-09-24.0"}})
    assert result.links == {A: {"gers": G1, "release": RELEASE}, B: {"gers": G2, "release": RELEASE}}


def test_a_changelog_run_does_not_recheck_a_place_whose_overture_place_the_changelog_does_not_list_as_changed():
    # Overture's phone differs, but the changelog says unchanged or does not list
    # the GERS id, so the place is not looked at again.
    for rows in ([ov(phones=["+61 2 5550 0202"], change="unchanged")], [ov(phones=["+61 2 5550 0202"], change=None)], []):
        result = run([filled_park()], rows, links=LINKED, changelog_only=True)
        assert result.records == {} and result.fills == []
        assert result.links == {A: {"gers": G1, "release": RELEASE}}
    # A full run does.
    result = run([filled_park()], [ov(phones=["+61 2 5550 0202"])], links=LINKED)
    assert result.records[A]["facts"]["phone"] == "+61 2 5550 0202"


@pytest.mark.parametrize("rows,phone", [
    ([ov(phones=["+61 2 5550 0202"], change="data_changed")], "+61 2 5550 0202"),
    ([{"id": G1, "change": "removed"}], None),
])
def test_a_changelog_run_rechecks_a_changed_or_removed_overture_place(rows, phone):
    result = run([filled_park()], rows, links=LINKED, changelog_only=True)
    assert result.records[A]["facts"].get("phone") == phone


@pytest.mark.parametrize("change", ["added", "data_changed"])
def test_a_changelog_run_matches_places_with_no_link_to_added_or_changed_overture_places(change):
    result = run([place()], [ov(change=change)], changelog_only=True)
    assert result.links == {A: {"gers": G1, "release": RELEASE}}
    assert result.records[A]["facts"]["website"] == "https://riverbend.example.au/"


@pytest.mark.parametrize("change", ["unchanged", None])
def test_a_changelog_run_does_not_match_places_with_no_link_to_unchanged_overture_places(change):
    result = run([place()], [ov(change=change)], changelog_only=True)
    assert result.links == {} and result.records == {}
    unnamed = place(name=None)
    assert run([unnamed], [ov(change=change)], changelog_only=True).records == {}


def test_a_matched_overture_place_is_never_a_candidate():
    # The candidate step (test_overture_candidates.py) drops anything next to a place.
    assert run([place()], [ov()]).candidates == {"strong": [], "weak": []}


# ------------------------------------------------------------------ command


def write_repo(repo, records):
    for name in ("overture/categories.json", "overture/directory-hosts.json", "overture/do-not-match.json"):
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_bytes((REPO / name).read_bytes())
    for record in records:
        path = place_path(repo, record["id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dump_record(record), encoding="utf-8")


def test_the_command_writes_places_the_link_file_a_report_and_a_summary(repo, tmp_path):
    differs = place(B, name="Main Road Roadhouse", kind="fuel", metres_north=5000, facts={"phone": "+61 8 5550 9999"})
    write_repo(repo, [place(), differs])
    rows = [ov(addresses=[{"freeform": "12 River St", "locality": "Bendigo Creek", "postcode": "2640", "region": "NSW", "country": "AU"}],
               socials=["https://www.facebook.com/riverbendpark"]),
            ov(G2, name="Main Road Roadhouse", category="gas_station", metres_north=5010, websites=[], phones=["+61 8 5550 0303"])]
    source = tmp_path / "overture.ndjson"
    source.write_text("".join(json.dumps(line) + "\n" for line in extract(*rows)), encoding="utf-8")
    report, summary = tmp_path / "report.md", tmp_path / "summary.json"

    assert main([str(source), "--root", str(repo), "--report", str(report), "--summary", str(summary), "--now", RUN]) == 0

    record = json.loads(place_path(repo, A).read_text(encoding="utf-8"))
    assert record["facts"]["address"]["street"] == "River St"
    assert (repo / "overture" / "links.ndjson").read_text(encoding="utf-8") == (
        f'{{"gers":"{G1}","place":"{A}","release":"{RELEASE}"}}\n'
        f'{{"gers":"{G2}","place":"{B}","release":"{RELEASE}"}}\n'
    )
    text = report.read_text(encoding="utf-8")
    assert "| Facebook | Instagram | TikTok | X |" in text
    assert "## Overture differs" in text and "+61 8 5550 0303" in text and "+61 8 5550 9999" in text
    counts = json.loads(summary.read_text(encoding="utf-8"))
    assert counts["release"] == RELEASE
    assert counts["changed"] == 1 and counts["differs"] == 1
    assert counts["kinds"]["caravan_park"] == {
        "places": 1,
        "before": {"name": 1, "website": 0, "phone": 0, "address": 0,
                   "facebook": 0, "instagram": 0, "tiktok": 0, "x": 0},
        "after": {"name": 1, "website": 1, "phone": 1, "address": 1,
                  "facebook": 1, "instagram": 0, "tiktok": 0, "x": 0},
    }
    assert validate_repo(repo, now=RUN) == []

    # A second run, changelog only, changes nothing.
    assert main([str(source), "--root", str(repo), "--report", str(report), "--changelog", "--now", "2026-12-01T00:00:00Z"]) == 0
    assert json.loads(place_path(repo, A).read_text(encoding="utf-8")) == record
