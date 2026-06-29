"""Trigger CircleCI pipelines on ``VoodooTeam/vs-ci-deployer`` and poll their status.

The exact pipeline parameter shape was reverse-engineered from
``Assets/Scripts/Editor/LaunchCIBuildEditorWindow.cs`` — the in-Editor build
launcher that has been working for the PGT team for months. We just replicate
its POST body and run it from CI instead of a developer's machine.

Pipeline parameters (sourced from LaunchCIBuildEditorWindow.cs ~line 320):

    Common:
        repository_path        — GitHub URL of the game repo
        repository_branch      — branch in the game repo to build
        build_number           — opaque tracking number
        project-version        — version string written to the build
        upload-comment         — note attached to the build
        deployment-type        — "release"
        deploy-build-to-store  — true to push past TestFlight/Internal track
        is_proxy_build         — true (always, per the existing flow)
        is-lz4hc               — true (always, per the existing flow)

    iOS extra:
        repository_bundle_id            — iOS bundle id
        manual-trigger-ios              — true
        manual-trigger-ios-testflight   — true
        enable-pod-cache                — true

    Android extra:
        repository_bundle_id              — Android package name
        manual-trigger-android-deployment — true
        android-keystore                  — keystore filename
        enable-caching                    — true
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
class CircleCITrigger:
    token: str                          # CircleCI Personal API Token
    project_slug: str                   # e.g. "gh/VoodooTeam/vs-ci-deployer"
    deployer_config_branch: str         # vs-ci-deployer branch holding the per-game config
    poll_interval_seconds: int = 60
    poll_timeout_minutes: int = 90

    # ───────────────────────────────────────────────────────────────────────
    def trigger_ios(self, common: Dict[str, Any], bundle_id: str) -> str:
        params = {
            **common,
            "repository_bundle_id": bundle_id,
            "manual-trigger-ios": True,
            "manual-trigger-ios-testflight": True,
            "enable-pod-cache": True,
        }
        return self._post_pipeline(params)

    def trigger_android(self, common: Dict[str, Any], package_name: str, keystore: str) -> str:
        params = {
            **common,
            "repository_bundle_id": package_name,
            "manual-trigger-android-deployment": True,
            "android-keystore": keystore,
            "enable-caching": True,
        }
        return self._post_pipeline(params)

    # ───────────────────────────────────────────────────────────────────────
    @retry(stop=stop_after_attempt(5), wait=wait_exponential(min=2, max=20))
    def _post_pipeline(self, parameters: Dict[str, Any]) -> str:
        url = f"{_CIRCLECI_BASE}/project/{self.project_slug}/pipeline"
        body = {"branch": self.deployer_config_branch, "parameters": parameters}
        resp = requests.post(url, json=body, headers={"Circle-Token": self.token}, timeout=30)
        if resp.status_code != 201:
            raise RuntimeError(f"CircleCI trigger failed {resp.status_code}: {resp.text[:300]}")
        pipeline_id = resp.json().get("id")
        if not pipeline_id:
            raise RuntimeError(f"CircleCI trigger response missing 'id': {resp.text[:300]}")
        log.info("CircleCI pipeline triggered: %s (params: %s)", pipeline_id, _redact(parameters))
        return pipeline_id

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
        """Aggregate the workflow states for a pipeline into one summary state.

        CircleCI's data model is: pipeline → workflows → jobs. A pipeline is
        considered "success" only when all its workflows succeed, "failed"
        if any workflow ends in a non-success terminal state, and otherwise
        still "running".
        """
        url = f"{_CIRCLECI_BASE}/pipeline/{pipeline_id}/workflow"
        resp = requests.get(url, headers={"Circle-Token": self.token}, timeout=30)
        resp.raise_for_status()
        workflows = resp.json().get("items", [])
        if not workflows:
            return "running"   # No workflows yet — still spinning up.

        states = {w["status"] for w in workflows}
        terminal_bad = {"failed", "errored", "canceled", "cancelled", "unauthorized"}
        if states & terminal_bad:
            return next(iter(states & terminal_bad))
        if states == {"success"}:
            return "success"
        return "running"


def _redact(params: Dict[str, Any]) -> Dict[str, Any]:
    """For logging — never echo bundle IDs or other potentially-sensitive values verbatim."""
    return {k: ("***" if "bundle" in k.lower() else v) for k, v in params.items()}
