"""The Codex model catalog the launcher generates for the discovered local model.

Without a catalog entry Codex 0.164 runs a local model on fallback metadata: the long cloud
CLI prompt that tells the model to call an ``apply_patch`` tool oMLX never shows it. The entry
here is the one the catalog spike qualified (docs/qualification/2026-10-24-catalog-spike.md):
``codex_local_model_template.json`` beside this module, with four placeholders filled from the
job's pinned profile. Every other field stays exactly as qualified.

Standard library only. Nothing about any model is hardcoded: the slug, display name, window and
input modalities all come from discovery.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

from omlx_discovery import ModelProfile

__all__ = ["CATALOG_NAME", "TEMPLATE_PATH", "CloudModel", "list_cloud_models",
           "build_local_entry", "fill_template", "write_catalog"]


@dataclass(frozen=True)
class CloudModel:
    slug: str
    display_name: str
    description: str
    context_window: int
    reasoning_levels: List[str]
    default_reasoning: Optional[str]


def list_cloud_models(codex_bin: str,
                      run: Callable[..., Any] = subprocess.run) -> List[CloudModel]:
    """Read the currently installed Codex cloud catalog, preserving its list order."""
    try:
        completed = run([codex_bin, "debug", "models"], capture_output=True,
                        timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError("cannot read the Codex model catalog: %s" % error) from error
    if getattr(completed, "returncode", 1) != 0:
        detail = getattr(completed, "stderr", "")
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", "replace")
        raise RuntimeError("codex debug models failed: %s" % str(detail).strip())
    stdout = getattr(completed, "stdout", "")
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", "replace")
    try:
        payload = json.loads(stdout)
    except (TypeError, ValueError) as error:
        raise RuntimeError("codex debug models did not return JSON") from error
    rows = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError("codex debug models returned no models list")
    models = []
    for row in rows:
        if not isinstance(row, dict) or row.get("visibility") != "list":
            continue
        slug, description = row.get("slug"), row.get("description")
        window = row.get("context_window")
        if (not isinstance(slug, str) or not slug or not isinstance(description, str)
                or not description.strip() or type(window) is not int or window < 1):
            continue
        levels = row.get("supported_reasoning_levels", [])
        if not isinstance(levels, list):
            levels = []
        reasoning = [item.get("effort") if isinstance(item, dict) else item
                     for item in levels]
        reasoning = [item for item in reasoning if isinstance(item, str) and item]
        default = row.get("default_reasoning_level")
        if not isinstance(default, str):
            default = None
        models.append(CloudModel(slug, str(row.get("display_name") or slug),
                                 description.strip(), window, reasoning, default))
    return models

TEMPLATE_PATH = Path(__file__).resolve().with_name("codex_local_model_template.json")
# The file name inside the job's report directory (the launcher's, not the worker's, to write);
# ``-c model_catalog_json`` points at it.
CATALOG_NAME = "local-model-catalog.json"


def fill_template(template: Any, values: Mapping[str, Any]) -> Any:
    """Replace every placeholder in ``template`` with its typed value.

    A placeholder is a JSON string that is exactly ``"{{name}}"``; it becomes the value itself,
    so a window stays an integer and the modalities a list. A string that still holds ``{{``
    after that is a placeholder nobody fills, which is refused rather than sent to Codex.
    """
    if isinstance(template, dict):
        return {key: fill_template(value, values) for key, value in template.items()}
    if isinstance(template, list):
        return [fill_template(value, values) for value in template]
    if isinstance(template, str) and template in values:
        return values[template]
    if isinstance(template, str) and "{{" in template:
        raise ValueError("unknown placeholder in the model catalog template: %r" % template)
    return template


def build_local_entry(model: ModelProfile, context_window: int) -> Dict[str, Any]:
    """The catalog entry for ``model`` at the job's pinned ``context_window``.

    The slug is the name the worker asks for (``-m``), which is what makes Codex use this entry
    instead of its fallback metadata. A VLM takes text and images; anything else text only.
    """
    if isinstance(context_window, bool) or not isinstance(context_window, int) \
            or context_window < 1:
        raise ValueError("context window must be a positive integer, got %r" % (context_window,))
    template = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
    return fill_template(template, {
        "{{slug}}": model.api_model,
        "{{display_name}}": model.api_model,
        "{{context_window}}": context_window,
        "{{input_modalities}}": ["text", "image"] if model.accepts_images else ["text"],
    })


def write_catalog(path: Path, entry: Dict[str, Any]) -> str:
    """Write a one-model catalog holding ``entry`` to ``path``; return the sha256 of its bytes.

    Keys are sorted, so the same entry always produces the same bytes and the same hash. The
    hash is what the launch self-check compares the file against right before every launch.
    """
    data = (json.dumps({"models": [entry]}, indent=2, sort_keys=True) + "\n").encode("utf-8")
    Path(path).write_bytes(data)
    return hashlib.sha256(data).hexdigest()
