"""Values and places from Overture Maps (source overture).

An overture value is a gap fill: it validates like a contributed value, the
OSM sync replaces it as soon as OSM gives a value, and it never reaches OSM.
An Overture candidate (a place Overture lists and OSM lacks, with no OSM
link) never reaches OSM either.
"""

import json
from datetime import datetime, timezone

from pipeline.common import NOT_FOR_OSM, SOURCES, dump_record, load_places, place_path
from pipeline.maproulette import DOUBLE_MAPPING, REPORT_SOURCES, TRAVELLER_REPORTS, write_challenges
from pipeline.sync import sync
from pipeline.validate import validate_repo

T1 = "2026-11-16T16:00:00Z"
T2 = "2026-11-17T16:00:00Z"
FILLED = "2026-11-15T00:00:00Z"
CANDIDATE = "ocdb_01JBX0000000000000000000V1"
A = "ocdb_01M66ES9DS1AQB245SJJXW0J18"
B = "ocdb_01M66ES9WTB7KSKMPCWQGC6HXN"
NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)


def write(repo, rec):
    path = place_path(repo, rec["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_record(rec), encoding="utf-8", newline="\n")
    return path


def candidate(**changes):
    """An approved Overture candidate: no OSM link, published as not checked yet."""
    rec = {
        "id": CANDIDATE,
        "name": "Riverbend Holiday Park",
        "kind": "caravan_park",
        "group": "overnight",
        "location": {"lat": -30.0, "lon": 140.0},
        "osm": None,
        "facts": {"website": "https://riverbend.example.au/"},
        "status": "open",
        "rating": None,
        "standing": "shown",
        "fact_sources": {"name": "overture", "location": "overture", "website": "overture"},
        "fact_dates": {"name": FILLED, "location": FILLED, "website": FILLED},
        "updated": FILLED,
    }
    rec.update(changes)
    return rec


def osm_place(osm_id, name="Lake Camp", **facts):
    return {
        "osm": {"type": "node", "id": osm_id, "version": 1},
        "kind": "caravan_park",
        "name": name,
        "location": {"lat": -35.0, "lon": 145.0},
        "facts": facts,
    }


def osm_record(place_id, osm_id, **changes):
    rec = {
        "id": place_id,
        "name": "Lake Camp",
        "kind": "caravan_park",
        "group": "overnight",
        "location": {"lat": -35.0, "lon": 145.0},
        "osm": {"type": "node", "id": osm_id, "version": 1},
        "facts": {},
        "status": "open",
        "rating": None,
        "fact_sources": {},
        "updated": FILLED,
    }
    rec.update(changes)
    return rec


def filled(place_id=A, osm_id=1, **changes):
    """An OSM place with an Overture website and phone filling gaps."""
    rec = osm_record(
        place_id, osm_id,
        facts={"website": "https://lakecamp.example.au/", "phone": "+61 2 5550 0100"},
        fact_sources={"website": "overture", "phone": "overture"},
        fact_dates={"website": FILLED, "phone": FILLED},
    )
    rec.update(changes)
    return rec


def record(repo, place_id=A):
    return load_places(repo)[place_id]


# ------------------------------------------------------------------ sources


def test_overture_is_a_source_that_never_goes_to_osm():
    assert "overture" in SOURCES
    assert "overture" in NOT_FOR_OSM
    assert "overture" not in REPORT_SOURCES


# --------------------------------------------------------------- validation


def test_overture_values_on_an_osm_place_are_valid(repo):
    write(repo, filled())
    assert validate_repo(repo) == []


def test_overture_value_needs_a_date(repo):
    rec = filled()
    del rec["fact_dates"]["phone"]
    write(repo, rec)
    assert any("fact_dates" in e and "phone" in e for e in validate_repo(repo))


def test_overture_candidate_is_valid(repo):
    write(repo, candidate())
    assert validate_repo(repo) == []


# --------------------------------------------------------------------- sync


def test_sync_replaces_an_overture_value_once_osm_gives_one(repo):
    write(repo, filled())
    sync(repo, [osm_place(1, website="https://lakecamp.org.au/")], now=T2)
    after = record(repo)
    assert after["facts"]["website"] == "https://lakecamp.org.au/"
    assert after["fact_sources"]["website"] == "osm"
    assert "website" not in after.get("fact_dates", {})
    # OSM gives no phone, so the Overture phone stays with its source and date.
    assert after["facts"]["phone"] == "+61 2 5550 0100"
    assert after["fact_sources"]["phone"] == "overture"
    assert after["fact_dates"] == {"phone": FILLED}
    assert validate_repo(repo) == []


def test_sync_drops_overture_dates_when_osm_gives_every_value(repo):
    write(repo, filled())
    sync(repo, [osm_place(1, website="https://lakecamp.org.au/", phone="+61 2 5550 0199")], now=T2)
    after = record(repo)
    assert after["fact_sources"] == {"website": "osm", "phone": "osm"}
    assert "fact_dates" not in after
    assert validate_repo(repo) == []


def test_sync_keeps_overture_values_while_osm_has_none(repo):
    write(repo, filled())
    result = sync(repo, [osm_place(1)], now=T2)
    assert A not in {r["id"] for r in result.updated}
    assert record(repo) == filled()


def test_sync_replaces_an_overture_name_once_osm_names_the_place(repo):
    write(repo, osm_record(A, 1, name="Riverbend Holiday Park",
                           fact_sources={"name": "overture"}, fact_dates={"name": FILLED}))
    sync(repo, [osm_place(1, name=None)], now=T1)
    assert record(repo)["name"] == "Riverbend Holiday Park"
    assert record(repo)["fact_sources"] == {"name": "overture"}

    sync(repo, [osm_place(1, name="Riverbend Park")], now=T2)
    after = record(repo)
    assert after["name"] == "Riverbend Park"
    assert "name" not in after["fact_sources"]
    assert "fact_dates" not in after
    assert validate_repo(repo) == []


def test_sync_still_keeps_other_non_osm_values_over_osm(repo):
    write(repo, osm_record(A, 1, facts={"website": "https://mine.example.au/", "phone": "+61 2 5550 0100"},
                           fact_sources={"website": "owner", "phone": "feedback"},
                           fact_dates={"website": FILLED, "phone": FILLED}))
    sync(repo, [osm_place(1, website="https://lakecamp.org.au/", phone="+61 2 5550 0199")], now=T2)
    after = record(repo)
    assert after["facts"] == {"website": "https://mine.example.au/", "phone": "+61 2 5550 0100"}
    assert after["fact_sources"] == {"website": "owner", "phone": "feedback"}


def test_sync_never_touches_an_overture_candidate(repo):
    path = write(repo, candidate())
    before = path.read_bytes()
    result = sync(repo, [osm_place(1)], now=T2)
    assert CANDIDATE not in {r["id"] for r in result.new + result.updated + result.gone}
    assert path.read_bytes() == before


# --------------------------------------------------------------- MapRoulette


def inbox_with(tmp_path, place, **facts):
    """An inbox where two travellers reported each fact, one of them seen nearby: enough evidence for a task."""
    root = tmp_path / "inbox"
    for n, (field, value) in enumerate(facts.items()):
        for origin in ("o1", "o2"):
            item = {
                "id": f"fb_{n}{origin}", "received": "2026-11-18T00:00:00Z", "type": "report", "place": place,
                "device": origin, "origin": origin, "report": {"kind": "fact", "fact": field, "value": value},
                "outcome": {"result": "applied", "reason": "test", "run": "2026-11-18T01:00:00Z"},
            }
            if origin == "o1":
                item["report"]["first_hand"] = "seen_nearby"
            path = root / "applied" / "2026-11-18" / f"{item['id']}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(item), encoding="utf-8")
    (root / "applied").mkdir(parents=True, exist_ok=True)
    return root


def challenge(repo, name):
    return (repo / "maproulette" / name).read_text(encoding="utf-8")


def test_overture_values_never_become_tasks(repo, tmp_path):
    write(repo, filled())
    box = inbox_with(tmp_path, A, website="https://lakecamp.example.au/", phone="+61 2 5550 0100")
    write_challenges(repo, [osm_place(1)], inbox=box, now=NOW)
    assert challenge(repo, TRAVELLER_REPORTS) == ""


def test_feedback_still_goes_when_overture_values_sit_beside_it(repo, tmp_path):
    rec = filled()
    rec["facts"]["fee"] = True
    rec["fact_sources"]["fee"] = "feedback"
    rec["fact_dates"]["fee"] = FILLED
    write(repo, rec)
    box = inbox_with(tmp_path, A, fee=True, phone="+61 2 5550 0100")
    write_challenges(repo, [osm_place(1)], inbox=box, now=NOW)
    text = challenge(repo, TRAVELLER_REPORTS)
    assert "fee=yes" in text
    assert "5550 0100" not in text and "lakecamp.example.au" not in text


def test_overture_candidate_is_never_sent(repo, tmp_path):
    write(repo, candidate())
    write_challenges(repo, [osm_place(1)], inbox=inbox_with(tmp_path, CANDIDATE, toilets=True), now=NOW)
    assert challenge(repo, TRAVELLER_REPORTS) == ""
    assert challenge(repo, DOUBLE_MAPPING) == ""


def test_an_overture_name_never_reaches_a_task(repo, tmp_path):
    # An OSM place mapped twice, and one with a traveller's fee, both named
    # only by Overture: the tasks go out, without the Overture name.
    write(repo, osm_record(A, 1, name="Riverbend Holiday Park", fact_sources={"name": "overture"},
                           fact_dates={"name": FILLED}, merged_osm=[{"type": "node", "id": 2}]))
    write(repo, osm_record(B, 3, name="Creekside Tourist Park", location={"lat": -35.1, "lon": 145.1},
                           facts={"fee": True}, fact_sources={"name": "overture", "fee": "feedback"},
                           fact_dates={"name": FILLED, "fee": FILLED}))
    unnamed = [osm_place(1, name=None), osm_place(2, name=None), dict(osm_place(3, name=None), location={"lat": -35.1, "lon": 145.1})]
    write_challenges(repo, unnamed, inbox=inbox_with(tmp_path, B, fee=True), now=NOW)
    doubles, reports = challenge(repo, DOUBLE_MAPPING), challenge(repo, TRAVELLER_REPORTS)
    assert "node/2" in doubles and "fee=yes" in reports
    for text in (doubles, reports):
        assert "Riverbend" not in text and "Creekside" not in text
        assert "Unnamed place" in text
