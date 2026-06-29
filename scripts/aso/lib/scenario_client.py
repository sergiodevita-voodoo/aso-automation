"""Scenario AI workflow runner — mirrors the Scenario web UI's monthly
icon-generation workflow, reading EVERY parameter live from the workflow.

Workflow: `Icon Workflow fo AI ASO` (configured by `workflow_id`).

How it works:
  1. GET /v1/workflows/{id}     → fetch the workflow's nodes + their config
  2. POST /v1/assets             → upload current game icon as Reference image
  3. POST /v1/generate/custom/model_scenario-llm  ← LLM create rules
       (model, thinkingLevel, numOutputs, instruction → all read live)
  4. POST /v1/generate/custom/model_scenario-llm  ← LLM-Variations generation
       (same — all node params live)
  5. POST /v1/generate/custom/{image-gen-model}   ← Image generator
       (model + form params all read live; body modelId auto-resolved if the
        endpoint rejects the URL-form value)
  6. GET /v1/assets/{id}         → download new icon

If Sergio changes anything in the Scenario UI — node models, instruction
texts, thinking level, aspect ratio, resolution, etc. — the next run picks
it up automatically. No code changes needed.

Auth: HTTP Basic, `api_xxx:secret_yyy` in SCENARIO_API_KEY.

Why not the official `/workflows/{id}/run` endpoint: Scenario's workflow
runner doesn't accept this workflow's node types (`llm`, modern image
generators). The UI walks the graph client-side; this module does the same.
"""

from __future__ import annotations

import base64
import json as _json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

log = logging.getLogger(__name__)

_BASE_URL = "https://api.cloud.scenario.com/v1"

# Node IDs in the workflow we read live. Stable per workflow.
# (If a workflow is restructured these would need to change — but that's a
# bigger surgery than a model tweak.)
_NODE_LLM_RULES = "llmGenerator2"          # "LLM create rules"
_NODE_LLM_VARIATIONS = "llmGenerator4"     # "LLM-Variations generation"
_NODE_LLM_INSTRUCTION_TEXT = "text1"       # "LLM instruction" (feeds rules)
_NODE_IMAGE_GEN = "imageGenerator2"        # "Image Generator 2"


@dataclass
class _NodeConfig:
    """Resolved config for one node, ready to pass to a job POST."""
    instruction: str = ""
    model: str = ""                    # e.g. "claude-opus-4-7" for LLM
    thinking_level: str = "minimal"
    num_outputs: int = 1
    # Image-gen specific:
    image_model_url_path: str = ""     # e.g. "model_google-gemini-3-1-flash"
    image_model_id_body: str = ""      # e.g. "gemini-3.1-flash-image"
    aspect_ratio: str = "auto"
    resolution: Optional[str] = None
    use_google_search: Optional[bool] = None
    video_fps: Optional[int] = None
    extra_form: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ScenarioClient:
    """Mirrors the Scenario UI's workflow execution via direct API calls,
    reading every parameter live from the workflow definition."""

    api_key: str
    project_id: str
    workflow_id: str                   # e.g. "wflow_zaYJkzUzWmKogBwZK1hwmJub"
    poll_interval_seconds: int = 3
    poll_timeout_minutes: int = 15

    def _headers(self, json_body: bool = True) -> Dict[str, str]:
        enc = base64.b64encode(self.api_key.encode("utf-8")).decode("ascii")
        h = {"Authorization": f"Basic {enc}"}
        if json_body:
            h["Content-Type"] = "application/json"
        return h

    def _url(self, path: str) -> str:
        sep = "&" if "?" in path else "?"
        return f"{_BASE_URL}{path}{sep}projectId={self.project_id}"

    # ── Live workflow config ──────────────────────────────────────────────
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    def _fetch_workflow_nodes(self) -> Dict[str, Dict[str, Any]]:
        """Return a {nodeId: node} mapping from the workflow's editorInfo."""
        r = requests.get(self._url(f"/workflows/{self.workflow_id}"),
                         headers=self._headers(json_body=False), timeout=30)
        r.raise_for_status()
        nodes = r.json()["workflow"]["editorInfo"]["nodes"]
        return {n["id"]: n for n in nodes}

    def _resolve_workflow_config(self) -> Dict[str, _NodeConfig]:
        """Build runtime config for all 3 active nodes by reading the live workflow."""
        nodes = self._fetch_workflow_nodes()

        def _form(node_id: str) -> Dict[str, Any]:
            return (nodes.get(node_id, {}).get("data", {}) or {}).get("form", {}) or {}

        def _data(node_id: str) -> Dict[str, Any]:
            return nodes.get(node_id, {}).get("data", {}) or {}

        # 1) LLM-create-rules: instruction is wired from text1.data.value
        rules_form = _form(_NODE_LLM_RULES)
        text1_value = _data(_NODE_LLM_INSTRUCTION_TEXT).get("value", "").strip()
        if not text1_value:
            raise RuntimeError(
                f"Scenario workflow {self.workflow_id}: node "
                f"{_NODE_LLM_INSTRUCTION_TEXT!r} has empty `data.value` — "
                "the LLM instruction box is blank in the UI."
            )
        rules = _NodeConfig(
            instruction=text1_value,
            model=rules_form.get("model", ""),
            thinking_level=rules_form.get("thinkingLevel", "minimal"),
            num_outputs=int(rules_form.get("numOutputs", 1)),
        )
        if not rules.model:
            raise RuntimeError(f"Scenario node {_NODE_LLM_RULES}: `form.model` is empty")

        # 2) LLM-Variations: instruction is in its own form
        var_form = _form(_NODE_LLM_VARIATIONS)
        variations = _NodeConfig(
            instruction=var_form.get("instruction", "").strip(),
            model=var_form.get("model", ""),
            thinking_level=var_form.get("thinkingLevel", "minimal"),
            num_outputs=int(var_form.get("numOutputs", 1)),
        )
        if not variations.instruction:
            raise RuntimeError(f"Scenario node {_NODE_LLM_VARIATIONS}: `form.instruction` is empty")
        if not variations.model:
            raise RuntimeError(f"Scenario node {_NODE_LLM_VARIATIONS}: `form.model` is empty")

        # 3) Image generator
        img_data = _data(_NODE_IMAGE_GEN)
        img_form = _form(_NODE_IMAGE_GEN)
        url_path = img_data.get("modelId", "")
        if not url_path:
            raise RuntimeError(f"Scenario node {_NODE_IMAGE_GEN}: `data.modelId` is empty")
        body_id = _guess_body_model_id(url_path)
        # Pull every meaningful form param — pass through whatever Scenario
        # validates (Scenario rejects unknown keys cleanly).
        keys_we_handle = {"prompt", "referenceImages"}  # set by run-time
        extra = {k: v for k, v in img_form.items() if k not in keys_we_handle}
        image = _NodeConfig(
            image_model_url_path=url_path,
            image_model_id_body=body_id,
            aspect_ratio=img_form.get("aspectRatio", "auto"),
            resolution=img_form.get("resolution"),
            use_google_search=img_form.get("useGoogleSearch"),
            video_fps=img_form.get("videoFps"),
            num_outputs=int(img_form.get("numOutputs", 1)),
            extra_form=extra,
        )

        log.info(
            "Scenario workflow %s: rules.model=%s thinking=%s | "
            "variations.model=%s thinking=%s | image.url=%s body=%s aspectRatio=%s",
            self.workflow_id,
            rules.model, rules.thinking_level,
            variations.model, variations.thinking_level,
            image.image_model_url_path, image.image_model_id_body, image.aspect_ratio,
        )
        return {"rules": rules, "variations": variations, "image": image}

    # ── Asset upload ──────────────────────────────────────────────────────
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    def upload_asset(self, image_path: Path, name: str) -> str:
        img_b = Path(image_path).read_bytes()
        b64 = base64.b64encode(img_b).decode()
        body = {"image": f"data:image/png;base64,{b64}", "name": name}
        resp = requests.post(self._url("/assets"), json=body, headers=self._headers(), timeout=60)
        resp.raise_for_status()
        data = resp.json()
        asset_id = (data.get("asset") or {}).get("id") or data.get("id")
        if not asset_id:
            raise RuntimeError(f"upload_asset: no asset id in response: {data}")
        log.info("Scenario asset uploaded: %s (%s bytes)", asset_id, f"{len(img_b):,}")
        return asset_id

    # ── Job lifecycle ─────────────────────────────────────────────────────
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    def _post_job(self, model_path: str, body: Dict[str, Any]) -> str:
        url = self._url(f"/generate/custom/{model_path}")
        resp = requests.post(url, json=body, headers=self._headers(), timeout=60)
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Job POST {model_path} failed {resp.status_code}: {resp.text[:400]}")
        data = resp.json()
        job_id = (data.get("job") or {}).get("jobId") or data.get("jobId")
        if not job_id:
            raise RuntimeError(f"Job POST {model_path}: no jobId in response: {data}")
        return job_id

    def _post_and_wait_with_modelid_retry(self, model_path: str, body: Dict[str, Any], label: str) -> Dict[str, Any]:
        """POST a job, wait for it, and if Scenario rejects the body's
        `modelId` (either at POST or during job execution), parse the allowed
        enum from the error and retry once with the closest match.

        This lets us survive the URL-form vs. body-form modelId divergence
        without maintaining a hardcoded mapping table.
        """
        def _maybe_corrected_modelid(error_msg: str) -> Optional[str]:
            # Handles BOTH error-string variants Scenario produces:
            #   "Input should be one of: 'X', 'Y', 'Z'"
            #   "Input should be 'X', 'Y' or 'Z'"
            #   "must be one of the following values: 'X', 'Y' or 'Z'"
            patterns = [
                r"Input should be(?:\s*one of)?:?\s*([^\\\n]+?)(?:\\n|\"|$)",
                r"must be one of the following values:?\s*([^\\\n]+?)(?:\\n|\"|$)",
            ]
            m = None
            for p in patterns:
                m = re.search(p, error_msg)
                if m:
                    break
            if not m:
                return None
            opts_raw = m.group(1)
            # Split on commas / "or" / "and"
            raw = re.split(r"\s*,\s*|\s+or\s+|\s+and\s+", opts_raw)
            options = [o.strip().strip("'\"`. ").rstrip(".") for o in raw]
            options = [o for o in options if o and not o.lower().startswith("input")]
            if not options:
                return None
            current = body.get("modelId", "")
            best = _closest_match(current, options) or _closest_match(model_path, options)
            if not best or best == current:
                return None
            return best

        attempts = 0
        while True:
            attempts += 1
            try:
                job_id = self._post_job(model_path, body)
            except RuntimeError as e:
                fixed = _maybe_corrected_modelid(str(e))
                if not fixed or attempts > 2:
                    raise
                log.warning("Scenario [%s]: POST modelId %r rejected, retrying with %r",
                            label, body.get("modelId"), fixed)
                body = {**body, "modelId": fixed}
                continue
            try:
                return self._wait_for_job(job_id, label)
            except RuntimeError as e:
                fixed = _maybe_corrected_modelid(str(e))
                if not fixed or attempts > 2:
                    raise
                log.warning("Scenario [%s]: job-time modelId %r rejected, retrying with %r",
                            label, body.get("modelId"), fixed)
                body = {**body, "modelId": fixed}
                continue

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    def _get_job(self, job_id: str) -> Dict[str, Any]:
        resp = requests.get(self._url(f"/jobs/{job_id}"), headers=self._headers(json_body=False), timeout=30)
        resp.raise_for_status()
        return resp.json().get("job") or resp.json()

    def _wait_for_job(self, job_id: str, label: str) -> Dict[str, Any]:
        deadline = time.monotonic() + (self.poll_timeout_minutes * 60)
        last_logged = None
        while time.monotonic() < deadline:
            j = self._get_job(job_id)
            st = (j.get("status") or "").lower()
            progress = j.get("progress")
            key = (st, progress)
            if key != last_logged:
                log.info("Scenario [%s] job=%s status=%s progress=%s",
                         label, job_id, st, progress)
                last_logged = key
            if st in ("success", "succeeded", "completed"):
                return j
            if st in ("failure", "failed", "canceled", "cancelled", "error"):
                # Surface the server's actual error message (often in
                # metadata.error) at the start of the RuntimeError so
                # downstream retry logic can pattern-match cleanly.
                err = j.get("metadata", {}).get("error") or j.get("errorMessage") or j.get("reason") or ""
                raise RuntimeError(
                    f"Scenario [{label}] job {job_id} FAILED — error={err!r} — "
                    f"metadata={_json.dumps(j.get('metadata', {}))[:400]}"
                )
            time.sleep(self.poll_interval_seconds)
        raise TimeoutError(f"Scenario [{label}] job {job_id} timed out after {self.poll_timeout_minutes} min")

    # ── Asset download ────────────────────────────────────────────────────
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    def _get_asset_url(self, asset_id: str) -> str:
        resp = requests.get(self._url(f"/assets/{asset_id}"), headers=self._headers(json_body=False), timeout=30)
        resp.raise_for_status()
        data = resp.json().get("asset") or resp.json()
        url = data.get("url") or data.get("downloadUrl")
        if not url:
            raise RuntimeError(f"Asset {asset_id} has no url field: {data}")
        return url

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    def download_asset(self, asset_id: str, destination: Path) -> Path:
        url = self._get_asset_url(asset_id)
        resp = requests.get(url, timeout=120)
        resp.raise_for_status()
        if not resp.content:
            raise RuntimeError(f"CDN returned empty body for {asset_id}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(resp.content)
        log.info("Scenario asset %s downloaded → %s (%s bytes)",
                 asset_id, destination, f"{len(resp.content):,}")
        return destination

    # ── High-level: run the full workflow ─────────────────────────────────
    def run_workflow_icons(self, reference_image: Path, destination: Path) -> Path:
        """Run the configured Scenario workflow with ``reference_image`` as
        the Reference image input. Downloads the new icon to ``destination``.

        Mirrors the 3-node chain (LLM-create-rules → LLM-Variations → Image-gen)
        with every parameter — instructions, models, thinking levels, aspect
        ratio, etc. — read live from the workflow definition.
        """
        cfg = self._resolve_workflow_config()
        ref_id = self.upload_asset(reference_image, name="aso-monthly-reference")

        # ── Node: LLM create rules ─────────────────────────────────────────
        log.info("Scenario [LLM-create-rules] starting (model=%s)…", cfg["rules"].model)
        job_id_1 = self._post_job("model_scenario-llm", {
            "modelId": "model_scenario-llm",
            "images": [ref_id],
            "model": cfg["rules"].model,
            "numOutputs": cfg["rules"].num_outputs,
            "textInputs": [],
            "instruction": cfg["rules"].instruction,
            "thinkingLevel": cfg["rules"].thinking_level,
        })
        job_1 = self._wait_for_job(job_id_1, "LLM-create-rules")
        rules_output = job_1["metadata"]["output"]["results"][0]
        log.info("Scenario [LLM-create-rules] → %s chars", len(rules_output))

        # ── Node: LLM-Variations generation ────────────────────────────────
        log.info("Scenario [LLM-variations] starting (model=%s)…", cfg["variations"].model)
        job_id_2 = self._post_job("model_scenario-llm", {
            "modelId": "model_scenario-llm",
            "images": [ref_id],
            "model": cfg["variations"].model,
            "numOutputs": cfg["variations"].num_outputs,
            "textInputs": [rules_output],
            "instruction": cfg["variations"].instruction,
            "thinkingLevel": cfg["variations"].thinking_level,
        })
        job_2 = self._wait_for_job(job_id_2, "LLM-variations")
        variation_prompt = job_2["metadata"]["output"]["results"][0]
        log.info("Scenario [LLM-variations] → %s chars", len(variation_prompt))

        # ── Node: Image Generator ──────────────────────────────────────────
        img = cfg["image"]
        log.info("Scenario [image-gen] starting (url=%s body=%s)…",
                 img.image_model_url_path, img.image_model_id_body)
        body: Dict[str, Any] = {
            "modelId": img.image_model_id_body,
            "referenceImages": [ref_id],
            "prompt": variation_prompt,
            "numOutputs": img.num_outputs,
            "aspectRatio": img.aspect_ratio,
        }
        # Pass through any extra form params Scenario understands.
        for k, v in img.extra_form.items():
            if v is not None and k not in body:
                body[k] = v
        job_3 = self._post_and_wait_with_modelid_retry(img.image_model_url_path, body, "image-gen")
        meta = job_3.get("metadata", {})
        out_ids: List[str] = meta.get("assetIds") or [
            a.get("assetId") for a in meta.get("output", {}).get("assets", [])
        ]
        if not out_ids:
            raise RuntimeError(f"image-gen produced no output assets: {_json.dumps(job_3)[:500]}")
        log.info("Scenario [image-gen] → asset %s", out_ids[0])

        return self.download_asset(out_ids[0], destination)


# ── Helpers ──────────────────────────────────────────────────────────────────
def _guess_body_model_id(url_path: str) -> str:
    """Best-effort first guess of the body-form `modelId` from the URL-form
    one stored on the workflow node. Pattern observed on Scenario:

        model_google-gemini-3-1-flash   →  gemini-3-1-flash   (then retry adds -image)
        model_openai-gpt-image-2        →  gpt-image-2
        model_scenario-llm              →  model_scenario-llm  (no change)
        flux.1-dev                      →  flux.1-dev          (already bare)

    If the first guess fails, ``_post_job_with_modelid_retry`` parses the
    server's enum error and picks the closest valid option.
    """
    if not url_path.startswith("model_"):
        return url_path
    # Strip the leading "model_"
    bare = url_path[len("model_"):]
    # Strip a known provider prefix
    for prov in ("google-", "openai-", "anthropic-", "stability-", "black-forest-"):
        if bare.startswith(prov):
            bare = bare[len(prov):]
            break
    return bare


def _closest_match(candidate: str, options: List[str]) -> Optional[str]:
    """Return the option whose normalized form best overlaps with `candidate`."""
    if not options:
        return None
    def norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", s.lower())
    nc = norm(candidate)
    scored = []
    for o in options:
        no = norm(o)
        # token overlap as a quick proxy
        common = sum(1 for ch in set(nc) if ch in no)
        # prefer options that contain the candidate's full normalized form as a substring
        boost = 100 if nc and nc in no else 0
        boost += 50 if no and no in nc else 0
        scored.append((boost + common, o))
    scored.sort(reverse=True)
    return scored[0][1] if scored else None
