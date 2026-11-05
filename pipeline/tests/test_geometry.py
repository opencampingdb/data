"""Tests for the opposite-sides rules: pipeline.geometry, and how duplicates,
maproulette, merge and split use them."""

import csv
import json
from pathlib import Path

import pytest

from pipeline.common import load_distinct, load_places, place_path
from pipeline.duplicates import MAX_DISTANCE_M, find_duplicates
from pipeline.extract import extract_file
from pipeline.geometry import SidecarBuilder, is_major_road, judge, load_geometry
from pipeline.maproulette import DOUBLE_MAPPING, write_challenges
from pipeline.merge import MergeError, merge
from pipeline.split import review_path, split_all
from pipeline.sync import sync
from pipeline.validate import validate_repo

FIXTURE = Path(__file__).parent / "fixtures" / "opposite_sides.osm"
T1 = "2026-10-01T16:00:00Z"
T2 = "2026-10-02T16:00:00Z"
RS = "\x1e"

# OSM objects in the fixture.
IRONBARK_WEST, IRONBARK_EAST = "node/9003", "node/9004"  # case 1: either side of a trunk road
RED_GUM_PIN, RED_GUM_AREA = "node/9012", "way/9201"  # case 2: pin on a car park inside the area
YELLOW_BOX_PIN, YELLOW_BOX_AREA = "node/9020", "way/9203"  # case 3: free pin inside the area
BOX_FLAT_AREA, BOX_FLAT_NEAR, BOX_FLAT_ACROSS = "way/9204", "node/9025", "node/9028"


@pytest.fixture(scope="module")
def extracted(tmp_path_factory):
    """The fixture's places and its loaded geometry sidecar."""
    sidecar = SidecarBuilder()
    places = extract_file(FIXTURE, sidecar=sidecar)
    path = tmp_path_factory.mktemp("geometry") / "geometry.ndjson"
    sidecar.write(path, places)
    return places, load_geometry(path), path


def by_ref(places):
    return {f"{p['osm']['type']}/{p['osm']['id']}": p for p in places}


def verdict(extracted, a, b):
    places, geometry, _ = extracted
    found = by_ref(places)
    return judge(found[a], found[b], geometry, MAX_DISTANCE_M)


# ------------------------------------------------------------------ sidecar


def test_sidecar_holds_outlines_signals_and_major_roads_only(extracted):
    _, geometry, path = extracted
    assert set(geometry.areas) == {RED_GUM_AREA, YELLOW_BOX_AREA, BOX_FLAT_AREA}
    assert geometry.signals[RED_GUM_PIN] == {"operator": "Example Roads"}
    assert geometry.signals["node/9037"] == {"bound": ["north"]}
    assert {road.id for road in geometry.roads} == {9101, 9102, 9103, 9104}
    # Place records do not change: the sidecar is a separate file.
    assert all(set(p) == {"osm", "kind", "name", "location", "facts"} for p in extracted[0])
    assert path.read_text(encoding="utf-8").endswith("\n")


def test_extract_is_the_same_with_or_without_a_sidecar(extracted):
    assert extract_file(FIXTURE) == extracted[0]


def test_major_roads():
    assert is_major_road({"highway": "trunk"})
    assert is_major_road({"highway": "secondary_link"})
    assert is_major_road({"highway": "tertiary", "oneway": "-1"})
    assert not is_major_road({"highway": "tertiary"})
    assert not is_major_road({"highway": "residential", "oneway": "yes"})


# ------------------------------------------------------------------ rule 1


def test_pin_on_a_car_park_inside_the_area_is_the_same_place(extracted):
    v = verdict(extracted, RED_GUM_PIN, RED_GUM_AREA)
    assert v.same and not v.separate and v.gap_m == 0


def test_free_pin_inside_the_area_is_the_same_place(extracted):
    v = verdict(extracted, YELLOW_BOX_PIN, YELLOW_BOX_AREA)
    assert v.same and not v.separate


def test_pin_just_outside_with_no_road_is_the_same_place(extracted):
    v = verdict(extracted, BOX_FLAT_NEAR, BOX_FLAT_AREA)
    assert v.same and 5 < v.gap_m < 20


def test_rule_1_wins_over_a_different_operator(extracted):
    places, geometry, _ = extracted
    found = by_ref(places)
    geometry.signals[RED_GUM_AREA] = {"operator": "Former Roads Authority"}
    try:
        assert judge(found[RED_GUM_PIN], found[RED_GUM_AREA], geometry, MAX_DISTANCE_M).same
    finally:
        del geometry.signals[RED_GUM_AREA]


# ------------------------------------------------------------------ rule 2


def test_two_pins_either_side_of_a_highway_are_separate(extracted):
    v = verdict(extracted, IRONBARK_WEST, IRONBARK_EAST)
    assert v.separate and v.reason == "road"
    assert v.road.label() == "Example Highway (A99, trunk, way/9101)"
    assert 50 < v.gap_m < 80


def test_areas_and_pins_across_a_motorway_are_separate(extracted):
    assert verdict(extracted, RED_GUM_PIN, YELLOW_BOX_PIN).reason == "road"
    assert verdict(extracted, RED_GUM_AREA, YELLOW_BOX_AREA).reason == "road"
    # From the pin to the nearest point of the other area's outline.
    assert verdict(extracted, RED_GUM_PIN, YELLOW_BOX_AREA).reason == "road"


def test_pin_near_an_area_but_across_a_road_is_separate(extracted):
    v = verdict(extracted, BOX_FLAT_ACROSS, BOX_FLAT_AREA)
    assert not v.same and v.reason == "road" and v.road.name == "Example Road"


def test_one_way_tertiary_counts_and_two_way_does_not(extracted):
    assert verdict(extracted, "node/9031", "node/9032").reason == "road"
    assert not verdict(extracted, "node/9035", "node/9036").separate
    assert not verdict(extracted, "node/9045", "node/9046").separate  # residential


# ------------------------------------------------------------------ rule 3


def test_never_merge_signals(extracted):
    assert verdict(extracted, "node/9037", "node/9038").reason == "bound"
    assert verdict(extracted, "node/9039", "node/9040").reason == "operator"
    assert verdict(extracted, "node/9041", "node/9042").reason == "entry"


def test_direction_in_the_name_counts_without_the_sidecar():
    from pipeline.geometry import Geometry

    a = {"osm": {"type": "node", "id": 1}, "name": "Gum Rest Area Westbound", "location": {"lat": -35.0, "lon": 145.0}, "kind": "rest_area", "facts": {}}
    b = dict(a, osm={"type": "node", "id": 2}, name="Gum Rest Area Eastbound", location={"lat": -35.0005, "lon": 145.0})
    assert judge(a, b, Geometry(), MAX_DISTANCE_M).reason == "bound"


def test_operators_that_differ_only_in_form_are_not_a_signal():
    from pipeline.geometry import _operators_differ

    assert not _operators_differ("the discoverygroup", "The Discovery Group")
    assert not _operators_differ("Example Hotel", "The Example Hotel and Tourist Park")
    assert _operators_differ("Example Shire", "Example Roads")


def test_signals_never_make_a_pair(extracted):
    places, geometry, _ = extracted
    found = by_ref(places)
    a = dict(found["node/9039"], name="Myall Rest Area")
    b = dict(found["node/9039"], osm={"type": "node", "id": 1}, name="Smiths Flat", location={"lat": -35.0001, "lon": 146.6})
    geometry.signals["node/1"] = {"operator": "Example Shire"}
    try:
        v = judge(a, b, geometry, MAX_DISTANCE_M)
        assert v.separate and v.reason == "name"
    finally:
        del geometry.signals["node/1"]


# ---------------------------------------------------------- the pipeline


@pytest.fixture
def synced(repo, extracted):
    """repo synced from the fixture, with ids that follow the OSM object."""
    places, _, _ = extracted
    sync(repo, places, now=T1, make_id=iter(make_ids(len(places))).__next__)
    return repo


def make_ids(n):
    # Valid ocdb ids that sort in OSM order.
    return [f"ocdb_01M66ES9{i:018d}" for i in range(n)]


def id_of(repo, ref):
    osm_type, osm_id = ref.split("/")
    for record in load_places(repo).values():
        if record["osm"] and record["osm"]["type"] == osm_type and record["osm"]["id"] == int(osm_id):
            return record["id"]
    raise KeyError(ref)


def pair_ids(pairs):
    return {frozenset((p["a"]["id"], p["b"]["id"])) for p in pairs}


def test_duplicates_leave_out_opposite_sides(synced, extracted):
    _, geometry, _ = extracted
    records = list(load_places(synced).values())
    ironbark = frozenset((id_of(synced, IRONBARK_WEST), id_of(synced, IRONBARK_EAST)))
    red_gum = frozenset((id_of(synced, RED_GUM_PIN), id_of(synced, RED_GUM_AREA)))
    without = pair_ids(find_duplicates(records))
    with_rules = pair_ids(find_duplicates(records, geometry=geometry))
    assert ironbark in without and ironbark not in with_rules
    assert red_gum in without and red_gum in with_rules
    # Their directions are in the description tag, which only the sidecar holds.
    mallee = frozenset((id_of(synced, "node/9037"), id_of(synced, "node/9038")))
    assert mallee in without


def test_sync_report_uses_the_rules(repo, extracted):
    places, geometry, _ = extracted
    result = sync(repo, places, now=T1, geometry=geometry)
    names = {(p["a"]["name"], p["b"]["name"]) for p in result.duplicates}
    assert ("Ironbark Rest Area", "Ironbark Rest Area") not in names
    assert ("Red Gum Rest Area", "Red Gum Rest Area") in names


def tasks(repo):
    lines = (repo / "maproulette" / DOUBLE_MAPPING).read_text(encoding="utf-8").split("\n")[:-1]
    return [json.loads(line[1:]) for line in lines]


def test_no_double_mapping_task_for_opposite_sides(synced, extracted):
    places, geometry, _ = extracted
    merge(synced, id_of(synced, IRONBARK_WEST), id_of(synced, IRONBARK_EAST), now=T2)
    merge(synced, id_of(synced, RED_GUM_PIN), id_of(synced, RED_GUM_AREA), now=T2)
    write_challenges(synced, places)
    assert len(tasks(synced)) == 2
    write_challenges(synced, places, geometry=geometry)
    (task,) = tasks(synced)
    assert task["features"][0]["properties"]["objects"] == f"{RED_GUM_PIN} and {RED_GUM_AREA}"


def test_split_undoes_an_opposite_side_merge(synced, extracted):
    places, geometry, _ = extracted
    west, east = id_of(synced, IRONBARK_WEST), id_of(synced, IRONBARK_EAST)
    pin, area = id_of(synced, RED_GUM_PIN), id_of(synced, RED_GUM_AREA)
    merge(synced, west, east, now=T2)
    merge(synced, pin, area, now=T2)

    rows = split_all(synced, places, geometry, now=T2, today="2026-11-05")

    after = load_places(synced)
    assert "merged_osm" not in after[west]
    assert after[east]["status"] == "open" and "merged_into" not in after[east]  # the tombstone lives again
    assert after[east]["osm"]["id"] == 9004
    assert after[pin]["merged_osm"] == [{"id": 9201, "type": "way"}]  # rule 1 keeps it
    assert load_distinct(synced) == {frozenset((west, east))}
    (pair,) = json.loads((synced / "moderation" / "distinct_places.json").read_text())["pairs"]
    assert pair["reason"] == "direction" and "Example Highway (A99)" in pair["note"]
    assert [(r["old_id"], r["new_id"]) for r in rows] == [(west, east)]
    with open(review_path(synced), encoding="utf-8", newline="") as f:
        (row,) = list(csv.DictReader(f))
    assert row["old_osm"] == IRONBARK_WEST and row["new_osm"] == IRONBARK_EAST
    assert row["road"] == "Example Highway (A99, trunk, way/9101)"
    assert 50 < int(row["distance_m"]) < 80
    assert validate_repo(synced) == []

    # Once is enough.
    assert split_all(synced, places, geometry, now=T2) == []


def test_split_makes_a_new_record_when_there_is_no_tombstone(synced, extracted):
    places, geometry, _ = extracted
    west, east = id_of(synced, IRONBARK_WEST), id_of(synced, IRONBARK_EAST)
    merge(synced, west, east, now=T2)
    place_path(synced, east).unlink()
    split_all(synced, places, geometry, now=T2, make_id=lambda: "ocdb_01M66ESZZZZZZZZZZZZZZZZZZZ")
    fresh = load_places(synced)["ocdb_01M66ESZZZZZZZZZZZZZZZZZZZ"]
    assert fresh["osm"]["id"] == 9004 and fresh["status"] == "open"
    assert validate_repo(synced) == []


def test_split_leaves_parts_alone(synced, extracted):
    places, geometry, _ = extracted
    west, east = id_of(synced, IRONBARK_WEST), id_of(synced, IRONBARK_EAST)
    merge(synced, west, east, now=T2, part=True)
    assert split_all(synced, places, geometry, now=T2) == []


def test_merge_refuses_a_distinct_pair_unless_part(synced, extracted):
    places, geometry, _ = extracted
    west, east = id_of(synced, IRONBARK_WEST), id_of(synced, IRONBARK_EAST)
    merge(synced, west, east, now=T2)
    split_all(synced, places, geometry, now=T2)
    with pytest.raises(MergeError, match="separate places"):
        merge(synced, west, east, now=T2)
    kept = merge(synced, west, east, now=T2, part=True)
    assert kept["merged_osm"] == [{"id": 9004, "part": True, "type": "node"}]


def test_validate_allows_only_the_known_moderation_files(synced):
    (synced / "moderation").mkdir(exist_ok=True)
    review_path(synced).write_text("old_id\n", encoding="utf-8")
    assert validate_repo(synced) == []
    (synced / "moderation" / "other.csv").write_text("x\n", encoding="utf-8")
    assert any("unexpected file" in e for e in validate_repo(synced))
