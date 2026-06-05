from pim_whitelist.normalize import clean, match_key


def test_clean_unescapes_html_entities():
    # &#8217; is the curly right single quote (U+2019); clean preserves it
    # verbatim (only match_key folds it to a straight apostrophe).
    assert clean("NASA&#8217;s SpaceX Crew-13") == "NASA’s SpaceX Crew-13"


def test_clean_collapses_whitespace_and_strips():
    assert clean("  Multi   Angle\tImaging  ") == "Multi Angle Imaging"


def test_clean_preserves_case_and_punctuation():
    # clean() must not alter case or punctuation; those are meaningful aliases.
    assert clean("ACCELEROMETER") == "ACCELEROMETER"
    assert clean("Multi-Angle Imaging SpectroRadiometer") == (
        "Multi-Angle Imaging SpectroRadiometer"
    )


def test_match_key_is_case_and_punctuation_insensitive():
    a = match_key("Multi-Angle Imaging SpectroRadiometer")
    b = match_key("multi angle imaging spectroradiometer")
    assert a == b == "multiangleimagingspectroradiometer"


def test_match_key_folds_curly_and_straight_apostrophes():
    assert match_key("NASA’s Crew") == match_key("NASA's Crew")


def test_match_key_empty_for_punctuation_only():
    assert match_key("---") == ""
    assert match_key("") == ""
