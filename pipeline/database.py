"""Build the release's SQLite database, ocdb.sqlite.gz, and changesets to it
from earlier releases' databases, and add them to the manifest.

Usage:
    python -m pipeline.database --release-dir dist [--previous DIR ...]

--release-dir holds the files pipeline.release wrote: manifest.json and the
data files. The database is built from those files alone, so it holds
exactly what the release holds, plus each place's state (pipeline.states).

Each --previous DIR holds an earlier release's manifest.json and
ocdb.sqlite.gz, downloaded from its GitHub release. For each one whose
database checks out against its manifest and has the same LAYOUT_VERSION,
this writes a changeset in SQLite's session extension format that turns that
database into this one. Before writing it, the changeset is applied to a copy
of the earlier database and every table compared with this release's, so a
changeset that does not give exactly this release never ships. An earlier
release without a database, with another layout or with this release's own
data version is skipped.

A changeset holds the old values of every row it deletes, a review's text
among them. So no changeset is written from a database holding a review that
left this release, unless its contributor replaced it with a newer one and
its id is not on moderation/removed_reviews.json (--root, the data
repository, default .). pipeline/README.md, "Database and changesets", has
the full rule. Apps on that release download the whole database.

The changeset from the release just before is changeset-<its data_version>.bin.gz.
Pass --day DIR (also a --previous) for the last release of the previous UTC
day: its changeset is changeset-day-<that day, YYYY-MM-DD>.bin.gz, unless it
is the release just before, which has one already.

The manifest's "changesets" list carries this release's new changesets and
every entry of the release just before's list, trimmed to the changesets into
the CHAIN_RELEASES newest releases and ordered by "from", so an app finds its
whole path from one manifest. Each entry's url points at the release that
made it, so no changeset is uploaded twice. The list starts again when the
release just before has no database of this layout.

The release just before is a downloaded GitHub release, so its list is not
trusted as it stands. An entry is carried only when its file is the name
this module gives a changeset from its "from", its url is this repository's
download url for that file in the release named by its "to", and its "to"
is not later than the release just before. Any other entry is left out and
named in the log, so a forged entry can neither point apps at another host
nor stay in the list for ever with a far-future "to".

    python -m pipeline.database --day-base <data_version>

reads release tags on standard input and prints the newest tag from a UTC day
before the one data_version was made on, or nothing.

    python -m pipeline.database --previous-base <data_version>

reads release tags on standard input and prints them newest first, keeping
only data- tags made before data_version, one per line. The release workflow
takes the first that it made from main as the release just before.

SQLite comes from apsw, which bundles its own SQLite built with the session
extension (https://rogerbinns.github.io/apsw/session.html), so the SQLite
version is pinned in requirements.txt and the same input gives a
byte-identical database on any machine.

LAYOUT_VERSION is the version of the tables below. Raise it by hand whenever
a table, column or index changes: no changeset is made across a change, so
apps download the whole database once and refuse a layout they cannot read.
"""

import argparse
import gzip
import hashlib
import json
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import apsw

from .common import load_removed, write_text
from .release import asset_url, gzip_bytes, release_tag
from .states import StateLookup

# 1: the first layout. 2: the fees table is gone (release format 13).
LAYOUT_VERSION = 2
DATABASE_FILE = "ocdb.sqlite.gz"
PAGE_SIZE = 4096
DEFAULT_REPOSITORY = "opencampingdb/data"
# The manifest lists the changesets into each of this many newest releases.
CHAIN_RELEASES = 50

SCHEMA = """
CREATE TABLE release (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    data_version TEXT NOT NULL,
    release TEXT NOT NULL,
    generated TEXT NOT NULL,
    format_version INTEGER NOT NULL,
    format_minor INTEGER NOT NULL,
    layout_version INTEGER NOT NULL,
    licence TEXT NOT NULL,
    attribution TEXT NOT NULL
);
CREATE TABLE places (
    id TEXT PRIMARY KEY NOT NULL,
    name TEXT,
    kind TEXT NOT NULL,
    "group" TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    status TEXT NOT NULL,
    state TEXT,
    standing TEXT,
    rating_count INTEGER,
    rating_mean REAL,
    updated TEXT NOT NULL,
    record TEXT NOT NULL
);
CREATE TABLE merged (
    id TEXT PRIMARY KEY NOT NULL,
    merged_into TEXT NOT NULL
);
CREATE TABLE reviews (
    id TEXT PRIMARY KEY NOT NULL,
    place TEXT NOT NULL,
    stars INTEGER NOT NULL,
    visited TEXT NOT NULL,
    created TEXT NOT NULL,
    record TEXT NOT NULL
);
CREATE TABLE commons (
    place TEXT PRIMARY KEY NOT NULL,
    files TEXT NOT NULL
);
"""

INDEXES = """
CREATE INDEX places_kind ON places (kind);
CREATE INDEX places_group ON places ("group");
CREATE INDEX places_state ON places (state);
CREATE INDEX places_lat_lon ON places (lat, lon);
CREATE INDEX reviews_place ON reviews (place);
"""

# Every table, with the column it is keyed and ordered by.
TABLES = {
    "release": "id",
    "places": "id",
    "merged": "id",
    "reviews": "id",
    "commons": "place",
}


class ChangesetError(Exception):
    """A changeset did not turn the earlier database into exactly this release's."""


def read_lines(path: Path) -> list[tuple[dict, str]]:
    """(record, line) for each line of a gzipped NDJSON file."""
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return [(json.loads(line), line.rstrip("\n")) for line in f if line.strip()]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def build_database(release_dir: Path, out_path: Path, states: StateLookup | None = None) -> None:
    """Write the release in release_dir as an SQLite database at out_path.

    Rows go in in key order, indexes are made after the rows, and the file is
    written by VACUUM INTO with a fixed page size, so the same release gives
    a byte-identical file.
    """
    release_dir, out_path = Path(release_dir), Path(out_path)
    states = states or StateLookup()
    manifest = json.loads((release_dir / "manifest.json").read_text(encoding="utf-8"))
    out_path.unlink(missing_ok=True)

    with tempfile.TemporaryDirectory() as work:
        con = apsw.Connection(str(Path(work) / "build.sqlite"))
        con.execute(f"PRAGMA page_size = {PAGE_SIZE}; PRAGMA journal_mode = OFF; PRAGMA synchronous = OFF")
        with con:
            con.execute(SCHEMA)
            con.execute(
                "INSERT INTO release VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    manifest["data_version"], manifest["release"], manifest["generated"],
                    manifest["format_version"], manifest["format_minor"], LAYOUT_VERSION,
                    manifest["licence"], manifest["attribution"],
                ),
            )
            places = sorted(read_lines(release_dir / manifest["places"]["file"]), key=lambda p: p[0]["id"])
            place_states = states.states_of([(r["location"]["lon"], r["location"]["lat"]) for r, _ in places])
            con.executemany(
                "INSERT INTO places VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    (
                        r["id"], r.get("name"), r["kind"], r["group"],
                        r["location"]["lat"], r["location"]["lon"], r["status"],
                        state,
                        r.get("standing"),
                        (r.get("rating") or {}).get("count"), (r.get("rating") or {}).get("mean"),
                        r["updated"], line,
                    )
                    for (r, line), state in zip(places, place_states)
                ),
            )
            merged = sorted(read_lines(release_dir / manifest["merged"]["file"]), key=lambda m: m[0]["id"])
            con.executemany("INSERT INTO merged VALUES (?, ?)", ((r["id"], r["merged_into"]) for r, _ in merged))
            reviews = sorted(read_lines(release_dir / manifest["reviews"]["file"]), key=lambda m: m[0]["id"])
            con.executemany(
                "INSERT INTO reviews VALUES (?, ?, ?, ?, ?, ?)",
                ((r["id"], r["place"], r["stars"], r["visited"], r["created"], line) for r, line in reviews),
            )
            commons = sorted(read_lines(release_dir / manifest["commons"]["file"]), key=lambda m: m[0]["place"])
            con.executemany(
                "INSERT INTO commons VALUES (?, ?)",
                ((r["place"], json.dumps(r["files"], sort_keys=True, separators=(",", ":"), ensure_ascii=False))
                 for r, _ in commons),
            )
            con.execute(INDEXES)
            con.execute(f"PRAGMA user_version = {LAYOUT_VERSION}")
        con.execute("VACUUM INTO ?", (str(out_path),))
        con.close()


def table_rows(db_path: Path, table: str):
    """Every row of one table, in key order."""
    con = apsw.Connection(str(db_path), flags=apsw.SQLITE_OPEN_READONLY)
    try:
        yield from con.execute(f'SELECT * FROM "{table}" ORDER BY "{TABLES[table]}"')
    finally:
        con.close()


def same_rows(a: Path, b: Path) -> list[str]:
    """The tables whose rows differ between two databases (empty when every table matches)."""
    differ = []
    for table in TABLES:
        rows_a, rows_b = table_rows(a, table), table_rows(b, table)
        sentinel = object()
        while True:
            row_a, row_b = next(rows_a, sentinel), next(rows_b, sentinel)
            if row_a != row_b:
                differ.append(table)
                break
            if row_a is sentinel:
                break
        rows_a.close()
        rows_b.close()
    return differ


def layout_version(db_path: Path) -> int:
    con = apsw.Connection(str(db_path), flags=apsw.SQLITE_OPEN_READONLY)
    try:
        return con.execute("SELECT layout_version FROM release").get
    finally:
        con.close()


def make_changeset(old_db: Path, new_db: Path) -> bytes:
    """The changeset that turns old_db into new_db, from sqlite3session_diff on every table."""
    con = apsw.Connection(str(new_db), flags=apsw.SQLITE_OPEN_READONLY | apsw.SQLITE_OPEN_URI)
    try:
        con.execute("ATTACH ? AS old", (f"file:{Path(old_db).resolve()}?mode=ro",))
        session = apsw.Session(con, "main")
        for table in TABLES:
            session.attach(table)
            session.diff("old", table)
        changeset = session.changeset()
        session.close()
        return changeset
    finally:
        con.close()


def apply_changeset(db_path: Path, changeset: bytes) -> None:
    """Apply a changeset with sqlite3changeset_apply, aborting on any conflict."""
    con = apsw.Connection(str(db_path))
    try:
        apsw.Changeset.apply(changeset, con)
    finally:
        con.close()


def checked_changeset(old_db: Path, new_db: Path) -> bytes:
    """make_changeset, after proving that applying it to a copy of old_db gives new_db's rows."""
    changeset = make_changeset(old_db, new_db)
    with tempfile.TemporaryDirectory() as work:
        copy = Path(work) / "applied.sqlite"
        shutil.copyfile(old_db, copy)
        try:
            apply_changeset(copy, changeset)
        except apsw.Error as error:
            raise ChangesetError(f"the changeset does not apply cleanly to {old_db}: {error}") from error
        differ = same_rows(copy, new_db)
        if differ:
            raise ChangesetError(f"applying the changeset to {old_db} leaves {', '.join(differ)} different")
    return changeset


def review_rows(db_path: Path) -> list[tuple[str, str, str | None, datetime]]:
    """(id, place, contributor, created) of every row of a database's reviews table."""
    con = apsw.Connection(str(db_path), flags=apsw.SQLITE_OPEN_READONLY)
    try:
        found = con.execute("SELECT id, place, record, created FROM reviews ORDER BY id").fetchall()
    finally:
        con.close()
    rows = []
    for review_id, place, record, created in found:
        contributor = json.loads(record).get("contributor")
        when = datetime.fromisoformat(created.replace("Z", "+00:00"))
        rows.append((review_id, place, contributor if isinstance(contributor, str) else None, when))
    return rows


def blocking_review_id(old_db: Path, new_db: Path, removed: set[str] = frozenset()) -> str | None:
    """The id of a review of old_db that stops a changeset to new_db, or None.

    A changeset holds every old column of a row it deletes, the review's
    text among them, so a review that left new_db stops the changeset unless
    its contributor replaced it with a newer review and its id is not in
    removed. pipeline/README.md, "Database and changesets", has the full
    rule.
    """
    old = review_rows(old_db)
    before = {review_id for review_id, _, _, _ in old}
    new = review_rows(new_db)
    kept = {review_id for review_id, _, _, _ in new}
    latest: dict[tuple[str, str], datetime] = {}
    for review_id, place, contributor, created in new:
        if contributor and review_id not in before:
            key = (place, contributor)
            latest[key] = max(latest.get(key, created), created)
    for review_id, place, contributor, created in old:
        if review_id in removed:
            return review_id
        if review_id in kept:
            continue
        replacement = latest.get((place, contributor)) if contributor else None
        if replacement is None or replacement <= created:
            return review_id
    return None


def previous_database(directory: Path, work: Path) -> tuple[str, Path] | None:
    """(data_version, unpacked database) of an earlier release, or None when it has none this release can use."""
    directory = Path(directory)
    manifest_path = directory / "manifest.json"
    if not manifest_path.exists():
        print(f"{directory}: no manifest.json, skipped")
        return None
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = manifest.get("database")
    packed = directory / DATABASE_FILE
    if not entry or not packed.exists():
        print(f"{directory}: release {manifest.get('data_version')} has no database, skipped")
        return None
    if manifest.get("layout_version") != LAYOUT_VERSION:
        print(f"{directory}: layout {manifest.get('layout_version')} is not {LAYOUT_VERSION}, skipped")
        return None
    if packed.stat().st_size != entry["bytes"] or sha256_file(packed) != entry["sha256"]:
        raise ChangesetError(f"{packed} does not match its manifest")
    unpacked = work / f"{manifest['data_version']}.sqlite"
    with gzip.open(packed, "rb") as source, open(unpacked, "wb") as target:
        shutil.copyfileobj(source, target)
    if layout_version(unpacked) != LAYOUT_VERSION:
        print(f"{directory}: the database's layout is not {LAYOUT_VERSION}, skipped")
        return None
    return manifest["data_version"], unpacked


def day_of(data_version: str) -> str:
    """YYYY-MM-DD of a data_version (<YYYYMMDDTHHMMSSZ>-<sha>)."""
    return f"{data_version[0:4]}-{data_version[4:6]}-{data_version[6:8]}"


def day_base(tags: list[str], data_version: str) -> str | None:
    """The newest data- tag made on a UTC day before data_version's day, or None."""
    today = data_version[:8]
    earlier = [tag for tag in tags if tag.startswith("data-") and tag[5:13] < today]
    return max(earlier) if earlier else None


def previous_base(tags: list[str], data_version: str) -> str | None:
    """The newest data- tag made before data_version, or None. A tag from the future is never chosen."""
    earlier = earlier_tags(tags, data_version)
    return earlier[0] if earlier else None


def earlier_tags(tags: list[str], data_version: str) -> list[str]:
    """The data- tags made before data_version, newest first."""
    this = release_tag(data_version)
    return sorted((tag for tag in tags if tag.startswith("data-") and tag < this), reverse=True)


def changeset_name(from_version: str, is_day: bool) -> str:
    """The file name this module gives a changeset from from_version."""
    return f"changeset-day-{day_of(from_version)}.bin.gz" if is_day else f"changeset-{from_version}.bin.gz"


def carried_entry_problem(entry, repository: str, before: str) -> str | None:
    """Why an entry of the previous release's list must not be carried, or None when it may."""
    if not isinstance(entry, dict):
        return "not an object"
    from_version, to_version = entry.get("from"), entry.get("to")
    if not isinstance(from_version, str) or not isinstance(to_version, str):
        return "no from or to"
    if not (from_version < to_version and release_tag(to_version) <= release_tag(before)):
        return f"to {to_version} is not between from {from_version} and the release just before, {before}"
    name = changeset_name(from_version, entry.get("day") is True)
    if entry.get("file") != name:
        return f"file {entry.get('file')!r} is not {name}"
    if entry.get("url") != asset_url(repository, release_tag(to_version), name):
        return f"url {entry.get('url')!r} is not this repository's release asset"
    if not isinstance(entry.get("sha256"), str) or not isinstance(entry.get("bytes"), int):
        return "no sha256 or bytes"
    return None


def carried(directory: Path, repository: str = DEFAULT_REPOSITORY) -> list[dict]:
    """The changesets the release just before listed, to carry into this release's list.

    Only called once that release's database has checked out with this
    layout, so every entry is of this layout. Entries without "to" (from
    before the list carried forward) are left out, and so is any entry
    carried_entry_problem refuses: the list comes from a downloaded release.
    """
    manifest = json.loads((Path(directory) / "manifest.json").read_text(encoding="utf-8"))
    before = manifest.get("data_version")
    if not isinstance(before, str):
        return []
    kept = []
    for entry in manifest.get("changesets", []):
        if isinstance(entry, dict) and "to" not in entry:
            continue
        problem = carried_entry_problem(entry, repository, before)
        if problem:
            print(f"{directory}: not carrying a changeset entry: {problem}")
            continue
        kept.append(entry)
    return kept


def trimmed(entries: list[dict]) -> list[dict]:
    """The entries whose "to" is one of the CHAIN_RELEASES newest releases, one per (from, to), ordered by from."""
    unique = {(entry["from"], entry["to"]): entry for entry in entries}
    newest = set(sorted({to for _, to in unique}, reverse=True)[:CHAIN_RELEASES])
    return [unique[key] for key in sorted(unique) if key[1] in newest]


def build(
    release_dir: Path,
    previous: list[Path] = (),
    day: Path | None = None,
    repository: str = DEFAULT_REPOSITORY,
    states: StateLookup | None = None,
    removed: set[str] = frozenset(),
) -> dict:
    """Write ocdb.sqlite.gz and the changesets into release_dir, add them to manifest.json and return it.

    previous lists earlier release directories, the release just before
    first; day is the last release of the previous UTC day, if any.
    removed holds the ids on moderation/removed_reviews.json: no changeset
    is made from a database that holds one of them.
    """
    release_dir = Path(release_dir)
    manifest_path = release_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    release = manifest["release"]
    entries = []

    with tempfile.TemporaryDirectory() as work:
        work = Path(work)
        database = work / "ocdb.sqlite"
        build_database(release_dir, database, states)
        packed = gzip_bytes(database.read_bytes())
        (release_dir / DATABASE_FILE).write_bytes(packed)
        manifest["layout_version"] = LAYOUT_VERSION
        manifest["database"] = {
            "file": DATABASE_FILE,
            "url": asset_url(repository, release, DATABASE_FILE),
            "sha256": hashlib.sha256(packed).hexdigest(),
            "bytes": len(packed),
        }

        sources = [(path, False) for path in previous] + ([(day, True)] if day else [])
        done = {manifest["data_version"]}
        for index, (directory, is_day) in enumerate(sources):
            found = previous_database(directory, work)
            if not found or found[0] in done:
                continue
            from_version, old_db = found
            done.add(from_version)
            if index == 0 and not is_day:
                entries.extend(carried(directory, repository))
            name = changeset_name(from_version, is_day)
            blocking = blocking_review_id(old_db, database, removed)
            if blocking in removed:
                print(f"{directory}: review {blocking} of {from_version} is on the removed list, no {name}")
                continue
            if blocking:
                print(f"{directory}: review {blocking} of {from_version} left with no newer review by its contributor, no {name}")
                continue
            changeset = gzip_bytes(checked_changeset(old_db, database))
            (release_dir / name).write_bytes(changeset)
            entries.append({
                "file": name,
                "url": asset_url(repository, release, name),
                "sha256": hashlib.sha256(changeset).hexdigest(),
                "bytes": len(changeset),
                "from": from_version,
                "to": manifest["data_version"],
                "day": is_day,
            })

    manifest["changesets"] = trimmed(entries)
    write_text(manifest_path, json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--release-dir", type=Path, help="the folder pipeline.release wrote")
    parser.add_argument("--previous", type=Path, action="append", default=[],
                        help="an earlier release's folder with manifest.json and ocdb.sqlite.gz, the newest first")
    parser.add_argument("--day", type=Path, help="the last release of the previous UTC day, as for --previous")
    parser.add_argument("--root", type=Path, default=Path("."),
                        help="the data repository, for moderation/removed_reviews.json (default: .)")
    parser.add_argument("--repository", default=DEFAULT_REPOSITORY,
                        help=f"owner/name used in the asset URLs (default {DEFAULT_REPOSITORY})")
    parser.add_argument("--day-base", metavar="DATA_VERSION",
                        help="print the newest tag on standard input from a UTC day before this one, and stop")
    parser.add_argument("--previous-base", metavar="DATA_VERSION",
                        help="print the data- tags on standard input made before this one, newest first, and stop")
    args = parser.parse_args(argv)

    if args.previous_base:
        for tag in earlier_tags(sys.stdin.read().split(), args.previous_base):
            print(tag)
        return 0
    if args.day_base:
        tag = day_base(sys.stdin.read().split(), args.day_base)
        if tag:
            print(tag)
        return 0
    if not args.release_dir:
        parser.error("--release-dir is required")
    manifest = build(args.release_dir, args.previous, args.day, args.repository, removed=load_removed(args.root))
    print(json.dumps({key: manifest[key] for key in ("layout_version", "database", "changesets")}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
