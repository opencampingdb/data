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
from pipeline.tests.test_release import add_review

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
    """Two release folders built by pipeline.release: the first from v1, the second from v2 with a review and a photo."""
    sync(repo, extract_file(v1_osm), now="2026-10-01T16:00:00Z")
    first = tmp_path / "first"
    build_release(repo, first, data_version=FIRST, generated="2026-10-30T16:00:00Z")

    sync(repo, extract_file(v2_osm), now="2026-10-31T01:00:00Z")
    ids = place_ids(repo)
    add_review(repo, ids[0], "rev_01JBX0000000000000000000AA")
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
    assert len(rows(new)["commons"]) == 1


def test_manifest_lists_the_database_and_changeset(releases):
    first, second = releases
    database.build(first)
    manifest = database.build(second, previous=[first])
    assert manifest == json.loads((second / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["layout_version"] == database.LAYOUT_VERSION == 2
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
        "to": SECOND,
        "day": False,
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
        manifest["format_version"], manifest["format_minor"], 2,
        "ODbL-1.0", "© OpenStreetMap contributors, Open Camping DB",
    )]
    con = apsw.Connection(str(tmp_path / "new.sqlite"))
    assert con.execute("PRAGMA user_version").get == 2
    assert con.execute("PRAGMA page_size").get == 4096
    indexes = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL")}
    assert indexes == {"places_kind", "places_group", "places_state", "places_lat_lon", "reviews_place"}


def test_no_changeset_across_a_layout_change(releases, monkeypatch):
    first, second = releases
    database.build(first)
    monkeypatch.setattr(database, "LAYOUT_VERSION", 3)
    manifest = database.build(second, previous=[first])
    assert manifest["layout_version"] == 3
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

    made = [entry for entry in manifest["changesets"] if entry["to"] == THIRD]
    assert [(entry["from"], entry["file"], entry["day"]) for entry in made] == [
        (FIRST, "changeset-day-2026-10-30.bin.gz", True),
        (SECOND, f"changeset-{SECOND}.bin.gz", False),
    ]
    assert sorted(p.name for p in third.glob("changeset-*")) == [
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


def release_folder(repo, tmp_path, n):
    """The nth release of the repo, an hour apart from 1 November, built by pipeline.release."""
    version = f"202611{1 + n // 24:02d}T{n % 24:02d}0000Z-{n:07x}"
    folder = tmp_path / f"r{n}"
    build_release(repo, folder, data_version=version, generated=f"2026-11-{1 + n // 24:02d}T{n % 24:02d}:00:00Z")
    return folder, version


def chain(repo, tmp_path, count, add=None):
    """Build count releases in turn, each from the one before, and return [(folder, version, manifest)]."""
    built = []
    for n in range(count):
        if add:
            add(n)
        folder, version = release_folder(repo, tmp_path, n)
        manifest = database.build(folder, previous=[built[-1][0]] if built else [])
        built.append((folder, version, manifest))
    return built


def path_to(changesets, start, end):
    """The changesets from start to end, found from one manifest's list (breadth first, fewest files)."""
    paths = {start: []}
    frontier = [start]
    while frontier:
        step = []
        for version in frontier:
            for entry in changesets:
                if entry["from"] == version and entry["to"] not in paths:
                    paths[entry["to"]] = paths[version] + [entry]
                    step.append(entry["to"])
        frontier = step
    return paths.get(end)


def test_the_list_carries_forward_and_keeps_the_newest_50_releases(repo, v1_osm, tmp_path, monkeypatch):
    sync(repo, extract_file(v1_osm), now="2026-10-01T16:00:00Z")
    monkeypatch.setattr(database, "CHAIN_RELEASES", 5)
    built = chain(repo, tmp_path, 8)
    versions = [version for _, version, _ in built]
    changesets = built[-1][2]["changesets"]
    # Releases 3 to 7 are the 5 newest with a changeset into them.
    assert [(entry["from"], entry["to"]) for entry in changesets] == [
        (versions[n - 1], versions[n]) for n in range(3, 8)
    ]
    assert changesets == sorted(changesets, key=lambda entry: entry["from"])
    # Each entry points at the release that made it: nothing is uploaded again.
    for entry in changesets:
        tag = "data-" + entry["to"].split("-")[0]
        assert entry["url"] == f"https://github.com/opencampingdb/data/releases/download/{tag}/{entry['file']}"
    assert sorted(p.name for p in built[-1][0].glob("changeset-*")) == [f"changeset-{versions[6]}.bin.gz"]
    assert database.CHAIN_RELEASES == 5
    monkeypatch.undo()
    assert database.CHAIN_RELEASES == 50


def test_a_path_from_three_releases_back_is_found_from_one_manifest(repo, v1_osm, v2_osm, tmp_path):
    sync(repo, extract_file(v1_osm), now="2026-10-01T16:00:00Z")
    ids = place_ids(repo)
    changes = {
        1: lambda: add_review(repo, ids[0], "rev_01JBX0000000000000000000AA"),
        2: lambda: sync(repo, extract_file(v2_osm), now="2026-11-01T01:30:00Z"),
        3: lambda: add_review(repo, ids[1], "rev_01JBX0000000000000000000AB"),
    }
    built = chain(repo, tmp_path, 4, add=lambda n: changes.get(n, lambda: None)())
    (first, start, _), (last, end, manifest) = built[0], built[-1]

    path = path_to(manifest["changesets"], start, end)
    assert [entry["from"] for entry in path] == [version for _, version, _ in built[:3]]
    db = unpack(first, tmp_path, "phone.sqlite")
    folders = {version: folder for folder, version, _ in built}
    con = apsw.Connection(str(db))
    for entry in path:
        packed = (folders[entry["to"]] / entry["file"]).read_bytes()
        assert len(packed) == entry["bytes"]
        assert hashlib.sha256(packed).hexdigest() == entry["sha256"]
        apsw.Changeset.apply(gzip.decompress(packed), con)
    con.close()
    assert rows(db) == rows(unpack(last, tmp_path, "last.sqlite"))


def test_a_layout_change_resets_the_list(repo, v1_osm, tmp_path, monkeypatch):
    sync(repo, extract_file(v1_osm), now="2026-10-01T16:00:00Z")
    built = chain(repo, tmp_path, 3)
    assert len(built[-1][2]["changesets"]) == 2
    monkeypatch.setattr(database, "LAYOUT_VERSION", 3)
    folder, version = release_folder(repo, tmp_path, 3)
    assert database.build(folder, previous=[built[-1][0]])["changesets"] == []
    folder2, version2 = release_folder(repo, tmp_path, 4)
    assert [(e["from"], e["to"]) for e in database.build(folder2, previous=[folder])["changesets"]] == [(version, version2)]


def test_day_changesets_stay_in_the_list_marked_day(releases, repo, tmp_path):
    first, second = releases
    database.build(first)
    database.build(second, previous=[first])
    third = tmp_path / "third"
    build_release(repo, third, data_version=THIRD, generated="2026-10-31T09:00:00Z")
    database.build(third, previous=[second], day=first)
    fourth = tmp_path / "fourth"
    build_release(repo, fourth, data_version="20261031T100000Z-1111111", generated="2026-10-31T10:00:00Z")
    manifest = database.build(fourth, previous=[third])
    assert [(e["from"], e["to"], e["day"]) for e in manifest["changesets"]] == [
        (FIRST, SECOND, False),
        (FIRST, THIRD, True),
        (SECOND, THIRD, False),
        (THIRD, "20261031T100000Z-1111111", False),
    ]


# ---------------------------------------------------- audit run-2: OCDB-NV-02


def tamper(folder, change):
    """Rewrite only the changesets list of a built release's manifest; its database stays consistent."""
    path = folder / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["changesets"] = change(manifest["changesets"])
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def forged(entry, **changes):
    return {**entry, **changes}


def test_carried_entries_must_be_this_repositorys_release_assets(repo, v1_osm, tmp_path):
    sync(repo, extract_file(v1_osm), now="2026-10-01T16:00:00Z")
    built = chain(repo, tmp_path, 3)
    honest = built[-1][2]["changesets"]
    far = "99991231T235959Z-0000000"
    bad = [
        # Foreign host.
        forged(honest[0], url="https://example.invalid/dummy-changeset.bin.gz"),
        # Far-future "to", so it would never age out.
        forged(honest[0], to=far, url=f"https://github.com/opencampingdb/data/releases/download/data-{far.split('-')[0]}/{honest[0]['file']}"),
        # Own repository, but a release other than the one named by "to".
        forged(honest[1], url=honest[0]["url"].rsplit("/", 1)[0] + "/" + honest[1]["file"]),
        # Another repository on GitHub.
        forged(honest[1], url=honest[1]["url"].replace("opencampingdb/data", "someone/else")),
    ]
    tamper(built[-1][0], lambda entries: entries + bad)
    folder, version = release_folder(repo, tmp_path, 3)
    manifest = database.build(folder, previous=[built[-1][0]])
    urls = [entry["url"] for entry in manifest["changesets"]]
    assert not any("example.invalid" in url or "someone/else" in url for url in urls)
    assert all(entry["to"] != far for entry in manifest["changesets"])
    assert [(e["from"], e["to"]) for e in manifest["changesets"]] == [
        (built[0][1], built[1][1]), (built[1][1], built[2][1]), (built[2][1], version),
    ]
    for entry in manifest["changesets"]:
        tag = "data-" + entry["to"].split("-")[0]
        assert entry["url"] == f"https://github.com/opencampingdb/data/releases/download/{tag}/{entry['file']}"


def test_a_forged_entry_does_not_survive_later_releases(repo, v1_osm, tmp_path):
    sync(repo, extract_file(v1_osm), now="2026-10-01T16:00:00Z")
    built = chain(repo, tmp_path, 2)
    far = "99991231T235959Z-0000000"
    tamper(built[-1][0], lambda entries: entries + [forged(entries[0], to=far, url="https://example.invalid/x.bin.gz")])
    second = release_folder(repo, tmp_path, 2)[0]
    database.build(second, previous=[built[-1][0]])
    third = release_folder(repo, tmp_path, 3)[0]
    manifest = database.build(third, previous=[second])
    assert all("example.invalid" not in e["url"] and e["to"] != far for e in manifest["changesets"])


def test_previous_base_skips_tags_not_earlier_than_this_release():
    tags = [
        "data-20261031T080000Z",
        "data-20261031T090000Z",  # THIRD's own tag
        "data-99991231T235959Z",  # a tag from the future
        "v1.0",
    ]
    assert database.previous_base(tags, THIRD) == "data-20261031T080000Z"
    assert database.previous_base(tags[1:], THIRD) is None
    assert database.previous_base([], THIRD) is None
