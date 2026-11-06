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

    python -m pipeline.database --day-base <data_version>

reads release tags on standard input and prints the newest tag from a UTC day
before the one data_version was made on, or nothing.

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
from pathlib import Path

import apsw

from .common import write_text
from .release import asset_url, gzip_bytes
from .states import StateLookup

# 1: the first layout.
LAYOUT_VERSION = 1
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
CREATE TABLE fees (
    id TEXT PRIMARY KEY NOT NULL,
    place TEXT NOT NULL,
    amount REAL NOT NULL,
    site TEXT NOT NULL,
    paid TEXT NOT NULL,
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
CREATE INDEX fees_place ON fees (place);
"""

# Every table, with the column it is keyed and ordered by.
TABLES = {
    "release": "id",
    "places": "id",
    "merged": "id",
    "reviews": "id",
    "fees": "id",
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
            con.executemany(
                "INSERT INTO places VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    (
                        r["id"], r.get("name"), r["kind"], r["group"],
                        r["location"]["lat"], r["location"]["lon"], r["status"],
                        states.state_of(r["location"]["lon"], r["location"]["lat"]),
                        r.get("standing"),
                        (r.get("rating") or {}).get("count"), (r.get("rating") or {}).get("mean"),
                        r["updated"], line,
                    )
                    for r, line in places
                ),
            )
            merged = sorted(read_lines(release_dir / manifest["merged"]["file"]), key=lambda m: m[0]["id"])
            con.executemany("INSERT INTO merged VALUES (?, ?)", ((r["id"], r["merged_into"]) for r, _ in merged))
            reviews = sorted(read_lines(release_dir / manifest["reviews"]["file"]), key=lambda m: m[0]["id"])
            con.executemany(
                "INSERT INTO reviews VALUES (?, ?, ?, ?, ?, ?)",
                ((r["id"], r["place"], r["stars"], r["visited"], r["created"], line) for r, line in reviews),
            )
            fees = sorted(read_lines(release_dir / manifest["fees"]["file"]), key=lambda m: m[0]["id"])
            con.executemany(
                "INSERT INTO fees VALUES (?, ?, ?, ?, ?, ?)",
                ((r["id"], r["place"], r["amount"], r["site"], r["paid"], line) for r, line in fees),
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


def carried(directory: Path) -> list[dict]:
    """The changesets the release just before listed, to carry into this release's list.

    Only called once that release's database has checked out with this
    layout, so every entry is of this layout. Entries without "to" (from
    before the list carried forward) are left out.
    """
    manifest = json.loads((Path(directory) / "manifest.json").read_text(encoding="utf-8"))
    return [entry for entry in manifest.get("changesets", []) if "to" in entry and "from" in entry]


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
) -> dict:
    """Write ocdb.sqlite.gz and the changesets into release_dir, add them to manifest.json and return it.

    previous lists earlier release directories, the release just before
    first; day is the last release of the previous UTC day, if any.
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
                entries.extend(carried(directory))
            changeset = gzip_bytes(checked_changeset(old_db, database))
            name = f"changeset-day-{day_of(from_version)}.bin.gz" if is_day else f"changeset-{from_version}.bin.gz"
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
    parser.add_argument("--repository", default=DEFAULT_REPOSITORY,
                        help=f"owner/name used in the asset URLs (default {DEFAULT_REPOSITORY})")
    parser.add_argument("--day-base", metavar="DATA_VERSION",
                        help="print the newest tag on standard input from a UTC day before this one, and stop")
    args = parser.parse_args(argv)

    if args.day_base:
        tag = day_base(sys.stdin.read().split(), args.day_base)
        if tag:
            print(tag)
        return 0
    if not args.release_dir:
        parser.error("--release-dir is required")
    manifest = build(args.release_dir, args.previous, args.day, args.repository)
    print(json.dumps({key: manifest[key] for key in ("layout_version", "database", "changesets")}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
