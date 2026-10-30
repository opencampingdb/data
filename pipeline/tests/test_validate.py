"""Tests for pipeline.validate: schema and content checks over the repo."""

import json

import pytest

from pipeline.common import dump_line, dump_record, place_path
from pipeline.extract import extract_file
from pipeline.sync import sync
from pipeline.validate import validate_repo


@pytest.fixture
def synced(repo, v1_osm):
    sync(repo, extract_file(v1_osm), now="2026-10-01T16:00:00Z")
    return repo


def first_place(repo):
    path = sorted((repo / "places").rglob("*.json"))[0]
    return path, json.loads(path.read_text(encoding="utf-8"))


def write_place(path, rec):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_record(rec), encoding="utf-8", newline="\n")


def make_review(place_id, **changes):
    review = {
        "id": "rev_01JBX0000000000000000000AA",
        "place": place_id,
        "stars": 4,
        "text": "Quiet spot by the creek, clean drop toilets.",
        "visited": "2026-09",
        "created": "2026-10-01T10:00:00Z",
        "app": "camporacle",
    }
    review.update(changes)
    return review


def write_review_lines(repo, place_id, lines):
    path = repo / "reviews" / f"{place_id}.ndjson"
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8", newline="\n")
    return path


def write_review(repo, place_id, **changes):
    """One review, written in canonical form."""
    return write_review_lines(repo, place_id, [dump_line(make_review(place_id, **changes))])


def set_rating(repo, count, mean):
    path, rec = first_place(repo)
    rec["rating"] = {"count": count, "mean": mean}
    write_place(path, rec)
    return rec["id"]


def test_clean_repo_passes(synced):
    assert validate_repo(synced) == []


def test_valid_review_passes(synced):
    place_id = set_rating(synced, 1, 4.0)
    write_review(synced, place_id)
    assert validate_repo(synced) == []


def test_unknown_key_in_place_is_refused(synced):
    path, rec = first_place(synced)
    rec["owner"] = "someone"
    write_place(path, rec)
    assert any("owner" in e for e in validate_repo(synced))


def test_unknown_fact_is_refused(synced):
    path, rec = first_place(synced)
    rec["facts"]["wifi"] = True
    rec["fact_sources"]["wifi"] = "osm"
    write_place(path, rec)
    assert any("wifi" in e for e in validate_repo(synced))


def test_fact_without_source_is_refused(synced):
    path, rec = first_place(synced)
    key = next(iter(rec["facts"]))
    del rec["fact_sources"][key]
    write_place(path, rec)
    assert any("fact_sources" in e for e in validate_repo(synced))


def test_non_canonical_formatting_is_refused(synced):
    path, rec = first_place(synced)
    path.write_text(json.dumps(rec, indent=4), encoding="utf-8")
    assert any("canonical" in e for e in validate_repo(synced))


def test_too_many_decimal_places_is_refused(synced):
    path, rec = first_place(synced)
    rec["location"]["lat"] = -37.1234567
    write_place(path, rec)
    assert any("decimal" in e for e in validate_repo(synced))


def test_file_in_wrong_folder_is_refused(synced):
    path, rec = first_place(synced)
    wrong = synced / "places" / "__" / path.name
    wrong.parent.mkdir()
    path.rename(wrong)
    assert any("should be at" in e for e in validate_repo(synced))


def test_duplicate_osm_object_is_refused(synced):
    path, rec = first_place(synced)
    copy = dict(rec, id="ocdb_01JBX0000000000000000000ZZ")
    write_place(place_path(synced, copy["id"]), copy)
    assert any("same OSM object" in e for e in validate_repo(synced))


def test_review_for_unknown_place_is_refused(synced):
    write_review(synced, "ocdb_01JBX0000000000000000000ZZ")
    assert any("no such place" in e for e in validate_repo(synced))


def test_unknown_key_in_review_is_refused(synced):
    place_id = set_rating(synced, 1, 4.0)
    write_review(synced, place_id, author="Sam")
    assert any("author" in e for e in validate_repo(synced))


@pytest.mark.parametrize(
    "text",
    [
        "Email me at sam.smith@example.com for directions",
        "Ring the ranger on 0412 345 678",
        "Ring the ranger on +61 3 5555 0000",
        "My name is Sam and we loved it",
        "Great spot. Cheers, Sam",
        "Follow @samcamps for more",
        "Details at https://example.org",
    ],
)
def test_personal_details_in_review_text_are_refused(synced, text):
    place_id = set_rating(synced, 1, 4.0)
    write_review(synced, place_id, text=text)
    errors = validate_repo(synced)
    assert any("text" in e for e in errors), text


@pytest.mark.parametrize(
    "text",
    [
        "Sites 12 to 18 have 240V power, $25 a night in 2026.",
        "Drove in from Halls Gap, 4 km of gravel.",
    ],
)
def test_ordinary_review_text_passes(synced, text):
    place_id = set_rating(synced, 1, 4.0)
    write_review(synced, place_id, text=text)
    assert validate_repo(synced) == []


def test_rating_must_match_reviews(synced):
    place_id = set_rating(synced, 2, 4.0)
    write_review(synced, place_id)
    assert any("rating" in e for e in validate_repo(synced))


def test_review_line_not_in_canonical_form_is_refused(synced):
    place_id = set_rating(synced, 1, 4.0)
    review = make_review(place_id)
    write_review_lines(synced, place_id, [json.dumps(review)])
    assert any("canonical" in e for e in validate_repo(synced))


def test_review_lines_must_be_sorted_by_id(synced):
    place_id = set_rating(synced, 2, 4.0)
    first = make_review(place_id, id="rev_01JBX0000000000000000000AB")
    second = make_review(place_id, id="rev_01JBX0000000000000000000AA")
    write_review_lines(synced, place_id, [dump_line(first), dump_line(second)])
    assert any("sorted by id" in e for e in validate_repo(synced))


def review_set(repo, stars_list, mean):
    place_id = set_rating(repo, len(stars_list), mean)
    lines = [
        dump_line(make_review(place_id, id=f"rev_01JBX00000000000000000000{n}", stars=stars))
        for n, stars in enumerate(stars_list)
    ]
    write_review_lines(repo, place_id, lines)


@pytest.mark.parametrize(
    "stars, mean",
    [
        ([4, 5], 4.5),
        ([4, 4, 5], 4.3),
        # 4.25 rounds half up to 4.3, where Python's round() would give 4.2.
        ([4, 4, 4, 5], 4.3),
        ([5, 5, 4], 4.7),
    ],
)
def test_rating_mean_rounded_half_up_passes(synced, stars, mean):
    review_set(synced, stars, mean)
    assert validate_repo(synced) == []


@pytest.mark.parametrize(
    "stars, mean",
    [
        ([4, 4, 5], 4.33),  # not rounded to 1 decimal place
        ([4, 4, 5], 4.35),  # close, but not the exact rounded value
        ([4, 4, 4, 5], 4.2),  # rounded half to even, not half up
    ],
)
def test_rating_mean_must_be_exact(synced, stars, mean):
    review_set(synced, stars, mean)
    assert any("rating" in e for e in validate_repo(synced))


def test_name_and_location_sources_are_allowed(synced):
    path, rec = first_place(synced)
    rec["fact_sources"]["name"] = "feedback"
    rec["fact_sources"]["location"] = "osm"
    write_place(path, rec)
    assert validate_repo(synced) == []


def test_unknown_fact_source_key_is_refused(synced):
    path, rec = first_place(synced)
    rec["fact_sources"]["kind"] = "feedback"
    write_place(path, rec)
    assert any("kind" in e for e in validate_repo(synced))


def test_entry_fact_is_allowed(synced):
    path, rec = first_place(synced)
    rec["facts"]["entry"] = "customers"
    rec["fact_sources"]["entry"] = "osm"
    write_place(path, rec)
    assert validate_repo(synced) == []
    rec["facts"]["entry"] = "members"
    write_place(path, rec)
    assert any("entry" in e for e in validate_repo(synced))


@pytest.mark.parametrize(
    "text",
    [
        "Stayed 2026-10-30 to 2026-11-02, quiet all week.",
        "Visited 2026-10 and again 2026-11 2026-12.",
        "Arrived 2026-10-30T16:00:00Z and the gate was open.",
    ],
)
def test_iso_dates_in_review_text_are_not_phone_numbers(synced, text):
    place_id = set_rating(synced, 1, 4.0)
    write_review(synced, place_id, text=text)
    assert validate_repo(synced) == []


def test_phone_number_next_to_a_date_is_still_refused(synced):
    place_id = set_rating(synced, 1, 4.0)
    write_review(synced, place_id, text="On 2026-10-30 ring 0412 345 678 for the key")
    assert any("phone number" in e for e in validate_repo(synced))


def test_old_kind_is_refused(synced):
    path, rec = first_place(synced)
    rec["kind"] = "camp_site"
    write_place(path, rec)
    assert any("camp_site" in e for e in validate_repo(synced))


def test_group_must_match_kind(synced):
    path, rec = first_place(synced)
    rec["group"] = "emergency" if rec["group"] != "emergency" else "utility"
    write_place(path, rec)
    assert any("group" in e for e in validate_repo(synced))


def test_missing_group_is_refused(synced):
    path, rec = first_place(synced)
    del rec["group"]
    write_place(path, rec)
    assert any("group" in e for e in validate_repo(synced))


def test_water_type_fact(synced):
    path, rec = first_place(synced)
    rec["facts"]["water_type"] = "tank_fill"
    rec["fact_sources"]["water_type"] = "osm"
    write_place(path, rec)
    assert validate_repo(synced) == []
    rec["facts"]["water_type"] = "bore"
    write_place(path, rec)
    assert any("water_type" in e for e in validate_repo(synced))
