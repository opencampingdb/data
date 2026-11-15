"""Tests for the monthly Overture workflow's Python side: the import report
and summary as the PR body and the auto-merge limit (pipeline.overture), and
the release check, the applied release, the reviewer's files carried over and
the PR body (pipeline.overture_run). The workflow itself is
.github/workflows/sync-overture.yml."""

import json

import pytest

from pipeline.common import place_path
from pipeline.merge import main as merge_main
from pipeline.overture import ImportResult, render_report, summary_of
from pipeline.overture_run import (
    REPORT_MAX_CHARS,
    applied_release,
    main,
    plan_run,
    pr_body,
    releases_in_listing,
    should_auto_merge,
)
from pipeline.sync import MAX_REPORT_CHARS
from pipeline.tests.test_overture_import import A, B, G1, G2, RELEASE, REPO, extract, ov, place, run, write_repo

LISTING = """<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Name>overturemaps-us-west-2</Name>
<Prefix>release/</Prefix><KeyCount>3</KeyCount><MaxKeys>1000</MaxKeys><Delimiter>/</Delimiter>
<IsTruncated>false</IsTruncated>
<CommonPrefixes><Prefix>release/2026-09-23.1/</Prefix></CommonPrefixes>
<CommonPrefixes><Prefix>release/2026-10-21.0/</Prefix></CommonPrefixes>
<CommonPrefixes><Prefix>release/2026-09-23.0/</Prefix></CommonPrefixes>
</ListBucketResult>
"""


def places_of(*records):
    return {r["id"]: r for r in records}


def many_places(n):
    return places_of(*(place(f"ocdb_01M66CKP2X55A8CVGR95Z{i:05d}", metres_north=i * 1000) for i in range(n)))


# ------------------------------------------------------------ the report


def test_the_report_lists_each_gap_fill_with_its_place_value_and_what_it_replaced():
    result = run([place()], [ov()])
    text = render_report(result, summary_of(result, places_of(place())))
    assert "## Gap fills" in text
    assert f"| Riverbend Caravan Park (`{A}`) | website | https://riverbend.example.au/ |  |" in text
    assert f"| Riverbend Caravan Park (`{A}`) | phone | +61 2 5550 0101 |  |" in text


def test_the_report_lists_the_candidates_by_tier():
    result = ImportResult(release=RELEASE, candidates={
        "approved": [{"gers": G1, "name": "Hidden Creek Camp", "category": "campground", "confidence": 0.91,
                      "location": {"lat": -35.1, "lon": 145.2}}],
        "flagged": [],
    })
    text = render_report(result, summary_of(result, {}))
    assert "## Candidates" in text
    assert "### approved (1)" in text and "Hidden Creek Camp" in text and G1 in text
    assert "### flagged (0)" in text


def test_the_report_counts_overture_matches_and_name_sources_apart():
    places = [place(), place(B, name=None, metres_north=5000)]
    rows = [ov(), ov(G2, name="Hilltop Camp", metres_north=5000)]
    result = run(places, rows)
    summary = summary_of(result, places_of(*places))
    assert (summary["matches"], summary["name_sources"]) == (1, 1)
    text = render_report(result, summary)
    assert "| Overture matches | 1 |" in text and "| Name sources | 1 |" in text
    assert "Matched (links)" not in text


def test_the_report_and_summary_list_the_shared_pages_left_out():
    result = ImportResult(release=RELEASE, shared={"https://www.facebook.com/Shell": 642})
    summary = summary_of(result, {})
    assert summary["shared_pages"] == 1
    text = render_report(result, summary)
    assert "## Shared pages left out" in text
    assert "| https://www.facebook.com/Shell | 642 |" in text


def test_the_report_says_none_when_there_are_no_gap_fills_or_candidates():
    result = run([place(facts={"website": "https://riverbend.example.au/", "phone": "+61 2 5550 0101"})], [ov()])
    text = render_report(result, summary_of(result, {}))
    assert "## Gap fills\n\nNone." in text
    assert "## Candidates\n\nNone." in text


def test_the_report_fits_a_pr_body_when_every_place_is_filled():
    places = many_places(3000)
    rows = [ov(f"08f2b1a0c4d5e6f70321a1b2c{i:07d}", metres_north=i * 1000, websites=[f"https://park{i}.example.au/"])
            for i in range(3000)]
    result = run(list(places.values()), rows)
    assert len(result.fills) == 6000
    text = render_report(result, summary_of(result, places))
    assert len(text) <= MAX_REPORT_CHARS
    assert "more, not listed here" in text


# ----------------------------------------------------- the auto-merge limit


def test_gap_fills_under_the_limit_merge_themselves():
    places = many_places(100)
    result = run(list(places.values()), [ov()])
    summary = summary_of(result, places)
    assert summary["change_percent"] == 1.0
    assert summary["auto_merge"] is True
    assert summary["wait_reasons"] == []


def test_gap_fills_at_the_limit_wait_for_review():
    places = many_places(50)
    result = run(list(places.values()), [ov()])
    summary = summary_of(result, places)
    assert summary["change_percent"] == 2.0
    assert summary["auto_merge"] is False
    assert summary["wait_reasons"] == ["2.00% of records changed, 2% or more"]


def test_overture_differs_waits_for_review_even_under_the_limit():
    places = many_places(100)
    places[B] = place(B, name="Main Road Roadhouse", kind="fuel", metres_north=-5000, facts={"phone": "+61 8 5550 9999"})
    rows = [ov(G2, name="Main Road Roadhouse", category="gas_station", metres_north=-5000, websites=[],
               phones=["+61 8 5550 0303"])]
    result = run(list(places.values()), rows)
    summary = summary_of(result, places)
    assert summary["changed"] == 0
    assert summary["auto_merge"] is False
    assert summary["wait_reasons"] == ["1 Overture differs"]


def test_candidates_found_do_not_hold_the_gap_fills():
    # Candidates are only listed: nothing in places/ changes until a decision publishes one.
    result = ImportResult(release=RELEASE, candidates={"strong": [{"gers": G1}], "weak": []})
    summary = summary_of(result, many_places(100))
    assert summary["auto_merge"] is True
    assert summary["wait_reasons"] == []


def test_tiers_with_no_candidates_do_not_hold_the_run():
    result = ImportResult(release=RELEASE, candidates={"approved": [], "flagged": []})
    assert summary_of(result, many_places(100))["auto_merge"] is True


# --------------------------------------------------------- the release check


def test_the_listing_gives_every_release_oldest_first():
    assert releases_in_listing(LISTING) == ["2026-09-23.0", "2026-09-23.1", "2026-10-21.0"]


def test_the_newest_release_runs_when_none_was_applied():
    assert plan_run(LISTING, applied=None) == {"release": "2026-10-21.0", "changelog": False, "skip": None}


def test_the_newest_release_is_skipped_when_it_was_already_applied():
    assert plan_run(LISTING, applied="2026-10-21.0") == {
        "release": "2026-10-21.0", "changelog": False, "skip": "Overture 2026-10-21.0 was already applied",
    }


def test_a_changelog_run_only_follows_the_release_just_before_it():
    assert plan_run(LISTING, applied="2026-09-23.1")["changelog"] is True
    # A missed release: its changes are not in the newest changelog.
    assert plan_run(LISTING, applied="2026-09-23.0")["changelog"] is False


def test_a_release_asked_for_by_hand_runs_even_when_it_was_applied():
    assert plan_run(LISTING, applied="2026-10-21.0", wanted="2026-10-21.0") == {
        "release": "2026-10-21.0", "changelog": False, "skip": None,
    }
    assert plan_run(LISTING, applied="2026-09-23.1", wanted="2026-10-21.0")["changelog"] is True


@pytest.mark.parametrize("wanted", ["2026-11-18.0", "../2026-10-21.0", "latest"])
def test_a_release_asked_for_by_hand_must_be_in_the_listing(wanted):
    with pytest.raises(ValueError):
        plan_run(LISTING, applied=None, wanted=wanted)


def test_an_empty_listing_is_an_error():
    with pytest.raises(ValueError):
        plan_run("<ListBucketResult></ListBucketResult>", applied=None)


def test_the_check_command_writes_step_outputs(repo, tmp_path):
    listing, out = tmp_path / "listing.xml", tmp_path / "output"
    listing.write_text(LISTING, encoding="utf-8")
    assert main(["check", "--listing", str(listing), "--root", str(repo), "--output", str(out)]) == 0
    assert out.read_text(encoding="utf-8") == "release=2026-10-21.0\nchangelog=false\nskip=false\n"

    assert main(["applied", "2026-10-21.0", "--root", str(repo)]) == 0
    assert applied_release(repo) == "2026-10-21.0"
    assert json.loads((repo / "overture" / "release.json").read_text(encoding="utf-8")) == {"release": "2026-10-21.0"}

    out.write_text("", encoding="utf-8")
    assert main(["check", "--listing", str(listing), "--root", str(repo), "--output", str(out)]) == 0
    assert out.read_text(encoding="utf-8") == "release=2026-10-21.0\nchangelog=false\nskip=true\n"


# -------------------------------------------------------------- the PR body


def test_the_body_says_whether_the_pr_merges_itself_then_gives_the_report():
    body = pr_body("# Overture import report\n", {"auto_merge": True, "wait_reasons": []})
    assert body.startswith("Only gap fills, under the limit: the PR merges itself once CI passes.")
    assert "# Overture import report" in body
    assert "Automatic merges" not in body


def test_only_gap_fills_under_the_limit_merge_themselves():
    assert should_auto_merge({"auto_merge": True, "wait_reasons": []}) is True
    assert should_auto_merge({"auto_merge": False, "wait_reasons": ["1 Overture differs"]}) is False


def test_a_candidate_published_in_this_run_waits_for_a_person():
    summary = {"auto_merge": True, "wait_reasons": []}
    assert should_auto_merge(summary, published=1) is False
    body = pr_body("report\n", {"auto_merge": False, "wait_reasons": ["1 Overture differs"]}, published=2)
    assert "waits for a person: 1 Overture differs, 2 candidates published in this run." in body


def test_decision_lines_that_were_skipped_are_listed_for_a_person():
    skipped = [{"line": 3, "gers": "08f2b1a0c4d5e6f70321a1b2c3d4e599", "problem": "unknown decision 'maybe'"},
               {"line": 4, "gers": None, "problem": "not a JSON object: oops"}]
    summary = {"auto_merge": True, "wait_reasons": []}
    assert should_auto_merge(summary, skipped=skipped) is False
    body = pr_body("# Overture import report\n", summary, skipped=skipped)
    assert "waits for a person: 2 decision lines skipped." in body
    assert "## Decision lines that need a person" in body
    assert "| 3 | 08f2b1a0c4d5e6f70321a1b2c3d4e599 | unknown decision 'maybe' |" in body
    assert "| 4 |  | not a JSON object: oops |" in body
    assert body.index("## Decision lines that need a person") < body.index("# Overture import report")


def test_the_body_command_lists_the_skipped_decision_lines(repo, tmp_path):
    report, summary, decided = tmp_path / "report.md", tmp_path / "summary.json", tmp_path / "decided.json"
    body, out = tmp_path / "body.md", tmp_path / "output"
    report.write_text("# Overture import report\n", encoding="utf-8")
    summary.write_text(json.dumps({"auto_merge": True, "wait_reasons": []}), encoding="utf-8")
    decided.write_text(json.dumps({"published": 0, "skipped": 1,
                                   "skipped_lines": [{"line": 1, "gers": None, "problem": "no GERS id"}]}),
                       encoding="utf-8")
    assert main(["body", "--report", str(report), "--summary", str(summary), "--decided", str(decided),
                 "--out", str(body), "--output", str(out)]) == 0
    assert out.read_text(encoding="utf-8") == "auto_merge=false\n"
    assert "| 1 |  | no GERS id |" in body.read_text(encoding="utf-8")


def test_the_body_fits_a_pr_body():
    body = pr_body("x" * (MAX_REPORT_CHARS + 10), {"auto_merge": False, "wait_reasons": []})
    assert len(body) <= MAX_REPORT_CHARS
    assert body.endswith("The full report is in the workflow run's log.\n")


def test_the_report_budget_leaves_room_for_the_decision_line():
    assert REPORT_MAX_CHARS + 1_000 <= MAX_REPORT_CHARS


def test_the_body_command_writes_the_body_and_the_decision(repo, tmp_path):
    report, summary, decided = tmp_path / "report.md", tmp_path / "summary.json", tmp_path / "decided.json"
    body, out = tmp_path / "body.md", tmp_path / "output"
    report.write_text("# Overture import report\n", encoding="utf-8")
    summary.write_text(json.dumps({"auto_merge": True, "wait_reasons": []}), encoding="utf-8")
    args = ["body", "--report", str(report), "--summary", str(summary), "--out", str(body), "--output", str(out)]
    assert main(args) == 0
    assert "# Overture import report" in body.read_text(encoding="utf-8")
    assert out.read_text(encoding="utf-8") == "auto_merge=true\n"

    decided.write_text(json.dumps({"published": 1, "held": 0}), encoding="utf-8")
    out.write_text("", encoding="utf-8")
    assert main([*args, "--decided", str(decided)]) == 0
    assert out.read_text(encoding="utf-8") == "auto_merge=false\n"
    assert "1 candidate published in this run" in body.read_text(encoding="utf-8")


# ------------------------------------------------- the reviewer's work carried


def test_the_reviewers_decisions_and_rejections_are_carried_into_the_rebuilt_branch(repo, tmp_path):
    (repo / "overture").mkdir(exist_ok=True)
    (repo / "overture" / "do-not-match.json").write_text(
        json.dumps({"pairs": [{"place": A, "gers": G1}], "gers": ["g-main"]}), encoding="utf-8")
    branch = tmp_path / "branch"
    branch.mkdir()
    (branch / "decisions.ndjson").write_text('{"decision":"approve","gers":"g-new"}\n', encoding="utf-8")
    (branch / "do-not-match.json").write_text(json.dumps({"pairs": [], "gers": ["g-branch", "g-main"]}), encoding="utf-8")

    assert main(["carry", "--decisions", str(branch / "decisions.ndjson"),
                 "--do-not-match", str(branch / "do-not-match.json"), "--root", str(repo)]) == 0

    assert (repo / "overture" / "decisions.ndjson").read_text(encoding="utf-8") == '{"decision":"approve","gers":"g-new"}\n'
    assert json.loads((repo / "overture" / "do-not-match.json").read_text(encoding="utf-8")) == {
        "pairs": [{"place": A, "gers": G1}], "gers": ["g-branch", "g-main"],
    }


def test_carrying_from_a_branch_without_the_files_changes_nothing(repo, tmp_path):
    (repo / "overture").mkdir(exist_ok=True)
    kept = json.dumps({"pairs": [], "gers": ["g-main"]})
    (repo / "overture" / "do-not-match.json").write_text(kept, encoding="utf-8")
    assert main(["carry", "--decisions", str(tmp_path / "none.ndjson"),
                 "--do-not-match", str(tmp_path / "none.json"), "--root", str(repo)]) == 0
    assert not (repo / "overture" / "decisions.ndjson").exists()
    assert (repo / "overture" / "do-not-match.json").read_text(encoding="utf-8") == kept


# ------------------------------------------------------------ the workflow


WORKFLOW = (REPO / ".github" / "workflows" / "sync-overture.yml").read_text(encoding="utf-8")


def test_the_workflow_does_not_run_the_automatic_merge_rule():
    assert "pipeline.merge" not in WORKFLOW


def test_the_workflow_carries_the_reviewers_work_then_decides_before_the_import():
    carry = WORKFLOW.index("pipeline.overture_run carry")
    decide = WORKFLOW.index("pipeline.overture_decide overture/decisions.ndjson")
    imported = WORKFLOW.index("python -m pipeline.overture \"$RUNNER_TEMP/overture.ndjson\"")
    assert carry < decide < imported
    assert "--publish-candidates" in WORKFLOW[decide:imported]
    assert "--candidates overture/candidates.ndjson" in WORKFLOW[imported:]


# ------------------------------------------------------ merge --auto summary


def test_the_automatic_merge_command_writes_the_merges_it_made(repo, tmp_path):
    write_repo(repo, [place(A, osm={"type": "node", "id": 1, "version": 1}),
                      place(B, osm={"type": "node", "id": 2, "version": 1}, metres_north=10)])
    geometry, merges = tmp_path / "geometry.ndjson", tmp_path / "merges.json"
    geometry.write_text("", encoding="utf-8")
    assert merge_main(["--auto", "--geometry", str(geometry), "--root", str(repo), "--summary", str(merges)]) == 0
    made = json.loads(merges.read_text(encoding="utf-8"))
    assert [(m["kept"], m["dropped"], m["distance_m"], m["part"]) for m in made] == [(A, B, 10, False)]
    assert made[0]["kept_name"] == made[0]["dropped_name"] == "Riverbend Caravan Park"
    assert json.loads(place_path(repo, B).read_text(encoding="utf-8"))["status"] == "merged"
