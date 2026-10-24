"""`src/plugin_setup.py` runs as a subprocess against real temporary homes and state paths.

Every case copies the package to a relocated path containing spaces, invokes the script from a
different working directory, and points `CODEX_HOME`, `LOCAL_WORKER_STATE_DIR` and `HOME` at
throwaway directories, so root/config resolution is exercised rather than mirrored. Nothing here
touches a developer's real `~/.codex` or `~/.local-implement`, and the only launcher involved is
an explicitly fake one: no server, no model inference, no network.
"""
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICY_RELATIVE = "skills/local-worker/references/global-policy.md"
BEGIN = "<!-- local-worker begin -->"
END = "<!-- local-worker end -->"
SENTINEL = "sk-SENTINEL-9f2c41d7b6e4"
LEGACY_BYTES = (ROOT / "evidence" / "global-default" / "AGENTS.md").read_bytes()
HEAD = b"# My own Codex defaults\n\n- Always run the focused tests before shipping.\n"
TAIL = b"\n## Private notes\n- keep \xff\xfe these exact bytes\n"
PACKAGE_KEYS = ("canonical_policy", "launcher", "launcher_skill", "setup_script",
                "launcher_engine", "skill_local-worker", "skill_setup", "skill_doctor",
                "onboarding_skill")

# The exact success receipt `bin/local-worker preflight` prints; nothing else may pass.
RECEIPT = {"endpoint": "http://127.0.0.1:8000/v1",
           "canonical_model": "Qwen3.8-Flash-Next-UD-Q4_K_XL",
           "api_model": "qwen-3.8-nf-splash",
           "model_fallback": False,
           "api_key": SENTINEL,
           "models_endpoint": "http://127.0.0.1:8000/v1/models",
           "served_models": ["gpt-oss-120b", "qwen-3.8-nf-splash"],
           "proxies": "disabled",
           "redirects": "refused"}


def launcher_script(payload, returncode, before=""):
    """A fake packaged launcher: heredoc JSON, optional side effect, fixed exit status."""
    body = "cat <<'ENDJSON'\n" + json.dumps(payload) + "\nENDJSON\n"
    return "#!/bin/sh\n" + before + body + "exit " + str(returncode) + "\n"


class SetupCase(unittest.TestCase):
    """One relocated package, one temporary home, one temporary shared state directory."""

    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="Local Worker Setup "))
        self.package = self.copy_package("codex-local-worker")
        self.home = self.temp / "codex home"
        self.state = self.temp / "shared state" / "state dir"
        self.fake_home = self.temp / "user home"
        self.working = self.temp / "elsewhere"
        for directory in (self.fake_home, self.working):
            directory.mkdir(parents=True)
        self.agents = self.home / "AGENTS.md"
        self.addCleanup(shutil.rmtree, str(self.temp), True)

    def copy_package(self, name, evidence=True):
        """A relocated installed copy, with spaces in the path, never the developer checkout."""
        target = self.temp / "installs with spaces" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        patterns = [".git", "__pycache__", ".pytest_cache"]
        if not evidence:
            patterns.append("evidence")
        shutil.copytree(str(ROOT), str(target), symlinks=False,
                        ignore=shutil.ignore_patterns(*patterns))
        return Path(os.path.realpath(target))

    def environment(self, **overrides):
        base = {key: value for key, value in os.environ.items()
                if not key.startswith("LOCAL_WORKER_")}
        base.update({"HOME": str(self.fake_home), "CODEX_HOME": str(self.home),
                     "LOCAL_WORKER_STATE_DIR": str(self.state)})
        base.update(overrides)
        for key in [key for key, value in base.items() if value is None]:
            del base[key]
        return base

    def invoke(self, args=("--check",), package=None, env=None, cwd=None):
        script = Path(package or self.package) / "src" / "plugin_setup.py"
        result = subprocess.run(["python3", str(script)] + list(args),
                                cwd=str(cwd or self.working), env=env or self.environment(),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                universal_newlines=True, timeout=120)
        output = result.stdout + result.stderr
        try:
            report = json.loads(result.stdout)
        except ValueError:
            self.fail("stdout is not one JSON document for %r:\n%s" % (list(args), output))
        self.assertIsInstance(report, dict)
        return result.returncode, report, output

    def write_agents(self, raw):
        self.home.mkdir(parents=True, exist_ok=True)
        self.agents.write_bytes(raw)
        return raw

    def block_of(self, text):
        return text[text.index(BEGIN):text.index(END) + len(END)]

    def backups(self):
        folder = self.state / "setup-backups"
        return sorted(str(path) for path in folder.iterdir()) if folder.is_dir() else []

    def snapshot(self, base):
        return {str(path.relative_to(base)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(Path(base).rglob("*")) if path.is_file()}

    def fake_launcher(self, payload, returncode=0, before="", package=None):
        launcher = Path(package or self.package) / "bin" / "local-worker"
        launcher.write_text(launcher_script(payload, returncode, before), encoding="utf-8")
        os.chmod(str(launcher), 0o755)
        return launcher


class ManagedBlockTextTest(SetupCase):
    """The block's inline repair command must survive a shell wherever the plugin lands."""

    def relocated_copy(self, name):
        """An install root with spaces *and* an apostrophe, beyond what `copy_package` uses."""
        target = self.temp / "installs with spaces" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(str(ROOT), str(target), symlinks=False,
                        ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"))
        return Path(os.path.realpath(target))

    def test_generated_repair_command_round_trips_through_a_shell(self):
        package = self.relocated_copy("codex-local-worker's copy")
        code, report, output = self.invoke(["--apply"], package=package)
        self.assertEqual(code, 0, output)
        text = self.agents.read_text(encoding="utf-8")
        setup = str(Path(package) / "src" / "plugin_setup.py")
        self.assertIn("'", setup, "the fixture must relocate through an apostrophe")
        matches = re.findall(r"`(python3 [^`\n]* --check)`", text)
        self.assertEqual(len(matches), 1, "exactly one inline repair command expected:\n" + text)
        self.assertEqual(shlex.split(matches[0]), ["python3", setup, "--check"],
                         "the generated command must be shell-safe and round-trip")
        self.assertNotIn("python3 %s --check" % setup, text,
                         "the raw unquoted path must not be emitted")

    def test_markdown_links_keep_their_angle_brackets_unquoted(self):
        package = self.relocated_copy("plain copy")
        code, report, output = self.invoke(["--apply"], package=package)
        self.assertEqual(code, 0, output)
        text = self.agents.read_text(encoding="utf-8")
        policy = str(Path(package) / POLICY_RELATIVE)
        self.assertIn("[global-policy.md](<%s>)" % policy, text)
        self.assertIn("[local-worker/SKILL.md](<%s>)" % str(
            Path(package) / "skills" / "local-worker" / "SKILL.md"), text)
        self.assertIn("<%s>" % str(package), text)

    def test_managed_block_carries_the_completion_rule_idempotently(self):
        """Completion cleanup is global guidance, so it belongs in the one owned block."""
        self.write_agents(HEAD + TAIL)
        code, report, output = self.invoke(["--apply"])
        self.assertEqual(code, 0, output)
        raw = self.agents.read_bytes()
        text = raw.decode("utf-8", "surrogateescape")
        self.assertEqual(text.replace(self.block_of(text), "").strip(),
                         (HEAD + TAIL).decode("utf-8", "surrogateescape").strip(),
                         "unrelated bytes survive the completion rule")
        self.assertEqual(text.count(BEGIN), 1)
        self.assertEqual(text.count(END), 1)
        block = self.block_of(text).lower()
        for phrase in ("commit and push", "archive", "remove completed",
                       "preserve the primary checkout", "report the blocker"):
            self.assertIn(phrase, block, "completion instruction missing: " + phrase)
        again, repeat, output = self.invoke(["--apply"])
        self.assertEqual(again, 0, output)
        self.assertFalse(repeat["apply"]["changed"], "the completion rule must not churn")
        self.assertEqual(self.agents.read_bytes(), raw)


class CheckReportTest(SetupCase):
    """The default mode is a read-only inventory; it must never touch a user file."""

    def test_check_reports_identity_package_and_environment_without_writing(self):
        before = self.snapshot(self.package)
        code, report, output = self.invoke()
        self.assertEqual(code, 0, output)
        self.assertTrue(report["ok"])
        self.assertEqual(report["mode"], "check")
        self.assertEqual(report["exit_code"], 0)
        self.assertFalse(report["writes_performed"])
        self.assertEqual(report["plugin"]["name"], "codex-local-worker")
        self.assertEqual(report["plugin"]["root"], str(self.package))
        self.assertEqual(report["plugin"]["version"], "0.2.0")
        self.assertEqual(Path(report["plugin"]["manifest"]).name, "plugin.json")
        self.assertEqual(Path(report["plugin"]["onboarding_skill"]).name, "SKILL.md")
        self.assertEqual(Path(report["codex_home"]).resolve(), self.home.resolve())
        self.assertEqual(Path(report["state_dir"]).resolve(), self.state.resolve())
        for key in PACKAGE_KEYS:
            entry = report["package"][key]
            self.assertTrue(entry["exists"], key)
            self.assertTrue(Path(entry["path"]).is_file(), key)
            self.assertTrue(str(Path(entry["path"])).startswith(str(self.package)), key)
        self.assertEqual(report["package"]["canonical_policy"]["path"],
                         str(self.package / POLICY_RELATIVE))
        self.assertEqual(report["managed_block"]["state"], "absent")
        self.assertFalse(report["managed_block"]["file_exists"])
        self.assertIsNone(report["managed_block"]["install_root"])
        environment = report["environment"]
        self.assertTrue(environment["python"]["version"])
        self.assertIn(environment["git"]["source"], ("PATH", "missing"))
        self.assertIn(report["environment"]["codex_cli"]["source"],
                      ("PATH", "missing", "LOCAL_WORKER_CODEX_BIN"))
        self.assertTrue(environment["native"]["optional"])
        self.assertFalse(environment["omlx"]["contents_read"])
        self.assertEqual(report["preflight"]["status"], "not-requested")
        self.assertFalse(report["preflight"]["endpoint_passed"])
        self.assertIn("endpoint", output)
        self.assertFalse(self.home.exists(), "check must not create the global directory")
        self.assertFalse(self.state.exists(), "check must not create shared state")
        self.assertEqual(self.snapshot(self.package), before, "check must not write in the package")
        self.assertEqual(list(Path(self.package).rglob("__pycache__")), [])

    def test_home_defaults_come_from_home_when_the_env_vars_are_unset(self):
        code, report, output = self.invoke(env=self.environment(CODEX_HOME=None,
                                                                LOCAL_WORKER_STATE_DIR=None))
        self.assertEqual(code, 0, output)
        self.assertEqual(report["codex_home"], os.path.normpath(
            os.path.join(str(self.fake_home), ".codex")))
        self.assertEqual(report["state_dir"], os.path.normpath(
            os.path.join(str(self.fake_home), ".local-implement")))
        self.assertFalse(self.fake_home.joinpath(".codex").exists())

    def test_codex_binary_override_is_reported_without_running_anything(self):
        override = self.temp / "some bin" / "codex-fake"
        override.parent.mkdir(parents=True, exist_ok=True)
        override.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        os.chmod(str(override), 0o755)
        code, report, output = self.invoke(env=self.environment(LOCAL_WORKER_CODEX_BIN=
                                                                str(override)))
        self.assertEqual(code, 0, output)
        cli = report["environment"]["codex_cli"]
        self.assertEqual(cli["source"], "LOCAL_WORKER_CODEX_BIN")
        self.assertTrue(cli["available"], cli)
        self.assertEqual(cli["path"], str(override))
        _, unset, _ = self.invoke(env=self.environment(LOCAL_WORKER_CODEX_BIN=None))
        self.assertEqual(unset["environment"]["codex_cli"]["source"],
                         "PATH" if shutil.which("codex") else "missing")

    def test_local_model_files_are_reported_as_presence_only(self):
        settings = self.fake_home / ".omlx" / "settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(json.dumps({"api_key": SENTINEL, "model": "Qwen3.8-Flash-Next"}),
                            encoding="utf-8")
        (settings.parent / "model_settings.json").write_text('{"note": "' + SENTINEL + '"}',
                                                            encoding="utf-8")
        code, report, output = self.invoke()
        self.assertEqual(code, 0, output)
        omlx = report["environment"]["omlx"]
        self.assertEqual(omlx["settings"]["kind"], "file")
        self.assertTrue(omlx["model_settings"]["exists"])
        self.assertFalse(omlx["contents_read"])
        self.assertNotIn(SENTINEL, output)
        self.assertNotIn("api_key", output)
        self.assertNotIn("Qwen3.8-Flash-Next", output)

    def test_conflicting_or_unknown_modes_are_usage_errors_that_write_nothing(self):
        for args in (["--check", "--apply"], ["--apply", "--check"], ["--repair"]):
            result = subprocess.run(["python3", str(Path(self.package) / "src" /
                                                    "plugin_setup.py")] + args,
                                    cwd=str(self.working), env=self.environment(),
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    universal_newlines=True, timeout=60)
            self.assertEqual(result.returncode, 2, args)
        self.assertFalse(self.home.exists())
        self.assertFalse(self.state.exists())

    def test_missing_packaged_launcher_engine_is_refused_not_reported_ready(self):
        trimmed = self.copy_package("launcher without its engine")
        (trimmed / "src" / "local_worker.py").unlink()
        self.write_agents(HEAD)
        code, report, output = self.invoke(package=trimmed)
        self.assertEqual(code, 3, output)
        self.assertEqual(report["error"]["stage"], "package")
        self.assertIn("launcher_engine", report["error"]["detail"])
        self.assertIn("local_worker.py", report["error"]["detail"])
        code, report, output = self.invoke(["--apply"], package=trimmed)
        self.assertEqual(code, 3, output)
        self.assertNotIn("apply", report, "an incomplete package may never reach the write path")
        self.assertEqual(self.agents.read_bytes(), HEAD, "refusal must leave AGENTS.md alone")
        self.assertEqual(self.backups(), [])
        code, report, output = self.invoke()
        self.assertEqual(code, 0, output)
        engine = report["package"]["launcher_engine"]
        self.assertTrue(engine["exists"])
        self.assertEqual(Path(engine["path"]), Path(self.package) / "src" / "local_worker.py")


class ApplyTest(SetupCase):
    """Authorized apply: one small authoritative block, backups first, unrelated bytes intact."""

    def test_apply_installs_one_small_block_and_backs_up_existing_bytes(self):
        self.write_agents(HEAD)
        code, report, output = self.invoke(["--apply"])
        self.assertEqual(code, 0, output)
        self.assertEqual(report["mode"], "apply")
        self.assertTrue(report["writes_performed"])
        receipt = report["apply"]
        self.assertTrue(receipt["changed"])
        self.assertEqual(receipt["reason"], "installed")
        self.assertEqual(Path(receipt["destination"]).resolve(), self.agents.resolve())
        self.assertEqual(receipt["written_paths"], [str(self.agents)])
        self.assertEqual(receipt["before_sha256"], hashlib.sha256(HEAD).hexdigest())
        self.assertIsNotNone(receipt["backup"], "existing AGENTS bytes must be backed up first")
        backup = Path(receipt["backup"])
        self.assertEqual(backup.read_bytes(), HEAD)
        self.assertEqual(backup.parent, self.state / "setup-backups")
        self.assertEqual(backup.stat().st_mode & 0o077, 0)
        self.assertIn("setup-backups", receipt["backup"])
        raw = self.agents.read_bytes()
        self.assertTrue(raw.startswith(HEAD), "unrelated instructions must survive byte-for-byte")
        text = raw.decode("utf-8", "surrogateescape")
        self.assertEqual(text.count(BEGIN), 1)
        self.assertEqual(text.count(END), 1)
        block = self.block_of(text)
        # Pointer paths plus the completion rule: still a fraction of a page, and bounded
        # even when the install root and temporary home are both long.
        self.assertLess(len(block), 2000, "global guidance must stay small")
        self.assertIn("[global-policy.md](<" + str(self.package) + "/" + POLICY_RELATIVE + ">)",
                      block)
        self.assertIn("<" + str(self.package) + "/skills/local-worker/SKILL.md>", block)
        self.assertIn("<" + str(self.package) + ">", block)
        policy = (self.package / POLICY_RELATIVE).read_text(encoding="utf-8")
        self.assertNotIn(policy[:120], block, "the authoritative policy stays in the package")
        self.assertEqual(report["apply"]["verified_state"], "current")
        installed = receipt["installed"]["canonical_policy"]
        self.assertEqual(installed["path"], str(self.package / POLICY_RELATIVE))
        self.assertEqual(installed["sha256"],
                         hashlib.sha256((self.package / POLICY_RELATIVE).read_bytes()).hexdigest())
        self.assertEqual(receipt["after_sha256"], hashlib.sha256(raw).hexdigest())

    def test_apply_to_a_fresh_home_creates_no_backup_folder(self):
        code, report, output = self.invoke(["--apply"])
        self.assertEqual(code, 0, output)
        receipt = report["apply"]
        self.assertTrue(receipt["changed"])
        self.assertEqual(receipt["before_sha256"], None)
        self.assertIsNone(receipt["backup"], "nothing existed to back up")
        self.assertTrue(self.home.is_dir())
        self.assertTrue(self.agents.is_file())
        self.assertFalse((self.state / "setup-backups").exists())
        self.assertFalse(self.state.exists())
        again_code, again, _ = self.invoke(["--apply"])
        self.assertEqual(again_code, 0)
        self.assertFalse(again["apply"]["changed"])
        self.assertEqual(self.backups(), [])

    def test_exact_legacy_file_migrates_to_a_header_and_one_block(self):
        self.write_agents(LEGACY_BYTES)
        code, report, _ = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(report["managed_block"]["state"], "absent")
        self.assertTrue(report["managed_block"].get("legacy_exact"))
        self.assertTrue(report["legacy"]["shipped"])
        self.assertTrue(report["legacy"]["matches_embedded_digest"])
        code, report, output = self.invoke(["--apply"])
        self.assertEqual(code, 0, output)
        self.assertTrue(report["apply"]["changed"])
        self.assertEqual(report["apply"]["reason"], "migrated_legacy")
        text = self.agents.read_text(encoding="utf-8")
        lines = text.splitlines()
        self.assertEqual(lines[0], LEGACY_BYTES.decode("utf-8").splitlines()[0])
        self.assertEqual(lines[1], "")
        self.assertEqual(lines[2], BEGIN)
        self.assertEqual(text.count(BEGIN), 1)
        self.assertNotIn("/.codex/skills/local-worker/SKILL.md", text,
                         "the legacy global file must not be kept as a duplicate policy")
        self.assertIn("<" + str(self.package) + "/" + POLICY_RELATIVE + ">", text)
        self.assertEqual(Path(report["apply"]["backup"]).read_bytes(), LEGACY_BYTES)
        after_code, after, _ = self.invoke()
        self.assertEqual(after_code, 0)
        self.assertEqual(after["managed_block"]["state"], "current")

    def test_legacy_migration_works_in_a_relocated_copy_without_the_fixture(self):
        relocated = self.copy_package("no evidence here", evidence=False)
        self.assertFalse((relocated / "evidence").exists())
        self.write_agents(LEGACY_BYTES)
        code, report, _ = self.invoke(package=relocated)
        self.assertEqual(code, 0)
        self.assertFalse(report["legacy"]["shipped"])
        self.assertTrue(report["managed_block"].get("legacy_exact"),
                        "the embedded digest must still recognize the legacy file")
        code, report, output = self.invoke(["--apply"], package=relocated)
        self.assertEqual(code, 0, output)
        self.assertEqual(report["apply"]["reason"], "migrated_legacy")
        self.assertIn("<" + str(relocated) + "/" + POLICY_RELATIVE + ">",
                      self.agents.read_text(encoding="utf-8"))

    def test_unrelated_bytes_survive_when_a_relocated_root_updates_the_block(self):
        relocated = self.copy_package("relocated target with spaces")
        self.assertIn(" ", str(relocated))
        self.write_agents(HEAD)
        first, first_report, output = self.invoke(["--apply"])
        self.assertEqual(first, 0, output)
        with open(str(self.agents), "ab") as handle:
            handle.write(TAIL)
        before = self.agents.read_bytes()
        code, report, _ = self.invoke(package=relocated)
        self.assertEqual(code, 0)
        self.assertEqual(report["managed_block"]["state"], "drift")
        self.assertEqual(report["managed_block"]["install_root"], str(self.package))
        self.assertFalse(report["managed_block"]["managed_root_matches_this_root"])
        code, report, output = self.invoke(["--apply"], package=relocated)
        self.assertEqual(code, 0, output)
        self.assertEqual(report["apply"]["reason"], "updated")
        raw = self.agents.read_bytes()
        self.assertTrue(raw.startswith(HEAD))
        self.assertTrue(raw.endswith(TAIL), "trailing user bytes must survive exactly")
        self.assertNotIn(str(self.package), raw.decode("utf-8", "surrogateescape"))
        self.assertIn(str(relocated) + "/" + POLICY_RELATIVE,
                      raw.decode("utf-8", "surrogateescape"))
        self.assertEqual(raw.split(TAIL, 1)[0].split(HEAD, 1)[1].count(BEGIN.encode()), 1)
        self.assertEqual(Path(report["apply"]["backup"]).read_bytes(), before)
        check_code, check, _ = self.invoke(package=relocated)
        self.assertEqual(check_code, 0)
        self.assertEqual(check["managed_block"]["state"], "current")
        stale_code, stale, _ = self.invoke(package=self.package)
        self.assertEqual(stale_code, 0, "a stale root reports drift informationally")
        self.assertEqual(stale["managed_block"]["state"], "drift")

    def test_repeated_apply_is_a_noop_without_extra_backups(self):
        self.write_agents(HEAD)
        first, first_report, _ = self.invoke(["--apply"])
        self.assertEqual(first, 0)
        count_after_first = len(self.backups())
        self.assertEqual(count_after_first, 1)
        raw = self.agents.read_bytes()
        stamp = os.stat(str(self.agents)).st_mtime_ns
        for _ in range(2):
            code, report, output = self.invoke(["--apply"])
            self.assertEqual(code, 0, output)
            self.assertFalse(report["apply"]["changed"], "identical apply is a no-op")
            self.assertEqual(report["apply"]["reason"],
                             "managed block already current; nothing written")
            self.assertIsNone(report["apply"]["backup"])
            self.assertEqual(report["apply"]["written_paths"], [])
            self.assertFalse(report["writes_performed"])
        self.assertEqual(self.agents.read_bytes(), raw)
        self.assertEqual(os.stat(str(self.agents)).st_mtime_ns, stamp)
        self.assertEqual(len(self.backups()), count_after_first)

    def test_malformed_or_duplicate_markers_are_refused_without_modification(self):
        cases = {
            "duplicate": HEAD + BEGIN.encode() + b"\n## old\n" + END.encode() + b"\n"
                         + BEGIN.encode() + b"\n## another\n" + END.encode() + b"\n",
            "unbalanced": HEAD + BEGIN.encode() + b"\n## never closed\n",
            "inline": HEAD + b"see " + BEGIN.encode() + b" inline for details\n",
        }
        for label, raw in cases.items():
            with self.subTest(label):
                self.write_agents(raw)
                code, report, output = self.invoke()
                self.assertEqual(code, 4, output)
                self.assertEqual(report["managed_block"]["state"], "malformed")
                self.assertEqual(report["exit_code"], 4)
                self.assertFalse(report["ok"])
                code, report, output = self.invoke(["--apply"])
                self.assertEqual(code, 4, output)
                self.assertEqual(report["error"]["stage"], "managed_block")
                self.assertNotIn("apply", report)
                self.assertEqual(self.agents.read_bytes(), raw, "refusal must preserve bytes")
                self.assertEqual(self.backups(), [])
                self.assertFalse(self.state.exists())

    def test_missing_canonical_policy_refuses_before_any_write(self):
        (self.package / POLICY_RELATIVE).unlink()
        self.write_agents(HEAD)
        code, report, output = self.invoke()
        self.assertEqual(code, 3, output)
        self.assertEqual(report["error"]["stage"], "package")
        self.assertIn("global-policy.md", report["error"]["detail"])
        code, report, output = self.invoke(["--apply"])
        self.assertEqual(code, 3, output)
        self.assertNotIn("apply", report)
        self.assertEqual(self.agents.read_bytes(), HEAD)
        self.assertEqual(self.backups(), [])
        self.assertFalse(self.state.exists())

    def test_malformed_manifest_identity_refuses_with_a_nonzero_code(self):
        (self.package / "plugin.json").write_text("{ not json at all", encoding="utf-8")
        code, report, output = self.invoke()
        self.assertEqual(code, 3, output)
        self.assertEqual(report["error"]["stage"], "manifest")
        self.assertFalse(self.home.exists())
        wrong = self.copy_package("wrong name")
        manifest = json.loads((wrong / "plugin.json").read_text(encoding="utf-8"))
        manifest["name"] = "some-other-plugin"
        (wrong / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
        code, report, output = self.invoke(package=wrong)
        self.assertEqual(code, 3, output)
        self.assertEqual(report["error"]["stage"], "manifest")
        self.assertFalse(self.home.exists())


class PreflightReceiptTest(SetupCase):
    """Endpoint claims only exist when the packaged launcher actually answered."""

    def test_zero_exit_without_a_complete_success_receipt_is_not_an_endpoint_pass(self):
        incomplete = {key: value for key, value in RECEIPT.items() if key != "api_model"}
        cases = [("json list", []),
                 ("json string", "ready"),
                 ("blocked object", {"status": "blocked", "reason": "model not loaded"}),
                 ("empty object", {}),
                 ("fallback enabled", dict(RECEIPT, model_fallback=True)),
                 ("fallback as text", dict(RECEIPT, model_fallback="false")),
                 ("missing api_model", incomplete),
                 ("blank endpoint", dict(RECEIPT, endpoint="   ")),
                 ("model not served", dict(RECEIPT, served_models=["gpt-oss-120b"])),
                 ("served_models not a list", dict(RECEIPT, served_models="qwen-3.8-nf-splash"))]
        for label, payload in cases:
            with self.subTest(case=label):
                self.fake_launcher(payload)
                code, report, output = self.invoke(["--check", "--preflight"])
                self.assertEqual(code, 6, output)
                self.assertFalse(report["ok"])
                preflight = report["preflight"]
                self.assertEqual(preflight["status"], "failed")
                self.assertFalse(preflight["endpoint_passed"])
                self.assertEqual(preflight["returncode"], 0)
                self.assertIn("receipt", preflight["reason"])
                self.assertFalse(report["writes_performed"])
                self.assertNotIn(SENTINEL, output)


class AgentsOverrideTest(SetupCase):
    """Codex reads a nonempty `AGENTS.override.md` before `AGENTS.md`: setup must say so."""

    def test_active_override_is_a_check_blocker_that_prints_no_contents(self):
        original = self.write_agents(HEAD)
        override = self.home / "AGENTS.override.md"
        override.write_text("# private active defaults\n- route everything locally\n",
                            encoding="utf-8")
        before = self.snapshot(self.home)
        code, report, output = self.invoke(["--check"])
        self.assertEqual(code, 7, output)
        self.assertFalse(report["ok"])
        section = report["agents_override"]
        self.assertEqual(section["path"], str(override))
        self.assertTrue(section["exists"])
        self.assertEqual(section["state"], "active")
        self.assertTrue(section["active"])
        self.assertTrue(section["supersedes_managed_block"])
        self.assertIn("AGENTS.override.md", section["reason"])
        self.assertIn("AGENTS.md", section["reason"])
        self.assertNotIn("private active defaults", output, "contents must never be printed")
        self.assertEqual(report["managed_block"]["state"], "absent")
        self.assertEqual(self.snapshot(self.home), before, "check must read only")
        self.assertFalse(self.state.exists())
        self.assertEqual(self.agents.read_bytes(), original)

    def test_apply_refuses_before_any_backup_or_agents_mutation(self):
        original = self.write_agents(HEAD)
        override = self.home / "AGENTS.override.md"
        override.write_text("# private active defaults\n", encoding="utf-8")
        override_bytes = override.read_bytes()
        code, report, output = self.invoke(["--apply"])
        self.assertEqual(code, 7, output)
        self.assertFalse(report["ok"])
        self.assertEqual(report["error"]["stage"], "agents_override")
        self.assertIn("refuses before any backup", report["error"]["detail"])
        self.assertNotIn("apply", report, "no apply receipt may exist")
        self.assertEqual(self.agents.read_bytes(), original, "AGENTS.md must stay untouched")
        self.assertNotIn(BEGIN, self.agents.read_text(encoding="utf-8"))
        self.assertEqual(self.backups(), [], "no backup may be written")
        self.assertFalse(self.state.exists())
        self.assertEqual(override.read_bytes(), override_bytes, "the override must survive")

    def test_empty_or_whitespace_override_still_installs_normally(self):
        for raw in (b"", b"   \n\t\n"):
            with self.subTest(raw=repr(raw)):
                for stale in (self.agents, self.home / "AGENTS.override.md"):
                    if stale.exists():
                        stale.unlink()
                shutil.rmtree(str(self.state), ignore_errors=True)
                self.write_agents(HEAD)
                (self.home / "AGENTS.override.md").write_bytes(raw)
                code, report, output = self.invoke(["--check"])
                self.assertEqual(code, 0, output)
                self.assertEqual(report["agents_override"]["state"], "empty")
                self.assertFalse(report["agents_override"]["active"])
                code, report, output = self.invoke(["--apply"])
                self.assertEqual(code, 0, output)
                text = self.agents.read_text(encoding="utf-8")
                self.assertIn(BEGIN, text)
                self.assertTrue(text.startswith("# My own Codex defaults"))
                self.assertEqual(len(self.backups()), 1)
                code, report, output = self.invoke(["--check"])
                self.assertEqual(code, 0, output)
                self.assertEqual(report["managed_block"]["state"], "current")

    @unittest.skipIf(os.geteuid() == 0, "root ignores file modes")
    def test_unreadable_override_is_a_concrete_error_never_an_absence(self):
        original = self.write_agents(HEAD)
        override = self.home / "AGENTS.override.md"
        override.write_text("# active but unreadable\n", encoding="utf-8")
        os.chmod(str(override), 0o000)
        self.addCleanup(os.chmod, str(override), 0o644)
        for args in (["--check"], ["--apply"]):
            with self.subTest(args=args):
                code, report, output = self.invoke(args)
                self.assertEqual(code, 7, output)
                self.assertFalse(report["ok"])
                self.assertEqual(report["error"]["stage"], "agents_override")
                self.assertIn("cannot read", report["error"]["detail"])
                self.assertNotIn("absent", str(report["error"]["detail"]))
        self.assertEqual(self.agents.read_bytes(), original)
        self.assertEqual(self.backups(), [])

    def test_a_symlinked_override_is_refused_rather_than_followed(self):
        self.write_agents(HEAD)
        target = self.temp / "somewhere else.md"
        target.write_text("# elsewhere\n", encoding="utf-8")
        os.symlink(str(target), str(self.home / "AGENTS.override.md"))
        code, report, output = self.invoke(["--check"])
        self.assertEqual(code, 7, output)
        self.assertEqual(report["error"]["stage"], "agents_override")
        self.assertIn("symlink", report["error"]["detail"])
        self.assertTrue(target.is_file(), "the symlink target must stay untouched")


class PreflightTest(SetupCase):
    """Endpoint claims only exist when the packaged launcher actually answered."""

    def test_check_without_preflight_never_delegates_or_claims_the_endpoint(self):
        marker = self.temp / "launcher-was-run"
        self.fake_launcher({"ok": True}, 0, before='touch "%s"\n' % marker)
        code, report, output = self.invoke()
        self.assertEqual(code, 0, output)
        self.assertFalse(Path(str(marker)).exists(), "check must not run the launcher")
        self.assertFalse(report["preflight"]["requested"])
        self.assertFalse(report["preflight"]["endpoint_passed"])
        self.assertEqual(report["preflight"]["status"], "not-requested")

    def test_successful_preflight_delegates_and_sanitizes_the_report(self):
        self.fake_launcher(dict(RECEIPT, message="model ready token=" + SENTINEL,
                                nested={"Authorization": "Bearer " + SENTINEL, "model": "qwen"},
                                profile="Qwen3.8-Flash-Next-UD-Q4_K_XL"))
        started = time.time()
        code, report, output = self.invoke(["--check", "--preflight"])
        self.assertLess(time.time() - started, 60)
        self.assertEqual(code, 0, output)
        preflight = report["preflight"]
        self.assertTrue(preflight["requested"])
        self.assertEqual(preflight["status"], "passed")
        self.assertTrue(preflight["endpoint_passed"])
        self.assertEqual(preflight["returncode"], 0)
        self.assertTrue(preflight["stdout_parsed"])
        self.assertEqual(preflight["cwd"], str(self.package))
        self.assertEqual(Path(preflight["command"][0]), Path(self.package) / "bin" /
                         "local-worker")
        self.assertEqual(preflight["command"][1], "preflight")
        self.assertEqual(preflight["timeout_seconds"], 60.0)
        self.assertNotIn(SENTINEL, output)
        detail = preflight["detail"]
        self.assertEqual(detail["api_key"], "<withheld>")
        self.assertEqual(detail["nested"]["Authorization"], "<withheld>")
        self.assertNotIn(SENTINEL, detail["message"])
        self.assertIn("<redacted>", detail["message"])
        self.assertIs(detail["model_fallback"], False)
        self.assertIn(RECEIPT["api_model"], detail["served_models"])
        self.assertIn("receipt", preflight["reason"])

    def test_failing_preflight_is_nonzero_and_never_claims_the_endpoint(self):
        self.fake_launcher({"ok": False, "reason": "endpoint down api_key=" + SENTINEL}, 3)
        code, report, output = self.invoke(["--check", "--preflight"])
        self.assertEqual(code, 6, output)
        self.assertFalse(report["ok"])
        self.assertEqual(report["exit_code"], 6)
        preflight = report["preflight"]
        self.assertEqual(preflight["status"], "failed")
        self.assertFalse(preflight["endpoint_passed"])
        self.assertEqual(preflight["returncode"], 3)
        self.assertIn("exited 3", preflight["reason"])
        self.assertNotIn(SENTINEL, output)

    def test_zero_exit_without_json_is_not_an_endpoint_pass(self):
        launcher = Path(self.package) / "bin" / "local-worker"
        launcher.write_text("#!/bin/sh\necho \"ready api_key=" + SENTINEL + "\"\n"
                            "echo \"raw Bearer " + SENTINEL + "\" >&2\nexit 0\n", encoding="utf-8")
        os.chmod(str(launcher), 0o755)
        code, report, output = self.invoke(["--check", "--preflight"])
        self.assertEqual(code, 6, output)
        self.assertFalse(report["preflight"]["endpoint_passed"])
        self.assertFalse(report["preflight"]["stdout_parsed"])
        self.assertIn("JSON", report["preflight"]["reason"])
        self.assertNotIn(SENTINEL, output, "unparsed launcher output must be redacted too")
        self.assertIn("<redacted>", str(report["preflight"]["stdout_tail"]))

    def test_zero_exit_json_that_is_not_the_receipt_shape_fails(self):
        self.fake_launcher({"status": "blocked", "reason": "model not loaded"})
        code, report, output = self.invoke(["--check", "--preflight"])
        self.assertEqual(code, 6, output)
        preflight = report["preflight"]
        self.assertTrue(preflight["stdout_parsed"], "the JSON did parse; it is simply not a receipt")
        self.assertFalse(preflight["endpoint_passed"])
        self.assertIn("endpoint is missing", preflight["reason"])
        self.assertIsNone(preflight["stdout_tail"])

    def test_explicit_preflight_timeout_is_bounded(self):
        self.fake_launcher({"ok": True}, 0, before="sleep 3\n")
        started = time.time()
        code, report, output = self.invoke(["--check", "--preflight", "--preflight-timeout", "1"])
        elapsed = time.time() - started
        self.assertLess(elapsed, 20, "the delegated call must be bounded")
        self.assertEqual(code, 6, output)
        preflight = report["preflight"]
        self.assertEqual(preflight["timeout_seconds"], 1.0)
        self.assertFalse(preflight["endpoint_passed"])
        self.assertIn("timed out", preflight["reason"])

    def test_missing_launcher_is_reported_rather_than_claimed(self):
        launcher = Path(self.package) / "bin" / "local-worker"
        launcher.unlink()
        code, report, output = self.invoke()
        self.assertEqual(code, 3, output)
        self.assertEqual(report["error"]["stage"], "package")
        partial = self.copy_package("half a package")
        os.chmod(str(partial / "bin" / "local-worker"), 0o644)
        code, report, output = self.invoke(["--check", "--preflight"], package=partial)
        self.assertEqual(code, 6, output)
        self.assertFalse(report["preflight"]["endpoint_passed"])


if __name__ == "__main__":
    unittest.main()
