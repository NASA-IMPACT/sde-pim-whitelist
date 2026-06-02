from pim_whitelist.diff import compute_delta, merge_source_concepts
from pim_whitelist.merge import apply_delta
from pim_whitelist.sources.base import Provenance, make_concept
from pim_whitelist.whitelist import Whitelist


def _wl():
    return Whitelist.parse(
        "MISR;Multi-Angle Imaging SpectroRadiometer\n"
        "ABI;Advanced Baseline Imager\n"
        "RetiredThing;Old Platform"
    )


def test_delta_classifies_new_concept_alias_and_orphan():
    wl = _wl()
    source = [
        # matches MISR by short name, contributes a new alias spelling
        make_concept(["MISR", "Terra MISR Camera"]),
        # matches ABI exactly -> no new alias
        make_concept(["ABI", "Advanced Baseline Imager"]),
        # brand new concept
        make_concept(["SWOT", "Surface Water and Ocean Topography"]),
    ]
    delta = compute_delta("platforms", wl, source)

    assert [c.canonical for c in delta.new_concepts] == ["SWOT"]
    assert len(delta.new_aliases) == 1
    assert delta.new_aliases[0].concept.canonical == "MISR"
    assert delta.new_aliases[0].aliases == ["Terra MISR Camera"]
    assert [c.canonical for c in delta.orphans] == ["RetiredThing"]


def test_apply_delta_appends_and_preserves_order_without_loss():
    wl = _wl()
    source = [
        make_concept(["MISR", "Terra MISR Camera"]),
        make_concept(["SWOT", "Surface Water and Ocean Topography"]),
    ]
    delta = compute_delta("platforms", wl, source)
    merged = apply_delta(wl, delta)

    canon = [c.canonical for c in merged.concepts]
    # Original order preserved, new concept appended at the end.
    assert canon == ["MISR", "ABI", "RetiredThing", "SWOT"]
    # Existing aliases preserved, new alias appended.
    assert merged.concepts[0].aliases == [
        "MISR",
        "Multi-Angle Imaging SpectroRadiometer",
        "Terra MISR Camera",
    ]
    # Orphan retained.
    assert "RetiredThing" in canon
    # Original whitelist object untouched.
    assert [c.canonical for c in wl.concepts] == ["MISR", "ABI", "RetiredThing"]


def test_merge_source_concepts_dedupes_across_sources():
    gcmd = make_concept(
        ["GPS", "Global Positioning System"],
        Provenance(origins=["gcmd"], uuid="u1"),
    )
    sde = make_concept(
        ["gps"], Provenance(origins=["sde"], collection_keys=["CMR_API"])
    )
    merged = merge_source_concepts([gcmd, sde])

    assert len(merged) == 1
    m = merged[0]
    assert m.canonical == "GPS"  # GCMD seen first -> canonical
    assert m.aliases == ["GPS", "Global Positioning System"]  # "gps" folds by key
    assert m.provenance.origins == ["gcmd", "sde"]
    assert m.provenance.collection_keys == ["CMR_API"]
    assert m.provenance.uuid == "u1"
    # Inputs untouched.
    assert gcmd.provenance.origins == ["gcmd"]


def test_combined_sources_yield_single_new_concept():
    wl = Whitelist.parse("MISR;Multi-Angle Imaging SpectroRadiometer")
    # SWOT is absent from the whitelist but present in both sources.
    concepts = [
        make_concept(
            ["SWOT", "Surface Water and Ocean Topography"],
            Provenance(origins=["gcmd"]),
        ),
        make_concept(
            ["SWOT"], Provenance(origins=["sde"], collection_keys=["CMR_API"])
        ),
    ]
    delta = compute_delta("platforms", wl, merge_source_concepts(concepts))

    assert [c.canonical for c in delta.new_concepts] == ["SWOT"]  # not duplicated
    assert delta.new_concepts[0].provenance.origins == ["gcmd", "sde"]


def test_empty_delta_is_noop():
    wl = _wl()
    source = [
        make_concept(["MISR"]),
        make_concept(["ABI"]),
        make_concept(["RetiredThing"]),
    ]
    delta = compute_delta("platforms", wl, source)
    assert delta.is_empty
    merged = apply_delta(wl, delta)
    assert merged.serialize() == wl.serialize()
