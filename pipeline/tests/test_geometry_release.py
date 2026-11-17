"""Tests for pipeline.geometry_release: checking a place geometry build and packing it for the release."""

import gzip
import hashlib
import json
import shutil
from pathlib import Path

import pytest

from pipeline.geometry_release import BuildRejected, main, pack
from pipeline.tests.place_geometry_fixtures import run

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
    assert manifest["allowed_shrink"] == []


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


@pytest.mark.parametrize("layer, field", [("water", "sea_lines"), ("water", "lakes"), ("roads", "roads")])
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


def previous_with(build: Path, tmp_path: Path, layer: str, field: str, times: int) -> Path:
    """A previous release like this build, with one count times larger."""
    previous = pack(build, tmp_path / "previous", **SOURCES)
    previous[layer][field] *= times
    path = tmp_path / "previous" / "manifest.json"
    path.write_text(json.dumps(previous))
    return path


def test_the_lake_guard_counts_lakes_not_their_pieces(build, tmp_path):
    rejected(build, tmp_path, "lakes", previous_manifest=previous_with(build, tmp_path, "water", "lakes", 3))


def test_more_lake_pieces_in_the_previous_release_with_as_many_lakes_passes(build, tmp_path):
    pack(build, tmp_path / "publish", **SOURCES, previous_manifest=previous_with(build, tmp_path, "water", "lake_lines", 3))


def test_allow_shrink_lets_a_build_under_half_the_previous_release_through(build, tmp_path):
    previous = previous_with(build, tmp_path, "roads", "roads", 3)
    manifest = pack(build, tmp_path / "publish", **SOURCES, previous_manifest=previous, allow_shrink=True)
    assert manifest["roads"]["roads"] == 1
    assert manifest["allowed_shrink"] == ["roads roads fell from 3 to 1, under 50% of the previous release"]


def test_allow_shrink_still_rejects_a_build_with_no_roads(build, tmp_path):
    summary = json.loads((build / "summary.json").read_text())
    summary["roads"]["roads"] = 0
    (build / "summary.json").write_text(json.dumps(summary))
    rejected(build, tmp_path, "roads", allow_shrink=True)


def cli(build: Path, tmp_path: Path, *extra: str) -> int:
    return main([
        "--build", str(build), "--out", str(tmp_path / "publish"), "--extract-source", "Geofabrik", *extra,
    ])


def test_a_rejected_build_writes_its_reason_to_the_run_summary(build, tmp_path, monkeypatch, capsys):
    run_summary = tmp_path / "step-summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(run_summary))
    previous = previous_with(build, tmp_path, "roads", "roads", 3)
    assert cli(build, tmp_path, "--previous-manifest", str(previous)) == 1
    reason = capsys.readouterr().err.strip().removeprefix("geometry release: ")
    assert "roads roads fell from 3 to 1" in reason
    text = run_summary.read_text()
    assert "rejected" in text and reason in text and "allow-shrink" in text


def test_a_rejected_build_with_no_run_summary_only_logs(build, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    (build / "roads.ndjson").unlink()
    assert cli(build, tmp_path) == 1
    assert "roads.ndjson is missing" in capsys.readouterr().err


def test_allow_shrink_on_the_command_line_passes_and_says_so_in_the_run_summary(build, tmp_path, monkeypatch, capsys):
    run_summary = tmp_path / "step-summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(run_summary))
    previous = previous_with(build, tmp_path, "roads", "roads", 3)
    assert cli(build, tmp_path, "--previous-manifest", str(previous), "--allow-shrink") == 0
    assert "roads roads fell from 3 to 1" in run_summary.read_text()
