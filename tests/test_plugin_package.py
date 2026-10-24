"""Native plugin package: manifests, portable resolution, relocated copy, packaged launcher.

Everything here is offline and deterministic: JSON manifests, path resolution on a copied
package whose path contains spaces, and one `--help` invocation of the packaged launcher.
No model inference, no server, no simulator, no installation.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_ID = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
PLUGIN_NAME = "local-qwen"
PLUGIN_VERSION = "0.1.0"
MARKETPLACE_NAME = "local-qwen-tools"
ONBOARDING = "./skills/setup/SKILL.md"

# Closed portable manifest: anything else at top level is a schema violation.
MANIFEST_FIELDS = {"$schema", "name", "version", "description", "author", "homepage",
                   "repository", "license", "keywords", "extensions"}
NAME_PATTERN = re.compile(r"^(?!.*(?:--|\.\.))[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")

LAUNCHER_FILES = ["bin/local-implement", "src/local_worker.py", "src/run_policy.py",
                  "src/worker_pool.py", "src/native_readiness.py", "src/worker_observability.py",
                  "src/apple_verification.py", "examples/task.json"]
SKILL_NAMES = {"local-implement", "setup", "doctor"}
PERSONAL = re.compile(r"oleksii|/Users/", re.IGNORECASE)
ACTIVE_INSTRUCTIONS = ["plugin.json", ".agents/plugins/marketplace.json", "README.md",
                       "skills/local-implement/SKILL.md",
                       "skills/local-implement/references/global-policy.md",
                       "skills/setup/SKILL.md", "skills/doctor/SKILL.md"]
NON_MCP_OR_HOOKS_NAMES = {"mcp.json", ".mcp.json", "hooks.json", "hooks.yaml", "plugin.lock.json"}
POLICY_ANCHORS = ["Selected planner, local Qwen implementation", "danger-full-access",
                  "--ignore-rules", "sandboxed: false", "4 LOCAL Qwen workers",
                  "Local-worker exception", "Global completion rule", "exec_command"]


def tree(base):
    """Inventory a directory by path, size and mtime: enough to prove nothing was written."""
    entries = {}
    for path in sorted(Path(base).rglob("*")):
        info = os.lstat(str(path))
        entries[str(path.relative_to(base))] = (info.st_size, info.st_mtime_ns)
    return entries


def load_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def frontmatter(text):
    """Parse the leading `---` block of a SKILL.md into a flat key/value dict."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise AssertionError("skill has no opening frontmatter fence")
    fields = {}
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            if index == 1:
                raise AssertionError("empty frontmatter block")
            return fields, index
        match = re.match(r"^([A-Za-z][A-Za-z0-9_-]*):[ \t]*(.*)$", line)
        if not match:
            raise AssertionError("frontmatter line is not `key: value`: " + line)
        fields[match.group(1)] = match.group(2).strip()
    raise AssertionError("frontmatter block is never closed")


def iter_keys(node):
    """Yield every mapping key anywhere in a decoded JSON document."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield str(key)
            for nested in iter_keys(value):
                yield nested
    elif isinstance(node, list):
        for value in node:
            for nested in iter_keys(value):
                yield nested


class PackageBase(unittest.TestCase):
    """Shared resolution helpers; `root` is the package under test."""

    root = None

    def resolve(self, relative, base=None):
        """Resolve a package-relative reference and prove it stays inside the package root."""
        base = Path(base or self.root)
        raw = str(relative)
        self.assertFalse(os.path.isabs(raw), "package reference must be relative: " + raw)
        target = (base / raw).resolve()
        root_real = Path(base).resolve()
        package = Path(self.root).resolve()
        self.assertEqual(str(package), os.path.commonpath([str(package), str(target)]),
                         "reference resolves outside the package: " + raw)
        return target

    def assertInsideRoot(self, path):
        root_real = Path(self.root).resolve()
        self.assertTrue(Path(path).resolve().is_relative_to(root_real)
                        if hasattr(Path, "is_relative_to")
                        else str(Path(path).resolve()).startswith(str(root_real) + os.sep),
                        "path resolves outside the package: " + str(path))

    def manifest(self):
        return load_json(Path(self.root) / "plugin.json")


class CopiedPackageTest(PackageBase):
    """The package as an installed copy: relocated, spaces in path, nothing inferred."""

    @classmethod
    def setUpClass(cls):
        cls.temp = Path(tempfile.mkdtemp(prefix="Local Qwen Plugin "))
        cls.root = cls.temp / "installs with spaces" / "local-qwen"
        cls.root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(str(ROOT), str(cls.root), symlinks=False,
                        ignore=shutil.ignore_patterns(".git", "evidence", "__pycache__",
                                                      ".pytest_cache"))
        cls.root = Path(os.path.realpath(cls.root))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.temp, ignore_errors=True)

    def test_copied_package_is_relocated_and_holds_spaces(self):
        self.assertNotEqual(Path(self.root).resolve(), Path(ROOT).resolve())
        self.assertIn(" ", str(self.root))
        self.assertTrue((Path(self.root) / "plugin.json").is_file())
        self.assertFalse((Path(self.root) / ".git").exists(), "a copy must not carry VCS state")
        for path in Path(self.root).rglob("*"):
            if path.is_symlink():
                self.assertInsideRoot(path)

    def test_manifest_matches_closed_portable_schema(self):
        manifest = self.manifest()
        self.assertEqual(manifest.get("$schema"), SCHEMA_ID)
        self.assertEqual(manifest.get("name"), PLUGIN_NAME)
        self.assertTrue(NAME_PATTERN.match(manifest["name"]), "plugin name is not a valid token")
        self.assertEqual(manifest.get("version"), PLUGIN_VERSION)
        self.assertGreaterEqual(len(manifest.get("description", "")), 40)
        self.assertIsInstance(manifest["description"], str)
        self.assertIsInstance(manifest.get("keywords"), list)
        self.assertTrue(all(isinstance(word, str) and word for word in manifest["keywords"]))
        self.assertIsInstance(manifest.get("extensions"), dict)
        self.assertTrue(all(isinstance(value, dict)
                            for value in manifest["extensions"].values()),
                        "every extension namespace must be an object")
        self.assertEqual(set(manifest) - MANIFEST_FIELDS, set(),
                         "unknown top-level manifest field (the portable schema is closed)")
        for invented in ("author", "homepage", "repository", "license"):
            self.assertNotIn(invented, manifest, "must not invent " + invented)

    def test_ui_identity_and_onboarding_skill(self):
        manifest = self.manifest()
        # Official nesting: the client namespace holds `onboardingSkill`, and `interface` holds
        # only the UI fields. `com.openai.interface` is not a namespace and must not appear.
        self.assertNotIn("com.openai.interface", manifest["extensions"],
                         "the client namespace is com.openai, not com.openai.interface")
        client = manifest["extensions"].get("com.openai")
        self.assertIsInstance(client, dict, "extensions.com.openai must be an object")
        self.assertEqual(set(client), {"onboardingSkill", "interface"},
                         "onboardingSkill sits beside interface, never inside it")
        self.assertEqual(client.get("onboardingSkill"), ONBOARDING)
        onboarding = self.resolve(client["onboardingSkill"])
        self.assertTrue(onboarding.is_file(), "onboarding skill is missing: " + str(onboarding))
        self.assertEqual(onboarding.name, "SKILL.md")
        self.assertIn("name: setup", onboarding.read_text(encoding="utf-8"))
        interface = client.get("interface")
        self.assertIsInstance(interface, dict)
        self.assertEqual(set(interface), {"displayName", "shortDescription"})
        self.assertEqual(interface.get("displayName"), "Local Qwen")
        short = interface.get("shortDescription")
        self.assertTrue(short and len(short) <= 80 and short != interface["displayName"])
        self.assertNotIn("onboardingSkill", interface, "onboardingSkill is not an interface field")

    def test_marketplace_resolves_the_repository_root(self):
        marketplace = load_json(Path(self.root) / ".agents" / "plugins" / "marketplace.json")
        self.assertEqual(marketplace.get("name"), MARKETPLACE_NAME)
        self.assertEqual(marketplace["interface"]["displayName"], "Local Qwen Tools")
        entries = marketplace.get("plugins")
        self.assertIsInstance(entries, list)
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry.get("name"), PLUGIN_NAME)
        self.assertEqual(entry["source"].get("source"), "local")
        source = self.resolve(entry["source"]["path"])
        self.assertEqual(source, Path(self.root).resolve(),
                         "the repository root itself is the package source")
        self.assertEqual(load_json(source / "plugin.json")["name"], PLUGIN_NAME)
        self.assertEqual(entry["policy"], {"installation": "AVAILABLE",
                                          "authentication": "ON_INSTALL"})
        self.assertEqual(entry.get("category"), "Productivity")

    def test_no_hooks_or_mcp_autoloading(self):
        manifest = self.manifest()
        documents = {"plugin manifest": manifest,
                     "marketplace": load_json(Path(self.root) / ".agents" / "plugins" / "marketplace.json")}
        for label, document in documents.items():
            for key in iter_keys(document):
                lowered = key.lower()
                for token in ("mcp", "hook"):
                    self.assertNotIn(token, lowered,
                                     label + " must not declare MCP or hooks (" + key + ")")
        for path in Path(self.root).rglob("*"):
            if path.is_file():
                self.assertNotIn(path.name, NON_MCP_OR_HOOKS_NAMES,
                                 "MCP or hooks component must not ship: " + str(path))
            else:
                self.assertNotEqual(path.name, "hooks", "hooks directory must not ship")
        self.assertFalse(any(Path(self.root).glob("com.*/**")),
                         "no client extension directory ships in the MVP")

    def test_launcher_package_is_included(self):
        for relative in LAUNCHER_FILES:
            shipped = self.resolve(relative)
            self.assertTrue(shipped.is_file(), "launcher file missing from package: " + relative)
        shim = Path(self.root) / "bin" / "local-implement"
        self.assertTrue(os.access(str(shim), os.X_OK), "launcher shim is not executable")
        self.assertIn("src/local_worker.py", shim.read_text(encoding="utf-8"))

    def test_all_three_skills_are_discovered(self):
        skills = Path(self.root) / "skills"
        discovered = sorted(child.name for child in skills.iterdir()
                            if child.is_dir() and (child / "SKILL.md").is_file())
        self.assertEqual(discovered, sorted(SKILL_NAMES))
        for child in skills.iterdir():
            if not child.is_dir():
                self.fail("unexpected skills/ entry that is not a skill directory: " + str(child))
            text = (child / "SKILL.md").read_text(encoding="utf-8")
            fields, _ = frontmatter(text)
            self.assertEqual(fields.get("name"), child.name)
            self.assertTrue(NAME_PATTERN.match(fields["name"]))
            description = fields.get("description", "")
            self.assertTrue(40 <= len(description) <= 1024, child.name)
            self.assertTrue(text.splitlines()[_].strip(), "instructions must follow frontmatter")
            self.assertLessEqual(set(fields), {"name", "description", "license", "metadata",
                                               "allowed-tools"})
        self.assertNotIn("mcp.json", [p.name for p in skills.rglob("*") if p.is_file()])

    def test_active_instructions_are_portable(self):
        for relative in ACTIVE_INSTRUCTIONS:
            text = (Path(self.root) / relative).read_text(encoding="utf-8")
            self.assertIsNone(PERSONAL.search(text),
                              "hardcoded personal path in active instructions: " + relative)
        for skill in (Path(self.root) / "skills").rglob("*.md"):
            text = skill.read_text(encoding="utf-8")
            self.assertIsNone(PERSONAL.search(text), "personal path in " + str(skill))
            self.assertNotIn("codex-local-first", text,
                             "doc must resolve the installed root, not a checkout: " + str(skill))
        for name in sorted(SKILL_NAMES):
            skills = Path(self.root) / "skills"
            text = (skills / name / "SKILL.md").read_text(encoding="utf-8")
            self.assertIn("<root>", text, "skill must document plugin-root resolution: " + name)

    def test_local_implement_skill_resolves_root_and_packaged_example(self):
        skill = (Path(self.root) / "skills" / "local-implement" / "SKILL.md")
        text = skill.read_text(encoding="utf-8")
        self.assertIn("<root>/skills/local-implement/SKILL.md", text)
        self.assertIn("<root>/bin/local-implement", text)
        self.assertIn("[`examples/task.json`](../../examples/task.json)", text)
        link = re.search(r"\]\((\.\./\.\./examples/task\.json)\)", text).group(1)
        example = self.resolve(link, skill.parent)
        self.assertTrue(example.is_file())
        contract = load_json(example)
        self.assertEqual(sorted(contract), ["acceptance_criteria", "allowed_paths", "base_ref",
                                            "goal", "max_attempts", "repo_path", "task_id",
                                            "test_commands", "timeout_seconds"])
        for reference in ("references/operating-policy.md", "references/apple-verification.md",
                          "references/global-policy.md", "../setup/SKILL.md"):
            target = self.resolve(reference, skill.parent)
            self.assertTrue(target.is_file(), "skill reference is missing: " + reference)

    def test_canonical_global_policy_is_portable_and_faithful(self):
        policy_path = self.resolve("skills/local-implement/references/global-policy.md")
        policy = policy_path.read_text(encoding="utf-8")
        for anchor in POLICY_ANCHORS:
            self.assertIn(anchor, policy, "policy anchor missing: " + anchor)
        self.assertIsNone(PERSONAL.search(policy))
        self.assertIn("<root>/bin/local-implement", policy)
        self.assertIn("<root>/skills/local-implement/SKILL.md", policy)
        self.assertIn("plugin.json", policy)
        self.assertIn("bundled local-implement skill", policy)
        legacy = (ROOT / "evidence" / "global-default" / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("/Users/oleksii.naboichenko/.codex/skills/local-implement/SKILL.md", legacy,
                      "the historical migration input must stay unchanged")
        moved = 0
        for line in legacy.splitlines():
            if not line.startswith("- "):
                continue
            if "/.codex/skills/local-implement/SKILL.md" in line:
                moved += 1
                continue
            self.assertIn(line, policy, "policy bullet lost: " + line[:60])
        self.assertEqual(moved, 1, "exactly the machine-specific skill path may change")

    def test_setup_and_doctor_document_the_same_interface(self):
        setup = (Path(self.root) / "skills" / "setup" / "SKILL.md").read_text(encoding="utf-8")
        doctor = (Path(self.root) / "skills" / "doctor" / "SKILL.md").read_text(encoding="utf-8")
        for phrase in ("python3 <root>/src/plugin_setup.py --check",
                       "python3 <root>/src/plugin_setup.py --apply",
                       "CODEX_HOME", "AGENTS.md", "global-policy.md", "idempotent",
                       "backup"):
            self.assertIn(phrase, setup, "setup must document: " + phrase)
        self.assertIn("python3 <root>/src/plugin_setup.py --check --preflight", doctor)
        self.assertIn("<root>/bin/local-implement preflight", doctor)
        for must_not in ("plugin_setup.py --apply", "install oMLX"):
            self.assertNotIn(must_not, doctor, "doctor must stay read-only: " + must_not)
        self.assertIn("no credentials", setup.lower())
        self.assertIn("never fall", setup.lower())
        script = Path(self.root) / "src" / "plugin_setup.py"
        self.assertTrue(script.is_file(), "the documented setup helper must ship: " + str(script))
        source = script.read_text(encoding="utf-8")
        # The stdlib-only guarantee is proven by running the helper under `-I -S` (see
        # `test_setup_helper_is_stdlib_only_when_run_in_isolation`), so no import-name allowlist is
        # kept here. Only the one real boundary is checked statically: the helper may name the
        # packaged launcher engine as a file, but must never import it.
        self.assertNotRegex(source, r"(?m)^[ \t]*(?:from|import)[ \t]+(?:src\.)?local_worker\b",
                            "setup must not import the launcher engine")
        for phrase in ("--check", "--apply", "--preflight", "CODEX_HOME",
                       "LOCAL_IMPLEMENT_STATE_DIR", "AGENTS.md"):
            self.assertIn(phrase, source, "helper must document " + phrase)
        self.assertIn("endpoint_passed", source)

    def test_setup_helper_is_stdlib_only_when_run_in_isolation(self):
        """Run the real packaged helper under `-I -S`: no site, no PYTHON env, nothing written."""
        script = Path(self.root) / "src" / "plugin_setup.py"
        home = Path(self.temp) / "isolated CODEX home"
        state = Path(self.temp) / "isolated shared state"
        working = Path(self.temp) / "unrelated cwd with spaces"
        for directory in (home, state, working):
            directory.mkdir(parents=True)
        for directory in (home, state):
            (directory / "sentinel.txt").write_text("must stay untouched", encoding="utf-8")
        before = (tree(home), tree(state))
        env = {key: value for key, value in os.environ.items()
               if key not in ("CODEX_HOME", "LOCAL_IMPLEMENT_STATE_DIR",
                              "LOCAL_IMPLEMENT_CODEX_BIN")}
        env.update({"CODEX_HOME": str(home), "LOCAL_IMPLEMENT_STATE_DIR": str(state)})
        result = subprocess.run([sys.executable, "-I", "-S", str(script), "--check"],
                                cwd=str(working), env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, universal_newlines=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout)
        report = json.loads(result.stdout)
        self.assertTrue(report["ok"])
        self.assertEqual(report["mode"], "check")
        self.assertEqual(report["exit_code"], 0)
        self.assertEqual(report["plugin"]["name"], PLUGIN_NAME)
        self.assertEqual(report["plugin"]["version"], PLUGIN_VERSION)
        self.assertEqual(Path(report["plugin"]["root"]).resolve(), Path(self.root).resolve())
        self.assertEqual(Path(report["codex_home"]).resolve(), home.resolve())
        self.assertEqual(Path(report["state_dir"]).resolve(), state.resolve())
        self.assertFalse(report["writes_performed"])
        engine = report["package"]["launcher_engine"]
        self.assertTrue(engine["exists"], "the packaged launcher engine must ship beside the shim")
        self.assertEqual(Path(engine["path"]).name, "local_worker.py")
        self.assertEqual((tree(home), tree(state)), before,
                         "a check must write nothing into CODEX_HOME or the state directory")
        self.assertEqual(report["agents_override"]["state"], "absent")
        self.assertEqual(report["preflight"]["status"], "not-requested")

    def test_packaged_launcher_runs_from_the_relocated_path(self):
        result = subprocess.run([str(Path(self.root) / "bin" / "local-implement"), "--help"],
                                cwd=str(Path(self.root)), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, universal_newlines=True, timeout=120,
                                env={key: value for key, value in os.environ.items()
                                     if key != "LOCAL_IMPLEMENT_STATE_DIR"})
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("usage: local-implement", result.stdout)
        for command in ("run", "status", "preflight"):
            self.assertIn(command, result.stdout)
        self.assertNotIn(str(Path(self.root)), result.stdout)


class ManifestParityTest(PackageBase):
    """The same assertions must hold for the package as it is developed, not only as a copy."""

    @classmethod
    def setUpClass(cls):
        cls.root = ROOT

    def test_source_package_matches_installed_shape(self):
        manifest = self.manifest()
        self.assertEqual(manifest["name"], PLUGIN_NAME)
        self.assertEqual(manifest["extensions"]["com.openai"]["onboardingSkill"], ONBOARDING)
        self.assertEqual(manifest["extensions"]["com.openai"]["interface"]["displayName"],
                         "Local Qwen")
        self.assertNotIn("com.openai.interface", manifest["extensions"])
        marketplace = load_json(ROOT / ".agents" / "plugins" / "marketplace.json")
        entry = marketplace["plugins"][0]
        self.assertEqual(self.resolve(entry["source"]["path"]), Path(ROOT).resolve())
        self.assertEqual(sorted(child.name for child in (ROOT / "skills").iterdir()
                                if child.is_dir()), sorted(SKILL_NAMES))


if __name__ == "__main__":
    unittest.main()
