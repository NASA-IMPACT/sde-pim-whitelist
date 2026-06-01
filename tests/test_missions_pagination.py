import json

from pim_whitelist.config import MISSIONS
from pim_whitelist.sources.missions import MissionsSource


class _FakeResp:
    def __init__(self, payload, headers=None):
        self._payload = payload
        self.headers = headers or {}

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeSession:
    """Returns scripted batches per page; records calls.

    Page sequence models the real endpoint: a full page, a transiently-empty
    page that fills on retry, a genuinely-empty page, then a final page.
    """

    def __init__(self):
        self.calls = []
        self._page_batches = {
            1: [[{"id": 1, "title": {"rendered": "Alpha"}}]],
            2: [[], [{"id": 2, "title": {"rendered": "Beta"}}]],  # empty, then fills
            3: [[], [], []],  # genuinely empty across all retries
            4: [[{"id": 1, "title": {"rendered": "Alpha"}},  # dup id -> deduped
                 {"id": 4, "title": {"rendered": "Delta"}}]],
        }
        self._attempt = {}

    def get(self, url, params=None, timeout=None):
        page = params["page"]
        self.calls.append((page, params.get("orderby"), params.get("order")))
        attempts = self._page_batches[page]
        i = min(self._attempt.get(page, 0), len(attempts) - 1)
        self._attempt[page] = self._attempt.get(page, 0) + 1
        headers = {"X-WP-TotalPages": "4"} if page == 1 else {}
        return _FakeResp(attempts[i], headers)


def test_pagination_dedupes_retries_and_uses_stable_order(monkeypatch):
    # Make retry sleeps instant.
    monkeypatch.setattr("pim_whitelist.sources.missions.time.sleep", lambda *_: None)
    src = MissionsSource(MISSIONS)
    raw = src.fetch_raw(_FakeSession())
    posts = json.loads(raw)
    ids = [p["id"] for p in posts]
    # All 4 pages visited; dup id collapsed; empty page 3 tolerated.
    assert ids == [1, 2, 4]
    concepts = src.parse(raw)
    assert [c.canonical for c in concepts] == ["Alpha", "Beta", "Delta"]


def test_pagination_requests_orderby_id():
    session = _FakeSession()
    src = MissionsSource(MISSIONS)
    # Avoid real sleeps by ensuring no empty-retry path needs them here is fine;
    # page 2/3 will sleep, so patch via a session with no empties instead.
    session._page_batches = {1: [[{"id": 1, "title": {"rendered": "A"}}]]}
    session._page_batches[1] = [[{"id": 1, "title": {"rendered": "A"}}]]
    # Force single page.

    class _OnePage(_FakeSession):
        def get(self, url, params=None, timeout=None):
            self.calls.append((params["page"], params.get("orderby"), params.get("order")))
            return _FakeResp([{"id": 1, "title": {"rendered": "A"}}], {"X-WP-TotalPages": "1"})

    s = _OnePage()
    src.fetch_raw(s)
    assert s.calls == [(1, "id", "asc")]
