"""Shared helpers for the suite: real disposable git repos, mocked worker subprocess."""
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import local_worker as lw  # noqa: E402

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


def write_settings(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    settings = root / "settings.json"
    settings.write_text(json.dumps({"auth": {"api_key": SECRET},
                                    "server": {"host": lw.HOST, "port": lw.PORT},
                                    "model": {"model_fallback": False}}), encoding="utf-8")
    model_settings = root / "model_settings.json"
    model_settings.write_text(json.dumps(
        {"models": {lw.CANONICAL_MODEL: {"model_alias": lw.EXPECTED_ALIAS}}}), encoding="utf-8")
    return settings, model_settings


def models_available(endpoint, api_key):
    return [lw.EXPECTED_ALIAS, "some-other-model"]


def models_missing(endpoint, api_key):
    return ["some-other-model"]


def task_payload(repo, **overrides):
    payload = {"task_id": "task-1", "repo_path": str(Path(repo)), "base_ref": "main",
               "goal": "Make add() return the sum.", "acceptance_criteria": ["test_add passes"],
               "allowed_paths": ["add.py"], "test_commands": [["python3", "-m", "unittest", "-v"]],
               "timeout_seconds": 60, "max_attempts": 1}
    payload.update(overrides)
    return payload


def simctl_json(devices=None, runtime=APPLE_RUNTIME):
    """`xcrun simctl list devices available --json` payload with the devices asked for."""
    if devices is None:
        devices = [{"udid": APPLE_UDID, "name": "Apple TV 4K 3", "isAvailable": True}]
    return json.dumps({"devices": {runtime: devices}, "runtimes": [], "bundles": []})


def apple_home(parent, udid=APPLE_UDID, name="home"):
    """Fake HOME holding exactly one CoreSimulator device directory."""
    home = Path(parent) / name
    (home / lw.apple.DEVICES_RELATIVE / udid).mkdir(parents=True, exist_ok=True)
    return home


def job_options(root, repo, verify=True, list_models=models_available, max_attempts=1,
                apple_udid=None):
    settings, model_settings = write_settings(Path(root) / "omlx")
    task = lw.Task.from_payload(task_payload(repo, max_attempts=max_attempts))
    return lw.JobOptions(task=task, state_dir=Path(root) / "state", codex_bin="codex-mock",
                         settings_path=settings, model_settings_path=model_settings,
                         list_models=list_models, verify=verify, apple_simulator_udid=apple_udid)


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


class FakeRun:
    """Stand-in for run_bounded: the worker and the sandbox are mocked, every git call is real."""

    def __init__(self, worker_rc=0, test_rc=0, probe_ok=True, worker_edit=None,
                 worker_timed_out=False, worker_stderr="", apple=None):
        self.worker_rc, self.test_rc, self.probe_ok = worker_rc, test_rc, probe_ok
        self.worker_edit, self.worker_timed_out, self.worker_stderr = (worker_edit,
            worker_timed_out, worker_stderr)
        # Knobs for the mocked /usr/bin/sandbox-exec backend: devices, verdicts, failures.
        self.apple = dict(apple or {})
        self.calls, self.exec_calls = [], 0

    def calls_of(self, kind):
        return [call for call in self.calls if call["argv"][1:2] == [kind]]

    def apple_calls(self):
        return [call for call in self.calls if call["argv"][0] == lw.apple.SANDBOX_EXEC]

    @staticmethod
    def _seatbelt_roots(text):
        """Read the generated profile's own allow/deny file-write* subpaths."""
        allowed, denied = [], []
        for action, entries in write_statements(text):
            paths = [path for kind, path in entries if kind == "subpath"]
            (allowed if action == "allow" else denied).extend(paths)
        return allowed, denied

    def _apple_write_allowed(self, text, target):
        """Seatbelt keeps the last match, so a deny entry always beats an earlier allow."""
        if self.apple.get("writes") == "allow_all":
            return True
        if self.apple.get("writes") == "deny_all":
            return False
        return seatbelt_write_allowed(text, target)

    def _sandbox_exec(self, argv):
        """Mock /usr/bin/sandbox-exec: honour the profile it was handed, never run bare."""
        if self.apple.get("no_backend"):
            return result(argv, 1, "", "sandbox-exec: No such file or directory")
        if argv[1:2] != ["-f"]:
            return result(argv, 1, "", "sandbox-exec: expected -f <profile>")
        try:
            text = Path(argv[2]).read_text(encoding="utf-8")
        except OSError as error:
            return result(argv, 1, "", f"sandbox-exec: cannot read profile: {error}")
        inner = argv[3:]
        command = Path(inner[0]).name if inner else ""
        if self.apple.get("deny_everything"):
            return result(argv, 1, "", "sandbox-exec: operation not permitted")
        if command == "xcodebuild":
            if self.apple.get("xcode_fail"):
                return result(argv, 1, "", "xcode-select: error: no developer directory")
            return result(argv, 0, "Xcode 16.0\nBuild version 16A242d\n")
        if command == "xcrun":
            if "simctl" not in inner:
                return result(argv, 1, "", "sandbox-exec: not mocked: " + " ".join(inner))
            if self.apple.get("simctl_fail"):
                return result(argv, 1, "", "xcrun: error: unable to find utility simctl")
            # JSON belongs on stdout and diagnostics on stderr, exactly like the real simctl,
            # so a test can prove the backend parses stdout alone.
            return result(argv, 0, self.apple.get("simctl")
                          or simctl_json(self.apple.get("devices")),
                          self.apple.get("simctl_stderr", ""))
        if command == "touch":
            if self._apple_write_allowed(text, inner[1]):
                Path(inner[1]).parent.mkdir(parents=True, exist_ok=True)
                Path(inner[1]).write_text("bounded\n", encoding="utf-8")
                return result(argv, 0, "")
            return result(argv, 1, "", f"touch: {inner[1]}: Operation not permitted")
        if command.startswith("python") and "-c" in inner:
            verdict = self.apple.get("connect")
            if verdict is None:
                verdict = "DENIED" if "(deny network" in text else "CONNECTED"
            return result(argv, 0, verdict + "\n")
        # Anything else is a contract verification command wrapped in the profile: the same
        # verdict the mocked codex sandbox gives, so pass/fail knobs stay comparable.
        return result(argv, self.test_rc, "Ran 1 test OK\n")

    def __call__(self, argv, *, cwd, env, timeout, stdin_data=None):
        argv = [str(item) for item in argv]
        self.calls.append({"argv": argv, "cwd": Path(cwd) if cwd else None, "env": dict(env),
                           "timeout": timeout, "stdin": (stdin_data or b"").decode("utf-8", "replace")})
        if argv[0] == lw.apple.SANDBOX_EXEC:
            return self._sandbox_exec(argv)
        if argv == list(lw.FULL_HOST_PROBE_ARGV):
            # The default readiness probe is one trivial command on the host: answer it the way
            # the host would, so ``probe_ok`` still decides whether execution is available.
            return result(argv, 0 if self.probe_ok else 1,
                          lw.PROBE_MARKER if self.probe_ok else "full host refused")
        if argv[1:2] == ["sandbox"] and lw.PROBE_MARKER in argv:
            return result(argv, 0 if self.probe_ok else 1,
                          lw.PROBE_MARKER if self.probe_ok else "sandbox refused")
        if argv[1:2] == ["exec"]:
            self.exec_calls += 1
            if self.worker_edit:
                self.worker_edit(Path(cwd), self.exec_calls)
            stream = json.dumps({"type": "turn.completed", "model": lw.EXPECTED_ALIAS,
                                 "usage": {"input_tokens": 11, "output_tokens": 7}})
            return result(argv, self.worker_rc, stream + "\n", self.worker_stderr,
                          timed_out=self.worker_timed_out)
        return result(argv, self.test_rc, "Ran 1 test OK\n")
