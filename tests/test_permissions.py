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
import unicodedata
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

    def test_protection_off_leaves_the_primary_checkout_writable(self):
        text = permissions.seatbelt_profile(
            dataclasses.replace(self.perms, protect_primary_checkout=False), primary_repo=self.repo)
        self.assertNotIn(real(self.repo), text)
        self.assertIn("file-read", text)
        # What is hidden still cannot be changed or moved, whether or not the checkout is protected.
        (write_rule,) = [line for line in text.splitlines() if line.startswith("(deny file-write* (subpath")]
        self.assertIn(f'(subpath "{real(os.path.expanduser("~/.ssh"))}")', write_rule)

    def test_nothing_to_protect_writes_no_write_rule_at_all(self):
        text = permissions.seatbelt_profile(
            dataclasses.replace(self.perms, deny_read=[], protect_primary_checkout=False),
            primary_repo=self.repo)
        self.assertEqual(text, "(version 1)\n(allow default)\n")

    def test_paths_are_real_paths_and_listed_once(self):
        target = self.root / "vault"
        target.mkdir()
        (self.root / "link").symlink_to(target)
        perms = dataclasses.replace(self.perms, deny_read=[str(self.root / "link"), str(target),
                                                           str(self.root / "link")])
        text = permissions.seatbelt_profile(perms, primary_repo=self.repo)
        (read_rule,) = [line for line in text.splitlines() if line.startswith("(deny file-read*")]
        self.assertEqual(read_rule.count(f'(subpath "{real(target)}")'), 1)
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

    def rule_after(self, text, marker):
        """The statements that follow the first line starting with ``marker``."""
        lines = text.splitlines()
        return lines[[line.startswith(marker) for line in lines].index(True) + 1:]

    def test_a_hidden_path_cannot_be_changed_or_moved_and_neither_can_its_parents(self):
        vault = self.root / "outer" / "vault"
        vault.mkdir(parents=True)
        perms = dataclasses.replace(self.perms, deny_read=[str(vault)])
        text = permissions.seatbelt_profile(perms, primary_repo=self.repo)
        self.assertIn(f'(deny file-write* (subpath "{real(vault)}"))', text.splitlines())
        (literals,) = [line for line in text.splitlines() if "(literal " in line and "/.git/HEAD" not in line]
        for guarded in [real(vault), real(vault.parent), real(self.root), "/"]:
            self.assertIn(f'(literal "{guarded}")', literals)
        self.assertTrue(literals.startswith("(deny file-write* "))

    def test_the_primary_checkout_and_its_parents_cannot_be_moved(self):
        text = permissions.seatbelt_profile(self.perms, primary_repo=self.repo)
        repo = real(self.repo)
        (literals,) = [line for line in text.splitlines()
                       if line.startswith("(deny file-write* (literal") and "/.git/HEAD" not in line]
        for guarded in (repo, f"{repo}/.git", f"{repo}/.git/refs", real(self.repo.parent), "/"):
            self.assertIn(f'(literal "{guarded}")', literals)
        # Children of the parents stay writable: only the directories themselves are guarded.
        self.assertNotIn(f'(literal "{repo}/.git/objects")', text)
        self.assertNotIn(f'(subpath "{real(self.repo.parent)}")', text)

    def test_the_rename_rules_come_after_every_rule_they_must_outrank(self):
        text = permissions.seatbelt_profile(self.perms, primary_repo=self.repo)
        lines = text.splitlines()
        repo = real(self.repo)
        reopen = lines.index(f'(allow file-write* (subpath "{repo}/.git"))')
        guard = [i for i, line in enumerate(lines) if f'(literal "{repo}/.git")' in line]
        self.assertEqual(len(guard), 1)
        self.assertGreater(guard[0], reopen)  # the last match wins, so the .git re-opening must not outrank it
        for tail in lines[guard[0]:]:
            self.assertTrue(tail.startswith("(deny file-write* "), tail)

    def test_the_rename_protection_helper_guards_each_path_and_every_parent_once(self):
        a, b = self.root / "x" / "a", self.root / "x" / "y" / "b"
        (statement,) = permissions._rename_protection([real(a.parent) + "/a", real(b.parent) + "/b", real(a.parent) + "/a"])
        self.assertTrue(statement.startswith("(deny file-write* (literal "))
        self.assertTrue(statement.endswith(")"))
        expected = {real(a.parent) + "/a", real(b.parent) + "/b", real(a.parent), real(b.parent), real(self.root), "/"}
        expected |= set(permissions._ancestors(real(self.root)))
        self.assertEqual(sorted(re.findall(r'\(literal "((?:[^"\\]|\\.)*)"\)', statement)), sorted(expected))
        self.assertEqual(statement.count("(literal "), len(expected))
        self.assertEqual(permissions._rename_protection([]), [])

    def test_the_helper_walks_up_to_the_root_and_refuses_a_relative_path(self):
        self.assertEqual(permissions._ancestors("/a/b/c"), ["/a/b", "/a", "/"])
        self.assertEqual(permissions._ancestors("/a"), ["/"])
        self.assertEqual(permissions._ancestors("/"), [])
        with self.assertRaises(permissions.PermissionsError):
            permissions._ancestors("relative/path")

    def test_the_helper_escapes_and_treats_case_variants_as_one_path(self):
        (statement,) = permissions._rename_protection(['/tmp/a "b"/c', '/TMP/A "B"/C'])
        self.assertIn('(literal "/tmp/a \\"b\\"/c")', statement)
        self.assertEqual(statement.count('/c")'), 1)

    def test_a_case_variant_of_a_hidden_path_is_listed_once(self):
        perms = dataclasses.replace(self.perms, deny_read=["/opt/Vault", "/OPT/vault"])
        text = permissions.seatbelt_profile(perms, primary_repo=self.repo)
        (read_rule,) = [line for line in text.splitlines() if line.startswith("(deny file-read*")]
        self.assertEqual(read_rule.count("(subpath "), 1)

    def test_a_backslash_is_escaped(self):
        perms = dataclasses.replace(self.perms, deny_read=[str(self.root / "back\\slash")])
        text = permissions.seatbelt_profile(perms, primary_repo=self.repo)
        self.assertIn("back\\\\slash", text)

    def test_a_control_character_in_a_path_is_refused(self):
        for bad in ("/tmp/a\nb", "/tmp/a\x00b", "/tmp/a\tb"):
            with self.subTest(bad=bad), self.assertRaises(permissions.PermissionsError):
                permissions.seatbelt_profile(dataclasses.replace(self.perms, deny_read=[bad]),
                                             primary_repo=self.repo)

    def test_the_docstring_states_what_the_branch_protection_does_not_cover(self):
        doc = permissions.seatbelt_profile.__doc__
        self.assertIn("reftable", doc)
        self.assertIn("gitfile", doc)

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


class ConflictComparison(PermissionsCase):
    """Containment is by whole path component, and blind to case and Unicode normalisation."""

    def test_a_sibling_with_a_longer_name_is_not_hidden(self):
        sibling = str(self.repo) + "-old"
        self.assertEqual(self.conflicts(dataclasses.replace(self.perms, deny_read=[sibling])), [])
        self.assertTrue(self.conflicts(dataclasses.replace(self.perms, deny_read=[str(self.repo)])))

    def test_a_codex_home_does_not_hide_a_codex_install_with_a_longer_name(self):
        with mock.patch.dict(os.environ, {"HOME": str(self.root)}):
            cli = self.root / ".codex-cli" / "bin" / "codex"
            self.assertEqual(self.conflicts(dataclasses.replace(self.perms, deny_read=["~/.codex"]),
                                            codex_bin=cli), [])
            inside = self.root / ".codex" / "bin" / "codex"
            problems = self.conflicts(dataclasses.replace(self.perms, deny_read=["~/.codex"]),
                                      codex_bin=inside)
        self.assertTrue(any("Codex" in problem and "~/.codex" in problem for problem in problems), problems)

    def test_a_case_variant_entry_above_the_repo_is_refused(self):
        entry = str(self.repo.parent).upper()
        problems = self.conflicts(dataclasses.replace(self.perms, deny_read=[entry]))
        self.assertTrue(any(entry in problem and "repository" in problem for problem in problems), problems)

    def test_a_differently_normalised_entry_above_the_repo_is_refused(self):
        parent = self.root / unicodedata.normalize("NFC", "Caf\u00e9")
        parent.mkdir()
        repo = sp.make_repo(parent)
        entry = unicodedata.normalize("NFD", str(parent))
        self.assertNotEqual(entry, str(parent))
        problems = self.conflicts(dataclasses.replace(self.perms, deny_read=[entry]), repo=repo)
        self.assertTrue(any("repository" in problem for problem in problems), problems)

    def test_a_case_variant_deny_entry_is_one_entry_for_deny_and_allow(self):
        target = self.root / "Vault"
        permissions.add_deny_read(self.state, str(target))
        permissions.add_deny_read(self.state, str(target).lower())
        self.assertEqual(sum(1 for item in permissions.load_permissions(self.state).deny_read
                             if item.lower() == str(target).lower()), 1)
        permissions.remove_deny_read(self.state, str(target).upper())
        self.assertNotIn(str(target), permissions.load_permissions(self.state).deny_read)

    def test_allow_read_of_a_longer_sibling_is_not_found_under_the_entry(self):
        with self.assertRaises(permissions.PermissionsError) as caught:
            permissions.remove_deny_read(self.state, os.path.expanduser("~/.codex-old"))
        self.assertIn("is not in the deny_read list", str(caught.exception))
        self.assertNotIn("hidden by", str(caught.exception))

    def test_a_lone_surrogate_is_refused_not_a_crash(self):
        bad = "/tmp/\udcff"
        with self.assertRaises(permissions.PermissionsError):
            permissions.add_deny_read(self.state, bad)
        with self.assertRaises(permissions.PermissionsError):
            permissions.seatbelt_profile(dataclasses.replace(self.perms, deny_read=[bad]), primary_repo=self.repo)
        with self.assertRaises(permissions.PermissionsError):
            permissions.normalize_entry(bad)
        self.assertEqual(permissions.load_permissions(self.state), self.perms)  # nothing was saved
        self.assertFalse((self.state / "permissions.log").exists())

    def test_a_lone_surrogate_in_the_file_is_refused_by_name(self):
        self.file.write_text(json.dumps({"schema_version": 1, "mode": "full_host", "deny_read": ["/tmp/\udcff"],
                                         "protect_primary_checkout": True, "writable_roots": [],
                                         "network": {"allowed_domains": []}}), encoding="utf-8")
        with self.assertRaises(permissions.PermissionsError) as caught:
            permissions.load_permissions(self.state)
        self.assertIn("deny_read", str(caught.exception))


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

    def test_a_read_denial_inside_the_checkout_is_a_read_not_also_a_write(self):
        env = self.repo / ".env"
        perms = dataclasses.replace(self.perms, deny_read=[str(env)])
        for spelled in (str(env), f"{real(self.repo)}/.env"):
            with self.subTest(spelled=spelled):
                self.assertEqual(self.find(f"cat: {spelled}: Operation not permitted", perms=perms),
                                 [{"path": spelled, "kind": "read"}])
        # A path in the checkout that is not hidden is still a write, on the same line.
        other = f"{real(self.repo)}/src/a.py"
        self.assertEqual(self.find(f"cp: {env} {other}: Operation not permitted", perms=perms),
                         [{"path": str(env), "kind": "read"}, {"path": other, "kind": "write"}])

    def test_a_path_with_spaces_is_reported_whole(self):
        with mock.patch.dict(os.environ, {"HOME": str(self.root)}):
            perms = dataclasses.replace(self.perms, deny_read=["~/Library/Application Support/Google/Chrome"])
            login = f"{self.root}/Library/Application Support/Google/Chrome/Default/Login Data"
            found = self.find(f"cat: {login}: Operation not permitted",
                              f"cannot open '{login}': Operation not permitted",
                              f"PermissionError: [Errno 1] Operation not permitted: \"{login}\"",
                              f"error: could not open {login}. Operation not permitted",
                              perms=perms)
        self.assertEqual(found, [{"path": login, "kind": "read"}])

    def test_several_paths_with_spaces_on_one_line_are_each_whole(self):
        with mock.patch.dict(os.environ, {"HOME": str(self.root)}):
            perms = dataclasses.replace(self.perms, deny_read=["~/Library/Application Support/Firefox"])
            a = f"{self.root}/Library/Application Support/Firefox/Profiles/x y/logins.json"
            b = f"{real(self.repo)}/my notes/todo list.txt"
            found = self.find(f"cp: {a} {b}: Operation not permitted", perms=perms)
        self.assertEqual(found, [{"path": a, "kind": "read"}, {"path": b, "kind": "write"}])

    def test_balanced_brackets_stay_in_the_path_and_a_closing_one_ends_it(self):
        with_parens = f"{self.vault}/Chrome (Canary)/Login Data"
        self.assertEqual(self.find(f"cat: {with_parens}: Operation not permitted"),
                         [{"path": with_parens, "kind": "read"}])
        self.assertEqual(self.find(f"(open {self.vault}/a) failed: Operation not permitted"),
                         [{"path": f"{self.vault}/a", "kind": "read"}])

    def test_case_and_normalisation_do_not_change_what_matches(self):
        upper = f"{str(self.vault).upper()}/Token.txt"
        self.assertEqual(self.find(f"cat: {upper}: Operation not permitted"), [{"path": upper, "kind": "read"}])
        accented = self.root / unicodedata.normalize("NFC", "Cl\u00e9s")
        perms = dataclasses.replace(self.perms, deny_read=[str(accented)])
        spelled = unicodedata.normalize("NFD", str(accented)) + "/id"
        self.assertEqual(self.find(f"cat: {spelled}: Operation not permitted", perms=perms),
                         [{"path": spelled, "kind": "read"}])

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

    def move_then(self, profile, first, second, third=None):
        """``mv first second`` then (if that worked) read ``third`` or write into it."""
        command = 'mv "$1" "$2" && cat "$3"'
        return self.sandboxed(profile, ["/bin/sh", "-c", command, "sh", str(first), str(second), str(third or second)])

    def hidden_tree(self):
        vault = self.root / "vault"
        vault.mkdir()
        (vault / "Secret.txt").write_text("SECRET\n")
        (vault / "s3").write_text("S3\n")
        netrc = self.root / "netrc"
        netrc.write_text("machine x password SECRET\n")
        deep = self.root / "outer" / "inner"
        deep.mkdir(parents=True)
        (deep / "token").write_text("TOKEN\n")
        perms = dataclasses.replace(self.perms, deny_read=[str(vault), str(netrc), str(deep / "token")])
        return perms, vault, netrc, deep

    def assert_not_readable(self, outcome, *secrets):
        self.assertNotEqual(outcome.returncode, 0)
        for secret in secrets:
            self.assertNotIn(secret, outcome.stdout)

    def test_a_hidden_directory_cannot_be_renamed_to_read_it(self):
        perms, vault, _, _ = self.hidden_tree()
        profile = self.write_profile(perms)
        moved = self.root / "vault-moved"
        outcome = self.move_then(profile, vault, moved, moved / "Secret.txt")
        self.assert_not_readable(outcome, b"SECRET")
        self.assertIn(b"Operation not permitted", outcome.stderr)
        self.assertTrue((vault / "Secret.txt").exists())
        self.assertFalse(moved.exists())

    def test_a_hidden_file_cannot_be_renamed_to_read_it(self):
        perms, _, netrc, _ = self.hidden_tree()
        profile = self.write_profile(perms)
        moved = self.root / "netrc-moved"
        outcome = self.move_then(profile, netrc, moved)
        self.assert_not_readable(outcome, b"SECRET")
        self.assertTrue(netrc.exists())
        self.assertFalse(moved.exists())

    def test_a_file_cannot_be_moved_out_of_a_hidden_directory(self):
        perms, vault, _, _ = self.hidden_tree()
        profile = self.write_profile(perms)
        out = self.root / "out"
        outcome = self.move_then(profile, vault / "s3", out)
        self.assert_not_readable(outcome, b"S3")
        self.assertTrue((vault / "s3").exists())
        self.assertFalse(out.exists())

    def test_the_parent_of_a_hidden_path_cannot_be_renamed_to_read_it(self):
        perms, _, _, deep = self.hidden_tree()
        profile = self.write_profile(perms)
        for parent in (deep, deep.parent):
            with self.subTest(parent=str(parent)):
                moved = parent.with_name(parent.name + "-moved")
                outcome = self.move_then(profile, parent, moved, moved / "token" if parent == deep else moved / "inner" / "token")
                self.assert_not_readable(outcome, b"TOKEN")
                self.assertTrue((deep / "token").exists())
                self.assertFalse(moved.exists())

    def test_a_hidden_file_cannot_be_replaced_by_a_decoy(self):
        perms, _, netrc, _ = self.hidden_tree()
        profile = self.write_profile(perms)
        decoy = self.root / "decoy"
        decoy.write_text("decoy\n")
        swap = self.sandboxed(profile, ["/bin/mv", str(decoy), str(netrc)])
        self.assertNotEqual(swap.returncode, 0)
        self.assertIn(b"Operation not permitted", swap.stderr)
        self.assertEqual(netrc.read_text(), "machine x password SECRET\n")
        self.assertTrue(decoy.exists())

    def test_the_refs_directory_cannot_be_swapped_to_replace_branches(self):
        fake = self.root / "fake-refs"
        (fake / "heads").mkdir(parents=True)
        (fake / "heads" / "main").write_text("0" * 40 + "\n")
        profile = self.write_profile(dataclasses.replace(self.perms, deny_read=[]))
        before = sp.git(self.repo, "rev-parse", "main").stdout
        refs = self.repo / ".git" / "refs"
        aside = self.root / "refs-aside"
        moved = self.sandboxed(profile, ["/bin/mv", str(refs), str(aside)])
        self.assertNotEqual(moved.returncode, 0)
        self.assertFalse(aside.exists())
        swapped = self.sandboxed(profile, ["/bin/sh", "-c", 'mv "$1" "$3" && mv "$2" "$1"', "sh", str(refs), str(fake), str(aside)])
        self.assertNotEqual(swapped.returncode, 0)
        self.assertEqual(sp.git(self.repo, "rev-parse", "main").stdout, before)

    def test_the_git_directory_cannot_be_moved_or_replaced(self):
        profile = self.write_profile(dataclasses.replace(self.perms, deny_read=[]))
        git_dir = self.repo / ".git"
        moved = self.sandboxed(profile, ["/bin/mv", str(git_dir), str(self.root / "git-aside")])
        self.assertNotEqual(moved.returncode, 0)
        self.assertTrue((git_dir / "HEAD").exists())

    def test_a_parent_of_the_primary_checkout_cannot_be_moved_to_rewrite_it(self):
        anc = self.root / "anc"
        anc.mkdir()
        self.repo = sp.make_repo(anc)
        profile = self.write_profile(dataclasses.replace(self.perms, deny_read=[]))
        moved = self.root / "anc-moved"
        attack = ('mv "$1" "$2" && echo pwned > "$2/repo/add.py" && echo "ref: refs/heads/x" > "$2/repo/.git/HEAD" '
                  '; mv "$2" "$1"')
        outcome = self.sandboxed(profile, ["/bin/sh", "-c", attack, "sh", str(anc), str(moved)])
        self.assertFalse(moved.exists())
        self.assertEqual((self.repo / "add.py").read_text(), sp.BASE_FILES["add.py"])
        self.assertEqual((self.repo / ".git" / "HEAD").read_text(), "ref: refs/heads/main\n")
        self.assertIn(b"Operation not permitted", outcome.stderr)

    def test_the_parent_of_a_hidden_path_can_still_have_other_things_written_beside_it(self):
        perms, vault, _, _ = self.hidden_tree()
        profile = self.write_profile(perms)
        sibling = self.root / "sibling"
        self.assertEqual(self.sandboxed(profile, ["/usr/bin/touch", str(sibling)]).returncode, 0)
        self.assertEqual(self.sandboxed(profile, ["/bin/mv", str(sibling), str(self.root / "sibling-moved")]).returncode, 0)
        self.assertEqual(self.sandboxed(profile, ["/bin/mkdir", str(self.root / "new-dir")]).returncode, 0)
        scratch = self.repo / ".git" / "scratch"
        self.assertEqual(self.sandboxed(profile, ["/bin/sh", "-c", 'echo ok > "$1"', "sh", str(scratch)]).returncode, 0)

    def test_a_detached_worktree_still_commits_with_the_rename_rules_in_force(self):
        perms, vault, _, _ = self.hidden_tree()
        worktree = self.root / "wt"
        sp.git(self.repo, "worktree", "add", "-q", "--detach", str(worktree))
        profile = self.write_profile(perms)
        (worktree / "new.txt").write_text("n")
        git = ["/usr/bin/git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false"]
        add = self.sandboxed(profile, git + ["-C", str(worktree), "add", "new.txt"])
        commit = self.sandboxed(profile, git + ["-C", str(worktree), "commit", "-q", "-m", "from worktree"])
        self.assertEqual((add.returncode, commit.returncode), (0, 0), (add.stderr, commit.stderr))
        self.assertNotEqual(sp.git(worktree, "rev-parse", "HEAD").stdout, sp.git(self.repo, "rev-parse", "main").stdout)

    def test_a_case_variant_entry_still_hides_the_real_directory(self):
        vault = self.root / "Vault"
        vault.mkdir()
        (vault / "s").write_text("SECRET\n")
        variant = self.root / "vAULT"
        if not variant.exists():
            self.skipTest("case-sensitive volume")
        profile = self.write_profile(dataclasses.replace(self.perms, deny_read=[str(variant)]))
        outcome = self.sandboxed(profile, ["/bin/cat", str(vault / "s")])
        self.assert_not_readable(outcome, b"SECRET")
        self.assertIn(b"Operation not permitted", outcome.stderr)

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
