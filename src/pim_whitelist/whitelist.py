"""Model and (de)serialization for the ``;``-delimited whitelist files.

Each line is one concept; aliases are separated by ``;``; the first alias is the
canonical/preferred name. The parser preserves alias substrings verbatim so that
parsing a file and serializing it back yields byte-identical output (see the
round-trip test). Per-file trailing-newline state is preserved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .normalize import match_key


@dataclass
class Concept:
    """An ordered group of aliases for a single concept.

    ``aliases[0]`` is the canonical name. Alias strings are stored verbatim
    (no stripping) to guarantee round-trip fidelity for existing files.
    """

    aliases: list[str] = field(default_factory=list)

    @property
    def canonical(self) -> str:
        return self.aliases[0] if self.aliases else ""

    @property
    def text(self) -> str:
        """The serialized line for this concept."""
        return ";".join(self.aliases)

    def keys(self) -> set[str]:
        """Non-empty match keys across all aliases."""
        return {k for k in (match_key(a) for a in self.aliases) if k}

    def has_key(self, key: str) -> bool:
        return key in self.keys()

    def add_alias(self, alias: str) -> bool:
        """Append ``alias`` if its match key is not already present.

        Returns ``True`` if the alias was added.
        """
        key = match_key(alias)
        if not key or key in self.keys():
            return False
        self.aliases.append(alias)
        return True


@dataclass
class Whitelist:
    """An ordered list of concepts plus the file's trailing-newline state."""

    concepts: list[Concept] = field(default_factory=list)
    trailing_newline: bool = True
    path: Path | None = None

    @classmethod
    def parse(cls, text: str, path: Path | None = None) -> "Whitelist":
        trailing_newline = text.endswith("\n")
        body = text[:-1] if trailing_newline else text
        # An empty file has no concepts; "".split("\n") would yield [""].
        lines = body.split("\n") if body != "" else []
        concepts = [Concept(aliases=line.split(";")) for line in lines]
        return cls(concepts=concepts, trailing_newline=trailing_newline, path=path)

    @classmethod
    def load(cls, path: str | Path) -> "Whitelist":
        path = Path(path)
        return cls.parse(path.read_text(encoding="utf-8"), path=path)

    def serialize(self) -> str:
        text = "\n".join(c.text for c in self.concepts)
        if self.trailing_newline and text:
            text += "\n"
        elif self.trailing_newline and not text:
            text = "\n"
        return text

    def save(self, path: str | Path | None = None) -> None:
        target = Path(path) if path is not None else self.path
        if target is None:
            raise ValueError("no path to save to")
        target.write_text(self.serialize(), encoding="utf-8")

    def build_index(self) -> dict[str, Concept]:
        """Map every alias match key to its owning concept.

        If two concepts share a key (rare; would indicate a duplicate in the
        source file), the first one wins.
        """
        index: dict[str, Concept] = {}
        for concept in self.concepts:
            for key in concept.keys():
                index.setdefault(key, concept)
        return index
