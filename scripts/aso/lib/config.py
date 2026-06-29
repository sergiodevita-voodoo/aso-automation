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
    def scenario(self) -> Dict[str, Any]:
        return self.raw["scenario"]

    @property
    def aso_content(self) -> Dict[str, Any]:
        return self.raw["aso_content"]

    @property
    def secret_names(self) -> Dict[str, str]:
        return self.raw["secrets"]


_REQUIRED_TOP_KEYS = ("game", "stores", "icon", "versioning", "build", "scenario", "aso_content", "secrets")


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
