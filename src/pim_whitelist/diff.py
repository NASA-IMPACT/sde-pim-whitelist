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


def compute_delta(
    dataset: str, whitelist: Whitelist, source_concepts: list[SourceConcept]
) -> Delta:
    index = whitelist.build_index()
    delta = Delta(dataset=dataset)
    matched_concepts: set[int] = set()

    for source in source_concepts:
        # Find the existing concept matched by any of this source's keys.
        existing: Concept | None = None
        for key in source.keys():
            if key in index:
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
