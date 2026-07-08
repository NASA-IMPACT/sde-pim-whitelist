"""Runtime settings for the read API (Pydantic ``BaseSettings``).

Environment knobs are prefixed ``PIM_`` (e.g. ``PIM_DATA_DIR``,
``PIM_CORS_ORIGINS``). The ``.env`` file is loaded only **off** Lambda so local
development picks up overrides while the deployed function relies on real env
vars set by CDK.

The data directory is resolved from ``LAMBDA_TASK_ROOT`` when present, falling
back to the repo root off-Lambda. This deliberately avoids
:data:`pim_whitelist.config.CLASSIFIED_DIR`, whose ``REPO_ROOT`` is derived from
``__file__`` and resolves to ``/var`` inside the Lambda zip.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .. import config

# Page-size window enforced everywhere (the plan's 50–100 records/page). Kept as
# module constants so both the settings model and the query model agree.
PAGE_SIZE_MIN = 50
PAGE_SIZE_MAX = 100
PAGE_SIZE_DEFAULT = 50


def _default_data_dir() -> Path:
    """Directory holding ``{instruments,platforms,missions}.json``.

    In Lambda the code unzips under ``LAMBDA_TASK_ROOT`` (``/var/task``) with the
    data bundled at ``whitelist/classified/``. Off-Lambda we use the repo root.
    """
    root = os.environ.get("LAMBDA_TASK_ROOT")
    base = Path(root) if root else config.REPO_ROOT
    return base / "whitelist" / "classified"


def _in_lambda() -> bool:
    return bool(os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))


class Settings(BaseSettings):
    """API configuration; overridable via ``PIM_*`` env vars or ``.env`` locally."""

    model_config = SettingsConfigDict(
        env_prefix="PIM_",
        env_file=None if _in_lambda() else ".env",
        extra="ignore",
    )

    data_dir: Path = Field(default_factory=_default_data_dir)

    # Browser origins allowed by CORS. Defaults to permissive for local dev;
    # CDK sets PIM_CORS_ORIGINS to the real UI origin(s) in the deployed stack.
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])

    # Cache-Control max-age (seconds) advertised on read responses so CloudFront
    # can absorb hot queries. 0 disables client/edge caching.
    cache_max_age: int = 300


def load_settings() -> Settings:
    """Construct a fresh Settings instance (cached by ``deps.get_settings``)."""
    return Settings()
