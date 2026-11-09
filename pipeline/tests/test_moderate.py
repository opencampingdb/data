"""Tests for pipeline.moderate: hiding reviews and blocking contributors."""

import json

import pytest

from pipeline.common import dump_line, dump_record, load_places, place_path
from pipeline.extract import extract_file
from pipeline.moderate import ModerateError, main, remove_review, set_blocked, set_hidden
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
        "app": "example-app",
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


# ------------------------------------------------------- audit run-2: OCDB-07

GONE = "rev_01JBX000000000000000000002"


def removed_list(repo):
    return json.loads((repo / "moderation" / "removed_reviews.json").read_text(encoding="utf-8"))


def test_remove_deletes_the_line_lists_the_id_and_rerates(rated):
    repo, place_id = rated
    assert remove_review(repo, GONE, "personal_details", now=T) == place_id
    text = (repo / "reviews" / f"{place_id}.ndjson").read_text(encoding="utf-8")
    assert GONE not in text
    assert "rev_01JBX000000000000000000001" in text
    assert removed_list(repo) == {"reviews": [{"review": GONE, "place": place_id, "removed": "2026-11-02", "reason": "personal_details"}]}
    record = load_places(repo)[place_id]
    assert record["rating"] == {"count": 1, "mean": 5.0}
    assert record["updated"] == T
    assert validate_repo(repo, now=T) == []


def test_remove_keeps_no_text_of_the_review(rated):
    repo, place_id = rated
    path = repo / "reviews" / f"{place_id}.ndjson"
    lines = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]
    lines[1]["text"] = "Ring Jo Bloggs on 0400 000 000."
    lines[1]["hidden"] = "moderator"
    path.write_text("".join(dump_line(r) + "\n" for r in lines), encoding="utf-8")
    remove_review(repo, GONE, "personal_details", now=T)
    for f in (repo / "reviews").rglob("*"):
        if f.is_file():
            assert "Bloggs" not in f.read_text(encoding="utf-8")
    assert "Bloggs" not in (repo / "moderation" / "removed_reviews.json").read_text(encoding="utf-8")


def test_removing_the_last_review_deletes_the_file(rated):
    repo, place_id = rated
    remove_review(repo, "rev_01JBX000000000000000000001", "personal_details", now=T)
    remove_review(repo, GONE, "abuse", now=T)
    assert not (repo / "reviews" / f"{place_id}.ndjson").exists()
    assert load_places(repo)[place_id]["rating"] is None
    assert [e["review"] for e in removed_list(repo)["reviews"]] == ["rev_01JBX000000000000000000001", GONE]
    assert validate_repo(repo, now=T) == []


def test_remove_refuses_unknown_review_and_reason(rated):
    repo, _ = rated
    with pytest.raises(ModerateError):
        remove_review(repo, "rev_01JBX0000000000000000000ZZ", "personal_details", now=T)
    with pytest.raises(ModerateError):
        remove_review(repo, GONE, "rude", now=T)
    assert not (repo / "moderation" / "removed_reviews.json").exists()


def test_validation_refuses_a_removed_review_added_again(rated):
    repo, place_id = rated
    path = repo / "reviews" / f"{place_id}.ndjson"
    before = path.read_text(encoding="utf-8")
    remove_review(repo, GONE, "personal_details", now=T)
    # A later run puts the same review back, rating and all.
    path.write_text(before, encoding="utf-8")
    record = load_places(repo)[place_id]
    record["rating"] = {"count": 2, "mean": 3.0}
    place_path(repo, place_id).write_text(dump_record(record), encoding="utf-8")
    errors = validate_repo(repo, now=T)
    assert len(errors) == 1
    assert GONE in errors[0] and "removed" in errors[0]


def test_validation_checks_the_removed_list(rated):
    repo, place_id = rated
    path = repo / "moderation" / "removed_reviews.json"
    path.parent.mkdir(exist_ok=True)
    entry = {"review": GONE, "place": place_id, "removed": "2026-11-02", "reason": "personal_details"}
    path.write_text(dump_record({"reviews": [entry, entry]}), encoding="utf-8")
    assert any("listed twice" in e for e in validate_repo(repo, now=T))
    path.write_text(dump_record({"reviews": [{**entry, "text": "kept"}]}), encoding="utf-8")
    assert validate_repo(repo, now=T) != []


def test_remove_command(rated, capsys):
    repo, place_id = rated
    assert main(["remove", GONE, "--reason", "personal_details", "--root", str(repo)]) == 0
    assert GONE in capsys.readouterr().err
    assert main(["remove", GONE, "--root", str(repo)]) == 1
