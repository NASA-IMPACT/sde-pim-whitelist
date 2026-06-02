"""Render a :class:`~pim_whitelist.diff.Delta` as a Markdown report."""

from __future__ import annotations

from .diff import Delta
from .sources.base import Provenance

# Cap how many orphans we enumerate inline; the count is always reported.
ORPHAN_PREVIEW = 50


def _origin_tag(provenance: Provenance) -> str:
    origins = provenance.origins
    return f"  [{', '.join(origins)}]" if origins else ""


def render(delta: Delta, *, date: str) -> str:
    lines: list[str] = []
    lines.append(f"### {delta.dataset.capitalize()} delta ({date})")
    lines.append("")
    lines.append(
        f"- **{len(delta.new_concepts)}** new concepts"
        f" · **{delta.added_alias_count}** new aliases on existing concepts"
        f" · **{len(delta.orphans)}** in whitelist but not in source"
    )
    lines.append("")

    if delta.new_concepts:
        lines.append(f"#### Added ({len(delta.new_concepts)} new concepts)")
        for sc in delta.new_concepts:
            lines.append(f"  + {';'.join(sc.aliases)}{_origin_tag(sc.provenance)}")
        lines.append("")

    if delta.new_aliases:
        lines.append(
            f"#### New aliases on existing concepts ({delta.added_alias_count})"
        )
        for na in delta.new_aliases:
            added = ", ".join(f'"{a}"' for a in na.aliases)
            lines.append(
                f"  ~ {na.concept.canonical}  +{added}{_origin_tag(na.source.provenance)}"
            )
        lines.append("")

    if delta.orphans:
        lines.append(
            f"#### In whitelist, not in source — review ({len(delta.orphans)})"
        )
        for concept in delta.orphans[:ORPHAN_PREVIEW]:
            lines.append(f"  ? {concept.canonical}")
        if len(delta.orphans) > ORPHAN_PREVIEW:
            lines.append(f"  … and {len(delta.orphans) - ORPHAN_PREVIEW} more")
        lines.append("")

    if delta.is_empty:
        lines.append("_No additions; whitelist already covers the source._")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_many(deltas: list[Delta], *, date: str) -> str:
    parts = [f"# PIM whitelist update — {date}", ""]
    total_new = sum(len(d.new_concepts) for d in deltas)
    total_alias = sum(d.added_alias_count for d in deltas)
    parts.append(
        f"**Totals:** {total_new} new concepts, {total_alias} new aliases "
        f"across {len(deltas)} dataset(s)."
    )
    parts.append("")
    for delta in deltas:
        parts.append(render(delta, date=date))
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"
