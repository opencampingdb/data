"""The Overture import's report, in Markdown: the monthly workflow's PR body.

See pipeline/README.md "Overture import", "Report and summary".
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .overture_rules import SHARED_PAGE_LIMIT
from .overture_run import REPORT_MAX_CHARS
from .overture_socials import SOCIAL_FACTS
from .sync import REPORT_LIST_LIMIT

if TYPE_CHECKING:
    from .overture import ImportResult


# The values counted per kind, before and after the run.
COUNTED = ("name", "website", "phone", "address", *SOCIAL_FACTS)


def md_cell(text) -> str:
    """Text for one Markdown table cell: | escaped, new lines as spaces."""
    return str(text if text is not None else "").replace("|", "\\|").replace("\n", " ")


def render_report(result: ImportResult, summary: dict) -> str:
    """The run's report as Markdown, the monthly workflow's PR body.

    Lists are cut at REPORT_LIST_LIMIT rows, and shorter when the report
    would be longer than REPORT_MAX_CHARS.
    """
    limit = REPORT_LIST_LIMIT
    while True:
        report = _render_report(result, summary, limit)
        if len(report) <= REPORT_MAX_CHARS or limit == 0:
            return report
        limit //= 2


def more(count: int, limit: int) -> list[str]:
    return [f"And {count - limit} more, not listed here."] if count > limit else []


def place_label(result: ImportResult, place_id: str) -> str:
    name = (result.records.get(place_id) or {}).get("name")
    return f"{md_cell(name)} (`{place_id}`)" if name else f"`{place_id}`"


def value_text(value) -> str:
    if isinstance(value, dict):
        return md_cell(", ".join(f"{k}: {v}" for k, v in value.items()))
    return md_cell(value)


def _render_report(result: ImportResult, summary: dict, limit: int) -> str:
    lines = [
        "# Overture import report",
        "",
        f"Overture release {result.release}.",
        "",
        "| | Count |",
        "|---|---:|",
        f"| Places | {summary['places']} |",
        f"| Checked | {summary['checked']} |",
        f"| Overture matches | {summary['matches']} |",
        f"| Wide matches | {summary.get('wide_matches', 0)} |",
        f"| Name sources | {summary['name_sources']} |",
        f"| Places in the link file | {summary['links']} |",
        f"| Records changed | {summary['changed']} |",
        f"| Values filled or changed | {summary['fills']} |",
        f"| Values taken off | {summary['removals']} |",
        f"| Overture differs | {summary['differs']} |",
        "",
        f"Change: {summary['change_percent']:.2f}% of records.",
        "",
        "## Filled values by kind",
        "",
        "| Kind | Places | Names | Websites | Phones | Addresses | Facebook | Instagram | TikTok | X |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for kind, entry in summary["kinds"].items():
        cells = [f"{entry['before'][key]} to {entry['after'][key]}" for key in COUNTED]
        lines.append(f"| {kind} | {entry['places']} | " + " | ".join(cells) + " |")
    lines += ["", "## Gap fills", ""]
    if result.fills:
        lines += [
            "Values taken or changed, each sourced `overture`.",
            "",
            "| Place | Value | Overture | Was |",
            "|---|---|---|---|",
        ]
        lines += [
            f"| {place_label(result, f['place'])} | {f['key']} | {value_text(f['value'])} | {value_text(f['previous'])} |"
            for f in result.fills[:limit]
        ]
        lines += more(len(result.fills), limit)
    else:
        lines.append("None.")
    lines += [
        "",
        "## Overture differs",
        "",
    ]
    if result.differs:
        lines += [
            "OSM's value stays unless a person takes Overture's by listing it in `overture/taken.json`, "
            "which then makes it `overture`-sourced.",
            "",
            "| Place | Kind | Value | OSM | Overture | Overture place |",
            "|---|---|---|---|---|---|",
        ]
        for row in result.differs[:limit]:
            loc, oloc = row["location"], row["overture_location"]
            lines.append(
                f"| {md_cell(row['name'])} (`{row['place']}`, {loc['lat']},{loc['lon']}) | {row['kind']} | {row['key']} "
                f"| {md_cell(row['osm'])} | {md_cell(row['overture'])} "
                f"| {md_cell(row['overture_name'])} (`{row['gers']}`, {oloc['lat']},{oloc['lon']}) |"
            )
        lines += more(len(result.differs), limit)
    else:
        lines.append("None.")
    lines += ["", "## Shared pages left out", ""]
    if result.shared:
        lines += [
            f"Pages Overture gives for more than {SHARED_PAGE_LIMIT} places, taken for none of them.",
            "",
            "| Page | Places |",
            "|---|---:|",
        ]
        lines += [f"| {md_cell(page)} | {count} |" for page, count in list(result.shared.items())[:limit]]
        lines += more(len(result.shared), limit)
    else:
        lines.append("None.")
    lines += ["", "## Taken off", ""]
    if result.removals:
        lines += ["| Place | Value | Was |", "|---|---|---|"]
        lines += [f"| `{r['place']}` | {r['key']} | {md_cell(r['value'])} |" for r in result.removals[:limit]]
        lines += more(len(result.removals), limit)
    else:
        lines.append("None.")
    if result.twins:
        lines += [
            "",
            "## Published candidates that are places we have",
            "",
            "Each place's wide match is the Overture place a published candidate came from. A person merges the",
            "candidate into the place: `python -m pipeline.merge <place> <candidate>`.",
            "",
            "| Place | Candidate |",
            "|---|---|",
        ]
        lines += [f"| {place_label(result, t['place'])} | `{t['candidate']}` |" for t in result.twins[:limit]]
        lines += more(len(result.twins), limit)
    lines += ["", "## Candidates", ""]
    if any(result.candidates.values()):
        lines += [
            "Overture places we lack, by tier. Each one waits for a person, and none is published until",
            "publishing is switched on (`python -m pipeline.overture_decide --publish-candidates`).",
            "",
        ]
        for tier, rows in result.candidates.items():
            lines += [f"### {tier} ({len(rows)})", ""]
            if rows:
                lines += [
                    "| Overture place | Category | Confidence | State | Website | Location |",
                    "|---|---|---:|---|---|---|",
                ]
                for row in rows[:limit]:
                    loc = row.get("location") or {}
                    where = f"{loc['lat']},{loc['lon']}" if "lat" in loc else ""
                    lines.append(
                        f"| {md_cell(row.get('name'))} (`{row.get('gers', '')}`) | {md_cell(row.get('category'))} "
                        f"| {md_cell(row.get('confidence'))} | {md_cell(row.get('state'))} | {md_cell(row.get('website'))} | {where} |"
                    )
                lines += more(len(rows), limit)
            lines.append("")
        lines.pop()
    else:
        lines.append("None.")
    if result.dropped:
        lines += ["", "Dropped by the junk rules: " + ", ".join(
            f"{rule} {len(rows)}" for rule, rows in sorted(result.dropped.items())
        ) + "."]
    return "\n".join(lines) + "\n"
