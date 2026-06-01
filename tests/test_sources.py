import json

from pim_whitelist.sources.csv_source import parse_gcmd_csv
from pim_whitelist.sources.missions import MissionsSource

PLATFORMS_CSV = (
    '"Keyword Version: 24.0","Revision: x","Timestamp: y"\r\n'
    '"Basis","Category","Sub_Category","Short_Name","Long_Name","UUID"\r\n'
    '"Air-based Platforms","","","","","ef71c514"\r\n'  # hierarchy node, no short name -> skip
    '"Air-based Platforms","Balloons","","BALLOONS","","a158"\r\n'  # short only
    '"Earth Observation","Sat","","MISR","Multi-Angle Imaging SpectroRadiometer","u-misr"\r\n'
)

INSTRUMENTS_CSV = (
    '"Keyword Version: 24.0","Revision: x"\r\n'
    '"Category","Class","Type","Subtype","Short_Name","Long_Name","UUID"\r\n'
    '"Earth Remote Sensing","Active","","","","","6015"\r\n'  # skip
    '"Earth Remote Sensing","Active","","","ABI","Advanced Baseline Imager","u-abi"\r\n'
)


def test_parse_platforms_csv_skips_hierarchy_nodes():
    concepts = parse_gcmd_csv(PLATFORMS_CSV.encode("utf-8"))
    canon = [c.canonical for c in concepts]
    assert canon == ["BALLOONS", "MISR"]
    misr = concepts[1]
    assert misr.aliases == ["MISR", "Multi-Angle Imaging SpectroRadiometer"]
    assert misr.provenance["uuid"] == "u-misr"


def test_parse_instruments_csv():
    concepts = parse_gcmd_csv(INSTRUMENTS_CSV.encode("utf-8"))
    assert [c.canonical for c in concepts] == ["ABI"]
    assert concepts[0].aliases == ["ABI", "Advanced Baseline Imager"]


def test_long_name_equal_to_short_name_is_deduped():
    csv = (
        '"meta"\r\n'
        '"Short_Name","Long_Name","UUID"\r\n'
        '"GPS","gps","u1"\r\n'  # long name only differs by case -> single alias
    )
    concepts = parse_gcmd_csv(csv.encode("utf-8"))
    assert concepts[0].aliases == ["GPS"]


def test_missions_parse_decodes_entities_and_provenance():
    posts = [
        {
            "id": 1,
            "slug": "crew-13",
            "title": {"rendered": "NASA&#8217;s SpaceX Crew-13"},
            "mission-type": [10994],
            "mission-status": [10873],
            "link": "https://nasa.gov/crew-13",
        },
        {"id": 2, "title": {"rendered": "Artemis IV"}, "mission-type": []},
    ]
    raw = json.dumps(posts).encode("utf-8")
    concepts = MissionsSource.__new__(MissionsSource).parse(raw)
    assert [c.canonical for c in concepts] == ["NASA’s SpaceX Crew-13", "Artemis IV"]
    assert concepts[0].provenance["id"] == 1
    assert concepts[0].provenance["mission_type"] == [10994]
