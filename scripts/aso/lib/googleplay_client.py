"""Google Play Developer API client.

After CircleCI uploads an .aab to the Play Internal track, this module:
1. Opens an edit on the package
2. Sets the release notes ("recentChanges") per locale on the Production track
3. Promotes the Production track release to 100% rollout
4. Commits the edit

Auth uses the dedicated PGT service account
(``icon-updater@pgt-icon-update.iam.gserviceaccount.com``) granted Release
Manager role per game in Play Console.

Reference: https://developers.google.com/android-publisher
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
from tenacity import retry, stop_after_attempt, wait_exponential

log = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/androidpublisher"]


@dataclass
class GooglePlayClient:
    sa_json: str             # Full service account JSON contents (string form)
    package_name: str        # e.g. "com.wildbeep.dribblehoops"

    _service: Any = None

    def _client(self) -> Any:
        if self._service is None:
            sa_info = json.loads(self.sa_json) if isinstance(self.sa_json, str) else self.sa_json
            creds = service_account.Credentials.from_service_account_info(sa_info, scopes=_SCOPES)
            self._service = build("androidpublisher", "v3", credentials=creds, cache_discovery=False)
        return self._service

    # ── Edits ─────────────────────────────────────────────────────────────
    @retry(stop=stop_after_attempt(5), wait=wait_exponential(min=2, max=15))
    def open_edit(self) -> str:
        edit = self._client().edits().insert(packageName=self.package_name, body={}).execute()
        log.info("Play edit opened: %s", edit["id"])
        return edit["id"]

    @retry(stop=stop_after_attempt(5), wait=wait_exponential(min=2, max=15))
    def commit_edit(self, edit_id: str) -> None:
        self._client().edits().commit(packageName=self.package_name, editId=edit_id).execute()
        log.info("Play edit %s committed", edit_id)

    def delete_edit(self, edit_id: str) -> None:
        """Discard the edit. Useful in dry-run / error paths."""
        try:
            self._client().edits().delete(packageName=self.package_name, editId=edit_id).execute()
            log.info("Play edit %s discarded", edit_id)
        except HttpError as e:
            log.warning("Play edit delete returned %s — continuing", e.resp.status)

    # ── Listings & track info ─────────────────────────────────────────────
    def list_listings_locales(self, edit_id: str) -> List[str]:
        listings = self._client().edits().listings().list(
            packageName=self.package_name, editId=edit_id,
        ).execute()
        return [l["language"] for l in listings.get("listings", [])]

    def get_track(self, edit_id: str, track: str) -> Dict[str, Any]:
        return self._client().edits().tracks().get(
            packageName=self.package_name, editId=edit_id, track=track,
        ).execute()

    # ── Internal → Production promotion ───────────────────────────────────
    def get_max_version_code(self, edit_id: str) -> int:
        """Return the highest AAB versionCode ever uploaded across ALL tracks
        (production, beta, alpha, internal, plus any custom closed tracks).

        Play rejects an AAB whose versionCode is <= any previously uploaded
        code, so we use this as the source of truth for the next code to write
        into ProjectSettings.asset. Any drift between develop's local counter
        and what's on Play (from manual uploads, CI post-build patchers,
        parallel release branches, or hotfixes) is transparently absorbed.
        """
        client = self._client()
        tracks = client.edits().tracks().list(packageName=self.package_name, editId=edit_id).execute().get("tracks", [])
        codes: List[int] = []
        for t in tracks:
            for r in t.get("releases", []):
                for c in (r.get("versionCodes") or []):
                    try:
                        codes.append(int(c))
                    except (TypeError, ValueError):
                        pass
        if not codes:
            log.warning("Play returned no versionCodes across any track — the app has never had an AAB uploaded")
            return 0
        m = max(codes)
        log.info("Play max versionCode across all tracks = %d", m)
        return m

    def get_max_version_name(self, edit_id: str) -> str:
        """Return the highest release *name* (versionName) ever set across ALL
        tracks. Parsed as semver; ties broken by string comparison.

        Play's ``release.name`` shape varies — sometimes ``"2.9.53"``,
        sometimes ``"111 (2.9.17)"``. Both parse cleanly via
        ``unity_settings._parse_semver``.
        """
        from scripts.aso.lib import unity_settings  # local import — avoid circular
        client = self._client()
        tracks = client.edits().tracks().list(packageName=self.package_name, editId=edit_id).execute().get("tracks", [])
        names = []
        for t in tracks:
            for r in t.get("releases", []):
                n = r.get("name")
                if n:
                    names.append(n)
        if not names:
            return "0.0.0"
        # Sort by parsed semver tuple, take the last (highest)
        names.sort(key=lambda n: unity_settings._parse_semver(n))
        best = names[-1]
        canonical = unity_settings.format_semver(best)
        log.info("Play max release name across all tracks = %r → semver %s", best, canonical)
        return canonical

    def get_latest_internal_version_codes(self, edit_id: str) -> List[int]:
        """Return the version codes of the latest release on the Internal track.

        CircleCI uploads each new AAB to the Internal track. To promote to
        Production we read the most recent Internal release and pass its
        ``versionCodes`` through to the new Production release.
        """
        track = self.get_track(edit_id, "internal")
        releases = track.get("releases", [])
        if not releases:
            raise RuntimeError("Play Internal track has no releases — did CircleCI upload the AAB?")
        version_codes = releases[0].get("versionCodes") or []
        if not version_codes:
            raise RuntimeError("Latest Internal release has no versionCodes — corrupted upload?")
        log.info("Play Internal latest release version codes: %s", version_codes)
        return [int(v) for v in version_codes]

    def promote_internal_to_production(
        self,
        edit_id: str,
        version_codes: List[int],
        notes_per_locale: Dict[str, str],
        release_name: str,
        rollout_fraction: float = 1.0,
    ) -> None:
        """Create a Production release containing ``version_codes`` with the given
        release notes and rollout fraction.

        ``rollout_fraction`` is 0.0–1.0. Default 1.0 (= ``status: "completed"``)
        means immediate 100% rollout. Anything less than 1.0 stages a partial
        rollout (``status: "inProgress"``).
        """
        if rollout_fraction >= 1.0:
            release: Dict[str, Any] = {"status": "completed"}
        else:
            release = {"status": "inProgress", "userFraction": rollout_fraction}

        release["name"] = release_name
        release["versionCodes"] = [str(v) for v in version_codes]
        release["releaseNotes"] = [
            {"language": locale, "text": text} for locale, text in notes_per_locale.items()
        ]

        # Replace any existing draft on Production with our new release.
        body = {"track": "production", "releases": [release]}
        self._client().edits().tracks().update(
            packageName=self.package_name, editId=edit_id, track="production", body=body,
        ).execute()
        log.info(
            "Play Production: promoted versionCodes=%s with name=%s rollout=%.2f locales=%s",
            version_codes, release_name, rollout_fraction, list(notes_per_locale),
        )

    # ── Store Listing icon ────────────────────────────────────────────────
    def upload_store_icon(self, edit_id: str, locale: str, icon_path: Path) -> Dict[str, Any]:
        """Replace the Play Store *listing* icon for ``locale``.

        On Android the icon shown on the Play Store page is uploaded
        separately from the AAB's launcher icon — this writes that asset.
        Play requires a 512x512 PNG. Caller is responsible for resizing.

        Deletes the existing icon first (Play won't overwrite) then uploads.
        """
        # Delete existing
        self._client().edits().images().deleteall(
            packageName=self.package_name, editId=edit_id,
            language=locale, imageType="icon",
        ).execute()
        # Upload new
        media = MediaFileUpload(str(icon_path), mimetype="image/png", resumable=False)
        res = self._client().edits().images().upload(
            packageName=self.package_name, editId=edit_id,
            language=locale, imageType="icon",
            media_body=media,
        ).execute()
        log.info("Play Store Listing icon updated for locale=%s (sha1=%s…)",
                 locale, res.get("image", {}).get("sha1", "?")[:16])
        return res
