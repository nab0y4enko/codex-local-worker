#!/usr/bin/env python3
"""Record the real oMLX and Codex payloads that the plugin's tests replay.

Run it by hand against a healthy local oMLX that has exactly one chat model loaded; it never
triggers inference. It writes, under --out:

    omlx/health_ok.json          GET /health                (HTTP 200 body, recorded as served)
    omlx/api_status.json         GET /api/status
    omlx/models_status_one.json  GET /v1/models/status
    codex/exec_pilot.jsonl       a real `codex exec --json` worker stream (--pilot-attempt)
    codex/debug_models.json      `codex debug models`, trimmed to the fields the plugin reads

Only those five files are written. The synthetic variants (health_loading, the none/two/helper
oMLX status files) and codex/rollout_planner.jsonl are written by hand from them and are never
touched by this script. A recorded HTTP status other than 200 is stored as ``_http_status`` in
the body; a body with no ``_http_status`` was served with 200.

Privacy: the oMLX API key is read from settings.json and sent only as a request header, to a
loopback host. It is never printed or written. Before anything touches disk, every payload has
`api_key`, `secret_key` and `sub_keys` dropped and the home directory replaced with `~`, and
the script refuses to write a file that still contains a credential, the home path, or a
`/Users/` or `/private/tmp/claude` path. Scratch paths in a recorded stream become `<scratch>`
(`<scratch>/codex-home` for the throwaway Codex home).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# oMLX's own documented default, used only when settings.json omits server.port.
DEFAULT_PORT = 8000
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
HTTP_TIMEOUT_SECONDS = 15
CODEX_TIMEOUT_SECONDS = 60
DROP_KEYS = frozenset({"api_key", "secret_key", "sub_keys"})
CATALOG_FIELDS = ("slug", "display_name", "description", "context_window",
                  "supported_reasoning_levels", "default_reasoning_level", "visibility")
CODEX_BIN_ENV = "LOCAL_WORKER_CODEX_BIN"
LEGACY_CODEX_BIN_ENV = "LOCAL_IMPLEMENT_CODEX_BIN"
MIN_SECRET_LENGTH = 8
# The repository is public. A recorded stream can name the throwaway Codex home or scratch
# directory of the machine that produced it; those become neutral placeholders.
SCRATCH_CODEX_HOME = re.compile(r"/private/tmp/claude[^\s\"'`\\]*/codexhome")
SCRATCH_PATH = re.compile(r"/private/tmp/claude[^\s\"'`\\]*")
# Never allowed in a written fixture, whatever produced it.
FORBIDDEN_MARKERS = ("/Users/", "/private/tmp/claude")

# (base_url, path, api_key or None) -> (HTTP status, decoded JSON body)
Fetch = Callable[[str, str, Optional[str]], Tuple[int, Any]]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def http_get(base_url: str, path: str, api_key: Optional[str]) -> Tuple[int, Any]:
    """GET a loopback oMLX endpoint: no proxies, no redirects, key only in the header."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    headers = {"Authorization": "Bearer " + api_key} if api_key else {}
    request = urllib.request.Request(base_url + path, headers=headers)
    try:
        with opener.open(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    except (urllib.error.URLError, OSError) as error:
        raise SystemExit("cannot reach oMLX at {0}{1}: {2}".format(
            base_url, path, getattr(error, "reason", error)))
    try:
        return status, json.loads(raw.decode("utf-8"))
    except ValueError:
        raise SystemExit("oMLX {0} (HTTP {1}) did not return JSON".format(path, status))


def read_omlx_settings(path: Path) -> Tuple[str, Optional[str]]:
    """Return ``(base_url, api_key)`` from oMLX's settings.json; loopback hosts only."""
    try:
        settings = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SystemExit("cannot read oMLX settings {0}: {1}".format(path, error))
    server = settings.get("server") or {}
    host = server.get("host") or "127.0.0.1"
    if host not in LOOPBACK_HOSTS:
        raise SystemExit("refusing a non-loopback oMLX host {0!r}".format(host))
    port = server.get("port", DEFAULT_PORT)
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
        raise SystemExit("oMLX settings server.port is not a valid port")
    netloc = "[::1]" if host == "::1" else host
    key = (settings.get("auth") or {}).get("api_key")
    return "http://{0}:{1}".format(netloc, port), key if isinstance(key, str) and key else None


def credential_values(path: Path) -> List[str]:
    """Every credential-sized string under settings.json's ``auth`` block, to scan outputs for."""
    try:
        auth = json.loads(Path(path).read_text(encoding="utf-8")).get("auth") or {}
    except (OSError, ValueError):
        return []
    found: List[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, str) and len(value) >= MIN_SECRET_LENGTH:
            found.append(value)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(auth)
    return found


def _home_token(home: str) -> str:
    return home.rstrip("/") or home


def sanitize_text(text: str, home: str) -> str:
    """Replace the home directory with ``~`` and scratch paths with neutral placeholders."""
    text = text.replace(_home_token(home), "~")
    text = SCRATCH_CODEX_HOME.sub("<scratch>/codex-home", text)
    return SCRATCH_PATH.sub("<scratch>", text)


def sanitize(value: Any, home: str) -> Any:
    """Drop credential keys everywhere and replace the home directory with ``~``."""
    if isinstance(value, dict):
        return {key: sanitize(item, home) for key, item in value.items() if key not in DROP_KEYS}
    if isinstance(value, list):
        return [sanitize(item, home) for item in value]
    if isinstance(value, str):
        return sanitize_text(value, home)
    return value


def trim_catalog(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Keep the fields the plugin reads from each ``codex debug models`` entry."""
    models = raw.get("models")
    if not isinstance(models, list):
        raise SystemExit("codex model catalog has no `models` list")
    return {"models": [{field: model[field] for field in CATALOG_FIELDS if field in model}
                       for model in models]}


def codex_catalog(codex_bin: str) -> Dict[str, Any]:
    """Run ``codex debug models`` (a catalog read, no model call) and decode its output."""
    try:
        proc = subprocess.run([codex_bin, "debug", "models"], capture_output=True,
                              timeout=CODEX_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SystemExit("cannot run `{0} debug models`: {1}".format(codex_bin, error))
    if proc.returncode != 0:
        raise SystemExit("`{0} debug models` exited {1}".format(codex_bin, proc.returncode))
    return decode_json(proc.stdout.decode("utf-8", "replace"), "`codex debug models` output")


def decode_json(text: str, label: str) -> Dict[str, Any]:
    try:
        value = json.loads(text)
    except ValueError as error:
        raise SystemExit("{0} is not JSON: {1}".format(label, error))
    if not isinstance(value, dict):
        raise SystemExit("{0} is not a JSON object".format(label))
    return value


def dump_json(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def checked_jsonl(text: str, label: str) -> str:
    """The stream as recorded, after proving every non-blank line is JSON."""
    for number, line in enumerate(text.splitlines(), 1):
        if line.strip():
            try:
                json.loads(line)
            except ValueError:
                raise SystemExit("{0} line {1} is not JSON".format(label, number))
    return text if text.endswith("\n") else text + "\n"


def require_healthy_server(fetch: Fetch, base_url: str) -> Dict[str, Any]:
    status, health = fetch(base_url, "/health", None)
    if status != 200 or not isinstance(health, dict) or health.get("status") != "healthy":
        raise SystemExit("oMLX /health is HTTP {0} / {1!r}; wait for a healthy server with one "
                         "loaded chat model and run again".format(
                             status, health.get("status") if isinstance(health, dict) else None))
    return health


def authed_get(fetch: Fetch, base_url: str, path: str, key: Optional[str]) -> Dict[str, Any]:
    status, body = fetch(base_url, path, key)
    if status != 200 or not isinstance(body, dict):
        raise SystemExit("oMLX {0} returned HTTP {1}".format(path, status))
    return body


def capture(out_dir: Path, *, settings_path: Path, pilot_attempt: Path,
            codex_models_json: Optional[Path] = None, codex_bin: str = "codex",
            fetch: Fetch = http_get, home: Optional[str] = None) -> List[Path]:
    """Record the five real fixtures under ``out_dir``; write nothing unless all are clean."""
    home = home if home is not None else os.path.expanduser("~")
    base_url, key = read_omlx_settings(settings_path)
    health = require_healthy_server(fetch, base_url)
    api_status = authed_get(fetch, base_url, "/api/status", key)
    models_status = authed_get(fetch, base_url, "/v1/models/status", key)
    loaded = [m for m in models_status.get("models", []) if m.get("loaded")]
    if len(loaded) != 1:
        raise SystemExit("models_status_one needs exactly one loaded model; oMLX reports "
                         "{0}".format(len(loaded)))
    if codex_models_json is not None:
        try:
            catalog_text = Path(codex_models_json).read_text(encoding="utf-8")
        except OSError as error:
            raise SystemExit("cannot read {0}: {1}".format(codex_models_json, error))
        catalog = decode_json(catalog_text, str(codex_models_json))
    else:
        catalog = codex_catalog(codex_bin)
    try:
        pilot_text = Path(pilot_attempt).read_text(encoding="utf-8")
    except OSError as error:
        raise SystemExit("cannot read pilot stream {0}: {1}".format(pilot_attempt, error))

    files = {
        "omlx/health_ok.json": dump_json(sanitize(health, home)),
        "omlx/api_status.json": dump_json(sanitize(api_status, home)),
        "omlx/models_status_one.json": dump_json(sanitize(models_status, home)),
        "codex/exec_pilot.jsonl": sanitize_text(checked_jsonl(pilot_text, "pilot stream"), home),
        "codex/debug_models.json": dump_json(sanitize(trim_catalog(catalog), home)),
    }
    secrets = set(credential_values(settings_path))
    if key:
        secrets.add(key)
    for relative, text in files.items():
        if any(secret in text for secret in secrets):
            raise SystemExit("refusing to write {0}: it contains an oMLX credential".format(
                relative))
        if _home_token(home) in text or any(marker in text for marker in FORBIDDEN_MARKERS):
            raise SystemExit("refusing to write {0}: it still contains a personal or scratch "
                             "path".format(relative))
    written = []
    for relative, text in files.items():
        target = Path(out_dir) / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        written.append(target)
    return written


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True, type=Path,
                        help="fixtures directory to write (normally tests/fixtures)")
    parser.add_argument("--pilot-attempt", required=True, type=Path,
                        help="attempt-1.jsonl from a real pilot run, recorded as exec_pilot.jsonl")
    parser.add_argument("--codex-models-json", type=Path,
                        help="saved `codex debug models` output to use instead of running it")
    parser.add_argument("--codex-bin", help="Codex CLI (default ${0}, ${1}, or `codex`)".format(
        CODEX_BIN_ENV, LEGACY_CODEX_BIN_ENV))
    parser.add_argument("--omlx-settings", type=Path,
                        default=Path(os.path.expanduser("~/.omlx/settings.json")))
    args = parser.parse_args(argv)
    codex_bin = (args.codex_bin or os.environ.get(CODEX_BIN_ENV)
                 or os.environ.get(LEGACY_CODEX_BIN_ENV) or "codex")
    for path in capture(args.out, settings_path=args.omlx_settings,
                        pilot_attempt=args.pilot_attempt,
                        codex_models_json=args.codex_models_json, codex_bin=codex_bin):
        print("wrote {0}".format(path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
