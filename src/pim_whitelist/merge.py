"""Apply a :class:`~pim_whitelist.diff.Delta` to a whitelist.

The merge only ever *adds*: new aliases are appended to their existing concept
(order preserved) and new concepts are appended to the end of the file. Nothing
is reordered or removed, so a ``git diff`` of the result shows additions only.
"""

from __future__ import annotations

import copy

from .diff import Delta
from .whitelist import Concept, Whitelist


def apply_delta(whitelist: Whitelist, delta: Delta) -> Whitelist:
    """Return a new Whitelist with ``delta`` applied; the input is untouched."""
    merged = copy.deepcopy(whitelist)

    # Map identity of original concepts to their copies so new aliases land on
    # the right (copied) concept.
    by_original = {id(orig): clone for orig, clone in zip(whitelist.concepts, merged.concepts)}

    for new_alias in delta.new_aliases:
        target = by_original.get(id(new_alias.concept))
        if target is None:
            continue
        for alias in new_alias.aliases:
            target.add_alias(alias)

    for source in delta.new_concepts:
        merged.concepts.append(Concept(aliases=list(source.aliases)))

    # A file that previously had no trailing newline gains one once we start
    # appending lines, so the final concept isn't left mid-line.
    if delta.new_concepts:
        merged.trailing_newline = True

    return merged
