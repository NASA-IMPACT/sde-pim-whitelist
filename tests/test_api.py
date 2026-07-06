"""Tests for the read API.

Runs against a small, hand-built index injected via ``dependency_overrides`` so
the assertions are deterministic and independent of the on-disk sidecars.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from pim_whitelist.api.app import app
from pim_whitelist.api.deps import get_index
from pim_whitelist.api.index import PimIndex
from pim_whitelist.api.models import PimRecord, PimType
from pim_whitelist.classify import ClassifiedRecord


def _rec(canonical, divisions, source, pim_type):
    """Build a PimRecord through the real from_classified projection."""
    cr = ClassifiedRecord(
        match_key=canonical.lower().replace(" ", ""),
        canonical=canonical,
        aliases=[canonical],
        divisions=divisions,
        division_source=source,
    )
    return PimRecord.from_classified(cr, pim_type)


def _index(by_type):
    return PimIndex(by_type=by_type, data_version="testv1")


def _client(index):
    app.dependency_overrides[get_index] = lambda: index
    client = TestClient(app)
    return client


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def small_index():
    instruments = [
        _rec("MISR", ["earth", "planetary"], "provenance", PimType.instrument),
        _rec("ACME Sounder", ["earth"], "openai", PimType.instrument),
        _rec("Opaque XYZ", [], "openai", PimType.instrument),  # no divisions
    ]
    platforms = [_rec("Terra", ["earth"], "provenance", PimType.platform)]
    missions = [_rec("Apollo 11", ["planetary"], "openai", PimType.mission)]
    return _index(
        {
            PimType.instrument: instruments,
            PimType.platform: platforms,
            PimType.mission: missions,
        }
    )


def test_instruments_envelope_and_record_shape(small_index):
    r = _client(small_index).get("/fetch_pims_records/instruments")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"items", "page", "page_size", "total", "total_pages"}
    assert body["total"] == 3
    item = body["items"][0]
    assert set(item) == {"canonical_name", "aliases", "division", "source", "type"}
    assert item["type"] == "instrument"


def test_source_mapping(small_index):
    items = _client(small_index).get("/fetch_pims_records/instruments").json()["items"]
    by_name = {i["canonical_name"]: i for i in items}
    assert by_name["MISR"]["source"] == "SDE"  # provenance
    assert by_name["ACME Sounder"]["source"] == "LLM"  # openai/LLM classifier


def test_division_filter_includes_multi_and_excludes_empty(small_index):
    client = _client(small_index)
    earth = client.get("/fetch_pims_records/instruments?division=earth").json()
    names = {i["canonical_name"] for i in earth["items"]}
    assert names == {"MISR", "ACME Sounder"}  # MISR is multi-division; Opaque excluded
    assert earth["total"] == 2

    planetary = client.get("/fetch_pims_records/instruments?division=planetary").json()
    assert {i["canonical_name"] for i in planetary["items"]} == {"MISR"}


def test_bad_division_returns_422(small_index):
    r = _client(small_index).get("/fetch_pims_records/instruments?division=bogus")
    assert r.status_code == 422


@pytest.mark.parametrize("page_size", [10, 49, 101, 500])
def test_page_size_out_of_bounds_422(small_index, page_size):
    r = _client(small_index).get(
        f"/fetch_pims_records/instruments?page_size={page_size}"
    )
    assert r.status_code == 422


def test_page_below_one_422(small_index):
    r = _client(small_index).get("/fetch_pims_records/instruments?page=0")
    assert r.status_code == 422


def test_pagination_slices_and_page_count():
    many = [
        _rec(f"Inst {n:03d}", ["earth"], "openai", PimType.instrument)
        for n in range(120)
    ]
    index = _index(
        {PimType.instrument: many, PimType.platform: [], PimType.mission: []}
    )
    client = _client(index)

    p1 = client.get("/fetch_pims_records/instruments?page=1&page_size=50").json()
    assert p1["total"] == 120 and p1["total_pages"] == 3 and len(p1["items"]) == 50
    assert p1["items"][0]["canonical_name"] == "Inst 000"

    p3 = client.get("/fetch_pims_records/instruments?page=3&page_size=50").json()
    assert len(p3["items"]) == 20  # 120 - 100
    assert p3["items"][-1]["canonical_name"] == "Inst 119"

    # Page beyond the end: empty items, correct total.
    p9 = client.get("/fetch_pims_records/instruments?page=9&page_size=50").json()
    assert p9["items"] == [] and p9["total"] == 120


def test_base_endpoint_is_flat_and_typed(small_index):
    body = _client(small_index).get("/fetch_pims_records?page_size=100").json()
    assert body["total"] == 5  # 3 instruments + 1 platform + 1 mission
    assert {i["type"] for i in body["items"]} == {
        "instrument",
        "platform",
        "mission",
    }


def test_base_endpoint_division_filter(small_index):
    body = (
        _client(small_index)
        .get("/fetch_pims_records?division=planetary&page_size=100")
        .json()
    )
    assert {i["canonical_name"] for i in body["items"]} == {"MISR", "Apollo 11"}


def test_etag_and_304(small_index):
    client = _client(small_index)
    r = client.get("/fetch_pims_records/missions")
    etag = r.headers["etag"]
    assert r.headers["cache-control"].startswith("public, max-age=")
    r2 = client.get("/fetch_pims_records/missions", headers={"If-None-Match": etag})
    assert r2.status_code == 304


def test_healthz_ok(small_index):
    r = _client(small_index).get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_data_dir_uses_lambda_task_root(monkeypatch, tmp_path):
    # In Lambda the data resolves under LAMBDA_TASK_ROOT, NOT config.REPO_ROOT
    # (whose __file__-relative path points at /var inside the zip).
    from pim_whitelist.api import settings as s

    monkeypatch.setenv("LAMBDA_TASK_ROOT", str(tmp_path))
    assert s._default_data_dir() == tmp_path / "whitelist" / "classified"


def test_data_dir_falls_back_to_repo_root_off_lambda(monkeypatch):
    from pim_whitelist import config
    from pim_whitelist.api import settings as s

    monkeypatch.delenv("LAMBDA_TASK_ROOT", raising=False)
    assert s._default_data_dir() == config.REPO_ROOT / "whitelist" / "classified"


def test_healthz_unhealthy_when_a_dataset_empty():
    index = _index(
        {
            PimType.instrument: [
                _rec("MISR", ["earth"], "provenance", PimType.instrument)
            ],
            PimType.platform: [],  # empty -> not ready
            PimType.mission: [],
        }
    )
    r = _client(index).get("/healthz")
    assert r.status_code == 503
    assert r.json()["status"] == "unhealthy"
