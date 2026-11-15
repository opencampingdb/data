"""Tests for the automatic merge rule (pipeline.duplicates.automatic_merge) and
the batch that applies it (python -m pipeline.merge --auto)."""

import csv
import json

import pytest

from pipeline.common import KIND_GROUP, dump_record, load_places, place_path
from pipeline.duplicates import automatic_merge, tidy_name
from pipeline.geometry import Geometry, Road
from pipeline.merge import AUTO_MERGES_FILE, merge_batch
from pipeline.validate import validate_repo

T1 = "2026-10-01T16:00:00Z"
NOW = "2026-11-05T00:00:00Z"
M = 1 / 111_320  # about 1 m of latitude, in degrees
LAT, LON = -35.0, 145.0


def place(place_id, kind, name, metres_north=0.0, osm_id=None, facts=None, **changes):
    facts = facts or {}
    record = {
        "id": place_id,
        "name": name,
        "kind": kind,
        "group": KIND_GROUP[kind],
        "location": {"lat": round(LAT + metres_north * M, 6), "lon": LON},
        "osm": {"type": "node", "id": osm_id, "version": 1} if osm_id else None,
        "facts": facts,
        "status": "open",
        "rating": None,
        "fact_sources": {k: "osm" for k in facts},
        "updated": T1,
    }
    record.update(changes)
    return record


A = "ocdb_01M66CKP2X55A8CVGR95ZFJ1A0"
B = "ocdb_01M66CKP2X55A8CVGR95ZFJ1B0"
C = "ocdb_01M66CKP2X55A8CVGR95ZFJ1C0"


@pytest.mark.parametrize("a,b", [
    ("Dalmeny Caravan & Camping Ground", "Dalmeny Caravan And Camping Ground"),
    ("Calliope River RA", "Calliope River Rest Area"),
    ("Lucky Bay Campground", "Lucky Bay Caravan Park"),
    ("Moon Ck Camp", "Moon Creek Camping Area"),
])
def test_tidied_names_match(a, b):
    assert tidy_name(a) == tidy_name(b)


@pytest.mark.parametrize("a,b", [
    ("Site 20", "Site 26"),
    ("Moon Creek", "Moon River"),
    ("Riverside Northbound", "Riverside Southbound"),
])
def test_tidied_names_differ(a, b):
    assert tidy_name(a) != tidy_name(b)


def test_same_name_same_group_close_and_no_road_merges():
    a = place(A, "caravan_park", "Lucky Bay Caravan Park", osm_id=1)
    b = place(B, "national_park_campsite", "Lucky Bay Campground", 30, osm_id=2)
    assert automatic_merge(a, b, Geometry()) is None


@pytest.mark.parametrize("b,why", [
    (place(B, "caravan_park", "Lucky Bay", 55, osm_id=2), "distance"),
    (place(B, "rest_area", "Lucky Bay", 30, osm_id=2), "group"),
    (place(B, "caravan_park", "Lucky Beach", 30, osm_id=2), "name"),
    (place(B, "caravan_park", None, 10, osm_id=2), "name"),
    (place(B, "caravan_park", "Caravan Park", 10, osm_id=2), "name"),
    (place(B, "caravan_park", "Lucky Bay", 10, osm_id=2, status="merged", merged_into=C), "merged"),
    (place(B, "caravan_park", "Lucky Bay", 10, osm_id=2, owner_verified=True), "owner"),
    (place(B, "caravan_park", "Lucky Bay", 10, osm_id=2, facts={"entry": "private"}), "entry"),
])
def test_other_pairs_wait_for_a_reviewer(b, why):
    a = place(A, "caravan_park", "Lucky Bay Caravan Park", osm_id=1, facts={"entry": "public"})
    reason = automatic_merge(a, b, Geometry())
    assert reason is not None and why in reason, reason


def test_a_major_road_between_keeps_them_apart():
    geometry = Geometry()
    geometry.add_road(Road(7, "trunk", "Example Highway"), [(LON - 0.01, LAT + 15 * M), (LON + 0.01, LAT + 15 * M)])
    a = place(A, "rest_area", "Ironbark Rest Area", osm_id=1)
    b = place(B, "rest_area", "Ironbark Rest Area", 30, osm_id=2)
    reason = automatic_merge(a, b, geometry)
    assert reason is not None and "road" in reason


def test_a_pair_on_the_distinct_list_is_never_merged():
    a = place(A, "caravan_park", "Lucky Bay", osm_id=1)
    b = place(B, "caravan_park", "Lucky Bay", 10, osm_id=2)
    assert "distinct" in automatic_merge(a, b, Geometry(), distinct={frozenset((A, B))})


def write(repo, record):
    path = place_path(repo, record["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_record(record), encoding="utf-8", newline="\n")


def test_the_batch_merges_each_group_into_one_place_and_lists_every_merge(repo):
    write(repo, place(A, "campground", "Lucky Bay", osm_id=1))
    write(repo, place(B, "caravan_park", "Lucky Bay Caravan Park", 20, osm_id=2, facts={"power": True}))
    write(repo, place(C, "national_park_campsite", "Lucky Bay Campground", 40, osm_id=3))
    write(repo, place("ocdb_01M66CKP2X55A8CVGR95ZFJ1D0", "free_camp", "Other Place", 5000, osm_id=4))

    merges = merge_batch(repo, Geometry(), now=NOW)

    places = load_places(repo)
    assert [(m["kept"], m["dropped"]) for m in merges] == [(B, A), (B, C)]
    assert places[A]["merged_into"] == B and places[C]["merged_into"] == B
    assert places[B]["merged_osm"] == [{"type": "node", "id": 1}, {"type": "node", "id": 3}]
    with open(repo / AUTO_MERGES_FILE, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert [(r["kept_id"], r["dropped_id"], r["distance_m"], r["merged"]) for r in rows] == [
        (B, A, "20", "2026-11-05"), (B, C, "20", "2026-11-05"),
    ]
    assert rows[0]["kept_name"] == "Lucky Bay Caravan Park" and rows[0]["dropped_osm"] == "node/1"
    assert validate_repo(repo, now=NOW) == []


def test_the_batch_only_merges_the_groups_it_is_given(repo):
    write(repo, place(A, "rest_area", "Ironbark Rest Area", osm_id=1))
    write(repo, place(B, "rest_area", "Ironbark Rest Area", 10, osm_id=2))
    assert merge_batch(repo, Geometry(), now=NOW, groups=("overnight",)) == []
    assert len(merge_batch(repo, Geometry(), now=NOW, groups=("utility",))) == 1


def test_a_second_batch_adds_to_the_review_file(repo):
    write(repo, place(A, "caravan_park", "Lucky Bay", osm_id=1))
    write(repo, place(B, "caravan_park", "Lucky Bay", 10, osm_id=2))
    merge_batch(repo, Geometry(), now=NOW)
    write(repo, place(C, "free_camp", "Moon Creek", 9000, osm_id=3))
    write(repo, place("ocdb_01M66CKP2X55A8CVGR95ZFJ1D0", "free_camp", "Moon Ck", 9010, osm_id=4))
    merge_batch(repo, Geometry(), now="2026-11-06T00:00:00Z")
    with open(repo / AUTO_MERGES_FILE, encoding="utf-8", newline="") as f:
        assert [r["merged"] for r in csv.DictReader(f)] == ["2026-11-05", "2026-11-06"]


def test_a_toilet_named_for_its_rest_area_is_not_the_rest_area():
    a = place(A, "rest_area", "Ironbark Rest Area", osm_id=1)
    b = place(B, "public_toilet", "Ironbark Rest Area", 10, osm_id=2)
    assert "kind" in automatic_merge(a, b, Geometry())


def test_a_motel_merged_into_its_caravan_park_is_a_part(repo):
    write(repo, place(A, "caravan_park", "Tambo Mill Motel and Caravan Park", osm_id=1, facts={"power": True}))
    write(repo, place(B, "roofed_accommodation", "Tambo Mill Motel and Caravan Park", 20, osm_id=2))
    merge_batch(repo, Geometry(), now=NOW)
    assert load_places(repo)[A]["merged_osm"] == [{"type": "node", "id": 2, "part": True}]
    with open(repo / AUTO_MERGES_FILE, encoding="utf-8", newline="") as f:
        assert [r["part"] for r in csv.DictReader(f)] == ["yes"]


def test_two_camp_sites_mapped_twice_are_not_parts(repo):
    write(repo, place(A, "caravan_park", "Dalmeny Caravan & Camping Ground", osm_id=1, facts={"power": True}))
    write(repo, place(B, "campground", "Dalmeny Caravan And Camping Ground", 27, osm_id=2))
    merge_batch(repo, Geometry(), now=NOW)
    assert load_places(repo)[A]["merged_osm"] == [{"type": "node", "id": 2}]


def test_the_caravan_park_is_kept_over_its_motel(repo):
    write(repo, place(A, "roofed_accommodation", "Eromanga Motel & Caravan Park", osm_id=1, facts={"phone": "07 0000 0000", "website": "https://example.com"}))
    write(repo, place(B, "paid_campground", "Eromanga Motel and Caravan Park", 20, osm_id=2))
    [made] = merge_batch(repo, Geometry(), now=NOW)
    assert (made["kept"], made["dropped"]) == (B, A)


def test_a_merged_new_place_keeps_its_draft_record_as_a_tombstone(repo, tmp_path):
    import gzip

    from pipeline.release import build_release

    write(repo, place(A, "free_camp", "Moon Creek", osm_id=1))
    write(repo, place(
        B, "free_camp", "Moon Creek", 10, status="merged", merged_into=A, standing="held",
        fact_sources={"name": "feedback", "location": "feedback"},
        fact_dates={"name": "2026-10-01T00:00:00Z", "location": "2026-10-01T00:00:00Z"},
    ))
    assert validate_repo(repo, now=NOW) == []
    build_release(repo, tmp_path / "dist", data_version="20261030T160000Z-abc1234", generated="2026-10-30T16:00:00Z")
    merged = gzip.decompress((tmp_path / "dist" / "merged.ndjson.gz").read_bytes()).decode("utf-8").splitlines()
    assert [json.loads(line) for line in merged] == [{"id": B, "merged_into": A}]


# An unnamed park and the named place an outline makes it one place with.

def square(metres: float, north: float = 0.0):
    """A square outline about metres across, centred metres north of LAT, LON."""
    from shapely import Polygon

    h, w = metres / 2 * M, metres / 2 * M * 1.22  # a metre of longitude is shorter at 35 S
    c = LAT + north * M
    return Polygon([(LON - w, c - h), (LON + w, c - h), (LON + w, c + h), (LON - w, c + h), (LON - w, c - h)])


def way(record, osm_id):
    return dict(record, osm={"type": "way", "id": osm_id, "version": 1})


def test_an_unnamed_park_drawn_around_a_named_point_merges_into_it():
    geometry = Geometry(areas={"way/9": square(240)})
    unnamed = way(place(A, "caravan_park", None), 9)
    named = place(B, "campground", "Wongan Hills Caravan Park", 90, osm_id=2)
    assert automatic_merge(unnamed, named, geometry) is None


def test_an_unnamed_park_near_a_named_point_outside_its_outline_waits():
    geometry = Geometry(areas={"way/9": square(40)})
    unnamed = way(place(A, "caravan_park", None), 9)
    named = place(B, "campground", "Inland Sea Restaurant", 45, osm_id=2)
    assert "outline" in automatic_merge(unnamed, named, geometry)


def test_two_unnamed_points_close_together_still_wait():
    a = place(A, "caravan_park", None, osm_id=1)
    b = place(B, "caravan_park", "Lucky Bay", 10, osm_id=2)
    assert "outline" in automatic_merge(a, b, Geometry())


@pytest.mark.parametrize("named", [
    place(B, "roofed_accommodation", "Birchip Motel", 30, osm_id=2),  # a named motel keeps its own record
    place(B, "campground", "Lucky Bay", 30, osm_id=None),
    place(B, "rest_area", "Ironbark Rest Area", 30, osm_id=2),
])
def test_an_unnamed_park_does_not_merge_into_a_motel_a_non_osm_place_or_another_group(named):
    geometry = Geometry(areas={"way/9": square(240)})
    unnamed = way(place(A, "caravan_park", None), 9)
    assert automatic_merge(unnamed, named, geometry) is not None


def test_a_road_through_the_outline_keeps_them_apart():
    geometry = Geometry(areas={"way/9": square(40)})
    geometry.add_road(Road(7, "trunk", "Example Highway"), [(LON - 0.01, LAT + 30 * M), (LON + 0.01, LAT + 30 * M)])
    unnamed = way(place(A, "caravan_park", None), 9)
    named = place(B, "caravan_park", "Lucky Bay", 35, osm_id=2)
    assert "road" in automatic_merge(unnamed, named, geometry)


def test_the_batch_keeps_the_named_record_and_lists_the_unnamed_outline_as_mapped_twice(repo):
    geometry = Geometry(areas={"way/9": square(240)})
    write(repo, way(place(A, "caravan_park", None, facts={"power": True, "showers": True}), 9))
    write(repo, place(B, "campground", "Wongan Hills Caravan Park", 90, osm_id=2))
    [made] = merge_batch(repo, geometry, now=NOW)
    assert (made["kept"], made["dropped"]) == (B, A)
    assert load_places(repo)[B]["merged_osm"] == [{"type": "way", "id": 9}]


def test_unnamed_sections_inside_a_named_park_are_merged_as_parts(repo):
    geometry = Geometry(areas={"way/9": square(300), "way/10": square(30, 60), "way/11": square(30, -60)})
    write(repo, way(place(A, "caravan_park", "BIG4 The Bend"), 9))
    write(repo, way(place(B, "caravan_park", None, 60), 10))
    write(repo, way(place(C, "caravan_park", None, -60), 11))
    merges = merge_batch(repo, geometry, now=NOW)
    assert sorted(m["dropped"] for m in merges) == [B, C]
    assert load_places(repo)[A]["merged_osm"] == [{"type": "way", "id": 10, "part": True}, {"type": "way", "id": 11, "part": True}]
    assert validate_repo(repo, now=NOW) == []


def test_an_unnamed_outline_around_two_differently_named_places_waits(repo):
    geometry = Geometry(areas={"way/9": square(300)})
    write(repo, way(place(A, "caravan_park", None), 9))
    write(repo, place(B, "campground", "Moon Creek", 40, osm_id=2))
    write(repo, place(C, "campground", "Sun Valley", -40, osm_id=3))
    assert merge_batch(repo, geometry, now=NOW) == []


def test_an_unnamed_cabin_inside_a_named_park_is_merged_as_a_part(repo):
    geometry = Geometry(areas={"way/9": square(300), "way/10": square(10, 50)})
    write(repo, way(place(A, "caravan_park", "Lucky Bay Caravan Park"), 9))
    write(repo, way(place(B, "roofed_accommodation", None, 50), 10))
    [made] = merge_batch(repo, geometry, now=NOW)
    assert (made["kept"], made["dropped"]) == (A, B)
    assert load_places(repo)[A]["merged_osm"] == [{"type": "way", "id": 10, "part": True}]
