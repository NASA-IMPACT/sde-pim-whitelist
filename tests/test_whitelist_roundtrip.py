import pytest

from pim_whitelist.config import DATASETS
from pim_whitelist.whitelist import Whitelist


@pytest.mark.parametrize("dataset", DATASETS.values(), ids=lambda d: d.name)
def test_roundtrip_byte_identical(dataset):
    """Parsing then serializing an unchanged whitelist must be byte-identical."""
    original = dataset.whitelist_path.read_text(encoding="utf-8")
    wl = Whitelist.parse(original)
    assert wl.serialize() == original


def test_parse_canonical_and_aliases():
    wl = Whitelist.parse(
        "MISR;Multi-Angle Imaging SpectroRadiometer\nABI;Advanced Baseline Imager"
    )
    assert len(wl.concepts) == 2
    assert wl.concepts[0].canonical == "MISR"
    assert wl.concepts[0].aliases == ["MISR", "Multi-Angle Imaging SpectroRadiometer"]


def test_index_maps_all_aliases():
    wl = Whitelist.parse("ACC;ACCELEROMETER;Accelerometer")
    index = wl.build_index()
    # All case variants collapse to the same key -> same concept.
    assert index["acc"] is wl.concepts[0]
    assert index["accelerometer"] is wl.concepts[0]


def test_add_alias_dedups_by_key():
    wl = Whitelist.parse("MISR;Multi-Angle Imaging SpectroRadiometer")
    concept = wl.concepts[0]
    # Same key (different spacing/case) -> not added.
    assert concept.add_alias("multi angle imaging spectroradiometer") is False
    # New key -> added.
    assert concept.add_alias("MISR Camera") is True
    assert "MISR Camera" in concept.aliases


def test_roundtrip_no_trailing_newline_preserved():
    text = "A;Alpha\nB;Beta"  # no trailing newline
    wl = Whitelist.parse(text)
    assert wl.trailing_newline is False
    assert wl.serialize() == text
