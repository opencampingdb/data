"""Tests for pipeline.release: building the published files."""

import gzip
import hashlib
import json

from pipeline.common import dump_line, dump_record, place_path
from pipeline.extract import extract_file
from pipeline.release import build_release
from pipeline.sync import sync

VERSION = "20261030T160000Z-abc1234"
GENERATED = "2026-10-30T16:00:00Z"


def add_review(repo, place_id, review_id, **changes):
    review = {
        "id": review_id,
        "place": place_id,
        "stars": 4,
        "text": "Quiet spot by the creek.",
        "visited": "2026-09",
        "created": "2026-10-01T10:00:00Z",
        "app": "example-app",
        "contributor": "ctr_0123456789abcdef",
        **changes,
    }
    path = repo / "reviews" / f"{place_id}.ndjson"
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(dump_line(review) + "\n")


def build(repo, out):
    return build_release(repo, out, data_version=VERSION, generated=GENERATED)


def test_release_files_and_manifest(repo, v1_osm, tmp_path):
    sync(repo, extract_file(v1_osm), now="2026-10-01T16:00:00Z")
    place_ids = sorted(p.stem for p in (repo / "places").rglob("*.json"))
    add_review(repo, place_ids[0], "rev_01JBX0000000000000000000AA")

    out = tmp_path / "dist"
    manifest = build(repo, out)

    assert sorted(p.name for p in out.iterdir()) == [
        "commons.ndjson.gz",
        "manifest.json",
        "merged.ndjson.gz",
        "place-index.ndjson.gz",
        "places.ndjson.gz",
        "reviews.ndjson.gz",
    ]
    places_bytes = (out / "places.ndjson.gz").read_bytes()
    lines = gzip.decompress(places_bytes).decode("utf-8").splitlines()
    assert [json.loads(line)["id"] for line in lines] == place_ids

    assert manifest == json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    # 13: fees.ndjson.gz is gone, and the charge facts arrive.
    assert manifest["format_version"] == 13
    # 13.1: the facts locality and lga.
    assert manifest["format_minor"] == 1
    assert "fees" not in manifest
    assert manifest["data_version"] == VERSION
    assert manifest["generated"] == GENERATED
    assert manifest["licence"] == "ODbL-1.0"
    assert manifest["attribution"] == "© OpenStreetMap contributors, Open Camping DB"
    assert manifest["release"] == "data-20261030T160000Z"
    assert manifest["places"] == {
        "file": "places.ndjson.gz",
        "url": "https://github.com/opencampingdb/data/releases/download/"
        "data-20261030T160000Z/places.ndjson.gz",
        "sha256": hashlib.sha256(places_bytes).hexdigest(),
        "bytes": len(places_bytes),
        "count": 3,
    }
    assert manifest["reviews"]["file"] == "reviews.ndjson.gz"
    assert manifest["reviews"]["url"].endswith("/data-20261030T160000Z/reviews.ndjson.gz")
    assert manifest["reviews"]["count"] == 1


def test_release_is_byte_identical_on_rerun(repo, v1_osm, tmp_path):
    sync(repo, extract_file(v1_osm), now="2026-10-01T16:00:00Z")
    build(repo, tmp_path / "a")
    build(repo, tmp_path / "b")
    for name in ("manifest.json", "places.ndjson.gz", "reviews.ndjson.gz", "merged.ndjson.gz", "commons.ndjson.gz", "place-index.ndjson.gz"):
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes()


def test_gzip_header_has_no_timestamp_or_name(repo, v1_osm, tmp_path):
    sync(repo, extract_file(v1_osm), now="2026-10-01T16:00:00Z")
    build(repo, tmp_path / "dist")
    header = (tmp_path / "dist" / "places.ndjson.gz").read_bytes()[:10]
    assert header[4:8] == b"\x00\x00\x00\x00"  # MTIME is zero
    assert header[3] & 0x08 == 0  # FNAME flag not set


def test_empty_repo_builds_empty_release(repo, tmp_path):
    manifest = build(repo, tmp_path / "dist")
    assert manifest["places"]["count"] == 0
    assert manifest["reviews"]["count"] == 0


def test_release_tag_and_repository_can_be_set(repo, tmp_path):
    manifest = build_release(
        repo, tmp_path / "dist", VERSION, GENERATED, release="data-test", repository="someone/fork"
    )
    assert manifest["release"] == "data-test"
    assert manifest["places"]["url"] == (
        "https://github.com/someone/fork/releases/download/data-test/places.ndjson.gz"
    )


def test_release_leaves_out_hidden_and_blocked_reviews(repo, v1_osm, tmp_path):
    sync(repo, extract_file(v1_osm), now="2026-10-01T16:00:00Z")
    place_id = sorted(p.stem for p in (repo / "places").rglob("*.json"))[0]
    add_review(repo, place_id, "rev_01JBX0000000000000000000AA", visited_nearby=True)
    add_review(repo, place_id, "rev_01JBX0000000000000000000AB", hidden="reported")
    add_review(repo, place_id, "rev_01JBX0000000000000000000AC", contributor="ctr_ffffffffffffffff")
    blocked = repo / "moderation" / "blocked_contributors.json"
    blocked.parent.mkdir()
    blocked.write_text(json.dumps({"contributors": [
        {"contributor": "ctr_ffffffffffffffff", "added": "2026-11-02", "reason": "spam_ad"},
    ]}), encoding="utf-8")

    manifest = build(repo, tmp_path / "dist")

    lines = gzip.decompress((tmp_path / "dist" / "reviews.ndjson.gz").read_bytes()).decode("utf-8").splitlines()
    assert [json.loads(line)["id"] for line in lines] == ["rev_01JBX0000000000000000000AA"]
    assert json.loads(lines[0])["contributor"] == "ctr_0123456789abcdef"
    assert json.loads(lines[0])["visited_nearby"] is True
    assert manifest["reviews"]["count"] == 1


def test_release_leaves_out_held_and_hidden_new_places(repo, v1_osm, tmp_path):
    sync(repo, extract_file(v1_osm), now="2026-10-01T16:00:00Z")
    osm_count = len(list((repo / "places").rglob("*.json")))
    for n, standing in enumerate(["shown", "confirmed", "held", "hidden"]):
        place_id = f"ocdb_01JC000000000000000000000{n}"
        rec = {
            "id": place_id, "name": None, "kind": "free_camp", "group": "overnight",
            "location": {"lat": -35.1, "lon": 149.2 + n}, "osm": None, "facts": {}, "status": "open",
            "rating": None, "standing": standing, "fact_sources": {"location": "feedback"},
            "fact_dates": {"location": "2026-10-30T01:00:00Z"}, "updated": "2026-10-30T01:00:00Z",
        }
        path = place_path(repo, place_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dump_record(rec), encoding="utf-8", newline="\n")

    manifest = build(repo, tmp_path / "dist")

    lines = gzip.decompress((tmp_path / "dist" / "places.ndjson.gz").read_bytes()).decode("utf-8").splitlines()
    standings = sorted(json.loads(line)["standing"] for line in lines if "standing" in json.loads(line))
    assert standings == ["confirmed", "shown"]
    assert manifest["places"]["count"] == osm_count + 2
    assert manifest["format_version"] == 13


def test_commons_lines_are_released_matches_only(tmp_path, v1_osm):
    repo = tmp_path / "repo"
    sync(repo, extract_file(v1_osm), now="2026-10-01T16:00:00Z")
    place_ids = sorted(p.stem for p in (repo / "places").rglob("*.json"))
    files = [{"file": "A.jpg", "match": "name"}]
    (repo / "commons").mkdir()
    (repo / "commons" / "matches.ndjson").write_text(
        json.dumps({"place": place_ids[1], "checked": "2026-11-04", "files": files}) + "\n"
        + json.dumps({"place": place_ids[0], "checked": "2026-11-04", "files": []}) + "\n"
        + json.dumps({"place": "ocdb_not_released", "checked": "2026-11-04", "files": files}) + "\n",
        encoding="utf-8",
    )
    manifest = build(repo, tmp_path / "dist")
    lines = gzip.decompress((tmp_path / "dist" / "commons.ndjson.gz").read_bytes()).decode("utf-8").splitlines()
    assert [json.loads(line) for line in lines] == [{"place": place_ids[1], "files": files}]
    assert manifest["commons"]["count"] == 1
    assert manifest["commons"]["url"].endswith("/commons.ndjson.gz")


def test_commons_pairs_on_the_do_not_match_list_are_left_out(tmp_path, v1_osm):
    repo = tmp_path / "repo"
    sync(repo, extract_file(v1_osm), now="2026-10-01T16:00:00Z")
    place_ids = sorted(p.stem for p in (repo / "places").rglob("*.json"))
    (repo / "commons").mkdir()
    (repo / "commons" / "matches.ndjson").write_text(
        json.dumps({"place": place_ids[0], "checked": "2026-11-04", "files": [{"file": "Wrong.jpg"}, {"file": "Right.jpg"}]}) + "\n"
        + json.dumps({"place": place_ids[1], "checked": "2026-11-04", "files": [{"file": "Wrong.jpg"}]}) + "\n",
        encoding="utf-8",
    )
    (repo / "moderation").mkdir(exist_ok=True)
    (repo / "moderation" / "commons-do-not-match.json").write_text(dump_record({"pairs": [
        {"place": place_ids[0], "file": "Wrong.jpg", "added": "2026-11-05", "source": "reviewer"},
    ]}), encoding="utf-8")
    build(repo, tmp_path / "dist")
    lines = gzip.decompress((tmp_path / "dist" / "commons.ndjson.gz").read_bytes()).decode("utf-8").splitlines()
    assert [json.loads(line) for line in lines] == [
        {"place": place_ids[0], "files": [{"file": "Right.jpg"}]},
        {"place": place_ids[1], "files": [{"file": "Wrong.jpg"}]},
    ]
