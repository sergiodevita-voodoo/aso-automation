"""Load and validate aso-automation.config.yml.

Game-agnostic. Reads the config from the repo root, exposes typed accessors,
and fails fast if anything required is missing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


@dataclass(frozen=True)
class IconPath:
    path: str
    size: int


@dataclass(frozen=True)
class Config:
    raw: Dict[str, Any]
    repo_root: Path

    # Convenience accessors — keep them shallow so the YAML stays the source of truth.
    @property
    def game_name(self) -> str:
        return self.raw["game"]["name"]

    @property
    def game_code(self) -> str:
        return self.raw["game"]["code"]

    @property
    def repo_url(self) -> str:
        return self.raw["game"]["repo_url"]

    @property
    def default_branch(self) -> str:
        return self.raw["game"]["default_branch"]

    @property
    def integration_branch(self) -> str:
        return self.raw["game"]["integration_branch"]

    @property
    def release_branch_pattern(self) -> str:
        return self.raw["game"]["release_branch_pattern"]

    @property
    def commit_prefix(self) -> str:
        return self.raw["game"]["ticket_placeholder"]

    @property
    def activity_guard_days(self) -> int:
        """How many days of recency count as "recent activity" for rule #4.

        If any store track or human commit has been touched within this window,
        the automation falls back to icon-only mode (regenerate + push icon to
        integration branch + develop, skip all store side-effects).

        Default 7 days. Override with ``build.activity_guard_days`` in the
        per-game config to widen (e.g. 14) or narrow (e.g. 3) the window.
        Set to 0 to disable the guard entirely (not recommended — original
        2026-07-13 wave issue would recur).
        """
        return int(self.raw.get("build", {}).get("activity_guard_days", 7))

    @property
    def ios_bundle_id(self) -> str:
        return self.raw["stores"]["ios"]["bundle_id"]

    @property
    def apple_app_id(self) -> str:
        return str(self.raw["stores"]["ios"]["apple_app_id"])

    @property
    def apple_team_id(self) -> str:
        return self.raw["stores"]["ios"]["apple_team_id"]

    @property
    def android_package_name(self) -> str:
        return self.raw["stores"]["android"]["package_name"]

    @property
    def icon_master_size(self) -> int:
        return int(self.raw["icon"]["master_source_size"])

    @property
    def icon_paths(self) -> List[IconPath]:
        return [IconPath(p["path"], int(p["size"])) for p in self.raw["icon"]["paths_to_overwrite"]]

    @property
    def reference_icon_path(self) -> Path:
        """Path (relative to repo root, returned absolute) to the icon used
        BOTH as the reference for the Scenario workflow AND as the source
        for the Google Play Store-listing 512×512 icon.

        Resolution order:
        1. ``icon.reference_path`` if explicitly set in the config.
        2. The largest entry in ``icon.paths_to_overwrite``.

        We pick the largest because Play requires a 512×512 store icon, and
        the iOS reference image quality scales with input size.
        """
        explicit = self.raw["icon"].get("reference_path")
        if explicit:
            return self.repo_root / explicit
        paths = self.icon_paths
        if not paths:
            raise ValueError("icon.paths_to_overwrite is empty — no reference icon available")
        largest = max(paths, key=lambda p: p.size)
        return self.repo_root / largest.path

    @property
    def project_settings_file(self) -> Path:
        return self.repo_root / self.raw["versioning"]["project_settings_file"]

    @property
    def monthly_bump(self) -> str:
        return self.raw["versioning"]["monthly_bump"]

    @property
    def also_increment_android_code(self) -> bool:
        return bool(self.raw["versioning"]["also_increment"]["android_bundle_version_code"])

    @property
    def also_increment_ios_build_number(self) -> bool:
        return bool(self.raw["versioning"]["also_increment"]["ios_build_number"])

    @property
    def build(self) -> Dict[str, Any]:
        return self.raw["build"]

    @property
    def build_provider(self) -> str:
        """CI provider selector — determines which CircleCI shape run.py's
        step 7 uses. ``"vgci"`` (default) triggers the game repo's own
        ``voodoosauce-ci`` orb; ``"vs-ci-deployer"`` triggers legacy pipelines
        on the shared ``VoodooTeam/vs-ci-deployer`` project."""
        v = str(self.raw.get("build", {}).get("provider", "vgci")).lower()
        if v not in ("vgci", "vs-ci-deployer"):
            raise ValueError(f"Unknown build.provider {v!r} — must be 'vgci' or 'vs-ci-deployer'")
        return v

    @property
    def scenario(self) -> Dict[str, Any]:
        return self.raw["scenario"]

    @property
    def aso_content(self) -> Dict[str, Any]:
        return self.raw["aso_content"]

    @property
    def secret_names(self) -> Dict[str, str]:
        """Map of logical secret key → env-var name.

        Defaults to the canonical env-var names used by the shared
        ``VoodooStudios/aso-automation`` GH Action. Per-game configs only
        need to override this if running outside the shared Action with
        non-standard env-var names.
        """
        return {**_DEFAULT_SECRET_NAMES, **(self.raw.get("secrets") or {})}


_DEFAULT_SECRET_NAMES: Dict[str, str] = {
    "scenario_api_key":     "SCENARIO_API_KEY",
    "circleci_token":       "CIRCLECI_TOKEN",
    "asc_key_id":           "ASC_KEY_ID",
    "asc_private_key_p8":   "ASC_PRIVATE_KEY_P8",
    "google_play_sa_json":  "GOOGLE_PLAY_SA_JSON",
    "anthropic_api_key":    "ANTHROPIC_API_KEY",
    "github_token":         "GITHUB_TOKEN",
}

# 'secrets' is optional — defaults above cover the shared-action env-var names.
_REQUIRED_TOP_KEYS = ("game", "stores", "icon", "versioning", "build", "scenario", "aso_content")


def load(repo_root: Path | str, config_filename: str = "aso-automation.config.yml") -> Config:
    """Load and validate the config file at ``<repo_root>/<config_filename>``.

    Raises ``ValueError`` if a required section is missing.
    """
    repo_root = Path(repo_root).resolve()
    path = repo_root / config_filename
    if not path.is_file():
        raise FileNotFoundError(f"Config not found: {path}")

    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise ValueError(f"Config file {path} did not parse to a dict")

    missing = [k for k in _REQUIRED_TOP_KEYS if k not in raw]
    if missing:
        raise ValueError(f"Config {path} missing required top-level keys: {missing}")

    return Config(raw=raw, repo_root=repo_root)


def secret(name: str) -> str:
    """Fetch a secret value from the environment, failing if it's missing.

    The automation reads all credentials from env vars (which GitHub Actions
    populates from repo secrets). This helper standardises the error message.
    """
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(
            f"Required secret env var '{name}' is not set. "
            f"In GitHub Actions, configure it under Settings → Secrets and variables → Actions."
        )
    return val
