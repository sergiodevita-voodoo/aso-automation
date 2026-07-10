"""App Store Connect API client (individual API key auth).

Used to:
1. Wait for the new build to be processed by Apple
2. Create a new App Store version record tied to that build
3. Set the ``whatsNew`` text on each locale of the new version
4. Submit the version for App Store review (with auto-release on approval)

JWT format for individual API keys (LEARNED THE HARD WAY — see secrets cache):

    Header: { "alg": "ES256", "kid": <KEY_ID>, "typ": "JWT" }
    Payload: {
        "iss": <KEY_ID>,            # ← Key ID, not a separate Issuer ID
        "sub": "user",              # ← literal string "user" — required for individual keys
        "iat": <now>,
        "exp": <now + 1200>,        # 20 min max per Apple's spec
        "aud": "appstoreconnect-v1"
    }

Reference: https://developer.apple.com/documentation/appstoreconnectapi
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import jwt
import requests
from tenacity import retry, stop_after_attempt, wait_exponential


# Emoji code-point blocks Apple's "What's New" field rejects. The Play Store
# accepts the same characters, so we only strip when sending to ASC.
# Reference: confirmed by direct ASC API testing (codes ⚡, 🔥, 🏀 all rejected
# with ENTITY_ERROR.ATTRIBUTE.INVALID.INVALID_CHARACTERS).
_EMOJI_RE = re.compile(
    "["
    "\U0001F600-\U0001F64F"   # emoticons
    "\U0001F300-\U0001F5FF"   # symbols & pictographs
    "\U0001F680-\U0001F6FF"   # transport & map
    "\U0001F700-\U0001F77F"   # alchemical
    "\U0001F780-\U0001F7FF"   # geometric extended
    "\U0001F800-\U0001F8FF"   # supplemental arrows
    "\U0001F900-\U0001F9FF"   # supplemental symbols & pictographs
    "\U0001FA00-\U0001FA6F"   # chess / extended
    "\U0001FA70-\U0001FAFF"   # symbols and pictographs extended-A
    "\U0001F1E0-\U0001F1FF"   # flags (regional indicators)
    "☀-⛿"           # misc symbols (★ ☀ ⚡ etc.)
    "✀-➿"           # dingbats
    "⌀-⏿"           # misc technical (⏰ etc.)
    "⬀-⯿"           # misc symbols & arrows
    "]+",
    flags=re.UNICODE,
)


def _strip_emojis(text: str) -> str:
    """Remove emoji code points from ``text``. Used to sanitise the
    Claude-generated "What's New" before sending it to ASC."""
    return _EMOJI_RE.sub("", text)

log = logging.getLogger(__name__)

_API_BASE = "https://api.appstoreconnect.apple.com"
_TOKEN_LIFETIME_SECONDS = 1200          # Apple caps at 20 min
_TOKEN_REFRESH_MARGIN_SECONDS = 60      # Refresh 1 min before expiry


@dataclass
class AppStoreConnectClient:
    key_id: str
    private_key_p8: str      # Full PEM contents incl. BEGIN/END markers
    app_id: str              # Numeric App Store Connect app ID

    _token: Optional[str] = None
    _token_expires_at: int = 0

    # ── Auth ──────────────────────────────────────────────────────────────
    def _bearer(self) -> str:
        now = int(time.time())
        if not self._token or now >= self._token_expires_at - _TOKEN_REFRESH_MARGIN_SECONDS:
            self._token = self._mint_token(now)
            self._token_expires_at = now + _TOKEN_LIFETIME_SECONDS
        return self._token

    def _mint_token(self, now: int) -> str:
        payload = {
            "iss": self.key_id,
            "sub": "user",
            "iat": now,
            "exp": now + _TOKEN_LIFETIME_SECONDS,
            "aud": "appstoreconnect-v1",
        }
        headers = {"alg": "ES256", "kid": self.key_id, "typ": "JWT"}
        return jwt.encode(payload, self.private_key_p8, algorithm="ES256", headers=headers)

    @retry(stop=stop_after_attempt(5), wait=wait_exponential(min=2, max=15))
    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        url = f"{_API_BASE}{path}"
        headers = kwargs.pop("headers", {}) or {}
        headers["Authorization"] = f"Bearer {self._bearer()}"
        headers.setdefault("Content-Type", "application/json")
        resp = requests.request(method, url, headers=headers, timeout=30, **kwargs)
        if resp.status_code >= 500:
            raise RuntimeError(f"ASC {method} {path} → {resp.status_code}")
        return resp

    # ── Build lookup ──────────────────────────────────────────────────────
    def get_max_build_number(self) -> int:
        """Return the highest buildNumber ever uploaded to this app in ASC
        across EVERY state — TestFlight beta (active + expired), App Store
        production, in-review, withdrawn, rejected, prerelease — and EVERY
        versionString.

        ASC rejects a build whose ``version`` (buildNumber) is <= any
        previously uploaded build under the same CFBundleShortVersionString,
        AND fastlane's ``latest_testflight_build_number`` sees ALL TF builds
        regardless of state. So we treat this "max across everything" as the
        source of truth for the next buildNumber to inject into the trigger.

        Absorbs drift from manual uploads, parallel release branches, and CI
        post-build patchers. Fully paginated — the previous single-page probe
        (max limit=200) undercounted apps with >200 TF builds and missed the
        true max (real case: RopeAndDemolish returned 80 while fastlane saw 98).
        """
        params = {
            "filter[app]": self.app_id,
            "limit": 200,
            "fields[builds]": "version,uploadedDate,expired,processingState",
            # No filter[state] / filter[processingState] → returns every state.
            # No sort filter → paginate the whole set; sorting is per-page anyway
            # (version is a string; "9" > "11" lexicographic), so we compute the
            # numeric max after collecting everything.
        }
        all_versions: List[int] = []
        page_count = 0
        total_rows = 0
        next_path: Optional[str] = None
        while True:
            page_count += 1
            if next_path is not None:
                resp = self._request("GET", next_path)
            else:
                resp = self._request("GET", "/v1/builds", params=params)
            resp.raise_for_status()
            body = resp.json()
            builds = body.get("data", [])
            total_rows += len(builds)
            for b in builds:
                v = b.get("attributes", {}).get("version")
                try:
                    all_versions.append(int(v))
                except (TypeError, ValueError):
                    pass
            # ASC pagination: links.next is a full URL — strip host to reuse _request
            next_url = (body.get("links") or {}).get("next")
            if not next_url:
                break
            next_path = next_url.replace(_API_BASE, "")
            if page_count >= 100:
                log.warning("ASC pagination hit 100-page safety cap (>20k builds) — may under-count")
                break
        if not all_versions:
            log.warning("ASC returned no parseable numeric buildNumbers for app %s (%d rows / %d pages)",
                        self.app_id, total_rows, page_count)
            return 0
        m = max(all_versions)
        log.info("ASC max buildNumber across ALL states/versions = %d (scanned %d builds over %d pages)",
                 m, total_rows, page_count)
        return m

    def get_max_version_string(self) -> str:
        """Return the highest versionString ever seen for this app.

        Uses ``/v1/builds?include=preReleaseVersion,appStoreVersion`` — which
        only requires ``builds.read`` scope (the paginated buildNumber probe
        already uses it successfully). This bypasses the older
        ``/v1/appStoreVersions`` endpoint that many ASC keys 403 on due to
        missing ``appStoreVersions.read`` scope. Falls back to the old
        endpoint if the builds+include path returns nothing.

        The returned string is the RAW versionString as ASC stores it —
        e.g. ``"109"`` for apps on monotonic-integer schemes, ``"8.31.0"``
        for apps on semver. Callers use ``unity_settings.detect_and_bump()``
        to figure out the scheme and produce the next value; DO NOT
        canonicalize as semver here or integer schemes get lost.
        """
        # Path B (preferred): walk /v1/builds + included relationships.
        # We already page through /v1/builds in get_max_build_number, but
        # that fetch strips relationships. Do a fresh pass here with
        # include= so the "included" array carries the versionStrings.
        seen_strings: List[str] = []
        params = {
            "filter[app]": self.app_id,
            "limit": 200,
            "include": "preReleaseVersion,appStoreVersion",
            "fields[builds]": "version",
            "fields[preReleaseVersions]": "version",
            "fields[appStoreVersions]": "versionString",
        }
        page_count = 0
        next_path: Optional[str] = None
        try:
            while True:
                page_count += 1
                if next_path is not None:
                    resp = self._request("GET", next_path)
                else:
                    resp = self._request("GET", "/v1/builds", params=params)
                resp.raise_for_status()
                body = resp.json()
                for inc in body.get("included", []):
                    t = inc.get("type")
                    attr = inc.get("attributes", {}) or {}
                    if t == "preReleaseVersions":
                        v = attr.get("version")
                        if v: seen_strings.append(str(v))
                    elif t == "appStoreVersions":
                        v = attr.get("versionString")
                        if v: seen_strings.append(str(v))
                next_url = (body.get("links") or {}).get("next")
                if not next_url or page_count >= 100: break
                next_path = next_url.replace(_API_BASE, "")
        except Exception as e:
            log.warning("ASC versionString probe via /v1/builds+include failed (%s) — trying legacy /v1/appStoreVersions", e)

        if seen_strings:
            # Deduplicate + pick the one with the largest numeric-tuple representation.
            from scripts.aso.lib import unity_settings  # local import — avoid circular
            unique = list(dict.fromkeys(seen_strings))
            # For sorting: integers sort numerically, semvers sort by parsed tuple. Convert integer strings
            # to their numeric value; semvers get parsed via _parse_semver. Mixed pool → sort keeps ordering
            # within each family, and callers pick per-scheme.
            def sort_key(s: str):
                if re.match(r"^\d+$", s.strip()):
                    return (0, int(s.strip()))
                return (1, unity_settings._parse_semver(s))
            unique.sort(key=sort_key)
            best = unique[-1]
            log.info("ASC max versionString across %d builds+included (page-scanned %d) = %r (%d unique strings seen)",
                     len(seen_strings), page_count, best, len(unique))
            return best

        # Path A (legacy fallback): the old /v1/appStoreVersions endpoint.
        params = {
            "filter[app]": self.app_id,
            "limit": 200,
            "fields[appStoreVersions]": "versionString",
        }
        try:
            resp = self._request("GET", "/v1/appStoreVersions", params=params)
            resp.raise_for_status()
            versions = resp.json().get("data", [])
            strings = [v.get("attributes", {}).get("versionString") for v in versions if v.get("attributes", {}).get("versionString")]
        except Exception as e:
            log.warning("ASC legacy versionString probe also failed (%s)", e)
            return "0.0.0"
        if not strings:
            log.warning("ASC returned no appStoreVersions for app %s (both probes empty)", self.app_id)
            return "0.0.0"
        from scripts.aso.lib import unity_settings  # local import
        strings.sort(key=lambda s: unity_settings._parse_semver(s))
        best = strings[-1]
        log.info("ASC max versionString (legacy path) across all versions = %r", best)
        return best

    def find_build(self, version_string: str, build_number: str, poll_timeout_minutes: int = 60, poll_interval_seconds: int = 60) -> str:
        """Poll until the matching uploaded build is visible in ASC, returns its build id.

        After CircleCI uploads an .ipa to TestFlight, Apple typically takes
        5–15 min to process it before it's queryable via the API.
        """
        deadline = time.monotonic() + (poll_timeout_minutes * 60)
        params = {
            "filter[app]": self.app_id,
            "filter[preReleaseVersion.version]": version_string,
            "filter[version]": build_number,
            "limit": 1,
        }
        while time.monotonic() < deadline:
            resp = self._request("GET", "/v1/builds", params=params)
            if resp.status_code == 200:
                data = resp.json().get("data", [])
                if data:
                    build_id = data[0]["id"]
                    log.info("ASC found build %s for version %s (#%s)", build_id, version_string, build_number)
                    return build_id
            log.debug("ASC build not yet visible — version=%s build=%s", version_string, build_number)
            time.sleep(poll_interval_seconds)
        raise TimeoutError(f"ASC build {version_string} (#{build_number}) not found within {poll_timeout_minutes} min")

    # ── Version record ────────────────────────────────────────────────────
    def create_version(self, version_string: str, platform: str = "IOS",
                       release_type: str = "AFTER_APPROVAL") -> str:
        """Create a new appStoreVersions record (or return existing). Returns version ID.

        ``release_type=AFTER_APPROVAL`` means Apple will auto-release the version
        as soon as Review approves it — no manual "Release this version" click.
        For existing versions the release type is reconciled via PATCH so prior
        runs that left it on MANUAL get fixed.
        """
        # Check if a version already exists in editable state
        resp = self._request(
            "GET",
            f"/v1/apps/{self.app_id}/appStoreVersions",
            params={
                "filter[versionString]": version_string,
                "filter[platform]": platform,
                "limit": 1,
            },
        )
        existing = resp.json().get("data") if resp.status_code == 200 else []
        if existing:
            vid = existing[0]["id"]
            log.info("ASC version %s already exists — id=%s", version_string, vid)
            # Reconcile releaseType — only if the version is still editable.
            current_rt = existing[0]["attributes"].get("releaseType")
            current_state = existing[0]["attributes"].get("appStoreState")
            if current_rt != release_type and current_state == "PREPARE_FOR_SUBMISSION":
                self._patch_release_type(vid, release_type)
            return vid

        body = {
            "data": {
                "type": "appStoreVersions",
                "attributes": {
                    "versionString": version_string,
                    "platform": platform,
                    "releaseType": release_type,
                },
                "relationships": {"app": {"data": {"type": "apps", "id": self.app_id}}},
            }
        }
        resp = self._request("POST", "/v1/appStoreVersions", data=json.dumps(body))
        if resp.status_code != 201:
            raise RuntimeError(f"ASC create_version failed {resp.status_code}: {resp.text[:300]}")
        vid = resp.json()["data"]["id"]
        log.info("ASC created version %s (releaseType=%s) — id=%s", version_string, release_type, vid)
        return vid

    def _patch_release_type(self, version_id: str, release_type: str) -> None:
        body = {"data": {"type": "appStoreVersions", "id": version_id,
                         "attributes": {"releaseType": release_type}}}
        r = self._request("PATCH", f"/v1/appStoreVersions/{version_id}", data=json.dumps(body))
        if r.status_code not in (200, 204):
            log.warning("ASC patch releaseType→%s failed %s: %s", release_type, r.status_code, r.text[:200])
        else:
            log.info("ASC version %s releaseType set to %s", version_id, release_type)

    def attach_build(self, version_id: str, build_id: str) -> None:
        body = {"data": {"type": "builds", "id": build_id}}
        resp = self._request(
            "PATCH",
            f"/v1/appStoreVersions/{version_id}/relationships/build",
            data=json.dumps(body),
        )
        if resp.status_code not in (200, 204):
            raise RuntimeError(f"ASC attach_build failed {resp.status_code}: {resp.text[:300]}")
        log.info("ASC build %s attached to version %s", build_id, version_id)

    # ── Localizations ─────────────────────────────────────────────────────
    def list_localizations(self, version_id: str) -> List[Dict[str, Any]]:
        resp = self._request(
            "GET",
            f"/v1/appStoreVersions/{version_id}/appStoreVersionLocalizations",
            params={"limit": 200},
        )
        resp.raise_for_status()
        return resp.json().get("data", [])

    def set_whats_new(self, localization_id: str, whats_new: str) -> None:
        # Apple's App Store "What's New" field rejects emojis (confirmed by
        # direct ASC API testing: ⚡, 🔥, 🏀 all return 409 INVALID_CHARACTERS).
        # Strip them before sending. Em-dash, curly quotes, accented characters
        # all work fine, so we only filter the emoji code-point blocks.
        clean = _strip_emojis(whats_new)
        # Collapse any blank lines + strip the leading space each stripped
        # emoji left behind (e.g. "🏀 Fresh look" → " Fresh look" → "Fresh look").
        clean = "\n".join(line.strip() for line in clean.splitlines() if line.strip())
        if clean != whats_new:
            log.info("ASC set_whats_new: stripped emojis (%d → %d chars)", len(whats_new), len(clean))

        body = {
            "data": {
                "type": "appStoreVersionLocalizations",
                "id": localization_id,
                "attributes": {"whatsNew": clean},
            }
        }
        resp = self._request(
            "PATCH",
            f"/v1/appStoreVersionLocalizations/{localization_id}",
            data=json.dumps(body),
        )
        if resp.status_code not in (200, 204):
            raise RuntimeError(f"ASC set_whats_new failed {resp.status_code}: {resp.text[:600]}")

    # ── Submission (uses the new reviewSubmissions flow) ──────────────────
    # Apple deprecated the per-version `appStoreVersionSubmissions` endpoint.
    # The new account-level flow is a 3-step dance:
    #   1. POST /v1/reviewSubmissions (per-app, per-platform "draft envelope")
    #   2. POST /v1/reviewSubmissionItems (attach our appStoreVersion to it)
    #   3. PATCH /v1/reviewSubmissions/{id} with submitted=true to send it
    #
    # The "draft envelope" can already exist if a previous run created it and
    # then died before PATCH — we look for an in-progress one first and reuse
    # it instead of creating a duplicate.
    def submit_for_review(self, version_id: str) -> str:
        """Submit ``version_id`` to App Store Review via the new reviewSubmissions flow.

        Returns the reviewSubmission id (account-level, NOT per-version).
        """
        # 1. Reuse an in-progress submission if one exists for this app+platform.
        r = self._request(
            "GET", "/v1/reviewSubmissions",
            params={
                "filter[app]": self.app_id,
                "filter[platform]": "IOS",
                "filter[state]": "READY_FOR_REVIEW,WAITING_FOR_REVIEW",
                "limit": 1,
            },
        )
        existing = r.json().get("data", []) if r.status_code == 200 else []
        if existing:
            sub_id = existing[0]["id"]
            state = existing[0]["attributes"].get("state")
            log.info("ASC reviewSubmission already exists for app %s — id=%s state=%s", self.app_id, sub_id, state)
            # If it's already WAITING_FOR_REVIEW, nothing to do
            if state == "WAITING_FOR_REVIEW":
                return sub_id
            # Otherwise it's READY_FOR_REVIEW — submit it
            return self._patch_submission_submitted(sub_id)

        # 2. Create a new draft envelope
        create_body = {
            "data": {
                "type": "reviewSubmissions",
                "attributes": {"platform": "IOS"},
                "relationships": {"app": {"data": {"type": "apps", "id": self.app_id}}},
            }
        }
        r = self._request("POST", "/v1/reviewSubmissions", data=json.dumps(create_body))
        if r.status_code != 201:
            raise RuntimeError(f"ASC reviewSubmission create failed {r.status_code}: {r.text[:300]}")
        sub_id = r.json()["data"]["id"]
        log.info("ASC reviewSubmission created — id=%s", sub_id)

        # 3. Attach the appStoreVersion as an item
        item_body = {
            "data": {
                "type": "reviewSubmissionItems",
                "relationships": {
                    "appStoreVersion": {"data": {"type": "appStoreVersions", "id": version_id}},
                    "reviewSubmission": {"data": {"type": "reviewSubmissions", "id": sub_id}},
                },
            }
        }
        r = self._request("POST", "/v1/reviewSubmissionItems", data=json.dumps(item_body))
        if r.status_code != 201:
            raise RuntimeError(f"ASC reviewSubmissionItem add failed {r.status_code}: {r.text[:300]}")
        log.info("ASC reviewSubmission %s: version %s attached", sub_id, version_id)

        # 4. PATCH submitted=true
        return self._patch_submission_submitted(sub_id)

    def _patch_submission_submitted(self, sub_id: str) -> str:
        body = {"data": {"type": "reviewSubmissions", "id": sub_id, "attributes": {"submitted": True}}}
        r = self._request("PATCH", f"/v1/reviewSubmissions/{sub_id}", data=json.dumps(body))
        if r.status_code not in (200, 204):
            raise RuntimeError(f"ASC reviewSubmission submit failed {r.status_code}: {r.text[:500]}")
        log.info("ASC reviewSubmission %s submitted to Apple Review", sub_id)
        return sub_id
