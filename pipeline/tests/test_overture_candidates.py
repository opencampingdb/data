"""Tests for Overture candidates (opencampingdb/data#192), at the importer's
seam (place records and an Overture extract in, candidates by tier and the
rows each junk rule dropped out) and at the decision step (reviewed
candidates in, published records, the link file and the do-not-match list
out)."""

import json
from datetime import datetime, timezone

import pytest

from pipeline.common import dump_line, dump_record, load_places, place_path
from pipeline.maproulette import DOUBLE_MAPPING, TRAVELLER_REPORTS, out_of_spec_tasks, write_challenges
from pipeline.overture import import_overture
from pipeline.overture_candidates import apply_decisions
from pipeline.overture_rules import SHARED_PAGE_LIMIT, load_kept_lists, load_links
from pipeline.overture import main as overture_main
from pipeline.overture_decide import main as decide_main
from pipeline.tests.test_overture_source import inbox_with
from pipeline.tests.test_overture_import import A, G1, G2, LISTS, REPO, extract, ov, place, run
from pipeline.validate import validate_repo

KM = 1000  # metres

CANDIDATE_LISTS = LISTS.__class__(
    categories=LISTS.categories,
    directory_hosts=LISTS.directory_hosts,
    do_not_match=LISTS.do_not_match,
    candidate_kinds={"campground": "campground", "rv_park": "caravan_park", "holiday_park": "caravan_park"},
    chains=("big4 holiday parks", "discovery parks"),
)


def far(gers=G2, name="Sheepskin Hut Campground", category="campground", **changes):
    """An Overture camping place 5 km from the test place."""
    return ov(gers, name=name, category=category, metres_north=5 * KM, **changes)


def candidates(places, rows, lists=CANDIDATE_LISTS, **kwargs):
    result = run(places, rows, lists=lists, **kwargs)
    return result


def gers_in(result, tier):
    return [c["gers"] for c in result.candidates[tier]]


# ------------------------------------------------------------ the pool


def test_a_confident_camping_place_with_no_place_near_it_is_a_candidate():
    result = candidates([place()], [far()])
    found = result.candidates["strong"] + result.candidates["weak"]
    assert [c["gers"] for c in found] == [G2]
    assert found[0]["name"] == "Sheepskin Hut Campground"
    assert found[0]["kind"] == "campground"


def all_gers(result):
    return [c["gers"] for tier in result.candidates.values() for c in tier]


def test_an_overture_place_below_confidence_or_outside_the_camping_categories_is_never_a_candidate():
    rows = [far(confidence=0.69), far("08f2b1a0c4d5e6f70321a1b2c3d4e503", name="Main Road Roadhouse", category="gas_station")]
    result = candidates([place()], rows)
    assert all_gers(result) == []
    assert result.dropped == {}


# ------------------------------------------------------------ junk rules


def dropped_by(result, rule):
    return [c["gers"] for c in result.dropped.get(rule, [])]


def test_a_place_of_any_kind_within_250_m_drops_the_candidate():
    fuel = place(kind="fuel", name="Hut Road Fuel", metres_north=5 * KM + 249)
    result = candidates([fuel], [far()])
    assert all_gers(result) == []
    assert dropped_by(result, "near_place") == [G2]


def test_a_place_just_over_250_m_away_or_merged_leaves_the_candidate():
    over = place(kind="fuel", name="Hut Road Fuel", metres_north=5 * KM + 252)
    merged = place("ocdb_01M66CKP2X55A8CVGR95ZFJ1C0", metres_north=5 * KM, status="merged")
    assert all_gers(candidates([over, merged], [far()])) == [G2]


@pytest.mark.parametrize("name", ["BIG4 Holiday Parks", "Discovery Parks", "discovery-parks"])
def test_a_chain_only_name_drops_the_candidate(name):
    result = candidates([place()], [far(name=name)])
    assert all_gers(result) == []
    assert dropped_by(result, "chain_name") == [G2]


def test_a_park_of_a_chain_with_its_own_name_stays():
    assert all_gers(candidates([place()], [far(name="BIG4 Swan Bay Holiday Park")])) == [G2]


@pytest.mark.parametrize("name", [
    "Camp Niall Scouts", "Burnet Park Girl Guide Camp", "Quorn Area School", "Arbury Park Outdoor School",
    "Cheshunt Christian Camp", "Busselton Seventh-Day Adventist Church Camp", "Baptist Youth Camps",
    "Anglican Campsite", "YMCA Camp Wyuna", "Uniting Venues SA", "Chaverim Shalom College Outdoor Centre",
])
def test_a_scout_school_or_church_camp_drops_the_candidate(name):
    result = candidates([place()], [far(name=name)])
    assert all_gers(result) == []
    assert dropped_by(result, "scout_school_church") == [G2]


@pytest.mark.parametrize("name", ["The Old School Caravan Park", "The Old School House Campground", "School Point camping area"])
def test_a_place_named_for_an_old_school_or_a_school_point_stays(name):
    assert all_gers(candidates([place()], [far(name=name)])) == [G2]


def test_a_gers_id_on_the_do_not_match_list_is_never_a_candidate_again():
    lists = CANDIDATE_LISTS.__class__(**{**CANDIDATE_LISTS.__dict__, "rejected": frozenset({G2})})
    result = candidates([place()], [far()], lists=lists)
    assert all_gers(result) == []
    assert dropped_by(result, "do_not_match") == [G2]


def test_an_overture_place_with_no_name_is_dropped():
    result = candidates([place()], [far(name=None)])
    assert dropped_by(result, "no_name") == [G2]


def test_each_dropped_row_counts_under_the_first_rule_it_breaks():
    # On the do-not-match list, a chain name and next to a place: do-not-match wins.
    lists = CANDIDATE_LISTS.__class__(**{**CANDIDATE_LISTS.__dict__, "rejected": frozenset({G2})})
    result = candidates([place(metres_north=5 * KM)], [far(name="Discovery Parks")], lists=lists)
    assert {rule: dropped_by(result, rule) for rule in result.dropped} == {"do_not_match": [G2]}


# ------------------------------------------------------------------ tiers


def tier_of(row):
    result = candidates([place()], [row])
    return next(tier for tier, rows in result.candidates.items() if rows)


def test_a_candidate_with_high_confidence_a_camping_category_and_its_own_website_is_strong():
    assert tier_of(far(confidence=0.9, websites=["https://sheepskinhut.example.au/"])) == "strong"


@pytest.mark.parametrize("why, row", [
    ("confidence under 0.9", far(confidence=0.89, websites=["https://sheepskinhut.example.au/"])),
    ("no website", far(confidence=0.99, websites=[])),
    ("only a directory website", far(confidence=0.99, websites=["https://www.placestars.com/sheepskin-hut"])),
    ("camping only in the taxonomy", dict(far(confidence=0.99), basic_category="lodging", taxonomy_primary="holiday_park")),
])
def test_a_candidate_short_of_the_strong_tier_is_weak(why, row):
    assert tier_of(row) == "weak"


def test_a_social_website_is_not_its_own_website_for_the_strong_tier():
    lists = CANDIDATE_LISTS.__class__(**{**CANDIDATE_LISTS.__dict__,
                                         "directory_hosts": load_kept_lists(REPO).directory_hosts})
    for social in ("https://www.facebook.com/sheepskinhut", "https://www.tiktok.com/@sheepskinhut"):
        result = candidates([place()], [far(confidence=0.99, websites=[social])], lists=lists)
        assert gers_in(result, "strong") == []
        assert [c["website"] for c in result.candidates["weak"]] == [None]


def test_a_hipcamp_listing_overture_gives_as_the_website_counts_as_its_website():
    listing = "https://www.hipcamp.com/en-AU/land/sheepskin-hut-12345"
    lists = CANDIDATE_LISTS.__class__(**{**CANDIDATE_LISTS.__dict__,
                                         "directory_hosts": load_kept_lists(REPO).directory_hosts})
    result = candidates([place()], [far(confidence=0.99, websites=[listing])], lists=lists)
    assert [c["website"] for c in result.candidates["strong"]] == [listing]
    record = run([place()], [ov(websites=[listing])], lists=load_kept_lists(REPO)).records[A]
    assert record["facts"]["website"] == listing and record["fact_sources"]["website"] == "overture"


def test_every_candidate_carries_what_the_reviewer_needs():
    row = far(confidence=0.95, websites=["https://www.placestars.com/x", "https://sheepskinhut.example.au/"],
              phones=["(03) 5550 0101"],
              addresses=[{"freeform": "Hut Rd", "locality": "Bright", "postcode": "3741", "region": "VIC", "country": "AU"}])
    [entry] = candidates([place()], [row]).candidates["strong"]
    assert {k: v for k, v in entry.items() if k != "row"} == {
        "gers": G2,
        "name": "Sheepskin Hut Campground",
        "kind": "campground",
        "category": "campground",
        "taxonomy": "campground",
        "confidence": 0.95,
        "location": {"lat": -34.955084, "lon": 145.0},  # six decimal places, as the schema allows
        "state": "VIC",
        "website": "https://sheepskinhut.example.au/",
        "phone": "(03) 5550 0101",
        "release": "2026-10-21.0",
        "tier": "strong",
    }


def test_an_rv_park_or_holiday_park_candidate_is_a_caravan_park():
    rows = [far(category="rv_park"), dict(far("08f2b1a0c4d5e6f70321a1b2c3d4e503"), lat=-34.9, basic_category="lodging", taxonomy_primary="holiday_park")]
    kinds = {c["gers"]: c["kind"] for tier in candidates([place()], rows).candidates.values() for c in tier}
    assert set(kinds.values()) == {"caravan_park"}


# ------------------------------------------------------------ kept lists


def test_the_kept_lists_hold_the_chains_the_camping_categories_and_rejected_ids(tmp_path):
    lists = load_kept_lists(REPO)
    assert {"big4 holiday parks", "discovery parks"} <= set(lists.chains)
    assert lists.candidate_kinds == {"campground": "campground", "rv_park": "caravan_park", "holiday_park": "caravan_park"}
    # Every candidate category fits the kind it publishes as, so the next run can match it.
    assert all(category in lists.categories[kind] for category, kind in lists.candidate_kinds.items())

    for name in ("categories.json", "directory-hosts.json"):
        (tmp_path / "overture").mkdir(exist_ok=True)
        (tmp_path / "overture" / name).write_bytes((REPO / "overture" / name).read_bytes())
    (tmp_path / "overture" / "chains.json").write_text('{"names": ["BIG4 Holiday-Parks"]}', encoding="utf-8")
    (tmp_path / "overture" / "do-not-match.json").write_text(json.dumps({"pairs": [], "gers": [G2]}), encoding="utf-8")
    lists = load_kept_lists(tmp_path)
    assert lists.chains == ("big4 holiday parks",)
    assert lists.rejected == frozenset({G2})


# ------------------------------------------------------- decisions, publish


NEW_ID = "ocdb_01M66CKP2X55A8CVGR95ZFJ1N0"
RUN = "2026-11-15T00:00:00Z"


def kept_repo(repo):
    for name in ("categories.json", "directory-hosts.json", "do-not-match.json", "chains.json"):
        (repo / "overture").mkdir(exist_ok=True)
        (repo / "overture" / name).write_bytes((REPO / "overture" / name).read_bytes())
    return repo


def strong_candidate(**changes):
    row = far(confidence=0.95, websites=["https://sheepskinhut.example.au/"], phones=["(03) 5550 0101"],
              addresses=[{"freeform": "Hut Rd", "locality": "Bright", "postcode": "3741", "region": "VIC", "country": "AU"}],
              **changes)
    [entry] = candidates([place()], [row]).candidates["strong"]
    return entry


def decide(repo, *decisions, publish=None):
    kwargs = {} if publish is None else {"publish": publish}
    return apply_decisions(repo, [dict(entry, decision=d) for entry, d in decisions], now=RUN,
                           make_id=lambda: NEW_ID, **kwargs)


def test_a_rejected_candidate_goes_on_the_do_not_match_list_and_never_returns(repo):
    kept_repo(repo)
    outcome = decide(repo, (strong_candidate(), "reject"))
    assert outcome["rejected"] == [G2]
    saved = json.loads((repo / "overture" / "do-not-match.json").read_text(encoding="utf-8"))
    assert saved == {"pairs": [], "gers": [G2]}
    # Rejecting it again does not list it twice.
    decide(repo, (strong_candidate(), "reject"))
    assert json.loads((repo / "overture" / "do-not-match.json").read_text(encoding="utf-8"))["gers"] == [G2]
    result = run([place()], [far()], lists=load_kept_lists(repo))
    assert all_gers(result) == [] and dropped_by(result, "do_not_match") == [G2]


def test_publishing_is_off_unless_asked_for(repo):
    kept_repo(repo)
    outcome = decide(repo, (strong_candidate(), "approve"))
    assert outcome["published"] == [] and outcome["held"] == [G2]
    assert load_places(repo) == {}
    assert load_links(repo) == {}


def test_an_approved_candidate_publishes_as_not_checked_yet_with_overture_as_its_source(repo):
    kept_repo(repo)
    outcome = decide(repo, (strong_candidate(), "approve"), publish=True)
    assert outcome["published"] == [NEW_ID]
    assert load_places(repo)[NEW_ID] == {
        "id": NEW_ID,
        "name": "Sheepskin Hut Campground",
        "kind": "campground",
        "group": "overnight",
        "location": {"lat": -34.955084, "lon": 145.0},
        "osm": None,
        "facts": {
            "website": "https://sheepskinhut.example.au/",
            "phone": "(03) 5550 0101",
            "address": {"street": "Hut Rd", "suburb": "Bright", "state": "VIC", "postcode": "3741"},
        },
        "status": "open",
        "rating": None,
        "standing": "shown",
        "fact_sources": {k: "overture" for k in ("name", "location", "website", "phone", "address")},
        "fact_dates": {k: RUN for k in ("name", "location", "website", "phone", "address")},
        "updated": RUN,
    }
    assert load_links(repo) == {NEW_ID: {"gers": G2, "release": "2026-10-21.0"}}
    assert validate_repo(repo, now=RUN) == []


def test_the_overture_place_that_names_a_place_by_its_outline_is_never_a_candidate():
    # 450 m from the place's point, but inside its outline: it is the place's
    # name source, so it is no camping place the dataset lacks.
    from pipeline.geometry import Geometry
    from pipeline.tests.test_overture_import import outline
    geometry = Geometry(areas={"way/1010": outline(100, 600)})
    result = candidates([place(name=None)], [ov(metres_north=450, category="campground")], geometry=geometry)
    assert result.links[A]["gers"] == G1
    assert all_gers(result) == []


def test_a_candidates_pages_count_towards_the_shared_page_limit_and_a_shared_page_is_not_its_own():
    # Five parks of one chain match their Overture places, and a sixth park of
    # the chain is a candidate. Publishing it would give the chain's page to
    # six places, so the page is shared now, not only after publishing.
    chain = "https://www.chainparks.example.au/"
    parks = [place(f"ocdb_01M66CKP2X55A8CVGR95Z{i:05d}", name=f"Chain Park {i}", metres_north=i * 1000)
             for i in range(SHARED_PAGE_LIMIT)]
    rows = [ov(f"08f2b1a0c4d5e6f70321a1b2c{i:07d}", name=f"Chain Park {i}", metres_north=i * 1000, websites=[chain])
            for i in range(SHARED_PAGE_LIMIT)]
    rows.append(ov(G2, name="Chain Park Bright", category="campground", metres_north=20 * KM, confidence=0.95,
                   websites=[chain], socials=["https://facebook.com/ChainParks/"]))
    for row in rows[:-1]:
        row["socials"] = ["https://www.facebook.com/ChainParks"]
    result = candidates(parks, rows)
    assert result.shared == {chain: SHARED_PAGE_LIMIT + 1, "https://www.facebook.com/ChainParks": SHARED_PAGE_LIMIT + 1}
    assert [f for f in result.fills if f["key"] == "website"] == []
    [entry] = result.candidates["weak"]
    assert entry["gers"] == G2 and entry["website"] is None
    assert entry["row"]["websites"] == [] and entry["row"]["socials"] == []


def test_a_published_candidate_never_takes_the_id_of_a_place_already_there(repo):
    kept_repo(repo)
    existing = place(NEW_ID)
    path = place_path(repo, NEW_ID)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_record(existing), encoding="utf-8", newline="\n")
    ids = iter([NEW_ID, A])
    outcome = apply_decisions(repo, [dict(strong_candidate(), decision="approve")], now=RUN, publish=True,
                              make_id=lambda: next(ids))
    assert outcome["published"] == [A]
    assert load_places(repo)[NEW_ID] == existing
    assert load_links(repo) == {A: {"gers": G2, "release": "2026-10-21.0"}}


def test_flagged_candidates_wait_and_a_published_one_is_not_published_twice(repo):
    kept_repo(repo)
    entry = strong_candidate()
    decide(repo, (entry, "approve"), publish=True)
    outcome = decide(repo, (entry, "approve"), (dict(entry, gers=G1), "flag"), publish=True)
    assert outcome == {"published": [], "held": [], "rejected": [], "flagged": [G1], "already": [G2], "near": []}
    assert list(load_places(repo)) == [NEW_ID]


def test_a_published_candidate_is_no_candidate_next_run_and_keeps_its_name_and_location(repo):
    kept_repo(repo)
    decide(repo, (strong_candidate(), "approve"), publish=True)
    places, links = load_places(repo), load_links(repo)
    row = far(confidence=0.75, websites=[], phones=["(03) 5550 0101"])  # below a name source's 0.8, no website now
    result = import_overture(places, extract(row), links, load_kept_lists(repo), now="2026-12-15T00:00:00Z")
    assert all_gers(result) == [] and dropped_by(result, "near_place") == [G2]
    record = result.records[NEW_ID]
    assert (record["name"], record["location"]) == ("Sheepskin Hut Campground", {"lat": -34.955084, "lon": 145.0})
    assert record["fact_sources"]["name"] == record["fact_sources"]["location"] == "overture"
    assert "website" not in record["facts"]
    assert result.links[NEW_ID]["gers"] == G2


def test_a_published_candidate_never_becomes_a_maproulette_task(repo, tmp_path):
    kept_repo(repo)
    decide(repo, (strong_candidate(), "approve"), publish=True)
    # Travellers report a fact on it, which would make a task for an OSM place.
    box = inbox_with(tmp_path, NEW_ID, toilets=True, phone="(03) 5550 0101")
    write_challenges(repo, [], inbox=box, now=datetime(2026, 11, 20, tzinfo=timezone.utc))
    for name in (DOUBLE_MAPPING, TRAVELLER_REPORTS):
        assert (repo / "maproulette" / name).read_text(encoding="utf-8") == ""
    assert out_of_spec_tasks(load_places(repo), {}) == []


# ------------------------------------------------------------------ commands


def test_the_commands_write_the_candidates_then_act_on_the_decisions(repo, tmp_path):
    kept_repo(repo)
    rows = [far(confidence=0.95, websites=["https://sheepskinhut.example.au/"]),
            far("08f2b1a0c4d5e6f70321a1b2c3d4e503", name="McIntyres Campground", confidence=0.8, websites=[]),
            far("08f2b1a0c4d5e6f70321a1b2c3d4e504", name="BIG4 Holiday Parks")]
    source = tmp_path / "overture.ndjson"
    source.write_text("".join(json.dumps(line) + "\n" for line in extract(*[dict(r, lat=r["lat"] + i * 0.05) for i, r in enumerate(rows)])),
                      encoding="utf-8")
    report, summary, found = tmp_path / "report.md", tmp_path / "summary.json", tmp_path / "candidates.ndjson"

    assert overture_main([str(source), "--root", str(repo), "--report", str(report), "--summary", str(summary),
                          "--candidates", str(found), "--now", RUN]) == 0

    counts = json.loads(summary.read_text(encoding="utf-8"))
    assert counts["candidates"] == {"strong": 1, "weak": 1}
    assert counts["dropped"] == {"chain_name": 1}
    text = report.read_text(encoding="utf-8")
    assert "## Candidates" in text and "chain_name 1" in text and "Sheepskin Hut Campground" in text and "McIntyres Campground" in text
    lines = [json.loads(t) for t in found.read_text(encoding="utf-8").splitlines()]
    assert [(c["tier"], c["name"]) for c in lines] == [("strong", "Sheepskin Hut Campground"), ("weak", "McIntyres Campground")]

    # The reviewer's decisions: approve the strong one, reject the weak one.
    decisions = tmp_path / "decisions.ndjson"
    decisions.write_text("".join(json.dumps(dict(c, decision=d)) + "\n" for c, d in zip(lines, ("approve", "reject"))),
                         encoding="utf-8")
    assert decide_main([str(decisions), "--root", str(repo), "--now", RUN]) == 0
    assert load_places(repo) == {}  # publishing is off by default
    assert json.loads((repo / "overture" / "do-not-match.json").read_text(encoding="utf-8"))["gers"] == ["08f2b1a0c4d5e6f70321a1b2c3d4e503"]

    assert decide_main([str(decisions), "--root", str(repo), "--now", RUN, "--publish-candidates"]) == 0
    [record] = load_places(repo).values()
    assert record["name"] == "Sheepskin Hut Campground" and record["standing"] == "shown"
    assert validate_repo(repo, now=RUN) == []


def test_an_approved_candidate_within_250_m_of_a_place_or_of_one_just_published_waits(repo):
    kept_repo(repo)
    first = strong_candidate()
    twin = dict(first, gers=G1, name="Sheepskin Hut Camping Area",
                location={"lat": first["location"]["lat"] + 100 / 111_320, "lon": 145.0})
    ids = iter([NEW_ID, "ocdb_01M66CKP2X55A8CVGR95ZFJ1N1"])
    outcome = apply_decisions(repo, [dict(first, decision="approve"), dict(twin, decision="approve")],
                              now=RUN, publish=True, make_id=lambda: next(ids))
    assert outcome["published"] == [NEW_ID] and outcome["near"] == [G1]
    assert list(load_places(repo)) == [NEW_ID]


# ------------------------------------------------------------ tiers


def test_only_approvals_in_the_allowed_tiers_publish(repo):
    kept_repo(repo)
    strong = strong_candidate()
    weak = dict(strong, gers=G1, name="McIntyres Campground", tier="weak",
                location={"lat": strong["location"]["lat"] + 0.05, "lon": 145.0})
    outcome = apply_decisions(repo, [dict(strong, decision="approve"), dict(weak, decision="approve")],
                              now=RUN, publish=True, tiers={"strong"}, make_id=lambda: NEW_ID)
    assert outcome["published"] == [NEW_ID] and outcome["held"] == [G1]
    assert list(load_places(repo)) == [NEW_ID]


def test_every_tier_publishes_when_no_tier_is_named(repo):
    kept_repo(repo)
    weak = dict(strong_candidate(), tier="weak")
    outcome = apply_decisions(repo, [dict(weak, decision="approve")], now=RUN, publish=True, make_id=lambda: NEW_ID)
    assert outcome["published"] == [NEW_ID]


def test_the_decide_command_publishes_the_named_tiers_and_writes_its_counts(repo, tmp_path):
    kept_repo(repo)
    strong = strong_candidate()
    weak = dict(strong, gers=G1, name="McIntyres Campground", tier="weak",
                location={"lat": strong["location"]["lat"] + 0.05, "lon": 145.0})
    decisions, counts = tmp_path / "decisions.ndjson", tmp_path / "decided.json"
    decisions.write_text("".join(json.dumps(dict(c, decision="approve")) + "\n" for c in (strong, weak)), encoding="utf-8")
    assert decide_main([str(decisions), "--root", str(repo), "--now", RUN, "--publish-candidates",
                        "--tier", "strong", "--summary", str(counts)]) == 0
    assert json.loads(counts.read_text(encoding="utf-8")) == {
        "already": 0, "flagged": 0, "held": 1, "near": 0, "published": 1, "rejected": 0,
    }


# ------------------------------------------------------------ the shared format


# The decision line in pipeline/README.md "Overture decisions". The feedback
# engine's reviewer tests check the same line.
EXAMPLE_DECISION = (
    "{\"by_rules\":false,\"category\":\"campground\",\"confidence\":0.95,\"decision\":\"approve\",\"gers\":\"08f2b1a0c4d"
    "5e6f70321a1b2c3d4e502\",\"kind\":\"campground\",\"location\":{\"lat\":-36.7311,\"lon\":146.9602},\"name\":\"Sheeps"
    "kin Hut Campground\",\"phone\":\"(03) 5550 0101\",\"reason\":\"The name and the website show a campground.\","
    "\"release\":\"2026-10-21.0\",\"reviewer\":\"ai:@cf/meta/llama-3.3-70b-instruct-fp8-fast\",\"row\":{\"addresses\""
    ":[{\"country\":\"AU\",\"freeform\":\"Hut Rd\",\"locality\":\"Bright\",\"postcode\":\"3741\",\"region\":\"VIC\"}],\"basic_"
    "category\":\"campground\",\"change\":\"added\",\"confidence\":0.95,\"id\":\"08f2b1a0c4d5e6f70321a1b2c3d4e502\",\"l"
    "at\":-36.7311,\"lon\":146.9602,\"name\":\"Sheepskin Hut Campground\",\"phones\":[\"(03) 5550 0101\"],\"socials\":"
    "[],\"sources\":[{\"dataset\":\"meta\",\"record_id\":\"1\",\"update_time\":\"2026-08-14T03:21:00.000Z\"}],\"taxonomy"
    "_primary\":\"campground\",\"websites\":[\"https://sheepskinhut.example.au/\"]},\"state\":\"VIC\",\"taxonomy\":\"ca"
    "mpground\",\"tier\":\"strong\",\"website\":\"https://sheepskinhut.example.au/\"}"
)


def test_the_example_decision_line_is_the_one_in_the_readme():
    assert f"```\n{EXAMPLE_DECISION}\n```" in (REPO / "pipeline" / "README.md").read_text(encoding="utf-8")


def test_the_example_decision_line_is_in_the_form_the_reviewer_writes_and_overture_decide_reads(repo, tmp_path):
    kept_repo(repo)
    line = json.loads(EXAMPLE_DECISION)
    assert dump_line(line) == EXAMPLE_DECISION
    decisions = tmp_path / "decisions.ndjson"
    decisions.write_text(EXAMPLE_DECISION + "\n", encoding="utf-8")
    assert decide_main([str(decisions), "--root", str(repo), "--now", RUN, "--publish-candidates"]) == 0
    [record] = load_places(repo).values()
    assert (record["name"], record["kind"], record["standing"]) == ("Sheepskin Hut Campground", "campground", "shown")
    assert load_links(repo)[record["id"]] == {"gers": line["gers"], "release": "2026-10-21.0"}
    assert validate_repo(repo, now=RUN) == []
