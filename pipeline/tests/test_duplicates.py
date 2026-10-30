"""Tests for pipeline.duplicates (finding likely duplicates) and pipeline.merge."""

import gzip
import json
from pathlib import Path

import pytest

from pipeline.common import dump_line, dump_record, load_places, place_path
from pipeline.duplicates import find_duplicates, normalise_name
from pipeline.extract import extract_file
from pipeline.merge import MergeError, merge
from pipeline.release import build_release
from pipeline.sync import render_report, sync
from pipeline.validate import validate_repo

DUPLICATES = Path(__file__).parent / "fixtures" / "duplicates.osm"
T1 = "2026-10-01T16:00:00Z"
T2 = "2026-10-02T16:00:00Z"
T3 = "2026-10-03T16:00:00Z"

# About 1 m of latitude, in degrees.
M = 1 / 111_320


def place(place_id, kind, name, lat=-35.0, lon=145.0, group=None, status="open"):
    from pipeline.common import KIND_GROUP

    return {
        "id": place_id,
        "name": name,
        "kind": kind,
        "group": group or KIND_GROUP[kind],
        "location": {"lat": lat, "lon": lon},
        "osm": None,
        "facts": {},
        "status": status,
        "rating": None,
        "fact_sources": {},
        "updated": T1,
    }


def ids(pairs):
    return {(p["a"]["id"], p["b"]["id"]) for p in pairs}


# ---------------------------------------------------------------- detection


def test_name_normalisation_drops_generic_words():
    assert normalise_name("The Riverside Caravan & Holiday Park") == "riverside"
    assert normalise_name("Lake Bend Camping Area") == "lake bend"
    assert normalise_name(None) is None


def test_overnight_places_pair_across_kinds():
    a = place("ocdb_A", "caravan_park", "Riverside Caravan Park")
    b = place("ocdb_B", "paid_campground", "Riverside Holiday Park", lat=-35.0 - 100 * M)
    assert ids(find_duplicates([a, b])) == {("ocdb_A", "ocdb_B")}


def test_other_groups_pair_only_within_one_kind():
    a = place("ocdb_A", "lookout", "Big View")
    b = place("ocdb_B", "attraction", "Big View")
    assert find_duplicates([a, b]) == []
    c = place("ocdb_C", "lookout", "Big View", lat=-35.0 - 20 * M)
    assert ids(find_duplicates([a, c])) == {("ocdb_A", "ocdb_C")}


def test_too_far_apart_is_not_a_pair():
    a = place("ocdb_A", "free_camp", "Lake Bend")
    b = place("ocdb_B", "free_camp", "Lake Bend", lat=-35.0 - 250 * M)
    assert find_duplicates([a, b]) == []


def test_different_names_are_not_a_pair():
    a = place("ocdb_A", "free_camp", "Lake Bend")
    b = place("ocdb_B", "free_camp", "Smiths Flat", lat=-35.0 - 20 * M)
    assert find_duplicates([a, b]) == []


def test_unnamed_place_pairs_only_within_50_m():
    a = place("ocdb_A", "free_camp", "Lake Bend")
    near = place("ocdb_B", "free_camp", None, lat=-35.0 - 40 * M)
    far = place("ocdb_C", "free_camp", None, lat=-35.0 + 120 * M)
    assert ids(find_duplicates([a, near, far])) == {("ocdb_A", "ocdb_B")}


def test_two_unnamed_utility_places_are_never_paired():
    a = place("ocdb_A", "public_toilet", None)
    b = place("ocdb_B", "public_toilet", None, lat=-35.0 - 5 * M)
    assert find_duplicates([a, b]) == []


def test_two_unnamed_places_in_other_groups_pair_within_50_m():
    a = place("ocdb_A", "lookout", None)
    b = place("ocdb_B", "lookout", None, lat=-35.0 - 5 * M)
    assert ids(find_duplicates([a, b])) == {("ocdb_A", "ocdb_B")}


def test_tombstones_are_never_paired():
    a = place("ocdb_A", "free_camp", "Lake Bend")
    b = place("ocdb_B", "free_camp", "Lake Bend", status="merged")
    b["merged_into"] = "ocdb_A"
    assert find_duplicates([a, b]) == []


def test_pair_carries_its_distance():
    a = place("ocdb_A", "free_camp", "Lake Bend")
    b = place("ocdb_B", "free_camp", "Lake Bend", lat=-35.0 - 100 * M)
    (pair,) = find_duplicates([a, b])
    assert round(pair["distance_m"]) == 100


def test_node_and_way_of_one_camp_are_detected_by_sync(repo):
    result = sync(repo, extract_file(DUPLICATES), now=T1)
    pairs = result.duplicates
    assert len(pairs) == 1
    kinds = {pairs[0]["a"]["osm"]["type"], pairs[0]["b"]["osm"]["type"]}
    assert kinds == {"node", "way"}
    report = render_report(result)
    assert "## Possible duplicates" in report
    assert pairs[0]["a"]["id"] in report and pairs[0]["b"]["id"] in report
    assert "Lake Bend Camp" in report and "Lake Bend Campground" in report
    # Sync only flags. Both records are still there and open.
    assert all(r["status"] == "open" for r in load_places(repo).values())


# ---------------------------------------------------------------- merge


def write(repo, record):
    path = place_path(repo, record["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_record(record), encoding="utf-8", newline="\n")


def review(review_id, place_id, stars):
    return {
        "id": review_id,
        "place": place_id,
        "stars": stars,
        "text": "Quiet spot by the water.",
        "visited": "2026-09",
        "created": "2026-10-01T10:00:00Z",
        "app": "camporacle",
    }


def write_reviews(repo, place_id, reviews):
    path = repo / "reviews" / f"{place_id}.ndjson"
    path.write_text("".join(dump_line(r) + "\n" for r in reviews), encoding="utf-8", newline="\n")


@pytest.fixture
def pair(repo):
    """The node and the way of one camp, synced, with feedback on the way."""
    sync(repo, extract_file(DUPLICATES), now=T1)
    records = {(r["osm"]["type"], r["osm"]["id"]): r for r in load_places(repo).values()}
    keep = records[("node", 8001)]
    drop = records[("way", 8101)]

    drop["facts"]["power"] = True
    drop["fact_sources"]["power"] = "feedback"
    drop["facts"]["showers"] = False
    drop["fact_sources"]["showers"] = "contributed"
    drop["facts"]["toilets"] = True
    drop["fact_sources"]["toilets"] = "osm"
    keep["facts"]["showers"] = True
    keep["fact_sources"]["showers"] = "feedback"
    keep["rating"] = {"count": 1, "mean": 5.0}
    drop["rating"] = {"count": 2, "mean": 3.5}
    write(repo, keep)
    write(repo, drop)
    write_reviews(repo, keep["id"], [review("rev_01JBX0000000000000000000K1", keep["id"], 5)])
    write_reviews(repo, drop["id"], [
        review("rev_01JBX0000000000000000000D1", drop["id"], 3),
        review("rev_01JBX0000000000000000000D2", drop["id"], 4),
    ])
    assert validate_repo(repo) == []
    return repo, keep, drop


def test_merge_moves_feedback_facts_and_reviews(pair):
    repo, keep, drop = pair
    merge(repo, keep["id"], drop["id"], now=T2)
    places = load_places(repo)
    kept = places[keep["id"]]

    # Feedback and contributed facts move only where the kept record has none.
    assert kept["facts"]["power"] is True
    assert kept["fact_sources"]["power"] == "feedback"
    assert kept["facts"]["showers"] is True
    assert kept["fact_sources"]["showers"] == "feedback"
    # OSM facts of the dropped record do not move.
    assert "toilets" not in kept["facts"]

    assert kept["merged_osm"] == [{"type": "way", "id": 8101}]
    assert kept["updated"] == T2

    lines = (repo / "reviews" / f"{keep['id']}.ndjson").read_text(encoding="utf-8").splitlines()
    moved = [json.loads(line) for line in lines]
    assert [r["id"] for r in moved] == sorted(r["id"] for r in moved)
    assert {r["place"] for r in moved} == {keep["id"]}
    assert len(moved) == 3
    assert kept["rating"] == {"count": 3, "mean": 4.0}
    assert not (repo / "reviews" / f"{drop['id']}.ndjson").exists()
    assert validate_repo(repo) == []


def test_merge_leaves_a_tombstone(pair):
    repo, keep, drop = pair
    merge(repo, keep["id"], drop["id"], now=T2)
    tombstone = load_places(repo)[drop["id"]]
    expected = dict(drop, status="merged", merged_into=keep["id"])
    assert tombstone == expected


def test_resync_after_merge_creates_nothing_new(pair):
    repo, keep, drop = pair
    merge(repo, keep["id"], drop["id"], now=T2)
    before = {p.name: p.read_bytes() for p in (repo / "places").rglob("*.json")}
    result = sync(repo, extract_file(DUPLICATES), now=T3)
    assert result.new == []
    assert result.updated == []
    assert result.gone == []
    assert result.duplicates == []
    after = {p.name: p.read_bytes() for p in (repo / "places").rglob("*.json")}
    assert after == before


def test_tombstone_is_never_revived(pair):
    repo, keep, drop = pair
    merge(repo, keep["id"], drop["id"], now=T2)
    # Even if the kept record's own object vanished, the way stays with it.
    result = sync(repo, [r for r in extract_file(DUPLICATES) if r["osm"]["id"] != 8001], now=T3)
    assert result.new == []
    assert load_places(repo)[drop["id"]]["status"] == "merged"


def test_release_leaves_tombstones_out_and_lists_them(pair, tmp_path):
    repo, keep, drop = pair
    merge(repo, keep["id"], drop["id"], now=T2)
    manifest = build_release(repo, tmp_path / "dist", "20261030T160000Z-abc1234", T2)
    places = [
        json.loads(line)
        for line in gzip.decompress((tmp_path / "dist" / "places.ndjson.gz").read_bytes()).splitlines()
    ]
    assert drop["id"] not in {p["id"] for p in places}
    merged = [
        json.loads(line)
        for line in gzip.decompress((tmp_path / "dist" / "merged.ndjson.gz").read_bytes()).splitlines()
    ]
    assert merged == [{"id": drop["id"], "merged_into": keep["id"]}]
    assert manifest["merged"]["file"] == "merged.ndjson.gz"
    assert manifest["merged"]["count"] == 1
    assert manifest["merged"]["url"].endswith("/merged.ndjson.gz")
    assert manifest["places"]["count"] == len(places)


def test_merge_into_a_place_that_was_itself_merged_redirects_old_tombstones(pair, repo):
    _, keep, drop = pair
    merge(repo, keep["id"], drop["id"], now=T2)
    third = place("ocdb_01JBX0000000000000000000T3", "free_camp", "Lake Bend")
    write(repo, third)
    merge(repo, third["id"], keep["id"], now=T3)
    places = load_places(repo)
    assert places[keep["id"]]["merged_into"] == third["id"]
    # The older tombstone now points straight at the place that is left.
    assert places[drop["id"]]["merged_into"] == third["id"]
    assert places[third["id"]]["merged_osm"] == [
        {"type": "node", "id": 8001},
        {"type": "way", "id": 8101},
    ]
    assert validate_repo(repo) == []


def test_merge_refuses_bad_requests(pair):
    repo, keep, drop = pair
    with pytest.raises(MergeError):
        merge(repo, keep["id"], keep["id"], now=T2)
    with pytest.raises(MergeError):
        merge(repo, keep["id"], "ocdb_01JBX0000000000000000000ZZ", now=T2)
    merge(repo, keep["id"], drop["id"], now=T2)
    with pytest.raises(MergeError):
        merge(repo, keep["id"], drop["id"], now=T3)
    with pytest.raises(MergeError):
        merge(repo, drop["id"], keep["id"], now=T3)


# ---------------------------------------------------------------- validation


def test_validate_tombstone_rules(pair):
    repo, keep, drop = pair
    merge(repo, keep["id"], drop["id"], now=T2)
    path = place_path(repo, drop["id"])
    tombstone = json.loads(path.read_text(encoding="utf-8"))

    broken = dict(tombstone, merged_into="ocdb_01JBX0000000000000000000ZZ")
    path.write_text(dump_record(broken), encoding="utf-8", newline="\n")
    assert any("merged_into" in e for e in validate_repo(repo))

    broken = dict(tombstone)
    del broken["merged_into"]
    path.write_text(dump_record(broken), encoding="utf-8", newline="\n")
    assert any("merged_into" in e for e in validate_repo(repo))

    broken = dict(tombstone, status="open")
    path.write_text(dump_record(broken), encoding="utf-8", newline="\n")
    assert any("merged_into" in e for e in validate_repo(repo))


def test_validate_refuses_an_osm_object_claimed_twice(pair):
    repo, keep, drop = pair
    merge(repo, keep["id"], drop["id"], now=T2)
    other = place("ocdb_01JBX0000000000000000000T4", "free_camp", "Elsewhere", lat=-36.0)
    other["osm"] = {"type": "way", "id": 8101, "version": 2}
    write(repo, other)
    assert any("same OSM object" in e for e in validate_repo(repo))
