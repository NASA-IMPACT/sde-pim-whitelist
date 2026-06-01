"""Static configuration: source URLs, file locations, and the dataset registry."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Repository root (…/sde-pim-whitelist), derived from this file's location.
REPO_ROOT = Path(__file__).resolve().parents[2]
WHITELIST_DIR = REPO_ROOT / "whitelist"
RAW_CACHE_DIR = REPO_ROOT / "data" / "raw"
REPORTS_DIR = REPO_ROOT / "reports"

USER_AGENT = "sde-pim-whitelist/0.1 (NASA SMD whitelist sync)"


@dataclass(frozen=True)
class DatasetConfig:
    name: str
    whitelist_filename: str
    cache_filename: str

    @property
    def whitelist_path(self) -> Path:
        return WHITELIST_DIR / self.whitelist_filename

    @property
    def cache_path(self) -> Path:
        return RAW_CACHE_DIR / self.cache_filename


PLATFORMS = DatasetConfig(
    name="platforms",
    whitelist_filename="SMD_Platforms_Consolidated.txt",
    cache_filename="platforms.csv",
)
INSTRUMENTS = DatasetConfig(
    name="instruments",
    whitelist_filename="SMD_Instruments_Consolidated.txt",
    cache_filename="instruments.csv",
)
MISSIONS = DatasetConfig(
    name="missions",
    whitelist_filename="SMD_Missions_Consolidated.txt",
    cache_filename="missions.json",
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
