"""Text-processing core.

Two distinct operations, deliberately kept separate:

- :func:`clean` produces the *stored* form of a string. It only fixes encoding
  artefacts (HTML entities, odd Unicode forms, stray whitespace). It never
  changes case or strips punctuation, because the whitelists deliberately keep
  case and punctuation variants as separate aliases.

- :func:`match_key` produces a *comparison-only* key. It is aggressive
  (casefold + drop every non-alphanumeric) so that "Multi-Angle Imaging
  SpectroRadiometer" and "Multi angle imaging spectroradiometer" collapse to the
  same key for dedup/matching. The key is never written to a file.
"""

from __future__ import annotations

import html
import re
import unicodedata

# Curly quotes / dashes that frequently differ between sources are folded to
# their ASCII equivalents *before* taking a match key so that, e.g., a curly
# apostrophe and a straight one compare equal. This map is intentionally small.
_UNICODE_FOLD = str.maketrans(
    {
        "‘": "'",  # left single quote
        "’": "'",  # right single quote
        "“": '"',  # left double quote
        "”": '"',  # right double quote
        "–": "-",  # en dash
        "—": "-",  # em dash
        "−": "-",  # minus sign
        " ": " ",  # non-breaking space
    }
)

_WHITESPACE_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def clean(text: str) -> str:
    """Return the canonical *stored* form of ``text``.

    Unescapes HTML entities, applies Unicode NFKC normalization, collapses runs
    of whitespace to a single space, and strips leading/trailing whitespace.
    Case and punctuation are preserved.
    """
    if text is None:
        return ""
    out = html.unescape(text)
    out = unicodedata.normalize("NFKC", out)
    out = _WHITESPACE_RE.sub(" ", out)
    return out.strip()


def match_key(text: str) -> str:
    """Return a comparison-only key for ``text``.

    Cleans the text, folds common Unicode punctuation to ASCII, casefolds, and
    removes every non-alphanumeric character. Two strings that differ only by
    case, spacing, or punctuation produce the same key. Returns ``""`` for input
    that has no alphanumeric content.
    """
    cleaned = clean(text).translate(_UNICODE_FOLD)
    folded = cleaned.casefold()
    folded = unicodedata.normalize("NFKD", folded)
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return _NON_ALNUM_RE.sub("", folded)
