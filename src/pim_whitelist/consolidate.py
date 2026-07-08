"""Reprocess a whole whitelist: fold duplicates, merge safe acronyms, sort.

Unlike the append-only merge path this replaces, this module treats the *entire*
existing whitelist as just another set of concepts and rebuilds it from scratch each
run. Three things happen, in order:

1. **Exact-key fold.** Existing concepts (prepended, so their curated canonical wins)
   and any new source concepts are folded together by :func:`match_key` equality via
   the existing :func:`~pim_whitelist.diff.merge_source_concepts`. This collapses
   pre-existing exact-key duplicates *and* attaches new source entries as aliases of
   the concept they match.

2. **Safe acronym merge.** An entry like ``Science in Microgravity Box (SIMBOX)``
   carries a parenthetical acronym (``SIMBOX``) that, normalized, equals a separate
   *bare* entry (``Simbox``). Those are folded together — but only under a deliberately
   conservative rule (see :data:`MIN_ACRONYM_LEN` and :func:`_acronym_merge`) so generic
   short acronyms (ACE, CCD, MRI) never trigger a wrong merge. Anything the safe rule
   rejects is *reported*, not applied.

3. **Sort.** Concepts are ordered case-insensitively by canonical name; alias order
   within each line is left untouched.

The function returns the rebuilt :class:`~pim_whitelist.whitelist.Whitelist` plus a
:class:`ConsolidationReport` describing what was merged and which fuzzy/ambiguous
candidates a human should review.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

from . import normalize
from .diff import merge_source_concepts
from .sources.base import Provenance, SourceConcept
from .whitelist import Concept, Whitelist

# A parenthetical acronym must normalize to at least this many characters before
# it can trigger an automatic merge. Three-letter acronyms (ACE, CCD, MRI, HRI)
# collide far too often across unrelated concepts, so they are only ever reported.
MIN_ACRONYM_LEN = 4

# Normalized-key similarity at or above which two adjacent (post-sort) canonicals
# are surfaced as a likely typo/near-duplicate for manual review.
FUZZY_RATIO = 0.90
# How far on either side of each concept to look for fuzzy neighbours. Kept small
# so the scan stays O(n·window) rather than O(n²) over thousands of lines; after
# sorting, near-duplicates almost always sit close together.
FUZZY_WINDOW = 3
# Primary template-sibling signal. When two names differ in exactly one content
# token, that token is treated as an enumerated *series slot* — and the pair as
# distinct siblings, not duplicates — if at least this many concepts share the same
# blanked template (e.g. the 54 "CPMN fluxgate magnetometer at <code>" stations).
# Counting members is structural; it catches a series regardless of whether its
# codes happen to look alike, which the token-ratio fallback below cannot.
FAMILY_MIN = 3
# Fallback for *isolated* one-token-differing pairs (family below FAMILY_MIN, or
# single-token names with no shared template). The differing token pair must be at
# least this similar to count as a typo/near-duplicate; below it the word is a
# distinct identifier ("Narrow Angle"/"Wide Angle", "Rumba"/"Samba") — siblings,
# not duplicates, so they are not surfaced.
FUZZY_TOKEN_RATIO = 0.6

_PAREN_RE = re.compile(r"\(([^)]+)\)")

# Tokenizer for fuzzy de-noising: split a name into maximal runs of letters or
# digits, discarding punctuation and whitespace. Letter/digit boundaries split
# too, so "AVHRR-2" -> ["avhrr", "2"] and "Alouette1" -> ["alouette", "1"].
_TOKEN_RE = re.compile(r"[0-9]+|[^\W\d_]+", re.UNICODE)

# Roman numerals (I..XV) that mark a generation/series member, e.g. "ACRIM II".
# An explicit set keeps real words that happen to be roman-letters-only ("mix",
# "did", "mild") from being misread as enumerators.
_ROMAN = frozenset("i ii iii iv v vi vii viii ix x xi xii xiii xiv xv".split())


def _is_enum_token(token: str) -> bool:
    """True if ``token`` distinguishes a serial/series member rather than naming it.

    Pure digit runs ("11", "3"), single serial letters ("a", "b"), and small roman
    numerals ("ii", "iv") are all enumerators. Multi-letter words ("avhrr", "amer")
    are content, not enumerators.
    """
    return token.isdigit() or (len(token) == 1 and token.isalpha()) or token in _ROMAN


def _content_signature(text: str) -> tuple[str, ...]:
    """Return ``text``'s non-enumerator tokens, casefolded, in order.

    Two names with the *same* signature differ only in enumeration tokens — i.e.
    they are members of one serial/generation family (``BARREL 1A``/``BARREL 1B``,
    ``ACRIM I``/``ACRIM II``, ``Apollo 14 ...``/``Apollo 15 ...``) and are
    deliberately distinct entities, not near-duplicates to review.
    """
    tokens = _TOKEN_RE.findall(normalize.clean(text).casefold())
    return tuple(t for t in tokens if not _is_enum_token(t))


def _build_family_index(
    sigs: list[tuple[str, ...]],
) -> dict[tuple[str | None, ...], set[str]]:
    """Map each "blank one slot" template to the distinct values seen in that slot.

    For every signature and each token position ``p``, the template
    ``sig[:p] + (None,) + sig[p+1:]`` collects the values appearing at ``p`` across
    the whole corpus. The size of a template's value set is its *family size* — how
    many concepts are enumerated members of that one varying slot ("CPMN fluxgate
    magnetometer at <code>" → 54). Single-token signatures are skipped: their only
    template, ``(None,)``, would lump every one-word concept into one meaningless
    bucket.
    """
    index: dict[tuple[str | None, ...], set[str]] = {}
    for sig in sigs:
        if len(sig) < 2:
            continue
        for p, value in enumerate(sig):
            key = sig[:p] + (None,) + sig[p + 1 :]
            index.setdefault(key, set()).add(value)
    return index


def _is_template_sibling(
    sig_a: tuple[str, ...],
    sig_b: tuple[str, ...],
    family: dict[tuple[str | None, ...], set[str]],
) -> bool:
    """True if two names share a template but name *different* things, not typos.

    Triggers when the signatures are identical (they differ only in enumeration
    tokens — see :func:`_content_signature`), or when they differ in exactly one
    aligned token whose slot is either a large enumerated series (``family`` size
    >= :data:`FAMILY_MIN`) or, for an isolated pair, holds two values too dissimilar
    to be a misspelling. Either way the pair is a deliberate sibling, not a
    near-duplicate to merge.
    """
    if sig_a and sig_a == sig_b:
        return True
    if len(sig_a) != len(sig_b):
        return False
    diff = [p for p, (a, b) in enumerate(zip(sig_a, sig_b)) if a != b]
    if len(diff) != 1:
        return False
    p = diff[0]
    if len(sig_a) >= 2:
        key = sig_a[:p] + (None,) + sig_a[p + 1 :]
        if len(family.get(key, ())) >= FAMILY_MIN:
            return True  # one slot of a deliberately enumerated series
    a, b = sig_a[p], sig_b[p]
    return difflib.SequenceMatcher(None, a, b).ratio() < FUZZY_TOKEN_RATIO


@dataclass
class MergePair:
    """One applied merge: ``other`` was folded into ``primary``."""

    primary: str  # surviving canonical
    other: str  # canonical that was absorbed


@dataclass
class ConsolidationReport:
    dataset: str
    concepts_before: int = 0
    concepts_after: int = 0
    exact_merges: int = 0  # duplicates collapsed by the exact-key fold
    acronym_merges: list[MergePair] = field(default_factory=list)
    fuzzy_candidates: list[MergePair] = field(default_factory=list)
    ambiguous_acronyms: list[MergePair] = field(default_factory=list)

    @property
    def total_merged(self) -> int:
        return self.exact_merges + len(self.acronym_merges)


def _is_bare_token(alias: str) -> bool:
    """True if ``alias`` is a single whitespace-free token (e.g. ``Simbox``)."""
    return bool(alias.strip()) and not any(c.isspace() for c in alias.strip())


def _fold_concepts(concepts: list[Concept]) -> tuple[list[Concept], int]:
    """Fold a list of concepts by exact match key; return (folded, n_collapsed)."""
    wrapped = [SourceConcept(aliases=list(c.aliases)) for c in concepts]
    folded = merge_source_concepts(wrapped)
    result = [Concept(aliases=list(sc.aliases)) for sc in folded]
    return result, len(concepts) - len(result)


def _initial_fold(
    whitelist: Whitelist, source_concepts: list[SourceConcept]
) -> tuple[list[Concept], int]:
    """Fold existing + source concepts by exact key (existing canonical wins)."""
    existing = [
        SourceConcept(
            aliases=list(c.aliases),
            provenance=Provenance(origins=["whitelist"]),
        )
        for c in whitelist.concepts
        if c.keys()  # skip blank/placeholder lines
    ]
    inputs = existing + list(source_concepts)
    folded = merge_source_concepts(inputs)
    result = [Concept(aliases=list(sc.aliases)) for sc in folded]
    return result, len(inputs) - len(result)


def _bare_index(
    concepts: list[Concept], min_len: int = MIN_ACRONYM_LEN
) -> dict[str, Concept]:
    """Map acronym key -> concept whose *canonical* is solely that acronym.

    Keying on the canonical (not any alias) enforces "the other entry IS the
    acronym" rather than merely "contains it" — so "WIND Spacecraft;WIND" is not a
    merge target for a stray "(WIND)" parenthetical.
    """
    bare: dict[str, Concept] = {}
    for concept in concepts:
        key = normalize.match_key(concept.canonical)
        if len(key) >= min_len and _is_bare_token(concept.canonical):
            bare.setdefault(key, concept)
    return bare


def _self_is_acronym(concept: Concept) -> bool:
    return _is_bare_token(concept.canonical) and (
        len(normalize.match_key(concept.canonical)) >= MIN_ACRONYM_LEN
    )


def _acronym_merge(
    concepts: list[Concept], report: ConsolidationReport
) -> list[Concept]:
    """Fold each *descriptively-named* concept into the bare acronym it parenthesizes.

    Conservative by design: a concept that is itself a bare acronym (JADE, IKAR-P,
    COSPIN/AT) is a distinct named entity and is never swallowed just because its
    line happens to mention another acronym in parentheses — e.g. line 2448 wrongly
    lists "...(JEDI)" as an alias of JADE, which would otherwise fold JADE → JEDI.
    """
    bare = _bare_index(concepts)
    absorbed: set[int] = set()
    for concept in concepts:
        if id(concept) in absorbed or _self_is_acronym(concept):
            continue
        for alias in concept.aliases:
            for paren in _PAREN_RE.findall(alias):
                key = normalize.match_key(paren)
                if len(key) < MIN_ACRONYM_LEN:
                    continue
                target = bare.get(key)
                if target is None or target is concept or id(target) in absorbed:
                    continue
                for a in concept.aliases:
                    target.add_alias(a)
                report.acronym_merges.append(
                    MergePair(primary=target.canonical, other=concept.canonical)
                )
                absorbed.add(id(concept))
                break
            if id(concept) in absorbed:
                break

    return [c for c in concepts if id(c) not in absorbed]


def _find_ambiguous(concepts: list[Concept], report: ConsolidationReport) -> None:
    """Record parenthetical acronyms that *look* mergeable but were not merged.

    Run once on the final concept set so entries are not double-counted: a length-3
    acronym (below the auto threshold), or a parenthetical naming a bare acronym
    while the host entry is itself a distinct bare acronym.
    """
    bare = _bare_index(concepts, min_len=3)
    seen: set[tuple[str, str]] = set()
    for concept in concepts:
        for alias in concept.aliases:
            for paren in _PAREN_RE.findall(alias):
                key = normalize.match_key(paren)
                target = bare.get(key)
                if target is None or target is concept or len(key) < 3:
                    continue
                # Flag if the acronym is too short to auto-merge, or if the host
                # entry is itself a distinct bare acronym (the JADE/JEDI case).
                if len(key) < MIN_ACRONYM_LEN or _self_is_acronym(concept):
                    pair = (target.canonical, concept.canonical)
                    if pair not in seen:
                        seen.add(pair)
                        report.ambiguous_acronyms.append(
                            MergePair(primary=pair[0], other=pair[1])
                        )


def _sort_key(concept: Concept) -> str:
    canonical = concept.canonical
    return normalize.match_key(canonical) or canonical.casefold()


def _find_fuzzy(concepts: list[Concept]) -> list[MergePair]:
    """Surface near-duplicate canonicals among sorted neighbours (review only).

    Template siblings — names that differ only by an enumeration token or by a single
    unrelated identifier (``BARREL 1A``/``1B``, ``...at ADL``/``...at ANC``) — are
    deliberately distinct entities, not typos, and are skipped; otherwise they flood
    the list with false positives. See :func:`_is_template_sibling`.
    """
    keys = [normalize.match_key(c.canonical) for c in concepts]
    sigs = [_content_signature(c.canonical) for c in concepts]
    family = _build_family_index(sigs)
    pairs: list[MergePair] = []
    for i, ki in enumerate(keys):
        if not ki:
            continue
        for j in range(i + 1, min(i + 1 + FUZZY_WINDOW, len(concepts))):
            kj = keys[j]
            if not kj or ki == kj:
                continue
            if _is_template_sibling(sigs[i], sigs[j], family):
                continue
            if difflib.SequenceMatcher(None, ki, kj).ratio() >= FUZZY_RATIO:
                pairs.append(
                    MergePair(
                        primary=concepts[i].canonical,
                        other=concepts[j].canonical,
                    )
                )
    return pairs


def consolidate(
    whitelist: Whitelist, source_concepts: list[SourceConcept], dataset: str = ""
) -> tuple[Whitelist, ConsolidationReport]:
    """Rebuild ``whitelist`` (folded, acronym-merged, sorted) and report what changed.

    ``source_concepts`` may be empty for a pure self-consolidation pass. The input
    ``whitelist`` is not mutated.
    """
    report = ConsolidationReport(dataset=dataset)
    report.concepts_before = sum(1 for c in whitelist.concepts if c.keys())

    concepts, collapsed = _initial_fold(whitelist, source_concepts)
    report.exact_merges += collapsed

    # Acronym merges add aliases to the surviving concept, which can create *new*
    # exact-key collisions the initial fold already passed (e.g. a folded entry
    # contributes a "Gamma-ray Spectrometer" alias that now matches a standalone
    # "Gamma Ray Spectrometer"). Re-fold after each acronym pass and loop until the
    # result stops changing, so a single consolidate run is a fixed point.
    while True:
        merges_before = len(report.acronym_merges)
        concepts = _acronym_merge(concepts, report)
        concepts, collapsed = _fold_concepts(concepts)
        report.exact_merges += collapsed
        if len(report.acronym_merges) == merges_before and collapsed == 0:
            break

    _find_ambiguous(concepts, report)
    concepts.sort(key=_sort_key)

    report.fuzzy_candidates = _find_fuzzy(concepts)
    report.concepts_after = len(concepts)

    result = Whitelist(
        concepts=concepts,
        trailing_newline=True,
        path=whitelist.path,
    )
    return result, report
