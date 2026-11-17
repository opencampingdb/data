"""Tests for pipeline.geometry_release: checking a place geometry build and packing it for the release."""

import gzip
import hashlib
import json
import shutil
from pathlib import Path

import pytest

from pipeline.geometry_release import BuildRejected, pack
from pipeline.tests.test_place_geometry import run

SOURCES = {"extract_source": "Geofabrik", "water_polygons_modified": "2026-11-10T03:12:00Z"}


@pytest.fixture(scope="module")
def built(tmp_path_factory) -> Path:
    return run(tmp_path_factory.mktemp("geometry"))


@pytest.fixture
def build(built, tmp_path) -> Path:
    """A copy of a real build of the fixtures, free to break."""
    return Path(shutil.copytree(built, tmp_path / "build"))


def test_packs_the_gzipped_files_the_lists_and_a_manifest(build, tmp_path):
    out = tmp_path / "publish"
    manifest = pack(build, out, **SOURCES)
    assert sorted(p.name for p in out.iterdir()) == [
        "manifest.json", "roads.ndjson.gz", "summary.json", "tiles.json", "water.ndjson.gz",
    ]
    assert gzip.decompress((out / "water.ndjson.gz").read_bytes()) == (build / "water.ndjson").read_bytes()
    assert gzip.decompress((out / "roads.ndjson.gz").read_bytes()) == (build / "roads.ndjson").read_bytes()
    assert json.loads((out / "manifest.json").read_text()) == manifest
    water = manifest["assets"]["water.ndjson.gz"]
    assert water["sha256"] == hashlib.sha256((out / "water.ndjson.gz").read_bytes()).hexdigest()
    assert water["bytes"] == (out / "water.ndjson.gz").stat().st_size
    assert water["ndjson_bytes"] == (build / "water.ndjson").stat().st_size
    assert manifest["sources"]["extract"]["source"] == "Geofabrik"
    assert manifest["sources"]["water_polygons"]["last_modified"] == "2026-11-10T03:12:00Z"
    summary = json.loads((build / "summary.json").read_text())
    assert manifest["sources"]["extract"]["timestamp"] == summary["extract"]["timestamp"]
    assert manifest["roads"] == summary["roads"]
    assert manifest["water"] == summary["water"]
    assert manifest["licence"] == "ODbL-1.0"


def test_the_same_build_packs_to_the_same_bytes(build, tmp_path):
    pack(build, tmp_path / "a", **SOURCES)
    pack(build, tmp_path / "b", **SOURCES)
    for name in ("water.ndjson.gz", "roads.ndjson.gz"):
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes()


def rejected(build: Path, tmp_path: Path, match: str, **kwargs):
    out = tmp_path / "publish"
    with pytest.raises(BuildRejected, match=match):
        pack(build, out, **SOURCES, **kwargs)
    assert not out.exists() or not any(out.iterdir())


def first_tile(build: Path) -> str:
    return sorted(json.loads((build / "tiles.json").read_text())["tiles"])[0]


def test_a_tile_missing_from_disk_is_rejected(build, tmp_path):
    (build / "tiles" / first_tile(build)).unlink()
    rejected(build, tmp_path, "missing")


def test_a_tile_whose_bytes_differ_from_the_list_is_rejected(build, tmp_path):
    (build / "tiles" / first_tile(build)).write_text("{}\n")
    rejected(build, tmp_path, "sha256")


def test_a_tile_on_disk_but_not_in_the_list_is_rejected(build, tmp_path):
    extra = build / "tiles" / "water" / "1" / "1.ndjson"
    extra.parent.mkdir(parents=True)
    extra.write_text("{}\n")
    rejected(build, tmp_path, "not in tiles.json")


def test_a_put_of_a_tile_not_in_the_list_is_rejected(build, tmp_path):
    changed = json.loads((build / "changed.json").read_text())
    changed["put"].append("water/2/2.ndjson")
    (build / "changed.json").write_text(json.dumps(changed))
    rejected(build, tmp_path, "put")


def test_a_delete_of_a_tile_still_in_the_list_is_rejected(build, tmp_path):
    changed = json.loads((build / "changed.json").read_text())
    changed["delete"].append(first_tile(build))
    (build / "changed.json").write_text(json.dumps(changed))
    rejected(build, tmp_path, "delete")


@pytest.mark.parametrize("layer, field", [("water", "sea_lines"), ("water", "lake_lines"), ("roads", "roads")])
def test_a_build_with_no_sea_lakes_or_roads_is_rejected(build, tmp_path, layer, field):
    summary = json.loads((build / "summary.json").read_text())
    summary[layer][field] = 0
    (build / "summary.json").write_text(json.dumps(summary))
    rejected(build, tmp_path, field)


def test_a_build_without_its_summary_is_rejected(build, tmp_path):
    (build / "summary.json").unlink()
    rejected(build, tmp_path, "summary.json")


def test_a_build_under_half_the_previous_release_is_rejected(build, tmp_path):
    previous = pack(build, tmp_path / "previous", **SOURCES)
    previous["roads"]["roads"] *= 3
    (tmp_path / "previous" / "manifest.json").write_text(json.dumps(previous))
    rejected(build, tmp_path, "roads", previous_manifest=tmp_path / "previous" / "manifest.json")


def test_a_build_like_the_previous_release_passes(build, tmp_path):
    pack(build, tmp_path / "previous", **SOURCES)
    pack(build, tmp_path / "publish", **SOURCES, previous_manifest=tmp_path / "previous" / "manifest.json")
