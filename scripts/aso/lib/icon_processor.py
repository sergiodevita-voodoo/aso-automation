"""Resize an icon master PNG to all variant sizes and overwrite the files in place.

CRITICAL: we overwrite the PNG bytes only — the sibling ``.meta`` files (which
carry the Unity GUIDs referenced in ``ProjectSettings.asset``) MUST stay
untouched. Renaming, deleting, or regenerating the .meta files would break
every icon binding in the project.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable

from PIL import Image

from .config import IconPath

log = logging.getLogger(__name__)


def overwrite_all_variants(repo_root: Path, master_png: Path, variants: Iterable[IconPath]) -> list[Path]:
    """Resize ``master_png`` to each variant's size and overwrite the file at its path.

    Returns the list of written file paths (relative to repo_root, for logging
    / commit message generation).

    Raises ``FileNotFoundError`` if any target PNG doesn't already exist —
    we never CREATE icon files, only overwrite existing ones (so we don't
    accidentally orphan or duplicate Unity GUIDs).
    """
    with Image.open(master_png) as master:
        # Ensure RGBA — App Store / Google Play accept both RGB and RGBA, but
        # RGBA is the safer default for icons with transparency baked in.
        master.load()
        if master.mode != "RGBA":
            master = master.convert("RGBA")

        written: list[Path] = []
        for variant in variants:
            target = (repo_root / variant.path).resolve()
            if not target.is_file():
                raise FileNotFoundError(
                    f"Icon variant {variant.path} does not exist. Refusing to create — "
                    "the .meta sidecar would be missing and Unity would auto-generate a new GUID, "
                    "breaking the icon binding. Restore the file first or remove the entry from the config."
                )

            resized = master.resize((variant.size, variant.size), resample=Image.Resampling.LANCZOS)
            resized.save(target, format="PNG", optimize=True)
            log.info("  ↳ %s (%dx%d)", variant.path, variant.size, variant.size)
            written.append(target)

        return written
