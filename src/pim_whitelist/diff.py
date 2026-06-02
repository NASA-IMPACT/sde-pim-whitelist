"""Compute the delta between an upstream source and an existing whitelist.

Three categories, all derived through the match-key index so comparison is
case/whitespace/punctuation insensitive:

- ``new_concepts``  – source concepts whose every alias key is absent from the
  whitelist.
- ``new_aliases``   – source concepts that match an existing whitelist concept
  but contribute at least one alias whose key is new to that concept.
- ``orphans``       – whitelist concepts none of whose keys appear in the source
  (kept, never removed; surfaced for human review).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .normalize import match_key
from .sources.base import SourceConcept
from .whitelist import Concept, Whitelist


@dataclass
class NewAlias:
    concept: Concept  # the existing whitelist concept to extend
    aliases: list[str]  # cleaned alias strings to append
    source: SourceConcept


@dataclass
class Delta:
    dataset: str
    new_concepts: list[SourceConcept] = field(default_factory=list)
    new_aliases: list[NewAlias] = field(default_factory=list)
    orphans: list[Concept] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.new_concepts or self.new_aliases)

    @property
    def added_alias_count(self) -> int:
        return sum(len(na.aliases) for na in self.new_aliases)


def merge_source_concepts(concepts: list[SourceConcept]) -> list[SourceConcept]:
    """Combine concepts from several sources into one de-duplicated list.

    Concepts sharing any match key are folded together: aliases are unioned
    (canonical/first-seen order preserved) and provenance ``origins`` /
    ``collection_keys`` are merged. This must run before :func:`compute_delta`,
    which only matches against the *existing whitelist* and would otherwise
    append the same name once per source. Inputs are not mutated.

    A concept that bridges two previously-separate groups is folded into the
    first group it matches (good enough for an append-only delta).
    """
    merged: list[SourceConcept] = []
    index: dict[str, SourceConcept] = {}
    for sc in concepts:
        target: SourceConcept | None = None
        # Iterate aliases in order (canonical first) so matching is
        # deterministic across processes — ``keys()`` is an unordered set and
        # str hashing is randomized per run.
        for alias in sc.aliases:
            key = match_key(alias)
            if key and key in index:
                target = index[key]
                break
        if target is None:
            target = SourceConcept(
                aliases=list(sc.aliases),
                provenance=sc.provenance.copy(),
            )
            merged.append(target)
        else:
            existing_keys = target.keys()
            for alias in sc.aliases:
                key = match_key(alias)
                if key and key not in existing_keys:
                    target.aliases.append(alias)
                    existing_keys.add(key)
            target.provenance.merge(sc.provenance)
        for key in target.keys():
            index.setdefault(key, target)
    return merged


def compute_delta(
    dataset: str, whitelist: Whitelist, source_concepts: list[SourceConcept]
) -> Delta:
    index = whitelist.build_index()
    delta = Delta(dataset=dataset)
    matched_concepts: set[int] = set()

    for source in source_concepts:
        # Find the existing concept matched by any of this source's keys.
        # Iterate aliases in order (canonical first) for deterministic matching;
        # ``keys()`` is an unordered set and str hashing is randomized per run.
        existing: Concept | None = None
        for alias in source.aliases:
            key = match_key(alias)
            if key and key in index:
                existing = index[key]
                break

        if existing is None:
            delta.new_concepts.append(source)
            continue

        matched_concepts.add(id(existing))
        existing_keys = existing.keys()
        fresh = [a for a in source.aliases if match_key(a) not in existing_keys]
        if fresh:
            delta.new_aliases.append(
                NewAlias(concept=existing, aliases=fresh, source=source)
            )

    for concept in whitelist.concepts:
        if not concept.keys():
            continue  # blank/placeholder line
        if id(concept) not in matched_concepts:
            delta.orphans.append(concept)

    return delta
