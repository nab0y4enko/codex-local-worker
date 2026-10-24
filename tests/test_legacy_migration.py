"""The pre-rename (0.1.0) install must migrate into exactly one current managed block.

The old managed block below is frozen *independently* of `src/plugin_setup.py`: if the setup
helper ever changes its markers, these bytes stay what a real 0.1.0 install wrote to
`$CODEX_HOME/AGENTS.md`, so the migration cannot quietly start recognizing only its own output.
Every case runs the helper as a subprocess against a relocated copy of the package with a
throwaway `CODEX_HOME`, so nothing here touches a developer's real global file.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

BEGIN = "<!-- local-worker begin -->"
END = "<!-- local-worker end -->"
OLD_BEGIN = "<!-- local-qwen begin -->"
OLD_END = "<!-- local-qwen end -->"

# Frozen 0.1.0 output: pre-rename markers, pre-rename skill paths, pre-rename section heading.
OLD_BLOCK = """{begin}
## Local Qwen: selected planner, local Qwen implementation

Planning stays on the model selected in the Codex window. Implementation, fixes, refactors
and test authoring run on the pinned local Qwen worker through the plugin launcher. This
block is the only global text the plugin owns; the authoritative policy stays in the package.

- Canonical policy: [global-policy.md](<{root}/skills/local-implement/references/global-policy.md>)
- Bundled launcher skill: [local-implement/SKILL.md](<{root}/skills/local-implement/SKILL.md>)
- Install root: <{root}>
- Read the canonical policy before implementing. If that file is missing, the plugin was
  moved or uninstalled: run `python3 {root}/src/plugin_setup.py --check` from the current
  install root and report the blocker instead of routing implementation to a cloud model.
{end}
""".format(begin=OLD_BEGIN, end=OLD_END, root="/Users/dev/old-checkout/codex-local-first")

HEAD = b"# My own Codex defaults\n\n- Keep these instructions untouched.\n"
TAIL = b"\n## Private notes\n- keep \xff\xfe these exact bytes\n"
STATE_ENV, LEGACY_STATE_ENV = "LOCAL_WORKER_STATE_DIR", "LOCAL_IMPLEMENT_STATE_DIR"
CODEX_ENV, LEGACY_CODEX_ENV = "LOCAL_WORKER_CODEX_BIN", "LOCAL_IMPLEMENT_CODEX_BIN"


class MigrationCase(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="Local Worker Migration "))
        self.addCleanup(shutil.rmtree, str(self.temp), True)
        self.package = self.temp / "installs with spaces" / "codex-local-worker"
        self.package.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(str(ROOT), str(self.package), symlinks=False,
                        ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"))
        self.package = Path(os.path.realpath(self.package))
        self.home = self.temp / "codex home"
        self.state = self.temp / "shared state"
        self.fake_home = self.temp / "user home"
        self.working = self.temp / "elsewhere"
        self.fake_home.mkdir()
        self.working.mkdir()
        self.agents = self.home / "AGENTS.md"

    def environment(self, **overrides):
        base = {key: value for key, value in os.environ.items()
                if not key.startswith(("LOCAL_WORKER_", "LOCAL_IMPLEMENT_"))}
        base.update({"HOME": str(self.fake_home), "CODEX_HOME": str(self.home),
                     STATE_ENV: str(self.state)})
        base.update(overrides)
        for key in [key for key, value in base.items() if value is None]:
            del base[key]
        return base

    def invoke(self, args=("--check",), env=None):
        script = self.package / "src" / "plugin_setup.py"
        result = subprocess.run(["python3", str(script)] + list(args), cwd=str(self.working),
                                env=env or self.environment(), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, universal_newlines=True, timeout=120)
        output = result.stdout + result.stderr
        try:
            report = json.loads(result.stdout)
        except ValueError:
            self.fail("stdout is not one JSON document for %r:\n%s" % (list(args), output))
        return result.returncode, report, output

    def write_agents(self, raw):
        self.home.mkdir(parents=True, exist_ok=True)
        self.agents.write_bytes(raw)
        return raw

    def backups(self):
        folder = self.state / "setup-backups"
        return sorted(str(path) for path in folder.iterdir()) if folder.is_dir() else []


class LegacyPointerMigrationTest(MigrationCase):
    def test_frozen_old_block_is_recognized_and_migrated_to_one_current_block(self):
        raw = self.write_agents(HEAD + OLD_BLOCK.encode("utf-8") + TAIL)
        code, report, output = self.invoke()
        self.assertEqual(code, 0, output)
        block = report["managed_block"]
        self.assertEqual(block["state"], "legacy_pointer")
        self.assertEqual(block["install_root"], "/Users/dev/old-checkout/codex-local-first")
        self.assertFalse(block["managed_root_matches_this_root"])
        self.assertEqual(self.agents.read_bytes(), raw, "--check must not rewrite anything")
        self.assertEqual(self.backups(), [])

        code, report, output = self.invoke(["--apply"])
        self.assertEqual(code, 0, output)
        self.assertEqual(report["apply"]["reason"], "migrated_legacy_pointer")
        self.assertEqual(report["apply"]["verified_state"], "current")
        self.assertEqual(Path(report["apply"]["backup"]).read_bytes(), raw)

        migrated = self.agents.read_bytes()
        self.assertTrue(migrated.startswith(HEAD), "user prefix must survive byte-for-byte")
        self.assertTrue(migrated.endswith(TAIL), "user suffix (incl. non-UTF8 bytes) must survive")
        text = migrated.decode("utf-8", "surrogateescape")
        self.assertEqual(text.count(BEGIN), 1, "exactly one current block may exist")
        self.assertEqual(text.count(END), 1)
        self.assertNotIn(OLD_BEGIN, text, "the pre-rename block must not stay as a second policy")
        self.assertNotIn(OLD_END, text)
        self.assertNotIn("/Users/dev/old-checkout", text, "the stale root must be rewritten")
        self.assertIn(str(self.package / "skills/local-worker/references/global-policy.md"), text)
        self.assertIn(str(self.package / "skills/local-worker/SKILL.md"), text)
        self.assertIn("<%s>" % str(self.package), text)

        again_code, again, again_output = self.invoke(["--apply"])
        self.assertEqual(again_code, 0, again_output)
        self.assertFalse(again["apply"]["changed"], "a second apply must be a no-op")
        self.assertEqual(again["apply"]["reason"], "managed block already current; nothing written")
        self.assertEqual(self.agents.read_bytes(), migrated)
        self.assertEqual(len(self.backups()), 1, "a no-op must not add a backup")
        check_code, check, _ = self.invoke()
        self.assertEqual(check_code, 0)
        self.assertEqual(check["managed_block"]["state"], "current")
        self.assertTrue(check["managed_block"]["managed_root_matches_this_root"])

    def test_mixed_current_and_pre_rename_blocks_refuse_without_writes(self):
        raw = (HEAD + self.current_block().encode("utf-8")
               + OLD_BLOCK.encode("utf-8") + TAIL)
        self.assertRefused(raw)

    def test_duplicate_pre_rename_blocks_refuse_without_writes(self):
        self.assertRefused(HEAD + OLD_BLOCK.encode("utf-8") * 2 + TAIL)

    def test_unbalanced_and_inline_pre_rename_markers_refuse_without_writes(self):
        self.assertRefused(HEAD + OLD_BEGIN.encode() + b"\n## never closed\n")
        self.assertRefused(HEAD + b"see " + OLD_BEGIN.encode() + b" inline for details\n")

    def assertRefused(self, raw):
        self.write_agents(raw)
        code, report, output = self.invoke()
        self.assertEqual(code, 4, output)
        self.assertEqual(report["managed_block"]["state"], "malformed")
        self.assertFalse(report["ok"])
        code, report, output = self.invoke(["--apply"])
        self.assertEqual(code, 4, output)
        self.assertEqual(report["error"]["stage"], "managed_block")
        self.assertNotIn("apply", report, "a refused file may never be rewritten")
        self.assertEqual(self.agents.read_bytes(), raw, "refusal must preserve every byte")
        self.assertEqual(self.backups(), [])
        self.assertFalse(self.state.exists(), "refusal must not create shared state")

    def current_block(self):
        code, report, output = self.invoke(["--apply"], env=self.environment(
            CODEX_HOME=str(self.temp / "other home")))
        self.assertEqual(code, 0, output)
        text = (self.temp / "other home" / "AGENTS.md").read_text(encoding="utf-8")
        return text[text.index(BEGIN):text.index(END) + len(END)] + "\n"


class EnvironmentNameCompatibilityTest(MigrationCase):
    """Canonical env names win, legacy names still work, and conflicts refuse with no writes."""

    def executable(self, name):
        path = self.temp / "bin" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        os.chmod(str(path), 0o755)
        return path

    def test_legacy_state_dir_is_still_honored(self):
        legacy = self.temp / "legacy pool"
        code, report, output = self.invoke(env=self.environment(**{STATE_ENV: None,
                                                                   LEGACY_STATE_ENV: str(legacy)}))
        self.assertEqual(code, 0, output)
        self.assertEqual(report["state_dir"], os.path.normpath(str(legacy)))

    def test_canonical_name_wins_over_an_equal_legacy_pair(self):
        shared = self.temp / "one shared pool"
        code, report, output = self.invoke(env=self.environment(
            **{STATE_ENV: str(shared), LEGACY_STATE_ENV: str(shared)}))
        self.assertEqual(code, 0, output)
        self.assertEqual(report["state_dir"], os.path.normpath(str(shared)))

    def test_conflicting_state_dirs_refuse_without_writes(self):
        code, report, output = self.invoke(
            env=self.environment(**{STATE_ENV: str(self.temp / "new pool"),
                                     LEGACY_STATE_ENV: str(self.temp / "old pool")}))
        self.assertEqual(code, 2, output)
        self.assertEqual(report["error"]["stage"], "environment")
        self.assertIn(STATE_ENV, report["error"]["detail"])
        self.assertIn(LEGACY_STATE_ENV, report["error"]["detail"])
        self.assertFalse(self.home.exists())
        self.assertFalse(self.state.exists())
        apply_code, apply_report, apply_output = self.invoke(
            ["--apply"], env=self.environment(**{STATE_ENV: str(self.temp / "new pool"),
                                                 LEGACY_STATE_ENV: str(self.temp / "old pool")}))
        self.assertEqual(apply_code, 2, apply_output)
        self.assertEqual(apply_report["error"]["stage"], "environment")
        self.assertNotIn("apply", apply_report, "a conflict must refuse before any backup or write")
        self.assertFalse(self.home.exists())

    def test_legacy_codex_bin_override_is_reported_by_name(self):
        candidate = self.executable("codex-legacy")
        code, report, output = self.invoke(env=self.environment(**{CODEX_ENV: None,
                                                                   LEGACY_CODEX_ENV: str(candidate)}))
        self.assertEqual(code, 0, output)
        cli = report["environment"]["codex_cli"]
        self.assertEqual(cli["source"], LEGACY_CODEX_ENV)
        self.assertEqual(cli["path"], str(candidate))

    def test_default_state_dir_stays_the_shared_legacy_root(self):
        code, report, output = self.invoke(env=self.environment(**{STATE_ENV: None}))
        self.assertEqual(code, 0, output)
        self.assertEqual(report["state_dir"], os.path.normpath(
            os.path.join(str(self.fake_home), ".local-implement")),
                         "one shared pool: storage keeps the pre-rename path on purpose")


class LauncherEnvironmentTest(unittest.TestCase):
    """The launcher resolves the same pair, with the explicit CLI argument taking precedence."""

    def setUp(self):
        import local_worker as lw  # noqa: PLC0415 - the engine lives beside the tests
        self.lw = lw
        self.saved = {key: os.environ.get(key) for key in
                      (STATE_ENV, LEGACY_STATE_ENV, CODEX_ENV, LEGACY_CODEX_ENV)}
        self.addCleanup(self.restore)
        for key in self.saved:
            os.environ.pop(key, None)

    def restore(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_explicit_argument_beats_both_environment_names(self):
        os.environ[STATE_ENV] = str(Path(tempfile.gettempdir()) / "canonical pool")
        os.environ[LEGACY_STATE_ENV] = str(Path(tempfile.gettempdir()) / "legacy pool")
        explicit = str(Path(tempfile.gettempdir()) / "explicit pool")
        self.assertEqual(self.lw.resolve_state_dir(explicit), Path(explicit).absolute())
        self.assertEqual(self.lw.resolve_codex_bin(explicit),
                         str(Path(explicit).expanduser()))

    def test_legacy_names_are_used_when_the_canonical_names_are_unset(self):
        legacy = Path(tempfile.gettempdir()) / "legacy pool"
        os.environ[LEGACY_STATE_ENV] = str(legacy)
        self.assertEqual(self.lw.resolve_state_dir(), legacy.absolute())

    def test_equal_pairs_are_accepted(self):
        shared = Path(tempfile.gettempdir()) / "one shared pool"
        os.environ[STATE_ENV] = str(shared)
        os.environ[LEGACY_STATE_ENV] = str(shared)
        self.assertEqual(self.lw.resolve_state_dir(), shared.absolute())

    def test_conflicting_pairs_refuse_clearly(self):
        os.environ[STATE_ENV] = str(Path(tempfile.gettempdir()) / "new pool")
        os.environ[LEGACY_STATE_ENV] = str(Path(tempfile.gettempdir()) / "old pool")
        with self.assertRaises(self.lw.PreflightError) as state:
            self.lw.resolve_state_dir()
        self.assertIn(STATE_ENV, str(state.exception))
        self.assertIn(LEGACY_STATE_ENV, str(state.exception))
        os.environ[CODEX_ENV] = str(Path(tempfile.gettempdir()) / "new codex")
        os.environ[LEGACY_CODEX_ENV] = str(Path(tempfile.gettempdir()) / "old codex")
        with self.assertRaises(self.lw.PreflightError) as codex:
            self.lw.resolve_codex_bin()
        self.assertIn(CODEX_ENV, str(codex.exception))
        self.assertIn(LEGACY_CODEX_ENV, str(codex.exception))

    def test_default_state_dir_is_the_shared_legacy_root(self):
        self.assertEqual(str(self.lw.resolve_state_dir()),
                         os.path.expanduser(os.path.join("~", ".local-implement")))


if __name__ == "__main__":
    unittest.main()
