#!/usr/bin/env python3
"""Monthly ASO automation orchestrator for PGT games.

Run as: ``python scripts/aso/run.py [--dry-run]``

This script is intentionally a thin orchestrator — it wires together the
library modules in ``scripts/aso/lib/`` and stays game-agnostic. All
game-specific values come from ``aso-automation.config.yml`` at the repo root.

Steps (each one is idempotent — re-running on the same branch picks up where
it left off):

1. Load config + secrets
2. Bump version (X.YY → X.YY.Z+1) in ProjectSettings.asset
3. Create release/X.YY.Z branch from ``develop``
4. Trigger Scenario AI workflow → download master icon PNG
5. Resize master to every variant in ``Assets/Icons/``, overwriting in place
6. Stage + commit + push the branch
7. Trigger CircleCI iOS + Android pipelines on vs-ci-deployer; wait for both
8. On success — CI uploaded the build to TestFlight + Play Internal track
9. (TODO) Promote Play Internal → Production, set release notes
10. ASC: wait for build to be visible, create new version, attach build,
    set whatsNew per locale, submit for review
11. Done — GitHub for Slack posts success card to #pgt-dribble-hoops

``--dry-run`` short-circuits at step 6: it commits the branch locally but
doesn't push, doesn't trigger CI, doesn't touch the stores. Used for the
first run and for any config change.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
from pathlib import Path

# Make lib package importable regardless of cwd
_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR.parent.parent))

from scripts.aso.lib import (
    appstoreconnect_client,
    circleci_trigger,
    config,
    git_ops,
    googleplay_client,
    icon_processor,
    scenario_client,
    text_generator,
    unity_settings,
)


class _RunState:
    """Tracks what we created in this run so we can roll it back on failure.

    Each attribute starts as None and is set as the corresponding side-effect
    happens. ``rollback`` then walks the populated fields in reverse order
    and tries to undo each one — best-effort, never raises.
    """

    def __init__(self):
        self.release_branch_pushed: str | None = None        # branch name on origin
        self.ios_pipeline_id: str | None = None              # CircleCI pipeline (cannot cancel after success)
        self.android_pipeline_id: str | None = None
        self.asc_version_id: str | None = None               # if created in this run
        self.asc_attached_build_id: str | None = None        # if we attached a build
        self.asc_submission_id: str | None = None            # if we submitted
        self.ios_build_id: str | None = None                 # build to expire on rollback
        self.play_internal_committed: bool = False           # Play Internal release was committed

    def rollback(self, log, cfg, asc=None, play=None):
        log.warning("=== Rolling back this run's side-effects ===")
        # 1. ASC reviewSubmission — try the new endpoint; if already submitted to
        #    Apple Review, this will 409 (cannot cancel) and we log + continue.
        if self.asc_submission_id and asc is not None:
            try:
                r = asc._request("DELETE", f"/v1/reviewSubmissions/{self.asc_submission_id}")
                log.info("  ASC reviewSubmission %s delete → %s", self.asc_submission_id, r.status_code)
            except Exception as e:
                log.warning("  ASC reviewSubmission delete failed: %s", e)
        # 2. ASC build detach
        if self.asc_version_id and self.asc_attached_build_id and asc is not None:
            try:
                import json as _j
                r = asc._request("PATCH",
                    f"/v1/appStoreVersions/{self.asc_version_id}/relationships/build",
                    data=_j.dumps({"data": None}))
                log.info("  ASC detach build → %s", r.status_code)
            except Exception as e:
                log.warning("  ASC detach build failed: %s", e)
        # 3. ASC version delete (best-effort — Apple often refuses with STATE_ERROR)
        if self.asc_version_id and asc is not None:
            try:
                r = asc._request("DELETE", f"/v1/appStoreVersions/{self.asc_version_id}")
                log.info("  ASC version %s delete → %s", self.asc_version_id, r.status_code)
            except Exception as e:
                log.warning("  ASC version delete failed: %s", e)
        # 4. Expire TestFlight build
        if self.ios_build_id and asc is not None:
            try:
                import json as _j
                r = asc._request("PATCH", f"/v1/builds/{self.ios_build_id}",
                    data=_j.dumps({"data": {"type":"builds","id":self.ios_build_id,"attributes":{"expired":True}}}))
                log.info("  TestFlight build %s expire → %s", self.ios_build_id, r.status_code)
            except Exception as e:
                log.warning("  TestFlight expire failed: %s", e)
        # 5. Play Internal — clear the release we pushed
        if self.play_internal_committed and play is not None:
            try:
                edit_id = play.open_edit()
                play._client().edits().tracks().update(
                    packageName=play.package_name, editId=edit_id, track="internal",
                    body={"track": "internal", "releases": []},
                ).execute()
                play.commit_edit(edit_id)
                log.info("  Play internal track cleared")
            except Exception as e:
                log.warning("  Play internal clear failed: %s", e)
        # 6. Release branch — delete on origin
        if self.release_branch_pushed:
            try:
                import subprocess as _sp
                _sp.run(["git", "push", "origin", "--delete", self.release_branch_pushed],
                        check=False, capture_output=True)
                log.info("  release branch %s deleted on origin", self.release_branch_pushed)
            except Exception as e:
                log.warning("  branch delete failed: %s", e)
        log.warning("=== Rollback complete ===")


def main() -> int:
    parser = argparse.ArgumentParser(description="Monthly ASO automation orchestrator")
    parser.add_argument("--dry-run", action="store_true", help="Generate assets + commit locally, skip push / CI / stores")
    parser.add_argument("--repo-root", default=str(_SCRIPT_DIR.parent.parent), help="Path to the game repo root")
    args = parser.parse_args()

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s :: %(message)s",
    )
    log = logging.getLogger("aso.run")

    cfg = config.load(args.repo_root)
    log.info("ASO automation starting for %s (%s) — dry_run=%s", cfg.game_name, cfg.game_code, args.dry_run)

    state = _RunState()
    asc_client_for_rollback = None
    play_client_for_rollback = None
    try:
        return _run(args, cfg, log, state)
    except Exception as exc:
        log.exception("ASO automation failed — rolling back")
        # Build clients for rollback if we have enough config to do so
        try:
            asc_client_for_rollback = appstoreconnect_client.AppStoreConnectClient(
                key_id=config.secret(cfg.secret_names["asc_key_id"]),
                private_key_p8=config.secret(cfg.secret_names["asc_private_key_p8"]),
                app_id=cfg.apple_app_id,
            )
        except Exception:
            pass
        try:
            play_client_for_rollback = googleplay_client.GooglePlayClient(
                sa_json=config.secret(cfg.secret_names["google_play_sa_json"]),
                package_name=cfg.android_package_name,
            )
        except Exception:
            pass
        state.rollback(log, cfg, asc=asc_client_for_rollback, play=play_client_for_rollback)
        return 1


def _run(args, cfg, log, state: _RunState) -> int:
    """Inner pipeline. Mutates ``state`` so the outer ``main`` can roll back on failure."""
    # ─── Step 1: release branch (cut from develop FIRST, then read state) ──
    # IMPORTANT: branch cut must come before reading ProjectSettings.asset.
    # The CircleCI runner clones the default branch (master), which may have
    # stale build numbers. We need to read develop's state, so we switch
    # the working tree to origin/<integration_branch> first.
    log.info("[1/10] Create release branch (off %s)", cfg.integration_branch)
    repo_root = Path(args.repo_root)
    git_ops.configure_actor(repo_root)
    # First read bundleVersion off integration branch so we can compute the
    # release branch name. Re-read everything else after the checkout.
    pre_state = unity_settings.read_current(cfg.project_settings_file)  # placeholder for branch name
    # Actually: cut the branch unconditionally, naming it by the post-bump
    # patch version computed from the *integration branch*'s state. To get
    # that, fetch the integration branch first, read it via git show.
    import subprocess
    subprocess.run(["git", "fetch", "origin", cfg.integration_branch], cwd=repo_root, check=True)
    show = subprocess.run(
        ["git", "show", f"origin/{cfg.integration_branch}:{cfg.raw['versioning']['project_settings_file']}"],
        cwd=repo_root, capture_output=True, text=True, check=True,
    )
    # Parse bundleVersion from the integration branch's ProjectSettings text
    import re
    bv_match = re.search(r"^\s*bundleVersion:\s*(.+?)\s*$", show.stdout, re.MULTILINE)
    if not bv_match:
        raise RuntimeError(f"bundleVersion not found on origin/{cfg.integration_branch}")
    integration_bundle_version = bv_match.group(1).strip().strip('"').strip("'")
    new_version = unity_settings._patch_bump(integration_bundle_version)
    release_branch = git_ops.create_release_branch(
        repo_root, version=new_version,
        branch_pattern=cfg.release_branch_pattern,
        base=cfg.integration_branch,
    )

    # ─── Step 2: version bump (now reading develop's actual state) ─────────
    log.info("[2/10] Version bump (read from %s)", cfg.integration_branch)
    current_state = unity_settings.read_current(cfg.project_settings_file)
    next_state = unity_settings.bump_patch(current_state)

    # Store-driven build-number reconciliation.
    # The develop counter can lag behind the stores (manual uploads, CI
    # post-build patchers, hotfixes on other branches). Play/ASC reject any
    # upload whose code is <= a previously used one, so we take:
    #   android_code    = max(local+1, Play_max+1)
    #   ios_build_num   = max(local+1, ASC_max+1)
    # bundleVersion (semver string) stays derived from ProjectSettings —
    # we don't want to accidentally downgrade if develop is intentionally
    # ahead of production (e.g. major version prep on a "3.0" branch).
    try:
        _play_probe = googleplay_client.GooglePlayClient(
            sa_json=config.secret(cfg.secret_names["google_play_sa_json"]),
            package_name=cfg.android_package_name,
        )
        _edit = _play_probe.open_edit()
        try:
            play_max = _play_probe.get_max_version_code(_edit)
            play_max_name = _play_probe.get_max_version_name(_edit)
        finally:
            _play_probe.delete_edit(_edit)
        if play_max + 1 > next_state.android_code:
            log.info("  Play max versionCode (%d) is ahead of develop counter (%d) — snapping android_code to %d",
                     play_max, current_state.android_code, play_max + 1)
            next_state = unity_settings.VersionState(
                bundle_version=next_state.bundle_version,
                android_code=play_max + 1,
                ios_build_number=next_state.ios_build_number,
            )
        # bundleVersion reconciliation against Play
        if unity_settings.semver_gt(play_max_name, next_state.bundle_version):
            snapped = unity_settings._patch_bump(play_max_name)
            log.info("  Play max versionName (%s) is ahead of proposed %s — snapping bundleVersion to %s",
                     play_max_name, next_state.bundle_version, snapped)
            next_state = unity_settings.VersionState(
                bundle_version=snapped,
                android_code=next_state.android_code,
                ios_build_number=next_state.ios_build_number,
            )
    except Exception as e:
        log.warning("Play version probes failed (%s) — falling back to develop counter + 1", e)

    try:
        _asc_probe = appstoreconnect_client.AppStoreConnectClient(
            key_id=config.secret(cfg.secret_names["asc_key_id"]),
            private_key_p8=config.secret(cfg.secret_names["asc_private_key_p8"]),
            app_id=cfg.apple_app_id,
        )
        asc_max = _asc_probe.get_max_build_number()
        asc_max_ver = _asc_probe.get_max_version_string()
        if asc_max + 1 > next_state.ios_build_number:
            log.info("  ASC max buildNumber (%d) is ahead of develop counter (%d) — snapping ios_build_number to %d",
                     asc_max, current_state.ios_build_number, asc_max + 1)
            next_state = unity_settings.VersionState(
                bundle_version=next_state.bundle_version,
                android_code=next_state.android_code,
                ios_build_number=asc_max + 1,
            )
        # bundleVersion reconciliation against ASC
        if unity_settings.semver_gt(asc_max_ver, next_state.bundle_version):
            snapped = unity_settings._patch_bump(asc_max_ver)
            log.info("  ASC max versionString (%s) is ahead of proposed %s — snapping bundleVersion to %s",
                     asc_max_ver, next_state.bundle_version, snapped)
            next_state = unity_settings.VersionState(
                bundle_version=snapped,
                android_code=next_state.android_code,
                ios_build_number=next_state.ios_build_number,
            )
    except Exception as e:
        log.warning("ASC version probes failed (%s) — falling back to develop counter + 1", e)

    new_version = next_state.bundle_version
    log.info("  %s → %s   |   AndroidBundleVersionCode %d → %d   |   iOS build #%d → #%d",
             current_state.bundle_version, next_state.bundle_version,
             current_state.android_code, next_state.android_code,
             current_state.ios_build_number, next_state.ios_build_number)

    # ─── Step 3: write new ProjectSettings ─────────────────────────────────
    log.info("[3/10] Patch ProjectSettings.asset")
    unity_settings.write(cfg.project_settings_file, next_state)

    # ─── Step 4: Scenario workflow ─────────────────────────────────────────
    # Mirrors what Sergio's icons_DribbleHoops workflow does in the Scenario
    # UI: uploads the CURRENT icon as the Reference image, then runs the
    # 3-node chain (LLM JSON-extract → LLM variations → GPT-image-2 with
    # reference) by POSTing each node to Scenario in sequence.
    log.info("[4/10] Run Scenario workflow (LLM→LLM→image-gen) on current icon")
    scenario = scenario_client.ScenarioClient(
        api_key=config.secret(cfg.secret_names["scenario_api_key"]),
        project_id=cfg.scenario["project_id"],
        workflow_id=cfg.scenario["workflow_id"],
        poll_interval_seconds=cfg.scenario["poll_interval_seconds"],
        poll_timeout_minutes=cfg.scenario["poll_timeout_minutes"],
    )
    # Reference image = the largest current icon variant on disk (path comes
    # from the per-game config, derived from icon.paths_to_overwrite).
    # We use the post-Step-3 repo state but the icons haven't been overwritten
    # yet, so this is the LIVE icon shipped in the last release.
    ref_icon = cfg.reference_icon_path
    with tempfile.TemporaryDirectory(prefix="aso-icon-") as tmp:
        master_png = Path(tmp) / f"master_{cfg.icon_master_size}.png"
        scenario.run_workflow_icons(reference_image=ref_icon, destination=master_png)

        # ─── Step 5: resize + overwrite ────────────────────────────────────
        log.info("[5/10] Resize + overwrite icon variants")
        written_paths = icon_processor.overwrite_all_variants(
            repo_root=repo_root,
            master_png=master_png,
            variants=cfg.icon_paths,
        )

    # ─── Step 6: commit ────────────────────────────────────────────────────
    log.info("[6/10] Commit changes")
    commit_msg = (
        f"{cfg.commit_prefix} Monthly ASO update — {new_version}\n\n"
        f"• Icon refreshed via Scenario workflow {cfg.scenario['workflow_id']}\n"
        f"• Version bumped: {current_state.bundle_version} → {new_version}\n"
        f"• Android build code: {current_state.android_code} → {next_state.android_code}\n"
        f"• iOS build number: {current_state.ios_build_number} → {next_state.ios_build_number}\n"
    )
    git_ops.stage_and_commit(
        repo_root,
        paths=[*written_paths, cfg.project_settings_file],
        commit_message=commit_msg,
    )

    if args.dry_run:
        log.warning("DRY RUN — stopping after local commit. Branch %s is ready for review but not pushed.", release_branch)
        return 0

    git_ops.push_branch(repo_root, release_branch)
    state.release_branch_pushed = release_branch

    # ─── Step 7: CircleCI build ────────────────────────────────────────────
    log.info("[7/10] Trigger CircleCI iOS + Android pipelines")
    ci = circleci_trigger.CircleCITrigger(
        token=config.secret(cfg.secret_names["circleci_token"]),
        project_slug=cfg.build["circleci_project"],
        deployer_config_branch=cfg.build["circleci_config_branch"],
        poll_interval_seconds=cfg.build["poll_interval_seconds"],
        poll_timeout_minutes=cfg.build["poll_timeout_minutes"],
    )
    common = {
        "repository_path": cfg.repo_url,
        "repository_branch": release_branch,
        "build_number": str(next_state.ios_build_number),
        "project-version": new_version,
        "upload-comment": f"Monthly ASO update — {new_version}",
        "deployment-type": "release",
        "deploy-build-to-store": cfg.build["deploy_to_store"],
        "is_proxy_build": cfg.build["is_proxy_build"],
        "is-lz4hc": cfg.build["is_lz4hc"],
    }
    ios_pipeline = ci.trigger_ios(common, cfg.ios_bundle_id)
    state.ios_pipeline_id = ios_pipeline
    android_pipeline = ci.trigger_android(common, cfg.android_package_name, cfg.build["android_keystore_name"])
    state.android_pipeline_id = android_pipeline

    log.info("[7/10] Waiting for iOS pipeline %s", ios_pipeline)
    ci.wait_for_pipeline(ios_pipeline)
    log.info("[7/10] Waiting for Android pipeline %s", android_pipeline)
    ci.wait_for_pipeline(android_pipeline)

    # ─── Step 8: ASC version + What's New ──────────────────────────────────
    log.info("[8/10] App Store Connect — version + What's New")
    asc = appstoreconnect_client.AppStoreConnectClient(
        key_id=config.secret(cfg.secret_names["asc_key_id"]),
        private_key_p8=config.secret(cfg.secret_names["asc_private_key_p8"]),
        app_id=cfg.apple_app_id,
    )
    build_id = asc.find_build(version_string=new_version, build_number=str(next_state.ios_build_number))
    state.ios_build_id = build_id
    version_id = asc.create_version(new_version, platform="IOS")
    state.asc_version_id = version_id
    asc.attach_build(version_id, build_id)
    state.asc_attached_build_id = build_id
    locales_ios = [l["attributes"]["locale"] for l in asc.list_localizations(version_id)]
    notes = text_generator.generate_per_locale(cfg.aso_content, locales_ios, repo_root=repo_root)
    for loc_obj in asc.list_localizations(version_id):
        locale = loc_obj["attributes"]["locale"]
        asc.set_whats_new(loc_obj["id"], notes[locale])
    state.asc_submission_id = asc.submit_for_review(version_id)

    # ─── Step 9: Google Play — promote Internal → Production ───────────────
    # CircleCI only uploads the AAB to the Internal track. This step reads
    # that AAB's versionCodes and creates a Production release containing
    # them with the new release notes + 100% rollout, then commits.
    log.info("[9/10] Google Play — promote Internal → Production")
    play = googleplay_client.GooglePlayClient(
        sa_json=config.secret(cfg.secret_names["google_play_sa_json"]),
        package_name=cfg.android_package_name,
    )
    edit_id = play.open_edit()
    try:
        version_codes = play.get_latest_internal_version_codes(edit_id)
        locales_play = play.list_listings_locales(edit_id)
        play_notes = text_generator.generate_per_locale(cfg.aso_content, locales_play, repo_root=repo_root)
        play.promote_internal_to_production(
            edit_id=edit_id,
            version_codes=version_codes,
            notes_per_locale=play_notes,
            release_name=new_version,
            rollout_fraction=1.0,
        )

        # Upload the new icon to the Play Store *listing* (separate asset from
        # the launcher icon baked into the AAB). Play requires 512×512 PNG.
        # Source: the per-game reference icon path (just overwritten by Step 5).
        from PIL import Image as _PILImage
        src_1024 = cfg.reference_icon_path
        with tempfile.TemporaryDirectory(prefix="aso-play-icon-") as itmp:
            store_icon_512 = Path(itmp) / "store_icon_512.png"
            _PILImage.open(src_1024).resize((512, 512), _PILImage.LANCZOS).save(store_icon_512, "PNG")
            for loc in locales_play:
                try:
                    play.upload_store_icon(edit_id, locale=loc, icon_path=store_icon_512)
                except Exception as e:
                    log.warning("Play store icon upload failed for %s: %s — continuing", loc, e)

        play.commit_edit(edit_id)
        state.play_internal_committed = True
    except Exception:
        play.delete_edit(edit_id)
        raise

    # ─── Step 10: merge release back to develop + master ─────────────────
    # PGT convention: once a release has shipped to the stores, the release
    # branch is merged back into both develop (so develop has the bumped
    # version numbers) and master (so master mirrors what's actually live).
    # Best-effort — failure here doesn't roll back the stores, because the
    # release is already live there.
    log.info("[10/11] Merge %s back into develop + master", release_branch)
    try:
        git_ops.merge_release_back(
            repo_root,
            release_branch=release_branch,
            targets=["develop", "master"],
        )
        git_ops.delete_branch(repo_root, release_branch)
    except Exception as e:
        log.warning("Merge-back failed — release branch left behind for manual merge: %s", e)

    # ─── Step 11: done ────────────────────────────────────────────────────
    log.info("[11/11] ✅ ASO automation complete for %s %s", cfg.game_name, new_version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
