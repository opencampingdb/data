"""Tests for pipeline.moderate: hiding reviews and blocking contributors."""

import json

import pytest

from pipeline.common import dump_line, dump_record, load_places, place_path
from pipeline.extract import extract_file
from pipeline.moderate import ModerateError, set_blocked, set_hidden
from pipeline.sync import sync
from pipeline.validate import validate_repo

T = "2026-11-02T00:00:00Z"


def review(n, place_id, stars, contributor):
    return {
        "id": f"rev_01JBX00000000000000000000{n}",
        "place": place_id,
        "stars": stars,
        "text": "Quiet spot.",
        "visited": "2026-09",
        "created": "2026-10-01T10:00:00Z",
        "app": "camporacle",
        "contributor": contributor,
    }


@pytest.fixture
def rated(repo, v1_osm):
    sync(repo, extract_file(v1_osm), now="2026-10-01T16:00:00Z")
    place_id = sorted(load_places(repo))[0]
    record = load_places(repo)[place_id]
    record["rating"] = {"count": 2, "mean": 3.0}
    place_path(repo, place_id).write_text(dump_record(record), encoding="utf-8")
    lines = [review(1, place_id, 5, "ctr_aaaaaaaaaaaaaaaa"), review(2, place_id, 1, "ctr_bbbbbbbbbbbbbbbb")]
    (repo / "reviews" / f"{place_id}.ndjson").write_text("".join(dump_line(r) + "\n" for r in lines), encoding="utf-8")
    assert validate_repo(repo) == []
    return repo, place_id


def test_hide_and_show_rerate(rated):
    repo, place_id = rated
    set_hidden(repo, "rev_01JBX000000000000000000002", True, now=T)
    record = load_places(repo)[place_id]
    assert record["rating"] == {"count": 1, "mean": 5.0}
    assert record["updated"] == T
    assert validate_repo(repo, now=T) == []
    set_hidden(repo, "rev_01JBX000000000000000000002", False, now=T)
    assert load_places(repo)[place_id]["rating"] == {"count": 2, "mean": 3.0}
    assert validate_repo(repo, now=T) == []


def test_block_and_unblock_rerate(rated):
    repo, place_id = rated
    assert set_blocked(repo, "ctr_bbbbbbbbbbbbbbbb", "spam_ad", now=T) == {place_id}
    blocked = json.loads((repo / "moderation" / "blocked_contributors.json").read_text(encoding="utf-8"))
    assert blocked == {"contributors": [{"contributor": "ctr_bbbbbbbbbbbbbbbb", "added": "2026-11-02", "reason": "spam_ad"}]}
    assert load_places(repo)[place_id]["rating"] == {"count": 1, "mean": 5.0}
    assert validate_repo(repo, now=T) == []
    with pytest.raises(ModerateError):
        set_blocked(repo, "ctr_bbbbbbbbbbbbbbbb", "spam_ad", now=T)
    set_blocked(repo, "ctr_bbbbbbbbbbbbbbbb", None, now=T)
    assert load_places(repo)[place_id]["rating"] == {"count": 2, "mean": 3.0}
    assert validate_repo(repo, now=T) == []


def test_refuses_unknown_review_and_reason(rated):
    repo, _ = rated
    with pytest.raises(ModerateError):
        set_hidden(repo, "rev_01JBX0000000000000000000ZZ", True)
    with pytest.raises(ModerateError):
        set_blocked(repo, "ctr_aaaaaaaaaaaaaaaa", "rude")
