"""Tests for the label the release gives an unnamed place to stay (pipeline.labels)."""

import gzip
import json

from pipeline.common import dump_record, place_path
from pipeline.release import build_release

VERSION = "20261030T160000Z-abc1234"
GENERATED = "2026-10-30T16:00:00Z"

CREEK = {"name": "Black Bobs Creek", "osm_id": 404, "distance_m": 90}


def place(number, facts, kind="campground", group="overnight", name=None, location=None):
    return {
        "id": f"ocdb_01M66CKP2X55A8CVGR95ZFJ{number:03d}",
        "name": name,
        "kind": kind,
        "group": group,
        "location": location or {"lat": -37.107745, "lon": 142.423232},
        "osm": {"type": "node", "id": 316599000 + number, "version": 1},
        "facts": facts,
        "fact_sources": {k: "osm" for k in facts},
        "status": "open",
        "rating": None,
        "updated": "2026-10-30T16:00:00Z",
    }


def labels(repo, tmp_path, *records):
    for record in records:
        path = place_path(repo, record["id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dump_record(record), encoding="utf-8", newline="\n")
    out = tmp_path / "dist"
    build_release(repo, out, data_version=VERSION, generated=GENERATED)
    lines = gzip.decompress((out / "places.ndjson.gz").read_bytes()).decode("utf-8").splitlines()
    return [json.loads(line).get("label") for line in lines]


def test_a_waterway_gives_kind_by_the_waterway(repo, tmp_path):
    assert labels(repo, tmp_path, place(1, {"nearest_waterway": CREEK})) == [
        {"text": "Campground by Black Bobs Creek", "from": "nearest_waterway"}
    ]


def test_a_river_gives_on_the_river_unless_its_name_starts_with_the(repo, tmp_path):
    river = {"name": "Murray River", "osm_id": 405, "distance_m": 40}
    the_river = {"name": "The River", "osm_id": 406, "distance_m": 40}
    assert labels(
        repo, tmp_path,
        place(1, {"nearest_waterway": river}, kind="national_park_campsite"),
        place(2, {"nearest_waterway": the_river}),
    ) == [
        {"text": "National park campsite on the Murray River", "from": "nearest_waterway"},
        {"text": "Campground by The River", "from": "nearest_waterway"},
    ]


def test_a_named_place_and_a_place_outside_the_overnight_group_get_none(repo, tmp_path):
    assert labels(
        repo, tmp_path,
        place(1, {"nearest_waterway": CREEK}, name="Bobs Bend"),
        place(2, {"nearest_waterway": CREEK}, kind="rest_area", group="utility"),
    ) == [None, None]


def test_a_name_that_says_nothing_gets_a_label(repo, tmp_path):
    # "Caravan Park" alone is no name (pipeline.duplicates.has_name).
    assert labels(repo, tmp_path, place(1, {"nearest_waterway": CREEK}, kind="caravan_park", name="Caravan Park")) == [
        {"text": "Caravan park by Black Bobs Creek", "from": "nearest_waterway"}
    ]


def feature(name, kind, osm_id, distance_m=100):
    return {"name": name, "type": kind, "osm_id": osm_id, "osm_type": "node", "distance_m": distance_m}


def test_a_feature_gives_at_a_beach_by_water_and_near_anything_else(repo, tmp_path):
    assert labels(
        repo, tmp_path,
        place(1, {"nearest_feature": feature("Trousers Point Beach", "beach", 1)}),
        place(2, {"nearest_feature": feature("Lake Eppalock", "water", 2)}, kind="free_camp"),
        place(3, {"nearest_feature": feature("Wallaby Spring", "spring", 3)}),
        place(4, {"nearest_feature": feature("Nepean Gorge", "gorge", 4)}),
        place(5, {"nearest_feature": feature("Bobs Flat", "locality", 5, 800)}),
    ) == [
        {"text": "Campground at Trousers Point Beach", "from": "nearest_feature"},
        {"text": "Free camp by Lake Eppalock", "from": "nearest_feature"},
        {"text": "Campground by Wallaby Spring", "from": "nearest_feature"},
        {"text": "Campground near Nepean Gorge", "from": "nearest_feature"},
        {"text": "Campground near Bobs Flat", "from": "nearest_feature"},
    ]


def test_a_waterway_comes_before_a_feature_and_a_feature_before_a_park(repo, tmp_path):
    park = {"name": "Oxley Wild Rivers National Park", "osm_id": 7, "osm_type": "relation"}
    hill = feature("Fixture Hill", "peak", 8)
    assert labels(
        repo, tmp_path,
        place(1, {"nearest_waterway": CREEK, "nearest_feature": hill, "protected_area": park}),
        place(2, {"nearest_feature": hill, "protected_area": park}),
        place(3, {"protected_area": park}),
    ) == [
        {"text": "Campground by Black Bobs Creek", "from": "nearest_waterway"},
        {"text": "Campground near Fixture Hill", "from": "nearest_feature"},
        {"text": "Campground in Oxley Wild Rivers National Park", "from": "protected_area"},
    ]


def test_a_park_loses_any_bracketed_part_of_its_name(repo, tmp_path):
    park = {"name": "Birriliburu Indigenous Protected Area (IUCN Cat. VI)", "osm_id": 9, "osm_type": "relation"}
    assert labels(repo, tmp_path, place(1, {"protected_area": park})) == [
        {"text": "Campground in Birriliburu Indigenous Protected Area", "from": "protected_area"}
    ]


def test_a_place_with_none_of_the_facts_gets_no_label(repo, tmp_path):
    town = {"name": "Winton", "osm_id": 247811870, "type": "town", "distance_m": 4120}
    assert labels(repo, tmp_path, place(1, {"locality": town, "toilets": True})) == [None]


def town(name, osm_id):
    return {"name": name, "osm_id": osm_id, "type": "town", "distance_m": 4000}


def test_places_in_one_state_with_the_same_text_each_add_their_town(repo, tmp_path):
    river = {"name": "Murray River", "osm_id": 405, "distance_m": 40}
    nsw = {"lat": -35.99, "lon": 146.4}  # Corowa, across the river from Victoria
    assert labels(
        repo, tmp_path,
        place(1, {"nearest_waterway": river, "locality": town("Cobram", 1)}),
        place(2, {"nearest_waterway": river, "locality": town("Echuca", 2)}),
        place(3, {"nearest_waterway": river}),
        place(4, {"nearest_waterway": river, "locality": town("Corowa", 3)}, location=nsw),
    ) == [
        {"text": "Campground on the Murray River, Cobram", "from": "nearest_waterway"},
        {"text": "Campground on the Murray River, Echuca", "from": "nearest_waterway"},
        # No town to add.
        {"text": "Campground on the Murray River", "from": "nearest_waterway"},
        # The only one in New South Wales.
        {"text": "Campground on the Murray River", "from": "nearest_waterway"},
    ]


def test_a_town_already_in_the_text_is_not_added_again(repo, tmp_path):
    lake = feature("Lake Bolac", "water", 11)
    assert labels(
        repo, tmp_path,
        place(1, {"nearest_feature": lake, "locality": town("Lake Bolac", 1)}),
        place(2, {"nearest_feature": lake, "locality": town("Wickliffe", 2)}),
    ) == [
        {"text": "Campground by Lake Bolac", "from": "nearest_feature"},
        {"text": "Campground by Lake Bolac, Wickliffe", "from": "nearest_feature"},
    ]


MERBEIN = {"lat": -34.17, "lon": 142.06}


def write_localities(repo, *rows):
    path = repo / "localities" / "localities.ndjson"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8", newline="\n")


def locality_row(name, osm_id, location):
    return {"name": name, "osm_id": osm_id, "type": "town", "population": 1000, **location}


def moved(location, north_m=0.0, east_m=0.0):
    import math

    return {
        "lat": round(location["lat"] + north_m / 111_195, 6),
        "lon": round(location["lon"] + east_m / (111_195 * math.cos(math.radians(location["lat"]))), 6),
    }


def test_places_that_share_a_town_say_how_far_and_which_way_from_it(repo, tmp_path):
    river = {"name": "Murray River", "osm_id": 405, "distance_m": 40}
    write_localities(repo, locality_row("Merbein", 3, MERBEIN))
    assert labels(
        repo, tmp_path,
        place(1, {"nearest_waterway": river, "locality": town("Merbein", 3)}, kind="reserve_campsite",
              location=moved(MERBEIN, north_m=1270, east_m=1270)),
        place(2, {"nearest_waterway": river, "locality": town("Merbein", 3)}, kind="reserve_campsite",
              location=moved(MERBEIN, east_m=-800)),
        place(3, {"nearest_waterway": river, "locality": town("Merbein", 3)}, kind="reserve_campsite",
              location=moved(MERBEIN, north_m=-4000)),
    ) == [
        {"text": "Reserve campsite on the Murray River, 1.8 km NE of Merbein", "from": "nearest_waterway"},
        {"text": "Reserve campsite on the Murray River, 800 m W of Merbein", "from": "nearest_waterway"},
        {"text": "Reserve campsite on the Murray River, 4.0 km S of Merbein", "from": "nearest_waterway"},
    ]


def test_places_with_no_town_measure_from_the_nearest_locality(repo, tmp_path):
    river = {"name": "Pentecost River", "osm_id": 407, "distance_m": 40}
    wyndham = {"lat": -15.49, "lon": 128.12}
    write_localities(repo, locality_row("Wyndham", 4, wyndham), locality_row("Kununurra", 5, {"lat": -15.77, "lon": 128.74}))
    assert labels(
        repo, tmp_path,
        place(1, {"nearest_waterway": river}, location=moved(wyndham, north_m=-52_000)),
        place(2, {"nearest_waterway": river}, location=moved(wyndham, north_m=-56_000, east_m=-3000)),
    ) == [
        {"text": "Campground on the Pentecost River, 52.0 km S of Wyndham", "from": "nearest_waterway"},
        {"text": "Campground on the Pentecost River, 56.1 km S of Wyndham", "from": "nearest_waterway"},
    ]


def test_places_within_100_m_of_each_other_keep_the_same_text(repo, tmp_path):
    lake = feature("Lake Burrinjuck", "water", 12)
    write_localities(repo, locality_row("Wee Jasper", 6, MERBEIN))
    cabin = {"nearest_feature": lake, "locality": town("Wee Jasper", 6)}
    assert labels(
        repo, tmp_path,
        place(1, cabin, kind="roofed_accommodation", location=moved(MERBEIN, north_m=15_010)),
        place(2, cabin, kind="roofed_accommodation", location=moved(MERBEIN, north_m=15_040)),
    ) == [{"text": "Roofed accommodation by Lake Burrinjuck, 15.0 km N of Wee Jasper", "from": "nearest_feature"}] * 2


def test_a_released_record_with_a_label_matches_the_schema(repo, tmp_path):
    from jsonschema import Draft202012Validator

    from pipeline.common import SCHEMA_DIR

    labels(repo, tmp_path, place(1, {"nearest_waterway": CREEK}))
    out = tmp_path / "dist"
    [record] = [json.loads(line) for line in gzip.decompress((out / "places.ndjson.gz").read_bytes()).splitlines()]
    schema = json.loads((SCHEMA_DIR / "place.schema.json").read_text(encoding="utf-8"))
    assert list(Draft202012Validator(schema).iter_errors(record)) == []


def test_a_label_in_a_repository_file_is_refused(repo):
    from pipeline.validate import validate_repo

    record = place(1, {"nearest_waterway": CREEK})
    record["label"] = {"text": "Campground by Black Bobs Creek", "from": "nearest_waterway"}
    path = place_path(repo, record["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_record(record), encoding="utf-8", newline="\n")
    errors = validate_repo(repo, now="2026-11-01T00:00:00Z")
    assert any("label" in e and "release" in e for e in errors), errors
