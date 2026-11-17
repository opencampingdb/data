"""Tests for the weekly geometry workflow's shell side: tools/geometry-previous,
run against a fake gh, and the settings .github/workflows/geometry.yml passes
to the pipeline steps."""

import os
import re
import subprocess
from pathlib import Path

import pytest

from pipeline.tile_upload import ENV

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "tools" / "geometry-previous"
WORKFLOW = (REPO / ".github" / "workflows" / "geometry.yml").read_text(encoding="utf-8")

# A gh that answers `release view` and `release download` the way FAKE_GH says,
# and logs each call to FAKE_GH_LOG.
FAKE_GH = r"""#!/usr/bin/env bash
echo "$*" >> "$FAKE_GH_LOG"
case "$FAKE_GH" in
  missing) echo "release not found" >&2; exit 1 ;;
  broken) echo "HTTP 502: Bad Gateway (https://api.github.com/graphql)" >&2; exit 1 ;;
esac
if [ "$1 $2" = "release view" ]; then
  printf '%s\n' $FAKE_GH_ASSETS
  exit 0
fi
if [ "$1 $2" = "release download" ]; then
  shift 3
  while [ $# -gt 0 ]; do
    case "$1" in
      --dir) dir="$2"; shift 2 ;;
      --pattern) pattern="$2"; shift 2 ;;
      *) shift ;;
    esac
  done
  echo "{\"from\": \"$pattern\"}" > "$dir/$pattern"
  exit 0
fi
exit 3
"""


def previous(tmp_path: Path, mode: str, assets: str = "") -> tuple[subprocess.CompletedProcess, Path, dict]:
    """Run tools/geometry-previous with the fake gh. Returns the run, the folder and its outputs."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(0o755)
    folder = tmp_path / "previous"
    output = tmp_path / "github-output"
    output.touch()
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_GH": mode,
        "FAKE_GH_ASSETS": assets,
        "FAKE_GH_LOG": str(tmp_path / "gh.log"),
        "GITHUB_OUTPUT": str(output),
    }
    done = subprocess.run([str(SCRIPT), "geometry", str(folder)], env=env, capture_output=True, text=True)
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines() if "=" in line)
    return done, folder, outputs


def test_with_no_release_yet_the_run_goes_on_with_no_lists(tmp_path):
    done, folder, outputs = previous(tmp_path, "missing")
    assert done.returncode == 0, done.stderr
    assert outputs == {"exists": "false"}
    assert list(folder.iterdir()) == []


def test_any_other_gh_error_fails_the_step(tmp_path):
    done, folder, outputs = previous(tmp_path, "broken")
    assert done.returncode != 0
    assert "HTTP 502" in done.stderr
    assert outputs == {}
    assert not folder.exists() or list(folder.iterdir()) == []


def test_the_release_lists_are_downloaded(tmp_path):
    done, folder, outputs = previous(tmp_path, "ok", "water.ndjson.gz tiles.json manifest.json")
    assert done.returncode == 0, done.stderr
    assert outputs == {"exists": "true"}
    assert sorted(p.name for p in folder.iterdir()) == ["manifest.json", "tiles.json"]


def test_a_release_without_a_tile_list_downloads_only_its_manifest(tmp_path):
    done, folder, outputs = previous(tmp_path, "ok", "manifest.json")
    assert done.returncode == 0, done.stderr
    assert outputs == {"exists": "true"}
    assert [p.name for p in folder.iterdir()] == ["manifest.json"]
    assert "no tiles.json" in done.stdout


def step(name: str) -> str:
    """The text of one step of the workflow, from its name to the next step."""
    match = re.search(rf"\n      - name: {re.escape(name)}\n(.*?)(?=\n      - name: |\Z)", WORKFLOW, re.S)
    assert match, name
    return match.group(1)


def test_the_workflow_fetches_the_lists_with_the_tool_and_creates_the_release_only_when_it_found_none():
    assert "tools/geometry-previous" in step("Fetch the current geometry release's lists")
    publish = step("Publish the water, roads and manifest")
    assert "steps.previous.outputs.exists" in publish
    assert "gh release view" not in WORKFLOW


def test_the_upload_step_sets_exactly_the_settings_tile_upload_reads():
    upload = step("Upload the tiles")
    assert "python -m pipeline.tile_upload" in upload
    names = set(re.findall(r"^          (R2_[A-Z_]+):", upload, re.M))
    assert names == set(ENV)
    check = step("Check the R2 settings")
    for name in ENV:
        assert name in check


@pytest.mark.parametrize("name", ENV)
def test_the_r2_settings_come_from_secrets_or_variables(name):
    assert re.search(rf"{name}: \$\{{\{{ (secrets|vars)\.{name} \}}\}}", step("Upload the tiles"))
