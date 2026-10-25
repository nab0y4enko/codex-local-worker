"""The user-owned permissions list, its Seatbelt deny profile, and the ``permissions`` CLI.

Everything but the two Darwin tests is offline text work on disposable directories. The Darwin
tests drive the real ``/usr/bin/sandbox-exec`` and skip when it is missing, or when the test
process is itself inside a Seatbelt sandbox (a nested ``sandbox_apply`` exits 71).
"""
import contextlib
import dataclasses
import io
import json
import os
import re
import subprocess
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support as sp  # noqa: E402
import permissions  # noqa: E402

lw = sp.lw
CODEX = Path("/opt/homebrew/bin/codex")
NESTED_SANDBOX = 71


def real(path):
    return os.path.realpath(str(path))


class PermissionsCase(unittest.TestCase):
    def setUp(self):
        self.root = sp.tmpdir()
        self.addCleanup(sp.cleanup, self.root)
        self.state = self.root / "state"
        self.repo = sp.make_repo(self.root)
        self.perms = permissions.load_permissions(self.state)

    @property
    def file(self):
        return self.state / permissions.PERMISSIONS_FILE

    def write_file(self, **overrides):
        """A valid permissions file with ``overrides`` applied; ``None`` deletes a key."""
        payload = {"schema_version": 1, "mode": "full_host", "deny_read": ["~/.ssh"],
                   "protect_primary_checkout": True, "writable_roots": [],
                   "network": {"allowed_domains": []}}
        payload.update(overrides)
        self.state.mkdir(parents=True, exist_ok=True)
        self.file.write_text(json.dumps({k: v for k, v in payload.items() if v is not None}),
                             encoding="utf-8")

    def conflicts(self, perms=None, **overrides):
        arguments = dict(repo=self.repo, worktree_root=self.state / "worktrees",
                         state_dir=self.state, codex_bin=CODEX)
        arguments.update(overrides)
        return permissions.check_conflicts(perms or self.perms, **arguments)


class DefaultsAndFile(PermissionsCase):
    def test_default_file_is_created_with_full_host_and_deny_list(self):
        perms = permissions.load_permissions(self.state)
        self.assertEqual(perms.mode, "full_host")
        self.assertIn("~/.ssh", perms.deny_read)
        self.assertTrue((self.state / "permissions.json").is_file())

    def test_the_default_list_is_the_builders_list(self):
        self.assertEqual(self.perms.deny_read, list(permissions.DEFAULT_DENY_READ))
        for entry in ("~/.codex", "~/.ssh", "~/.omlx/settings.json", "~/.aws", "~/.config/gh",
                      "~/.config/gcloud", "~/.kube", "~/.docker/config.json", "~/.netrc",
                      "~/.npmrc", "~/.pypirc", "~/.gnupg",
                      "~/Library/Application Support/Google/Chrome",
                      "~/Library/Application Support/Firefox", "~/Library/Safari",
                      "~/Library/Group Containers/2BUA8C4S2C.com.1password"):
            self.assertIn(entry, permissions.DEFAULT_DENY_READ)
        self.assertEqual(len(permissions.DEFAULT_DENY_READ), 16)
        self.assertTrue(self.perms.protect_primary_checkout)
        self.assertEqual((self.perms.writable_roots, self.perms.allowed_domains), ([], []))

    def test_the_default_file_is_schema_versioned_json(self):
        payload = json.loads(self.file.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], permissions.SCHEMA_VERSION)
        self.assertEqual(payload["network"], {"allowed_domains": []})
        self.assertEqual(set(payload), {"schema_version", "mode", "deny_read",
                                        "protect_primary_checkout", "writable_roots", "network"})

    def test_an_existing_file_is_never_overwritten_by_default_creation(self):
        self.write_file(deny_read=["~/.only-this"], protect_primary_checkout=False)
        before = self.file.read_bytes()
        perms = permissions.load_permissions(self.state)
        self.assertEqual(perms.deny_read, ["~/.only-this"])
        self.assertFalse(perms.protect_primary_checkout)
        self.assertEqual(self.file.read_bytes(), before)

    def test_default_creation_loses_a_race_to_a_file_that_appears_first(self):
        """The file shows up between the existence check and the create: it is kept, not replaced."""
        self.file.unlink()
        mine = json.dumps({"schema_version": 1, "mode": "full_host", "deny_read": ["~/.mine"],
                           "protect_primary_checkout": True, "writable_roots": [],
                           "network": {"allowed_domains": []}})
        real_link = os.link

        def link_after_someone_else_wrote(source, destination, *args, **kwargs):
            Path(destination).write_text(mine, encoding="utf-8")
            return real_link(source, destination, *args, **kwargs)

        with mock.patch.object(permissions.os, "link", link_after_someone_else_wrote):
            perms = permissions.load_permissions(self.state)
        self.assertEqual(perms.deny_read, ["~/.mine"])
        self.assertEqual(self.file.read_text(encoding="utf-8"), mine)
        self.assertEqual(sorted(p.name for p in self.state.iterdir()), ["permissions.json"])

    def test_save_and_load_round_trip_atomically(self):
        edited = dataclasses.replace(self.perms, deny_read=["~/.ssh", str(self.root / "vault")],
                                     protect_primary_checkout=False)
        path = permissions.save_permissions(self.state, edited)
        self.assertEqual(path, self.file)
        self.assertEqual(permissions.load_permissions(self.state), edited)
        self.assertEqual(sorted(p.name for p in self.state.iterdir()), ["permissions.json"])

    def test_a_non_ascii_path_is_stored_readably_and_survives_the_round_trip(self):
        edited = dataclasses.replace(self.perms, deny_read=["~/Bibliothèque/Clés"])
        permissions.save_permissions(self.state, edited)
        self.assertIn("Bibliothèque/Clés", self.file.read_text(encoding="utf-8"))
        self.assertEqual(permissions.load_permissions(self.state), edited)

    def test_save_creates_a_missing_state_directory(self):
        fresh = self.root / "nested" / "state"
        path = permissions.save_permissions(fresh, permissions.default_permissions())
        self.assertTrue(path.is_file())

    def test_loading_returns_independent_lists(self):
        first = permissions.load_permissions(self.state)
        first.deny_read.append("~/.mutated")
        self.assertNotIn("~/.mutated", permissions.load_permissions(self.state).deny_read)


class RefusedFiles(PermissionsCase):
    def refused(self, expected):
        before = self.file.read_bytes()
        with self.assertRaises(permissions.PermissionsError) as caught:
            permissions.load_permissions(self.state)
        self.assertIn(expected, str(caught.exception))
        # Fail closed: a bad file is reported, never replaced by defaults.
        self.assertEqual(self.file.read_bytes(), before)
        return str(caught.exception)

    def test_an_unknown_top_level_key_is_refused_by_name(self):
        self.write_file(deny_write=["~/x"])
        self.refused("deny_write")

    def test_an_unknown_network_key_is_refused_by_name(self):
        self.write_file(network={"allowed_domains": [], "proxy": "x"})
        self.refused("proxy")

    def test_a_missing_key_is_refused_by_name_not_defaulted(self):
        self.write_file(deny_read=None)
        self.refused("deny_read")

    def test_sandboxed_mode_is_reserved(self):
        self.write_file(mode="sandboxed")
        self.refused("sandboxed mode is reserved")

    def test_any_other_mode_is_refused_too(self):
        self.write_file(mode="full-host")
        self.refused("sandboxed mode is reserved")

    def test_a_wrong_schema_version_is_refused(self):
        self.write_file(schema_version=2)
        self.refused("schema_version")

    def test_invalid_json_is_refused_naming_the_file(self):
        self.state.mkdir(parents=True, exist_ok=True)
        self.file.write_text("{not json", encoding="utf-8")
        self.refused(str(self.file))

    def test_a_non_object_is_refused(self):
        self.state.mkdir(parents=True, exist_ok=True)
        self.file.write_text("[]", encoding="utf-8")
        self.refused("object")

    def test_a_duplicate_key_is_refused_so_a_deny_list_cannot_be_shadowed(self):
        self.state.mkdir(parents=True, exist_ok=True)
        self.file.write_text('{"schema_version": 1, "mode": "full_host", "deny_read": ["~/.ssh"], '
                             '"deny_read": [], "protect_primary_checkout": true, '
                             '"writable_roots": [], "network": {"allowed_domains": []}}',
                             encoding="utf-8")
        self.refused("deny_read")

    def test_wrong_value_types_are_refused(self):
        cases = {"deny_read": ("~/.ssh", [1], [""], ["relative/path"], ["~/.ssh\n(allow default)"]),
                 "protect_primary_checkout": (1, "true", None),
                 "writable_roots": ("x", [3]),
                 "network": ([], {"allowed_domains": "a"}, {"allowed_domains": [1]})}
        for key, values in cases.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    payload = dict(schema_version=1, mode="full_host", deny_read=["~/.ssh"],
                                   protect_primary_checkout=True, writable_roots=[],
                                   network={"allowed_domains": []})
                    payload[key] = value
                    self.state.mkdir(parents=True, exist_ok=True)
                    self.file.write_text(json.dumps(payload), encoding="utf-8")
                    self.refused(key)

    def test_an_unreadable_file_is_refused(self):
        self.file.unlink()
        self.file.mkdir()
        with self.assertRaises(permissions.PermissionsError) as caught:
            permissions.load_permissions(self.state)
        self.assertIn(str(self.file), str(caught.exception))

    def test_saving_an_invalid_list_is_refused_and_keeps_the_file(self):
        before = self.file.read_bytes()
        for bad in (dataclasses.replace(self.perms, mode="sandboxed"),
                    dataclasses.replace(self.perms, deny_read=["relative"]),
                    dataclasses.replace(self.perms, protect_primary_checkout="yes")):
            with self.subTest(bad=bad), self.assertRaises(permissions.PermissionsError):
                permissions.save_permissions(self.state, bad)
        self.assertEqual(self.file.read_bytes(), before)


class Profile(PermissionsCase):
    def test_profile_allows_by_default_and_lists_only_denials(self):
        text = permissions.seatbelt_profile(self.perms, primary_repo=self.repo)
        self.assertTrue(text.startswith("(version 1)\n(allow default)"))
        self.assertIn(f'(subpath "{os.path.realpath(os.path.expanduser("~/.ssh"))}")', text)
        for literal in ("HEAD", "index", "packed-refs", "config"):
            self.assertIn(f'(literal "{os.path.realpath(self.repo)}/.git/{literal}")', text)
        self.assertLess(text.index(f'(deny file-write* (subpath "{os.path.realpath(self.repo)}"))'),
                        text.index(f'(allow file-write* (subpath "{os.path.realpath(self.repo)}/.git"))'))

    def test_every_statement_is_a_denial_except_the_two_defaults_the_design_names(self):
        text = permissions.seatbelt_profile(self.perms, primary_repo=self.repo)
        statements = text.splitlines()
        self.assertEqual(statements[:2], ["(version 1)", "(allow default)"])
        for line in statements[2:]:
            self.assertRegex(line, r"^\((deny file-read\*|deny file-write\*|allow file-write\*) ")
        allows = [line for line in statements if line.startswith("(allow ")]
        self.assertEqual(allows, ["(allow default)",
                                  f'(allow file-write* (subpath "{real(self.repo)}/.git"))'])
        self.assertTrue(text.endswith("\n"))

    def test_the_documented_order_is_read_denials_then_the_primary_checkout_rules(self):
        text = permissions.seatbelt_profile(self.perms, primary_repo=self.repo)
        repo = real(self.repo)
        positions = [text.index("(deny file-read*"),
                     text.index(f'(deny file-write* (subpath "{repo}"))'),
                     text.index(f'(allow file-write* (subpath "{repo}/.git"))'),
                     text.index(f'(deny file-write* (literal "{repo}/.git/HEAD")')]
        self.assertEqual(positions, sorted(positions))
        self.assertIn(f'(literal "{repo}/.git/index")', text)
        self.assertIn(f'(subpath "{repo}/.git/refs/heads"))', text)

    def test_every_default_entry_is_listed_by_its_real_path(self):
        text = permissions.seatbelt_profile(self.perms, primary_repo=self.repo)
        for entry in permissions.DEFAULT_DENY_READ:
            with self.subTest(entry=entry):
                self.assertIn(f'(subpath "{real(os.path.expanduser(entry))}")', text)

    def test_an_empty_deny_list_writes_no_read_rule_at_all(self):
        """``(deny file-read*)`` with no filter would hide the whole disk."""
        text = permissions.seatbelt_profile(dataclasses.replace(self.perms, deny_read=[]),
                                            primary_repo=self.repo)
        self.assertNotIn("file-read", text)

    def test_protection_off_writes_no_write_rule(self):
        text = permissions.seatbelt_profile(
            dataclasses.replace(self.perms, protect_primary_checkout=False), primary_repo=self.repo)
        self.assertNotIn("file-write", text)
        self.assertIn("file-read", text)

    def test_paths_are_real_paths_and_listed_once(self):
        target = self.root / "vault"
        target.mkdir()
        (self.root / "link").symlink_to(target)
        perms = dataclasses.replace(self.perms, deny_read=[str(self.root / "link"), str(target),
                                                           str(self.root / "link")])
        text = permissions.seatbelt_profile(perms, primary_repo=self.repo)
        self.assertEqual(text.count(f'(subpath "{real(target)}")'), 1)
        self.assertNotIn(str(self.root / "link"), text.replace(real(target), ""))

    def test_a_tilde_entry_is_expanded_from_the_current_home(self):
        with mock.patch.dict(os.environ, {"HOME": str(self.root)}):
            text = permissions.seatbelt_profile(
                dataclasses.replace(self.perms, deny_read=["~/.vault"]), primary_repo=self.repo)
        self.assertIn(f'(subpath "{real(self.root / ".vault")}")', text)
        self.assertNotIn("~", text)

    def test_profile_escapes_awkward_paths(self):
        parent = self.root / 'with space "quote" ü'
        parent.mkdir()
        repo = sp.make_repo(sp.tmpdir_under(parent))
        text = permissions.seatbelt_profile(self.perms, primary_repo=repo)
        self.assertIn('\\"quote\\"', text)
        escaped = real(repo).replace("\\", "\\\\").replace('"', '\\"')
        self.assertIn(f'(allow file-write* (subpath "{escaped}/.git"))', text)
        self.assertIn("ü", text)

    def test_a_backslash_is_escaped(self):
        perms = dataclasses.replace(self.perms, deny_read=[str(self.root / "back\\slash")])
        text = permissions.seatbelt_profile(perms, primary_repo=self.repo)
        self.assertIn("back\\\\slash", text)

    def test_a_control_character_in_a_path_is_refused(self):
        for bad in ("/tmp/a\nb", "/tmp/a\x00b", "/tmp/a\tb"):
            with self.subTest(bad=bad), self.assertRaises(permissions.PermissionsError):
                permissions.seatbelt_profile(dataclasses.replace(self.perms, deny_read=[bad]),
                                             primary_repo=self.repo)

    def test_a_mode_other_than_full_host_has_no_profile(self):
        with self.assertRaises(permissions.PermissionsError) as caught:
            permissions.seatbelt_profile(dataclasses.replace(self.perms, mode="sandboxed"),
                                         primary_repo=self.repo)
        self.assertIn("sandboxed mode is reserved", str(caught.exception))


class Conflicts(PermissionsCase):
    def test_conflicting_deny_entry_is_refused(self):
        self.perms.deny_read.append(str(self.repo.parent))
        problems = permissions.check_conflicts(self.perms, repo=self.repo, worktree_root=self.state / "worktrees",
                                               state_dir=self.state, codex_bin=Path("/opt/homebrew/bin/codex"))
        self.assertTrue(any(str(self.repo.parent) in problem for problem in problems))

    def test_the_default_list_conflicts_with_nothing(self):
        self.assertEqual(self.conflicts(), [])

    def test_an_entry_equal_to_each_protected_path_is_a_conflict_naming_it(self):
        cases = {"repo": self.repo, "worktree root": self.state / "worktrees",
                 "state": self.state, "Codex": CODEX}
        for label, path in cases.items():
            with self.subTest(label=label):
                problems = self.conflicts(dataclasses.replace(self.perms, deny_read=[str(path)]))
                self.assertEqual(len(problems), 1 + (label == "state"))  # state also holds worktrees
                self.assertTrue(any(label in problem and str(path) in problem for problem in problems),
                                problems)

    def test_an_entry_above_a_protected_path_hides_it(self):
        for entry in (self.state.parent, Path("/"), CODEX.parent, Path("/opt")):
            with self.subTest(entry=str(entry)):
                self.assertTrue(self.conflicts(dataclasses.replace(self.perms, deny_read=[str(entry)])))

    def test_an_entry_inside_the_repo_or_beside_codex_is_not_a_conflict(self):
        inside = [str(self.repo / ".env"), str(self.repo / "secrets"), str(CODEX.parent / "other")]
        self.assertEqual(self.conflicts(dataclasses.replace(self.perms, deny_read=inside)), [])

    def test_a_tilde_entry_is_expanded_before_comparing(self):
        with mock.patch.dict(os.environ, {"HOME": str(self.root)}):
            problems = self.conflicts(dataclasses.replace(self.perms, deny_read=["~"]))
        self.assertTrue(any("repo" in problem and "'~'" in problem for problem in problems), problems)

    def test_the_real_path_is_compared_not_the_spelling(self):
        link = self.root / "alias"
        link.symlink_to(self.repo)
        problems = self.conflicts(dataclasses.replace(self.perms, deny_read=[str(link)]))
        self.assertTrue(any(str(link) in problem for problem in problems), problems)

    def test_a_codex_symlink_target_is_hidden_too(self):
        target = self.root / "cellar" / "codex"
        target.parent.mkdir()
        target.write_text("#!/bin/sh\n")
        launcher = self.root / "bin" / "codex"
        launcher.parent.mkdir()
        launcher.symlink_to(target)
        problems = self.conflicts(dataclasses.replace(self.perms, deny_read=[str(target.parent)]),
                                  codex_bin=launcher)
        self.assertTrue(any("Codex" in problem for problem in problems), problems)

    def test_worktrees_inside_a_protected_primary_checkout_could_not_be_written(self):
        inside = self.repo / "state"
        problems = self.conflicts(self.perms, state_dir=inside, worktree_root=inside / "worktrees")
        self.assertTrue(any("protect_primary_checkout" in problem for problem in problems), problems)
        unprotected = dataclasses.replace(self.perms, protect_primary_checkout=False)
        self.assertEqual(self.conflicts(unprotected, state_dir=inside, worktree_root=inside / "worktrees"), [])


class Wrapper(PermissionsCase):
    def test_wrap_argv_puts_the_profile_first_and_keeps_the_command_intact(self):
        profile = self.root / "p.sb"
        argv = permissions.wrap_argv(["/bin/echo", "a b", "--flag"], profile)
        self.assertEqual(argv, [permissions.SANDBOX_EXEC, "-f", str(profile), "/bin/echo", "a b", "--flag"])
        self.assertEqual(permissions.SANDBOX_EXEC, "/usr/bin/sandbox-exec")

    def test_wrap_argv_never_returns_a_bare_command(self):
        for bad_argv, profile in (([], self.root / "p.sb"), ([""], self.root / "p.sb"),
                                  (["/bin/echo"], Path("p.sb")), (["/bin/echo"], Path(""))):
            with self.subTest(argv=bad_argv, profile=str(profile)):
                with self.assertRaises(permissions.PermissionsError):
                    permissions.wrap_argv(bad_argv, profile)

    def test_wrapper_available_tracks_the_executable(self):
        missing = str(self.root / "no-sandbox-exec")
        with mock.patch.object(permissions, "SANDBOX_EXEC", missing):
            self.assertFalse(permissions.wrapper_available())
        stub = self.root / "stub"
        stub.write_text("#!/bin/sh\n")
        with mock.patch.object(permissions, "SANDBOX_EXEC", str(stub)):
            self.assertFalse(permissions.wrapper_available())  # not executable
            stub.chmod(0o755)
            self.assertEqual(permissions.wrapper_available(), sys.platform == "darwin")
        with mock.patch.object(permissions.sys, "platform", "linux"):
            self.assertFalse(permissions.wrapper_available())


class FindDenials(PermissionsCase):
    def setUp(self):
        super().setUp()
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.perms = dataclasses.replace(self.perms, deny_read=[str(self.vault), "~/.nothing-here"])

    def find(self, *lines, perms=None):
        return permissions.find_denials("\n".join(lines) + "\n", perms or self.perms, self.repo)

    def test_a_denied_read_is_reported_with_the_exact_path(self):
        secret = f"{real(self.vault)}/token.txt"
        self.assertEqual(self.find(f"cat: {secret}: Operation not permitted"),
                         [{"path": secret, "kind": "read"}])

    def test_the_denied_directory_itself_counts(self):
        self.assertEqual(self.find(f"ls: {self.vault}: Operation not permitted"),
                         [{"path": str(self.vault), "kind": "read"}])

    def test_a_write_to_the_primary_checkout_is_reported(self):
        target = f"{real(self.repo)}/src/new.py"
        self.assertEqual(self.find(f"touch: {target}: Operation not permitted"),
                         [{"path": target, "kind": "write"}])
        git_lock = f"{real(self.repo)}/.git/HEAD.lock"
        self.assertEqual(self.find(f"fatal: cannot lock '{git_lock}': Operation not permitted"),
                         [{"path": git_lock, "kind": "write"}])

    def test_both_the_spelled_and_the_real_form_match(self):
        self.assertEqual(real(self.repo) != str(self.repo), sys.platform == "darwin")
        spelled = f"{self.repo}/a.txt"
        self.assertEqual(self.find(f"touch: {spelled}: Operation not permitted"),
                         [{"path": spelled, "kind": "write"}])

    def test_a_tilde_entry_matches_its_expanded_and_real_forms(self):
        with mock.patch.dict(os.environ, {"HOME": str(self.root)}):
            expanded = f"{self.root}/.nothing-here/key"
            resolved = f"{real(self.root)}/.nothing-here/key"
            found = self.find(f"cat: {expanded}: Operation not permitted",
                              f"cat: {resolved}: Operation not permitted")
        self.assertEqual(found, [{"path": expanded, "kind": "read"},
                                 {"path": resolved, "kind": "read"}])

    def test_only_operation_not_permitted_lines_count(self):
        self.assertEqual(self.find(f"cat: {self.vault}/a: No such file or directory",
                                   f"reading {self.vault}/b",
                                   f"cat: /etc/hosts: Operation not permitted"), [])

    def test_a_longer_sibling_name_is_not_a_match(self):
        self.assertEqual(self.find(f"cat: {self.vault}-backup/a: Operation not permitted",
                                   f"cat: {self.vault}2: Operation not permitted",
                                   f"cat: {self.vault}.bak: Operation not permitted",
                                   f"cat: /mnt{self.vault}/a: Operation not permitted"), [])

    def test_duplicates_are_reported_once_in_order_of_appearance(self):
        first, second = f"{self.vault}/a", f"{real(self.repo)}/b"
        found = self.find(f"cat: {first}: Operation not permitted",
                          f"touch: {second}: Operation not permitted",
                          f"cat: {first}: Operation not permitted")
        self.assertEqual(found, [{"path": first, "kind": "read"}, {"path": second, "kind": "write"}])

    def test_two_paths_on_one_line_are_both_found(self):
        a, b = f"{self.vault}/a", f"{real(self.repo)}/b"
        found = self.find(f"cp: {a} {b}: Operation not permitted")
        self.assertEqual(found, [{"path": a, "kind": "read"}, {"path": b, "kind": "write"}])

    def test_writes_are_not_reported_when_the_checkout_is_not_protected(self):
        unprotected = dataclasses.replace(self.perms, protect_primary_checkout=False)
        self.assertEqual(self.find(f"touch: {self.repo}/x: Operation not permitted", perms=unprotected), [])

    def test_a_trailing_sentence_period_is_not_part_of_the_path(self):
        self.assertEqual(self.find(f"error: could not open {self.vault}/a. Operation not permitted"),
                         [{"path": f"{self.vault}/a", "kind": "read"}])

    def test_empty_text_has_no_denials(self):
        self.assertEqual(permissions.find_denials("", self.perms, self.repo), [])


@unittest.skipUnless(sys.platform == "darwin" and permissions.wrapper_available(), "Seatbelt only")
class RealWrapper(PermissionsCase):
    def sandboxed(self, profile, argv, **kwargs):
        env = dict(os.environ, **sp.GIT_ID)
        return subprocess.run(permissions.wrap_argv(argv, profile), capture_output=True, env=env, **kwargs)

    def write_profile(self, perms):
        profile = self.root / "p.sb"
        profile.write_text(permissions.seatbelt_profile(perms, primary_repo=self.repo), encoding="utf-8")
        probe = subprocess.run(permissions.wrap_argv(["/usr/bin/true"], profile), capture_output=True)
        if probe.returncode == NESTED_SANDBOX:
            self.skipTest("already inside a sandbox")
        self.assertEqual(probe.returncode, 0, probe.stderr)
        return profile

    def test_wrapper_hides_a_denied_path_and_protects_the_primary_checkout(self):
        secret = self.root / "secret.txt"; secret.write_text("s")
        perms = dataclasses.replace(self.perms, deny_read=[str(secret)])
        profile = self.root / "p.sb"; profile.write_text(permissions.seatbelt_profile(perms, primary_repo=self.repo))
        read = subprocess.run(permissions.wrap_argv(["/bin/cat", str(secret)], profile), capture_output=True)
        write = subprocess.run(permissions.wrap_argv(["/usr/bin/touch", str(self.repo / "x")], profile), capture_output=True)
        if read.returncode == 71: self.skipTest("already inside a sandbox")
        self.assertNotEqual(read.returncode, 0)
        self.assertNotEqual(write.returncode, 0)
        self.assertFalse((self.repo / "x").exists())

    def test_the_denial_text_is_what_find_denials_reads(self):
        secret = self.root / "secret.txt"
        secret.write_text("s")
        perms = dataclasses.replace(self.perms, deny_read=[str(secret)])
        profile = self.write_profile(perms)
        read = self.sandboxed(profile, ["/bin/cat", str(secret)])
        write = self.sandboxed(profile, ["/usr/bin/touch", str(self.repo / "x")])
        text = (read.stderr + write.stderr).decode("utf-8", "replace")
        # The tools print the path as they were given it, here the spelled (not the real) form.
        self.assertEqual(permissions.find_denials(text, perms, self.repo),
                         [{"path": str(secret), "kind": "read"},
                          {"path": f"{self.repo}/x", "kind": "write"}])

    def test_everything_else_stays_allowed(self):
        perms = dataclasses.replace(self.perms, deny_read=[str(self.root / "secret.txt")])
        profile = self.write_profile(perms)
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        for argv in (["/usr/bin/touch", str(elsewhere / "ok")], ["/bin/ls", str(self.repo)],
                     ["/bin/cat", str(self.repo / "add.py")]):
            with self.subTest(argv=argv):
                self.assertEqual(self.sandboxed(profile, argv).returncode, 0)
        self.assertTrue((elsewhere / "ok").exists())

    def test_a_linked_worktree_commits_while_the_primary_checkout_is_protected(self):
        worktree = self.root / "wt"
        sp.git(self.repo, "worktree", "add", "-q", "--detach", str(worktree))
        profile = self.write_profile(dataclasses.replace(self.perms, deny_read=[]))
        (worktree / "new.txt").write_text("n")
        git = ["/usr/bin/git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false"]
        add = self.sandboxed(profile, git + ["-C", str(worktree), "add", "new.txt"])
        commit = self.sandboxed(profile, git + ["-C", str(worktree), "commit", "-q", "-m", "from worktree"])
        self.assertEqual((add.returncode, commit.returncode), (0, 0), (add.stderr, commit.stderr))
        switch = self.sandboxed(profile, git + ["-C", str(self.repo), "checkout", "-q", "-b", "stolen"])
        branch = self.sandboxed(profile, git + ["-C", str(self.repo), "branch", "stolen-too"])
        head = self.sandboxed(profile, ["/bin/sh", "-c", 'echo "ref: refs/heads/stolen" > "$1"', "sh",
                                        str(self.repo / ".git" / "HEAD")])
        edit = self.sandboxed(profile, ["/usr/bin/touch", str(self.repo / "add.py")])
        for name, outcome in (("checkout -b", switch), ("branch", branch), ("HEAD", head), ("edit", edit)):
            with self.subTest(name=name):
                self.assertNotEqual(outcome.returncode, 0)
        # The two direct writes are refused by the profile itself, not by something else.
        self.assertIn(b"Operation not permitted", head.stderr)
        self.assertIn(b"Operation not permitted", edit.stderr)
        self.assertEqual(sp.git(self.repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(), b"main")
        self.assertEqual(sp.git(self.repo, "branch", "--list", "stolen*").stdout.strip(), b"")

    def test_an_awkward_path_is_escaped_so_seatbelt_parses_and_still_denies(self):
        parent = self.root / 'with space "quote" ü'
        parent.mkdir()
        self.repo = sp.make_repo(parent)
        secret = parent / "sec ret.txt"
        secret.write_text("s")
        profile = self.write_profile(dataclasses.replace(self.perms, deny_read=[str(secret)]))
        touch = self.sandboxed(profile, ["/usr/bin/touch", str(self.repo / "x")])
        cat = self.sandboxed(profile, ["/bin/cat", str(secret)])
        locked = self.sandboxed(profile, ["/bin/sh", "-c", 'echo x > "$1"', "sh", str(self.repo / ".git" / "config")])
        free = self.sandboxed(profile, ["/bin/sh", "-c", 'echo x > "$1"', "sh", str(self.repo / ".git" / "scratch")])
        for name, outcome in (("touch", touch), ("cat", cat), ("config", locked)):
            with self.subTest(name=name):
                self.assertNotEqual(outcome.returncode, 0)
                self.assertIn(b"Operation not permitted", outcome.stderr)
        self.assertEqual(free.returncode, 0, free.stderr)  # the .git re-opening is escaped too


class Cli(PermissionsCase):
    def run_cli(self, *argv, state=True):
        args = ["permissions", *argv]
        if state:
            args += ["--state-dir", str(self.state)]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = lw.main(args)
        return code, json.loads(out.getvalue())

    def log_lines(self):
        path = self.state / "permissions.log"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    def test_show_prints_the_effective_list_and_the_wrapper_state(self):
        code, shown = self.run_cli("show")
        self.assertEqual(code, 0)
        self.assertEqual(shown["mode"], "full_host")
        self.assertEqual(shown["deny_read"], list(permissions.DEFAULT_DENY_READ))
        self.assertIs(shown["wrapper_available"], permissions.wrapper_available())
        self.assertTrue(shown["protect_primary_checkout"])
        self.assertEqual(self.log_lines(), [])

    def test_show_creates_the_default_file_when_missing(self):
        self.file.unlink()
        self.run_cli("show")
        self.assertTrue(self.file.is_file())

    def test_the_state_dir_comes_from_the_environment_like_every_other_command(self):
        with mock.patch.dict(os.environ, {lw.STATE_ENV: str(self.state)}):
            code, shown = self.run_cli("show", state=False)
        self.assertEqual((code, shown["mode"]), (0, "full_host"))

    def test_deny_read_adds_the_path_prints_the_file_and_logs_the_change(self):
        target = self.root / "vault"
        code, shown = self.run_cli("deny-read", str(target))
        self.assertEqual(code, 0)
        self.assertIn(str(target), shown["deny_read"])
        self.assertIn(str(target), permissions.load_permissions(self.state).deny_read)
        (entry,) = self.log_lines()
        self.assertEqual((entry["action"], entry["path"]), ("deny-read", str(target)))
        self.assertEqual(set(entry), {"at", "action", "path"})
        datetime.strptime(entry["at"], "%Y-%m-%dT%H:%M:%SZ")

    def test_deny_read_twice_changes_and_logs_once(self):
        target = str(self.root / "vault")
        self.run_cli("deny-read", target)
        self.run_cli("deny-read", target + "/")
        self.assertEqual(permissions.load_permissions(self.state).deny_read.count(target), 1)
        self.assertEqual(len(self.log_lines()), 1)

    def test_a_relative_path_is_stored_absolute(self):
        with contextlib.ExitStack() as stack:
            stack.callback(os.chdir, os.getcwd())
            os.chdir(str(self.root))
            expected = os.path.join(os.getcwd(), "vault", "key.pem")
            self.run_cli("deny-read", "vault/key.pem")
        self.assertIn(expected, permissions.load_permissions(self.state).deny_read)

    def test_a_tilde_path_is_stored_as_given(self):
        self.run_cli("deny-read", "~/.vault/")
        self.assertIn("~/.vault", permissions.load_permissions(self.state).deny_read)

    def test_deny_read_refuses_what_could_never_be_a_path(self):
        for bad in ("", "  ", "/tmp/a\nb", "~nobody-such-user-xyz/x"):
            with self.subTest(bad=bad):
                code, refusal = self.run_cli("deny-read", bad)
                self.assertEqual(code, 2)
                self.assertEqual(refusal["status"], "preflight_failed")
        self.assertEqual(self.log_lines(), [])
        self.assertEqual(permissions.load_permissions(self.state).deny_read,
                         list(permissions.DEFAULT_DENY_READ))

    def test_allow_read_removes_a_default_entry_given_either_spelling(self):
        for spelling in ("~/.ssh", os.path.expanduser("~/.ssh")):
            with self.subTest(spelling=spelling):
                permissions.save_permissions(self.state, permissions.default_permissions())
                code, shown = self.run_cli("allow-read", spelling)
                self.assertEqual(code, 0)
                self.assertNotIn("~/.ssh", shown["deny_read"])
                self.assertNotIn("~/.ssh", permissions.load_permissions(self.state).deny_read)
                self.assertIn("~/.aws", shown["deny_read"])
        self.assertEqual([item["action"] for item in self.log_lines()], ["allow-read"] * 2)

    def test_allow_read_logs_the_path_as_it_was_given(self):
        self.run_cli("allow-read", "~/.kube")
        self.run_cli("allow-read", os.path.expanduser("~/.aws"))
        self.assertEqual([(item["action"], item["path"]) for item in self.log_lines()],
                         [("allow-read", "~/.kube"), ("allow-read", os.path.expanduser("~/.aws"))])

    def test_allow_read_of_an_unlisted_path_is_refused_and_not_logged(self):
        before = self.file.read_bytes()
        code, refusal = self.run_cli("allow-read", str(self.root / "never-listed"))
        self.assertEqual(code, 2)
        self.assertIn("not in the deny_read list", refusal["failure_reason"])
        self.assertEqual(self.log_lines(), [])
        self.assertEqual(self.file.read_bytes(), before)

    def test_allow_read_of_a_path_under_an_entry_names_the_entry_to_lift(self):
        code, refusal = self.run_cli("allow-read", os.path.expanduser("~/.ssh/id_ed25519"))
        self.assertEqual(code, 2)
        self.assertIn("~/.ssh", refusal["failure_reason"])
        self.assertIn("allow-read", refusal["failure_reason"])
        self.assertIn("~/.ssh", permissions.load_permissions(self.state).deny_read)

    def test_a_malformed_file_is_a_refusal_not_a_silent_default(self):
        self.file.write_text("{broken", encoding="utf-8")
        for command in (["show"], ["deny-read", str(self.root / "x")], ["allow-read", "~/.ssh"]):
            with self.subTest(command=command):
                code, refusal = self.run_cli(*command)
                self.assertEqual((code, refusal["status"]), (2, "preflight_failed"))
                self.assertIn(str(self.file), refusal["failure_reason"])
        self.assertEqual(self.file.read_text(encoding="utf-8"), "{broken")
        self.assertEqual(self.log_lines(), [])

    def test_a_subcommand_is_required(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            lw.main(["permissions", "--state-dir", str(self.state)])

    def test_the_log_is_append_only_json_lines(self):
        self.run_cli("deny-read", str(self.root / "a"))
        self.run_cli("allow-read", str(self.root / "a"))
        self.run_cli("deny-read", str(self.root / "b"))
        lines = (self.state / "permissions.log").read_text(encoding="utf-8").splitlines()
        self.assertEqual([json.loads(line)["action"] for line in lines],
                         ["deny-read", "allow-read", "deny-read"])
        for line in lines:
            self.assertRegex(json.loads(line)["at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


if __name__ == "__main__":
    unittest.main()
