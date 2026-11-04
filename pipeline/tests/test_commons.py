import json
from datetime import date
from pathlib import Path

from pipeline import commons
from pipeline.common import dump_record


def page(title, *, dist=50.0, licence="CC BY-SA 4.0", artist="<a href='x'>Jane Doe</a>", width=4000, height=3000,
         mime="image/jpeg", categories=(), restrictions="", object_name=""):
    return {
        "title": title,
        "coordinates": [{"dist": dist}],
        "categories": [{"title": f"Category:{c}"} for c in categories],
        "imageinfo": [{
            "mime": mime,
            "width": width,
            "height": height,
            "url": f"https://upload.wikimedia.org/wikipedia/commons/a/ab/{title[5:]}",
            "thumburl": f"https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/{title[5:]}/960px-{title[5:]}",
            "descriptionurl": f"https://commons.wikimedia.org/wiki/{title.replace(' ', '_')}",
            "extmetadata": {
                "LicenseShortName": {"value": licence},
                "LicenseUrl": {"value": "https://creativecommons.org/licenses/by-sa/4.0"},
                "Artist": {"value": artist},
                "Restrictions": {"value": restrictions},
                "ObjectName": {"value": object_name},
            },
        }],
    }


def test_distinctive_words_drop_generic_and_short_words():
    assert commons.distinctive_words("Lake Eildon Caravan Park") == {"eildon"}
    assert commons.distinctive_words("Campsite C10") == {"c10"}
    assert commons.distinctive_words("Campsite 10") == set()
    assert commons.distinctive_words(None) == set()


def test_allowed_licences():
    for ok in ("CC BY-SA 4.0", "CC BY 2.0", "CC0", "Public domain", "CC BY-SA 3.0 au", "cc-by-sa 2.5"):
        assert commons.allowed_licence(ok), ok
    for no in ("GFDL", "CC BY-NC 2.0", "CC BY-ND 4.0", "Fair use", ""):
        assert not commons.allowed_licence(no), no


def test_match_kinds_in_order_of_trust():
    words = {"tidal"}
    assert commons.match_kind(words, "Tidal River campground", 1500) == "name"
    assert commons.match_kind(words, "Caravans at the beach", 200) == "camping"
    assert commons.match_kind(words, "Caravans at the beach", 400) is None
    assert commons.match_kind(words, "Tidal River at dusk", 400) == "area"
    assert commons.match_kind(words, "Tidal River at dusk", 900) is None
    # No distinctive words: only a camping word can match.
    assert commons.match_kind(set(), "Bridge at dusk", 10) is None


def test_file_entry_keeps_credit_licence_and_urls():
    entry = commons.file_entry(page("File:Tidal River campground.jpg", dist=120.4), {"tidal"})
    assert entry == {
        "file": "Tidal River campground.jpg",
        "width": 4000,
        "height": 3000,
        "thumb": "https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/Tidal River campground.jpg/960px-Tidal River campground.jpg",
        "full": "https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/Tidal River campground.jpg/1920px-Tidal River campground.jpg",
        "page": "https://commons.wikimedia.org/wiki/File:Tidal_River_campground.jpg",
        "licence": "CC BY-SA 4.0",
        "licence_url": "https://creativecommons.org/licenses/by-sa/4.0",
        "credit": "Jane Doe",
        "match": "name",
        "distance_m": 120,
    }


def test_file_entry_uses_the_original_when_it_is_small():
    entry = commons.file_entry(page("File:Tidal River campground.jpg", width=1200, height=900), {"tidal"})
    assert entry["full"] == "https://upload.wikimedia.org/wikipedia/commons/a/ab/Tidal River campground.jpg"


def test_file_entry_leaves_out_what_cannot_be_shown():
    name = {"tidal"}
    title = "File:Tidal River campground.jpg"
    assert commons.file_entry(page(title, licence="CC BY-NC 2.0"), name) is None
    assert commons.file_entry(page(title, mime="image/svg+xml"), name) is None
    assert commons.file_entry(page(title, width=600, height=400), name) is None
    assert commons.file_entry(page(title, restrictions="personality"), name) is None
    assert commons.file_entry(page(title, artist=""), name) is None
    assert commons.file_entry(page(title, artist="", licence="CC0"), name)["licence"] == "CC0"
    assert commons.file_entry(page("File:Bridge.jpg"), name) is None


def test_categories_count_as_text():
    entry = commons.file_entry(page("File:IMG 1234.jpg", categories=["Tidal River campground"]), {"tidal"})
    assert entry["match"] == "name"


def test_rank_orders_by_trust_then_distance_and_caps():
    entries = [{"match": m, "distance_m": d, "file": f} for m, d, f in [
        ("area", 10, "a"), ("camping", 200, "b"), ("name", 900, "c"), ("name", 100, "d"),
        ("camping", 50, "e"), ("area", 5, "f"),
    ]]
    assert [e["file"] for e in commons.rank(entries)] == ["d", "c", "e", "b", "f"]


def write_place(root: Path, place_id: str, *, group="overnight", name="Tidal River", status="open"):
    from pipeline.common import place_path

    record = {
        "id": place_id, "group": group, "kind": "national_park_campsite", "name": name, "status": status,
        "location": {"lat": -39.03, "lon": 146.32}, "facts": {}, "fact_sources": {}, "rating": None,
        "updated": "2026-10-30T00:00:00Z", "osm": {"type": "node", "id": 1, "version": 1},
    }
    path = place_path(root, place_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_record(record), encoding="utf-8")


def test_run_checks_due_places_and_keeps_the_rest(tmp_path):
    write_place(tmp_path, "ocdb_A")
    write_place(tmp_path, "ocdb_B")
    write_place(tmp_path, "ocdb_C")
    write_place(tmp_path, "ocdb_D", group="utility")
    matches = tmp_path / commons.MATCHES_FILE
    matches.parent.mkdir(parents=True)
    matches.write_text(
        json.dumps({"place": "ocdb_B", "checked": "2026-11-01", "files": []}) + "\n"
        + json.dumps({"place": "ocdb_C", "checked": "2026-09-01", "files": []}) + "\n"
        + json.dumps({"place": "ocdb_gone", "checked": "2026-11-01", "files": []}) + "\n",
        encoding="utf-8",
    )
    asked = []

    def fetch(url):
        asked.append(url)
        return {"query": {"pages": [page("File:Tidal River campground.jpg")]}}

    summary = commons.run(tmp_path, date(2026, 11, 4), None, 30, fetch=fetch, pause=0, log=lambda _: None)
    lines = [json.loads(line) for line in matches.read_text(encoding="utf-8").splitlines()]
    assert [line["place"] for line in lines] == ["ocdb_A", "ocdb_B", "ocdb_C"]
    assert lines[0]["checked"] == "2026-11-04" and lines[0]["files"][0]["match"] == "name"
    assert lines[1] == {"place": "ocdb_B", "checked": "2026-11-01", "files": []}
    assert lines[2]["checked"] == "2026-11-04"
    assert len(asked) == 2
    assert summary == {"checked": 2, "matched": 2, "places": 3, "with_photo": 2, "lines": 3}


def test_run_respects_max_places(tmp_path):
    for p in ("ocdb_A", "ocdb_B", "ocdb_C"):
        write_place(tmp_path, p)
    summary = commons.run(tmp_path, date(2026, 11, 4), 2, 30, fetch=lambda url: {}, pause=0, log=lambda _: None)
    assert summary["checked"] == 2 and summary["lines"] == 2


def test_validate_checks_the_matches_file(tmp_path):
    from pipeline.validate import check_commons

    assert check_commons(tmp_path) == []
    path = tmp_path / commons.MATCHES_FILE
    path.parent.mkdir(parents=True)
    good = {"place": "ocdb_01M66ESC6PTN65XY3VDH1Y283K", "checked": "2026-11-04", "files": []}
    commons.write_matches(path, {good["place"]: good})
    assert check_commons(tmp_path) == []
    path.write_text(json.dumps(good, indent=1) + "\n" + '{"place": "x"}\n', encoding="utf-8")
    assert check_commons(tmp_path)
