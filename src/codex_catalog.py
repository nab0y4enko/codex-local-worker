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
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from omlx_discovery import ModelProfile

__all__ = ["CATALOG_NAME", "TEMPLATE_PATH", "build_local_entry", "file_sha256",
           "fill_template", "write_catalog"]

TEMPLATE_PATH = Path(__file__).resolve().with_name("codex_local_model_template.json")
# The file name inside the job's Codex home; ``-c model_catalog_json`` points at it.
CATALOG_NAME = "local-model-catalog.json"
READ_CHUNK_BYTES = 1 << 16


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


def file_sha256(path: Path) -> Optional[str]:
    """The sha256 of the file at ``path`` as it is now; ``None`` when it cannot be read."""
    digest = hashlib.sha256()
    try:
        with open(str(path), "rb") as handle:
            for chunk in iter(lambda: handle.read(READ_CHUNK_BYTES), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()
