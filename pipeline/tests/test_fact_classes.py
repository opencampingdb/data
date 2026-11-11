"""Every fact in schema/place.schema.json says whether OSM can hold it.

x-ocdb-class is mapped (the extract fills it from an OSM tag or a nearby OSM
object), proposable (a mapper could check it, so it could become an OSM tag)
or ocdb_only (an opinion OSM can never hold). x-osm names the tag a mapped
fact comes from, or the form a proposable one would take. Extension values
of an otherwise mapped fact are listed under x-ocdb-extension-values.
"""

import json
from pathlib import Path

from jsonschema import Draft202012Validator

SCHEMA = Path(__file__).resolve().parents[2] / "schema" / "place.schema.json"
CLASSES = {"mapped", "proposable", "ocdb_only"}

# near_road is reported by travellers today and becomes a nearby fact worked
# out from OSM's major roads; it gets its class with that change.
UNCLASSED = {"near_road"}


def load_schema():
    return json.loads(SCHEMA.read_text(encoding="utf-8"))


def facts():
    return load_schema()["properties"]["facts"]["properties"]


def test_every_fact_has_a_class():
    missing = sorted(
        name
        for name, spec in facts().items()
        if name not in UNCLASSED and spec.get("x-ocdb-class") not in CLASSES
    )
    assert missing == []


def test_mapped_and_proposable_facts_name_their_osm_form():
    missing = sorted(
        name
        for name, spec in facts().items()
        if spec.get("x-ocdb-class") in {"mapped", "proposable"}
        and not (isinstance(spec.get("x-osm"), str) and spec["x-osm"].strip())
    )
    assert missing == []


def test_ocdb_only_facts_are_scenery_and_wildlife():
    only = {n for n, s in facts().items() if s.get("x-ocdb-class") == "ocdb_only"}
    assert only == {"scenery", "wildlife"}


def test_proposable_facts_are_the_agreed_list():
    proposable = {n for n, s in facts().items() if s.get("x-ocdb-class") == "proposable"}
    assert proposable == {
        "generators", "camper_trailers", "motorbikes", "big_rigs",
        "non_drinking_water", "drive_through", "ensuite_sites", "swimming",
        "walking_tracks", "games_room", "shade", "cycling_tracks",
        "offroad_tracks", "boat_ramp_nearby", "telstra_signal", "mobile_signal",
        "tv_reception", "pet_sitting", "lpg", "vehicle_storage",
        "self_contained_only", "sharks", "marine_stingers", "seasonal_closure",
        "dangerous_when_wet",
    }


def test_nearby_facts_are_mapped():
    spec = facts()
    for name in ("nearest_beach", "nearest_boat_ramp", "pub", "fuel", "car_wash", "crocodiles"):
        assert spec[name]["x-ocdb-class"] == "mapped", name
    assert "natural=beach" in spec["nearest_beach"]["x-osm"]
    assert "leisure=slipway" in spec["nearest_boat_ramp"]["x-osm"]


def test_extension_values_are_marked():
    spec = facts()
    assert spec["caravans"]["x-ocdb-extension-values"]["offroad"]["x-osm"] == "caravans=offroad"
    assert spec["pets"]["x-ocdb-extension-values"]["seasonal"]["x-ocdb-class"] in CLASSES
    assert spec["access"]["x-ocdb-extension-values"]["unknown"]["x-ocdb-class"] == "ocdb_only"


def test_every_extension_value_is_in_its_enum_and_classed():
    problems = []
    for name, spec in facts().items():
        for value, mark in spec.get("x-ocdb-extension-values", {}).items():
            if value not in spec.get("enum", []):
                problems.append(f"{name}: {value} not in enum")
            if mark.get("x-ocdb-class") not in CLASSES:
                problems.append(f"{name}: {value} has no class")
            if mark.get("x-ocdb-class") != "ocdb_only" and not mark.get("x-osm"):
                problems.append(f"{name}: {value} has no x-osm")
    assert problems == []


def test_annotations_do_not_change_validation():
    schema = load_schema()
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    record = {
        "id": "ocdb_01JBX00000000000000000000A",
        "name": "Creek camp",
        "kind": "free_camp",
        "group": "overnight",
        "location": {"lat": -30.5, "lon": 150.1},
        "osm": None,
        "facts": {"caravans": "offroad", "generators": True, "scenery": True},
        "status": "open",
        "rating": None,
        "fact_sources": {"caravans": "feedback", "generators": "feedback", "scenery": "feedback"},
        "updated": "2026-10-01T00:00:00Z",
    }
    assert list(validator.iter_errors(record)) == []
