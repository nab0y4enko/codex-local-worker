#!/usr/bin/env python3
"""Spike: which Codex model-catalog entry makes Codex treat the local oMLX model well.

Reproduces the evidence recorded in docs/qualification/2026-10-24-catalog-spike.md.

    schema  No inference. Which fields Codex refuses to load an entry without, and which
            values it accepts (a deliberately wrong value makes the parser list them).
    render  No inference. For each catalog variant, start Codex with the launcher's exact worker
            argv (plus ``-c model_catalog_json=...``) against a loopback *recording stub* that
            answers every Responses request with a canned "ok". The stub captures the request
            Codex really sends: base instructions, the tool list, reasoning settings. Also runs
            ``codex debug prompt-input`` with the same overrides (that rendering shows the input
            list only: no tools, and no base instructions unless ``use_responses_lite``).
    calls   No inference. The stub answers the first request with a scripted tool call (each
            way a model can ask for a patch, plus a 60,000-character command output) and the
            run shows how Codex handles it: applied, refused, or what it answers the model.
    pilot   Real local inference. For each chosen variant, lay down a fresh pilot repository
            (scripts/prepare_pilot.py) and run the real launcher on it
            (``bin/local-worker run --json``). The ``template`` variant runs on the catalog the
            launcher itself generates from the template; any other variant runs with
            ``--codex-bin`` pointing at a tiny wrapper that swaps the launcher's
            ``-c model_catalog_json=...`` for the variant's (``none`` removes it), so the worker
            never gets two catalogs. ``--record`` also routes the worker through a loopback
            recording proxy in front of oMLX, so the record shows what the model saw and which
            calls it made.

Every Codex process gets a fresh throwaway ``CODEX_HOME`` under ``--out`` (in ``pilot``, the
launcher's own job home); the user's ``~/.codex`` is never read. Inference only ever goes to the loopback oMLX server that discovery
finds; no cloud model is called. In ``pilot`` the launcher reads the oMLX key itself; the
recording proxy forwards the request headers unchanged and never records ``Authorization``.
Nothing here prints the key, and the stub runs use a dummy key.

Catalog values come from the live server: slug and display name = the discovered model,
context window = the discovered effective window, input modalities = text (+ image when the
engine is a VLM). Nothing about the model is hardcoded here.
"""

from __future__ import annotations

import argparse
import collections
import dataclasses
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(REPO / "src"))

import local_worker as lw  # noqa: E402
import omlx_discovery  # noqa: E402

TEMPLATE_PATH = REPO / "src" / "codex_local_model_template.json"
CATALOG_NAME = "local-model-catalog.json"
STUB_PROMPT = "Reply with the single word ok."
CODEX_TIMEOUT_SECONDS = 120
PILOT_TIMEOUT_SECONDS = 900
DUMMY_KEY = "spike-dummy-key"
METADATA_MISSING = "Model metadata for"
# ``--set key=value`` overrides appended to every worker argv (feature-flag exploration).
EXTRA_CONFIG: List[str] = []
UNSUPPORTED_CALL = re.compile(r"unsupported (?:custom tool )?call: \S+")
# Tools of the cloud "code mode" and multi-agent surfaces. The catalog entry controls them
# (``tool_mode``, ``multi_agent_version``); a local entry must offer none of them.
CODE_MODE_TOOLS = frozenset({
    "exec", "wait", "followup_task", "interrupt_agent", "list_agents",
    "request_user_input_async", "send_message", "sleep", "spawn_agent", "wait_agent",
    "close_agent", "send_input", "resume_agent",
})
# Tools Codex adds whatever the catalog says; only a feature flag (or nothing) removes them.
FEATURE_TOOLS = {
    "get_goal": "features.goals", "create_goal": "features.goals",
    "update_goal": "features.goals",
    "request_user_input": "always offered by codex exec 0.164 (Plan mode only)",
}
SHELL_TOOLS = frozenset({"exec_command", "shell", "shell_command", "local_shell"})


# --------------------------------------------------------------------------------------
# Catalog entries
# --------------------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class Facts:
    """Everything a catalog entry takes from the live server (and the recorded fallback)."""

    slug: str
    display_name: str
    context_window: int
    input_modalities: List[str]
    fallback_instructions: str
    cloud_entry: Dict[str, Any]


def fill_template(template: Dict[str, Any], facts: Facts) -> Dict[str, Any]:
    """Substitute the template's placeholders with typed values.

    A placeholder is a JSON string that is exactly ``"{{name}}"``; it is replaced by the value
    itself, so ``"{{context_window}}"`` becomes an integer and ``"{{input_modalities}}"`` a list.
    """
    values = {
        "{{slug}}": facts.slug,
        "{{display_name}}": facts.display_name,
        "{{context_window}}": facts.context_window,
        "{{input_modalities}}": list(facts.input_modalities),
    }

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            return {key: walk(value) for key, value in node.items()}
        if isinstance(node, list):
            return [walk(value) for value in node]
        if isinstance(node, str) and node in values:
            return values[node]
        if isinstance(node, str) and "{{" in node:
            raise ValueError(f"unknown placeholder in template: {node!r}")
        return node

    return walk(template)


def minimal_entry(facts: Facts) -> Dict[str, Any]:
    """The fields Codex 0.164 refuses to load without, with local-model values.

    Found by loading an entry with only ``slug`` and adding each field the parser reported as
    missing (``missing field `x```) until it loaded; ``base_instructions`` (or
    ``model_messages.instructions_template``) is required too.
    """
    return {
        "slug": facts.slug,
        "display_name": facts.display_name,
        "supported_reasoning_levels": [],
        "shell_type": "shell_command",
        "visibility": "list",
        "supported_in_api": True,
        "priority": 0,
        "support_verbosity": False,
        "truncation_policy": {"mode": "bytes", "limit": 10000},
        "experimental_supported_tools": [],
        "base_instructions": facts.fallback_instructions,
    }


def local_base(facts: Facts) -> Dict[str, Any]:
    """The candidate local entry the one-factor variants start from."""
    entry = minimal_entry(facts)
    entry.update({
        "description": "Local model served by oMLX on loopback.",
        "context_window": facts.context_window,
        "input_modalities": list(facts.input_modalities),
        "apply_patch_tool_type": "freeform",
        "tool_mode": "direct",
    })
    return entry


DROP = object()  # a field value meaning "remove this field from the entry"


def with_fields(**fields: Any) -> Callable[[Facts], Dict[str, Any]]:
    """The candidate base entry with *fields* changed (``DROP`` removes one)."""
    def build(facts: Facts) -> Dict[str, Any]:
        entry = local_base(facts)
        for key, value in fields.items():
            if value is DROP:
                entry.pop(key, None)
            else:
                entry[key] = value
        return entry
    return build


def cloud_clone(facts: Facts) -> Dict[str, Any]:
    """A cloud entry from ``codex debug models`` with only the identity and window swapped."""
    entry = json.loads(json.dumps(facts.cloud_entry))
    entry.update({"slug": facts.slug, "display_name": facts.display_name,
                  "context_window": facts.context_window})
    return entry


def shipped_template(facts: Facts) -> Dict[str, Any]:
    return fill_template(json.loads(TEMPLATE_PATH.read_text(encoding="utf-8")), facts)


def template_with(**fields: Any) -> Callable[[Facts], Dict[str, Any]]:
    """The shipped template with *fields* changed (``DROP`` removes one); a callable value
    is called with the facts."""
    def build(facts: Facts) -> Dict[str, Any]:
        entry = shipped_template(facts)
        for key, value in fields.items():
            if value is DROP:
                entry.pop(key, None)
            else:
                entry[key] = value(facts) if callable(value) else value
        return entry
    return build


# name -> (what it varies, builder; None = no catalog, Codex fallback metadata)
VARIANTS: Dict[str, Tuple[str, Optional[Callable[[Facts], Dict[str, Any]]]]] = {
    "none": ("no catalog: Codex fallback metadata", None),
    "cloud-clone": ("first cloud entry of `codex debug models`, slug/window swapped", cloud_clone),
    "minimal": ("only the required fields (shell_command, fallback instructions)", minimal_entry),
    "base": ("candidate: minimal + window, modalities, freeform patch, tool_mode direct",
             local_base),
    "tool-mode-code": ("base with tool_mode code_mode_only",
                       with_fields(tool_mode="code_mode_only")),
    "tool-mode-unset": ("base without tool_mode", with_fields(tool_mode=DROP)),
    "no-patch-tool": ("base without apply_patch_tool_type",
                      with_fields(apply_patch_tool_type=DROP)),
    "shell-unified": ("base with shell_type unified_exec", with_fields(shell_type="unified_exec")),
    "shell-default": ("base with shell_type default", with_fields(shell_type="default")),
    "shell-local": ("base with shell_type local", with_fields(shell_type="local")),
    "shell-disabled": ("base with shell_type disabled", with_fields(shell_type="disabled")),
    "instructions-empty": ("base with empty base_instructions", with_fields(base_instructions="")),
    "truncation-tokens": ("base with truncation_policy tokens/10000",
                          with_fields(truncation_policy={"mode": "tokens", "limit": 10000})),
    "text-only": ("base with input_modalities [text]", with_fields(input_modalities=["text"])),
    "responses-lite": ("base with use_responses_lite true", with_fields(use_responses_lite=True)),
    "template": ("src/codex_local_model_template.json, filled", shipped_template),
    "template-freeform": ("template plus apply_patch_tool_type freeform",
                          template_with(apply_patch_tool_type="freeform")),
    "template-fallback-instructions": ("template with the fallback base instructions",
                                       template_with(base_instructions=lambda f:
                                                     f.fallback_instructions)),
}


# --------------------------------------------------------------------------------------
# Loopback recorder: a canned stub, or a forwarding proxy in front of oMLX
# --------------------------------------------------------------------------------------
def _sse(event: Dict[str, Any]) -> bytes:
    return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode("utf-8")


STUB_EVENTS = (
    {"type": "response.created", "response": {"id": "resp_stub"}},
    {"type": "response.output_item.done",
     "item": {"type": "message", "role": "assistant", "id": "msg_stub",
              "content": [{"type": "output_text", "text": "ok"}]}},
    {"type": "response.completed",
     "response": {"id": "resp_stub",
                  "usage": {"input_tokens": 1, "input_tokens_details": {"cached_tokens": 0},
                            "output_tokens": 1, "output_tokens_details": {"reasoning_tokens": 0},
                            "total_tokens": 2}}},
)
# Response events worth keeping from a proxied stream: the finished items and usage.
KEPT_EVENTS = ("response.output_item.done", "response.completed", "response.failed", "error")


class Recorder:
    """Loopback HTTP server that records every request Codex sends.

    ``upstream=None``: stub mode, every POST is answered with STUB_EVENTS (no inference).
    ``upstream=url``: proxy mode, each request is forwarded unchanged (headers included, so the
    key reaches oMLX) and the streamed answer is relayed while its finished items are recorded.
    Recorded headers never include ``Authorization``.
    """

    def __init__(self, upstream: Optional[str] = None,
                 script: Optional[List[Dict[str, Any]]] = None) -> None:
        self.upstream = upstream.rstrip("/") if upstream else None
        # Stub mode only: output items to answer the 1st, 2nd, ... model request with, in
        # place of the canned "ok" (a scripted tool call, no inference).
        self.script = list(script or [])
        self.exchanges: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        recorder = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_GET(self) -> None:  # noqa: N802
                recorder._handle(self, None)

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                recorder._handle(self, self.rfile.read(length))

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return "http://127.0.0.1:%d" % self.server.server_address[1]

    def __enter__(self) -> "Recorder":
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()

    def _handle(self, handler: BaseHTTPRequestHandler, raw: Optional[bytes]) -> None:
        try:
            body: Any = json.loads(raw) if raw else None
        except ValueError:
            body = {"undecoded_bytes": len(raw or b"")}
        exchange: Dict[str, Any] = {
            "method": handler.command, "path": handler.path,
            "headers": {k: v for k, v in handler.headers.items()
                        if k.lower() not in ("authorization", "cookie")},
            "request": body, "response_events": [],
        }
        with self._lock:
            self.exchanges.append(exchange)
        if self.upstream is None:
            if raw is None:
                handler.send_response(404)
                handler.end_headers()
                return
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.end_headers()
            with self._lock:
                scripted = self.script.pop(0) if self.script else None
            events = list(STUB_EVENTS)
            if scripted is not None:
                events[1] = {"type": "response.output_item.done", "item": scripted}
            for event in events:
                handler.wfile.write(_sse(event))
            handler.wfile.flush()
            return
        self._forward(handler, raw, exchange)

    def _forward(self, handler: BaseHTTPRequestHandler, raw: Optional[bytes],
                 exchange: Dict[str, Any]) -> None:
        headers = {k: v for k, v in handler.headers.items()
                   if k.lower() not in ("host", "content-length", "connection",
                                        "accept-encoding")}
        request = urllib.request.Request(self.upstream + handler.path, data=raw,
                                         headers=headers, method=handler.command)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            response = opener.open(request, timeout=PILOT_TIMEOUT_SECONDS)
        except urllib.error.HTTPError as error:
            response = error
        exchange["status"] = response.status if hasattr(response, "status") else response.code
        handler.send_response(exchange["status"])
        for key, value in response.headers.items():
            if key.lower() not in ("content-length", "transfer-encoding", "connection"):
                handler.send_header(key, value)
        handler.end_headers()
        while True:
            line = response.readline()
            if not line:
                break
            handler.wfile.write(line)
            handler.wfile.flush()
            if line.startswith(b"data:"):
                try:
                    event = json.loads(line[5:].strip())
                except ValueError:
                    continue
                if isinstance(event, dict) and event.get("type") in KEPT_EVENTS:
                    exchange["response_events"].append(event)

    def model_requests(self) -> List[Dict[str, Any]]:
        return [x for x in self.exchanges
                if x["method"] == "POST" and x["path"].endswith("/responses")
                and isinstance(x["request"], dict)]


# --------------------------------------------------------------------------------------
# Summaries
# --------------------------------------------------------------------------------------
def tool_names(request: Dict[str, Any]) -> List[str]:
    """Flattened tool names: namespace members as ``namespace/member``, typed tools by type."""
    names: List[str] = []
    for tool in request.get("tools") or []:
        kind = tool.get("type")
        if kind == "namespace":
            names.extend(f"{tool.get('name')}/{m.get('name')}" for m in tool.get("tools") or [])
        elif kind == "function":
            names.append(tool.get("name"))
        else:
            names.append(f"{tool.get('name') or kind} [{kind}]")
    return names


def model_visible_tools(request: Dict[str, Any]) -> List[str]:
    """The tools oMLX 0.7.3 actually puts in front of the model.

    Its Responses layer (``omlx/api/responses_utils.convert_responses_tools``) keeps top-level
    ``function`` tools and the members of ``namespace`` tools, and silently drops every other
    type (``custom``, i.e. the freeform ``apply_patch``; ``web_search``; ...). It also ignores
    input items other than messages, reasoning and function calls/outputs, so tools that
    ``use_responses_lite`` moves into an ``additional_tools`` input item never reach the model.
    """
    names: List[str] = []
    for tool in request.get("tools") or []:
        if tool.get("type") == "function":
            names.append(tool.get("name"))
        elif tool.get("type") == "namespace":
            names.extend(m.get("name") for m in tool.get("tools") or []
                         if m.get("type") == "function")
    return names


def tools_in_input_items(request: Dict[str, Any]) -> List[str]:
    """Tool names carried in ``additional_tools`` input items (``use_responses_lite``)."""
    return [name for item in request.get("input") or [] if item.get("type") == "additional_tools"
            for name in tool_names({"tools": item.get("tools")})]


def _text_of(item: Dict[str, Any]) -> str:
    content = item.get("content")
    if isinstance(content, str):
        return content
    return "".join(c.get("text", "") for c in content or [] if isinstance(c, dict))


def _first_line(text: str) -> str:
    return text.strip().splitlines()[0][:120] if text.strip() else ""


def request_summary(request: Dict[str, Any]) -> Dict[str, Any]:
    developer = [_text_of(item) for item in request.get("input") or []
                 if item.get("role") == "developer" and item.get("type") in (None, "message")]
    instructions = request.get("instructions") or ""
    # Base instructions travel in ``instructions``, or as the first developer message.
    base = instructions or (developer[0] if developer and not developer[0].startswith("<")
                            else "")
    return {
        "tools": tool_names(request),
        "tool_types": sorted({t.get("type") for t in request.get("tools") or []}),
        "model_visible_tools": model_visible_tools(request),
        "tools_in_input_items": tools_in_input_items(request),
        "instructions_field_chars": len(instructions),
        "base_instructions_chars": len(base),
        "base_instructions_sha256": (hashlib.sha256(base.encode()).hexdigest()[:16]
                                     if base else None),
        "base_instructions_first_line": _first_line(base),
        "developer_messages": [{"chars": len(text), "first_line": _first_line(text)}
                               for text in developer],
        "settings": {k: request.get(k) for k in ("reasoning", "parallel_tool_calls",
                                                 "tool_choice", "include", "text",
                                                 "max_output_tokens", "store")
                     if k in request},
    }


def stream_summary(jsonl: str, stderr: str) -> Dict[str, Any]:
    """What a ``codex exec --json`` stream and its stderr say about tools and errors."""
    items: List[Dict[str, Any]] = []
    usage = None
    for line in jsonl.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "item.completed":
            items.append(event.get("item") or {})
        elif event.get("type") == "turn.completed":
            usage = event.get("usage")
    errors = [item.get("message", "") for item in items if item.get("type") == "error"]
    file_changes = [{"status": item.get("status"),
                     "changes": [(c.get("kind"), Path(c.get("path", "")).name)
                                 for c in item.get("changes") or []]}
                    for item in items if item.get("type") == "file_change"]
    commands = [{"command": item.get("command"), "exit_code": item.get("exit_code")}
                for item in items if item.get("type") == "command_execution"]
    return {
        "item_types": dict(collections.Counter(item.get("type") for item in items)),
        "error_items": errors,
        "metadata_missing": any(METADATA_MISSING in message for message in errors),
        "unsupported_calls": UNSUPPORTED_CALL.findall(stderr),
        "stderr_errors": [line.strip() for line in stderr.splitlines() if " ERROR " in line],
        "file_changes": file_changes,
        "commands": commands,
        "usage": usage,
    }


def model_calls(recorder: Recorder) -> List[str]:
    """The tool calls the model emitted, as ``type:name``, in order."""
    calls: List[str] = []
    for exchange in recorder.model_requests():
        for event in exchange["response_events"]:
            item = event.get("item") or {}
            if item.get("type") in ("function_call", "custom_tool_call", "local_shell_call"):
                calls.append(f"{item['type']}:{item.get('name') or ''}")
    return calls


# --------------------------------------------------------------------------------------
# Codex invocation (the launcher's own argv builders)
# --------------------------------------------------------------------------------------
def settings_path() -> Path:
    return Path(os.path.expanduser(str(lw.OMLX_SETTINGS)))


def discover() -> omlx_discovery.ServerProfile:
    """Live discovery (``/health``, ``/v1/models/status``, ``/api/status``; no inference)."""
    return omlx_discovery.discover(settings_path())


def resolve_codex() -> str:
    return lw.resolve_codex_bin(None)


def without_catalog(argv: List[str]) -> List[str]:
    """*argv* with every ``-c model_catalog_json=...`` pair removed."""
    kept: List[str] = []
    index = 0
    while index < len(argv):
        if argv[index] == "-c" and index + 1 < len(argv) \
                and argv[index + 1].startswith("model_catalog_json="):
            index += 2
            continue
        kept.append(argv[index])
        index += 1
    return kept


def with_catalog(argv: List[str], catalog: Optional[Path],
                 extra: Optional[List[str]] = None) -> List[str]:
    """Insert ``-c model_catalog_json=...`` (and *extra*) before the trailing ``-- -``.

    A catalog the argv already names (``worker_argv(..., catalog_path=...)``) is replaced, never
    duplicated; ``catalog=None`` leaves the argv's own catalog, if any, in place.
    """
    if catalog is not None:
        argv = without_catalog(argv)
    added: List[str] = []
    if catalog is not None:
        added += ["-c", f"model_catalog_json={json.dumps(str(catalog))}"]
    added += list(extra or []) + EXTRA_CONFIG
    cut = len(argv) - 2 if argv[-2:] == ["--", "-"] else len(argv)
    return argv[:cut] + added + argv[cut:]


def run_codex(argv: List[str], cwd: Path, env: Dict[str, str], stdin: str,
              timeout: float = CODEX_TIMEOUT_SECONDS) -> Tuple[int, str, str]:
    proc = subprocess.run(argv, cwd=str(cwd), env=env, input=stdin, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


def fresh_dir(path: Path) -> Path:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path


def prepare_pilot(root: Path, timeout_seconds: int) -> Path:
    """A fresh pilot repo + task.json under *root* (scripts/prepare_pilot.py, no inference)."""
    if root.exists():
        shutil.rmtree(root)
    subprocess.run([sys.executable, str(REPO / "scripts" / "prepare_pilot.py"),
                    "--root", str(root)], check=True, stdout=subprocess.DEVNULL)
    task_path = root / "task.json"
    task = json.loads(task_path.read_text(encoding="utf-8"))
    task["timeout_seconds"] = timeout_seconds
    task_path.write_text(json.dumps(task, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return root


def capture_fallback_instructions(codex: str, profile: omlx_discovery.ServerProfile,
                                  work: Path) -> str:
    """The base instructions Codex sends with fallback metadata (no catalog), via the stub."""
    repo = prepare_pilot(work / "fallback-probe", 60) / "repo"
    home = fresh_dir(work / "fallback-probe" / "codex-home")
    with Recorder() as stub:
        stub_profile = dataclasses.replace(profile, base_url=stub.base_url)
        argv = lw.worker_argv(codex, repo, stub_profile, profile.model.context_window,
                              home / "last.txt")
        env, _, _ = lw.worker_env(DUMMY_KEY, home)
        run_codex(argv, repo, env, STUB_PROMPT)
        requests = stub.model_requests()
    if not requests:
        raise SystemExit("fallback probe: Codex sent no request to the stub")
    developer = [_text_of(item) for item in requests[0]["request"].get("input") or []
                 if item.get("role") == "developer" and item.get("type") in (None, "message")]
    text = requests[0]["request"].get("instructions") or (developer[0] if developer else "")
    if not text or text.startswith("<"):
        raise SystemExit("fallback probe: no base instructions found in the request")
    return text


def cloud_entry(codex: str, work: Path) -> Dict[str, Any]:
    """The first entry of ``codex debug models`` (the bundled cloud catalog), throwaway home."""
    home = fresh_dir(work / "debug-models-home")
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""),
           "CODEX_HOME": str(home)}
    proc = subprocess.run([codex, "debug", "models"], env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, timeout=CODEX_TIMEOUT_SECONDS)
    models = json.loads(proc.stdout).get("models") or []
    if not models:
        raise SystemExit("codex debug models returned no models")
    return models[0]


def gather_facts(codex: str, profile: omlx_discovery.ServerProfile, work: Path) -> Facts:
    model = profile.model
    return Facts(
        slug=model.api_model,
        display_name=model.api_model,
        context_window=model.context_window,
        input_modalities=["text", "image"] if model.accepts_images else ["text"],
        fallback_instructions=capture_fallback_instructions(codex, profile, work),
        cloud_entry=cloud_entry(codex, work),
    )


def write_catalog(entry: Optional[Dict[str, Any]], home: Path) -> Optional[Path]:
    if entry is None:
        return None
    path = home / CATALOG_NAME
    path.write_text(json.dumps({"models": [entry]}, indent=2) + "\n", encoding="utf-8")
    return path


def prompt_input(codex: str, profile: omlx_discovery.ServerProfile,
                 entry: Optional[Dict[str, Any]], repo: Path,
                 home: Path) -> "subprocess.CompletedProcess[str]":
    """``codex debug prompt-input`` with the worker's own ``-c`` overrides (and the catalog).

    It takes no ``--ignore-user-config``; the fresh *home* has no config to load anyway. It
    sends nothing to any server: it only renders (or refuses to load) the catalog.
    """
    argv = [codex, "debug", "prompt-input"]
    for key, value in config_override_pairs_for(profile):
        argv += ["-c", f"{key}={value}"]
    catalog = write_catalog(entry, home)
    if catalog is not None:
        argv += ["-c", f"model_catalog_json={json.dumps(str(catalog))}"]
    argv += EXTRA_CONFIG + [STUB_PROMPT]
    env, _, _ = lw.worker_env(DUMMY_KEY, home)
    return subprocess.run(argv, cwd=str(repo), env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, timeout=CODEX_TIMEOUT_SECONDS)


def config_override_pairs_for(profile: omlx_discovery.ServerProfile) -> List[Tuple[str, str]]:
    return lw.config_override_pairs(lw.config_overrides(profile, profile.model.context_window))


# --------------------------------------------------------------------------------------
# schema: which fields Codex requires, which values it accepts (no inference)
# --------------------------------------------------------------------------------------
LOAD_ERROR = re.compile(r"as JSON: (.*)")
# Field -> a value Codex should reject, so its parse error lists the accepted spellings.
ENUM_PROBES: Dict[str, Any] = {
    "tool_mode": "bogus",
    "apply_patch_tool_type": "bogus",
    "shell_type": "bogus",
    "truncation_policy": {"mode": "bogus", "limit": 1},
    "input_modalities": ["bogus"],
    "visibility": "bogus",
    "web_search_tool_type": "bogus",
    "default_verbosity": "bogus",
    "default_reasoning_summary": "bogus",
    "multi_agent_version": "bogus",
}


def _load_error(proc: "subprocess.CompletedProcess[str]") -> Optional[str]:
    if proc.returncode == 0:
        return None
    match = LOAD_ERROR.search(proc.stderr)
    return (match.group(1) if match else proc.stderr.strip())[:300]


def cmd_schema(args: argparse.Namespace) -> int:
    out = Path(args.out).absolute()
    out.mkdir(parents=True, exist_ok=True)
    codex = resolve_codex()
    profile = discover()
    facts = gather_facts(codex, profile, out / "_facts")
    repo = prepare_pilot(out / "pilot", 60) / "repo"
    # Required fields: start from the slug alone and add whatever the parser names as missing,
    # taking the value from the candidate base entry (else the cloud entry).
    donors = {**facts.cloud_entry, **local_base(facts)}
    entry: Dict[str, Any] = {"slug": facts.slug}
    required: List[str] = []
    last_error: Optional[str] = None
    for _ in range(len(donors) + 1):
        last_error = _load_error(prompt_input(codex, profile, entry, repo,
                                              fresh_dir(out / "home")))
        missing = re.search(r"missing field `(\w+)`", last_error or "")
        if not missing:
            break
        required.append(missing.group(1))
        entry[missing.group(1)] = donors[missing.group(1)]
    print(f"required fields: {', '.join(required)}")
    print(f"then: {last_error or 'loads'}")
    enums: Dict[str, str] = {}
    for field, bogus in ENUM_PROBES.items():
        probe = {**minimal_entry(facts), field: bogus}
        error = _load_error(prompt_input(codex, profile, probe, repo, fresh_dir(out / "home")))
        enums[field] = error or "accepted (no validation of this value)"
        print(f"{field}: {enums[field]}")
    summary = {"codex_version": codex_version(codex), "required_fields": required,
               "after_required": last_error, "enum_probes": enums}
    (out / "schema-summary.json").write_text(json.dumps(summary, indent=2) + "\n",
                                             encoding="utf-8")
    print(f"summary: {out / 'schema-summary.json'}")
    return 0


# --------------------------------------------------------------------------------------
# render
# --------------------------------------------------------------------------------------
def render_variant(name: str, codex: str, profile: omlx_discovery.ServerProfile,
                   facts: Facts, out: Path) -> Dict[str, Any]:
    _, build = VARIANTS[name]
    work = fresh_dir(out / name)
    repo = prepare_pilot(work / "pilot", 60) / "repo"
    home = fresh_dir(work / "codex-home")
    entry = build(facts) if build else None
    catalog = write_catalog(entry, home)
    result: Dict[str, Any] = {"variant": name, "catalog": entry is not None}
    with Recorder() as stub:
        stub_profile = dataclasses.replace(profile, base_url=stub.base_url)
        argv = with_catalog(lw.worker_argv(codex, repo, stub_profile,
                                           profile.model.context_window, home / "last.txt"),
                            catalog)
        env, _, _ = lw.worker_env(DUMMY_KEY, home)
        code, stdout, stderr = run_codex(argv, repo, env, STUB_PROMPT)
        requests = stub.model_requests()
    (work / "exec.jsonl").write_text(stdout, encoding="utf-8")
    (work / "exec.stderr.txt").write_text(stderr, encoding="utf-8")
    result["exec_returncode"] = code
    result["stream"] = stream_summary(stdout, stderr)
    if code != 0 and not requests:
        result["load_error"] = (stderr.strip().splitlines() or [""])[-1][:400]
    if requests:
        (work / "request.json").write_text(json.dumps(requests[0]["request"], indent=1),
                                           encoding="utf-8")
        result["request"] = request_summary(requests[0]["request"])

    proc = prompt_input(codex, profile, entry, repo, fresh_dir(work / "prompt-input-home"))
    (work / "prompt-input.json").write_text(proc.stdout, encoding="utf-8")
    rendered: Dict[str, Any] = {"returncode": proc.returncode}
    try:
        items = json.loads(proc.stdout)
        rendered["items"] = [{"role": item.get("role"), "chars": len(_text_of(item)),
                              "first_line": _first_line(_text_of(item))} for item in items]
    except ValueError:
        rendered["error"] = (proc.stderr.strip().splitlines() or [""])[-1][:400]
    result["prompt_input"] = rendered
    result["verdict"] = render_verdict(result)
    (work / "summary.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def render_verdict(result: Dict[str, Any]) -> Dict[str, Any]:
    """Step 2's static gate, judged on what the model really sees through oMLX.

    Pass: Codex loaded the entry (no fallback metadata), the model sees a shell tool, no
    code-mode/multi-agent tool is offered, and the base instructions are not the cloud ones.
    The patch path is reported separately: ``apply_patch`` run through the shell tool is
    intercepted by Codex and applied by its patch engine (proved by ``calls``), while a
    registered freeform ``apply_patch`` is a ``custom`` tool that oMLX drops before the model.
    """
    request = result.get("request")
    if not request:
        return {"pass": False, "why": ["Codex sent no request (catalog refused?)"], "notes": []}
    visible = request["model_visible_tools"]
    offered = {name.split(" [")[0].split("/")[-1] for name in
               request["tools"] + request["tools_in_input_items"]}
    why, notes = [], []
    if request["tools_in_input_items"]:
        why.append("tools sent in an additional_tools input item: oMLX shows the model none")
    if not set(visible) & SHELL_TOOLS:
        why.append("no shell tool the model can see")
    code_mode = sorted(offered & CODE_MODE_TOOLS)
    if code_mode:
        why.append("code-mode/multi-agent tools: " + ", ".join(code_mode))
    if result["stream"]["metadata_missing"]:
        why.append("fallback metadata")
    if request["base_instructions_first_line"].startswith("You are Codex, an agent based on GPT"):
        why.append("cloud base instructions")
    if not request["base_instructions_chars"]:
        notes.append("no base instructions")
    if "apply_patch" in offered:
        notes.append("freeform apply_patch registered (custom tool: dropped by oMLX)")
    if set(visible) & SHELL_TOOLS:
        notes.append("apply_patch reachable through the shell tool")
    for name in sorted(offered & set(FEATURE_TOOLS)):
        notes.append(f"{name}: {FEATURE_TOOLS[name]}")
    return {"pass": not why, "why": why, "notes": notes}


def cmd_render(args: argparse.Namespace) -> int:
    out = Path(args.out).absolute()
    out.mkdir(parents=True, exist_ok=True)
    codex = resolve_codex()
    profile = discover()
    facts = gather_facts(codex, profile, out / "_facts")
    names = args.only.split(",") if args.only else list(VARIANTS)
    if not TEMPLATE_PATH.exists():
        names = [name for name in names if not name.startswith("template")]
    results = [render_variant(name, codex, profile, facts, out) for name in names]
    summary = {"codex_version": codex_version(codex), "model": facts.slug,
               "context_window": facts.context_window,
               "input_modalities": facts.input_modalities,
               "fallback_instructions_chars": len(facts.fallback_instructions),
               "variants": results}
    (out / "render-summary.json").write_text(json.dumps(summary, indent=2) + "\n",
                                             encoding="utf-8")
    for result in results:
        request = result.get("request") or {}
        verdict = result["verdict"]
        print(f"== {result['variant']}: {'PASS' if verdict['pass'] else 'fail'} "
              f"{'; '.join(verdict['why'])}")
        if verdict.get("notes"):
            print(f"   notes: {'; '.join(verdict['notes'])}")
        if result.get("load_error"):
            print(f"   load error: {result['load_error']}")
        if request:
            print(f"   tools: {', '.join(request['tools']) or '(none)'}")
            if request["tools_in_input_items"]:
                print(f"   tools in input items: {', '.join(request['tools_in_input_items'])}")
            print(f"   model sees: {', '.join(request['model_visible_tools']) or '(none)'}")
            print(f"   base instructions: {request['base_instructions_chars']} chars "
                  f"[{request['base_instructions_first_line'][:70]}]")
        if result["stream"]["error_items"]:
            print(f"   error items: {[m[:90] for m in result['stream']['error_items']]}")
    print(f"summary: {out / 'render-summary.json'}")
    return 0


# --------------------------------------------------------------------------------------
# calls: how Codex handles each way a model can ask for a patch (scripted, no inference)
# --------------------------------------------------------------------------------------
PILOT_PATCH = ("*** Begin Patch\n*** Update File: add.py\n@@ def add(a, b):\n"
               "-    return a - b\n+    return a + b\n*** End Patch\n")


def _call(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    return {"type": "function_call", "id": "fc_spike", "call_id": "call_spike", "name": name,
            "arguments": json.dumps(arguments), "status": "completed"}


# What oMLX can hand back is only ``function_call`` items (it drops ``custom`` tools from the
# model's view and never emits ``custom_tool_call``); the custom call is a control.
CALL_PROBES: Dict[str, Dict[str, Any]] = {
    "function apply_patch {input}": _call("apply_patch", {"input": PILOT_PATCH}),
    "function apply_patch {patch}": _call("apply_patch", {"patch": PILOT_PATCH}),
    "function apply_patch {command}": _call("apply_patch", {"command": ["apply_patch",
                                                                        PILOT_PATCH]}),
    "exec_command apply_patch heredoc": _call(
        "exec_command", {"cmd": "apply_patch <<'EOF'\n" + PILOT_PATCH + "EOF\n"}),
    "custom apply_patch (control)": {"type": "custom_tool_call", "id": "ctc_spike",
                                     "call_id": "call_spike", "name": "apply_patch",
                                     "input": PILOT_PATCH, "status": "completed"},
    # Not a patch: how much of a 60,000-character command output reaches the model
    # (truncation_policy).
    "exec_command 60k output": _call("exec_command",
                                     {"cmd": "python3 -c \"print('x' * 60000)\""}),
}


def _call_output(requests: List[Dict[str, Any]]) -> Tuple[str, int]:
    """What Codex answered the scripted call with (the output item in the next request), as
    a short excerpt and the full length in characters."""
    for exchange in requests[1:]:
        for item in exchange["request"].get("input") or []:
            if item.get("call_id") == "call_spike" and str(item.get("type")).endswith("output"):
                output = item.get("output")
                text = output if isinstance(output, str) else json.dumps(output)
                return f"{item.get('type')}: {text[:200]}", len(text)
    return "(no output item returned)", 0


def probe_call(variant: str, probe: str, codex: str, profile: omlx_discovery.ServerProfile,
               facts: Facts, out: Path) -> Dict[str, Any]:
    _, build = VARIANTS[variant]
    work = fresh_dir(out / variant / re.sub(r"[^a-z0-9]+", "-", probe.lower()).strip("-"))
    repo = prepare_pilot(work / "pilot", 60) / "repo"
    home = fresh_dir(work / "codex-home")
    catalog = write_catalog(build(facts) if build else None, home)
    with Recorder(script=[CALL_PROBES[probe]]) as stub:
        stub_profile = dataclasses.replace(profile, base_url=stub.base_url)
        argv = with_catalog(lw.worker_argv(codex, repo, stub_profile,
                                           profile.model.context_window, home / "last.txt"),
                            catalog)
        env, _, _ = lw.worker_env(DUMMY_KEY, home)
        code, stdout, stderr = run_codex(argv, repo, env, STUB_PROMPT)
        requests = stub.model_requests()
    (work / "exec.jsonl").write_text(stdout, encoding="utf-8")
    (work / "exec.stderr.txt").write_text(stderr, encoding="utf-8")
    (work / "exchanges.json").write_text(json.dumps(stub.exchanges, indent=1), encoding="utf-8")
    stream = stream_summary(stdout, stderr)
    fixed = "return a + b" in (repo / "add.py").read_text(encoding="utf-8")
    answer, answer_chars = _call_output(requests)
    return {"variant": variant, "probe": probe, "exec_returncode": code, "file_fixed": fixed,
            "model_requests": len(requests), "turn_completed": '"turn.completed"' in stdout,
            "file_changes": stream["file_changes"],
            "commands": [c["command"] for c in stream["commands"]],
            "unsupported_calls": stream["unsupported_calls"],
            "stderr_errors": stream["stderr_errors"], "answer": answer,
            "answer_chars": answer_chars}


def cmd_calls(args: argparse.Namespace) -> int:
    out = Path(args.out).absolute()
    out.mkdir(parents=True, exist_ok=True)
    codex = resolve_codex()
    profile = discover()
    facts = gather_facts(codex, profile, out / "_facts")
    results = []
    for variant in args.variant:
        for probe in CALL_PROBES:
            result = probe_call(variant, probe, codex, profile, facts, out)
            results.append(result)
            print(f"== {variant} | {probe}: fixed={result['file_fixed']} "
                  f"file_change={result['file_changes']} unsupported={result['unsupported_calls']}")
            print(f"   answer ({result['answer_chars']} chars): "
                  f"{result['answer'][:110]!r}")
            for line in result["stderr_errors"]:
                print(f"   stderr: {line[28:][:150]}")
    (out / "calls-summary.json").write_text(json.dumps({"codex_version": codex_version(codex),
                                                        "results": results}, indent=2) + "\n",
                                            encoding="utf-8")
    print(f"summary: {out / 'calls-summary.json'}")
    return 0


# --------------------------------------------------------------------------------------
# pilot
# --------------------------------------------------------------------------------------
WRAPPER = '''#!{python}
# Spike wrapper: adds the variant's overrides to a `codex exec` argv, then runs the real Codex.
# With SWAP_CATALOG the launcher's own `-c model_catalog_json=...` is dropped first, so the
# worker runs on the variant's catalog (or, with none in EXTRA, on Codex fallback metadata).
import os, sys
REAL = {real!r}
EXTRA = {extra!r}
SWAP_CATALOG = {swap!r}
argv = sys.argv[1:]
if argv[:1] == ["exec"] and SWAP_CATALOG:
    kept, index = [], 0
    while index < len(argv):
        if argv[index] == "-c" and index + 1 < len(argv) \\
                and argv[index + 1].startswith("model_catalog_json="):
            index += 2
            continue
        kept.append(argv[index])
        index += 1
    argv = kept
if argv[:1] == ["exec"] and EXTRA:
    cut = len(argv) - 2 if argv[-2:] == ["--", "-"] else len(argv)
    argv = argv[:cut] + EXTRA + argv[cut:]
os.execv(REAL, [REAL] + argv)
'''

# The variant whose catalog the launcher itself generates (from the same template), so its
# pilot runs the launcher's own catalog instead of a swapped-in copy.
LAUNCHER_VARIANT = "template"


def pilot_variant(name: str, run: int, codex: str, facts: Facts, out: Path, timeout: int,
                  record: bool, upstream: str) -> Dict[str, Any]:
    _, build = VARIANTS[name]
    root = prepare_pilot(out / f"{name}-{run}", timeout)
    home = fresh_dir(root / "codex-home")
    swap = name != LAUNCHER_VARIANT
    entry = build(facts) if build and swap else None
    catalog = write_catalog(entry, home)
    recorder = Recorder(upstream) if record else None
    extra: List[str] = []
    if catalog is not None:
        extra += ["-c", f"model_catalog_json={json.dumps(str(catalog))}"]
    if recorder is not None:
        extra += ["-c", f'model_providers.{lw.PROVIDER_ID}.base_url="{recorder.base_url}/v1"']
    extra += EXTRA_CONFIG
    wrapper = root / "codex-wrapper.py"
    wrapper.write_text(WRAPPER.format(python=sys.executable, real=codex, extra=extra,
                                      swap=swap), encoding="utf-8")
    wrapper.chmod(0o755)
    argv = [str(REPO / "bin" / "local-worker"), "run", "--task-file", str(root / "task.json"),
            "--state-dir", str(root / "state"), "--json", "--codex-bin", str(wrapper)]
    env = {**os.environ, "CODEX_HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1"}
    started = time.monotonic()
    if recorder is not None:
        recorder.__enter__()
    try:
        proc = subprocess.run(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, timeout=timeout + 120)
    finally:
        if recorder is not None:
            recorder.__exit__(None, None, None)
    wall = round(time.monotonic() - started, 1)
    result: Dict[str, Any] = {"variant": name, "run": run,
                              "catalog": "variant" if entry is not None
                              else ("none" if swap else "launcher"),
                              "launcher_exit": proc.returncode, "wall_seconds": wall}
    reports = sorted((root / "state" / "reports").glob("*/report.json"))
    if not reports:
        result["error"] = (proc.stderr.strip().splitlines() or [""])[-1][:400]
        return result
    report_dir = reports[-1].parent
    report = json.loads(reports[-1].read_text(encoding="utf-8"))
    jsonl = _read(report_dir / "attempt-1.jsonl")
    stderr = _read(report_dir / "attempt-1.stderr.txt")
    result.update({
        "status": report.get("status"),
        "tests": [t.get("returncode") for t in report.get("tests") or []],
        "changed_paths": report.get("changed_paths"),
        "violations": report.get("violations"),
        "worker_seconds": (report.get("timing") or {}).get("worker_seconds"),
        "usage": {k: (report.get("usage") or {}).get(k)
                  for k in ("input_tokens", "cached_input_tokens", "output_tokens")},
        "stream": stream_summary(jsonl, stderr),
        "patch_excerpt": report.get("patch_excerpt"),
        "catalog_sha256": report.get("catalog_sha256"),
    })
    if recorder is not None:
        requests = recorder.model_requests()
        result["model_requests"] = len(requests)
        result["tools_offered"] = tool_names(requests[0]["request"]) if requests else []
        result["model_calls"] = model_calls(recorder)
        (root / "exchanges.json").write_text(json.dumps(recorder.exchanges, indent=1),
                                             encoding="utf-8")
    result["verdict"] = pilot_verdict(result)
    (root / "summary.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def pilot_verdict(result: Dict[str, Any]) -> Dict[str, Any]:
    """Step 2's live gate: no fallback metadata, no unsupported call, patch applied by the
    patch tool, and the launcher's own verification green."""
    stream = result.get("stream") or {}
    why = []
    if stream.get("metadata_missing"):
        why.append("fallback metadata")
    if stream.get("unsupported_calls"):
        why.append("unsupported call: " + ", ".join(stream["unsupported_calls"]))
    applied = [c for c in stream.get("file_changes") or [] if c.get("status") == "completed"]
    if not applied:
        why.append("no change applied by the patch tool")
    if result.get("status") != "ok":
        why.append(f"launcher status {result.get('status')}")
    return {"pass": not why, "why": why}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


def cmd_pilot(args: argparse.Namespace) -> int:
    out = Path(args.out).absolute()
    out.mkdir(parents=True, exist_ok=True)
    codex = resolve_codex()
    profile = discover()
    facts = gather_facts(codex, profile, out / "_facts")
    upstream = lw.profile_endpoint(profile)[: -len(lw.API_PREFIX)]
    results = []
    for run in range(1, args.repeat + 1):
        for name in args.variant:
            result = pilot_variant(name, run, codex, facts, out, args.timeout, args.record,
                                   upstream)
            results.append(result)
            stream = result.get("stream") or {}
            verdict = result.get("verdict") or {"pass": False, "why": [result.get("error")]}
            print(f"== {name} #{run}: {'PASS' if verdict['pass'] else 'fail'} "
                  f"{'; '.join(verdict['why'])}")
            print(f"   status={result.get('status')} tests={result.get('tests')} "
                  f"worker={result.get('worker_seconds')}s usage={result.get('usage')}")
            print(f"   items={stream.get('item_types')} "
                  f"unsupported={stream.get('unsupported_calls')}")
            print(f"   file_changes={stream.get('file_changes')}")
            if "model_calls" in result:
                print(f"   model calls: {result['model_calls']}")
    summary = {"codex_version": codex_version(codex), "model": facts.slug,
               "context_window": facts.context_window, "variants": results}
    (out / "pilot-summary.json").write_text(json.dumps(summary, indent=2) + "\n",
                                            encoding="utf-8")
    print(f"summary: {out / 'pilot-summary.json'}")
    return 0 if all((r.get("verdict") or {}).get("pass") for r in results) else 1


def codex_version(codex: str) -> str:
    proc = subprocess.run([codex, "--version"], stdout=subprocess.PIPE, text=True,
                          timeout=CODEX_TIMEOUT_SECONDS)
    return proc.stdout.strip()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    render = sub.add_parser("render", help="tool list per variant via a loopback stub "
                                           "(no inference)")
    render.add_argument("--out", required=True, help="scratch directory for the evidence")
    render.add_argument("--only", help="comma-separated variant names (default: all)")
    pilot = sub.add_parser("pilot", help="real pilot job per variant (local inference)")
    pilot.add_argument("--out", required=True, help="scratch directory for the evidence")
    pilot.add_argument("--variant", action="append", required=True, choices=sorted(VARIANTS))
    pilot.add_argument("--timeout", type=int, default=PILOT_TIMEOUT_SECONDS,
                       help="pilot task timeout_seconds (default %(default)s)")
    pilot.add_argument("--repeat", type=int, default=1,
                       help="run every variant this many times (default %(default)s)")
    pilot.add_argument("--record", action="store_true",
                       help="route the worker through a loopback recording proxy")
    schema = sub.add_parser("schema", help="required fields and accepted values "
                                           "(no inference)")
    schema.add_argument("--out", required=True, help="scratch directory for the evidence")
    calls = sub.add_parser("calls", help="scripted patch calls per variant via the stub "
                                         "(no inference)")
    calls.add_argument("--out", required=True, help="scratch directory for the evidence")
    calls.add_argument("--variant", action="append", required=True, choices=sorted(VARIANTS))
    sub.add_parser("variants", help="list the catalog variants")
    for command in (schema, render, calls, pilot):
        command.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                             help="extra -c override for every worker run (repeatable)")
    args = parser.parse_args(argv)
    for item in getattr(args, "set", None) or []:
        EXTRA_CONFIG.extend(["-c", item])
    if args.command == "variants":
        for name, (what, _) in VARIANTS.items():
            print(f"{name:20} {what}")
        return 0
    commands = {"schema": cmd_schema, "render": cmd_render, "calls": cmd_calls,
                "pilot": cmd_pilot}
    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
