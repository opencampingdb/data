import json

from pipeline.common import dump_line, dump_record, write_text
from pipeline.photos import KEPT, PENDING, SERVING, photo_states, plan
from pipeline.release import photos_ndjson
from pipeline.validate import check_photos, check_removed_photos

A = "pho_01JA0000000000000000000001"
B = "pho_01JA0000000000000000000002"
C = "pho_01JA0000000000000000000003"
PLACE = "ocdb_01JBXK3M8Q4R5S6T7V8W9X0YZA"


def photo(pid, **extra):
    return {
        "id": pid, "place": PLACE, "contributor": "ctr_0123456789abcdef", "created": "2026-11-02T10:00:00Z",
        "app": "camporacle", "licence": "CC-BY-4.0", "width": 1600, "height": 1200, "sha256": "a" * 64, **extra,
    }


def keys(*pids, prefix=""):
    return {f"{prefix}{pid}/{size}.jpg" for pid in pids for size in ("full", "thumb")}


def write_photos(root, *lines):
    write_text(root / "photos" / f"{PLACE}.ndjson", "".join(dump_line(p) + "\n" for p in lines))


def test_publishes_visible_photos_from_pending():
    todo = plan({A}, set(), set(), serving=set(), pending=keys(A))
    assert todo.copies == [(PENDING, f"{A}/full.jpg", SERVING, f"{A}/full.jpg"), (PENDING, f"{A}/thumb.jpg", SERVING, f"{A}/thumb.jpg")]
    assert todo.deletes == {} and todo.missing == []


def test_hiding_keeps_a_copy_and_showing_again_uses_it():
    hide = plan(set(), {A}, set(), serving=keys(A), pending=set())
    assert hide.copies == [(SERVING, f"{A}/full.jpg", PENDING, f"{KEPT}{A}/full.jpg"), (SERVING, f"{A}/thumb.jpg", PENDING, f"{KEPT}{A}/thumb.jpg")]
    assert sorted(hide.deletes[SERVING]) == sorted(keys(A))
    show = plan({A}, set(), set(), serving=set(), pending=keys(A, prefix=KEPT))
    assert [c[1] for c in show.copies] == [f"{KEPT}{A}/full.jpg", f"{KEPT}{A}/thumb.jpg"]


def test_removed_photos_go_from_both_buckets_and_are_never_published():
    todo = plan({A}, set(), {A, B}, serving=keys(B), pending=keys(A) | keys(B, prefix=KEPT))
    assert todo.copies == []
    assert sorted(todo.deletes[SERVING]) == sorted(keys(B))
    assert sorted(todo.deletes[PENDING]) == sorted(keys(A) | keys(B, prefix=KEPT))


def test_reports_a_visible_photo_without_files():
    assert plan({C}, set(), set(), serving=set(), pending=set()).missing == [f"{C}/full.jpg", f"{C}/thumb.jpg"]


def test_a_blocked_contributors_photos_are_not_visible(tmp_path):
    write_photos(tmp_path, photo(A), photo(B, contributor="ctr_ffffffffffffffff"), photo(C, hidden="reported"))
    write_text(tmp_path / "moderation" / "blocked_contributors.json", dump_record({"contributors": [{"contributor": "ctr_ffffffffffffffff", "added": "2026-11-01", "reason": "spam"}]}))
    assert photo_states(tmp_path) == ({A}, {B, C})
    raw, count = photos_ndjson(tmp_path)
    assert count == 1 and json.loads(raw.decode().strip())["id"] == A


def test_validates_photo_lines(tmp_path):
    write_photos(tmp_path, photo(B), photo(A, licence="CC-BY-SA-4.0"))
    assert check_photos(tmp_path, {PLACE: {"status": "open"}}) == [f"photos/{PLACE}.ndjson line 2: licence: 'CC-BY-4.0' was expected"]
    write_photos(tmp_path, photo(B), photo(A))
    assert check_photos(tmp_path, {PLACE: {"status": "open"}}) == [f"photos/{PLACE}.ndjson line 2: lines must be sorted by id"]
    write_photos(tmp_path, photo(A))
    assert check_photos(tmp_path, {PLACE: {"status": "open"}}) == []
    assert check_photos(tmp_path, {}) == [f"photos/{PLACE}.ndjson: no such place {PLACE}"]


def test_a_removed_photo_may_not_have_a_record(tmp_path):
    write_photos(tmp_path, photo(A))
    write_text(tmp_path / "moderation" / "removed_photos.json", dump_record({"photos": [{"photo": A, "reason": "erased", "removed": "2026-11-03T00:00:00Z"}]}))
    assert check_removed_photos(tmp_path) == []
    assert check_photos(tmp_path, {PLACE: {"status": "open"}}) == [f"photos/{PLACE}.ndjson line 1: photo {A} is on moderation/removed_photos.json"]


def test_removed_photos_must_be_sorted(tmp_path):
    entries = [{"photo": B, "reason": "erased", "removed": "2026-11-03T00:00:00Z"}, {"photo": A, "reason": "refused", "removed": "2026-11-03T00:00:00Z"}]
    write_text(tmp_path / "moderation" / "removed_photos.json", dump_record({"photos": entries}))
    assert check_removed_photos(tmp_path) == ["moderation/removed_photos.json: photos must be sorted by id, each once"]
