from pim_whitelist.config import INSTRUMENTS, PLATFORMS
from pim_whitelist.sources.sde import (
    SdeInstrumentsSource,
    SdePlatformsSource,
    _reset_crawl_cache,
    crawl,
)


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeSession:
    """Scripted SDE search responses keyed by collection_key.

    ``script[collection_key]`` is a list of pages; each page is a list of
    document dicts. Collection keys absent from the script return a single
    empty page.
    """

    def __init__(self, script):
        self.script = script
        self.calls = []

    def post(self, url, json=None, timeout=None):
        ck = json["filters"]["collection_key"][0]
        page = json["page"]
        self.calls.append((ck, page))
        pages = self.script.get(ck, [[]])
        docs = pages[page - 1] if page - 1 < len(pages) else []
        return _FakeResp({"pagination": {"total_pages": len(pages)}, "documents": docs})


def test_crawl_paginates_filters_and_splits():
    _reset_crawl_cache()
    script = {
        "CMR_API": [
            [
                {"platform": "Terra", "instrument": "MODIS;ASTER"},
                {"platform": "Not provided", "instrument": "[]"},  # junk -> dropped
            ],
            [{"platform": "Aqua", "instrument": "MODIS"}],
        ]
    }
    out = crawl(_FakeSession(script))

    assert [r["value"] for r in out["platform"]] == ["Terra", "Aqua"]
    # ';' split into two; junk dropped; cross-doc dupes kept (deduped later).
    assert [r["value"] for r in out["instrument"]] == ["MODIS", "ASTER", "MODIS"]
    assert out["platform"][0]["collection_key"] == "CMR_API"


def test_sources_share_one_crawl_and_tag_origin():
    _reset_crawl_cache()
    script = {"CMR_API": [[{"platform": "Terra", "instrument": "MODIS"}]]}
    s1 = _FakeSession(script)
    s2 = _FakeSession(script)

    praw = SdePlatformsSource(PLATFORMS).fetch_raw(s1)
    iraw = SdeInstrumentsSource(INSTRUMENTS).fetch_raw(s2)

    # The first source triggers the crawl; the second reuses the memo.
    assert s1.calls
    assert s2.calls == []

    pconcepts = SdePlatformsSource(PLATFORMS).parse(praw)
    iconcepts = SdeInstrumentsSource(INSTRUMENTS).parse(iraw)
    assert [c.canonical for c in pconcepts] == ["Terra"]
    assert [c.canonical for c in iconcepts] == ["MODIS"]
    assert pconcepts[0].provenance["origins"] == ["sde"]
    assert pconcepts[0].provenance["collection_keys"] == ["CMR_API"]
    _reset_crawl_cache()
