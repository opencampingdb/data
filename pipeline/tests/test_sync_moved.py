"""Tests for pipeline.sync: a record follows its place when OSM draws it again as another object."""

import json

from shapely.geometry import Polygon

from pipeline.common import place_path
from pipeline.geometry import Geometry, Road
from pipeline.sync import render_report, sync

T1 = "2026-10-01T16:00:00Z"
T2 = "2026-10-02T16:00:00Z"
T3 = "2026-10-03T16:00:00Z"
LAT, LON = -36.5, 146.7
M = 1 / 111_000  # about a metre of latitude


def obj(osm_type, osm_id, name, lat=LAT, lon=LON, kind="caravan_park", version=1, facts=None):
    return {
        "name": name,
        "kind": kind,
        "location": {"lat": round(lat, 7), "lon": round(lon, 7)},
        "osm": {"type": osm_type, "id": osm_id, "version": version},
        "facts": facts or {"fee": True},
    }


def outline(lat, lon, half_m):
    d = half_m * M
    return Polygon([(lon - d, lat - d), (lon + d, lat - d), (lon + d, lat + d), (lon - d, lat + d), (lon - d, lat - d)])


def record_of(repo, place_id):
    return json.loads(place_path(repo, place_id).read_text(encoding="utf-8"))


def all_records(repo):
    return [json.loads(p.read_text(encoding="utf-8")) for p in (repo / "places").rglob("*.json")]


def first_sync(repo, *objects):
    result = sync(repo, list(objects), now=T1)
    return {(r["osm"]["type"], r["osm"]["id"]): r["id"] for r in result.new}


def add_feedback(repo, place_id):
    """A traveller's fact on the record, which must survive the move."""
    rec = record_of(repo, place_id)
    rec["facts"]["power"] = True
    rec["fact_sources"]["power"] = "feedback"
    rec["fact_dates"] = {"power": T1}
    place_path(repo, place_id).write_text(json.dumps(rec, sort_keys=True, indent=2) + "\n", encoding="utf-8")




def test_node_replaced_by_way_keeps_the_id_and_mints_no_second_record(repo):
    ids = first_sync(repo, obj("node", 10, "Lakeside Caravan Park"))
    place_id = ids[("node", 10)]
    add_feedback(repo, place_id)
    # The node is deleted and the park drawn as an outline round where it stood, its centre 300 m off.
    way = obj("way", 20, "Lakeside Caravan Park", lat=LAT + 300 * M, version=1)
    geometry = Geometry(areas={"way/20": outline(LAT + 300 * M, LON, 400)})
    result = sync(repo, [way], now=T2, geometry=geometry)

    assert result.gone == [] and result.new == []
    assert [(m["record"]["id"], m["from"]["id"]) for m in result.moved] == [(place_id, 10)]
    rec = record_of(repo, place_id)
    assert rec["osm"] == {"type": "way", "id": 20, "version": 1}
    assert rec["merged_osm"] == [{"type": "node", "id": 10}]
    assert rec["location"] == way["location"]
    assert rec["facts"]["power"] is True and rec["fact_sources"]["power"] == "feedback"
    assert rec["updated"] == T2
    assert len(all_records(repo)) == 1

    report = render_report(result)
    assert "| Moved to a new OSM object | 1 |" in report
    assert "[node/10](https://www.openstreetmap.org/node/10)" in report
    assert "[way/20](https://www.openstreetmap.org/way/20)" in report

    # The next run follows the way, and changes nothing.
    again = sync(repo, [way], now=T3, geometry=geometry)
    assert again.moved == [] and again.new == [] and again.gone == [] and again.updated == []


def test_a_node_whose_tags_were_taken_off_moves_to_the_way(repo):
    # Without tags the node leaves the extract, as a deleted one does. The way's name differs only in generic words.
    ids = first_sync(repo, obj("node", 10, "Lakeside Caravan Park"))
    result = sync(repo, [obj("way", 20, "Lakeside Holiday Park", lat=LAT + 40 * M)], now=T2)
    assert [m["record"]["id"] for m in result.moved] == [ids[("node", 10)]]
    assert result.new == []


def test_the_old_node_coming_back_makes_no_second_record(repo):
    ids = first_sync(repo, obj("node", 10, "Lakeside Caravan Park"))
    sync(repo, [obj("way", 20, "Lakeside Caravan Park", lat=LAT + 40 * M)], now=T2)
    result = sync(repo, [obj("way", 20, "Lakeside Caravan Park", lat=LAT + 40 * M), obj("node", 10, "Lakeside Caravan Park", version=2)], now=T3)
    assert result.new == []
    assert [r["id"] for r in all_records(repo)] == [ids[("node", 10)]]


def test_a_record_stays_gone_without_one_clear_match(repo):
    ids = first_sync(
        repo,
        obj("node", 10, "Lakeside Caravan Park"),
        obj("node", 11, "Hilltop Rest Area", lat=LAT + 0.05, kind="rest_area"),
        obj("node", 12, "Creek Camp", lat=LAT + 0.1, kind="free_camp"),
        obj("node", 13, "Riverside Park", lat=LAT + 0.15),
    )
    geometry = Geometry()
    geometry.add_road(Road(7, "trunk", "Example Highway"), [(LON - 0.01, LAT + 0.15 + 50 * M), (LON + 0.01, LAT + 0.15 + 50 * M)])
    extract = [
        # Two objects with the park's name: a person decides.
        obj("way", 20, "Lakeside Caravan Park", lat=LAT + 40 * M),
        obj("way", 21, "Lakeside Caravan Park", lat=LAT - 40 * M),
        # Another name.
        obj("way", 22, "Ridge Rest Area", lat=LAT + 0.05 + 40 * M, kind="rest_area"),
        # The same name, but too far away.
        obj("way", 23, "Creek Camp", lat=LAT + 0.1 + 500 * M, kind="free_camp"),
        # The same name, across a major road.
        obj("way", 24, "Riverside Park", lat=LAT + 0.15 + 100 * M),
    ]
    result = sync(repo, extract, now=T2, geometry=geometry)
    assert result.moved == []
    assert sorted(r["id"] for r in result.gone) == sorted(ids.values())
    assert len(result.new) == 5


def test_two_gone_records_matching_one_object_both_stay_gone(repo):
    first_sync(repo, obj("node", 10, "Lakeside Caravan Park"), obj("node", 11, "Lakeside Caravan Park", lat=LAT + 80 * M))
    result = sync(repo, [obj("way", 20, "Lakeside Caravan Park", lat=LAT + 40 * M)], now=T2)
    assert result.moved == [] and len(result.gone) == 2 and len(result.new) == 1


def test_an_object_that_already_has_a_record_is_not_taken(repo):
    ids = first_sync(repo, obj("node", 10, "Lakeside Caravan Park"), obj("way", 20, "Lakeside Caravan Park", lat=LAT + 40 * M))
    result = sync(repo, [obj("way", 20, "Lakeside Caravan Park", lat=LAT + 40 * M)], now=T2)
    assert result.moved == []
    assert [r["id"] for r in result.gone] == [ids[("node", 10)]]


def test_an_unnamed_record_never_moves(repo):
    first_sync(repo, obj("node", 10, None, kind="public_toilet", facts={}))
    result = sync(repo, [obj("way", 20, None, lat=LAT + 10 * M, kind="public_toilet", facts={})], now=T2)
    assert result.moved == [] and len(result.gone) == 1
