"""Recent-activity guardrail (rule #4).

The automation should NOT push a new build to the stores if the game already
has recent activity — either a build published to any store track (App Store,
TestFlight, Play Prod/Internal/Beta/Alpha) OR any human commit on the
integration branch. Reason: we don't want the monthly icon refresh to step on
an active release cycle (e.g. shipping a hotfix, pending review, mid-QA).

When ``has_recent_activity()`` returns a non-empty dict of signals, the
orchestrator switches to icon-only mode: regenerate + commit + push the new
icon, merge into both integration branch and develop, then STOP. No version
bump, no CI trigger, no ASC submit, no Play promote.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger(__name__)


@dataclass
class ActivitySignal:
    source: str          # "asc" | "git" | "play"
    detail: str          # human-readable description
    when: Optional[datetime] = None


# ─── Store-side probes ──────────────────────────────────────────────────────
def probe_asc_recent(asc_client, days: int) -> Optional[ActivitySignal]:
    """Return an ActivitySignal if the newest ASC build was uploaded within
    ``days`` days. ``asc_client`` is an already-constructed AppStoreConnectClient."""
    try:
        params = {
            "filter[app]": asc_client.app_id,
            "sort": "-uploadedDate",
            "limit": 1,
            "fields[builds]": "uploadedDate,version",
        }
        resp = asc_client._request("GET", "/v1/builds", params=params)
        resp.raise_for_status()
        data = resp.json().get("data", [])
    except Exception as e:
        log.warning("ASC recency probe failed: %s — treating as no signal", e)
        return None
    if not data:
        return None
    attrs = data[0].get("attributes", {})
    uploaded = attrs.get("uploadedDate")
    version = attrs.get("version")
    if not uploaded:
        return None
    try:
        # ASC returns ISO 8601 like "2026-07-13T09:00:00-07:00"
        dt = datetime.fromisoformat(uploaded.replace("Z", "+00:00"))
    except ValueError:
        log.warning("ASC uploadedDate unparseable: %r", uploaded)
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    if dt >= cutoff:
        return ActivitySignal(
            source="asc",
            detail=f"most-recent iOS build #{version} uploaded {uploaded}",
            when=dt,
        )
    return None


def probe_play_recent(play_client, days: int) -> Optional[ActivitySignal]:
    """Return an ActivitySignal if any Play track release has been staged or
    updated within ``days`` days.

    Play's API does not expose per-release timestamps directly. We use the
    workaround of comparing the currently-known-max versionCode against the
    versionCode range we'd expect from ``days`` of monthly-ish releases —
    but the more reliable signal in practice is: if a bundle exists on
    Internal that's above the current Play Production max, that means CI
    ran recently (either us or the game's own release pipeline).
    """
    try:
        edit_id = play_client.open_edit()
    except Exception as e:
        log.warning("Play recency probe: open_edit failed: %s", e)
        return None
    try:
        c = play_client._client()
        # Get Internal + Production releases; if Internal's max versionCode
        # exceeds Production's, someone shipped to Internal since Production
        # was last cut. That's a strong "recent upload" signal.
        tracks = c.edits().tracks().list(
            packageName=play_client.package_name, editId=edit_id,
        ).execute().get("tracks", [])
        prod_max = 0
        internal_max = 0
        for t in tracks:
            name = t.get("track")
            for r in t.get("releases", []):
                for vc in (r.get("versionCodes") or []):
                    try:
                        vc = int(vc)
                    except (TypeError, ValueError):
                        continue
                    if name == "production":
                        prod_max = max(prod_max, vc)
                    elif name == "internal":
                        internal_max = max(internal_max, vc)
        if internal_max > 0 and internal_max > prod_max:
            return ActivitySignal(
                source="play",
                detail=f"Internal track has code {internal_max} > Production max {prod_max} (recent CI upload)",
                when=None,
            )
    except Exception as e:
        log.warning("Play recency probe failed: %s — treating as no signal", e)
    finally:
        try: play_client.delete_edit(edit_id)
        except Exception: pass
    return None


# ─── Git probe ──────────────────────────────────────────────────────────────
_AUTOMATION_AUTHOR_PATTERNS = [
    "github-actions[bot]",
    "github-actions",
]

_AUTOMATION_MSG_PREFIX_PATTERN = re.compile(r"^\s*\[[A-Z]+-AUTO\]")


def probe_git_recent_human_commits(repo_root: Path, branch: str, days: int) -> List[ActivitySignal]:
    """Return an ActivitySignal per human commit on ``branch`` within ``days`` days.
    Filters out commits by the automation itself.
    """
    since = f"{days} days ago"
    try:
        # Ensure we have the remote branch locally
        subprocess.run(
            ["git", "fetch", "origin", branch],
            cwd=repo_root, check=True, capture_output=True,
        )
    except subprocess.CalledProcessError as e:
        log.warning("Git recency probe: fetch %s failed: %s — skipping", branch, e.stderr.decode(errors="replace")[:200])
        return []
    try:
        r = subprocess.run(
            [
                "git", "log", f"origin/{branch}",
                f"--since={since}",
                "--format=%H%x1f%an%x1f%ae%x1f%at%x1f%s",
                "--no-merges",
            ],
            cwd=repo_root, check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as e:
        log.warning("Git recency probe: git log failed: %s — skipping", e.stderr[:200])
        return []
    signals: List[ActivitySignal] = []
    for line in r.stdout.strip().splitlines():
        parts = line.split("\x1f")
        if len(parts) < 5:
            continue
        sha, author_name, author_email, ts_str, subject = parts[:5]
        # Automation filter — author or subject
        if any(pat in author_name for pat in _AUTOMATION_AUTHOR_PATTERNS):
            continue
        if _AUTOMATION_MSG_PREFIX_PATTERN.search(subject):
            continue
        try:
            when = datetime.fromtimestamp(int(ts_str), tz=timezone.utc)
        except ValueError:
            when = None
        signals.append(ActivitySignal(
            source="git",
            detail=f"commit {sha[:8]} by {author_name}: {subject[:80]}",
            when=when,
        ))
    return signals


# ─── Master probe ───────────────────────────────────────────────────────────
def has_recent_activity(
    *,
    cfg,
    repo_root: Path,
    days: int,
    asc_client=None,
    play_client=None,
) -> Dict[str, List[ActivitySignal]]:
    """Run every probe. Returns ``{source: [signals]}`` — empty dict means no
    recent activity → safe to run the full ASO pipeline.

    Callers should treat any non-empty return as "trigger icon-only mode".
    Non-terminal probe failures don't count as signals (they're logged and
    ignored) so a transient API blip doesn't spuriously downgrade the run.
    """
    signals: Dict[str, List[ActivitySignal]] = {}

    # 1. Git — human commits on integration branch
    git_signals = probe_git_recent_human_commits(repo_root, cfg.integration_branch, days)
    if git_signals:
        signals["git"] = git_signals

    # 2. ASC — recent build upload
    if asc_client is not None:
        asc_signal = probe_asc_recent(asc_client, days)
        if asc_signal is not None:
            signals["asc"] = [asc_signal]

    # 3. Play — bundle above Production max (recent CI upload)
    if play_client is not None:
        play_signal = probe_play_recent(play_client, days)
        if play_signal is not None:
            signals["play"] = [play_signal]

    return signals


def format_signals(signals: Dict[str, List[ActivitySignal]]) -> str:
    """Pretty-print signals for the run log."""
    if not signals:
        return "(none)"
    parts = []
    for source, sigs in signals.items():
        for s in sigs:
            when = s.when.isoformat() if s.when else "?"
            parts.append(f"  [{source}] {s.detail} (when={when})")
    return "\n".join(parts)
