"""Post-run audit and primary-checkout fingerprint, against real disposable repos."""
import sys
import unittest
import subprocess
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support as sp  # noqa: E402

lw = sp.lw
ALLOWED = ["add.py", "pkg/"]
FIX = "def add(a, b):\n    return a + b\n"


class Audit(unittest.TestCase):
    def setUp(self):
        self.root = sp.tmpdir()
        self.addCleanup(sp.cleanup, self.root)
        self.repo = sp.make_repo(self.root)
        self.base = lw.rev_parse(self.repo, "main")

    def audit(self, allowed=ALLOWED):
        return lw.audit_worktree(self.repo, self.base, list(allowed), [sp.SECRET],
                                 scratch_dir=self.root / "scratch")

    def test_in_scope_unstaged_edit_is_clean_and_produces_a_patch(self):
        (self.repo / "add.py").write_text(FIX)
        (self.repo / "pkg").mkdir()
        (self.repo / "pkg" / "new.py").write_text("NEW = 1\n")
        audit = self.audit()
        self.assertEqual(audit.changed_paths, ["add.py", "pkg/new.py"])
        self.assertEqual(audit.violations, [])
        self.assertFalse(audit.head_changed)
        self.assertIn("+    return a + b", audit.patch.decode())
        self.assertIn("add.py", audit.diffstat)
        self.assertEqual(lw.changed_paths(self.repo), ["add.py", "pkg/new.py"])

    def test_unstaged_and_untracked_edits_outside_allowed_paths_are_violations(self):
        (self.repo / "notes.md").write_text("sneaky\n")
        (self.repo / "extra.txt").write_text("dropped file\n")
        audit = self.audit()
        self.assertEqual([violation["path"] for violation in audit.violations],
                         ["extra.txt", "notes.md"])
        self.assertTrue(all(violation["reason"] == "outside allowed_paths"
                            for violation in audit.violations))

    def test_staged_changes_are_detected(self):
        (self.repo / "notes.md").write_text("staged\n")
        sp.git(self.repo, "add", "notes.md")
        audit = self.audit()
        self.assertIn("notes.md", audit.changed_paths)
        self.assertEqual([v["path"] for v in audit.violations], ["notes.md"])
        self.assertIn("M", {entry["status"].strip() for entry in audit.entries
                            if entry["path"] == "notes.md"})

    def test_rename_reports_both_sides(self):
        sp.git(self.repo, "mv", "notes.md", "moved.md")
        audit = self.audit(allowed=["moved.md"])
        self.assertEqual(audit.changed_paths, ["moved.md", "notes.md"])
        roles = {entry["path"]: entry.get("role") for entry in audit.entries if entry.get("role")}
        self.assertEqual(roles, {"notes.md": "rename-source", "moved.md": "rename-destination"})
        self.assertEqual([v["path"] for v in audit.violations], ["notes.md"])

    def test_worker_commit_is_reported_and_preserved_never_reset(self):
        (self.repo / "notes.md").write_text("committed by the worker\n")
        sp.git(self.repo, "add", "-A")
        sp.git(self.repo, "commit", "-q", "-m", "worker commit")
        audit = self.audit()
        self.assertTrue(audit.head_changed)
        self.assertNotEqual(audit.head_commit, self.base)
        self.assertEqual(lw.changed_paths(self.repo), [])      # tree looks clean ...
        self.assertEqual(audit.changed_paths, ["notes.md"])    # ... yet the audit sees it
        reasons = " ".join(violation["reason"] for violation in audit.violations)
        self.assertIn("moved HEAD", reasons)
        self.assertIn("outside allowed_paths", reasons)
        self.assertEqual(audit.head_commit, lw.rev_parse(self.repo, "HEAD"))

    def test_ignored_build_output_is_not_a_violation(self):
        (self.repo / "__pycache__").mkdir()
        (self.repo / "__pycache__" / "add.cpython-39.pyc").write_text("junk\n")
        (self.repo / "stale.pyc").write_text("junk\n")
        audit = self.audit()
        self.assertEqual(audit.ignored_paths, ["__pycache__", "stale.pyc"])
        self.assertEqual(audit.changed_paths, [])
        self.assertEqual(audit.violations, [])

    def test_symlink_leaving_the_worktree_is_a_violation(self):
        (self.root / "outside").mkdir()
        (self.repo / "pkg").mkdir()
        (self.repo / "pkg" / "escape").symlink_to(self.root / "outside")
        audit = self.audit()
        self.assertEqual([v["path"] for v in audit.violations], ["pkg/escape"])
        self.assertIn("symlink escape", audit.violations[0]["reason"])

    def test_audit_scratch_dir_never_lives_inside_the_worktree(self):
        self.assertNotEqual(lw.audit_scratch_dir(self.repo, self.repo), self.repo)
        self.assertFalse(lw._inside(lw.audit_scratch_dir(self.repo, self.repo), self.repo))
        self.assertFalse(lw._inside(lw.audit_scratch_dir(self.repo, None), self.repo))

    def test_changed_base_gitlink_is_reported_without_traversing_nested_repo(self):
        nested = self.root / "nested"
        sp.git(self.repo, "init", "-q", str(nested))
        sp.git(nested, "config", "user.email", "worker@test")
        sp.git(nested, "config", "user.name", "Worker")
        (nested / "nested.txt").write_text("base\n")
        sp.git(nested, "add", "nested.txt")
        sp.git(nested, "commit", "-q", "-m", "nested")
        nested_head = lw.rev_parse(nested, "HEAD")
        sp.git(self.repo, "update-index", "--add", "--cacheinfo", "160000", nested_head, "submod")
        sp.git(self.repo, "commit", "-q", "-m", "gitlink")
        base = lw.rev_parse(self.repo, "HEAD")
        (self.repo / "submod").mkdir()
        result = lw.audit_worktree(self.repo, base, ["submod"], [], scratch_dir=self.root / "scratch")
        self.assertIn("submod", result.changed_paths)
        self.assertTrue(any(entry.get("role") == "gitlink_uninspected" for entry in result.entries))
        self.assertTrue(any("not traversed" in item["reason"] for item in result.violations))


    def test_private_objects_are_validated_in_isolation_then_promoted_for_archive(self):
        (self.root / "report").mkdir()
        layout = lw.worktree_git_layout(self.repo)
        layout.update(lw.prepare_job_object_store(self.root / "report", self.repo))
        env = dict(layout)
        (self.repo / "private.txt").write_text("private commit\n")
        self.assertEqual(lw.git(self.repo, "add", "private.txt", env=env).returncode, 0)
        self.assertEqual(lw.git(self.repo, "-c", "user.name=Worker", "-c",
                                "user.email=worker@test", "commit", "-q", "-m",
                                "private", env=env).returncode, 0)
        head = lw.git(self.repo, "rev-parse", "HEAD", env=env).stdout.decode().strip()
        primary_env = dict(layout)
        primary_env.pop("GIT_OBJECT_DIRECTORY", None)
        primary_env.pop("GIT_ALTERNATE_OBJECT_DIRECTORIES", None)
        self.assertNotEqual(lw.git(self.repo, "cat-file", "-e", head, check=False,
                                   env=primary_env).returncode, 0)
        oids = lw.validate_job_object_store(self.repo, layout)
        self.assertIn(head, oids)
        self.assertEqual(lw.promote_job_object_store(self.repo,
                         Path(layout["GIT_OBJECT_DIRECTORY"])), oids)
        self.assertEqual(lw.git(self.repo, "cat-file", "-e", head, env=primary_env).returncode, 0)

    def test_private_object_hash_is_checked_before_parent_alternate_can_resolve_it(self):
        (self.root / "report").mkdir()
        layout = lw.worktree_git_layout(self.repo)
        layout.update(lw.prepare_job_object_store(self.root / "report", self.repo))
        oid = lw.rev_parse(self.repo, "HEAD")
        object_dir = Path(layout["GIT_OBJECT_DIRECTORY"])
        path = object_dir / oid[:2] / oid[2:]
        path.parent.mkdir(parents=True)
        path.write_bytes(zlib.compress(b"commit 5\0evil!"))
        with self.assertRaisesRegex(lw.GitError, "object|enumerated|hash"):
            lw.validate_job_object_store(self.repo, layout)


class PrimaryFingerprint(unittest.TestCase):
    def setUp(self):
        self.root = sp.tmpdir()
        self.addCleanup(sp.cleanup, self.root)
        self.repo = sp.make_repo(self.root)

    def names(self, fingerprint):
        return sorted(entry[1] for entry in fingerprint["entries"])

    def test_clean_checkout_fingerprint_is_stable(self):
        first = lw.primary_fingerprint(self.repo)
        self.assertEqual(first["entries"], [])
        self.assertEqual(first["head"], lw.rev_parse(self.repo, "main"))
        self.assertEqual(first, lw.primary_fingerprint(self.repo))

    def test_dirty_names_can_hide_changed_contents(self):
        (self.repo / "scratch.txt").write_text("dirty before the job\n")
        (self.repo / "add.py").write_text("def add(a, b):\n    return a - b  # local edit\n")
        before = lw.primary_fingerprint(self.repo)
        self.assertEqual(sorted(self.names(before)), ["add.py", "scratch.txt"])
        self.assertEqual(lw.dirty_primary_files(self.repo), ["add.py", "scratch.txt"])
        (self.repo / "scratch.txt").write_text("touched during the job\n")
        (self.repo / "add.py").write_text("def add(a, b):\n    return a * b\n")
        after = lw.primary_fingerprint(self.repo)
        self.assertEqual(self.names(before), self.names(after))       # names alone: unchanged
        self.assertEqual(before["head"], after["head"])
        self.assertEqual(before["index_digest"], after["index_digest"])
        self.assertNotEqual(before, after)                            # contents: changed
        self.assertEqual(after["contents"]["scratch.txt"], lw._digest_path(self.repo / "scratch.txt"))
        (self.repo / "notes.md").write_text("staged during the job\n")
        sp.git(self.repo, "add", "notes.md")
        self.assertNotEqual(after["index_digest"], lw.primary_fingerprint(self.repo)["index_digest"])


if __name__ == "__main__":
    unittest.main()
