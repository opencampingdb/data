"""Tests for pipeline.custody: following a place through OSM's history."""

import json

from pipeline.common import place_path
from pipeline.custody import HISTORY_FILE, OsmHistory
from pipeline.sync import render_report, sync

T1 = "2026-10-01T16:00:00Z"
T2 = "2026-10-02T16:00:00Z"
T3 = "2026-10-03T16:00:00Z"
LAT, LON = -36.5, 146.7
M = 1 / 111_000


def obj(osm_type, osm_id, name, lat=LAT, lon=LON, kind="caravan_park", version=1):
    return {
        "name": name,
        "kind": kind,
        "location": {"lat": round(lat, 7), "lon": round(lon, 7)},
        "osm": {"type": osm_type, "id": osm_id, "version": version},
        "facts": {"fee": True},
    }


def record_of(repo, place_id):
    return json.loads(place_path(repo, place_id).read_text(encoding="utf-8"))


def write(repo, rec):
    place_path(repo, rec["id"]).write_text(json.dumps(rec, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def all_records(repo):
    return [json.loads(p.read_text(encoding="utf-8")) for p in (repo / "places").rglob("*.json")]


def first_sync(repo, *objects):
    result = sync(repo, list(objects), now=T1)
    return {(r["osm"]["type"], r["osm"]["id"]): r["id"] for r in result.new}


class FakeOsm:
    """The OSM API for a few objects and changesets, counting requests."""

    def __init__(self):
        self.histories, self.changesets, self.urls = {}, {}, []

    def node(self, node_id, versions):
        self.histories[f"node/{node_id}"] = json.dumps({"elements": versions})

    def changeset(self, changeset_id, osc, count=3):
        self.changesets[changeset_id] = (osc, count)

    def __call__(self, url):
        self.urls.append(url)
        path = url.split("/api/0.6/")[1]
        if path.endswith("/history.json"):
            return self.histories.get(path[: -len("/history.json")])
        changeset_id = int(path.split("/")[1].split(".")[0])
        if changeset_id not in self.changesets:
            return None
        osc, count = self.changesets[changeset_id]
        return osc if path.endswith("/download") else json.dumps({"changeset": {"changes_count": count}})


def version(v, changeset, tags=None, visible=True):
    return {"version": v, "changeset": changeset, "visible": visible, **({"tags": tags} if tags else {})}


PARK = {"name": "Lakeside Caravan Park", "tourism": "caravan_site"}


def history(repo, fake):
    return OsmHistory(repo / HISTORY_FILE, fetch=fake, pause_s=0)


def test_a_point_merged_into_an_outline_follows_it_by_the_changeset(repo):
    """The point's tags taken off, the outline given them and the point, in one changeset, far from the point."""
    ids = first_sync(repo, obj("node", 10, "Lakeside Caravan Park"))
    place_id = ids[("node", 10)]
    fake = FakeOsm()
    fake.node(10, [version(1, 100, PARK), version(2, 500)])
    fake.changeset(
        500,
        '<osmChange><modify><node id="10" version="2"/></modify>'
        '<modify><way id="20" version="5"><nd ref="9"/><nd ref="10"/>'
        '<tag k="name" v="Lakeside Caravan Park"/><tag k="tourism" v="caravan_site"/></way></modify></osmChange>',
    )
    way = obj("way", 20, "Lakeside Caravan Park", lat=LAT + 900 * M, version=5)
    result = sync(repo, [way], now=T2, history=history(repo, fake))

    assert result.gone == [] and result.new == []
    assert [(m["record"]["id"], m["how"].outcome, m["how"].changeset) for m in result.moved] == [(place_id, "history", 500)]
    rec = record_of(repo, place_id)
    assert rec["osm"] == {"type": "way", "id": 20, "version": 5}
    assert rec["merged_osm"] == [{"type": "node", "id": 10}]
    assert "[changeset 500](https://www.openstreetmap.org/changeset/500)" in render_report(result)
    # A record that follows its place leaves nothing in the history file.
    assert (repo / HISTORY_FILE).read_text() == ""


def test_a_deleted_point_follows_the_outline_its_changeset_named(repo):
    ids = first_sync(repo, obj("node", 10, "Lakeside Caravan Park"))
    fake = FakeOsm()
    fake.node(10, [version(1, 100, PARK), version(2, 501, visible=False)])
    fake.changeset(
        501,
        '<osmChange><delete><node id="10" version="2"/></delete><modify><way id="20" version="2">'
        '<tag k="name" v="Lakeside Caravan Park"/><tag k="tourism" v="caravan_site"/></way></modify></osmChange>',
    )
    result = sync(repo, [obj("way", 20, "Lakeside Caravan Park", lat=LAT + 900 * M)], now=T2, history=history(repo, fake))
    assert [m["record"]["id"] for m in result.moved] == [ids[("node", 10)]]


def test_a_record_follows_its_own_merged_object_with_no_lookup(repo):
    """Open Camping DB merged the outline's record into the point's, then a mapper removed the point."""
    ids = first_sync(repo, obj("node", 10, "Lakeside Caravan Park"), obj("way", 20, "Lakeside Caravan Park", lat=LAT + 40 * M))
    keep, drop = ids[("node", 10)], ids[("way", 20)]
    from pipeline.merge import merge

    merge(repo, keep, drop, now=T1)
    fake = FakeOsm()
    result = sync(repo, [obj("way", 20, "Lakeside Caravan Park", lat=LAT + 40 * M, version=2)], now=T2, history=history(repo, fake))

    assert fake.urls == []
    assert [(m["record"]["id"], m["how"].outcome) for m in result.moved] == [(keep, "own")]
    rec = record_of(repo, keep)
    assert rec["osm"]["id"] == 20 and rec["merged_osm"] == [{"type": "node", "id": 10}]
    assert result.gone == [] and result.new == []
    # The next run finds nothing to do.
    again = sync(repo, [obj("way", 20, "Lakeside Caravan Park", lat=LAT + 40 * M, version=2)], now=T3, history=history(repo, fake))
    assert again.moved == [] and again.updated == [] and again.gone == []


def test_a_record_never_follows_a_part(repo):
    ids = first_sync(repo, obj("node", 10, "Lakeside Caravan Park"))
    rec = record_of(repo, ids[("node", 10)])
    rec["merged_osm"] = [{"type": "way", "id": 20, "part": True}]
    write(repo, rec)
    result = sync(repo, [obj("way", 20, "Lakeside Caravan Park North")], now=T2)
    assert result.moved == [] and len(result.gone) == 1


def test_when_the_object_that_took_over_has_a_record_it_is_merged_into_this_one(repo):
    ids = first_sync(repo, obj("node", 10, "Lakeside Caravan Park"), obj("way", 20, "Lakeside Caravan Park", lat=LAT + 900 * M))
    keep, other = ids[("node", 10)], ids[("way", 20)]
    rec = record_of(repo, keep)
    rec["facts"]["power"] = True
    rec["fact_sources"]["power"] = "feedback"
    rec["fact_dates"] = {"power": T1}
    write(repo, rec)
    (repo / "reviews").mkdir(exist_ok=True)
    fake = FakeOsm()
    fake.node(10, [version(1, 100, PARK), version(2, 502, visible=False)])
    fake.changeset(
        502,
        '<osmChange><delete><node id="10" version="2"/></delete><modify><way id="20" version="2">'
        '<tag k="name" v="Lakeside Caravan Park"/><tag k="tourism" v="caravan_site"/></way></modify></osmChange>',
    )
    result = sync(repo, [obj("way", 20, "Lakeside Caravan Park", lat=LAT + 900 * M, version=2)], now=T2, history=history(repo, fake))

    assert [(m["record"]["id"], m["how"].outcome, m["how"].merged_id) for m in result.moved] == [(keep, "merged", other)]
    kept = record_of(repo, keep)
    assert kept["osm"]["id"] == 20 and kept["facts"]["power"] is True
    assert record_of(repo, other)["status"] == "merged" and record_of(repo, other)["merged_into"] == keep
    assert result.new == [] and result.gone == []


def test_an_owner_listing_is_never_merged_by_the_history(repo):
    ids = first_sync(repo, obj("node", 10, "Lakeside Caravan Park"), obj("way", 20, "Lakeside Caravan Park", lat=LAT + 900 * M))
    rec = record_of(repo, ids[("way", 20)])
    rec["owner_verified"] = True
    write(repo, rec)
    fake = FakeOsm()
    fake.node(10, [version(1, 100, PARK), version(2, 502, visible=False)])
    fake.changeset(
        502,
        '<osmChange><delete><node id="10" version="2"/></delete><modify><way id="20" version="2">'
        '<tag k="name" v="Lakeside Caravan Park"/><tag k="tourism" v="caravan_site"/></way></modify></osmChange>',
    )
    result = sync(repo, [obj("way", 20, "Lakeside Caravan Park", lat=LAT + 900 * M, version=2)], now=T2, history=history(repo, fake))
    assert result.moved == []
    assert [r.outcome for r in result.custody] == ["unclear"]
    assert all(r["status"] == "open" for r in all_records(repo))


def test_closed_retagged_and_deleted_are_listed_for_a_person_with_the_changeset(repo):
    ids = first_sync(
        repo,
        obj("node", 10, "Lakeside Caravan Park"),
        obj("node", 11, "Hilltop Caravan Park", lat=LAT + 0.1),
        obj("node", 12, "Creek Caravan Park", lat=LAT + 0.2),
    )
    fake = FakeOsm()
    fake.node(10, [version(1, 100, PARK), version(2, 600, {"name": "Lakeside Caravan Park", "disused:tourism": "caravan_site"})])
    fake.node(11, [version(1, 100, {"name": "Hilltop", "tourism": "caravan_site"}), version(2, 601, {"name": "Hilltop", "tourism": "hotel"})])
    fake.node(12, [version(1, 100, {"name": "Creek", "tourism": "caravan_site"}), version(2, 602, visible=False)])
    fake.changeset(602, '<osmChange><delete><node id="12" version="2"/></delete></osmChange>')
    result = sync(repo, [], now=T2, history=history(repo, fake))

    said = {r.record_id: (r.outcome, r.changeset) for r in result.custody}
    assert said == {
        ids[("node", 10)]: ("closed_in_osm", 600),
        ids[("node", 11)]: ("retagged", 601),
        ids[("node", 12)]: ("deleted", 602),
    }
    assert len(result.gone) == 3 and result.moved == []
    report = render_report(result)
    assert "closed in OSM (disused:tourism=caravan_site)" in report
    assert "now tagged" in report and "tourism=hotel" in report
    assert "with nothing in its place" in report

    # Each object is looked up once: the history file answers the next run.
    lines = (repo / HISTORY_FILE).read_text().splitlines()
    assert [json.loads(line)["object"] for line in lines] == ["node/10", "node/11", "node/12"]
    asked = len(fake.urls)
    sync(repo, [], now=T3, history=history(repo, fake))
    assert len(fake.urls) == asked


def test_the_history_file_holds_nothing_about_the_mapper(repo):
    first_sync(repo, obj("node", 12, "Creek Caravan Park"))
    fake = FakeOsm()
    fake.node(12, [version(1, 100, {"name": "Creek", "tourism": "caravan_site", "note": "ring Jo"}), {**version(2, 602, visible=False), "user": "someone", "uid": 7}])
    fake.changeset(602, '<osmChange><delete><node id="12" version="2"/></delete></osmChange>')
    sync(repo, [], now=T2, history=history(repo, fake))
    text = (repo / HISTORY_FILE).read_text()
    assert "someone" not in text and '"uid"' not in text and "Jo" not in text


def test_an_api_failure_leaves_the_record_gone_and_unchanged(repo):
    ids = first_sync(repo, obj("node", 10, "Lakeside Caravan Park"))
    before = record_of(repo, ids[("node", 10)])

    def broken(url):
        raise ValueError("bad reply")

    hist = OsmHistory(repo / HISTORY_FILE, fetch=broken, pause_s=0)
    result = sync(repo, [], now=T2, history=hist)
    assert [r.outcome for r in result.custody] == ["not_looked_up"] and hist.failed == 1
    assert record_of(repo, ids[("node", 10)]) == before


DELETED_INTO_WAY_20 = (
    '<osmChange><delete><node id="10" version="2"/><node id="11" version="2"/></delete><modify><way id="20" version="2">'
    '<tag k="name" v="Lakeside Caravan Park"/><tag k="tourism" v="caravan_site"/></way></modify></osmChange>'
)


def test_two_records_whose_points_went_into_one_outline_both_wait_for_a_person(repo):
    ids = first_sync(repo, obj("node", 10, "Lakeside Caravan Park"), obj("node", 11, "Lakeside Caravan Park", lat=LAT + 80 * M))
    before = {i: record_of(repo, i) for i in ids.values()}
    fake = FakeOsm()
    for n in (10, 11):
        fake.node(n, [version(1, 100, PARK), version(2, 700, visible=False)])
    fake.changeset(700, DELETED_INTO_WAY_20)
    way = obj("way", 20, "Lakeside Caravan Park", lat=LAT + 900 * M, version=2)
    for now in (T2, T3):
        result = sync(repo, [way], now=now, history=history(repo, fake))
        assert result.moved == []
        assert sorted(r.outcome for r in result.custody) == ["unclear", "unclear"]
        assert sorted(r["id"] for r in result.gone) == sorted(ids.values())
    assert all(record_of(repo, i) == before[i] for i in ids.values())


def test_a_record_the_schema_refuses_after_a_move_stays_gone_and_nothing_is_merged(repo):
    ids = first_sync(repo, obj("node", 10, "Lakeside Caravan Park"), obj("way", 20, "Lakeside Caravan Park", lat=LAT + 900 * M))
    keep, other = ids[("node", 10)], ids[("way", 20)]
    fake = FakeOsm()
    fake.node(10, [version(1, 100, PARK), version(2, 702, visible=False)])
    fake.changeset(702, DELETED_INTO_WAY_20.replace('<node id="11" version="2"/>', ""))
    bad = dict(obj("way", 20, "Lakeside Caravan Park", lat=LAT + 900 * M, version=2), facts={"fee": "maybe"})
    result = sync(repo, [bad], now=T2, history=history(repo, fake))
    assert result.moved == []
    assert [r.outcome for r in result.custody] == ["refused"]
    assert [r["id"] for r in result.gone] == [keep]
    assert len(result.skipped) == 1 and result.new == []
    assert record_of(repo, other)["status"] == "open" and record_of(repo, keep)["osm"]["id"] == 10
    assert "the place schema refuses the record" in render_report(result)


def test_the_history_never_makes_a_record_follow_its_own_part(repo):
    ids = first_sync(repo, obj("node", 10, "Lakeside Caravan Park"))
    rec = record_of(repo, ids[("node", 10)])
    rec["merged_osm"] = [{"type": "way", "id": 20, "part": True}]
    write(repo, rec)
    fake = FakeOsm()
    fake.node(10, [version(1, 100, PARK), version(2, 703, visible=False)])
    fake.changeset(703, DELETED_INTO_WAY_20.replace('<node id="11" version="2"/>', ""))
    result = sync(repo, [obj("way", 20, "Lakeside Caravan Park", version=2)], now=T2, history=history(repo, fake))
    assert result.moved == [] and [r.outcome for r in result.custody] == ["unclear"]
    assert record_of(repo, ids[("node", 10)])["merged_osm"] == [{"type": "way", "id": 20, "part": True}]


def test_a_timeout_reading_osm_leaves_the_record_gone(repo):
    first_sync(repo, obj("node", 10, "Lakeside Caravan Park"))

    def slow(url):
        raise TimeoutError("read timed out")

    hist = OsmHistory(repo / HISTORY_FILE, fetch=slow, pause_s=0)
    result = sync(repo, [], now=T2, history=hist)
    assert [r.outcome for r in result.custody] == ["not_looked_up"] and len(result.gone) == 1


def test_a_record_a_person_closed_never_moves(repo):
    ids = first_sync(repo, obj("node", 10, "Lakeside Caravan Park"))
    rec = record_of(repo, ids[("node", 10)])
    rec["status"] = "closed"
    write(repo, rec)
    result = sync(repo, [obj("way", 20, "Lakeside Caravan Park", lat=LAT + 40 * M)], now=T2)
    assert result.moved == [] and result.custody == [] and len(result.new) == 1
    assert record_of(repo, ids[("node", 10)])["osm"]["id"] == 10


def test_a_record_with_an_owner_value_is_never_merged_by_the_history(repo):
    ids = first_sync(repo, obj("node", 10, "Lakeside Caravan Park"), obj("way", 20, "Lakeside Caravan Park", lat=LAT + 900 * M))
    rec = record_of(repo, ids[("way", 20)])
    rec["facts"]["phone"] = "03 5555 0100"
    rec["fact_sources"]["phone"] = "owner"
    write(repo, rec)
    fake = FakeOsm()
    fake.node(10, [version(1, 100, PARK), version(2, 704, visible=False)])
    fake.changeset(704, DELETED_INTO_WAY_20.replace('<node id="11" version="2"/>', ""))
    result = sync(repo, [obj("way", 20, "Lakeside Caravan Park", lat=LAT + 900 * M, version=2)], now=T2, history=history(repo, fake))
    assert result.moved == [] and [r.outcome for r in result.custody] == ["unclear"]


def test_a_cached_verdict_is_checked_again_after_thirty_days(repo):
    first_sync(repo, obj("node", 12, "Creek Caravan Park"))
    fake = FakeOsm()
    fake.node(12, [version(1, 100, {"name": "Creek", "tourism": "caravan_site"}), version(2, 602, {"name": "Creek", "tourism": "hotel"})])
    sync(repo, [], now="2026-10-02T16:00:00Z", history=history(repo, fake))
    asked = len(fake.urls)
    sync(repo, [], now="2026-10-20T16:00:00Z", history=history(repo, fake))
    assert len(fake.urls) == asked
    fake.node(12, [version(1, 100, {"name": "Creek", "tourism": "caravan_site"}), version(3, 650, {"name": "Creek", "disused:tourism": "caravan_site"})])
    result = sync(repo, [], now="2026-11-02T16:00:00Z", history=history(repo, fake))
    assert len(fake.urls) > asked
    assert [(r.outcome, r.changeset) for r in result.custody] == [("closed_in_osm", 650)]


def test_a_newer_gone_record_merges_into_the_older_one_that_follows_the_object(repo):
    ids = first_sync(repo, obj("way", 20, "Lakeside Caravan Park", lat=LAT + 900 * M))
    older = ids[("way", 20)]
    newer = sync(repo, [obj("way", 20, "Lakeside Caravan Park", lat=LAT + 900 * M), obj("node", 10, "Lakeside Caravan Park")], now=T2).new[0]["id"]
    assert newer > older
    fake = FakeOsm()
    fake.node(10, [version(1, 100, PARK), version(2, 705, visible=False)])
    fake.changeset(705, DELETED_INTO_WAY_20.replace('<node id="11" version="2"/>', ""))
    way = obj("way", 20, "Lakeside Caravan Park", lat=LAT + 900 * M, version=2)
    result = sync(repo, [way], now=T3, history=history(repo, fake))
    assert [(m["id"], m["how"].outcome, m["how"].kept_id) for m in result.moved] == [(newer, "merged", older)]
    assert record_of(repo, newer)["merged_into"] == older
    kept = record_of(repo, older)
    assert kept["osm"] == {"type": "way", "id": 20, "version": 2} and {"type": "node", "id": 10} in kept["merged_osm"]
    assert f"`{newer}` merged into `{older}`, the older record" in render_report(result)
    again = sync(repo, [way], now="2026-10-04T16:00:00Z", history=history(repo, fake))
    assert again.moved == [] and again.updated == [] and again.gone == []


def test_two_records_reaching_one_object_in_one_run_merge_into_the_older(repo):
    """The older record follows its own merged object; the newer one's history names the same object."""
    ids = first_sync(repo, obj("way", 30, "Lakeside Holiday Park"))
    older = ids[("way", 30)]
    rec = record_of(repo, older)
    rec["merged_osm"] = [{"type": "way", "id": 20}]
    write(repo, rec)
    newer = sync(repo, [obj("way", 30, "Lakeside Holiday Park"), obj("relation", 40, "Lakeside Holiday Park")], now=T2).new[0]["id"]
    fake = FakeOsm()
    fake.histories["relation/40"] = json.dumps({"elements": [version(1, 100, {"name": "Lakeside Holiday Park", "tourism": "caravan_site"}), version(2, 706, visible=False)]})
    fake.changeset(
        706,
        '<osmChange><delete><relation id="40" version="2"/></delete><modify><way id="20" version="3">'
        '<tag k="name" v="Lakeside Holiday Park"/><tag k="tourism" v="caravan_site"/></way></modify></osmChange>',
    )
    way = obj("way", 20, "Lakeside Holiday Park", version=3)
    result = sync(repo, [way], now=T3, history=history(repo, fake))
    assert sorted((m["id"], m["how"].outcome) for m in result.moved) == sorted([(older, "own"), (newer, "merged")])
    assert record_of(repo, newer)["merged_into"] == older and record_of(repo, older)["osm"]["id"] == 20
    again = sync(repo, [way], now="2026-10-04T16:00:00Z", history=history(repo, fake))
    assert again.moved == [] and again.updated == [] and again.gone == []


def test_a_refused_move_mints_no_second_record_for_the_place(repo):
    first_sync(repo, obj("node", 10, "Lakeside Caravan Park"))
    bad = dict(obj("way", 20, "Lakeside Caravan Park", lat=LAT + 40 * M), facts={"fee": "maybe"})
    result = sync(repo, [bad], now=T2)
    assert [r.outcome for r in result.custody] == ["refused"]
    assert result.new == [] and len(result.skipped) == 1 and len(all_records(repo)) == 1


def test_when_the_older_record_merges_with_one_that_moved_the_tombstone_leaves_the_moved_list(repo):
    ids = first_sync(repo, obj("way", 30, "Lakeside Holiday Park"))
    older = ids[("way", 30)]
    newer = sync(repo, [obj("way", 30, "Lakeside Holiday Park"), obj("node", 10, "Lakeside Holiday Park", lat=LAT + 0.01)], now=T2).new[0]["id"]
    rec = record_of(repo, newer)
    rec["merged_osm"] = [{"type": "way", "id": 20}]
    write(repo, rec)
    fake = FakeOsm()
    fake.histories["way/30"] = json.dumps({"elements": [version(1, 100, {"name": "Lakeside Holiday Park", "tourism": "caravan_site"}), version(2, 707, visible=False)]})
    fake.changeset(
        707,
        '<osmChange><delete><way id="30" version="2"/></delete><modify><way id="20" version="3">'
        '<tag k="name" v="Lakeside Holiday Park"/><tag k="tourism" v="caravan_site"/></way></modify></osmChange>',
    )
    way = obj("way", 20, "Lakeside Holiday Park", lat=LAT + 0.01, version=3)
    result = sync(repo, [way], now=T3, history=history(repo, fake))
    assert [(m["id"], m["how"].outcome, m["how"].kept_id) for m in result.moved] == [(older, "merged", older)]
    assert record_of(repo, newer)["merged_into"] == older
    kept = record_of(repo, older)
    assert kept["osm"]["id"] == 20 and {"type": "node", "id": 10} in kept["merged_osm"]
    again = sync(repo, [way], now="2026-10-04T16:00:00Z", history=history(repo, fake))
    assert again.moved == [] and again.updated == [] and again.gone == []
