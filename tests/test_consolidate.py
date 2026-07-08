from pim_whitelist.consolidate import consolidate
from pim_whitelist.sources.base import make_concept
from pim_whitelist.whitelist import Whitelist


def _canon(wl):
    return [c.canonical for c in wl.concepts]


def test_exact_key_fold_collapses_duplicates_keeping_first_canonical():
    # Two lines that normalize to the same key for one alias.
    wl = Whitelist.parse("MISR;Multi-Angle Imaging SpectroRadiometer\nmisr")
    merged, report = consolidate(wl, [], "instruments")

    assert _canon(merged) == ["MISR"]
    assert merged.concepts[0].aliases == [
        "MISR",
        "Multi-Angle Imaging SpectroRadiometer",
    ]
    assert report.exact_merges == 1


def test_acronym_merge_folds_bare_entry_with_parenthetical():
    wl = Whitelist.parse("Simbox\nScience in Microgravity Box (SIMBOX)")
    merged, report = consolidate(wl, [], "instruments")

    assert len(merged.concepts) == 1
    aliases = merged.concepts[0].aliases
    assert "Simbox" in aliases
    assert "Science in Microgravity Box (SIMBOX)" in aliases
    assert len(report.acronym_merges) == 1
    assert report.acronym_merges[0].primary == "Simbox"


def test_short_acronym_is_not_auto_merged_but_reported():
    # "MRI" is length 3 -> below MIN_ACRONYM_LEN; must not merge.
    wl = Whitelist.parse("MRI\nMedium Resolution Instrument (MRI)")
    merged, report = consolidate(wl, [], "instruments")

    assert len(merged.concepts) == 2
    assert not report.acronym_merges
    assert any(p.primary == "MRI" for p in report.ambiguous_acronyms)


def test_non_bare_target_is_not_auto_merged():
    # The acronym "WIND" matches an entry that is NOT a bare token, so no merge.
    wl = Whitelist.parse("WIND Spacecraft;WIND\nMagnetic Field Investigation (WIND)")
    merged, _ = consolidate(wl, [], "instruments")
    assert len(merged.concepts) == 2


def test_sorted_case_insensitively_alias_order_preserved():
    wl = Whitelist.parse("zebra;Z One\nABI;Advanced Baseline Imager\nmango")
    merged, _ = consolidate(wl, [], "instruments")

    assert _canon(merged) == ["ABI", "mango", "zebra"]
    # Alias order within a line is untouched (canonical still first).
    assert merged.concepts[0].aliases == ["ABI", "Advanced Baseline Imager"]


def test_idempotent():
    wl = Whitelist.parse(
        "Simbox\nScience in Microgravity Box (SIMBOX)\n"
        "zebra;Z One\nABI;Advanced Baseline Imager\nmisr\nMISR;Multi-Angle"
    )
    once, _ = consolidate(wl, [], "instruments")
    twice, _ = consolidate(once, [], "instruments")
    assert once.serialize() == twice.serialize()


def test_new_source_concept_folds_into_existing_then_sorts():
    wl = Whitelist.parse("MISR;Multi-Angle Imaging SpectroRadiometer\nABI;Imager")
    sources = [
        make_concept(["misr", "Terra MISR Camera"]),  # folds into MISR
        make_concept(["SWOT", "Surface Water and Ocean Topography"]),  # new
    ]
    merged, _ = consolidate(wl, sources, "platforms")

    assert _canon(merged) == ["ABI", "MISR", "SWOT"]
    misr = next(c for c in merged.concepts if c.canonical == "MISR")
    assert "Terra MISR Camera" in misr.aliases


def test_fuzzy_candidate_reported_not_merged():
    wl = Whitelist.parse("Rodent Habitat\nRodetn Habitat")
    merged, report = consolidate(wl, [], "instruments")

    # Distinct keys -> not merged.
    assert len(merged.concepts) == 2
    assert any(
        {p.primary, p.other} == {"Rodent Habitat", "Rodetn Habitat"}
        for p in report.fuzzy_candidates
    )


def test_serial_family_not_flagged_as_fuzzy():
    # Members differing only by a serial/generation token are distinct entities,
    # not near-duplicates: digits, single letters, and roman numerals all count.
    wl = Whitelist.parse(
        "BARREL 1A Optical Photometer\nBARREL 1B Optical Photometer\n"
        "ACRIM I\nACRIM II\nAVHRR\nAVHRR-2\n"
        "Apollo 14 Cold Cathode Ion Gauge Experiment\n"
        "Apollo 15 Cold Cathode Ion Gauge Experiment"
    )
    _, report = consolidate(wl, [], "instruments")
    assert report.fuzzy_candidates == []


def test_genuine_variant_still_flagged_alongside_serial_family():
    # A real spelling variant (Gage/Gauge) on the SAME serial number must survive
    # the enumeration filter, since its content words actually differ.
    wl = Whitelist.parse(
        "Apollo 14 Cold Cathode Ion Gage Experiment\n"
        "Apollo 14 Cold Cathode Ion Gauge Experiment\n"
        "Apollo 15 Cold Cathode Ion Gauge Experiment"
    )
    _, report = consolidate(wl, [], "instruments")
    flagged = [{p.primary, p.other} for p in report.fuzzy_candidates]
    assert {
        "Apollo 14 Cold Cathode Ion Gage Experiment",
        "Apollo 14 Cold Cathode Ion Gauge Experiment",
    } in flagged
    # ...but the 14-vs-15 serial pair is still suppressed.
    assert {
        "Apollo 14 Cold Cathode Ion Gauge Experiment",
        "Apollo 15 Cold Cathode Ion Gauge Experiment",
    } not in flagged


def test_template_siblings_with_unrelated_token_not_flagged():
    # Same template, one slot is a distinct station code / place — different
    # entities sharing a phrasing, never typos of each other.
    wl = Whitelist.parse(
        "CPMN fluxgate magnetometer at ADL\nCPMN fluxgate magnetometer at ANC\n"
        "Cassini ISS Narrow Angle Camera\nCassini ISS Wide Angle Camera"
    )
    _, report = consolidate(wl, [], "instruments")
    assert report.fuzzy_candidates == []


def test_large_series_suppressed_even_when_codes_look_alike():
    # bathythermograph type codes (BT/EBT/MBT/XBT) are similar enough that a pure
    # token-similarity check would leak them; the family of 4 marks the slot as an
    # enumerated series and suppresses the whole set.
    wl = Whitelist.parse(
        "bathythermograph - BT\nbathythermograph - EBT\n"
        "bathythermograph - MBT\nbathythermograph - XBT"
    )
    _, report = consolidate(wl, [], "instruments")
    assert report.fuzzy_candidates == []


def test_near_duplicate_with_similar_token_survives_template_filter():
    # One differing token, but the tokens are near-misspellings -> keep for review.
    wl = Whitelist.parse(
        "Abinger Magnetic Field Instrument\nAbinegr Magnetic Field Instrument"
    )
    _, report = consolidate(wl, [], "instruments")
    assert any(
        {p.primary, p.other}
        == {"Abinger Magnetic Field Instrument", "Abinegr Magnetic Field Instrument"}
        for p in report.fuzzy_candidates
    )
