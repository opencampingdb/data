"""Tests for pipeline.place_geometry: the water mask, every major road and their tiles."""

import json
import math

from pipeline.place_geometry import build
from pipeline.tests.place_geometry_fixtures import OSM, lines, run, tiles, water_polygons


def test_the_lake_is_in_the_water_file(tmp_path):
    water = lines(run(tmp_path) / "water.ndjson")
    lakes = [line for line in water if line["water"] == "lake"]
    assert [line.get("name") for line in lakes] == ["Fixture Lake"]


def test_rivers_drains_and_ponds_under_a_hectare_are_left_out(tmp_path):
    names = {line.get("name") for line in lines(run(tmp_path) / "water.ndjson")}
    assert not names & {"Fixture River", "Fixture Drain", "Fixture Pond"}


def test_the_sea_is_clipped_to_the_bounds_and_cut_along_the_grid(tmp_path):
    sea = [line for line in lines(run(tmp_path) / "water.ndjson") if line["water"] == "sea"]
    # 151.35 to 151.4 and 151.4 to 151.5: the part past 151.5 and the sea
    # elsewhere are outside the bounds.
    outlines = sorted(
        (min(x for x, _ in ring), max(x for x, _ in ring), min(y for _, y in ring), max(y for _, y in ring))
        for line in sea for polygon in line["polygons"] for ring in polygon[:1]
    )
    assert outlines == [(151.35, 151.4, -33.1, -33.0), (151.4, 151.5, -33.1, -33.0)]
    assert all("name" not in line for line in sea)


def test_the_motorway_is_in_the_roads_file_and_the_street_is_not(tmp_path):
    roads = lines(run(tmp_path) / "roads.ndjson")
    assert roads == [{
        "road": 701,
        "highway": "motorway",
        "name": "Fixture Motorway",
        "ref": "M1",
        "coords": [[151.12, -33.45], [151.15, -33.45], [151.18, -33.45], [151.25, -33.45]],
    }]


def test_water_tiles_hold_every_piece_within_100_m_of_them(tmp_path):
    water = {key: found for key, found in tiles(run(tmp_path)).items() if key.startswith("water/")}
    # The lake lies inside tile -333/1511, more than 100 m from its edges.
    # The sea's pieces reach into the tiles around their two squares.
    assert sorted(water) == sorted(
        ["water/-333/1511.ndjson"]
        + [f"water/{row}/{col}.ndjson" for row in (-332, -331, -330) for col in (1513, 1514, 1515)]
    )
    assert [line["name"] for line in water["water/-333/1511.ndjson"]] == ["Fixture Lake"]
    assert [line["water"] for line in water["water/-331/1514.ndjson"]] == ["sea", "sea"]
    assert len(water["water/-331/1515.ndjson"]) == 1


def test_road_tiles_hold_the_pieces_of_a_road_near_them(tmp_path):
    roads = {key: found for key, found in tiles(run(tmp_path)).items() if key.startswith("roads/")}
    assert sorted(roads) == ["roads/-335/1511.ndjson", "roads/-335/1512.ndjson"]
    # The last segment, 151.18 to 151.25, is the only one that reaches tile 1512.
    assert roads["roads/-335/1512.ndjson"] == [{
        "road": 701,
        "highway": "motorway",
        "name": "Fixture Motorway",
        "ref": "M1",
        "coords": [[151.18, -33.45], [151.25, -33.45]],
    }]
    assert len(roads["roads/-335/1511.ndjson"][0]["coords"]) == 4


def changed(out: Path) -> dict:
    return json.loads((out / "changed.json").read_text())


def test_a_first_build_puts_every_tile(tmp_path):
    out = run(tmp_path)
    assert changed(out) == {"put": sorted(tiles(out)), "delete": []}


def test_an_unchanged_rebuild_lists_no_changed_tiles(tmp_path):
    first = run(tmp_path, name="first")
    second = run(tmp_path, previous=first / "tiles.json", name="second")
    assert changed(second) == {"put": [], "delete": []}


def test_a_changed_lake_lists_exactly_its_tiles(tmp_path):
    first = run(tmp_path, name="first")
    moved = tmp_path / "moved.osm"
    moved.write_text(OSM.read_text().replace('<node id="3" version="1" lat="-33.248"', '<node id="3" version="2" lat="-33.2475"'))
    second = run(tmp_path, osm=moved, previous=first / "tiles.json", name="second")
    assert changed(second) == {"put": ["water/-333/1511.ndjson"], "delete": []}


def test_a_tile_with_nothing_left_in_it_is_deleted(tmp_path):
    first = run(tmp_path, name="first")
    dry = tmp_path / "dry.osm"
    dry.write_text(OSM.read_text().replace('<tag k="name" v="Fixture Lake"/>', '<tag k="name" v="Fixture Lake"/>\n    <tag k="water" v="wastewater"/>').replace('<tag k="water" v="lake"/>', ''))
    second = run(tmp_path, osm=dry, previous=first / "tiles.json", name="second")
    assert changed(second) == {"put": [], "delete": ["water/-333/1511.ndjson"]}


def test_open_sea_more_than_five_tiles_from_land_is_left_out(tmp_path):
    # One row of tiles from 151.0 to 152.5. Land runs to the coast at 151.35,
    # and the sea from there to the end of the bounds.
    osm = tmp_path / "wide.osm"
    osm.write_text('<osm version="0.6"><bounds minlat="-33.1" minlon="151.0" maxlat="-33.0" maxlon="152.5"/></osm>')
    out = tmp_path / "out"
    build(osm, water_polygons(tmp_path / "wp", [[(151.35, -33.1), (151.35, -33.0), (152.5, -33.0), (152.5, -33.1), (151.35, -33.1)]]), out)
    columns = sorted(
        math.floor(min(x for x, _ in line["polygons"][0][0]) * 10)
        for line in lines(out / "water.ndjson")
    )
    # Tile 1513 holds the coast, so the sea is kept to tile 1518.
    assert columns == [1513, 1514, 1515, 1516, 1517, 1518]


def test_tile_lines_follow_the_engines_format(tmp_path):
    for key, found in tiles(run(tmp_path)).items():
        for line in found:
            if key.startswith("water/"):
                assert set(line) <= {"water", "name", "polygons"} and line["water"] in {"sea", "lake"}
                points = [p for polygon in line["polygons"] for ring in polygon for p in ring]
                assert all(ring[0] == ring[-1] and len(ring) >= 4 for polygon in line["polygons"] for ring in polygon)
            else:
                assert set(line) <= {"road", "highway", "name", "ref", "coords"}
                points = line["coords"]
            assert all(round(v, 6) == v for point in points for v in point)


def test_the_summary_gives_the_counts_and_sizes(tmp_path):
    out = run(tmp_path)
    summary = json.loads((out / "summary.json").read_text())
    assert summary["water"]["sea_lines"] == 2
    assert summary["water"]["lakes"] == 1
    assert summary["water"]["tiles"] == 10
    assert summary["roads"]["roads"] == 1
    assert summary["roads"]["tiles"] == 2
    assert summary["water"]["bytes"] == (out / "water.ndjson").stat().st_size
    assert summary["tiles"]["put"] == 12 and summary["tiles"]["delete"] == 0


def test_a_tile_over_the_engines_limit_fails_the_build(tmp_path, monkeypatch, capsys):
    import pipeline.place_geometry as place_geometry

    monkeypatch.setattr(place_geometry, "MAX_TILE_BYTES", 100)
    code = place_geometry.main([
        "--extract", str(OSM), "--water-polygons", str(water_polygons(tmp_path / "wp")), "--out", str(tmp_path / "out"),
    ])
    assert code == 1
    assert "over 100 bytes" in capsys.readouterr().err
    assert not (tmp_path / "out" / "changed.json").exists()
