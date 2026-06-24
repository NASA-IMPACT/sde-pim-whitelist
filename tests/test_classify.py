"""Tests for the OpenAI division classifier (no network: the client is stubbed)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from pim_whitelist import classify, config
from pim_whitelist.classify import ClassifiedRecord
from pim_whitelist.whitelist import Concept


class FakeClient:
    """Minimal stand-in for the OpenAI client.

    ``answers`` maps a concept's canonical name to the divisions the "model"
    should return. Names absent from the map come back with an empty list. The
    fake mirrors the real call path: ``client.chat.completions.create(...)`` and
    reads back the indexed items from the user message.
    """

    def __init__(self, answers: dict[str, list[str]]):
        self.answers = answers
        self.calls = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, *, model, messages, response_format, temperature):
        self.calls += 1
        items = json.loads(messages[1]["content"].split("\n\n", 1)[1])
        results = [
            {
                "index": item["index"],
                "divisions": self.answers.get(item["canonical"], []),
            }
            for item in items
        ]
        content = json.dumps({"results": results})
        message = SimpleNamespace(content=content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class _ApiError(Exception):
    """Stand-in for openai.APIStatusError (carries an HTTP ``status_code``)."""

    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


def _concepts(*names: str) -> list[Concept]:
    return [Concept(aliases=[n]) for n in names]


def test_classify_batch_drops_temperature_when_model_rejects_it():
    """gpt-5 / o-series reject temperature=0: drop it and retry, don't fail."""
    batch = _concepts("MODIS")
    seen_temperatures: list[bool] = []

    def create(*, model, messages, response_format, **kwargs):
        seen_temperatures.append("temperature" in kwargs)
        if "temperature" in kwargs:
            raise _ApiError(
                "Unsupported value: 'temperature' does not support 0 ...", 400
            )
        content = json.dumps({"results": [{"index": 0, "divisions": ["earth"]}]})
        message = SimpleNamespace(content=content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    out = classify.classify_batch(client, "gpt-5-mini", batch)
    assert out == {0: ["earth"]}
    assert seen_temperatures == [True, False]  # first with, then dropped


def test_classify_batch_fails_fast_on_permanent_400():
    """A non-temperature 400 is permanent — raise immediately, no retries."""
    batch = _concepts("MODIS")
    calls = 0

    def create(**kwargs):
        nonlocal calls
        calls += 1
        raise _ApiError("Invalid 'response_format'", 400)

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    with pytest.raises(classify.ClassifierError):
        classify.classify_batch(client, "fake", batch)
    assert calls == 1  # no backoff retries on a permanent error


def test_valid_divisions_drops_unknown_and_orders_canonically():
    assert classify._valid_divisions(["bps", "mars", "earth", "earth"]) == [
        "earth",
        "bps",
    ]


def test_classify_batch_maps_by_index_and_filters_invalid():
    batch = _concepts("MODIS", "Hubble", "01620")
    client = FakeClient({"MODIS": ["earth", "bogus"], "Hubble": ["astrophysics"]})
    out = classify.classify_batch(client, "fake-model", batch)
    assert out == {0: ["earth"], 1: ["astrophysics"], 2: []}


def test_classify_concepts_returns_keyed_by_match_key():
    concepts = _concepts("MODIS", "Hubble Space Telescope")
    client = FakeClient(
        {"MODIS": ["earth"], "Hubble Space Telescope": ["astrophysics"]}
    )
    out = classify.classify_concepts(
        concepts, model="fake", batch_size=1, workers=2, client=client
    )
    assert out == {"modis": ["earth"], "hubblespacetelescope": ["astrophysics"]}
    assert client.calls == 2  # one per batch of size 1


def _dataset(tmp_path, names):
    wl = tmp_path / "list.txt"
    wl.write_text("\n".join(names) + "\n", encoding="utf-8")
    return config.DatasetConfig(name="instruments", whitelist_filename="list.txt"), wl


@pytest.fixture
def classified_dir(tmp_path, monkeypatch):
    """Isolate the classified output dir and the raw provenance cache.

    Pointing RAW_CACHE_DIR at an empty temp dir means provenance is empty by
    default, so concepts fall through to the (stubbed) OpenAI path unless a test
    writes its own cache file via ``write_provenance``.
    """
    d = tmp_path / "classified"
    monkeypatch.setattr(config, "CLASSIFIED_DIR", d)
    monkeypatch.setattr(config, "RAW_CACHE_DIR", tmp_path / "raw")
    return d


def write_provenance(rows):
    """Write an SDE instruments cache ({value, collection_key} rows)."""
    raw = config.RAW_CACHE_DIR
    raw.mkdir(parents=True, exist_ok=True)
    (raw / config.SDE_CACHE_FILENAMES["instruments"]).write_text(
        json.dumps(rows), encoding="utf-8"
    )


def test_classify_dataset_full_writes_sorted_openai_records(
    tmp_path, monkeypatch, classified_dir
):
    ds, wl = _dataset(tmp_path, ["MODIS", "Hubble"])
    monkeypatch.setattr(
        config.DatasetConfig, "whitelist_path", property(lambda self: wl)
    )
    client = FakeClient({"MODIS": ["earth"], "Hubble": ["astrophysics"]})

    stats = classify.classify_dataset(ds, model="fake", client=client)

    records = json.loads((classified_dir / "instruments.json").read_text())
    # Sorted by match_key: "hubble" < "modis".
    assert [r["canonical"] for r in records] == ["Hubble", "MODIS"]
    assert all(r["division_source"] == "openai" for r in records)
    assert stats == {
        "records": 2,
        "reused": 0,
        "provenance": 0,
        "classified": 2,
        "to_classify": 2,
        "empty": 0,
    }


def test_provenance_takes_priority_over_openai(tmp_path, monkeypatch, classified_dir):
    ds, wl = _dataset(tmp_path, ["MODIS", "GCMD-only Instrument"])
    monkeypatch.setattr(
        config.DatasetConfig, "whitelist_path", property(lambda self: wl)
    )
    # MODIS has provenance (Earth); the GCMD-only name does not.
    write_provenance([{"value": "MODIS", "collection_key": "CMR_API"}])
    client = FakeClient({"GCMD-only Instrument": ["heliophysics"]})

    stats = classify.classify_dataset(ds, model="fake", client=client)

    records = {
        r["match_key"]: r
        for r in json.loads((classified_dir / "instruments.json").read_text())
    }
    assert records["modis"]["divisions"] == ["earth"]
    assert records["modis"]["division_source"] == "provenance"
    assert records["gcmdonlyinstrument"]["division_source"] == "openai"
    assert records["gcmdonlyinstrument"]["divisions"] == ["heliophysics"]
    # Only the non-provenance concept was sent to OpenAI.
    assert client.calls == 1
    assert stats["provenance"] == 1
    assert stats["classified"] == 1
    assert stats["reused"] == 0


def test_dry_run_writes_nothing(tmp_path, monkeypatch, classified_dir):
    ds, wl = _dataset(tmp_path, ["MODIS"])
    monkeypatch.setattr(
        config.DatasetConfig, "whitelist_path", property(lambda self: wl)
    )
    client = FakeClient({"MODIS": ["earth"]})

    stats = classify.classify_dataset(ds, model="fake", client=client, dry_run=True)
    assert not (classified_dir / "instruments.json").exists()
    assert client.calls == 0  # dry run never calls OpenAI
    assert stats["to_classify"] == 1
    assert stats["classified"] == 0


def test_limit_caps_classified_count(tmp_path, monkeypatch, classified_dir):
    ds, wl = _dataset(tmp_path, ["MODIS", "Hubble", "Voyager"])
    monkeypatch.setattr(
        config.DatasetConfig, "whitelist_path", property(lambda self: wl)
    )
    client = FakeClient({})

    stats = classify.classify_dataset(ds, model="fake", client=client, limit=1)
    assert stats["classified"] == 1
    assert stats["records"] == 1


def write_classified(classified_dir, records):
    """Seed the dataset's classified JSON (the persistent cache)."""
    classified_dir.mkdir(parents=True, exist_ok=True)
    (classified_dir / "instruments.json").write_text(json.dumps(records))


def _record(match_key, divisions, source):
    return {
        "match_key": match_key,
        "canonical": match_key.upper(),
        "aliases": [match_key.upper()],
        "divisions": divisions,
        "division_source": source,
    }


def test_resolved_records_are_reused_and_new_ones_appended(
    tmp_path, monkeypatch, classified_dir
):
    # MODIS is already resolved (even via provenance — provenance is cached too).
    write_classified(classified_dir, [_record("modis", ["earth"], "provenance")])
    ds, wl = _dataset(tmp_path, ["MODIS", "Hubble"])
    monkeypatch.setattr(
        config.DatasetConfig, "whitelist_path", property(lambda self: wl)
    )
    client = FakeClient({"Hubble": ["astrophysics"]})

    stats = classify.classify_dataset(ds, model="fake", client=client)

    by_key = {
        r["match_key"]: r
        for r in json.loads((classified_dir / "instruments.json").read_text())
    }
    # MODIS reused verbatim (source preserved) and NOT sent to OpenAI.
    assert by_key["modis"]["divisions"] == ["earth"]
    assert by_key["modis"]["division_source"] == "provenance"
    assert by_key["hubble"]["division_source"] == "openai"
    assert client.calls == 1  # only Hubble
    assert stats == {
        "records": 2,
        "reused": 1,
        "provenance": 0,
        "classified": 1,
        "to_classify": 1,
        "empty": 0,
    }


def test_empty_cached_record_is_retried(tmp_path, monkeypatch, classified_dir):
    # An empty result is not "resolved" — it must be retried each run.
    write_classified(classified_dir, [_record("modis", [], "openai")])
    ds, wl = _dataset(tmp_path, ["MODIS"])
    monkeypatch.setattr(
        config.DatasetConfig, "whitelist_path", property(lambda self: wl)
    )
    client = FakeClient({"MODIS": ["earth"]})

    stats = classify.classify_dataset(ds, model="fake", client=client)

    records = json.loads((classified_dir / "instruments.json").read_text())
    assert records[0]["divisions"] == ["earth"]
    assert client.calls == 1
    assert stats["reused"] == 0
    assert stats["classified"] == 1


def test_removed_concept_is_pruned(tmp_path, monkeypatch, classified_dir):
    write_classified(
        classified_dir,
        [
            _record("modis", ["earth"], "openai"),
            _record("gone", ["heliophysics"], "openai"),
        ],
    )
    ds, wl = _dataset(tmp_path, ["MODIS"])  # "GONE" no longer in the whitelist
    monkeypatch.setattr(
        config.DatasetConfig, "whitelist_path", property(lambda self: wl)
    )
    client = FakeClient({})

    classify.classify_dataset(ds, model="fake", client=client)

    keys = {
        r["match_key"]
        for r in json.loads((classified_dir / "instruments.json").read_text())
    }
    assert keys == {"modis"}
    assert client.calls == 0  # MODIS reused, nothing to classify


def test_reclassify_ignores_cache(tmp_path, monkeypatch, classified_dir):
    write_classified(classified_dir, [_record("modis", ["earth"], "openai")])
    ds, wl = _dataset(tmp_path, ["MODIS"])
    monkeypatch.setattr(
        config.DatasetConfig, "whitelist_path", property(lambda self: wl)
    )
    client = FakeClient({"MODIS": ["planetary"]})

    stats = classify.classify_dataset(ds, model="fake", client=client, reclassify=True)

    records = json.loads((classified_dir / "instruments.json").read_text())
    assert records[0]["divisions"] == ["planetary"]  # re-resolved, not the cached earth
    assert client.calls == 1
    assert stats["reused"] == 0


def test_resolve_model_precedence(monkeypatch):
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    assert classify.resolve_model("explicit") == "explicit"
    monkeypatch.setenv("OPENAI_MODEL", "from-env")
    assert classify.resolve_model(None) == "from-env"
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    assert classify.resolve_model(None) == config.DEFAULT_OPENAI_MODEL


def test_require_api_key_raises_when_missing(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(classify.ClassifierError):
        classify.require_api_key()


def test_classified_record_round_trips_to_same_fields():
    data = {
        "match_key": "modis",
        "canonical": "MODIS",
        "aliases": ["MODIS"],
        "divisions": ["earth"],
        "division_source": "provenance",
    }
    assert ClassifiedRecord.model_validate(data).model_dump() == data


def test_classified_record_rejects_invalid_division_source():
    with pytest.raises(ValidationError):
        ClassifiedRecord(
            match_key="modis",
            canonical="MODIS",
            aliases=["MODIS"],
            divisions=["earth"],
            division_source="guessed",  # not provenance/openai
        )
