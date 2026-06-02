"""Missions source: NASA WordPress REST API (paginated).

The API returns one title per mission and no aliases. We fetch every page
(following the ``X-WP-TotalPages`` response header rather than hardcoding a
count), dedupe by post ``id``, and cache the combined list of raw post objects
as a single JSON array so the cache round-trips through the same parser.
"""

from __future__ import annotations

import json
import time

import requests

from ..config import MISSIONS_PER_PAGE, MISSIONS_URL
from .base import Provenance, Source, SourceConcept, make_concept

# Empty-page retries. The nasa.gov CDN occasionally serves an empty 200 for a
# page that has content; a couple of retries smooths that over. Some pages are
# *genuinely* empty (sparse ids), so we accept empty after the retries rather
# than aborting pagination.
_EMPTY_PAGE_RETRIES = 3
_RETRY_SLEEP_SECONDS = 1.0


class MissionsSource(Source):
    cache_filename = "missions.json"

    def fetch_raw(self, session: requests.Session) -> bytes:
        posts: dict[int, dict] = {}
        page = 1
        total_pages = 1
        while page <= total_pages:
            batch: list[dict] = []
            for attempt in range(_EMPTY_PAGE_RETRIES):
                resp = session.get(
                    MISSIONS_URL,
                    # orderby=id keeps pagination stable; without it the CDN
                    # returns inconsistent (often empty) pages.
                    params={
                        "per_page": MISSIONS_PER_PAGE,
                        "page": page,
                        "orderby": "id",
                        "order": "asc",
                    },
                    timeout=120,
                )
                resp.raise_for_status()
                if page == 1:
                    total_pages = int(resp.headers.get("X-WP-TotalPages", "1"))
                batch = resp.json()
                if batch:
                    break
                if attempt < _EMPTY_PAGE_RETRIES - 1:
                    time.sleep(_RETRY_SLEEP_SECONDS)
            for post in batch:
                posts[post["id"]] = post
            page += 1
        # Stable order by id keeps the cache deterministic.
        combined = [posts[i] for i in sorted(posts)]
        return json.dumps(combined, ensure_ascii=False).encode("utf-8")

    def parse(self, raw: bytes) -> list[SourceConcept]:
        posts = json.loads(raw.decode("utf-8"))
        concepts: list[SourceConcept] = []
        for post in posts:
            title = (post.get("title") or {}).get("rendered", "")
            provenance = Provenance(
                origins=["missions"],
                extra={
                    "id": post.get("id"),
                    "slug": post.get("slug"),
                    "mission_type": post.get("mission-type"),
                    "mission_status": post.get("mission-status"),
                    "link": post.get("link"),
                },
            )
            concept = make_concept([title], provenance)
            if concept is not None:
                concepts.append(concept)
        return concepts
