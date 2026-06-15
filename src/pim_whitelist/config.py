"""Static configuration: source URLs, file locations, and the dataset registry."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Repository root (…/sde-pim-whitelist), derived from this file's location.
REPO_ROOT = Path(__file__).resolve().parents[2]
WHITELIST_DIR = REPO_ROOT / "whitelist"
CLASSIFIED_DIR = WHITELIST_DIR / "classified"
RAW_CACHE_DIR = REPO_ROOT / "data" / "raw"
REPORTS_DIR = REPO_ROOT / "reports"

USER_AGENT = "sde-pim-whitelist/0.1 (NASA SMD whitelist sync)"


@dataclass(frozen=True)
class DatasetConfig:
    name: str
    whitelist_filename: str

    @property
    def whitelist_path(self) -> Path:
        return WHITELIST_DIR / self.whitelist_filename


PLATFORMS = DatasetConfig(
    name="platforms",
    whitelist_filename="SMD_Platforms_Consolidated.txt",
)
INSTRUMENTS = DatasetConfig(
    name="instruments",
    whitelist_filename="SMD_Instruments_Consolidated.txt",
)
MISSIONS = DatasetConfig(
    name="missions",
    whitelist_filename="SMD_Missions_Consolidated.txt",
)

DATASETS: dict[str, DatasetConfig] = {
    d.name: d for d in (PLATFORMS, INSTRUMENTS, MISSIONS)
}

# Upstream endpoints.
PLATFORMS_URL = (
    "https://cmr.earthdata.nasa.gov/kms/concepts/concept_scheme/platforms?format=csv"
)
INSTRUMENTS_URL = (
    "https://cmr.earthdata.nasa.gov/kms/concepts/concept_scheme/instruments?format=csv"
)
MISSIONS_URL = "https://www.nasa.gov/wp-json/wp/v2/mission/"
MISSIONS_PER_PAGE = 100

# NASA Science Discovery Engine search API. A single crawl over these collection
# keys yields platform/instrument names from across the SMD science divisions.
SDE_URL = "https://science.data.nasa.gov/science-discovery-engine/api/search"
SDE_API_SOURCES = (
    "CMR_API",  # Earth
    "SPASE_JSON",  # Helio
    "PDS_API_Legacy_All",  # Planetary
    "PDS4_API",  # Planetary
    "GENELAB_METADATA_OSDR",  # BPS
    "NAVO_HEASARC",  # Astro
)
SDE_PAGE_SIZE = 100

# NASA SMD science divisions. A concept may belong to one or more of these (or
# none, for opaque identifiers). This is the fixed taxonomy both the SDE
# provenance signal and the OpenAI classifier assign from.
DIVISIONS = ("earth", "heliophysics", "planetary", "astrophysics", "bps")

# Each SDE collection key maps 1:1 to a division. This is the authoritative
# ("provenance") signal: a name the SDE crawl surfaced under a given collection
# belongs to that collection's division. See SDE_API_SOURCES above.
COLLECTION_KEY_TO_DIVISION = {
    "CMR_API": "earth",
    "SPASE_JSON": "heliophysics",
    "PDS_API_Legacy_All": "planetary",
    "PDS4_API": "planetary",
    "GENELAB_METADATA_OSDR": "bps",
    "NAVO_HEASARC": "astrophysics",
}

# Cached SDE crawl rows ({"value", "collection_key"}) written under RAW_CACHE_DIR
# by the SDE sources. Missions are not crawled from the SDE, so they have no
# provenance file and fall entirely to the OpenAI classifier.
SDE_CACHE_FILENAMES = {
    "instruments": "sde_instruments.json",
    "platforms": "sde_platforms.json",
}

# Short glosses for each division, injected into the classifier prompt to ground
# the model in the NASA SMD meaning of each term.
DIVISION_DESCRIPTIONS = {
    "earth": (
        "Earth science: atmosphere, oceans, land, cryosphere, weather, and "
        "climate observed from space, aircraft, or ground."
    ),
    "heliophysics": (
        "Heliophysics: the Sun, solar wind, magnetospheres, ionospheres, space "
        "weather, and the heliosphere."
    ),
    "planetary": (
        "Planetary science: planets, moons, asteroids, comets, and other Solar "
        "System bodies (excluding the Sun and Earth's atmosphere/oceans)."
    ),
    "astrophysics": (
        "Astrophysics: stars, galaxies, exoplanets, cosmology, and the distant "
        "universe beyond the Solar System."
    ),
    "bps": (
        "Biological and Physical Sciences: life sciences and physical sciences "
        "experiments in space (microgravity biology, fundamental physics)."
    ),
}

# OpenAI classifier settings. The model resolves as: CLI --model flag, then the
# OPENAI_MODEL environment variable, then this fallback constant (edit here to
# change the default). The API key is read from OPENAI_API_KEY by the SDK.
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
OPENAI_BATCH_SIZE = 50
