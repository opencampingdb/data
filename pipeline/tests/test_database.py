"""Tests for pipeline.database: the release's SQLite database and its changesets."""

import gzip
import hashlib
import json
import shutil

import apsw
import pytest

from pipeline import database
from pipeline.common import dump_line
from pipeline.extract import extract_file
from pipeline.release import build_release
from pipeline.sync import sync
from pipeline.tests.test_release import add_fee, add_review

FIRST = "20261030T160000Z-abc1234"
SECOND = "20261031T020000Z-def5678"
THIRD = "20261031T090000Z-0a1b2c3"


def place_ids(repo):
    return sorted(p.stem for p in (repo / "places").rglob("*.json"))


def add_commons(repo, place_id):
    match = {
        "checked": "2026-10-20",
        "place": place_id,
        "rules": 2,
        "files": [{
            "file": "Creek camp.jpg",
            "width": 4000,
            "height": 3000,
            "thumb": "https://upload.wikimedia.org/thumb/960.jpg",
            "full": "https://upload.wikimedia.org/thumb/1920.jpg",
            "page": "https://commons.wikimedia.org/wiki/File:Creek_camp.jpg",
            "licence": "CC0",
            "match": "name",
            "distance_m": 40,
        }],
    }
    (repo / "commons").mkdir(exist_ok=True)
    with open(repo / "commons" / "matches.ndjson", "a", encoding="utf-8", newline="\n") as f:
        f.write(dump_line(match) + "\n")


@pytest.fixture
def releases(repo, v1_osm, v2_osm, tmp_path):
    """Two release folders built by pipeline.release: the first from v1, the second from v2 with a review, a fee and a photo."""
    sync(repo, extract_file(v1_osm), now="2026-10-01T16:00:00Z")
    first = tmp_path / "first"
    build_release(repo, first, data_version=FIRST, generated="2026-10-30T16:00:00Z")

    sync(repo, extract_file(v2_osm), now="2026-10-31T01:00:00Z")
    ids = place_ids(repo)
    add_review(repo, ids[0], "rev_01JBX0000000000000000000AA")
    add_fee(repo, ids[0], "fee_01JBX0000000000000000000AA")
    add_commons(repo, ids[0])
    second = tmp_path / "second"
    build_release(repo, second, data_version=SECOND, generated="2026-10-31T02:00:00Z")
    return first, second


def unpack(folder, tmp_path, name):
    path = tmp_path / name
    path.write_bytes(gzip.decompress((folder / database.DATABASE_FILE).read_bytes()))
    return path


def rows(db_path):
    con = apsw.Connection(str(db_path), flags=apsw.SQLITE_OPEN_READONLY)
    try:
        return {
            table: con.execute(f'SELECT * FROM "{table}" ORDER BY "{key}"').fetchall()
            for table, key in database.TABLES.items()
        }
    finally:
        con.close()


def test_changeset_applied_to_the_first_database_gives_exactly_the_second(releases, tmp_path):
    first, second = releases
    database.build(first)
    manifest = database.build(second, previous=[first])

    [entry] = manifest["changesets"]
    changeset = gzip.decompress((second / entry["file"]).read_bytes())
    old = unpack(first, tmp_path, "old.sqlite")
    new = unpack(second, tmp_path, "new.sqlite")
    assert rows(old) != rows(new)

    con = apsw.Connection(str(old))
    # No conflict handler: sqlite3changeset_apply aborts on any conflict.
    apsw.Changeset.apply(changeset, con)
    con.close()

    assert rows(old) == rows(new)
    assert rows(new)["release"][0][1] == SECOND
    assert len(rows(new)["reviews"]) == 1
    assert len(rows(new)["fees"]) == 1
    assert len(rows(new)["commons"]) == 1


def test_manifest_lists_the_database_and_changeset(releases):
    first, second = releases
    database.build(first)
    manifest = database.build(second, previous=[first])
    assert manifest == json.loads((second / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["layout_version"] == database.LAYOUT_VERSION == 1
    packed = (second / "ocdb.sqlite.gz").read_bytes()
    assert manifest["database"] == {
        "file": "ocdb.sqlite.gz",
        "url": "https://github.com/opencampingdb/data/releases/download/data-20261031T020000Z/ocdb.sqlite.gz",
        "sha256": hashlib.sha256(packed).hexdigest(),
        "bytes": len(packed),
    }
    name = f"changeset-{FIRST}.bin.gz"
    changeset = (second / name).read_bytes()
    assert manifest["changesets"] == [{
        "file": name,
        "url": f"https://github.com/opencampingdb/data/releases/download/data-20261031T020000Z/{name}",
        "sha256": hashlib.sha256(changeset).hexdigest(),
        "bytes": len(changeset),
        "from": FIRST,
    }]
    # The release files pipeline.release listed are still there, unchanged.
    assert manifest["places"]["sha256"] == hashlib.sha256((second / "places.ndjson.gz").read_bytes()).hexdigest()


def test_database_tables_hold_the_release(releases, tmp_path):
    first, second = releases
    database.build(second)
    db = rows(unpack(second, tmp_path, "new.sqlite"))
    with gzip.open(second / "places.ndjson.gz", "rt", encoding="utf-8") as f:
        lines = [line.rstrip("\n") for line in f]
    assert [row[-1] for row in db["places"]] == lines
    place = json.loads(lines[0])
    assert db["places"][0][:7] == (
        place["id"], place["name"], place["kind"], place["group"],
        place["location"]["lat"], place["location"]["lon"], place["status"],
    )
    # The fixture places are in the Grampians.
    assert {row[7] for row in db["places"]} == {"VIC"}
    manifest = json.loads((second / "manifest.json").read_text(encoding="utf-8"))
    assert db["release"] == [(
        1, SECOND, "data-20261031T020000Z", "2026-10-31T02:00:00Z",
        manifest["format_version"], manifest["format_minor"], 1,
        "ODbL-1.0", "© OpenStreetMap contributors, Open Camping DB",
    )]
    con = apsw.Connection(str(tmp_path / "new.sqlite"))
    assert con.execute("PRAGMA user_version").get == 1
    assert con.execute("PRAGMA page_size").get == 4096
    indexes = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL")}
    assert indexes == {"places_kind", "places_group", "places_state", "places_lat_lon", "reviews_place", "fees_place"}


def test_no_changeset_across_a_layout_change(releases, monkeypatch):
    first, second = releases
    database.build(first)
    monkeypatch.setattr(database, "LAYOUT_VERSION", 2)
    manifest = database.build(second, previous=[first])
    assert manifest["layout_version"] == 2
    assert manifest["changesets"] == []
    assert not list(second.glob("changeset-*"))


def test_no_changeset_without_a_previous_database(releases):
    first, second = releases
    # The first release was published before releases carried a database.
    manifest = database.build(second, previous=[first])
    assert manifest["changesets"] == []
    assert not list(second.glob("changeset-*"))


def test_a_previous_database_that_does_not_match_its_manifest_fails(releases):
    first, second = releases
    database.build(first)
    packed = bytearray((first / "ocdb.sqlite.gz").read_bytes())
    packed[-9] ^= 0xFF
    (first / "ocdb.sqlite.gz").write_bytes(bytes(packed))
    with pytest.raises(database.ChangesetError):
        database.build(second, previous=[first])


def test_a_changeset_that_does_not_give_this_release_never_ships(releases, monkeypatch):
    first, second = releases
    database.build(first)
    # An empty changeset applies cleanly but leaves the first release's rows.
    monkeypatch.setattr(database, "make_changeset", lambda old, new: b"")
    with pytest.raises(database.ChangesetError):
        database.build(second, previous=[first])
    assert not list(second.glob("changeset-*"))


def test_build_is_deterministic(releases, tmp_path):
    first, second = releases
    database.build_database(second, tmp_path / "a.sqlite")
    database.build_database(second, tmp_path / "b.sqlite")
    assert (tmp_path / "a.sqlite").read_bytes() == (tmp_path / "b.sqlite").read_bytes()

    database.build(first)
    copy = tmp_path / "second-again"
    shutil.copytree(second, copy)
    one = database.build(second, previous=[first])
    two = database.build(copy, previous=[first])
    assert one["database"]["sha256"] == two["database"]["sha256"]
    assert one["changesets"][0]["sha256"] == two["changesets"][0]["sha256"]


def test_day_changeset_from_the_last_release_of_the_previous_day(releases, repo, tmp_path):
    first, second = releases
    database.build(first)
    database.build(second, previous=[first])
    # A third release later on 31 October: the release before is the second,
    # and the last release of 30 October is the first.
    third = tmp_path / "third"
    build_release(repo, third, data_version=THIRD, generated="2026-10-31T09:00:00Z")
    manifest = database.build(third, previous=[second], day=first)

    assert [entry["from"] for entry in manifest["changesets"]] == [SECOND, FIRST]
    assert [entry["file"] for entry in manifest["changesets"]] == [
        f"changeset-{SECOND}.bin.gz",
        "changeset-day-2026-10-30.bin.gz",
    ]
    old = unpack(first, tmp_path, "old.sqlite")
    con = apsw.Connection(str(old))
    apsw.Changeset.apply(gzip.decompress((third / "changeset-day-2026-10-30.bin.gz").read_bytes()), con)
    con.close()
    assert rows(old) == rows(unpack(third, tmp_path, "third.sqlite"))


def test_no_second_changeset_when_the_release_before_is_the_last_of_the_previous_day(releases):
    first, second = releases
    database.build(first)
    manifest = database.build(second, previous=[first], day=first)
    assert [entry["file"] for entry in manifest["changesets"]] == [f"changeset-{FIRST}.bin.gz"]


def test_day_base_is_the_newest_tag_from_an_earlier_utc_day():
    tags = [
        "data-20261030T090000Z",
        "data-20261030T235900Z",
        "data-20261031T010000Z",
        "data-20261031T080000Z",
        "v1.0",
    ]
    assert database.day_base(tags, THIRD) == "data-20261030T235900Z"
    assert database.day_base(tags[2:], THIRD) is None
    assert database.day_base([], THIRD) is None
