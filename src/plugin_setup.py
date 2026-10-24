#!/usr/bin/env python3
"""Setup helper for the local-qwen plugin: stdlib only, portable, read-only by default.

Run it from any working directory; the plugin root is inferred from this file (``src`` is one
level below the root), so an installed copy works wherever the plugin lands:

    python3 <root>/src/plugin_setup.py --check                  # read-only report (default)
    python3 <root>/src/plugin_setup.py --apply                  # authorized installation
    python3 <root>/src/plugin_setup.py --check --preflight      # also ask the packaged launcher

``$CODEX_HOME`` (default ``~/.codex``) and ``$LOCAL_IMPLEMENT_STATE_DIR`` (default
``~/.local-implement``) are respected. stdout is one JSON document; nothing is written by
``--check``, no credential or settings content is ever read or printed, and no config, provider,
model or project file is touched by ``--apply``.

Exit codes: 0 ok, 2 usage, 3 malformed package or canonical policy, 4 unusable managed block,
5 apply failed, 6 explicitly requested preflight failed, 7 a nonempty or unreadable
``$CODEX_HOME/AGENTS.override.md`` supersedes (or hides) the managed block.

Codex discovery reads a nonempty ``AGENTS.override.md`` before ``AGENTS.md``, so a nonempty
override is reported as a blocker: ``--check`` exits nonzero and ``--apply`` refuses before any
backup or write. Presence and emptiness are inspected - the file is read only to decide whether it
is empty - and its contents are never printed or disclosed.
"""
import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
PLUGIN_NAME = "local-qwen"
POLICY_RELATIVE = ("skills", "local-implement", "references", "global-policy.md")
SKILL_RELATIVE = ("skills", "local-implement", "SKILL.md")
SETUP_RELATIVE = ("src", "plugin_setup.py")
LAUNCHER_ENGINE_RELATIVE = ("src", "local_worker.py")
SKILLS = ("local-implement", "setup", "doctor")
LEGACY_RELATIVE = ("evidence", "global-default", "AGENTS.md")
# sha256 of the historical global AGENTS.md that ships in this package as a migration input.
# Kept here so a package copy without evidence/ still recognizes (and migrates) the legacy file.
LEGACY_SHA256 = "cb1192774bb1f358c9df93780c16c3ff2388e5440651bcc98429a0635bb75560"
LEGACY_HEADER = "# User-wide Codex defaults"

MARKER_BEGIN = "<!-- local-qwen begin -->"
MARKER_END = "<!-- local-qwen end -->"
BACKUP_DIRNAME = "setup-backups"
CODEX_HOME_ENV = "CODEX_HOME"
STATE_DIR_ENV = "LOCAL_IMPLEMENT_STATE_DIR"
CODEX_BIN_ENV = "LOCAL_IMPLEMENT_CODEX_BIN"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_PACKAGE = 3
EXIT_BLOCK = 4
EXIT_APPLY = 5
EXIT_PREFLIGHT = 6
EXIT_OVERRIDE = 7

PREFLIGHT_COMMAND = ("bin", "local-implement")
PREFLIGHT_ARGS = ("preflight",)
DEFAULT_PREFLIGHT_TIMEOUT = 60.0
RECEIPT_STRING_FIELDS = ("endpoint", "canonical_model", "api_model")
AGENTS_OVERRIDE_FILENAME = "AGENTS.override.md"

SECRET_KEY_TOKENS = ("key", "token", "secret", "password", "passwd", "credential",
                     "authorization", "authorisation", "cookie")
SECRET_TEXT = re.compile(r"(?i)([\"']?\b(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|"
                         r"id[_ -]?token|auth(?:orization|isation)?[_ -]?token|"
                         r"client[_ -]?secret|secret[_ -]?key|password|passwd|credential|token|"
                         r"secret|cookie)\b[\"']?(\s*[:=]\s*))([\"']?)([^\"'\\\s,};]+)")
BEARER_TEXT = re.compile(r"(?i)\b(bearer\s+)([\"']?)([A-Za-z0-9._~+/-]{8,}[A-Za-z0-9])")


def redact(text):
    """Redact secret-shaped values in free text; always run this before any truncation."""
    text = SECRET_TEXT.sub(lambda match: match.group(1) + match.group(2) + match.group(3)
                           + "<redacted>", text)
    return BEARER_TEXT.sub(lambda match: match.group(1) + match.group(2) + "<redacted>", text)


class SetupError(Exception):
    """A refusal that must be reported as structured JSON with a stable exit code."""

    def __init__(self, code, stage, detail):
        Exception.__init__(self, "%s: %s" % (stage, detail))
        self.code = code
        self.stage = stage
        self.detail = detail


def absolute(*parts):
    """Join path parts without resolving symlinks, so `/var` stays whatever the caller passed."""
    return os.path.normpath(os.path.join(*[str(part) for part in parts]))


def inside_root(path, root):
    """True when `path` stays under `root`, compared without following symlinks."""
    candidate, base = str(path), str(root)
    return candidate == base or candidate.startswith(base.rstrip(os.sep) + os.sep)


def package_root():
    """The plugin root, inferred from this file, validated before anything else reads it."""
    root = Path(absolute(Path(__file__).resolve().parents[1]))
    if not (root / "plugin.json").is_file():
        raise SetupError(EXIT_PACKAGE, "package", "no plugin.json above " + str(root))
    return root


def read_bytes(path):
    with open(str(path), "rb") as handle:
        return handle.read()


def decode(raw):
    return raw.decode("utf-8", "surrogateescape")


def encode(text):
    return text.encode("utf-8", "surrogateescape")


def sha256_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path):
    return sha256_bytes(read_bytes(path))


def presence(path):
    entry = {"path": str(path)}
    if os.path.islink(str(path)):
        entry["kind"] = "symlink"
    elif os.path.isfile(str(path)):
        entry["kind"] = "file"
    elif os.path.isdir(str(path)):
        entry["kind"] = "directory"
    else:
        entry["kind"] = "missing"
    entry["exists"] = entry["kind"] in ("file", "directory")
    return entry


def write_atomic(path, raw, mode=None):
    """Write bytes in place atomically: same-directory temp file, fsync, then os.replace."""
    parent = os.path.dirname(str(path)) or "."
    descriptor, temporary = tempfile.mkstemp(prefix="." + os.path.basename(str(path)) + ".",
                                             dir=parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is None:
            mode = 0o644
        os.chmod(temporary, mode)
        os.replace(temporary, str(path))
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


# --------------------------------------------------------------------------- package


def link_destination(root, relative):
    return absolute(str(root), *relative)


def managed_block(root):
    """The only global text this plugin owns: a small pointer at the installed policy."""
    policy = link_destination(root, POLICY_RELATIVE)
    skill = link_destination(root, SKILL_RELATIVE)
    setup = link_destination(root, SETUP_RELATIVE)
    lines = [
        MARKER_BEGIN,
        "## Local Qwen: selected planner, local Qwen implementation",
        "",
        "Planning stays on the model selected in the Codex window. Implementation, fixes, refactors",
        "and test authoring run on the pinned local Qwen worker through the plugin launcher. This",
        "block is the only global text the plugin owns; the authoritative policy stays in the package.",
        "",
        "- Canonical policy: [global-policy.md](<%s>)" % policy,
        "- Bundled launcher skill: [local-implement/SKILL.md](<%s>)" % skill,
        "- Install root: <%s>" % str(root),
        "- Read the canonical policy before implementing. If that file is missing, the plugin was",
        "  moved or uninstalled: run `python3 %s --check` from the current install root and"
        % shlex.quote(setup),
        "  report the blocker instead of routing implementation to a cloud model.",
        MARKER_END,
    ]
    return "\n".join(lines) + "\n"


def validate_package(root):
    """Manifest identity, the onboarding skill, the canonical policy, the launcher and its engine."""
    manifest_path = root / "plugin.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise SetupError(EXIT_PACKAGE, "manifest", "plugin.json is not a regular file")
    try:
        manifest = json.loads(decode(read_bytes(manifest_path)))
    except ValueError as error:
        raise SetupError(EXIT_PACKAGE, "manifest", "plugin.json is not valid JSON: %s" % error)
    if not isinstance(manifest, dict):
        raise SetupError(EXIT_PACKAGE, "manifest", "plugin.json is not a JSON object")
    if manifest.get("name") != PLUGIN_NAME:
        raise SetupError(EXIT_PACKAGE, "manifest",
                         "plugin.json name is %r, expected %r" % (manifest.get("name"), PLUGIN_NAME))
    extensions = manifest.get("extensions")
    client = extensions.get("com.openai") if isinstance(extensions, dict) else None
    onboarding = client.get("onboardingSkill") if isinstance(client, dict) else None
    if not isinstance(onboarding, str) or not onboarding:
        raise SetupError(EXIT_PACKAGE, "manifest",
                         "extensions.com.openai.onboardingSkill is missing from plugin.json")
    if os.path.isabs(onboarding):
        raise SetupError(EXIT_PACKAGE, "manifest", "onboardingSkill must be package-relative")
    onboarding_path = Path(absolute(str(root), *onboarding.split("/")))
    if not inside_root(onboarding_path, root):
        raise SetupError(EXIT_PACKAGE, "manifest",
                         "onboardingSkill resolves outside the package: " + onboarding)
    if not onboarding_path.is_file():
        raise SetupError(EXIT_PACKAGE, "manifest",
                         "onboarding skill is missing: " + str(onboarding_path))

    files = {"canonical_policy": Path(link_destination(root, POLICY_RELATIVE)),
             "launcher_skill": Path(link_destination(root, SKILL_RELATIVE)),
             "setup_script": Path(link_destination(root, SETUP_RELATIVE)),
             "launcher": Path(link_destination(root, PREFLIGHT_COMMAND)),
             "launcher_engine": Path(link_destination(root, LAUNCHER_ENGINE_RELATIVE))}
    for name in SKILLS:
        files["skill_" + name] = root / "skills" / name / "SKILL.md"
    for name, path in sorted(files.items()):
        if not path.is_file() or path.is_symlink():
            raise SetupError(EXIT_PACKAGE, "package",
                             "%s is not a regular file: %s" % (name, path))
    policy_raw = read_bytes(files["canonical_policy"])
    if not policy_raw.strip():
        raise SetupError(EXIT_PACKAGE, "policy",
                         "canonical policy is empty: " + str(files["canonical_policy"]))
    try:
        policy_text = policy_raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SetupError(EXIT_PACKAGE, "policy", "canonical policy is not UTF-8: %s" % error)
    if PLUGIN_NAME not in policy_text:
        raise SetupError(EXIT_PACKAGE, "policy",
                         "canonical policy does not name " + PLUGIN_NAME)
    return {"manifest": manifest, "onboarding_skill": str(onboarding_path),
            "files": {name: str(path) for name, path in files.items()}}


# --------------------------------------------------------------------- managed block


def analyze_block(text, expected):
    """Classify the managed block: absent, current, drift or malformed (never guess)."""
    begins, ends = [], []
    position = 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped == MARKER_BEGIN:
            begins.append(position)
        elif stripped == MARKER_END:
            ends.append(position)
        position += len(line)
    stray = (text.count(MARKER_BEGIN) - len(begins)) + (text.count(MARKER_END) - len(ends))
    result = {"marker_begin": MARKER_BEGIN, "marker_end": MARKER_END,
              "begin_lines": len(begins), "end_lines": len(ends), "stray_markers": stray}
    if not begins and not ends and not stray:
        result.update({"state": "absent", "block_count": 0, "install_root": None, "block": None,
                       "reason": "no managed block in this file"})
        return result
    if len(begins) != 1 or len(ends) != 1 or begins[0] >= ends[0] or stray:
        result.update({"state": "malformed", "block_count": max(len(begins), len(ends)),
                       "install_root": None, "block": None,
                       "reason": "managed markers must appear exactly once, balanced, each on its "
                                 "own line; nothing is modified until they are repaired"})
        return result
    start = begins[0]
    newline = text.find("\n", ends[0])
    end = len(text) if newline == -1 else newline + 1
    block = text[start:end]
    match = re.search(r"\]\(<([^<>]*)/skills/local-implement/references/global-policy\.md>\)", block)
    result.update({"block_count": 1, "install_root": match.group(1) if match else None,
                   "block": block})
    if block == expected:
        result.update({"state": "current", "reason": "managed block already points at this root"})
    else:
        result.update({"state": "drift",
                       "reason": "managed block differs from the block for this install root"})
    result["span"] = (start, end)
    return result


# ----------------------------------------------------------------------- environment


def codex_home():
    return Path(absolute(os.environ.get(CODEX_HOME_ENV)
                         or os.path.join(os.path.expanduser("~"), ".codex")))


def state_dir():
    return Path(absolute(os.environ.get(STATE_DIR_ENV)
                         or os.path.join(os.path.expanduser("~"), ".local-implement")))


def inspect_override(home):
    """Report a read-only `$CODEX_HOME/AGENTS.override.md` check; contents are never disclosed.

    Codex discovery reads a nonempty `AGENTS.override.md` before `AGENTS.md`, so the managed block
    is not an active default while such a file exists. An empty or whitespace-only override is
    ignored by that discovery and stays compatible; the file is read only to decide whether it is
    empty, and an override that cannot be inspected is a concrete error never reported as absent.
    """
    path = Path(absolute(str(home), AGENTS_OVERRIDE_FILENAME))
    report = {"path": str(path), "exists": False, "state": "absent", "active": False,
              "supersedes_managed_block": False, "checked_read_only": True,
              "reason": "no %s beside AGENTS.md, so Codex reads AGENTS.md"
                        % AGENTS_OVERRIDE_FILENAME}
    try:
        os.lstat(str(path))
    except FileNotFoundError:
        return report
    except OSError as error:
        raise SetupError(EXIT_OVERRIDE, "agents_override",
                         "cannot inspect %s: %s" % (path, error))
    report["exists"] = True
    if path.is_symlink():
        raise SetupError(EXIT_OVERRIDE, "agents_override",
                         "%s is a symlink; refusing to read or follow it: %s"
                         % (AGENTS_OVERRIDE_FILENAME, path))
    if not path.is_file():
        raise SetupError(EXIT_OVERRIDE, "agents_override",
                         "%s is not a regular file, so its effect on AGENTS.md cannot be "
                         "determined: %s" % (AGENTS_OVERRIDE_FILENAME, path))
    try:
        text = decode(read_bytes(path))
    except OSError as error:
        raise SetupError(EXIT_OVERRIDE, "agents_override", "cannot read %s: %s" % (path, error))
    if text.strip():
        report.update({"state": "active", "active": True, "supersedes_managed_block": True,
                       "reason": "%s is nonempty, so Codex reads it instead of AGENTS.md and the "
                                 "managed block is not an active default until that override is "
                                 "resolved (its contents are never printed or disclosed)"
                                 % AGENTS_OVERRIDE_FILENAME})
    else:
        report.update({"state": "empty",
                       "reason": "%s exists but is empty or whitespace-only, which Codex "
                                 "discovery ignores, so AGENTS.md stays authoritative"
                                 % AGENTS_OVERRIDE_FILENAME})
    return report


def command_availability(name, env_var=None):
    """Report how a CLI was found (or why it was not) without executing anything."""
    override = (os.environ.get(env_var) or "").strip() if env_var else ""
    candidate = None
    source = "missing"
    if override:
        source = env_var
        candidate = override if os.path.isabs(override) else shutil.which(override)
        if candidate is None:
            candidate = override
    else:
        found = shutil.which(name)
        if found:
            candidate, source = found, "PATH"
    return {"available": bool(candidate) and os.path.isfile(str(candidate))
            and os.access(str(candidate), os.X_OK), "path": candidate, "source": source}


def environment_report():
    return {
        "python": {"executable": sys.executable or None,
                   "version": ".".join(str(part) for part in sys.version_info[:3])},
        "git": command_availability("git"),
        "codex_cli": command_availability("codex", "LOCAL_IMPLEMENT_CODEX_BIN"),
        "omlx": omlx_report(),
        "native": {"optional": True,
                   "required_for": "native Apple jobs only, never for installation",
                   "xcrun": command_availability("xcrun")},
    }


def omlx_report():
    """Presence only: settings and model files are never opened, so no credential can escape."""
    base = Path(os.path.join(os.path.expanduser("~"), ".omlx"))
    return {"directory": str(base), "contents_read": False,
            "settings": presence(base / "settings.json"),
            "model_settings": presence(base / "model_settings.json"),
            "note": "presence is reported; file contents and credentials are never read"}


# ------------------------------------------------------------------------- preflight


def sanitize(value, depth=0):
    """Project the launcher report into something safe to print: no secret keys, bounded size."""
    if depth > 6:
        return "<nested>"
    if isinstance(value, dict):
        safe = {}
        for key, nested in sorted(value.items()):
            name = str(key).lower()
            if any(token in name for token in SECRET_KEY_TOKENS):
                safe[str(key)] = "<withheld>"
                continue
            safe[str(key)] = sanitize(nested, depth + 1)
        return safe
    if isinstance(value, list):
        kept = [sanitize(item, depth + 1) for item in value[:20]]
        if len(value) > 20:
            kept.append("<%d more>" % (len(value) - 20))
        return kept
    if isinstance(value, str):
        text = redact(value)
        return text if len(text) <= 200 else text[:200] + "<truncated>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def receipt_issue(parsed):
    """Why delegated output is not a launcher success receipt; `None` when it is one.

    This checks only the shape of the receipt the launcher emits on success (endpoint, canonical
    and API model, fallback disabled, model actually served). Routing policy stays in the launcher;
    without this check any parsed JSON - `[]`, or a blocked report - would become an endpoint claim.
    Launcher-supplied values are never echoed into the reason, so nothing secret can ride along.
    """
    if not isinstance(parsed, dict):
        return "output is %s, not the JSON object of a launcher receipt" % type(parsed).__name__
    for field in RECEIPT_STRING_FIELDS:
        value = parsed.get(field)
        if not isinstance(value, str) or not value.strip():
            return "%s is missing or not a nonempty string" % field
    if parsed.get("model_fallback") is not False:
        return "model_fallback is not exactly false, so the served model is not proven canonical"
    served = parsed.get("served_models")
    if not isinstance(served, list):
        return "served_models is missing or not a list"
    if parsed.get("api_model") not in served:
        return "served_models does not contain the receipt's api_model"
    return None


def run_preflight(root, timeout):
    """Delegate the endpoint claim to the packaged launcher, bounded, and prove it succeeded."""
    launcher = Path(link_destination(root, PREFLIGHT_COMMAND))
    command = [str(launcher)] + list(PREFLIGHT_ARGS)
    result = {"requested": True, "command": command, "cwd": str(root),
              "timeout_seconds": timeout, "status": "failed", "endpoint_passed": False,
              "reason": None, "returncode": None, "stdout_parsed": False, "detail": None}
    if not launcher.is_file():
        result["reason"] = "packaged launcher is missing: " + str(launcher)
        return result
    try:
        completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   universal_newlines=True, timeout=timeout, cwd=str(root))
    except subprocess.TimeoutExpired:
        result["reason"] = "preflight timed out after %s seconds; no endpoint claim is made" % timeout
        return result
    except OSError as error:
        result["reason"] = "preflight could not be started: %s" % error
        return result
    result["returncode"] = completed.returncode
    try:
        parsed = json.loads(completed.stdout)
        parsed_ok = True
    except ValueError:
        parsed, parsed_ok = None, False
    result["stdout_parsed"] = parsed_ok
    if parsed_ok:
        result["detail"] = sanitize(parsed)
        stdout_tail = None  # the sanitized projection in `detail` is the only copy kept
    else:
        stdout_tail = redact(completed.stdout.strip())[-400:] if completed.stdout.strip() else None
    stderr_tail = redact(completed.stderr.strip())[-400:] if completed.stderr.strip() else None
    if completed.returncode != 0:
        result["reason"] = "packaged launcher preflight exited %s" % completed.returncode
    elif not parsed_ok:
        result["reason"] = "packaged launcher preflight exited 0 without JSON output"
    else:
        issue = receipt_issue(parsed)
        if issue is None:
            result["status"] = "passed"
            result["endpoint_passed"] = True
            result["reason"] = ("delegated to the packaged launcher; it returned a complete success "
                                "receipt with fallback disabled and the API model actually served")
        else:
            result["reason"] = ("packaged launcher preflight exited 0 without a valid success "
                                "receipt: " + issue)
    result["stdout_tail"] = stdout_tail
    result["stderr_tail"] = stderr_tail
    return result


# ----------------------------------------------------------------------------- apply


def apply_block(root, analysis, agents_path, home, state):
    """Install or refresh the managed block; back up first, write atomically, report a receipt."""
    if analysis["state"] == "malformed":
        raise SetupError(EXIT_BLOCK, "managed_block", analysis["reason"])
    if agents_path.is_symlink():
        raise SetupError(EXIT_APPLY, "agents_file",
                         "refusing to follow a symlinked AGENTS.md: " + str(agents_path))
    expected = managed_block(root)
    current_raw = None
    text = ""
    if agents_path.is_file():
        current_raw = read_bytes(agents_path)
        text = decode(current_raw)

    if analysis["state"] == "current":
        new_text, reason = text, "noop"
    elif analysis["block_count"] == 0:
        if legacy_matches(current_raw):
            heading = text.split("\n", 1)[0].rstrip() if text.startswith("#") else LEGACY_HEADER
            new_text, reason = heading + "\n\n" + expected, "migrated_legacy"
        else:
            separator = ""
            if text:
                separator = "\n" if text.endswith("\n") else "\n\n"
            new_text, reason = text + separator + expected, "installed"
    else:
        start, end = analysis["span"]
        new_text, reason = text[:start] + expected + text[end:], "updated"

    new_raw = encode(new_text)
    receipt = {"requested": True, "destination": str(agents_path), "backup": None,
               "action": reason, "managed_block": "managed by the plugin",
               "before_sha256": sha256_bytes(current_raw) if current_raw is not None else None,
               "after_sha256": sha256_bytes(new_raw),
               "unrelated_text_preserved": "outside the managed markers, byte-for-byte"}
    if current_raw is not None and new_raw == current_raw:
        receipt.update({"changed": False, "reason": "managed block already current; nothing written",
                        "written_paths": []})
        return receipt

    try:
        os.makedirs(str(home), exist_ok=True)
    except OSError as error:
        raise SetupError(EXIT_APPLY, "codex_home", "cannot create %s: %s" % (home, error))
    if current_raw is not None:
        backups = state / BACKUP_DIRNAME
        try:
            os.makedirs(str(backups), exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup = backups / ("AGENTS-%s-%s.md" % (stamp, uuid.uuid4()))
            write_atomic(backup, current_raw, mode=0o400)
        except OSError as error:
            raise SetupError(EXIT_APPLY, "backup", "cannot back up %s: %s" % (agents_path, error))
        receipt["backup"] = str(backup)
    written = [str(agents_path)]
    try:
        write_atomic(agents_path, new_raw, mode=_mode(agents_path))
    except OSError as error:
        raise SetupError(EXIT_APPLY, "agents_file", "cannot write %s: %s" % (agents_path, error))
    receipt.update({"changed": True, "reason": reason, "written_paths": written})
    return receipt


def _mode(path):
    try:
        return os.stat(str(path)).st_mode & 0o777
    except OSError:
        return 0o644


def legacy_matches(raw):
    """True only for a byte-exact copy of the historical global policy in this package."""
    if raw is None:
        return False
    return sha256_bytes(raw.strip() + b"\n") == LEGACY_SHA256


def legacy_fixture(root):
    """The historical migration input, if the package still ships it (never modified)."""
    path = Path(link_destination(root, LEGACY_RELATIVE))
    if not path.is_file():
        return {"path": str(path), "shipped": False, "matches_embedded_digest": None}
    digest = sha256_file(path)
    return {"path": str(path), "shipped": True, "sha256": digest,
            "matches_embedded_digest": sha256_bytes(read_bytes(path).strip() + b"\n")
            == LEGACY_SHA256}


# -------------------------------------------------------------------------- report


def build_parser():
    parser = argparse.ArgumentParser(prog="plugin_setup.py",
                                     description="Inspect or install the local-qwen plugin's "
                                                 "user-wide routing reference.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true",
                      help="read-only report (the default; writes nothing)")
    mode.add_argument("--apply", action="store_true",
                      help="authorized install of the small managed block in $CODEX_HOME/AGENTS.md")
    parser.add_argument("--preflight", action="store_true",
                        help="also run the packaged launcher's model-endpoint preflight")
    parser.add_argument("--preflight-timeout", type=float, default=DEFAULT_PREFLIGHT_TIMEOUT,
                        help="seconds allowed for the delegated preflight (default 60)")
    return parser


def run(args):
    root = package_root()
    report = {"schema_version": SCHEMA_VERSION, "tool": "plugin_setup",
              "mode": "apply" if args.apply else "check",
              "plugin": {"name": PLUGIN_NAME, "root": str(root)},
              "codex_home": str(codex_home()), "state_dir": str(state_dir()),
              "writes_performed": False}
    package = validate_package(root)
    manifest = package["manifest"]
    report["plugin"]["version"] = manifest.get("version")
    report["plugin"]["manifest"] = str(root / "plugin.json")
    report["plugin"]["onboarding_skill"] = package["onboarding_skill"]
    report["plugin"]["description"] = manifest.get("description")
    report["package"] = {name: presence(Path(path))
                         for name, path in sorted(package["files"].items())}
    report["package"]["onboarding_skill"] = presence(Path(package["onboarding_skill"]))
    report["legacy"] = legacy_fixture(root)

    home, state = codex_home(), state_dir()
    agents = home / "AGENTS.md"
    override = inspect_override(home)
    report["agents_override"] = override
    expected = managed_block(root)
    analysis = {"state": "absent", "block_count": 0, "install_root": None, "stray_markers": 0,
                "begin_lines": 0, "end_lines": 0, "reason": "AGENTS.md does not exist",
                "block": None}
    if agents.is_symlink():
        analysis = {"state": "malformed", "block_count": 0, "install_root": None,
                    "reason": "AGENTS.md is a symlink; refusing to read or follow it",
                    "begin_lines": 0, "end_lines": 0, "stray_markers": 0, "block": None}
    elif agents.is_file():
        analysis = analyze_block(decode(read_bytes(agents)), expected)
        if legacy_matches(read_bytes(agents)) and analysis["state"] == "absent":
            analysis["legacy_exact"] = True
    analysis["agents_file"] = str(agents)
    analysis["file_exists"] = agents.is_file()
    analysis["managed_root_matches_this_root"] = analysis.get("install_root") == str(root)
    report["managed_block"] = {key: value for key, value in analysis.items()
                               if key not in ("block", "span")}

    report["environment"] = environment_report()
    report["preflight"] = {"requested": False, "status": "not-requested", "endpoint_passed": False,
                           "reason": "pass --preflight to ask the packaged launcher; no "
                                     "endpoint claim is made without it"}

    exit_code = EXIT_OK
    if analysis["state"] == "malformed":
        exit_code = EXIT_BLOCK
    if override["active"] and exit_code == EXIT_OK:
        exit_code = EXIT_OVERRIDE

    if args.apply:
        if override["active"]:
            raise SetupError(EXIT_OVERRIDE, "agents_override",
                             override["reason"] + "; --apply refuses before any backup or write to "
                                                  + str(agents))
        receipt = apply_block(root, analysis, agents, home, state)
        report["writes_performed"] = bool(receipt["changed"])
        receipt["installed"] = {
            "canonical_policy": {"path": package["files"]["canonical_policy"],
                                 "sha256": sha256_file(Path(package["files"]["canonical_policy"]))},
            "launcher_skill": {"path": package["files"]["launcher_skill"],
                               "sha256": sha256_file(Path(package["files"]["launcher_skill"]))},
            "setup_script": {"path": package["files"]["setup_script"],
                             "sha256": sha256_file(Path(package["files"]["setup_script"]))},
            "plugin_manifest": {"path": report["plugin"]["manifest"],
                                "sha256": sha256_file(Path(report["plugin"]["manifest"]))},
            "onboarding_skill": {"path": package["onboarding_skill"],
                                 "sha256": sha256_file(Path(package["onboarding_skill"]))},
        }
        report["apply"] = receipt
        if receipt["changed"] and analysis["state"] != "malformed":
            after = analyze_block(decode(read_bytes(agents)), expected)
            report["apply"]["verified_state"] = after["state"]
            if after["state"] != "current":
                raise SetupError(EXIT_APPLY, "verify",
                                 "managed block did not become current after apply")

    if args.preflight:
        timeout = max(1.0, float(args.preflight_timeout))
        report["preflight"] = run_preflight(root, timeout)
        if report["preflight"]["status"] != "passed":
            exit_code = EXIT_PREFLIGHT if exit_code == EXIT_OK else exit_code
    report["exit_code"] = exit_code
    return report, exit_code


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        report, exit_code = run(args)
    except SetupError as error:
        report = {"schema_version": SCHEMA_VERSION, "tool": "plugin_setup",
                  "mode": "apply" if args.apply else "check",
                  "ok": False,
                  "error": {"stage": error.stage, "detail": error.detail,
                            "exit_code": error.code},
                  "exit_code": error.code}
        print(json.dumps(report, indent=2, sort_keys=True))
        return error.code
    report["ok"] = exit_code == EXIT_OK
    print(json.dumps(report, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
