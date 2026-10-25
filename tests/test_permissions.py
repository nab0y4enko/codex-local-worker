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
        self.assertEqual(set(payload), {"schema_version", "mode", "deny_read", "deny_write",
                                        "protect_primary_checkout", "writable_roots", "network"})

    def test_the_default_file_has_the_deny_write_vectors(self):
        payload = json.loads(self.file.read_text(encoding="utf-8"))
        self.assertEqual(payload["deny_write"], list(permissions.DEFAULT_DENY_WRITE))

    def test_a_file_without_deny_write_keeps_the_default_vectors(self):
        # R19: deny_write is new, so a file written before it existed stays valid and protected.
        self.write_file(deny_write=None)
        perms = permissions.load_permissions(self.state)
        self.assertEqual(perms.deny_write, list(permissions.DEFAULT_DENY_WRITE))
        self.assertEqual((self.state / permissions.PERMISSIONS_FILE).read_text().count("deny_write"), 0)

    def test_an_explicit_empty_deny_write_is_honoured_not_defaulted(self):
        self.write_file(deny_write=[])
        self.assertEqual(permissions.load_permissions(self.state).deny_write, [])

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
        self.write_file(surprise=["~/x"])
        self.refused("surprise")

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
        git = os.path.realpath(self.repo) + "/.git"
        # The common dir, including objects/, remains write-denied.
        self.assertIn(f'(deny file-write* (subpath "{git}"))', text)
        self.assertLess(text.index(f'(deny file-write* (subpath "{os.path.realpath(self.repo)}"))'),
                        text.index(f'(deny file-write* (subpath "{git}"))'))
        self.assertNotIn(f'(allow file-write* (subpath "{git}/objects"))', text)

    def test_every_statement_is_a_denial_except_the_two_defaults_the_design_names(self):
        text = permissions.seatbelt_profile(self.perms, primary_repo=self.repo)
        statements = text.splitlines()
        self.assertEqual(statements[:2], ["(version 1)", "(allow default)"])
        for line in statements[2:]:
            self.assertRegex(line, r"^\((deny file-read\*|deny file-write\*|allow file-write\*|deny file-clone) ")
        allows = [line for line in statements if line.startswith("(allow ")]
        self.assertEqual(allows, ["(allow default)"])
        self.assertTrue(text.endswith("\n"))

    def test_the_documented_order_is_read_denials_then_the_primary_checkout_rules(self):
        text = permissions.seatbelt_profile(self.perms, primary_repo=self.repo)
        repo = real(self.repo)
        positions = [text.index("(deny file-read*"),
                     text.index(f'(deny file-write* (subpath "{repo}"))'),
                     text.index(f'(deny file-write* (subpath "{repo}/.git"))'),
                    ]
        self.assertEqual(positions, sorted(positions))
        # No file of the common dir is named: nothing in it is re-opened but objects/.
        for name in ("HEAD", "index", "config", "packed-refs"):
            self.assertNotIn(f'"{repo}/.git/{name}"', text)

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
            dataclasses.replace(self.perms, protect_primary_checkout=False, deny_write=[]),
            primary_repo=self.repo)
        self.assertNotIn(real(self.repo), text)
        self.assertIn("file-read", text)
        # What is hidden still cannot be changed or moved, whether or not the checkout is protected.
        (write_rule,) = [line for line in text.splitlines() if line.startswith("(deny file-write* (subpath")]
        self.assertIn(f'(subpath "{real(os.path.expanduser("~/.ssh"))}")', write_rule)

    def test_nothing_to_protect_writes_no_write_rule_at_all(self):
        text = permissions.seatbelt_profile(
            dataclasses.replace(self.perms, deny_read=[], protect_primary_checkout=False,
                                deny_write=[]),
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

    def test_cloud_codex_home_exceptions_are_limited_to_auth_and_observed_helper_scratch(self):
        home = self.root / "synthetic-codex-home"
        home.mkdir()
        auth = home / "auth.json"
        auth.write_text('{"synthetic":"auth"}')
        scratch = home / "tmp" / "arg0-macos"
        text = permissions.seatbelt_profile(
            self.perms, primary_repo=self.repo, additional_deny_read=[home],
            allow_read_metadata=[home],
            allow_read=[auth], allow_read_subpaths=[scratch],
            allow_write=[auth, home / "tmp", scratch], allow_write_subpaths=[scratch])
        self.assertIn(f'(subpath "{real(home)}")', text)
        self.assertIn(f'(allow file-read-metadata (literal "{real(home)}"))', text)
        self.assertIn(f'(allow file-read* (literal "{real(auth)}") '
                      f'(subpath "{real(scratch)}"))', text)
        self.assertIn(f'(allow file-write* (literal "{real(auth)}") '
                      f'(literal "{real(home / "tmp")}") '
                      f'(literal "{real(scratch)}") (subpath "{real(scratch)}"))', text)
        self.assertNotIn(f'(allow file-read* (subpath "{real(home)}")', text)
        self.assertNotIn(f'(allow file-read* (literal "{real(home / "config.toml")}")', text)

    def test_profile_refuses_an_exception_outside_a_denied_home(self):
        home = self.root / "synthetic-codex-home"
        home.mkdir()
        with self.assertRaises(permissions.PermissionsError):
            permissions.seatbelt_profile(self.perms, primary_repo=self.repo,
                additional_deny_read=[home], allow_read=[self.root / "outside-auth.json"])

    def test_profile_refuses_metadata_access_for_a_non_denied_directory(self):
        home = self.root / "synthetic-codex-home"
        home.mkdir()
        with self.assertRaises(permissions.PermissionsError):
            permissions.seatbelt_profile(self.perms, primary_repo=self.repo,
                additional_deny_read=[home], allow_read_metadata=[self.root])

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
        self.assertNotIn(f'(allow file-write* (subpath "{escaped}/.git/objects"))', text)
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
        literals = text.splitlines()[-1]  # the rename protection is the last statement
        for guarded in [real(vault), real(vault.parent), real(self.root), "/"]:
            self.assertIn(f'(literal "{guarded}")', literals)
        self.assertTrue(literals.startswith("(deny file-write* "))

    def test_the_primary_checkout_and_its_parents_cannot_be_moved(self):
        text = permissions.seatbelt_profile(self.perms, primary_repo=self.repo)
        repo = real(self.repo)
        literals = text.splitlines()[-1]  # the rename protection is the last statement
        for guarded in (repo, f"{repo}/.git", real(self.repo.parent), "/"):
            self.assertIn(f'(literal "{guarded}")', literals)
        # Children of the parents stay writable: only the directories themselves are guarded.
        self.assertNotIn(f'(literal "{repo}/.git/objects/pack")', text)
        self.assertNotIn(f'(subpath "{real(self.repo.parent)}")', text)

    def test_the_rename_rules_come_after_every_rule_they_must_outrank(self):
        text = permissions.seatbelt_profile(self.perms, primary_repo=self.repo)
        lines = text.splitlines()
        repo = real(self.repo)
        guard = [i for i, line in enumerate(lines) if f'(literal "{repo}/.git")' in line]
        self.assertEqual(len(guard), 1)
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

    def test_the_docstring_states_that_missing_parents_of_hidden_paths_cannot_be_created(self):
        doc = " ".join(permissions.seatbelt_profile.__doc__.split())
        self.assertIn("~/.docker", doc)
        self.assertIn("cannot be created", doc)


class StateProtection(PermissionsCase):
    """R16: a wrapped process cannot write the launcher's state, only this job's own paths."""

    def setUp(self):
        super().setUp()
        self.report_dir = self.state / "reports" / "job-1"
        self.worktree = self.state / "worktrees" / "job-1"
        for directory in (self.report_dir, self.worktree):
            directory.mkdir(parents=True)
        self.allowed = [self.worktree, self.report_dir / "codex-home",
                        self.report_dir / "attempt-1.last-message.txt"]

    def profile(self, perms=None, allowed=None):
        return permissions.seatbelt_profile(perms or self.perms, primary_repo=self.repo,
                                            state_dir=self.state,
                                            allowed_writes=self.allowed if allowed is None else allowed)

    def test_the_state_directory_is_write_denied_and_only_this_jobs_paths_reopened(self):
        lines = self.profile().splitlines()
        state = real(self.state)
        deny = lines.index(f'(deny file-write* (subpath "{state}"))')
        reopen = lines[deny + 1]
        self.assertEqual(reopen, "(allow file-write* " + " ".join(
            f'(subpath "{real(path)}")' for path in self.allowed) + ")")

    def test_the_state_rules_come_before_every_rule_that_must_outrank_them(self):
        """A job path can never re-open the primary checkout or a hidden path."""
        lines = self.profile().splitlines()
        state = real(self.state)
        reopen = [i for i, line in enumerate(lines) if line.startswith("(allow file-write* ")
                  and state in line]
        primary = lines.index(f'(deny file-write* (subpath "{real(self.repo)}"))')
        hidden = [i for i, line in enumerate(lines) if line.startswith("(deny file-write* (subpath")
                  and real(os.path.expanduser("~/.ssh")) in line]
        self.assertLess(reopen[0], primary)
        self.assertLess(reopen[0], hidden[0])

    def test_paths_outside_the_state_directory_are_not_reopened(self):
        """They are writable already; one *above* the state (a TMPDIR that holds it) would
        otherwise re-open the whole state directory."""
        above = self.root
        outside = self.root / "elsewhere"
        text = self.profile(allowed=[above, outside, self.state, self.worktree])
        (reopen,) = [line for line in text.splitlines() if line.startswith("(allow file-write* ")
                     and real(self.state) in line]
        self.assertEqual(reopen, f'(allow file-write* (subpath "{real(self.worktree)}"))')
        self.assertNotIn(f'(subpath "{real(above)}")', text)
        self.assertNotIn(f'(subpath "{real(outside)}")', text)

    def test_nothing_inside_the_state_to_reopen_writes_no_allow(self):
        text = self.profile(allowed=[])
        self.assertIn(f'(deny file-write* (subpath "{real(self.state)}"))', text)
        self.assertEqual([line for line in text.splitlines() if line.startswith("(allow file-write*")], [])

    def test_the_state_its_parents_and_the_permissions_files_cannot_be_moved(self):
        rename = self.profile().splitlines()[-1]
        state = real(self.state)
        for guarded in (state, f"{state}/permissions.json", f"{state}/permissions.log",
                        real(self.state.parent), "/"):
            with self.subTest(guarded=guarded):
                self.assertIn(f'(literal "{guarded}")', rename)
        self.assertTrue(rename.startswith("(deny file-write* (literal "))

    def test_without_a_state_directory_the_profile_is_the_task_8_profile(self):
        self.assertEqual(permissions.seatbelt_profile(self.perms, primary_repo=self.repo),
                         permissions.seatbelt_profile(self.perms, primary_repo=self.repo,
                                                      state_dir=None, allowed_writes=()))
        self.assertNotIn(real(self.state), permissions.seatbelt_profile(self.perms,
                                                                        primary_repo=self.repo))


class PlantingAndCloning(PermissionsCase):
    """R18: no hook can be planted in the primary checkout, no hidden file cloned out."""

    def test_hooks_info_modules_and_alternates_are_inside_the_shut_common_dir(self):
        lines = permissions.seatbelt_profile(self.perms, primary_repo=self.repo).splitlines()
        git = real(self.repo) + "/.git"
        shut = lines.index(f'(deny file-write* (subpath "{git}"))')
        reopened = [line for line in lines if line.startswith("(allow file-write* ")]
        # Nothing in the common directory is re-opened; the job has a separate object store.
        self.assertEqual(reopened, [])
        for planting in ("hooks", "info", "modules", "objects/info/alternates"):
            with self.subTest(planting=planting):
                self.assertTrue(sp.seatbelt_write_allowed("\n".join(lines), self.repo / ".git" / planting) is False)

    def test_no_planting_rule_without_primary_protection(self):
        text = permissions.seatbelt_profile(
            dataclasses.replace(self.perms, protect_primary_checkout=False), primary_repo=self.repo)
        self.assertNotIn("/.git/hooks", text)

    def test_cloning_a_hidden_path_or_any_parent_of_one_is_denied(self):
        vault = self.root / "outer" / "vault"
        vault.mkdir(parents=True)
        perms = dataclasses.replace(self.perms, deny_read=[str(vault)])
        lines = permissions.seatbelt_profile(perms, primary_repo=self.repo).splitlines()
        (clone,) = [line for line in lines if line.startswith("(deny file-clone ")]
        for parent in [real(vault.parent), real(self.root), "/"]:
            self.assertIn(f'(literal "{parent}")', clone)
        self.assertIn(f'(subpath "{real(vault)}")', clone)
        self.assertNotIn(f'(literal "{real(vault)}")', clone)
        self.assertLess(lines.index(clone), len(lines) - 1)  # the rename rule stays last

    def test_nothing_hidden_means_no_clone_rule(self):
        text = permissions.seatbelt_profile(dataclasses.replace(self.perms, deny_read=[]),
                                            primary_repo=self.repo)
        self.assertNotIn("file-clone", text)


class DenyWriteAndGitAdmin(PermissionsCase):
    """R19 deny_write vectors, and item 1's worktree-gitfile / linked-admin protection."""

    def test_deny_write_vectors_are_write_denied_and_rename_protected(self):
        with mock.patch.dict(os.environ, {"HOME": str(self.root)}):
            perms = dataclasses.replace(self.perms, deny_read=[], deny_write=["~/.zshrc", "~/.config/git"])
            text = permissions.seatbelt_profile(perms, primary_repo=self.repo)
        (rule,) = [line for line in text.splitlines()
                   if line.startswith("(deny file-write* (subpath") and ".zshrc" in line]
        self.assertIn(f'(subpath "{real(self.root / ".zshrc")}")', rule)
        self.assertIn(f'(subpath "{real(self.root / ".config/git")}")', rule)
        # Rename protection (last line) guards each vector and its parents, so none can be moved.
        self.assertIn(f'(literal "{real(self.root / ".zshrc")}")', text.splitlines()[-1])
        self.assertIn(f'(literal "{real(self.root / ".config")}")', text.splitlines()[-1])

    def test_an_empty_deny_write_emits_no_vector_rule(self):
        text = permissions.seatbelt_profile(dataclasses.replace(self.perms, deny_write=[]),
                                            primary_repo=self.repo)
        self.assertNotIn(".zshrc", text)

    def test_the_job_admin_dir_is_reopened_but_its_redirects_stay_denied(self):
        admin = self.repo / ".git" / "worktrees" / "job-1"
        worktree = self.state / "worktrees" / "job-1"
        text = permissions.seatbelt_profile(self.perms, primary_repo=self.repo,
                                            job_git_admin=admin, job_worktree=worktree)
        lines = text.splitlines()
        reopen = lines.index(f'(allow file-write* (subpath "{real(self.repo)}/.git/worktrees/job-1"))')
        # The whole common dir is denied earlier, the job's admin re-opened after it.
        common_deny = lines.index(f'(deny file-write* (subpath "{real(self.repo)}/.git"))')
        self.assertLess(common_deny, reopen)
        redirect_deny = lines[reopen + 1]
        for name in ("commondir", "gitdir", "config.worktree"):
            self.assertIn(f'(literal "{real(self.repo)}/.git/worktrees/job-1/{name}")', redirect_deny)
        # The worktree's own .git gitfile is denied, and everything is rename-protected.
        self.assertIn(f'(deny file-write* (literal "{real(worktree)}/.git"))', lines)
        self.assertIn(f'(literal "{real(worktree)}/.git")', lines[-1])
        self.assertIn(f'(literal "{real(self.repo)}/.git/worktrees/job-1/commondir")', lines[-1])


class GitLayoutCase(PermissionsCase):
    """Builders for the three shapes of primary checkout R22 has to protect."""

    def separate_primary(self, name="sep-primary"):
        repo = self.root / name
        sep = self.root / "gitdirs" / (name + ".git")
        sep.parent.mkdir(exist_ok=True)
        sp.git(self.root, "init", "-q", "--initial-branch=main", "--separate-git-dir", str(sep), str(repo))
        (repo / "add.py").write_text("x = 1\n")
        sp.git(repo, "add", "add.py")
        sp.git(repo, "commit", "-q", "-m", "base")
        return repo, sep

    def linked_primary(self, name="linked-primary"):
        primary = self.root / name
        sp.git(self.repo, "worktree", "add", "-q", "--detach", str(primary))
        return primary

    def rev_parse(self, cwd, *args):
        return os.path.realpath(sp.git(cwd, "rev-parse", *args).stdout.decode().strip())


class GitLayouts(GitLayoutCase):
    """R22: the resolved common dir (never a hardcoded ``<repo>/.git``) is an allowlist."""

    def test_resolve_git_dirs_matches_git_for_every_checkout_shape(self):
        repo, sep = self.separate_primary()
        linked = self.linked_primary()
        for primary in (self.repo, repo, linked):
            with self.subTest(primary=primary.name):
                git_dir, common = permissions.resolve_git_dirs(primary)
                self.assertEqual(git_dir, self.rev_parse(primary, "--absolute-git-dir"))
                self.assertEqual(common, self.rev_parse(primary, "--path-format=absolute", "--git-common-dir"))
        self.assertEqual(permissions.resolve_git_dirs(repo), (real(sep), real(sep)))
        git_dir, common = permissions.resolve_git_dirs(linked)
        self.assertEqual(common, real(self.repo / ".git"))
        self.assertEqual(git_dir, real(self.repo / ".git" / "worktrees" / "linked-primary"))

    def test_a_directory_that_is_not_a_checkout_keeps_the_nominal_git_path(self):
        bare = self.root / "not-a-repo"
        bare.mkdir()
        self.assertEqual(permissions.resolve_git_dirs(bare), (real(bare) + "/.git",) * 2)

    def test_a_malformed_gitfile_is_refused_not_guessed(self):
        broken = self.root / "broken"
        broken.mkdir()
        (broken / ".git").write_text("this is not a gitfile\n")
        with self.assertRaises(permissions.PermissionsError):
            permissions.resolve_git_dirs(broken)

    def test_the_common_dir_is_shut_and_only_the_job_admin_is_reopened(self):
        admin = self.repo / ".git" / "worktrees" / "job-1"
        worktree = self.state / "worktrees" / "job-1"
        lines = permissions.seatbelt_profile(self.perms, primary_repo=self.repo, job_git_admin=admin,
                                             job_worktree=worktree).splitlines()
        git = real(self.repo) + "/.git"
        shut = lines.index(f'(deny file-write* (subpath "{git}"))')
        self.assertGreater(shut, lines.index(f'(deny file-write* (subpath "{real(self.repo)}"))'))
        self.assertEqual(lines[shut + 1], f'(allow file-write* (subpath "{git}/worktrees/job-1"))')
        # Nothing else of the common dir is ever re-opened: no allow names a path inside it.
        reopened = [line for line in lines if line.startswith("(allow file-write* ") and git in line]
        self.assertEqual(len(reopened), 1, reopened)
        for old in ("(literal \"%s/HEAD\")", "(subpath \"%s/refs\")", "(subpath \"%s/hooks\")"):
            self.assertNotIn(old % git, "\n".join(lines))  # the denylist of rounds 1-2 is gone

    def test_without_a_job_worktree_the_common_dir_is_never_reopened(self):
        lines = permissions.seatbelt_profile(self.perms, primary_repo=self.repo).splitlines()
        git = real(self.repo) + "/.git"
        self.assertEqual([line for line in lines if line.startswith("(allow file-write*")],
                         [])

    def test_objects_and_its_info_directory_cannot_be_moved(self):
        rename = permissions.seatbelt_profile(self.perms, primary_repo=self.repo).splitlines()[-1]
        git = real(self.repo) + "/.git"
        for guarded in (git, real(self.repo), "/"):
            with self.subTest(guarded=guarded):
                self.assertIn(f'(literal "{guarded}")', rename)

    def test_a_separate_git_dir_primary_is_protected_where_its_git_dir_really_is(self):
        repo, sep = self.separate_primary()
        text = permissions.seatbelt_profile(self.perms, primary_repo=repo)
        lines = text.splitlines()
        self.assertIn(f'(deny file-write* (subpath "{real(sep)}"))', lines)
        self.assertNotIn(f'(allow file-write* (subpath "{real(sep)}/objects"))', lines)
        self.assertNotIn(f'(deny file-write* (subpath "{real(sep)}/objects/info"))', lines)
        self.assertNotIn(f"{real(repo)}/.git/", text)  # the gitfile is a file, never a directory
        self.assertNotIn(f'(literal "{real(sep)}/objects")', lines[-1])
        self.assertIn(f'(literal "{real(sep.parent)}")', lines[-1])

    def test_a_linked_worktree_primary_protects_the_common_dir_and_its_own_admin_dir(self):
        primary = self.linked_primary()
        git_dir, common = permissions.resolve_git_dirs(primary)
        lines = permissions.seatbelt_profile(self.perms, primary_repo=primary).splitlines()
        self.assertEqual(common, real(self.repo) + "/.git")
        self.assertIn(f'(deny file-write* (subpath "{common}") (subpath "{git_dir}"))', lines)
        self.assertNotIn(f'(allow file-write* (subpath "{common}/objects"))', lines)
        self.assertIn(f'(literal "{git_dir}")', lines[-1])

    def test_an_explicit_common_dir_is_protected_beside_the_resolved_one(self):
        elsewhere = self.root / "elsewhere.git"
        lines = permissions.seatbelt_profile(self.perms, primary_repo=self.repo,
                                             git_common_dir=elsewhere).splitlines()
        resolved = real(self.repo) + "/.git"
        self.assertIn(f'(deny file-write* (subpath "{resolved}") (subpath "{real(elsewhere)}"))', lines)
        self.assertNotIn(f'(allow file-write* (subpath "{resolved}/objects")', "\n".join(lines))

    def test_no_common_dir_rule_without_primary_protection(self):
        text = permissions.seatbelt_profile(dataclasses.replace(self.perms, protect_primary_checkout=False),
                                            primary_repo=self.repo, git_common_dir=self.root / "x.git")
        self.assertNotIn("x.git", text)
        self.assertNotIn(f"{real(self.repo)}/.git", text)


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

    def test_a_deny_write_entry_covering_a_job_path_is_refused_naming_it(self):
        """R24: ``deny_write`` is applied after the job's own re-openings, so an entry that is, or
        sits above, the state directory, report directory, worktree root or Codex home would make
        the job unable to write what it needs and fail silently at run time."""
        report = self.state / "reports" / "job-1"
        home = report / "codex-home"
        cases = {"state directory": self.state, "state directory (parent)": self.state.parent,
                 "report directory": report, "worktree root": self.state / "worktrees",
                 "Codex home": home, "reports": self.state / "reports"}
        for label, entry in cases.items():
            with self.subTest(label=label):
                problems = self.conflicts(dataclasses.replace(self.perms, deny_read=[], deny_write=[str(entry)]),
                                          report_dir=report, codex_home=home)
                self.assertTrue(problems)
                self.assertTrue(all("deny_write entry" in problem and "allow-write" in problem
                                    and str(entry) in problem for problem in problems), problems)
        named = " ".join(self.conflicts(dataclasses.replace(self.perms, deny_read=[], deny_write=[str(home)]),
                                        report_dir=report, codex_home=home))
        self.assertIn("Codex home", named)

    def test_a_deny_write_entry_inside_a_job_path_or_elsewhere_is_fine(self):
        report = self.state / "reports" / "job-1"
        fine = [str(report / "codex-home" / "secret"), str(self.state / "worktrees" / "job-1" / ".env"),
                str(self.root / "elsewhere"), str(self.repo / ".env")]
        self.assertEqual(self.conflicts(dataclasses.replace(self.perms, deny_read=[], deny_write=fine),
                                        report_dir=report, codex_home=report / "codex-home"), [])

    def test_the_default_deny_write_list_conflicts_with_nothing(self):
        report = self.state / "reports" / "job-1"
        self.assertEqual(self.conflicts(report_dir=report, codex_home=report / "codex-home"), [])

    def test_deny_write_is_compared_by_real_path_and_case(self):
        link = self.root / "state-alias"
        link.symlink_to(self.state)
        for entry in (link, Path(str(self.state).upper()), Path(unicodedata.normalize("NFD", str(self.state)))):
            with self.subTest(entry=str(entry)):
                self.assertTrue(self.conflicts(dataclasses.replace(self.perms, deny_read=[],
                                                                   deny_write=[str(entry)])))


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

    def test_a_rename_reports_both_denied_paths_and_not_the_word_between_them(self):
        repo = real(self.repo)
        self.assertEqual(self.find(f"mv: rename {repo}/a.py to {repo}/b.py: Operation not permitted"),
                         [{"path": f"{repo}/a.py", "kind": "write"},
                          {"path": f"{repo}/b.py", "kind": "write"}])
        self.assertEqual(self.find(f"mv: rename {self.vault}/s3 to /x/out: Operation not permitted"),
                         [{"path": f"{self.vault}/s3", "kind": "read"}])

    def test_the_sentence_after_a_path_is_not_part_of_it(self):
        repo = real(self.repo)
        cases = {
            f"error: could not open {repo}/a.py for writing: Operation not permitted": f"{repo}/a.py",
            f"open {repo}/x failed (Operation not permitted)": f"{repo}/x",
            f"error: couldn't create {repo}/y (Operation not permitted)": f"{repo}/y",
            f"cp {repo}/a -> {repo}: Operation not permitted": f"{repo}/a",
            f"Error Domain=NSCocoaErrorDomain Code=513 UserInfo={{NSFilePath={repo}/z.swift, "
            f'NSUnderlyingError=0x6 {{Error Domain=NSPOSIXErrorDomain Code=1 "Operation not permitted"}}}}':
                f"{repo}/z.swift",
        }
        for line, path in cases.items():
            with self.subTest(line=line):
                self.assertEqual(self.find(line)[0], {"path": path, "kind": "write"})

    def test_a_capitalised_or_prose_word_inside_a_path_name_survives(self):
        with mock.patch.dict(os.environ, {"HOME": str(self.root)}):
            perms = dataclasses.replace(self.perms, deny_read=["~/Library/Application Support/Google/Chrome"])
            login = f"{self.root}/Library/Application Support/Google/Chrome/Default/Login Data"
            nested = f"{self.root}/Library/Application Support/Google/Chrome/notes to self/for me.txt"
            found = self.find(f"mv: rename {login} to /tmp/x: Operation not permitted",
                              f"cat: {nested}: Operation not permitted", perms=perms)
        # A prose word with more path after it ("notes to self/...") is part of a name.
        self.assertEqual(found, [{"path": login, "kind": "read"}, {"path": nested, "kind": "read"}])

    def test_a_write_into_the_state_directory_is_reported_when_it_is_given(self):
        state = real(self.state)
        line = f"touch: {state}/permissions.json: Operation not permitted"
        self.assertEqual(self.find(line), [])
        self.assertEqual(permissions.find_denials(line, self.perms, self.repo, state_dir=self.state),
                         [{"path": f"{state}/permissions.json", "kind": "write"}])
        unprotected = dataclasses.replace(self.perms, protect_primary_checkout=False)
        self.assertEqual(permissions.find_denials(line, unprotected, self.repo, state_dir=self.state),
                         [{"path": f"{state}/permissions.json", "kind": "write"}])

    def test_a_deny_write_vector_is_reported_as_a_write(self):
        with mock.patch.dict(os.environ, {"HOME": str(self.root)}):
            perms = dataclasses.replace(self.perms, deny_write=["~/.gitconfig"])
            target = f"{self.root}/.gitconfig"
            self.assertEqual(permissions.find_denials(
                f"error: could not write {target}: Operation not permitted", perms, self.repo),
                [{"path": target, "kind": "write"}])

    def test_a_denial_in_a_git_dir_outside_the_checkout_is_a_primary_checkout_write(self):
        sep = self.root / "gitdirs" / "repo.git"
        line = f"sh: {real(self.root)}/gitdirs/repo.git/config: Operation not permitted"
        self.assertEqual(permissions.find_denials(line, self.perms, self.repo), [])
        self.assertEqual(
            permissions.find_denials(line, self.perms, self.repo, git_dirs=[sep], worktree=self.root / "wt"),
            [{"path": f"{real(sep)}/config", "kind": "write", "cause": "primary_checkout"}])
        unprotected = dataclasses.replace(self.perms, protect_primary_checkout=False)
        self.assertEqual(permissions.find_denials(line, unprotected, self.repo, git_dirs=[sep]), [])

    def test_with_a_worktree_every_entry_carries_a_cause(self):
        worktree = self.root / "wt"
        secret = f"{self.vault}/token"
        primary = f"{real(self.repo)}/a.py"
        state = f"{real(self.state)}/leases/x"
        found = permissions.find_denials(
            "\n".join([f"cat: {secret}: Operation not permitted",
                       f"touch: {primary}: Operation not permitted",
                       f"touch: {state}: Operation not permitted"]) + "\n",
            self.perms, self.repo, state_dir=self.state, worktree=worktree)
        self.assertEqual(found, [{"path": secret, "kind": "read", "cause": "deny_read"},
                                 {"path": primary, "kind": "write", "cause": "primary_checkout"},
                                 {"path": state, "kind": "write", "cause": "state_dir"}])

    def test_a_refused_hard_link_into_the_worktree_is_a_write_with_cause_unknown(self):
        # ``ln <secret> <worktree>/hl`` is refused on the source, but the tool often names only the
        # destination (inside the job's own worktree); without the worktree it is missed.
        worktree = self.root / "state" / "worktrees" / "job-1"
        dest = f"{real(self.root)}/state/worktrees/job-1/hl-key"
        line = f"ln: {dest}: Operation not permitted"
        self.assertEqual(self.find(line), [])  # no worktree given: nothing matches
        self.assertEqual(permissions.find_denials(line, self.perms, self.repo,
                                                  state_dir=self.state, worktree=worktree),
                         [{"path": dest, "kind": "write", "cause": "unknown"}])


@unittest.skipUnless(sys.platform == "darwin" and permissions.wrapper_available(), "Seatbelt only")
class RealWrapper(PermissionsCase):
    def sandboxed(self, profile, argv, **kwargs):
        env = dict(os.environ, **sp.GIT_ID)
        env.update(getattr(self, "object_env", {}) or {})
        return subprocess.run(permissions.wrap_argv(argv, profile), capture_output=True, env=env, **kwargs)

    def write_profile(self, perms, worktree=None):
        """Write the job profile. When ``worktree`` is a linked worktree of the primary, its admin
        dir is re-opened (as the launcher does) so a commit in it still works."""
        kwargs = {}
        if worktree is not None:
            admin = sp.git(worktree, "rev-parse", "--absolute-git-dir").stdout.decode().strip()
            objects = self.root / "job-objects"
            objects.mkdir(exist_ok=True)
            (objects / "info").mkdir(exist_ok=True)
            common = Path(sp.git(worktree, "rev-parse", "--path-format=absolute",
                                 "--git-common-dir").stdout.decode().strip())
            kwargs = {"job_git_admin": admin, "job_worktree": str(worktree),
                      "allowed_writes": [objects],
                      "protect_write_roots": [str(worktree), str(objects)],
                      "deny_write_subpaths": [str(objects / "info")]}
            self.object_env = {"GIT_OBJECT_DIRECTORY": str(objects),
                               "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(common / "objects")}
        profile = self.root / "p.sb"
        profile.write_text(permissions.seatbelt_profile(perms, primary_repo=self.repo, **kwargs),
                           encoding="utf-8")
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

    def test_cloud_home_profile_reads_auth_and_helper_scratch_but_hides_other_files(self):
        home = self.root / "synthetic-codex-home"
        home.mkdir()
        auth = home / "auth.json"
        auth.write_text('{"token":"synthetic-only"}')
        secret = home / "config.toml"
        secret.write_text("synthetic user config")
        history = home / "sessions" / "session.jsonl"
        history.parent.mkdir()
        history.write_text("synthetic session history")
        scratch = home / "tmp" / "arg0-macos"
        profile = self.root / "cloud-home.sb"
        profile.write_text(permissions.seatbelt_profile(
            self.perms, primary_repo=self.repo, additional_deny_read=[home],
            allow_read_metadata=[home],
            allow_read=[auth], allow_read_subpaths=[scratch],
            allow_write=[auth, home / "tmp", scratch], allow_write_subpaths=[scratch]),
            encoding="utf-8")
        auth_read = self.sandboxed(profile, ["/bin/cat", str(auth)])
        config_read = self.sandboxed(profile, ["/bin/cat", str(secret)])
        history_read = self.sandboxed(profile, ["/bin/cat", str(history)])
        directory_list = self.sandboxed(profile, ["/bin/ls", "-la", str(home)])
        scratch_parent = self.sandboxed(profile, ["/bin/mkdir", str(home / "tmp")])
        scratch_create = self.sandboxed(profile, ["/bin/mkdir", str(scratch)])
        scratch_write = self.sandboxed(profile, ["/bin/sh", "-c", 'echo helper > "$1"',
                                                "sh", str(scratch / "apply_patch")])
        scratch_read = self.sandboxed(profile, ["/bin/cat", str(scratch / "apply_patch")])
        self.assertEqual(auth_read.returncode, 0, auth_read.stderr)
        self.assertEqual(auth_read.stdout, b'{"token":"synthetic-only"}')
        self.assertNotEqual(config_read.returncode, 0)
        self.assertNotEqual(history_read.returncode, 0)
        self.assertNotEqual(directory_list.returncode, 0)
        self.assertEqual(scratch_parent.returncode, 0, scratch_parent.stderr)
        self.assertEqual(scratch_create.returncode, 0, scratch_create.stderr)
        self.assertEqual(scratch_write.returncode, 0, scratch_write.stderr)
        self.assertEqual(scratch_read.stdout.strip(), b"helper")

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
        profile = self.write_profile(dataclasses.replace(self.perms, deny_read=[]), worktree=worktree)
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
        # The primary object database is read-only even when it is the direct target.
        scratch = self.repo / ".git" / "objects" / "scratch"
        self.assertNotEqual(self.sandboxed(profile, ["/bin/sh", "-c", 'echo ok > "$1"', "sh", str(scratch)]).returncode, 0)
        beside = self.repo / ".git" / "scratch"
        self.assertNotEqual(self.sandboxed(profile, ["/bin/sh", "-c", 'echo ok > "$1"', "sh", str(beside)]).returncode, 0)

    def test_a_detached_worktree_still_commits_with_the_rename_rules_in_force(self):
        perms, vault, _, _ = self.hidden_tree()
        worktree = self.root / "wt"
        sp.git(self.repo, "worktree", "add", "-q", "--detach", str(worktree))
        profile = self.write_profile(perms, worktree=worktree)
        (worktree / "new.txt").write_text("n")
        git = ["/usr/bin/git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false"]
        add = self.sandboxed(profile, git + ["-C", str(worktree), "add", "new.txt"])
        commit = self.sandboxed(profile, git + ["-C", str(worktree), "commit", "-q", "-m", "from worktree"])
        self.assertEqual((add.returncode, commit.returncode), (0, 0), (add.stderr, commit.stderr))
        head = self.sandboxed(profile, git + ["-C", str(worktree), "rev-parse", "HEAD"]).stdout
        self.assertNotEqual(head, sp.git(self.repo, "rev-parse", "main").stdout)

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

    def state_profile(self, perms):
        """A profile with the launcher state shut and one job's own paths re-opened."""
        self.job_report = self.state / "reports" / "job-1"
        self.job_tree = self.state / "worktrees" / "job-1"
        self.other_tree = self.state / "worktrees" / "job-0"
        for directory in (self.job_report / "codex-home", self.job_tree, self.other_tree):
            directory.mkdir(parents=True, exist_ok=True)
        self.last_message = self.job_report / "attempt-1.last-message.txt"
        profile = self.root / "state.sb"
        profile.write_text(permissions.seatbelt_profile(
            perms, primary_repo=self.repo, state_dir=self.state,
            allowed_writes=[self.job_tree, self.job_report / "codex-home", self.last_message,
                            Path(os.environ.get("TMPDIR", "/tmp"))]), encoding="utf-8")
        probe = subprocess.run(permissions.wrap_argv(["/usr/bin/true"], profile), capture_output=True)
        if probe.returncode == NESTED_SANDBOX:
            self.skipTest("already inside a sandbox")
        self.assertEqual(probe.returncode, 0, probe.stderr)
        return profile

    def test_the_launcher_state_cannot_be_written_but_the_jobs_own_paths_can(self):
        profile = self.state_profile(self.perms)
        before = self.file.read_bytes()
        write = ["/bin/sh", "-c", 'echo x >> "$1"', "sh"]
        denied = {
            "permissions.json": self.sandboxed(profile, ["/usr/bin/touch", str(self.file)]),
            "append permissions.json": self.sandboxed(profile, write + [str(self.file)]),
            "permissions.log": self.sandboxed(profile, write + [str(self.state / "permissions.log")]),
            "report.json": self.sandboxed(profile, write + [str(self.job_report / "report.json")]),
            "another job's worktree": self.sandboxed(profile, write + [str(self.other_tree / "a")]),
            "a lock": self.sandboxed(profile, write + [str(self.state / "worker.lock")]),
            "move the state": self.sandboxed(profile, ["/bin/mv", str(self.state), str(self.root / "s2")]),
            "replace permissions.json": self.sandboxed(profile, ["/bin/sh", "-c",
                'echo "{}" > "$2" && mv "$2" "$1"', "sh", str(self.file), str(self.root / "decoy")]),
        }
        for name, outcome in denied.items():
            with self.subTest(denied=name):
                self.assertNotEqual(outcome.returncode, 0)
                self.assertIn(b"Operation not permitted", outcome.stderr)
        self.assertEqual(self.file.read_bytes(), before)
        self.assertTrue(self.state.is_dir())
        allowed = {
            "worktree": write + [str(self.job_tree / "add.py")],
            "codex home": write + [str(self.job_report / "codex-home" / "log")],
            "last message": write + [str(self.last_message)],
            "elsewhere": write + [str(self.root / "elsewhere.txt")],
        }
        for name, argv in allowed.items():
            with self.subTest(allowed=name):
                outcome = self.sandboxed(profile, argv)
                self.assertEqual(outcome.returncode, 0, outcome.stderr)

    def test_cloning_a_parent_of_a_hidden_path_cannot_copy_it_out(self):
        perms, vault, _, deep = self.hidden_tree()
        plain = deep.parent / "plain.txt"
        plain.write_text("plain\n")
        profile = self.write_profile(perms)
        out = sp.tmpdir()  # outside every source, so a clone is never into itself
        self.addCleanup(sp.cleanup, out)
        clone = ("import ctypes, sys\n"
                 "libc = ctypes.CDLL(None, use_errno=True)\n"
                 "rc = libc.clonefile(sys.argv[1].encode(), sys.argv[2].encode(), 0)\n"
                 "sys.exit(0 if rc == 0 else ctypes.get_errno())\n")
        for source, target, leak in ((self.root, out / "root", out / "root" / "vault" / "Secret.txt"),
                                     (deep.parent, out / "outer", out / "outer" / "inner" / "token"),
                                     (deep, out / "inner", out / "inner" / "token"),
                                     (vault, out / "vault", out / "vault" / "Secret.txt")):
            with self.subTest(source=str(source)):
                outcome = self.sandboxed(profile, [sys.executable, "-c", clone, str(source), str(target)])
                self.assertEqual(outcome.returncode, 1, outcome.stderr)  # EPERM
                self.assertFalse(leak.exists())
        copied = self.sandboxed(profile, ["/bin/cp", "-c", str(plain), str(out / "plain.txt")])
        self.assertEqual(copied.returncode, 0, copied.stderr)
        self.assertEqual((out / "plain.txt").read_text(), "plain\n")

    def test_no_hook_can_be_planted_while_a_linked_worktree_still_commits(self):
        worktree = self.root / "wt"
        sp.git(self.repo, "worktree", "add", "-q", "--detach", str(worktree))
        profile = self.write_profile(dataclasses.replace(self.perms, deny_read=[]), worktree=worktree)
        git_dir = self.repo / ".git"
        (git_dir / "modules").mkdir(exist_ok=True)
        write = ["/bin/sh", "-c", 'mkdir -p "$(dirname "$1")" ; echo "#!/bin/sh" > "$1"', "sh"]
        for planted in (git_dir / "hooks" / "post-checkout", git_dir / "info" / "attributes",
                        git_dir / "objects" / "info" / "alternates",
                        git_dir / "modules" / "sub" / "hooks" / "post-checkout"):
            with self.subTest(planted=str(planted.relative_to(git_dir))):
                outcome = self.sandboxed(profile, write + [str(planted)])
                self.assertNotEqual(outcome.returncode, 0)
                self.assertIn(b"Operation not permitted", outcome.stderr)
                self.assertFalse(planted.exists())
        moved = self.sandboxed(profile, ["/bin/mv", str(git_dir / "hooks"), str(self.root / "hooks")])
        self.assertNotEqual(moved.returncode, 0)
        (worktree / "new.txt").write_text("n")
        git = ["/usr/bin/git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false"]
        add = self.sandboxed(profile, git + ["-C", str(worktree), "add", "new.txt"])
        commit = self.sandboxed(profile, git + ["-C", str(worktree), "commit", "-q", "-m", "from worktree"])
        self.assertEqual((add.returncode, commit.returncode), (0, 0), (add.stderr, commit.stderr))
        head = self.sandboxed(profile, git + ["-C", str(worktree), "rev-parse", "HEAD"]).stdout
        self.assertNotEqual(head, sp.git(self.repo, "rev-parse", "main").stdout)

    def test_a_deny_write_persistence_vector_cannot_be_planted(self):
        home = self.root / "home"
        (home / ".config" / "git").mkdir(parents=True)
        zshrc = home / ".zshrc"
        zshrc.write_text("# user rc\n")
        perms = dataclasses.replace(self.perms, deny_read=[],
                                    deny_write=[str(zshrc), str(home / ".config" / "git")])
        profile = self.write_profile(perms)
        write = ["/bin/sh", "-c", 'echo "evil" >> "$1"', "sh"]
        for target in (zshrc, home / ".config" / "git" / "config"):
            with self.subTest(target=str(target)):
                outcome = self.sandboxed(profile, write + [str(target)])
                self.assertNotEqual(outcome.returncode, 0)
                self.assertIn(b"Operation not permitted", outcome.stderr)
        swap = self.sandboxed(profile, ["/bin/sh", "-c",
            'echo evil > "$2" && mv "$2" "$1"', "sh", str(zshrc), str(self.root / "decoy")])
        self.assertNotEqual(swap.returncode, 0)
        self.assertEqual(zshrc.read_text(), "# user rc\n")
        # A different file in the same HOME stays writable: deny_write is a list, not the home.
        self.assertEqual(self.sandboxed(profile, write + [str(home / ".other")]).returncode, 0)

    def test_tags_and_reflogs_of_the_primary_checkout_cannot_be_written(self):
        profile = self.write_profile(dataclasses.replace(self.perms, deny_read=[]))
        git_dir = self.repo / ".git"
        for parent in ("refs/tags", "refs/remotes", "logs"):
            (git_dir / parent).mkdir(parents=True, exist_ok=True)  # parents exist, so writing the
        write = ["/bin/sh", "-c", 'echo x > "$1"', "sh"]            # leaf is the denied operation
        for target in (git_dir / "refs" / "tags" / "evil", git_dir / "refs" / "remotes" / "x",
                       git_dir / "logs" / "HEAD"):
            with self.subTest(target=str(target.relative_to(git_dir))):
                outcome = self.sandboxed(profile, write + [str(target)])
                self.assertNotEqual(outcome.returncode, 0)
                self.assertIn(b"Operation not permitted", outcome.stderr)

    def test_another_jobs_linked_worktree_admin_dir_cannot_be_written(self):
        mine = self.root / "wt-mine"
        other = self.root / "wt-other"
        sp.git(self.repo, "worktree", "add", "-q", "--detach", str(mine))
        sp.git(self.repo, "worktree", "add", "-q", "--detach", str(other))
        profile = self.write_profile(dataclasses.replace(self.perms, deny_read=[]), worktree=mine)
        other_admin = Path(sp.git(other, "rev-parse", "--absolute-git-dir").stdout.decode().strip())
        write = ["/bin/sh", "-c", 'echo "ref: refs/heads/x" > "$1"', "sh"]
        for target in (other_admin / "HEAD", other_admin / "commondir", other_admin / "index"):
            with self.subTest(target=target.name):
                outcome = self.sandboxed(profile, write + [str(target)])
                self.assertNotEqual(outcome.returncode, 0)
                self.assertIn(b"Operation not permitted", outcome.stderr)
        # My own worktree still commits.
        (mine / "new.txt").write_text("n")
        git = ["/usr/bin/git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false"]
        add = self.sandboxed(profile, git + ["-C", str(mine), "add", "new.txt"])
        commit = self.sandboxed(profile, git + ["-C", str(mine), "commit", "-q", "-m", "mine"])
        self.assertEqual((add.returncode, commit.returncode), (0, 0), (add.stderr, commit.stderr))

    def test_a_refused_hard_link_to_a_protected_file_is_denied_and_attributed(self):
        worktree = self.root / "wt"
        sp.git(self.repo, "worktree", "add", "-q", "--detach", str(worktree))
        secret = self.root / "netrc"
        secret.write_text("machine x password SECRET\n")
        perms = dataclasses.replace(self.perms, deny_read=[str(secret)])
        profile = self.write_profile(perms, worktree=worktree)
        dest = worktree / "hl-netrc"
        outcome = self.sandboxed(profile, ["/bin/ln", str(secret), str(dest)])
        self.assertNotEqual(outcome.returncode, 0)
        self.assertFalse(dest.exists())
        # This ``ln`` names the source (the hidden file), so it is attributed as a read denial;
        # the destination-only case (where only the worktree path is named) is covered offline by
        # ``FindDenials.test_a_refused_hard_link_into_the_worktree_is_a_write_with_cause_unknown``.
        found = permissions.find_denials(outcome.stderr.decode("utf-8", "replace"), perms, self.repo,
                                        worktree=worktree)
        self.assertEqual(found, [{"path": str(secret), "kind": "read", "cause": "deny_read"}],
                         outcome.stderr)

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
        free = self.sandboxed(profile, ["/bin/sh", "-c", 'echo x > "$1"', "sh", str(self.repo / ".git" / "objects" / "scratch")])
        for name, outcome in (("touch", touch), ("cat", cat), ("config", locked)):
            with self.subTest(name=name):
                self.assertNotEqual(outcome.returncode, 0)
                self.assertIn(b"Operation not permitted", outcome.stderr)
        self.assertNotEqual(free.returncode, 0, free.stderr)  # primary objects stay write-denied


@unittest.skipUnless(sys.platform == "darwin" and permissions.wrapper_available(), "Seatbelt only")
class RealGitLayouts(GitLayoutCase):
    """R22 under the real ``sandbox-exec``: each shape of primary checkout keeps its common dir
    (and, for a linked worktree, its own admin dir) read-only while a job worktree still commits."""

    # Every checkout shape runs the same attacks: (label, how to build primary and job).
    def shapes(self):
        if not hasattr(self, "_shapes"):
            sep_repo, _ = self.separate_primary()
            self._shapes = (("plain .git directory", self.repo), ("separate git dir", sep_repo),
                            ("linked worktree", self.linked_primary()))
        return self._shapes

    def job_profile(self, primary, explicit):
        suffix = "explicit" if explicit else "resolved"
        job = self.root / f"job-{primary.name}-{suffix}"
        sp.git(primary, "worktree", "add", "-q", "--detach", str(job))
        layout = lw.worktree_git_layout(job)
        objects = self.root / f"objects-{primary.name}-{suffix}"
        objects.mkdir()
        (objects / "info").mkdir()
        kwargs = {"job_git_admin": layout["GIT_DIR"], "job_worktree": str(job),
                  "allowed_writes": [objects],
                  "protect_write_roots": [str(job), str(objects)],
                  "deny_write_subpaths": [str(objects / "info")]}
        if explicit:
            kwargs["git_common_dir"] = layout["GIT_COMMON_DIR"]
        profile = self.root / f"p-{primary.name}-{suffix}.sb"
        profile.write_text(permissions.seatbelt_profile(
            dataclasses.replace(self.perms, deny_read=[]), primary_repo=primary, **kwargs),
            encoding="utf-8")
        probe = subprocess.run(permissions.wrap_argv(["/usr/bin/true"], profile), capture_output=True)
        if probe.returncode == NESTED_SANDBOX:
            self.skipTest("already inside a sandbox")
        self.assertEqual(probe.returncode, 0, probe.stderr)
        if not hasattr(self, "_object_envs"):
            self._object_envs = {}
        self._object_envs[str(job)] = {
            "GIT_OBJECT_DIRECTORY": str(objects),
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(Path(layout["GIT_COMMON_DIR"]) / "objects"),
        }
        return profile, job, Path(layout["GIT_COMMON_DIR"]), Path(layout["GIT_DIR"])

    def sandboxed(self, profile, argv):
        env = dict(os.environ, **sp.GIT_ID)
        env.update(getattr(self, "_active_object_env", {}) or {})
        return subprocess.run(permissions.wrap_argv(argv, profile), capture_output=True, env=env)

    def refused(self, profile, script, *args):
        outcome = self.sandboxed(profile, ["/bin/sh", "-c", script, "sh", *map(str, args)])
        self.assertNotEqual(outcome.returncode, 0, script)
        self.assertIn(b"Operation not permitted", outcome.stderr)

    def test_the_common_dir_refuses_everything_but_objects_for_every_checkout_shape(self):
        for explicit in (False, True):
            for label, primary in self.shapes():
                with self.subTest(shape=label, explicit=explicit):
                    profile, job, common, admin = self.job_profile(primary, explicit)
                    (common / "hooks").mkdir(exist_ok=True)
                    for target in ("config", "info/attributes", "info/exclude", "hooks/post-checkout",
                                   "MERGE_HEAD", "index.lock", "shallow", "HEAD", "packed-refs",
                                   "refs/heads/planted", "objects/info/alternates", "FETCH_HEAD",
                                   "worktrees/planted"):
                        self.refused(profile, 'echo x > "$1"', common / target)
                    # An existing admin file of someone else is read-only too: this job's own admin
                    # dir is the only entry of worktrees/ that is writable.
                    self.assertEqual(self.sandboxed(profile, ["/bin/sh", "-c", 'echo x >> "$1"', "sh",
                                                              str(admin / "scratch")]).returncode, 0)
                    for redirect in lw.permissions.GIT_ADMIN_REDIRECTS:
                        self.refused(profile, 'echo x > "$1"', admin / redirect)

    def test_the_primarys_own_admin_dir_is_read_only_when_it_is_a_linked_worktree(self):
        primary = self.linked_primary()
        profile, job, common, admin = self.job_profile(primary, explicit=True)
        own = Path(permissions.resolve_git_dirs(primary)[0])
        self.assertNotEqual(own, common)
        for target in ("HEAD", "index", "commondir", "gitdir", "config.worktree"):
            self.refused(profile, 'echo x > "$1"', own / target)
        self.refused(profile, 'echo x > "$1"', primary / "add.py")

    def test_an_objects_info_swap_cannot_plant_alternates(self):
        for label, primary in self.shapes():
            with self.subTest(shape=label):
                profile, job, common, _ = self.job_profile(primary, explicit=True)
                decoy = self.root / ("decoy-" + primary.name)
                decoy.mkdir()
                info = common / "objects" / "info"
                self.refused(profile, 'mv "$1" "$2"', info, str(info) + ".old")
                self.refused(profile, 'rm -rf "$1" && ln -s "$2" "$1"', info, decoy)
                self.refused(profile, 'mv "$1" "$2"', common / "objects", self.root / ("objects-" + primary.name))
                self.refused(profile, 'echo "$2" > "$1"', info / "alternates", decoy)
                self.assertTrue(info.is_dir() and not info.is_symlink())
                self.assertFalse((decoy / "alternates").exists())
                self.assertFalse((info / "alternates").exists())

    def test_the_job_worktree_commits_to_private_objects_for_every_checkout_shape(self):
        for label, primary in self.shapes():
            with self.subTest(shape=label):
                profile, job, common, _ = self.job_profile(primary, explicit=True)
                self._active_object_env = self._object_envs[str(job)]
                before = sorted(str(path.relative_to(common / "objects"))
                                for path in (common / "objects").rglob("*") if path.is_file())
                (job / "new.txt").write_text("n")
                git = ["/usr/bin/git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false"]
                add = self.sandboxed(profile, git + ["-C", str(job), "add", "new.txt"])
                commit = self.sandboxed(profile, git + ["-C", str(job), "commit", "-q", "-m", "from the job"])
                self.assertEqual((add.returncode, commit.returncode), (0, 0), (add.stderr, commit.stderr))
                head = subprocess.run(git + ["-C", str(job), "rev-parse", "HEAD"],
                                      capture_output=True, check=True,
                                      env=dict(os.environ, **sp.GIT_ID, **self._active_object_env)).stdout
                self.assertNotEqual(head,
                                    sp.git(primary, "rev-parse", "HEAD").stdout)
                after = sorted(str(path.relative_to(common / "objects"))
                               for path in (common / "objects").rglob("*") if path.is_file())
                self.assertEqual(after, before)
                self.refused(profile, 'echo x > "$1"', common / "objects" / "zz")
                objects = Path(self._active_object_env["GIT_OBJECT_DIRECTORY"])
                self.assertTrue(any(path.is_file() for path in objects.rglob("*")))
                self.refused(profile, 'echo "$2" > "$1"', objects / "info" / "alternates",
                             common / "objects")
                self._active_object_env = None


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

    def test_deny_write_adds_the_path_and_logs_it(self):
        target = str(self.root / "sensitive")
        code, shown = self.run_cli("deny-write", target)
        self.assertEqual(code, 0)
        self.assertIn(target, shown["deny_write"])
        self.assertIn(target, permissions.load_permissions(self.state).deny_write)
        self.assertEqual([(i["action"], i["path"]) for i in self.log_lines()],
                         [("deny-write", target)])

    def test_allow_write_removes_a_default_vector_either_spelling(self):
        for spelling in ("~/.zshrc", os.path.expanduser("~/.zshrc")):
            with self.subTest(spelling=spelling):
                permissions.save_permissions(self.state, permissions.default_permissions())
                code, shown = self.run_cli("allow-write", spelling)
                self.assertEqual(code, 0)
                self.assertNotIn("~/.zshrc", shown["deny_write"])
                self.assertIn("~/.gitconfig", shown["deny_write"])
        self.assertEqual([i["action"] for i in self.log_lines()], ["allow-write"] * 2)

    def test_allow_write_of_an_unlisted_path_is_refused(self):
        code, refusal = self.run_cli("allow-write", str(self.root / "never"))
        self.assertEqual(code, 2)
        self.assertIn("not in the deny_write list", refusal["failure_reason"])

    def test_show_includes_the_deny_write_defaults(self):
        code, shown = self.run_cli("show")
        self.assertEqual(code, 0)
        self.assertEqual(shown["deny_write"], list(permissions.DEFAULT_DENY_WRITE))

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
