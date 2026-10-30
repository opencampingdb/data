"""Tests for pipeline.sync: merging an extract into places/."""

import json
import re
from pathlib import Path

from pipeline.common import place_path
from pipeline.extract import extract_file
from pipeline.sync import render_report, sync

T1 = "2026-10-01T16:00:00Z"
T2 = "2026-10-02T16:00:00Z"
T3 = "2026-10-03T16:00:00Z"


def snapshot(repo: Path) -> dict[str, bytes]:
    """Every file under places/, keyed by relative path."""
    return {
        str(p.relative_to(repo)): p.read_bytes()
        for p in sorted((repo / "places").rglob("*.json"))
    }


def load_all(repo: Path) -> dict[tuple[str, int], dict]:
    out = {}
    for path in (repo / "places").rglob("*.json"):
        rec = json.loads(path.read_text(encoding="utf-8"))
        out[(rec["osm"]["type"], rec["osm"]["id"])] = rec
    return out


def rewrite(path: Path, rec: dict) -> None:
    path.write_text(json.dumps(rec, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def test_first_sync_creates_one_file_per_place(repo, v1_osm):
    result = sync(repo, extract_file(v1_osm), now=T1)
    assert len(result.new) == 3
    assert result.updated == [] and result.gone == []
    records = load_all(repo)
    camp = records[("node", 1001)]
    assert re.fullmatch(r"ocdb_[0-9A-HJKMNP-TV-Z]{26}", camp["id"])
    assert place_path(repo, camp["id"]).exists()
    assert camp["status"] == "open"
    assert camp["rating"] is None
    assert camp["updated"] == T1
    assert camp["fact_sources"] == {key: "osm" for key in camp["facts"]}


def test_file_format_is_deterministic(repo, v1_osm):
    sync(repo, extract_file(v1_osm), now=T1)
    for path in (repo / "places").rglob("*.json"):
        text = path.read_bytes().decode("utf-8")
        rec = json.loads(text)
        assert text == json.dumps(rec, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
        assert "\r" not in text


def test_resync_is_idempotent(repo, v1_osm):
    sync(repo, extract_file(v1_osm), now=T1)
    before = snapshot(repo)
    result = sync(repo, extract_file(v1_osm), now=T2)
    assert result.new == [] and result.updated == [] and result.gone == []
    assert result.change_percent == 0
    # Nothing changed, so not even the "updated" stamp moves.
    assert snapshot(repo) == before


def test_changed_new_and_gone(repo, v1_osm, v2_osm):
    sync(repo, extract_file(v1_osm), now=T1)
    ids_before = {k: r["id"] for k, r in load_all(repo).items()}

    result = sync(repo, extract_file(v2_osm), now=T2)
    after = load_all(repo)

    # Matched records keep their id.
    assert after[("node", 1001)]["id"] == ids_before[("node", 1001)]
    assert after[("way", 2001)]["id"] == ids_before[("way", 2001)]

    camp = after[("node", 1001)]
    assert camp["name"] == "Fixture Creek Bush Camp"
    assert camp["facts"]["fee"] is True
    assert camp["osm"]["version"] == 2
    assert camp["updated"] == T2
    # The website tag was removed in OSM, so the OSM-sourced fact goes too.
    assert "website" not in camp["facts"]
    assert "website" not in camp["fact_sources"]

    # Unchanged record keeps its old timestamp.
    assert after[("way", 2001)]["updated"] == T1

    # The vanished rest area is still there, untouched, and reported.
    rest = after[("node", 1002)]
    assert rest["updated"] == T1
    assert [r["id"] for r in result.gone] == [rest["id"]]

    assert [r["osm"]["id"] for r in result.new] == [1004]
    assert [r["id"] for r in result.updated] == [camp["id"]]
    assert len(after) == 4


def test_feedback_sourced_fact_is_kept(repo, v1_osm, v2_osm):
    sync(repo, extract_file(v1_osm), now=T1)
    camp_id = load_all(repo)[("node", 1001)]["id"]
    path = place_path(repo, camp_id)
    rec = json.loads(path.read_text(encoding="utf-8"))
    # Feedback says there is power (OSM says no) and that there are showers
    # (OSM says nothing).
    rec["facts"]["power"] = True
    rec["fact_sources"]["power"] = "feedback"
    rec["facts"]["showers"] = True
    rec["fact_sources"]["showers"] = "feedback"
    rewrite(path, rec)

    sync(repo, extract_file(v2_osm), now=T2)
    camp = json.loads(path.read_text(encoding="utf-8"))
    assert camp["facts"]["power"] is True
    assert camp["fact_sources"]["power"] == "feedback"
    assert camp["facts"]["showers"] is True
    assert camp["fact_sources"]["showers"] == "feedback"
    # OSM-sourced facts are still refreshed around them.
    assert camp["facts"]["fee"] is True
    assert camp["fact_sources"]["fee"] == "osm"


def test_status_and_rating_survive_sync(repo, v1_osm, v2_osm):
    sync(repo, extract_file(v1_osm), now=T1)
    camp_id = load_all(repo)[("node", 1001)]["id"]
    path = place_path(repo, camp_id)
    rec = json.loads(path.read_text(encoding="utf-8"))
    rec["status"] = "reported_closed"
    rec["rating"] = {"count": 2, "mean": 4.5}
    rewrite(path, rec)

    sync(repo, extract_file(v2_osm), now=T2)
    camp = json.loads(path.read_text(encoding="utf-8"))
    assert camp["status"] == "reported_closed"
    assert camp["rating"] == {"count": 2, "mean": 4.5}


def test_gone_is_reported_not_deleted_and_closed_records_drop_off(repo, v1_osm, v2_osm):
    sync(repo, extract_file(v1_osm), now=T1)
    result = sync(repo, extract_file(v2_osm), now=T2)
    gone_id = result.gone[0]["id"]
    assert place_path(repo, gone_id).exists()

    report = render_report(result)
    assert "## Gone from OSM" in report
    assert gone_id in report
    assert "[node/1002](https://www.openstreetmap.org/node/1002)" in report

    # A person closes it; it stops being listed but the file stays.
    path = place_path(repo, gone_id)
    rec = json.loads(path.read_text(encoding="utf-8"))
    rec["status"] = "closed"
    rewrite(path, rec)
    result = sync(repo, extract_file(v2_osm), now=T3)
    assert result.gone == []
    assert path.exists()


def test_change_percent(repo, v1_osm, v2_osm):
    first = sync(repo, extract_file(v1_osm), now=T1)
    assert first.change_percent == 100.0
    second = sync(repo, extract_file(v2_osm), now=T2)
    # One new, one updated, one gone, out of four records after the sync.
    assert second.records_after == 4
    assert second.change_percent == 75.0
    assert "75.00%" in render_report(second)


def test_report_escapes_names(repo, v1_osm):
    records = extract_file(v1_osm)
    records[0]["name"] = "Pipe | and @someone and `tick`"
    result = sync(repo, records, now=T1)
    report = render_report(result)
    assert "@someone" not in report
    assert "Pipe \\| and" in report


def test_place_path_uses_last_two_characters_in_lower_case(tmp_path):
    path = place_path(tmp_path, "ocdb_01JBX0000000000000000000AB")
    assert path == tmp_path / "places" / "ab" / "ocdb_01JBX0000000000000000000AB.json"


def test_feedback_name_and_location_are_kept(repo, v1_osm, v2_osm):
    sync(repo, extract_file(v1_osm), now=T1)
    camp_id = load_all(repo)[("node", 1001)]["id"]
    path = place_path(repo, camp_id)
    rec = json.loads(path.read_text(encoding="utf-8"))
    rec["name"] = "Fixture Creek Camp (lower clearing)"
    rec["location"] = {"lat": -37.151234, "lon": 142.501234}
    rec["fact_sources"]["name"] = "feedback"
    rec["fact_sources"]["location"] = "feedback"
    rewrite(path, rec)

    # v2 renames the camp in OSM. The feedback name and position still win.
    sync(repo, extract_file(v2_osm), now=T2)
    camp = json.loads(path.read_text(encoding="utf-8"))
    assert camp["name"] == "Fixture Creek Camp (lower clearing)"
    assert camp["location"] == {"lat": -37.151234, "lon": 142.501234}
    assert camp["fact_sources"]["name"] == "feedback"
    assert camp["fact_sources"]["location"] == "feedback"
    # Facts from OSM are still refreshed.
    assert camp["facts"]["fee"] is True


def test_osm_name_source_is_refreshed(repo, v1_osm, v2_osm):
    sync(repo, extract_file(v1_osm), now=T1)
    camp_id = load_all(repo)[("node", 1001)]["id"]
    path = place_path(repo, camp_id)
    rec = json.loads(path.read_text(encoding="utf-8"))
    rec["fact_sources"]["name"] = "osm"
    rewrite(path, rec)

    sync(repo, extract_file(v2_osm), now=T2)
    camp = json.loads(path.read_text(encoding="utf-8"))
    assert camp["name"] == "Fixture Creek Bush Camp"
    assert camp["fact_sources"]["name"] == "osm"
