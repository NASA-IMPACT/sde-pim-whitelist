"""Self-contained config for the standalone division-clustering fine-tune.

Deliberately imports NOTHING from ``pim_whitelist`` so this pipeline has zero
coupling to the existing package. The two constants below are copied verbatim
from ``src/pim_whitelist/config.py`` (DIVISIONS + DIVISION_DESCRIPTIONS). If the
upstream taxonomy ever changes, re-sync them by hand.
"""

from __future__ import annotations

from pathlib import Path

# --- Paths -----------------------------------------------------------------
# finetune/ sits directly under the repo root.
FINETUNE_DIR = Path(__file__).resolve().parent
REPO_ROOT = FINETUNE_DIR.parent
CLASSIFIED_DIR = REPO_ROOT / "whitelist" / "classified"

DATA_DIR = FINETUNE_DIR / "data"
MODELS_DIR = FINETUNE_DIR / "models"
RUNS_DIR = FINETUNE_DIR / "runs"

# --- Model / reproducibility ----------------------------------------------
BASE_MODEL = "nasa-impact/indus-sde-st-v0.2"
SEED = 42

# --- Taxonomy (copied from src/pim_whitelist/config.py) -------------------
# NASA SMD science divisions, in canonical order. A concept may belong to one or
# more of these; divisions[0] is treated as the primary (single) training label.
DIVISIONS = ("earth", "heliophysics", "planetary", "astrophysics", "bps")

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

# Stable division -> integer id. Kept over the full taxonomy (not just populated
# divisions) so ids are stable even if astrophysics data appears later.
DIVISION_TO_ID = {name: i for i, name in enumerate(DIVISIONS)}
ID_TO_DIVISION = {i: name for name, i in DIVISION_TO_ID.items()}

# The datasets that carry SDE provenance (missions have none, so are excluded).
# kind label -> classified JSON filename.
PROVENANCE_DATASETS = {
    "instrument": "instruments.json",
    "platform": "platforms.json",
}

# Only records with this division_source are authoritative ("provenance").
PROVENANCE_SOURCE = "provenance"
