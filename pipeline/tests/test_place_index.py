"""Tests for the place index: pipeline.release writes it, pipeline.place_index loads it into an intake."""

import gzip
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from pipeline.common import dump_record, place_path
from pipeline.place_index import TOKEN_ENV, changed_parts, lines_by_cell, main, upload, urllib_send
from pipeline.release import build_release

VERSION = "20261030T160000Z-abc1234"
GENERATED = "2026-10-30T16:00:00Z"
TOKEN = "places-token-for-the-data-release"


def add_place(repo, n, lat, lon, **changes):
    place_id = f"ocdb_01JC00000000000000000000{n:02d}"
    rec = {
        "id": place_id, "name": f"Place {n}", "kind": "free_camp", "group": "overnight",
        "location": {"lat": lat, "lon": lon}, "osm": None, "facts": {"toilets": True},
        "fact_sources": {"toilets": "osm"}, "status": "open", "rating": None,
        "updated": "2026-10-30T01:00:00Z", **changes,
    }
    path = place_path(repo, place_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_record(rec), encoding="utf-8", newline="\n")
    return place_id


def build(repo, out):
    build_release(repo, out, data_version=VERSION, generated=GENERATED)
    return out / "place-index.ndjson.gz"


def index_lines(path):
    return [json.loads(line) for line in gzip.decompress(path.read_bytes()).decode("utf-8").splitlines()]


class FakeIntake:
    """The intake's two place index routes, keeping cells and places as the intake does."""

    def __init__(self):
        self.hashes = {}
        self.places = {}
        self.calls = []

    def send(self, method, url, headers, body):
        self.calls.append((method, url))
        if headers.get("authorization") != f"Bearer {TOKEN}":
            return 401, b'{"error":"unauthorised"}'
        if method == "GET" and url.endswith("/v1/place-index/cells"):
            cells = [{"cell": list(cell), "hash": h} for cell, h in sorted(self.hashes.items())]
            return 200, json.dumps({"cells": cells}).encode()
        if method == "PUT" and url.endswith("/v1/place-index"):
            assert body[:2] == b"\x1f\x8b"
            lines = gzip.decompress(body).decode("utf-8").splitlines()
            named = json.loads(lines[0])["cells"]
            replaced = {tuple(c["cell"]) for c in named}
            self.places = {k: v for k, v in self.places.items() if tuple(v["cell"]) not in replaced}
            for line in lines[1:]:
                place = json.loads(line)
                self.places[place["id"]] = place
            for c in named:
                if c["hash"] is None:
                    self.hashes.pop(tuple(c["cell"]), None)
                else:
                    self.hashes[tuple(c["cell"])] = c["hash"]
            return 200, json.dumps({"cells": len(named), "places": len(lines) - 1}).encode()
        return 404, b'{"error":"not_found"}'


def test_release_writes_the_index_and_leaves_it_out_of_the_manifest(repo, tmp_path):
    first = add_place(repo, 1, -32.369322, 117.001853)
    dated = add_place(
        repo, 2, -31.95, 115.86, facts={"fee": False, "phone": "+61 8 9000 0000"},
        fact_sources={"fee": "feedback", "phone": "owner", "name": "feedback"},
        fact_dates={"fee": "2026-11-01T10:00:00Z", "phone": "2026-11-02T10:00:00Z", "name": "2026-11-01T10:00:00Z"},
    )
    add_place(repo, 3, -31.9, 115.9, status="merged", merged_into=dated)
    add_place(repo, 4, -31.9, 115.9, standing="held")
    edge = add_place(repo, 5, 90.0, 180.0, facts={}, fact_sources={})

    path = build(repo, tmp_path / "dist")
    manifest = json.loads((tmp_path / "dist" / "manifest.json").read_text(encoding="utf-8"))

    assert "place-index.ndjson.gz" not in json.dumps(manifest)
    assert index_lines(path) == [
        {"id": first, "cell": [117, -33], "facts": {"toilets": True}, "fact_sources": {"toilets": "osm"}, "fact_dates": {}},
        {
            "id": dated, "cell": [115, -32], "facts": {"fee": False, "phone": "+61 8 9000 0000"},
            "fact_sources": {"fee": "feedback", "phone": "owner", "name": "feedback"},
            "fact_dates": {"fee": "2026-11-01T10:00:00Z", "phone": "2026-11-02T10:00:00Z", "name": "2026-11-01T10:00:00Z"},
        },
        {"id": edge, "cell": [179, 89], "facts": {}, "fact_sources": {}, "fact_dates": {}},
    ]
    # Canonical lines: compact and sorted keys, so equal data gives an equal hash.
    first_line = gzip.decompress(path.read_bytes()).decode("utf-8").splitlines()[0]
    assert first_line.startswith('{"cell":[117,-33],"fact_dates":{},')


def test_upload_sends_only_the_cells_that_changed(repo, tmp_path):
    a = add_place(repo, 1, -32.4, 117.0)
    add_place(repo, 2, -31.95, 115.86)
    add_place(repo, 3, -31.90, 115.80)
    intake = FakeIntake()
    log = []

    assert upload(build(repo, tmp_path / "one"), "https://intake.example/", TOKEN, send=intake.send, log=log.append) == 2
    assert sorted(intake.places) == sorted(p["id"] for p in index_lines(tmp_path / "one" / "place-index.ndjson.gz"))
    assert intake.calls == [("GET", "https://intake.example/v1/place-index/cells"), ("PUT", "https://intake.example/v1/place-index")]

    # Nothing changed: one GET, no PUT.
    intake.calls.clear()
    assert upload(build(repo, tmp_path / "two"), "https://intake.example", TOKEN, send=intake.send, log=log.append) == 0
    assert [method for method, _ in intake.calls] == ["GET"]

    # One fact changed in one cell: only that cell goes.
    add_place(repo, 2, -31.95, 115.86, facts={"toilets": False})
    intake.calls.clear()
    assert upload(build(repo, tmp_path / "three"), "https://intake.example", TOKEN, send=intake.send, log=log.append) == 1
    assert intake.places["ocdb_01JC0000000000000000000002"]["facts"] == {"toilets": False}
    assert set(intake.hashes) == {(117, -33), (115, -32)}

    # The only place in a cell moved away: the cell goes with a null hash, and the place lands in its new cell.
    add_place(repo, 1, -31.5, 115.5)
    assert upload(build(repo, tmp_path / "four"), "https://intake.example", TOKEN, send=intake.send, log=log.append) == 2
    assert set(intake.hashes) == {(115, -32)}
    assert intake.places[a]["cell"] == [115, -32]


def test_parts_keep_each_cell_whole_and_stay_small():
    big = ['{"cell":[%d,-30],"id":"x%04d","pad":"%s"}\n' % (lon, i, "x" * 2000) for lon in range(140, 143) for i in range(300)]
    cells = lines_by_cell("".join(big))
    parts = changed_parts(cells, {(150, -30): "f" * 64})
    named = [[tuple(c["cell"]) for c in json.loads(p.split(b"\n", 1)[0])["cells"]] for p in parts]
    assert named == [[(140, -30)], [(141, -30)], [(142, -30), (150, -30)]]
    assert json.loads(parts[2].split(b"\n", 1)[0])["cells"][1] == {"cell": [150, -30], "hash": None}
    for part in parts:
        assert len(part) < 4 * 1024 * 1024


def test_upload_stops_at_a_refused_call(repo, tmp_path):
    add_place(repo, 1, -32.4, 117.0)
    path = build(repo, tmp_path / "dist")
    intake = FakeIntake()
    with pytest.raises(RuntimeError, match="answered 401"):
        upload(path, "https://intake.example", "not-the-token", send=intake.send, log=lambda _: None)
    assert intake.places == {}

    def failing(method, url, headers, body):
        return (500, b"D1_ERROR") if method == "PUT" else intake.send(method, url, headers, body)

    with pytest.raises(RuntimeError, match="call 1 of 1: PUT /v1/place-index answered 500"):
        upload(path, "https://intake.example", TOKEN, send=failing, log=lambda _: None)


@pytest.fixture
def origins():
    """Loopback HTTP servers that record each request and its authorization header."""
    seen, servers = [], []

    def start(name, redirect_to=None):
        class Handler(BaseHTTPRequestHandler):
            def answer(self, status, body=b"", location=None):
                self.send_response(status)
                if location:
                    self.send_header("location", location)
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                seen.append((name, "GET", self.headers.get("authorization")))
                if redirect_to:
                    self.answer(302, location=redirect_to() + self.path)
                else:
                    self.answer(200, b'{"cells": []}')

            def do_PUT(self):
                self.rfile.read(int(self.headers.get("content-length", "0")))
                seen.append((name, "PUT", self.headers.get("authorization")))
                self.answer(200, b'{"cells": 1}')

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_port}"

    yield start, seen
    for server in servers:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("url", ["http://{origin}/", "ftp://127.0.0.1/x", "not-a-url", "https://user:pw@intake.example"])
def test_main_refuses_an_address_that_is_not_plain_https(repo, tmp_path, origins, monkeypatch, url):
    start, seen = origins
    add_place(repo, 1, -32.4, 117.0)
    path = build(repo, tmp_path / "dist")
    origin = start("intake").removeprefix("http://")
    monkeypatch.setenv(TOKEN_ENV, TOKEN)

    assert main(["--file", str(path), "--url", url.format(origin=origin)]) == 1
    assert seen == []


def test_upload_sends_nothing_to_an_http_address(repo, tmp_path):
    add_place(repo, 1, -32.4, 117.0)
    path = build(repo, tmp_path / "dist")
    intake = FakeIntake()
    with pytest.raises(ValueError, match="https"):
        upload(path, "http://intake.example", TOKEN, send=intake.send, log=lambda _: None)
    assert intake.calls == []


def test_a_redirect_is_not_followed(origins):
    start, seen = origins
    second = start("second")
    first = start("first", redirect_to=lambda: second)

    status, _ = urllib_send("GET", f"{first}/v1/place-index/cells", {"authorization": f"Bearer {TOKEN}"}, None)

    assert status == 302
    assert seen == [("first", "GET", f"Bearer {TOKEN}")]


@pytest.mark.parametrize(
    ("url", "configured", "printed"),
    [
        ("https://intake.example/", "https://intake.example", "https://intake.example\n"),
        ("https://intake.example/base", "https://intake.example/", "https://intake.example/base\n"),
        ("https://other.example", "https://intake.example", None),
        ("https://intake.example:8443", "https://intake.example", None),
        ("http://intake.example", "http://intake.example", None),
        ("https://intake.example", "", None),
        ("https://intake.example\n::warning::x", "https://intake.example", None),
    ],
)
def test_check_accepts_only_the_host_of_the_configured_intake(capsys, monkeypatch, url, configured, printed):
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    code = main(["--check", "--url", url, "--configured-url", configured])
    out = capsys.readouterr().out
    if printed is None:
        assert code == 1
        assert out == ""
    else:
        assert code == 0
        assert out == printed


def test_the_index_leaves_out_town_and_council_area(repo, tmp_path):
    town = {"name": "Example Town", "osm_id": 101, "type": "town", "distance_m": 2000}
    shire = {"name": "Example Shire", "osm_id": 202}
    place = add_place(
        repo, 1, -32.4, 117.0,
        facts={"toilets": True, "locality": town, "lga": shire},
        fact_sources={"toilets": "osm", "locality": "osm", "lga": "osm"},
    )
    path = build(repo, tmp_path / "dist")
    assert index_lines(path) == [
        {"id": place, "cell": [117, -33], "facts": {"toilets": True}, "fact_sources": {"toilets": "osm"}, "fact_dates": {}},
    ]
    # The released record keeps both.
    released = json.loads(gzip.decompress((tmp_path / "dist" / "places.ndjson.gz").read_bytes()).decode("utf-8").splitlines()[0])
    assert released["facts"]["locality"] == town and released["facts"]["lga"] == shire
