"""Shared helpers for the suite: real disposable git repos, mocked worker subprocess."""
import copy
import contextlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import urllib.parse
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import local_worker as lw  # noqa: E402
import omlx_discovery  # noqa: E402

SECRET = "local-key-super-secret-0123456789"
GIT_ID = {"GIT_AUTHOR_NAME": "Tester", "GIT_AUTHOR_EMAIL": "t@example.invalid",
          "GIT_COMMITTER_NAME": "Tester", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
APPLE_UDID = "1E3A2B4C-5D6E-4F70-8A9B-0C1D2E3F4A5B"
OTHER_UDID = "0F1E2D3C-4B5A-4978-9675-8493A2B1C0DE"
APPLE_RUNTIME = "com.apple.CoreSimulator.SimRuntime.tvOS-18-0"
SHORT_APPLE_RUNTIME = "tvOS-18.0"
IOS_RUNTIME = "com.apple.CoreSimulator.SimRuntime.iOS-18-0"
BASE_FILES = {".gitignore": "__pycache__/\n*.pyc\n",
              "add.py": "def add(a, b):\n    return a - b\n", "notes.md": "base notes\n"}
FIXTURES = Path(__file__).resolve().parent / "fixtures"
# The model ``FakeOmlx("one")`` has loaded, and its window: what discovery reports in tests.
MODEL = "Qwen3.5-9B-OptiQ-4bit"
MODEL_WINDOW = 262144
FIX_ADD = "def add(a, b):\n    return a + b\n"


def load_fixture_text(relative):
    """Raw text of a recorded fixture under ``tests/fixtures`` (``omlx/...`` or ``codex/...``)."""
    return (FIXTURES / relative).read_text(encoding="utf-8")


def load_fixture(relative):
    """A recorded fixture decoded: JSON for ``.json``, a list of parsed lines for ``.jsonl``."""
    text = load_fixture_text(relative)
    if relative.endswith(".jsonl"):
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    return json.loads(text)


def git(repo, *args):
    return lw.git(Path(repo), *args, env=GIT_ID)


def tmpdir():
    return Path(tempfile.mkdtemp())


def tmpdir_under(parent):
    """Disposable directory created *inside* ``parent``, for fixtures that must sit under a
    specific root (the host's per-user Darwin temporary root is a granted writable root, so a
    nested primary checkout has to be built there to be a real nested case)."""
    return Path(tempfile.mkdtemp(dir=str(parent)))


def cleanup(path):
    shutil.rmtree(str(path), ignore_errors=True)


def make_repo(parent, files=None):
    """Disposable repository with one commit, made under a local git identity."""
    repo = Path(parent) / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q", "--initial-branch=main")
    for name, text in (files or BASE_FILES).items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    return repo


def write_settings(path, *, host="127.0.0.1", port=8000, model_fallback=False):
    """oMLX ``settings.json`` at ``path`` (parent directories created); ``port=None`` omits the
    key, as a default install does. The key is ``SECRET``. Returns ``path``."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    server = {"host": host}
    if port is not None:
        server["port"] = port
    path.write_text(json.dumps({"auth": {"api_key": SECRET}, "server": server,
                                "model": {"model_fallback": model_fallback}}), encoding="utf-8")
    return path


class FakeOmlx:
    """A recorded oMLX server with the ``HttpFetch`` signature: ``fake(settings, path, auth)``.

    Serves ``/health``, ``/v1/models/status`` and ``/api/status`` from the fixtures under
    ``tests/fixtures/omlx`` for one of four variants (``one``, ``two_loaded``, ``none_loaded``,
    ``helper_and_embedding``) and the blocking model-load POST. ``/api/status`` always starts
    from ``api_status.json`` with its loaded/discovered/loading counts rebuilt from the variant.
    Like the real server, the protected endpoints need the right bearer key. ``.calls`` records
    every request path.

    ``overrides``: ``{"_health": "loading"}`` serves the 503 loading answer;
    ``{"_api_status": {...}}`` is merged over the ``/api/status`` body; ``{"model_alias": "x"}``
    sets the alias on the loaded chat model(s); any other key replaces that field on them.

    ``after_calls(n, ...)`` swaps what the server reports once ``n`` requests have been served,
    which is a model swapped in oMLX while a job is running.
    """

    VARIANTS = ("one", "two_loaded", "none_loaded", "helper_and_embedding")
    _LOAD = re.compile(r"/v1/models/([^/]+)/load")

    def __init__(self, variant: str = "one", overrides: Optional[Dict[str, Any]] = None):
        if variant not in self.VARIANTS:
            raise ValueError("unknown FakeOmlx variant %r" % variant)
        self.overrides, self.calls, self._swap = dict(overrides or {}), [], None
        self._serve(variant)

    def _serve(self, variant):
        """Report ``variant`` (with the current overrides) from the next request on."""
        self.variant = variant
        self.models_status = self._body("omlx/models_status_%s.json" % variant)[1]
        for model in self.models_status["models"]:
            if model["loaded"] and not model["is_helper"] and model["model_type"] in ("llm", "vlm"):
                for key, value in self.overrides.items():
                    if not key.startswith("_"):
                        model[key] = value

    def after_calls(self, count: int, variant: Optional[str] = None,
                    overrides: Optional[Dict[str, Any]] = None) -> "FakeOmlx":
        """Once ``count`` requests have been served, serve ``variant`` and/or ``overrides`` (each
        defaults to the current one) from request ``count + 1`` on. A discovery makes three
        requests and a recheck one, so ``after_calls(3, ...)`` swaps right after a job's preflight
        and the recheck before attempt 1 is the first to see it. The swap happens once; state the
        old server had (a model loaded through the load POST) does not carry over."""
        if variant is not None and variant not in self.VARIANTS:
            raise ValueError("unknown FakeOmlx variant %r" % variant)
        self._swap = (count, variant, overrides)
        return self

    @staticmethod
    def _body(relative):
        """``(status, body)`` of a fixture: ``_http_status`` is the status code, default 200."""
        body = load_fixture(relative)
        return body.pop("_http_status", 200), body

    def __call__(self, settings, path, auth):
        self.calls.append(path)
        if self._swap is not None and len(self.calls) > self._swap[0]:
            _, variant, overrides = self._swap
            self._swap = None
            if overrides is not None:
                self.overrides = dict(overrides)
            self._serve(variant or self.variant)
        if path == "/health":
            status, health = self._body("omlx/health_%s.json" % (
                "loading" if self.overrides.get("_health") == "loading" else "ok"))
            return status, health
        if path in ("/v1/models/status", "/api/status") or self._LOAD.fullmatch(path):
            if not auth or settings.api_key != SECRET:
                raise omlx_discovery.DiscoveryError("auth_failed", "FakeOmlx: bad or missing key")
            if path == "/v1/models/status":
                return 200, copy.deepcopy(self.models_status)
            if path == "/api/status":
                return 200, self._api_status()
            return self._load(urllib.parse.unquote(self._LOAD.fullmatch(path).group(1)))
        return 404, {"detail": "Not Found"}

    def _api_status(self):
        models = self.models_status["models"]
        body = self._body("omlx/api_status.json")[1]
        body.update({"loaded_models": [m["id"] for m in models if m["loaded"]],
                     "models_loaded": sum(1 for m in models if m["loaded"]),
                     "models_discovered": len(models),
                     "models_loading": sum(1 for m in models if m["is_loading"])})
        body.update(self.overrides.get("_api_status", {}))
        return body

    def _load(self, model_id):
        for model in self.models_status["models"]:
            if model["id"] == model_id:
                model["loaded"], model["is_loading"] = True, False
                return 200, {"status": "ok", "model_id": model_id,
                             "message": "Loaded " + model_id}
        return 404, {"detail": "Model not found: " + model_id}


# The option lines of a real ``codex exec --help`` (Codex CLI 0.164.0), one per flag.
EXEC_HELP_FLAGS = ("-c, --config <key=value>", "--enable <FEATURE>", "--disable <FEATURE>",
                   "-m, --model <MODEL>", "-s, --sandbox <SANDBOX_MODE>", "-C, --cd <DIR>",
                   "--skip-git-repo-check", "--ephemeral", "--ignore-user-config",
                   "--ignore-rules", "--output-schema <FILE>", "--color <COLOR>", "--json",
                   "--strict-config",
                   "-o, --output-last-message <FILE>", "-h, --help", "-V, --version")
CODEX_VERSION = "codex-cli 0.164.0"


def fake_help(version: str = CODEX_VERSION, missing: Sequence[str] = ()):
    """A fake ``subprocess.run`` for the two calls of the Codex compatibility check.

    ``codex --version`` prints ``version``; ``codex exec --help`` lists every option of the real
    0.164 help except the flags named in ``missing``. Anything else exits 2. ``.calls`` records
    every argv it was asked to run.
    """
    def run(argv, **kwargs):
        argv = [str(item) for item in argv]
        run.calls.append(argv)
        if argv[1:] == ["--version"]:
            return subprocess.CompletedProcess(argv, 0, version + "\n", "")
        if argv[1:] == ["exec", "--help"]:
            lines = ["Run Codex non-interactively", "", "Options:"]
            for option in EXEC_HELP_FLAGS:
                names = [name.split(" ")[0] for name in option.split(", ")]
                if not set(names) & set(missing):
                    lines += ["      " + option, "          (help text)", ""]
            return subprocess.CompletedProcess(argv, 0, "\n".join(lines) + "\n", "")
        if argv[1:] == ["debug", "models"]:
            return subprocess.CompletedProcess(argv, 0, load_fixture_text("codex/debug_models.json"), "")
        return subprocess.CompletedProcess(argv, 2, "", "error: unexpected argument\n")

    run.calls = []
    return run


def fake_completed(stdout: str = "", returncode: int = 0):
    """Completed subprocess result for catalog and other bounded command fixtures."""
    return subprocess.CompletedProcess([], returncode, stdout, "")


def task_payload(repo, **overrides):
    payload = {"task_id": "task-1", "repo_path": str(Path(repo)), "base_ref": "main",
               "goal": "Make add() return the sum.", "acceptance_criteria": ["test_add passes"],
               "allowed_paths": ["add.py"], "test_commands": [["python3", "-m", "unittest", "-v"]],
               "timeout_seconds": 60, "max_attempts": 1}
    if "tier" in overrides:
        payload.update(kind="write", workstream=payload["task_id"],
                       environment={"pass": []}, ui_review=False)
    payload.update(overrides)
    return payload


def simctl_json(devices=None, runtime=APPLE_RUNTIME):
    """`xcrun simctl list devices available --json` payload with the devices asked for."""
    if devices is None:
        devices = [{"udid": APPLE_UDID, "name": "Apple TV 4K 3", "isAvailable": True}]
    return json.dumps({"devices": {runtime: devices}, "runtimes": [], "bundles": []})


def simctl_devices(runtimes: Dict[str, List[Dict]]) -> Dict[str, Dict]:
    """Flatten simctl's runtime-keyed devices while retaining each runtime identifier."""
    return {device["udid"]: dict(device, runtime=runtime)
            for runtime, devices in runtimes.items() for device in devices
            if isinstance(device, dict) and isinstance(device.get("udid"), str)}


def write_png(path, width, height, rgb, dot=None):
    """Write a small valid, non-interlaced 8-bit RGB PNG for screenshot tests."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pixels = [list(rgb) * width for _ in range(height)]
    if dot is not None and width and height:
        x, y = width // 2, height // 2
        pixels[y][x * 3:x * 3 + 3] = list(dot)

    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data +
                struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff))

    scanlines = b"".join(b"\x00" + bytes(row) for row in pixels)
    data = (b"\x89PNG\r\n\x1a\n" +
            chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(scanlines)) + chunk(b"IEND", b""))
    path.write_bytes(data)
    return path


def apple_home(parent, udid=APPLE_UDID, name="home"):
    """Fake HOME holding exactly one CoreSimulator device directory."""
    home = Path(parent) / name
    (home / "Library/Developer/CoreSimulator/Devices" / udid).mkdir(parents=True, exist_ok=True)
    return home


def fix_add(cwd, attempt=1):
    """A worker edit that makes the base repository's ``add()`` correct."""
    Path(cwd, "add.py").write_text(FIX_ADD, encoding="utf-8")


def discovered_profile(variant="one", overrides=None, requested=None):
    """The ``ServerProfile`` discovery builds from a ``FakeOmlx`` variant (a throwaway
    ``settings.json`` is written and removed)."""
    root = tmpdir()
    try:
        settings = write_settings(root / "settings.json")
        return omlx_discovery.discover(settings, requested, fetch=FakeOmlx(variant, overrides))
    finally:
        cleanup(root)


def job_options(root, repo, verify=True, omlx=None, max_attempts=1, apple_udid=None,
                **contract_overrides):
    """``JobOptions`` for a disposable job: settings under ``root/omlx``, discovery served by
    ``omlx`` (default ``FakeOmlx("one")``), contract fields overridable by keyword."""
    settings = write_settings(Path(root) / "omlx" / "settings.json")
    task = lw.Task.from_payload(task_payload(repo, **dict({"max_attempts": max_attempts},
                                                          **contract_overrides)))
    return lw.JobOptions(task=task, state_dir=Path(root) / "state", codex_bin="codex-mock",
                         settings_path=settings,
                         omlx_fetch=omlx if omlx is not None else FakeOmlx("one"),
                         verify=verify, apple_simulator_udid=apple_udid,
                         codex_run=fake_help())


def run_clean_job(root, repo, *, omlx=None, fake_run=None, worker_edit=None,
                  escalates=None, **contract_overrides) -> Tuple[Dict, "FakeRun"]:
    """One job end to end on the mocked worker; returns ``(report, fake_run)``.

    The worker makes ``add()`` correct (``fix_add``) unless ``worker_edit`` says otherwise; a
    ``fake_run`` of the caller's is used as given (``worker_edit`` is still applied when passed).
    ``omlx`` is the discovery fake and ``contract_overrides`` replace task contract fields.
    """
    options = job_options(root, repo, omlx=omlx, **contract_overrides)
    options.escalates = escalates
    fake = fake_run if fake_run is not None else FakeRun(worker_edit=worker_edit or fix_add)
    if fake_run is not None and worker_edit is not None:
        fake.worker_edit = worker_edit
    auth_context = contextlib.nullcontext()
    if options.task.tier.kind == "cloud":
        source_home = Path(root) / "synthetic-codex-home"
        source_home.mkdir(parents=True, exist_ok=True)
        (source_home / "auth.json").write_text(json.dumps({
            "auth_mode": "chatgpt",
            "tokens": {"access_token": "synthetic-access-token-1234",
                       "refresh_token": "synthetic-refresh-token-1234",
                       "id_token": "synthetic.id.token", "account_id": "fixture-account"}}),
            encoding="utf-8")
        auth_context = mock.patch.dict(os.environ, {"CODEX_HOME": str(source_home)})
    with auth_context, mock.patch.object(lw, "run_bounded", fake):
        report = lw.run_job(options)
    return report, fake


def result(argv, rc, out="", err="", timed_out=False):
    return {"argv": list(argv), "returncode": rc, "timed_out": timed_out, "elapsed_seconds": 0.01,
            "stdout": out.encode(), "stderr": err.encode(),
            "termination": {"terminated": timed_out, "force_killed": False, "survivors": False}}


def _unescape(raw):
    """Decode a Seatbelt string literal body: \\\" and \\\\ are the only escapes used."""
    out, index = [], 0
    while index < len(raw):
        if raw[index] == "\\" and index + 1 < len(raw):
            out.append(raw[index + 1])
            index += 2
        else:
            out.append(raw[index])
            index += 1
    return "".join(out)


def write_statements(text):
    """Every ``(allow|deny file-write* ...)`` statement in profile order, entries included.

    Each entry is a ``(kind, path)`` pair where kind is ``subpath`` (a directory root) or
    ``literal`` (one exact path, the form used by the single device-node exception).
    """
    statements = []
    for match in re.finditer(r"^\((allow|deny) file-write\* (.*)\)$", text, re.M):
        statements.append((match.group(1), [
            (kind, _unescape(path)) for kind, path in
            re.findall(r'\((subpath|literal) "((?:[^"\\]|\\.)*)"\)', match.group(2))]))
    return statements


def literal_paths(text, action="allow"):
    """The literal paths of one action, so a test can pin the device exception exactly."""
    return [path for statement, entries in write_statements(text) if statement == action
            for kind, path in entries if kind == "literal"]


def seatbelt_write_allowed(text, target):
    """Honour the generated profile the way Seatbelt does: reads stay allowed, the *last*
    matching ``file-write*`` entry decides, ``subpath`` covers its subtree and ``literal``
    covers exactly one path. With no match, ``deny default`` means denied."""
    path = os.path.realpath(str(target))

    def covered(root):
        resolved = os.path.realpath(root)
        return path == resolved or path.startswith(resolved.rstrip("/") + "/")

    verdict = False
    for action, entries in write_statements(text):
        for kind, entry in entries:
            if covered(entry) if kind == "subpath" else path == entry:
                verdict = action == "allow"
    return verdict


def worker_model(argv):
    """The ``-m`` model of a worker argv, which is what a correctly routed worker reports."""
    argv = [str(item) for item in argv]
    return argv[argv.index("-m") + 1] if "-m" in argv else MODEL


# The job's deny wrapper: ``sandbox-exec -f <report_dir>/permissions.sb <command...>``, probed
# once per job with this trivial command before anything model-influenced runs under it.
DENY_PROFILE_NAME = "permissions.sb"
WRAPPER_PROBE = ["/usr/bin/true"]


def deny_wrapped(argv):
    """``(inner argv, True)`` when ``argv`` is a command run under the job's deny profile, else
    ``(argv, False)``."""
    if (len(argv) > 3 and argv[0] == lw.permissions.SANDBOX_EXEC and argv[1] == "-f"
            and Path(argv[2]).name == DENY_PROFILE_NAME):
        return argv[3:], True
    return argv, False


class FakeRun:
    """Stand-in for run_bounded: the worker and the sandbox are mocked, every git call is real.

    A command wrapped in the job's deny profile is recorded and answered as its inner command:
    each call keeps ``argv`` (the inner command, so ``calls_of`` works whether or not the wrapper
    was applied), ``raw_argv`` (exactly what the launcher ran) and ``wrapped``. The wrapper probe
    answers ``wrapper_rc`` (71 is ``sandbox-exec`` failing to apply the profile).
    """

    def __init__(self, worker_rc=0, test_rc=0, probe_ok=True, worker_edit=None,
                 worker_timed_out=False, worker_stderr="", apple=None, routed_model=None,
                 wrapper_rc=0, wrapper_stderr="", first_stream=None, attempt_streams=None,
                 auth_refresh=None):
        self.worker_rc, self.test_rc, self.probe_ok = worker_rc, test_rc, probe_ok
        # The model the worker's events name: the job's own ``-m`` model unless a test routes
        # the worker somewhere else on purpose.
        self.routed_model = routed_model
        self.worker_edit, self.worker_timed_out, self.worker_stderr = (worker_edit,
            worker_timed_out, worker_stderr)
        self.first_stream = first_stream
        self.attempt_streams = list(attempt_streams or [])
        self.auth_refresh = auth_refresh
        self.wrapper_rc, self.wrapper_stderr = wrapper_rc, wrapper_stderr
        self.calls, self.exec_calls = [], 0

    def calls_of(self, kind):
        return [call for call in self.calls if call["argv"][1:2] == [kind]]

    def verification_calls(self):
        """Every command the launcher ran that is neither the worker nor a readiness probe: the
        contract's verification commands (and native steps, which take the same path here)."""
        return [call for call in self.calls if call.get("kind") == "verification"]


    def worker_jsonl(self, argv, cwd, attempt):
        """The worker's JSONL stdout for one attempt (a subclass can add events)."""
        if attempt <= len(self.attempt_streams):
            return self.attempt_streams[attempt - 1]
        if attempt == 1 and self.first_stream is not None:
            return self.first_stream
        return json.dumps({"type": "turn.completed",
                           "model": self.routed_model or worker_model(argv),
                           "usage": {"input_tokens": 11, "output_tokens": 7}}) + "\n"

    def verification_output(self, argv, cwd):
        """What a verification command prints (a subclass can change it)."""
        return "Ran 1 test OK\n"

    def __call__(self, argv, *, cwd, env, timeout, stdin_data=None, stall_seconds=None,
                 on_stall=None):
        raw = [str(item) for item in argv]
        argv, wrapped = deny_wrapped(raw)
        call = {"argv": argv, "raw_argv": raw, "wrapped": wrapped,
                "cwd": Path(cwd) if cwd else None, "env": dict(env), "timeout": timeout,
                "stdin": (stdin_data or b"").decode("utf-8", "replace"),
                "stall_seconds": stall_seconds}
        self.calls.append(call)
        if wrapped and argv == WRAPPER_PROBE:
            call["kind"] = "wrapper_probe"
            return result(raw, self.wrapper_rc, "", self.wrapper_stderr)
        if argv == list(lw.FULL_HOST_PROBE_ARGV):
            # The default readiness probe is one trivial command on the host: answer it the way
            # the host would, so ``probe_ok`` still decides whether execution is available.
            call["kind"] = "probe"
            return result(argv, 0 if self.probe_ok else 1,
                          lw.PROBE_MARKER if self.probe_ok else "full host refused")
        if argv[1:2] == ["sandbox"] and lw.PROBE_MARKER in argv:
            call["kind"] = "probe"
            return result(argv, 0 if self.probe_ok else 1,
                          lw.PROBE_MARKER if self.probe_ok else "sandbox refused")
        if argv[1:2] == ["exec"]:
            call["kind"] = "exec"
            self.exec_calls += 1
            if self.auth_refresh is not None:
                auth_path = Path(env["CODEX_HOME"]) / "auth.json"
                auth_path.write_text(self.auth_refresh, encoding="utf-8")
            if self.worker_edit:
                self.worker_edit(Path(cwd), self.exec_calls)
            return result(argv, self.worker_rc, self.worker_jsonl(argv, cwd, self.exec_calls),
                          self.worker_stderr, timed_out=self.worker_timed_out)
        call["kind"] = "verification"
        return result(argv, self.test_rc, self.verification_output(argv, cwd))
