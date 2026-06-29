"""Generate the monthly "What's New" / release notes text per locale.

Two modes (selected via ``aso_content.mode`` in ``aso-automation.config.yml``):

  * **placeholder** — returns the strings from the config file. No API call.
    Useful for dry-running the pipeline before Anthropic access is set up.
  * **claude** — calls Anthropic's Messages API with the ASO Texts Agent
    system prompt + the two reference PDFs as document content blocks +
    hardcoded game inputs from the config. Picks "Version A — Short & punchy"
    from Claude's three-version output (Michel-approved default).

For non-primary locales, Claude is called again per locale to translate the
English text while preserving emojis, tone, and length.
"""

from __future__ import annotations

import base64
import logging
import os
import re
from pathlib import Path
from typing import Dict, List

log = logging.getLogger(__name__)


# ─── Public entry point ──────────────────────────────────────────────────────
def generate_per_locale(
    aso_content_config: Dict,
    locales: List[str],
    repo_root: Path | None = None,
) -> Dict[str, str]:
    """Return a dict ``{locale: whats_new_text}`` for every locale in ``locales``.

    ``locales`` is the union of locales discovered from the ASC version and
    the Play production track. ``repo_root`` is required for ``claude`` mode
    (used to resolve system_prompt + PDF paths relative to the repo).
    """
    mode = aso_content_config.get("mode", "placeholder")

    if mode == "placeholder":
        return _placeholder_generate(aso_content_config["placeholder"], locales)

    if mode == "claude":
        if repo_root is None:
            raise ValueError("repo_root is required for claude mode (to resolve prompt + PDF paths)")
        return _claude_generate(aso_content_config["claude"], locales, repo_root)

    raise ValueError(f"Unknown aso_content.mode: {mode!r}")


# ─── Placeholder mode ────────────────────────────────────────────────────────
def _placeholder_generate(placeholder_map: Dict[str, str], locales: List[str]) -> Dict[str, str]:
    """Pull strings straight from config. Locales not in the map fall back to
    the first configured locale's text."""
    if not placeholder_map:
        raise ValueError("aso_content.placeholder is empty — provide at least one locale")

    fallback = next(iter(placeholder_map.values()))
    out: Dict[str, str] = {}
    for loc in locales:
        if loc in placeholder_map:
            out[loc] = placeholder_map[loc]
        else:
            log.info("No placeholder for locale %r — falling back to %r", loc, fallback[:30])
            out[loc] = fallback
    return out


# ─── Claude mode ─────────────────────────────────────────────────────────────
def _claude_generate(claude_config: Dict, locales: List[str], repo_root: Path) -> Dict[str, str]:
    """Generate the English text once via the ASO Texts Agent prompt, then
    translate to every other locale via a follow-up Claude call.
    """
    # Lazy import so placeholder mode doesn't require anthropic to be installed.
    from anthropic import Anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY env var not set — required for claude mode")

    client = Anthropic(api_key=api_key)
    model = claude_config["model"]

    # ── Load system prompt + PDFs from disk ──────────────────────────────
    # The Claude prompts live with the orchestrator code, NOT in the game's
    # repo. When invoked from the shared `VoodooStudios/aso-automation`
    # GH Action, the action sets ``ASO_AUTOMATION_ROOT`` to its checkout
    # path (the action's `${{ github.action_path }}`). Falls back to
    # ``repo_root`` for legacy / local-dev runs where the prompts are
    # checked into the game's repo.
    assets_root = Path(os.environ.get("ASO_AUTOMATION_ROOT") or repo_root)

    def _resolve(rel: str) -> Path:
        from_assets = assets_root / rel
        if from_assets.exists():
            return from_assets
        from_repo = Path(repo_root) / rel
        if from_repo.exists():
            return from_repo
        # Surface a clear error rather than letting read_text raise FileNotFoundError
        raise FileNotFoundError(
            f"Claude prompt asset not found: {rel!r} (looked in "
            f"{assets_root!s} and {repo_root!s})"
        )

    system_prompt = _resolve(claude_config["system_prompt_file"]).read_text(encoding="utf-8")
    pdf_documents = []
    for pdf_path_str in claude_config.get("knowledge_files", []):
        pdf_path = _resolve(pdf_path_str)
        encoded = base64.b64encode(pdf_path.read_bytes()).decode("ascii")
        pdf_documents.append({
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": encoded},
        })

    # ── Build the user message ────────────────────────────────────────────
    inputs = claude_config["game_inputs"]
    update_context = claude_config.get("monthly_update_context", "").strip() or "Monthly app refresh."
    force_version = claude_config.get("force_version", "A")

    user_text = _build_user_prompt(inputs, update_context, force_version)
    user_content = [*pdf_documents, {"type": "text", "text": user_text}]

    # ── First call: generate English text ─────────────────────────────────
    log.info("Calling Claude (%s) for English ASO text…", model)
    response = client.messages.create(
        model=model,
        max_tokens=2048,
        system=system_prompt,
        messages=[{"role": "user", "content": user_content}],
    )
    response_text = response.content[0].text
    english_text = _extract_version(response_text, force_version)
    log.info("Claude returned Version %s (%d chars): %r",
             force_version, len(english_text), english_text[:80])

    # ── Translation pass for non-primary locales ─────────────────────────
    out: Dict[str, str] = {}
    primary_locale = next((l for l in locales if l.startswith("en")), locales[0] if locales else "en-US")
    out[primary_locale] = english_text

    for locale in locales:
        if locale == primary_locale:
            continue
        log.info("Translating to %s via Claude…", locale)
        translate_prompt = _build_translate_prompt(english_text, locale)
        t_resp = client.messages.create(
            model=model,
            max_tokens=1024,
            messages=[{"role": "user", "content": translate_prompt}],
        )
        translated = t_resp.content[0].text.strip()
        # Strip wrapping quotes / fences if Claude added them
        translated = re.sub(r"^[\"'`]+|[\"'`]+$", "", translated).strip()
        out[locale] = translated
        log.info("  %s → %r", locale, translated[:80])

    return out


# ─── Prompt builders ─────────────────────────────────────────────────────────
def _build_user_prompt(inputs: Dict[str, str], update_context: str, force_version: str) -> str:
    """Construct the user message that satisfies the GPT's "Always ask for
    missing inputs" preamble (hardcoded answers) and provides the update
    notes to rewrite.
    """
    return f"""I have provided the inputs you require — please skip the questions and proceed
straight to MODE 2 (ASO update text).

Inputs:
- Game name: {inputs['game_name']}
- Game genre: {inputs['game_genre']}
- Graphical style: {inputs['graphical_style']}
- Target audience: {inputs['target_audience']}
- Store tone: {inputs['store_tone']}
- Goal: {inputs['update_goal']}

Update notes to rewrite into a player-facing ASO update text:

{update_context}

Follow your standard output format (Version A, B, C, Recommended, Why).
After your reply, I will programmatically extract "Version {force_version}" and use it
verbatim as the App Store / Google Play "What's New" text — so make sure Version {force_version}
is self-contained, ≤500 characters (Google Play's hard cap), and ready to ship.
"""


def _build_translate_prompt(source_text: str, locale: str) -> str:
    """Build a translation prompt that preserves emojis, tone, and length."""
    return f"""Translate the following App Store / Google Play "What's New" text from
English to locale {locale!r}. Constraints:

- Preserve every emoji exactly as it appears (same characters, same positions).
- Preserve the bullet/line structure.
- Match the hypercasual playful tone — adapt idioms to feel native, not literal.
- Keep the result under 500 characters total (Google Play's hard cap).
- Output ONLY the translated text. No preamble, no explanation, no quotes around it.

Source text:
{source_text}
"""


# ─── Output parsing ──────────────────────────────────────────────────────────
def _extract_version(response_text: str, force_version: str) -> str:
    """Extract the body of "Version <X> — ..." from Claude's response.

    Claude formats its output in one of two ways:

    1. With explicit Version A header:
            **Version A — Short & punchy**
            <text>

            **Version B — More exciting**
            <text>

    2. Implicit Version A (header omitted, text comes first, then explicit B+C):
            <Version A text>

            **Version B — More exciting**
            <text>

    We handle both. The pattern: find the boundary "Version X" headers (with
    or without ** markdown bold), keep what's between the requested version's
    boundary and the next one. If the requested version is A and there's no
    explicit "Version A" header, take everything from the start of the
    response up to "Version B".
    """
    text = response_text.strip()

    # Match Version headers regardless of markdown bold wrapping.
    # Captures the position of "Version X" anywhere on a line.
    headers = list(re.finditer(
        r"(?im)^\s*\**\s*Version\s+([A-Z])\s*[—–-]",
        text,
    ))

    # Also find the "Recommended version" / "Why:" boundary (everything after
    # is metadata, not the answer text).
    rec_match = re.search(r"(?im)^\s*\**\s*Recommended\s+version\b", text)
    rec_idx = rec_match.start() if rec_match else None

    # Index "Version X" headers by their letter.
    by_letter: Dict[str, re.Match] = {h.group(1).upper(): h for h in headers}

    target = force_version.upper()

    if target in by_letter:
        # Explicit header for the requested version — slice from end-of-header-line
        # to next header (or recommended/end).
        h = by_letter[target]
        # End of the header line is the next newline after the match.
        nl = text.find("\n", h.end())
        body_start = nl + 1 if nl != -1 else h.end()

        # Find next "Version" header that comes after our header.
        next_starts = [m.start() for m in headers if m.start() > h.start()]
        body_end = min([s for s in next_starts + ([rec_idx] if rec_idx else []) if s is not None] or [len(text)])
        return _clean_block(text[body_start:body_end])

    # Implicit Version A — no explicit header, the response opens with the text.
    # Take everything from the start up to the first explicit Version header.
    if target == "A" and headers:
        return _clean_block(text[: headers[0].start()])

    log.warning("Could not extract Version %s from Claude response — using full text", force_version)
    return text


def _clean_block(block: str) -> str:
    """Strip horizontal rules, trailing meta-annotations Claude likes to add,
    and surrounding whitespace from an extracted version block.

    Claude periodically appends self-commentary like:
      *(Characters: 98)*
      (chars: 72)
      *(chars: 72 — well within 500)*
      *(72 chars, within limit)*
      [Length: 98 chars]
    None of this should ship to the store. We strip aggressively: any trailing
    italic/parenthetical/bracketed block on its own line that contains
    "char", "len", "count", or "within" gets removed.
    """
    out = block.strip()

    # Repeat the strip up to 3 times — Claude sometimes stacks multiple
    # meta-lines (e.g. character count + "perfect for stores").
    for _ in range(3):
        before = out
        # 1. Trailing parenthetical/italic meta-annotation on its own line
        #    Matches: *(...)*, (...), *...*, [...], when content mentions char/len/within/count/limit/word
        out = re.sub(
            r"\n\s*\**\s*[\(\[]?\s*[^\n()\[\]]*?\b(char|chars|character|characters|len|length|count|within|limit|words?)\b[^\n()\[\]]*?\s*[\)\]]?\s*\**\s*$",
            "",
            out,
            flags=re.IGNORECASE,
        )
        # 2. Trailing horizontal rule
        out = re.sub(r"\n\s*-{3,}\s*$", "", out)
        # 3. Leading horizontal rule
        out = re.sub(r"^\s*-{3,}\s*\n", "", out)
        out = out.strip()
        if out == before:
            break

    return out
