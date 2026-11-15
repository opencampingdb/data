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
from pipeline.overture import OVERTURE_KINDS, KeptLists, import_overture, load_kept_lists, main
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
       websites=None, phones=None, addresses=None, change="unchanged"):
    return {
        "id": gers,
        "change": change,
        "name": name,
        "basic_category": category,
        "taxonomy_primary": category,
        "confidence": confidence,
        "websites": websites if websites is not None else ["https://riverbend.example.au/"],
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


def test_the_kept_directory_host_list_starts_with_placestars_and_holds_social_hosts():
    hosts = load_kept_lists(REPO).directory_hosts
    assert hosts[0] == "placestars.com"
    assert {"facebook.com", "instagram.com", "linktr.ee"} <= set(hosts)
    rows = [ov(websites=["https://m.facebook.com/riverbend", "https://riverbend.example.au"])]
    assert run([place()], rows, lists=load_kept_lists(REPO)).records[A]["facts"]["website"] == "https://riverbend.example.au"


def test_email_socials_brand_operating_status_and_confidence_are_never_taken():
    row = ov(websites=[], phones=[])
    row.update({"emails": ["info@riverbend.example.au"], "socials": ["https://facebook.com/riverbend"],
                "brand": {"names": {"primary": "Big4"}}, "operating_status": "open"})
    result = run([place()], [row])
    assert result.records == {} and result.fills == []
    assert result.links[A]["gers"] == G1


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
    # place counts as unnamed and the name rule asks for 0.8.
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


def test_a_changelog_run_does_not_recheck_a_place_whose_overture_place_is_unchanged():
    # Overture's phone differs, but the changelog says unchanged, so the place is not looked at again.
    for change in ("unchanged", None):
        result = run([filled_park()], [ov(phones=["+61 2 5550 0202"], change=change)], links=LINKED, changelog_only=True)
        assert result.records == {} and result.fills == []
        assert result.links == {A: {"gers": G1, "release": RELEASE}}
    # A full run does.
    result = run([filled_park()], [ov(phones=["+61 2 5550 0202"])], links=LINKED)
    assert result.records[A]["facts"]["phone"] == "+61 2 5550 0202"


@pytest.mark.parametrize("rows,phone", [
    ([ov(phones=["+61 2 5550 0202"], change="data_changed")], "+61 2 5550 0202"),
    ([{"id": G1, "change": "removed"}], None),
    ([], None),
])
def test_a_changelog_run_rechecks_a_changed_or_removed_overture_place(rows, phone):
    result = run([filled_park()], rows, links=LINKED, changelog_only=True)
    assert result.records[A]["facts"].get("phone") == phone


def test_a_changelog_run_still_matches_places_with_no_link():
    result = run([place()], [ov(change="unchanged")], changelog_only=True)
    assert result.links == {A: {"gers": G1, "release": RELEASE}}
    assert result.records[A]["facts"]["website"] == "https://riverbend.example.au/"


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
    rows = [ov(addresses=[{"freeform": "12 River St", "locality": "Bendigo Creek", "postcode": "2640", "region": "NSW", "country": "AU"}]),
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
    assert "## Overture differs" in text and "+61 8 5550 0303" in text and "+61 8 5550 9999" in text
    counts = json.loads(summary.read_text(encoding="utf-8"))
    assert counts["release"] == RELEASE
    assert counts["changed"] == 1 and counts["differs"] == 1
    assert counts["kinds"]["caravan_park"] == {
        "places": 1,
        "before": {"name": 1, "website": 0, "phone": 0, "address": 0},
        "after": {"name": 1, "website": 1, "phone": 1, "address": 1},
    }
    assert validate_repo(repo, now=RUN) == []

    # A second run, changelog only, changes nothing.
    assert main([str(source), "--root", str(repo), "--report", str(report), "--changelog", "--now", "2026-12-01T00:00:00Z"]) == 0
    assert json.loads(place_path(repo, A).read_text(encoding="utf-8")) == record
