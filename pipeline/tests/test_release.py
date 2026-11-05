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


def add_fee(repo, place_id, fee_id, **changes):
    fee = {
        "id": fee_id,
        "place": place_id,
        "amount": 32.5,
        "site": "powered",
        "paid": "2026-09",
        "created": "2026-10-01T10:00:00Z",
        "app": "example-app",
        "contributor": "ctr_0123456789abcdef",
        **changes,
    }
    path = repo / "fees" / f"{place_id}.ndjson"
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(dump_line(fee) + "\n")


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
        "fees.ndjson.gz",
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
    # 6: fees.ndjson.gz, what travellers paid per night.
    assert manifest["format_version"] == 12
    assert manifest["format_minor"] == 0
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
    assert manifest["fees"]["file"] == "fees.ndjson.gz"
    assert manifest["fees"]["url"].endswith("/data-20261030T160000Z/fees.ndjson.gz")
    assert manifest["fees"]["count"] == 0


def test_release_is_byte_identical_on_rerun(repo, v1_osm, tmp_path):
    sync(repo, extract_file(v1_osm), now="2026-10-01T16:00:00Z")
    build(repo, tmp_path / "a")
    build(repo, tmp_path / "b")
    for name in ("manifest.json", "places.ndjson.gz", "reviews.ndjson.gz", "fees.ndjson.gz", "merged.ndjson.gz", "commons.ndjson.gz", "place-index.ndjson.gz"):
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
    assert manifest["fees"]["count"] == 0


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


def test_release_holds_fees_paid_in_the_last_24_months(repo, v1_osm, tmp_path):
    sync(repo, extract_file(v1_osm), now="2026-10-01T16:00:00Z")
    first, second = sorted(p.stem for p in (repo / "places").rglob("*.json"))[:2]
    # The release time is 2026-10-30, so 2024-10 is the oldest month kept.
    add_fee(repo, first, "fee_01JBX0000000000000000000AA", paid="2024-09")
    add_fee(repo, first, "fee_01JBX0000000000000000000AB", paid="2024-10", site="cabin", amount=120)
    add_fee(repo, first, "fee_01JBX0000000000000000000AC", paid="2026-10", contributor="ctr_ffffffffffffffff")
    add_fee(repo, second, "fee_01JBX0000000000000000000AD", paid="2026-10", site="unpowered", amount=18)
    blocked = repo / "moderation" / "blocked_contributors.json"
    blocked.parent.mkdir()
    blocked.write_text(json.dumps({"contributors": [
        {"contributor": "ctr_ffffffffffffffff", "added": "2026-11-02", "reason": "spam_ad"},
    ]}), encoding="utf-8")

    manifest = build(repo, tmp_path / "dist")

    fees_bytes = (tmp_path / "dist" / "fees.ndjson.gz").read_bytes()
    lines = gzip.decompress(fees_bytes).decode("utf-8").splitlines()
    expected_order = sorted([(first, "fee_01JBX0000000000000000000AB"), (second, "fee_01JBX0000000000000000000AD")])
    assert [json.loads(line)["id"] for line in lines] == [fee_id for _, fee_id in expected_order]
    assert all(line == dump_line(json.loads(line)) for line in lines)
    assert manifest["fees"] == {
        "file": "fees.ndjson.gz",
        "url": "https://github.com/opencampingdb/data/releases/download/data-20261030T160000Z/fees.ndjson.gz",
        "sha256": hashlib.sha256(fees_bytes).hexdigest(),
        "bytes": len(fees_bytes),
        "count": 2,
    }


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
    assert manifest["format_version"] == 12


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
