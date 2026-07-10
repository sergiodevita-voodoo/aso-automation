"""Opt-in Opus-based version-bump strategy.

Deterministic parsing (regex + max()) handles ~15 of 16 PGT games cleanly.
The outliers are games with unusual conventions the store-drift snap can't
reason about — most notably:

  * Rope and Demolish — repo uses a monotonic integer ("96") while Play ships
    semver ("8.30.1"). Neither is convertible into the other by string logic.
  * Any game whose team scribbles free-form annotations into release names
    (SDK versions, campaign codes, changelog snippets).

Opt-in with ``versioning.strategy: opus`` in the per-game config. When
enabled, this module replaces the deterministic version-bump step in
``run.py`` step 2 with a single Claude Opus call that receives the full
picture (repo state + Play tracks + ASC state + game identity) and returns
the three next-version numbers plus a plain-English rationale.

Deterministic fallback fires on any of:
  * anthropic library import failure
  * ANTHROPIC_API_KEY missing
  * network / timeout / rate-limit error
  * Opus response fails schema validation (three ints + optional string)
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

from scripts.aso.lib import unity_settings

log = logging.getLogger(__name__)


_SYSTEM_PROMPT = """You are a mobile-app release-versioning expert. You will be given the full \
picture of a PGT game's version state — the repo's current ProjectSettings.asset \
values, all releases on Google Play across every track, and Apple App Store's max \
build number and version string when available. Your job is to decide the three \
identifiers that the next monthly ASO release should ship with.

Rules of the road:
* Every new AndroidBundleVersionCode must be strictly greater than any value \
  ever uploaded to Play (across all tracks, including test tracks and archived).
* Every new iOS buildNumber must be strictly greater than any value ever uploaded \
  to App Store Connect (across TestFlight and App Store).
* iOS and Android may have INDEPENDENT version schemes. Look at what each \
  store actually ships and mirror that store's own format:
  - If ASC's max versionString is a monotonic integer (e.g. "109"), the next \
    iOS versionString is the next integer (e.g. "110"). Do NOT introduce dots.
  - If ASC's max versionString is semver (e.g. "5.21.9"), patch-bump it.
  - Play's Android versionName follows the same detection rule independently.
  Real case: RopeAndDemolish ships iOS as monotonic integer ("109" → "110") \
  and Android as semver ("8.31.0" → "8.31.1"). Emitting the Android semver \
  ("8.31.1") for iOS is REJECTED by Apple because "8" is parsed as integer \
  and compared against the existing 109 → 8 < 109 → build stuck in processing.
* Ignore free-form release-note annotations. "52100091 (5.21.9) - new VS 8.8.5" \
  means versionCode 52100091 and versionName 5.21.9; the "VS 8.8.5" is SDK \
  bundle info, not a version.
* Prefer a patch-bump (increment the last semver segment) unless the store \
  clearly shipped a newer version — then jump to that newer version + 1 patch.
* Return ONLY a JSON object matching the schema shown in the user prompt. No \
  narration outside the JSON.
"""


def _build_user_prompt(
    game_name: str,
    game_code: str,
    local: Dict[str, Any],
    play_tracks: List[Dict[str, Any]],
    asc_max_build_number: Optional[int],
    asc_max_version_string: Optional[str],
) -> str:
    """Assemble the JSON-shaped prompt Claude will parse."""
    return (
        f"Game: {game_name} ({game_code})\n\n"
        f"Repo ProjectSettings.asset (integration branch, pre-bump):\n"
        f"  bundleVersion:            {local['bundle_version']!r}\n"
        f"  AndroidBundleVersionCode: {local['android_code']}\n"
        f"  buildNumber.iPhone:       {local['ios_build_number']}\n\n"
        f"Google Play tracks (all releases across track history):\n"
        f"{json.dumps(play_tracks, indent=2, ensure_ascii=False)}\n\n"
        f"Apple App Store Connect:\n"
        f"  max buildNumber:  {asc_max_build_number}\n"
        f"  max versionString: {asc_max_version_string}\n\n"
        f"Return ONLY this JSON, filled in:\n"
        f"{{\n"
        f'  "bundle_version": "<Android versionName — follow Play\'s scheme, semver typical>",\n'
        f'  "ios_version_string": "<iOS CFBundleShortVersionString — follow ASC\'s scheme; integer if ASC uses integers, semver otherwise>",\n'
        f'  "android_code": <int, > every code ever on Play>,\n'
        f'  "ios_build_number": <int, > every buildNumber ever on ASC>,\n'
        f'  "reasoning": "<one-sentence explanation covering both platforms>"\n'
        f"}}\n"
    )


def decide_next_state(
    *,
    game_name: str,
    game_code: str,
    local_state: unity_settings.VersionState,
    play_tracks: List[Dict[str, Any]],
    asc_max_build_number: Optional[int],
    asc_max_version_string: Optional[str],
    model: str = "claude-opus-4-6",
    max_tokens: int = 512,
) -> Optional[unity_settings.VersionState]:
    """Ask Opus for the next version identifiers.

    Returns a ``VersionState`` on success. Returns ``None`` on any failure —
    the caller should fall back to the deterministic path in that case and log
    a warning. This function never raises.
    """
    try:
        from anthropic import Anthropic  # lazy import
    except Exception as e:
        log.warning("Opus versioning: anthropic library unavailable (%s) — falling back to deterministic", e)
        return None

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log.warning("Opus versioning: ANTHROPIC_API_KEY not set — falling back to deterministic")
        return None

    user_prompt = _build_user_prompt(
        game_name=game_name,
        game_code=game_code,
        local={
            "bundle_version": local_state.bundle_version,
            "android_code": local_state.android_code,
            "ios_build_number": local_state.ios_build_number,
        },
        play_tracks=play_tracks,
        asc_max_build_number=asc_max_build_number,
        asc_max_version_string=asc_max_version_string,
    )

    try:
        client = Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
    except Exception as e:
        log.warning("Opus versioning: API call failed (%s) — falling back to deterministic", e)
        return None

    # Extract JSON from the response — Opus sometimes wraps in ``` fences
    text = "".join(b.text for b in resp.content if getattr(b, "text", None))
    parsed = _extract_json(text)
    if parsed is None:
        log.warning("Opus versioning: response was not parseable JSON — falling back to deterministic")
        log.debug("Opus raw response: %s", text[:500])
        return None

    try:
        bv = str(parsed["bundle_version"]).strip()
        ac = int(parsed["android_code"])
        ib = int(parsed["ios_build_number"])
    except (KeyError, TypeError, ValueError) as e:
        log.warning("Opus versioning: response missing required fields (%s) — falling back to deterministic", e)
        return None

    # Optional field — back-compat: if Opus doesn't emit it, iOS reuses bundle_version.
    ios_ver = str(parsed.get("ios_version_string", "")).strip()
    reasoning = parsed.get("reasoning", "")
    log.info("Opus versioning: android=%s ios=%s → code=%d ios=#%d  (reason: %s)",
             bv, ios_ver or bv, ac, ib, reasoning)
    return unity_settings.VersionState(
        bundle_version=bv,
        android_code=ac,
        ios_build_number=ib,
        ios_version_string=ios_ver,
    )


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """Return the first JSON object embedded in ``text``. Tolerates code
    fences and prose around a single ``{...}`` block."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Find the outermost { ... } block
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
