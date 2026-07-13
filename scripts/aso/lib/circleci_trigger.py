"""Trigger CircleCI pipelines and poll their status.

Two provider shapes supported:

  * ``VsCiDeployerTrigger`` — the legacy provider. Triggers pipelines on the
    shared ``VoodooTeam/vs-ci-deployer`` project. Each game has a per-game
    config branch (``feature/<GameName>_<UnityVersion>``) and a Game-Config
    JSON. Pipeline runs iOS and Android as TWO separate triggers with ~12
    parameters each (repository_path, repository_branch, build_number,
    project-version, upload-comment, deployment-type, deploy-build-to-store,
    is_proxy_build, is-lz4hc, plus per-platform bundle/keystore/trigger flags).

  * ``VgciTrigger`` — the new default provider (Voodoo Game CI). Triggers a
    pipeline directly on the game repo's own CircleCI project
    (``VoodooStudios/<game-repo>``) against the release branch we just
    pushed. Single pipeline with ``platform: all`` handles both iOS +
    Android. Only 5 parameters: workflow_type, platform, pipeline_preset,
    comment, inject_tunables. Version bumping, build numbering, and store
    deployment are all controlled by the game repo's ``ci.yml`` + the
    ``voodoosauce-ci`` orb, driven by the ``pipeline_preset`` enum
    (``release_store`` | ``internal_no_deploy`` | ``dev_firebase``).

The provider is chosen per game via ``build.provider`` in
``aso-automation.config.yml``. Default is ``"vgci"``; legacy games must
explicitly set ``provider: vs-ci-deployer``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Tuple

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

log = logging.getLogger(__name__)

_CIRCLECI_BASE = "https://circleci.com/api/v2"


@dataclass
class _BaseTrigger:
    """Shared pipeline-polling logic. Both providers post pipelines and then
    wait for them by polling ``/pipeline/{id}/workflow`` — that part is
    identical, so we hoist it here."""

    token: str                          # CircleCI Personal API Token
    poll_interval_seconds: int = 60
    poll_timeout_minutes: int = 90

    # ───────────────────────────────────────────────────────────────────────
    def wait_for_pipeline(self, pipeline_id: str) -> str:
        """Block until the pipeline reaches a terminal state. Returns the final state.

        Terminal: ``success``, ``failed``, ``errored``, ``canceled``, ``unauthorized``.
        ``RuntimeError`` is raised on non-success terminal.
        ``TimeoutError`` is raised if the poll timeout elapses.
        """
        deadline = time.monotonic() + (self.poll_timeout_minutes * 60)
        while time.monotonic() < deadline:
            state = self._get_pipeline_state(pipeline_id)
            log.debug("CircleCI pipeline %s state=%s", pipeline_id, state)
            if state == "success":
                return state
            if state in ("failed", "errored", "canceled", "cancelled", "unauthorized"):
                raise RuntimeError(f"CircleCI pipeline {pipeline_id} ended with state={state}")
            time.sleep(self.poll_interval_seconds)
        raise TimeoutError(f"CircleCI pipeline {pipeline_id} did not finish within {self.poll_timeout_minutes} min")

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    def _get_pipeline_state(self, pipeline_id: str) -> str:
        """Aggregate workflow states for a pipeline into one summary state."""
        url = f"{_CIRCLECI_BASE}/pipeline/{pipeline_id}/workflow"
        resp = requests.get(url, headers={"Circle-Token": self.token}, timeout=30)
        resp.raise_for_status()
        workflows = resp.json().get("items", [])
        if not workflows:
            return "running"
        states = {w["status"] for w in workflows}
        terminal_bad = {"failed", "errored", "canceled", "cancelled", "unauthorized"}
        if states & terminal_bad:
            return next(iter(states & terminal_bad))
        if states == {"success"}:
            return "success"
        return "running"

    def cancel_pipeline(self, pipeline_id: str) -> None:
        """Best-effort cancel of every non-terminal workflow on a pipeline.

        Used by the orchestrator when the sibling platform's pipeline fails —
        we don't want the other platform to keep building (and potentially
        deploying to Play Internal / TestFlight) after the run has already
        raised. Failures are swallowed and logged: this is defensive cleanup,
        not a critical operation.
        """
        try:
            url = f"{_CIRCLECI_BASE}/pipeline/{pipeline_id}/workflow"
            resp = requests.get(url, headers={"Circle-Token": self.token}, timeout=30)
            resp.raise_for_status()
            wfs = resp.json().get("items", [])
        except Exception as e:
            log.warning("cancel_pipeline: could not list workflows for %s: %s", pipeline_id, e)
            return
        non_terminal = {"running", "on_hold", "failing"}
        cancelled = 0
        for w in wfs:
            if w.get("status") not in non_terminal:
                continue
            try:
                r = requests.post(
                    f"{_CIRCLECI_BASE}/workflow/{w['id']}/cancel",
                    headers={"Circle-Token": self.token}, timeout=30,
                )
                if r.status_code in (200, 202):
                    cancelled += 1
                    log.info("cancel_pipeline: cancelled workflow %s (name=%s)", w["id"], w.get("name"))
                else:
                    log.warning("cancel_pipeline: workflow %s cancel returned %s: %s",
                                w["id"], r.status_code, r.text[:200])
            except Exception as e:
                log.warning("cancel_pipeline: workflow %s cancel failed: %s", w["id"], e)
        log.info("cancel_pipeline: pipeline %s — cancelled %d workflow(s)", pipeline_id, cancelled)

    @retry(stop=stop_after_attempt(5), wait=wait_exponential(min=2, max=20))
    def _post_pipeline(self, project_slug: str, branch: str, parameters: Dict[str, Any]) -> str:
        url = f"{_CIRCLECI_BASE}/project/{project_slug}/pipeline"
        body = {"branch": branch, "parameters": parameters}
        resp = requests.post(url, json=body, headers={"Circle-Token": self.token}, timeout=30)
        if resp.status_code != 201:
            raise RuntimeError(f"CircleCI trigger failed {resp.status_code}: {resp.text[:300]}")
        pipeline_id = resp.json().get("id")
        if not pipeline_id:
            raise RuntimeError(f"CircleCI trigger response missing 'id': {resp.text[:300]}")
        log.info("CircleCI pipeline triggered: %s (project=%s branch=%s params=%s)",
                 pipeline_id, project_slug, branch, _redact(parameters))
        return pipeline_id


@dataclass
class VsCiDeployerTrigger(_BaseTrigger):
    """Legacy provider: shared ``VoodooTeam/vs-ci-deployer`` project.

    iOS and Android are fired as two separate pipelines with per-platform
    trigger flags. Kept intact for the games we've already onboarded while
    they migrate off it.
    """
    project_slug: str = "gh/VoodooTeam/vs-ci-deployer"
    deployer_config_branch: str = ""    # e.g. "feature/GameName_6000.0.72f1"

    def trigger_ios(self, common: Dict[str, Any], bundle_id: str) -> str:
        params = {
            **common,
            "repository_bundle_id": bundle_id,
            "manual-trigger-ios": True,
            "manual-trigger-ios-testflight": True,
            "enable-pod-cache": True,
        }
        return self._post_pipeline(self.project_slug, self.deployer_config_branch, params)

    def trigger_android(self, common: Dict[str, Any], package_name: str, keystore: str) -> str:
        params = {
            **common,
            "repository_bundle_id": package_name,
            "manual-trigger-android-deployment": True,
            "android-keystore": keystore,
            "enable-caching": True,
        }
        return self._post_pipeline(self.project_slug, self.deployer_config_branch, params)


@dataclass
class VgciTrigger(_BaseTrigger):
    """New default provider: game-repo-native ``voodoosauce-ci`` orb.

    Triggers a single pipeline on ``gh/VoodooStudios/<game-repo>`` against the
    release branch we just pushed. The ``pipeline_preset`` enum selects the
    build type + per-platform deploy targets (``release_store`` deploys to
    App Store + Play; ``internal_no_deploy`` builds without shipping;
    ``dev_firebase`` deploys development builds to Firebase).
    """
    project_slug: str = ""              # e.g. "gh/VoodooStudios/BallsGoHigh"

    def trigger_build(
        self,
        *,
        branch: str,
        pipeline_preset: str,
        comment: str = "",
        platform: str = "all",
        inject_tunables: str = "",
    ) -> str:
        params = {
            "workflow_type": "unity-build",
            "platform": platform,
            "pipeline_preset": pipeline_preset,
            "comment": comment,
            "inject_tunables": inject_tunables,
        }
        return self._post_pipeline(self.project_slug, branch, params)

    @staticmethod
    def build_inject_tunables(*, version_number: str, build_number: int) -> str:
        """Build the ``inject_tunables`` string that overrides VGCI's default
        auto-bump behavior with explicit values.

        The orb's default is to auto-bump both versionName and build numbers
        from a global registry — which loses the store-drift + Opus logic
        this orchestrator does upstream. Passing explicit tunables makes the
        orb use our exact numbers instead.

        NOTE ON KEY NAMING: VGCI's "Check Android/iOS build number" step reads
        the SHARED ``build_number`` tunable, not the platform-prefixed
        ``android_build_number`` / ``ios_build_number``. Prefixed keys resolve
        in the internal tunables table but never propagate to the check step,
        so passing them leaves the check reading placeholder=1 and rejecting
        the run ("version code 1 cannot be used"). We therefore always send
        the single shared ``build_number``. Feed it the ANDROID versionCode
        (Int32-safe) — iOS accepts the same value because each ASO release
        bumps the versionString, and buildNumber uniqueness in ASC is scoped
        per-CFBundleShortVersionString.

        Format: comma-separated `"key":"value"` pairs (JSON-fragment shape;
        NOT a full JSON object). Values are always strings — the orb's
        template parses them into typed values.
        """
        parts = [
            f'"version_number":"{version_number}"',
            f'"build_number":"{build_number}"',
            '"auto_bump_version":"false"',
            '"auto_bump_build":"false"',
            '"continuous_build_number":"false"',
            '"sync_build_numbers":"false"',
        ]
        return ",".join(parts)


# ───────────────────────────────────────────────────────────────────────────
# Backward-compat alias — old callers imported ``CircleCITrigger`` directly.
# Point that name at the legacy provider so nothing breaks during migration.
CircleCITrigger = VsCiDeployerTrigger


def _redact(params: Dict[str, Any]) -> Dict[str, Any]:
    """For logging — never echo bundle IDs or other potentially-sensitive values verbatim."""
    return {k: ("***" if "bundle" in k.lower() else v) for k, v in params.items()}
