"""Pulls one Overture Maps release's places for Australia into an extract.

Reads the places theme and the places changelog of one release from Overture's
public bucket with DuckDB, keeps the places inside the Australian bounding box
and only the fields the importer uses, and writes them as sorted NDJSON. The
format is in pipeline/README.md, under "Overture extract".

    python tools/overture-pull/pull.py 2026-10-21.0 out/overture-2026-10-21.0.ndjson

Install with `pip install -r tools/overture-pull/requirements.txt` first. Only
the DuckDB read needs the network, and only it imports duckdb.
"""

import argparse
import json

COORD_PLACES = 7
EXTRACT = "overture-places"
VERSION = 2
# Australia, as [west, south, east, north] in degrees.
BBOX = (112.0, -44.5, 154.5, -9.0)
REMOVED = "removed"
CHANGES = (None, "added", "data_changed", "unchanged")
HEADER_KEYS = ("extract", "version", "release", "bbox", "places", "removed")
PLACE_KEYS = (
    "id", "change", "name", "basic_category", "taxonomy_primary", "confidence",
    "websites", "socials", "phones", "addresses", "sources", "lon", "lat",
)


def _centre(bbox, low, high):
    return round((bbox[low] + bbox[high]) / 2, COORD_PLACES)


def place_line(row, change):
    """One Overture place row as one extract line, without its trailing newline."""
    names = row.get("names") or {}
    taxonomy = row.get("taxonomy") or {}
    bbox = row["bbox"]
    line = {
        "id": row["id"],
        "change": change,
        "name": names.get("primary"),
        "basic_category": row.get("basic_category"),
        "taxonomy_primary": taxonomy.get("primary"),
        "confidence": row.get("confidence"),
        "websites": list(row.get("websites") or []),
        "socials": list(row.get("socials") or []),
        "phones": list(row.get("phones") or []),
        "addresses": [
            {key: address.get(key) for key in ("freeform", "locality", "postcode", "region", "country")}
            for address in row.get("addresses") or []
        ],
        "sources": [
            {key: source.get(key) for key in ("dataset", "record_id", "update_time")}
            for source in row.get("sources") or []
        ],
        "lon": _centre(bbox, "xmin", "xmax"),
        "lat": _centre(bbox, "ymin", "ymax"),
    }
    return json.dumps(line, ensure_ascii=False, separators=(",", ":"))


def removed_line(gers_id):
    """One extract line for a place the changelog lists as removed."""
    return json.dumps({"id": gers_id, "change": REMOVED}, separators=(",", ":"))


def write_extract(out, release, rows, changes):
    """Writes the extract: a header line, then one line per place or removal, sorted by id.

    rows are the release's place rows. changes maps an id to its changelog
    change type. A removed id gets a removal line. Any other changelog id
    with no place row is left out.
    """
    lines = {}
    for row in rows:
        lines[row["id"]] = place_line(row, changes.get(row["id"]))
    removed = 0
    for gers_id, change in changes.items():
        if change == REMOVED and gers_id not in lines:
            lines[gers_id] = removed_line(gers_id)
            removed += 1
    header = {
        "extract": EXTRACT,
        "version": VERSION,
        "release": release,
        "bbox": list(BBOX),
        "places": len(lines) - removed,
        "removed": removed,
    }
    with open(out, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(header, separators=(",", ":")) + "\n")
        for gers_id in sorted(lines):
            handle.write(lines[gers_id] + "\n")


def check_extract(lines):
    """Lists every way the extract lines break the format. An empty list means none."""
    problems = []
    if not lines:
        return ["empty extract"]
    try:
        header = json.loads(lines[0])
    except ValueError:
        return ["line 1: not JSON"]
    if not isinstance(header, dict) or tuple(header) != HEADER_KEYS:
        problems.append(f"line 1: header keys must be {', '.join(HEADER_KEYS)}")
        header = {}
    elif header["extract"] != EXTRACT or header["version"] != VERSION:
        problems.append(f"line 1: not a version {VERSION} {EXTRACT} extract")
    places = removed = 0
    previous = None
    for number, text in enumerate(lines[1:], start=2):
        try:
            line = json.loads(text)
        except ValueError:
            problems.append(f"line {number}: not JSON")
            continue
        gers_id = line.get("id") if isinstance(line, dict) else None
        if not isinstance(gers_id, str) or not gers_id:
            problems.append(f"line {number}: no id")
            continue
        if previous is not None and gers_id <= previous:
            problems.append(f"line {number}: id {gers_id} out of order")
        previous = gers_id
        if line.get("change") == REMOVED:
            removed += 1
            if set(line) != {"id", "change"}:
                problems.append(f"line {number}: a removal has only id and change")
            continue
        places += 1
        extra = sorted(set(line) - set(PLACE_KEYS))
        missing = [key for key in PLACE_KEYS if key not in line]
        if extra:
            problems.append(f"line {number}: fields never taken: {', '.join(extra)}")
        if missing:
            problems.append(f"line {number}: missing {', '.join(missing)}")
        if line.get("change") not in CHANGES:
            problems.append(f"line {number}: unknown change {line.get('change')!r}")
        if not isinstance(line.get("lon"), (int, float)) or not isinstance(line.get("lat"), (int, float)):
            problems.append(f"line {number}: lon and lat must be numbers")
    if header and (header["places"], header["removed"]) != (places, removed):
        problems.append(f"line 1: header counts {header['places']} places and {header['removed']} removed, "
                        f"the lines hold {places} and {removed}")
    return problems


BUCKET = "s3://overturemaps-us-west-2"
REGION = "us-west-2"
BATCH = 50_000


def _inside(column="bbox"):
    west, south, east, north = BBOX
    return (f"{column}.xmin between {west} and {east} and {column}.ymin between {south} and {north}")


def read_release(release):
    """Reads the release's Australian place rows and changelog. Needs the network."""
    import duckdb

    con = duckdb.connect()
    con.sql(f"INSTALL httpfs; LOAD httpfs; SET s3_region='{REGION}';")
    changes = dict(
        con.sql(
            f"""select id, change_type
            from read_parquet('{BUCKET}/changelog/{release}/theme=places/type=place/*/*', hive_partitioning = true)
            where {_inside()}"""
        ).fetchall()
    )
    result = con.execute(
        f"""select id, names, basic_category, taxonomy, confidence, websites, socials, phones, addresses, sources, bbox
        from read_parquet('{BUCKET}/release/{release}/theme=places/type=place/*')
        where {_inside()}"""
    )
    columns = [column[0] for column in result.description]

    def rows():
        while batch := result.fetchmany(BATCH):
            for values in batch:
                yield dict(zip(columns, values))

    return rows(), changes


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("release", help="Overture release, such as 2026-10-21.0")
    parser.add_argument("out", help="extract file to write")
    args = parser.parse_args(argv)
    rows, changes = read_release(args.release)
    write_extract(args.out, args.release, rows, changes)
    with open(args.out, encoding="utf-8") as handle:
        header = json.loads(handle.readline())
    print(f"{args.out}: {header['places']} places, {header['removed']} removed")


if __name__ == "__main__":
    main()
