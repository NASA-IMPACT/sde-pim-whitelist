"""Shared parsing for the GCMD CSV sources (platforms, instruments).

Both files share the same shape: a one-row metadata banner, then a header row
containing ``Short_Name``/``Long_Name``/``UUID`` columns, then data rows. Rows
that are hierarchy nodes have an empty ``Short_Name`` and are skipped. A concept
is ``Short_Name`` (canonical) plus ``Long_Name`` (alias).
"""

from __future__ import annotations

import csv
import io

import requests

from .base import Source, SourceConcept, make_concept

SHORT_NAME = "Short_Name"
LONG_NAME = "Long_Name"
UUID = "UUID"


def parse_gcmd_csv(raw: bytes) -> list[SourceConcept]:
    text = raw.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)

    # Find the header row: the first row that contains the Short_Name column.
    header_idx = None
    for i, row in enumerate(rows):
        if SHORT_NAME in row:
            header_idx = i
            break
    if header_idx is None:
        raise ValueError(f"could not locate '{SHORT_NAME}' header row in CSV")

    header = rows[header_idx]
    short_i = header.index(SHORT_NAME)
    long_i = header.index(LONG_NAME)
    uuid_i = header.index(UUID) if UUID in header else None

    concepts: list[SourceConcept] = []
    for row in rows[header_idx + 1 :]:
        if len(row) <= short_i:
            continue
        short_name = row[short_i].strip()
        if not short_name:
            # Hierarchy node (category/class/etc.), not a real concept.
            continue
        long_name = row[long_i].strip() if len(row) > long_i else ""
        provenance: dict = {"origins": ["gcmd"]}
        if uuid_i is not None and len(row) > uuid_i:
            uuid = row[uuid_i].strip()
            if uuid:
                provenance["uuid"] = uuid
        concept = make_concept([short_name, long_name], provenance)
        if concept is not None:
            concepts.append(concept)
    return concepts


class CsvSource(Source):
    """A GCMD CSV source identified by its ``url`` class attribute."""

    url: str = ""

    def fetch_raw(self, session: requests.Session) -> bytes:
        resp = session.get(self.url, timeout=120)
        resp.raise_for_status()
        return resp.content

    def parse(self, raw: bytes) -> list[SourceConcept]:
        return parse_gcmd_csv(raw)
