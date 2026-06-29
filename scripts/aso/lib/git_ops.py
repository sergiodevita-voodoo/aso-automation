"""Git operations for the ASO automation.

All work happens in a subprocess shelling out to ``git`` directly so we don't
need to add a pure-Python git library as a dependency (CI is short-lived and
``git`` is preinstalled on every runner).

Inside GitHub Actions, ``GITHUB_TOKEN`` provides push access automatically;
the workflow YAML configures ``actions/checkout`` with ``persist-credentials: true``
so this module just runs the git commands.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)


def _run(cmd: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    log.debug("$ %s", " ".join(cmd))
    return subprocess.run(cmd, cwd=cwd, check=check, capture_output=True, text=True)


def configure_actor(repo_root: Path, name: str = "github-actions[bot]", email: str = "41898282+github-actions[bot]@users.noreply.github.com") -> None:
    """Configure the git author for commits made by the automation.

    Uses the conventional github-actions bot identity so commits show up
    with the right avatar / "GitHub Actions" attribution in the Insights tab.
    """
    _run(["git", "config", "user.name", name], cwd=repo_root)
    _run(["git", "config", "user.email", email], cwd=repo_root)


def create_release_branch(repo_root: Path, version: str, branch_pattern: str, base: str) -> str:
    """Create a release branch named per ``branch_pattern`` (e.g. ``release/{version}``)
    cut from ``base`` (typically ``develop``).

    Returns the branch name actually created.
    """
    branch = branch_pattern.replace("{version}", version)
    _run(["git", "fetch", "origin", base], cwd=repo_root)
    # -B forces creation even if a prior failed run left a stale branch around
    # locally — the prior remote state is preserved.
    _run(["git", "checkout", "-B", branch, f"origin/{base}"], cwd=repo_root)
    log.info("Created branch %s from origin/%s", branch, base)
    return branch


def stage_and_commit(repo_root: Path, paths: Iterable[Path], commit_message: str) -> None:
    """Stage the given paths and commit. No-op if there are no changes."""
    rel_paths = [str(p.relative_to(repo_root)) if p.is_absolute() else str(p) for p in paths]
    _run(["git", "add", "--"] + rel_paths, cwd=repo_root)

    # Check whether anything is actually staged before committing.
    diff = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=repo_root, capture_output=True,
    )
    if diff.returncode == 0:
        log.warning("No staged changes detected — skipping commit")
        return

    _run(["git", "commit", "-m", commit_message], cwd=repo_root)
    log.info("Committed: %s", commit_message.splitlines()[0])


def push_branch(repo_root: Path, branch: str) -> None:
    """Push the current branch to origin, setting upstream."""
    _run(["git", "push", "--set-upstream", "origin", branch], cwd=repo_root)
    log.info("Pushed %s to origin", branch)


def merge_release_back(repo_root: Path, release_branch: str, targets: list[str]) -> None:
    """Merge ``release_branch`` into each branch in ``targets`` (typically
    ['develop', 'master']) per PGT convention.

    Uses a no-ff merge so the release shows as an explicit merge commit on
    each target. Best-effort per target — logs the failure and continues
    rather than rolling back, because by the time this runs the stores
    already have the release.
    """
    # Fetch latest state for all targets
    _run(["git", "fetch", "origin", *targets, release_branch], cwd=repo_root)
    for target in targets:
        try:
            # Check out target, merge release with --no-ff, push
            _run(["git", "checkout", "-B", target, f"origin/{target}"], cwd=repo_root)
            msg = f"Merge release {release_branch} into {target} (ASO automation)"
            _run(["git", "merge", "--no-ff", f"origin/{release_branch}", "-m", msg], cwd=repo_root)
            _run(["git", "push", "origin", target], cwd=repo_root)
            log.info("Merged %s into %s and pushed", release_branch, target)
        except subprocess.CalledProcessError as e:
            log.error("Failed to merge %s into %s: %s", release_branch, target,
                      e.stderr.strip() if e.stderr else e)
            # Continue — don't fail the whole run for a merge issue


def delete_branch(repo_root: Path, branch: str) -> None:
    """Delete a remote branch on origin. Best-effort."""
    try:
        _run(["git", "push", "origin", "--delete", branch], cwd=repo_root)
        log.info("Deleted %s on origin", branch)
    except subprocess.CalledProcessError as e:
        log.warning("Could not delete %s on origin: %s", branch,
                    e.stderr.strip() if e.stderr else e)
