"""Read and mutate Unity's ``ProjectSettings/ProjectSettings.asset``.

The file is YAML-shaped but Unity uses tags that PyYAML can't safely round-trip
(custom tags like ``!u!129 &1`` on the document header, and the file is
extremely position-sensitive — Unity will re-import the project when the
file mtime changes). To stay safe we do **targeted line edits** by regex,
preserving everything else byte-for-byte.

Only three fields are mutated by the automation:
- ``bundleVersion``                  → patch-bumped (e.g. 4.17 → 4.17.1)
- ``buildNumber.iPhone`` (sub-key)   → incremented by 1
- ``AndroidBundleVersionCode``        → incremented by 1
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class VersionState:
    bundle_version: str          # Android versionName / Unity shared bundleVersion (semver typical, e.g. "4.17.1")
    android_code: int            # Play versionCode (e.g. 68)
    ios_build_number: int        # ASC buildNumber (e.g. 1)
    # iOS CFBundleShortVersionString, kept separate because some apps ship
    # iOS with a MONOTONIC INTEGER scheme (e.g. RopeAndDemolish: "109" → "110")
    # while shipping Android with SEMVER (e.g. "8.31.0" → "8.31.1"). Empty
    # string means "same as bundle_version" (back-compat for games where iOS
    # and Android share the semver scheme — the majority).
    ios_version_string: str = ""


# Regexes anchored to the indentation Unity actually uses, to avoid matching
# similar keys in nested blocks.
_BUNDLE_VERSION_RE = re.compile(r"^(\s*bundleVersion:\s*)(.+?)\s*$", re.MULTILINE)
_ANDROID_CODE_RE = re.compile(r"^(\s*AndroidBundleVersionCode:\s*)(-?\d+)\s*$", re.MULTILINE)
_BUILD_NUMBER_BLOCK_RE = re.compile(
    r"^(\s*buildNumber:\s*\n(?:\s+[^\n]+\n)*?)(\s+iPhone:\s*)(\d+)(\s*\n)",
    re.MULTILINE,
)


def read_current(project_settings_path: Path) -> VersionState:
    """Parse the current bundleVersion / AndroidBundleVersionCode / buildNumber.iPhone.

    Tolerates Unity's two-segment shorthand (e.g. ``4.17``) by treating it as
    ``4.17.0`` for semver purposes — the file content stays ``4.17`` until the
    next patch bump writes ``4.17.1``.
    """
    text = project_settings_path.read_text(encoding="utf-8")

    bv_match = _BUNDLE_VERSION_RE.search(text)
    if not bv_match:
        raise ValueError(f"bundleVersion not found in {project_settings_path}")
    bundle_version = bv_match.group(2).strip().strip('"').strip("'")

    code_match = _ANDROID_CODE_RE.search(text)
    if not code_match:
        raise ValueError(f"AndroidBundleVersionCode not found in {project_settings_path}")
    android_code = int(code_match.group(2))

    bn_match = _BUILD_NUMBER_BLOCK_RE.search(text)
    if not bn_match:
        raise ValueError(f"buildNumber.iPhone not found in {project_settings_path}")
    ios_build_number = int(bn_match.group(3))

    return VersionState(bundle_version=bundle_version, android_code=android_code, ios_build_number=ios_build_number)


def bump_patch(current: VersionState) -> VersionState:
    """Return a new VersionState with bundleVersion patch-bumped and both build numbers +1.

    Semver rules:
    - "4.17"      → "4.17.1"     (two-segment → treat as .0 implicitly, write .1)
    - "4.17.0"    → "4.17.1"
    - "4.17.5"    → "4.17.6"
    - "4"         → "4.0.1"      (degenerate, but handled)
    """
    return VersionState(
        bundle_version=_patch_bump(current.bundle_version),
        android_code=current.android_code + 1,
        ios_build_number=current.ios_build_number + 1,
    )


def write(project_settings_path: Path, next_state: VersionState) -> None:
    """Write the three mutations back to the file. Idempotent if values already match."""
    text = project_settings_path.read_text(encoding="utf-8")

    text, n1 = _BUNDLE_VERSION_RE.subn(rf"\g<1>{next_state.bundle_version}", text, count=1)
    if n1 != 1:
        raise ValueError("Failed to substitute bundleVersion")

    text, n2 = _ANDROID_CODE_RE.subn(rf"\g<1>{next_state.android_code}", text, count=1)
    if n2 != 1:
        raise ValueError("Failed to substitute AndroidBundleVersionCode")

    text, n3 = _BUILD_NUMBER_BLOCK_RE.subn(
        rf"\g<1>\g<2>{next_state.ios_build_number}\g<4>", text, count=1
    )
    if n3 != 1:
        raise ValueError("Failed to substitute buildNumber.iPhone")

    project_settings_path.write_text(text, encoding="utf-8")
    log.info(
        "ProjectSettings.asset updated → bundleVersion=%s, AndroidBundleVersionCode=%d, buildNumber.iPhone=%d",
        next_state.bundle_version, next_state.android_code, next_state.ios_build_number,
    )


def detect_and_bump(current_str: str) -> str:
    """Detect the version scheme of ``current_str`` and return the next value.

    Handles:
      "8.31.0"           → "8.31.1"     (semver)
      "5.21.9"           → "5.21.10"    (semver, multi-digit patch)
      "4.17"             → "4.17.1"     (short semver — treated as .0 implicit)
      "109"              → "110"        (monotonic integer)
      "2470001 (2.4.7)"  → "2.4.8"      (Play canonical: prefer parenthesized semver)
      "52100091 (5.21.9) - new VS 8.8.5" → "5.21.10"  (annotations trail parens)

    Raises ``ValueError`` if the string matches none of the shapes above —
    callers must handle this (e.g. fall back to local bundleVersion).

    The whole point of this helper is to let iOS and Android bump
    independently based on what each store SHIPS, not on a per-game config
    declaration. Rope's iOS ASC shows "109" → we emit "110"; its Play shows
    "8.31.0" → we emit "8.31.1". Every other game defaults to semver on both
    stores and gets the same behaviour as before.
    """
    s = (current_str or "").strip().strip('"').strip("'")
    if not s:
        raise ValueError("empty version string — cannot detect scheme")
    # 1. Play canonical "code (semver)" → the parenthesized part
    paren = re.findall(r"\((\d+(?:\.\d+)+)\)", s)
    if paren:
        return _patch_bump(paren[-1])
    # 2. Bare semver
    if re.match(r"^\d+(?:\.\d+)+$", s):
        return _patch_bump(s)
    # 3. Bare monotonic integer
    if re.match(r"^\d+$", s):
        return str(int(s) + 1)
    raise ValueError(f"cannot detect version scheme from {s!r} — neither semver nor integer")


def _patch_bump(version: str) -> str:
    parts = version.split(".")
    # Pad to at least three segments before bumping the last one.
    while len(parts) < 3:
        parts.append("0")
    try:
        parts[-1] = str(int(parts[-1]) + 1)
    except ValueError as exc:
        raise ValueError(f"Cannot patch-bump non-numeric version segment in '{version}'") from exc
    return ".".join(parts)


def _parse_semver(s: str) -> tuple[int, ...]:
    """Extract the semver-shaped ``X.Y[.Z[.…]]`` tail from an arbitrary string
    and return it as a tuple of ints (padded to 3 segments so comparisons are
    total).

    Handles all the shapes Play and ASC emit:
      "2.4.7"                → (2, 4, 7)
      "2470000 (2.4.7)"      → (2, 4, 7)
      "1.9.14"               → (1, 9, 14)
      "3.12.33"              → (3, 12, 33)
      "52100091 (5.21.9) - new VS 8.8.5"
                             → (5, 21, 9)   ← parenthesized version wins over
                                              trailing SDK-version annotation
    Returns ``(0, 0, 0)`` if nothing parseable was found — callers should
    treat that as "no useful signal, keep the local bump".

    Resolution order:
      1. Semver inside parentheses  ``\\((X.Y[.Z])\\)``  — Play's canonical
         format is ``"<code> (<name>)"``, and any annotations trail the
         parens. Preferring this handles Helix Jump's
         ``"52100091 (5.21.9) - new VS 8.8.5"`` correctly.
      2. Last semver-shape match anywhere in the string — fallback for
         games that emit just ``"1.9.14"`` with no wrapper.
    """
    import re
    if not s:
        return (0, 0, 0)
    paren = re.findall(r"\((\d+(?:\.\d+)+)\)", s)
    if paren:
        chosen = paren[-1]
    else:
        matches = re.findall(r"\d+(?:\.\d+)+", s)
        if not matches:
            return (0, 0, 0)
        chosen = matches[-1]
    parts = chosen.split(".")
    ints = []
    for p in parts:
        try:
            ints.append(int(p))
        except ValueError:
            ints.append(0)
    while len(ints) < 3:
        ints.append(0)
    return tuple(ints)


def semver_gt(a: str, b: str) -> bool:
    """Return True iff semver ``a`` > semver ``b`` under (major, minor, patch)
    integer comparison. Used to decide whether to snap bundleVersion upward
    to match a store's live release."""
    return _parse_semver(a) > _parse_semver(b)


def semver_gte(a: str, b: str) -> bool:
    """Return True iff semver ``a`` >= semver ``b`` under (major, minor, patch)
    integer comparison. Preferred over ``semver_gt`` for the store-drift
    snap: ASC + Play both reject a new appStoreVersion / release whose
    versionString equals a previously shipped one, so we must snap upward
    whenever the store max is >= our proposed value (not just strictly >)."""
    return _parse_semver(a) >= _parse_semver(b)


def format_semver(s: str) -> str:
    """Return the parsed semver as a canonical ``X.Y.Z`` string. Only used
    when snapping bundleVersion to a store max — we canonicalize so
    downstream regex substitutions have a predictable input."""
    ints = _parse_semver(s)
    return ".".join(str(i) for i in ints)
