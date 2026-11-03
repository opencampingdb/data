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
        "app": "example-app",
        "contributor": "ctr_0123456789abcdef",
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
    rec["facts"]["hot_tub"] = True
    rec["fact_sources"]["hot_tub"] = "osm"
    write_place(path, rec)
    assert any("hot_tub" in e for e in validate_repo(synced))


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
    rec["fact_dates"] = {"name": rec["updated"]}
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


def test_contributed_place_without_osm_link_is_valid(synced):
    rec = {
        "id": "ocdb_01JBX0000000000000000000C1",
        "name": "Contributed Camp",
        "kind": "free_camp",
        "group": "overnight",
        "location": {"lat": -30.0, "lon": 140.0},
        "osm": None,
        "facts": {},
        "status": "open",
        "rating": None,
        "fact_sources": {"name": "contributed", "location": "contributed"},
        "fact_dates": {"name": "2026-10-01T16:00:00Z", "location": "2026-10-01T16:00:00Z"},
        "updated": "2026-10-01T16:00:00Z",
    }
    write_place(place_path(synced, rec["id"]), rec)
    assert validate_repo(synced) == []


def test_contributed_status_source_is_valid(synced):
    path, rec = first_place(synced)
    rec["status"] = "closed"
    rec["fact_sources"]["status"] = "contributed"
    rec["fact_dates"] = {"status": rec["updated"]}
    write_place(path, rec)
    assert validate_repo(synced) == []


def test_unknown_source_value_is_refused(synced):
    path, rec = first_place(synced)
    rec["fact_sources"]["name"] = "scraped"
    write_place(path, rec)
    assert any("scraped" in e for e in validate_repo(synced))


def test_value_not_from_osm_needs_a_date(synced):
    path, rec = first_place(synced)
    rec["fact_sources"]["name"] = "feedback"
    write_place(path, rec)
    assert any("fact_dates" in e for e in validate_repo(synced))


def test_date_for_an_osm_value_is_refused(synced):
    path, rec = first_place(synced)
    key = next(iter(rec["facts"]))
    rec["fact_dates"] = {key: rec["updated"]}
    write_place(path, rec)
    assert any("fact_dates" in e for e in validate_repo(synced))


def test_fact_date_after_updated_is_refused(synced):
    path, rec = first_place(synced)
    rec["fact_sources"]["status"] = "contributed"
    rec["fact_dates"] = {"status": "2099-01-01T00:00:00Z"}
    write_place(path, rec)
    assert any("later than updated" in e for e in validate_repo(synced))


def test_osm_timestamp_must_be_utc_seconds(synced):
    path, rec = first_place(synced)
    rec["osm"]["timestamp"] = "2024-05-06 07:08"
    write_place(path, rec)
    assert any("timestamp" in e for e in validate_repo(synced))


def test_last_confirmed_may_be_later_than_updated(synced):
    path, rec = first_place(synced)
    rec["last_confirmed"] = "2026-10-20T03:00:00Z"
    write_place(path, rec)
    assert validate_repo(synced, now="2026-10-21T00:00:00Z") == []


def test_last_confirmed_in_the_future_is_refused(synced):
    path, rec = first_place(synced)
    rec["last_confirmed"] = "2026-10-22T00:00:00Z"
    write_place(path, rec)
    assert any("last_confirmed" in e for e in validate_repo(synced, now="2026-10-21T00:00:00Z"))


def test_last_confirmed_must_be_utc_seconds(synced):
    path, rec = first_place(synced)
    rec["last_confirmed"] = "2026-10-20"
    write_place(path, rec)
    assert any("last_confirmed" in e for e in validate_repo(synced, now="2026-10-21T00:00:00Z"))


def write_blocked(repo, contributors, text=None):
    path = repo / "moderation" / "blocked_contributors.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"contributors": [{"contributor": c, "added": "2026-11-02", "reason": "spam_ad"} for c in contributors]}
    path.write_text(text if text is not None else dump_record(body), encoding="utf-8", newline="\n")


def test_review_needs_a_contributor(synced):
    place_id = set_rating(synced, 1, 4.0)
    review = make_review(place_id)
    del review["contributor"]
    write_review_lines(synced, place_id, [dump_line(review)])
    assert any("contributor" in e for e in validate_repo(synced))


def test_visited_nearby_is_only_true(synced):
    place_id = set_rating(synced, 1, 4.0)
    write_review(synced, place_id, visited_nearby=True)
    assert validate_repo(synced) == []
    write_review(synced, place_id, visited_nearby=False)
    assert validate_repo(synced) != []


def test_hidden_review_is_left_out_of_the_rating(synced):
    place_id = set_rating(synced, 1, 5.0)
    shown = make_review(place_id, id="rev_01JBX0000000000000000000AA", stars=5)
    hidden = make_review(place_id, id="rev_01JBX0000000000000000000AB", stars=1, hidden="reported")
    write_review_lines(synced, place_id, [dump_line(shown), dump_line(hidden)])
    assert validate_repo(synced) == []


def test_only_hidden_reviews_means_no_rating(synced):
    path, rec = first_place(synced)
    write_review(synced, rec["id"], hidden="moderator")
    assert validate_repo(synced) == []
    set_rating(synced, 1, 4.0)
    assert any("no visible reviews" in e for e in validate_repo(synced))


def test_unknown_hidden_value_is_refused(synced):
    place_id = set_rating(synced, 1, 4.0)
    write_review(synced, place_id, hidden="spam")
    assert validate_repo(synced) != []


def test_blocked_contributor_is_left_out_of_the_rating(synced):
    place_id = set_rating(synced, 1, 5.0)
    shown = make_review(place_id, id="rev_01JBX0000000000000000000AA", stars=5)
    blocked = make_review(place_id, id="rev_01JBX0000000000000000000AB", stars=1, contributor="ctr_ffffffffffffffff")
    write_review_lines(synced, place_id, [dump_line(shown), dump_line(blocked)])
    write_blocked(synced, ["ctr_ffffffffffffffff"])
    assert validate_repo(synced) == []


def test_blocked_list_must_be_sorted_and_unique(synced):
    write_blocked(synced, ["ctr_ffffffffffffffff", "ctr_0000000000000000"])
    assert any("sorted" in e for e in validate_repo(synced))
    write_blocked(synced, ["ctr_0000000000000000", "ctr_0000000000000000"])
    assert validate_repo(synced) != []


def test_blocked_list_must_match_its_schema(synced):
    write_blocked(synced, [], text='{\n  "contributors": [\n    {\n      "contributor": "Sam"\n    }\n  ]\n}\n')
    assert any("blocked_contributors" in e for e in validate_repo(synced))


def test_unexpected_file_in_moderation_is_refused(synced):
    (synced / "moderation").mkdir()
    (synced / "moderation" / "notes.txt").write_text("x", encoding="utf-8")
    assert any("unexpected file in moderation" in e for e in validate_repo(synced))


# ---------------------------------------------------------------- fees


def make_fee(place_id, **changes):
    fee = {
        "id": "fee_01JBX0000000000000000000AA",
        "place": place_id,
        "amount": 32.5,
        "site": "powered",
        "paid": "2026-09",
        "created": "2026-10-01T10:00:00Z",
        "app": "example-app",
        "contributor": "ctr_0123456789abcdef",
    }
    fee.update(changes)
    return fee


def write_fee_lines(repo, place_id, lines):
    path = repo / "fees" / f"{place_id}.ndjson"
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8", newline="\n")
    return path


def write_fee(repo, place_id, **changes):
    return write_fee_lines(repo, place_id, [dump_line(make_fee(place_id, **changes))])


def test_valid_fees_pass(synced):
    _, rec = first_place(synced)
    write_fee_lines(synced, rec["id"], [
        dump_line(make_fee(rec["id"])),
        dump_line(make_fee(rec["id"], id="fee_01JBX0000000000000000000AB", amount=120, site="cabin")),
        dump_line(make_fee(rec["id"], id="fee_01JBX0000000000000000000AC", amount=0.01, site="unpowered")),
        dump_line(make_fee(rec["id"], id="fee_01JBX0000000000000000000AD", amount=1000)),
    ])
    assert validate_repo(synced) == []


def test_fee_for_unknown_place_is_refused(synced):
    write_fee(synced, "ocdb_01JBX00000000000000000ZZZZ")
    assert any("no such place" in e for e in validate_repo(synced))


def test_fee_for_merged_place_is_refused(synced):
    paths = sorted((synced / "places").rglob("*.json"))
    keep = json.loads(paths[0].read_text(encoding="utf-8"))
    drop = json.loads(paths[1].read_text(encoding="utf-8"))
    write_place(paths[1], dict(drop, status="merged", merged_into=keep["id"]))
    write_fee(synced, drop["id"])
    assert any(f"its fees belong to {keep['id']}" in e for e in validate_repo(synced))


@pytest.mark.parametrize(
    "changes",
    [
        {"extra": "x"},
        {"amount": 0},
        {"amount": -5},
        {"amount": 1000.5},
        {"amount": "32.50"},
        {"site": "tent"},
        {"paid": "2026-13"},
        {"paid": "2026-09-01"},
        {"id": "rev_01JBX0000000000000000000AA"},
        {"contributor": "someone@example.com"},
    ],
)
def test_fee_outside_the_schema_is_refused(synced, changes):
    _, rec = first_place(synced)
    write_fee(synced, rec["id"], **changes)
    assert validate_repo(synced) != []


def test_fee_missing_a_field_is_refused(synced):
    _, rec = first_place(synced)
    fee = make_fee(rec["id"])
    del fee["contributor"]
    write_fee_lines(synced, rec["id"], [dump_line(fee)])
    assert any("contributor" in e for e in validate_repo(synced))


def test_fee_with_more_than_2_decimal_places_is_refused(synced):
    _, rec = first_place(synced)
    write_fee(synced, rec["id"], amount=12.345)
    assert any("more than 2 decimal places" in e for e in validate_repo(synced))


def test_fee_lines_must_be_canonical_sorted_and_unique(synced):
    _, rec = first_place(synced)
    a = make_fee(rec["id"], id="fee_01JBX0000000000000000000AB")
    b = make_fee(rec["id"], id="fee_01JBX0000000000000000000AA")
    write_fee_lines(synced, rec["id"], [dump_line(a), dump_line(b)])
    assert any("sorted by id" in e for e in validate_repo(synced))
    write_fee_lines(synced, rec["id"], [json.dumps(a, sort_keys=True)])
    assert any("canonical form" in e for e in validate_repo(synced))
    write_fee_lines(synced, rec["id"], [dump_line(a), dump_line(a)])
    assert any("used twice" in e for e in validate_repo(synced))


def test_fee_place_must_match_file_name(synced):
    paths = sorted((synced / "places").rglob("*.json"))
    first = json.loads(paths[0].read_text(encoding="utf-8"))
    second = json.loads(paths[1].read_text(encoding="utf-8"))
    write_fee_lines(synced, first["id"], [dump_line(make_fee(second["id"]))])
    assert any("does not match file name" in e for e in validate_repo(synced))


def test_unexpected_file_in_fees_is_refused(synced):
    (synced / "fees" / "notes.txt").write_text("x", encoding="utf-8")
    assert any("unexpected file in fees/" in e for e in validate_repo(synced))


def new_place(**changes):
    """A place a traveller added, as the feedback engine writes it."""
    rec = {
        "id": "ocdb_01JC0000000000000000000009",
        "name": None,
        "kind": "free_camp",
        "group": "overnight",
        "location": {"lat": -35.1, "lon": 149.2},
        "osm": None,
        "facts": {},
        "status": "open",
        "rating": None,
        "standing": "shown",
        "fact_sources": {"location": "feedback"},
        "fact_dates": {"location": "2026-10-30T01:00:00Z"},
        "updated": "2026-10-30T01:00:00Z",
    }
    rec.update(changes)
    return rec


@pytest.mark.parametrize("standing", ["shown", "confirmed", "held", "hidden"])
def test_a_new_place_carries_its_standing(synced, standing):
    rec = new_place(standing=standing)
    write_place(place_path(synced, rec["id"]), rec)
    assert validate_repo(synced, now="2026-10-31T00:00:00Z") == []


def test_standing_must_be_a_known_value(synced):
    rec = new_place(standing="withdrawn")
    write_place(place_path(synced, rec["id"]), rec)
    assert any("standing" in e for e in validate_repo(synced, now="2026-10-31T00:00:00Z"))


def test_only_a_place_a_traveller_added_has_a_standing(synced):
    path, rec = first_place(synced)
    rec["standing"] = "shown"
    write_place(path, rec)
    assert any("standing" in e for e in validate_repo(synced))
