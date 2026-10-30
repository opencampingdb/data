"""Tests for pipeline.release: building the published files."""

import gzip
import hashlib
import json

from pipeline.common import dump_line
from pipeline.extract import extract_file
from pipeline.release import build_release
from pipeline.sync import sync

VERSION = "20261030T160000Z-abc1234"
GENERATED = "2026-10-30T16:00:00Z"


def add_review(repo, place_id, review_id):
    review = {
        "id": review_id,
        "place": place_id,
        "stars": 4,
        "text": "Quiet spot by the creek.",
        "visited": "2026-09",
        "created": "2026-10-01T10:00:00Z",
        "app": "camporacle",
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
        "manifest.json",
        "merged.ndjson.gz",
        "places.ndjson.gz",
        "reviews.ndjson.gz",
    ]
    places_bytes = (out / "places.ndjson.gz").read_bytes()
    lines = gzip.decompress(places_bytes).decode("utf-8").splitlines()
    assert [json.loads(line)["id"] for line in lines] == place_ids

    assert manifest == json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    # 2: OTWD kinds and the group field replaced the eight kinds of format 1.
    assert manifest["format_version"] == 2
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
    for name in ("manifest.json", "places.ndjson.gz", "reviews.ndjson.gz", "merged.ndjson.gz"):
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
