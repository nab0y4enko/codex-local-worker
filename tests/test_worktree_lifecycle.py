"""Recoverable worktree cleanup, against real disposable repositories only."""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support as sp  # noqa: E402

lw = sp.lw
wl = __import__("worktree_lifecycle")  # noqa: E402
ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "src" / "worktree_lifecycle.py"
JOB = "20261024-120000-demo-abc123"


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.root = sp.tmpdir()
        self.addCleanup(sp.cleanup, self.root)
        self.repo = sp.make_repo(self.root)
        self.state = self.root / "state"
        (self.state / "reports").mkdir(parents=True)

    def finish(self, job=JOB, name=None, status="ok", worktree=None):
        """A finished job: linked detached worktree plus its terminal report outside the tree."""
        target = worktree or (self.state / "worktrees" / (name or f"local-worker-{job}"))
        if worktree is None:
            lw.add_worktree(self.repo, lw.rev_parse(self.repo, "main"), target)
        report_dir = self.state / "reports" / job
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "report.json").write_text(json.dumps({
            "job_id": job, "status": status, "worktree": str(target),
            "report_dir": str(report_dir)}), encoding="utf-8")
        return target

    def run_archive(self, job=JOB, repo=None, state=None):
        return subprocess.run([sys.executable, str(HELPER), "archive", "--repo",
                               str(repo or self.repo), "--state-dir", str(state or self.state),
                               "--job-id", job], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True)

    def dirty(self, worktree):
        (worktree / "add.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        (worktree / "untracked-notes.md").write_text("keep me\n", encoding="utf-8")
        (worktree / "build.pyc").write_text("ignored artifact\n", encoding="utf-8")
        (worktree / "__pycache__").mkdir()
        (worktree / "__pycache__" / "cached.pyc").write_text("ignored nested\n", encoding="utf-8")

    def test_dirty_untracked_and_ignored_work_is_recoverable_after_removal(self):
        worktree = self.finish()
        self.dirty(worktree)
        result = self.run_archive()
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["source_worktree"], str(worktree))
        self.assertTrue(receipt["removed"])
        self.assertFalse(worktree.exists())
        self.assertFalse((self.state / "archives" / JOB / "receipt.json").is_symlink())

        ref = receipt["recovery_ref"]
        self.assertTrue(ref.startswith("refs/local-worker/archive/" + JOB))
        self.assertEqual(lw.rev_parse(self.repo, f"{ref}^{{commit}}"), receipt["recovery_commit"])
        self.assertEqual(lw.rev_parse(self.repo, f"{ref}~1^{{commit}}"), receipt["recovery_parent"])
        self.assertIn("return a + b", sp.git(self.repo, "show", f"{ref}:add.py").stdout
                      .decode("utf-8", "replace"))
        listed = sp.git(self.repo, "ls-tree", "-r", "--name-only", ref).stdout.decode()
        for preserved in ("untracked-notes.md", "build.pyc", "__pycache__/cached.pyc"):
            self.assertIn(preserved, listed)
        verify = lw.git(self.repo, "bundle", "verify", receipt["bundle"], check=False,
                          env=sp.GIT_ID)
        self.assertEqual(verify.returncode, 0, verify.stderr)
        archived = Path(receipt["archive"])
        self.assertEqual((archived / "build.pyc").read_text(encoding="utf-8"),
                         "ignored artifact\n")
        self.assertEqual((archived / "add.py").read_text(encoding="utf-8"),
                         "def add(a, b):\n    return a + b\n")
        self.assertFalse((archived / ".git").exists())
        self.assertEqual(Path(receipt["archived_report"]).read_text(encoding="utf-8"),
                         (self.state / "reports" / JOB / "report.json").read_text("utf-8"))
        self.assertEqual(receipt["original_report"],
                         str(self.state / "reports" / JOB / "report.json"))

    def test_preexisting_recovery_ref_is_never_moved(self):
        worktree = self.finish()
        self.dirty(worktree)
        ref = wl.REF_PREFIX + "/" + JOB
        existing = wl.text(lw.git(self.repo, "commit-tree",
                                  lw.rev_parse(self.repo, "main^{tree}"), "-p",
                                  lw.rev_parse(self.repo, "main"), "-m", "preexisting",
                                  env=sp.GIT_ID))
        lw.git(self.repo, "update-ref", ref, existing)
        result = self.run_archive()
        self.assertEqual(result.returncode, wl.Refusal.exit_code, result.stderr)
        self.assertIn("already exists", result.stderr)
        self.assertEqual(lw.rev_parse(self.repo, ref), existing)
        self.assertTrue(worktree.exists())
        self.assertTrue((worktree / "build.pyc").exists())
        self.assertFalse((self.state / "archives" / JOB / "receipt.json").exists())

    def test_staged_only_content_is_recoverable(self):
        """HEAD has A, the index has B, the worktree was reset back to A: B must survive too."""
        worktree = self.finish()
        (worktree / "add.py").write_text("def add(a, b):\n    return a * b\n", encoding="utf-8")
        sp.git(worktree, "add", "add.py")
        (worktree / "add.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
        result = self.run_archive()
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertFalse(worktree.exists())
        self.assertTrue(receipt["index_commit"])
        self.assertIn("a * b", sp.git(self.repo, "show",
                                      f"{receipt['index_commit']}:add.py").stdout.decode())
        self.assertIn("a - b", sp.git(self.repo, "show",
                                      f"{receipt['recovery_commit']}:add.py").stdout.decode())
        self.assertEqual(lw.rev_parse(self.repo, f"{receipt['recovery_ref']}^2^{{commit}}"),
                         receipt["index_commit"])
        self.assertEqual(lw.rev_parse(self.repo, f"{receipt['recovery_ref']}~1^{{commit}}"),
                         receipt["recovery_parent"])

    def test_receipt_write_failure_after_removal_keeps_every_evidence(self):
        worktree = self.finish()
        self.dirty(worktree)
        receipt_path = self.state / "archives" / JOB / "receipt.json"
        persist = wl.write_receipt

        def fail_the_completion(path, receipt):
            if receipt.get("status") == wl.ARCHIVED_STATUS:
                raise OSError("disk full")
            return persist(path, receipt)

        with mock.patch.object(wl, "write_receipt", fail_the_completion):
            with self.assertRaises(OSError):
                wl.archive(self.repo, self.state, JOB)
        self.assertFalse(worktree.exists())
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], wl.PREPARED_STATUS)
        self.assertFalse(receipt["removed"])
        self.assertEqual(lw.rev_parse(self.repo, f"{receipt['recovery_ref']}^{{commit}}"),
                         receipt["recovery_commit"])
        self.assertTrue(Path(receipt["bundle"]).is_file())
        self.assertTrue((Path(receipt["archive"]) / "build.pyc").is_file())
        self.assertEqual((Path(receipt["archive"]) / "build.pyc").read_text(encoding="utf-8"),
                         "ignored artifact\n")
        self.assertIsNone(wl.verify_evidence(self.repo, self.state, JOB, receipt))

        replay = self.run_archive()
        self.assertEqual(replay.returncode, wl.Refusal.exit_code, replay.stderr)
        self.assertIn("removal unrecorded", replay.stderr)
        self.assertEqual(json.loads(receipt_path.read_text(encoding="utf-8"))["status"],
                         wl.PREPARED_STATUS)

    def test_completed_receipt_replays_and_damaged_evidence_is_refused(self):
        worktree = self.finish()
        self.dirty(worktree)
        first = json.loads(self.run_archive().stdout)
        bundle = Path(first["bundle"])
        before = bundle.read_bytes()
        replay = self.run_archive()
        self.assertEqual(replay.returncode, 0, replay.stderr)
        again = json.loads(replay.stdout)
        self.assertTrue(again["removed"])
        self.assertTrue(again["idempotent"])
        self.assertEqual(again["recovery_commit"], first["recovery_commit"])
        self.assertEqual(bundle.read_bytes(), before)
        self.assertFalse(worktree.exists())

        bundle.write_bytes(before + b"\0damaged\0")
        third = self.run_archive()
        self.assertNotEqual(third.returncode, 0)
        self.assertIn("hash no longer matches", third.stderr)
        self.assertEqual(bundle.read_bytes(), before + b"\0damaged\0")
        self.assertFalse(worktree.exists())

    def test_primary_checkout_and_active_lock_are_refused(self):
        primary = self.finish(worktree=self.repo)
        result = self.run_archive()
        self.assertEqual(result.returncode, wl.Refusal.exit_code, result.stderr)
        self.assertIn("primary", result.stderr)
        self.assertTrue((self.repo / ".git").is_dir())
        self.assertFalse((self.state / "archives" / JOB).exists())

        job = "20261024-120100-active-def456"
        worktree = self.finish(job=job)
        holder = subprocess.Popen([sys.executable, "-c",
                                   "import fcntl,sys\n"
                                   "handle = open(sys.argv[1], 'a+')\n"
                                   "fcntl.flock(handle.fileno(), fcntl.LOCK_EX)\n"
                                   "print('locked', flush=True)\n"
                                   "import time; time.sleep(30)\n",
                                   str(self.state / "worker.lock")],
                                  cwd=str(self.root), stdout=subprocess.PIPE, text=True,
                                  env={"PATH": os.environ.get("PATH", "")})
        try:
            self.assertEqual(holder.stdout.readline().strip(), "locked")
            blocked = self.run_archive(job=job)
            self.assertEqual(blocked.returncode, wl.Blocked.exit_code, blocked.stderr)
            self.assertIn("worker.lock", blocked.stderr)
            self.assertTrue(worktree.exists())
            self.assertFalse((self.state / "archives" / job).exists())
        finally:
            holder.stdout.close()
            holder.terminate()
            holder.wait(timeout=10)

    def test_unrelated_malformed_and_running_targets_are_refused(self):
        kept = self.finish(job="20261024-120150-keep-jkl012")
        outside = self.root / "outside-worktree"
        lw.add_worktree(self.repo, lw.rev_parse(self.repo, "main"), outside)
        other = sp.make_repo(self.root / "elsewhere")
        foreign = self.state / "worktrees" / "foreign-tree"
        lw.add_worktree(other, lw.rev_parse(other, "main"), foreign)
        os.symlink(str(outside), str(self.state / "worktrees" / "link"))
        unregistered = self.state / "worktrees" / "plain-directory"
        unregistered.mkdir(parents=True, exist_ok=True)
        # Each case owns a job id and a report, and is checked before the next case is written,
        # so no refusal is ever judged against a report that a later case overwrote.
        cases = [
            ("20261024-120200-traversal-mno345", self.state / "worktrees" / ".." / "evil",
             "ok", "direct child"),
            ("20261024-120201-unrelated-pqr678", outside, "ok", "direct child"),
            ("20261024-120202-unregistered-stu901", unregistered, "ok", "registered worktree"),
            ("20261024-120203-foreign-vwx234", foreign, "ok", "registered worktree"),
            ("20261024-120204-running-yza567", None, "running", "not terminal"),
            ("20261024-120205-symlink-bcd890", self.state / "worktrees" / "link", "ok", "symlink"),
        ]
        for job, target, status, needle in cases:
            with self.subTest(needle):
                self.finish(job=job, worktree=target, status=status)
                with self.assertRaises(wl.LifecycleError) as caught:
                    wl.archive(self.repo, self.state, job)
                self.assertIn(needle, str(caught.exception))
                self.assertTrue(kept.exists())
                self.assertTrue(outside.exists())
                self.assertTrue(foreign.exists())
                self.assertTrue(other.exists())
                self.assertTrue(self.repo.exists())
                self.assertFalse((self.state / "archives" / job).exists())
        malformed = self.run_archive(job="../evil")
        self.assertEqual(malformed.returncode, wl.Refusal.exit_code, malformed.stderr)
        self.assertIn("path component", malformed.stderr)

    @unittest.skipIf(os.geteuid() == 0, "unreadable files are readable for root")
    def test_archive_failure_preserves_worktree_and_writes_no_receipt(self):
        worktree = self.finish()
        self.dirty(worktree)
        blocked = worktree / "secret.bin"
        blocked.write_text("do not copy\n", encoding="utf-8")
        os.chmod(str(blocked), 0)
        self.addCleanup(os.chmod, str(blocked), 0o644)
        result = self.run_archive()
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(worktree.exists())
        self.assertTrue((worktree / "build.pyc").exists())
        self.assertFalse((self.state / "archives" / JOB / "receipt.json").exists())
        self.assertEqual(lw.rev_parse(self.repo, "HEAD"),
                         lw.rev_parse(self.repo, "main"))
        self.assertTrue(sp.git(self.repo, "worktree", "list").stdout.decode().find(str(worktree))
                        >= 0)


    # --------------------------------------------------------------------------------------
    # index_snapshot fails closed: no snapshot of the staged index means no removal
    # --------------------------------------------------------------------------------------
    def index_path(self, worktree):
        """The real index of a linked worktree, reached through its ``.git`` pointer file."""
        pointer = (worktree / ".git").read_text(encoding="utf-8")
        self.assertTrue(pointer.startswith("gitdir:"), pointer)
        return Path(pointer.split(":", 1)[1].strip()) / "index"

    def index_state(self, worktree):
        """(raw index bytes, staged entries) of the worker's own index, once git is quiescent.

        The first ``write-tree`` stores git's own cache-tree extension into the index; after
        that one bookkeeping write the file is byte-stable, so any later difference is a change
        made by the helper under test rather than by git refreshing itself.
        """
        lw.git(worktree, "write-tree", check=False, env=sp.GIT_ID)
        entries = lw.git(worktree, "ls-files", "--stage", "--abbrev=40", env=sp.GIT_ID).stdout
        return self.index_path(worktree).read_bytes(), entries

    def assert_preserved(self, worktree, job, index_state):
        """Nothing was removed, no evidence was published, and the index bytes are untouched."""
        self.assertTrue(worktree.exists())
        self.assertTrue((worktree / "build.pyc").is_file())
        self.assertTrue((worktree / "untracked-notes.md").is_file())
        self.assertEqual(self.index_state(worktree), index_state)
        self.assertIn(wl.canonical(worktree), wl.registered_worktrees(self.repo))
        archive_dir = self.state / "archives" / job
        self.assertFalse((archive_dir / "receipt.json").exists())
        self.assertFalse(archive_dir.exists() and any(archive_dir.iterdir()))
        self.assertEqual(wl.text(lw.git(self.repo, "show-ref", "--verify", "--quiet",
                                        wl.REF_PREFIX + "/" + job, check=False)), "")

    def unmerge(self, worktree):
        """Leave unmerged stage entries in the worker's real index, as a conflicted merge does."""
        oids = [wl.text(lw.git(worktree, "hash-object", "-w", "--stdin", stdin=content,
                               env=sp.GIT_ID))
                for content in (b"base\n", b"ours\n", b"theirs\n")]
        staged = "".join(f"100644 {oid} {stage}\tadd.py\n"
                         for stage, oid in enumerate(oids, start=1))
        lw.git(worktree, "update-index", "--index-info", stdin=staged.encode(), env=sp.GIT_ID)
        unmerged = lw.git(worktree, "ls-files", "--unmerged", env=sp.GIT_ID).stdout.splitlines()
        self.assertEqual(sorted(line.split()[-1] for line in unmerged),
                         [b"add.py", b"add.py", b"add.py"])

    def test_unmerged_index_refuses_archive_and_preserves_worktree_and_index(self):
        """An unmerged index makes ``write-tree`` fail, so the archive stops instead of dropping
        staged content, leaving worktree and index byte for byte as they were."""
        worktree = self.finish()
        self.dirty(worktree)
        self.unmerge(worktree)
        before = self.index_state(worktree)
        self.assertIn(b"write-tree", lw.git(worktree, "write-tree", check=False,
                                            env=sp.GIT_ID).stderr)
        result = self.run_archive()
        self.assertEqual(result.returncode, wl.LifecycleError.exit_code, result.stderr)
        self.assertIn("write-tree", result.stderr)
        self.assertIn("failed", result.stderr)
        self.assert_preserved(worktree, JOB, before)

    def failing_git(self, on, replacement):
        """Answer git calls that ``on`` selects with ``replacement``; delegate all the others."""
        real = lw.git

        def run(repo, *args, **kwargs):
            if on(repo, args):
                return subprocess.CompletedProcess(["git", *args], replacement.returncode,
                                                   replacement.stdout, replacement.stderr)
            return real(repo, *args, **kwargs)

        return run

    def test_index_snapshot_failures_refuse_archive_without_removal(self):
        """HEAD, write-tree and commit-tree are each checked for returncode and a real oid."""
        broken = subprocess.CompletedProcess
        cases = [
            ("head", "rev-parse", ("HEAD",),
             broken(["git"], 1, b"", b"fatal: ambiguous argument 'HEAD'\n"),
             wl.Refusal, "HEAD"),
            ("write-tree-fails", "write-tree", (),
             broken(["git"], 128, b"", b"fatal: git-write-tree: error building trees\n"),
             wl.LifecycleError, "write-tree"),
            ("write-tree-garbage", "write-tree", (),
             broken(["git"], 0, b"not-an-object-id\n", b""),
             wl.LifecycleError, "write-tree"),
            ("commit-tree-fails", "commit-tree", (),
             broken(["git"], 1, b"", b"error: unable to write commit object\n"),
             wl.LifecycleError, "commit-tree"),
            ("commit-tree-garbage", "commit-tree", (),
             broken(["git"], 0, b"0000\n", b""),
             wl.LifecycleError, "commit-tree"),
        ]
        for position, (name, command, extra, replacement, expected, needle) in enumerate(cases):
            job = f"20261024-1210{position:02d}-snapshot-{name}"
            with self.subTest(name):
                worktree = self.finish(job=job)
                self.dirty(worktree)
                # staged content that differs from HEAD, so the second parent is really needed
                (worktree / "add.py").write_text("def add(a, b):\n    return a * b\n",
                                                 encoding="utf-8")
                lw.git(worktree, "add", "add.py", env=sp.GIT_ID)
                before = self.index_state(worktree)

                def matches(repo, args, command=command, extra=extra, worktree=worktree):
                    if not args or args[0] != command or not all(item in args for item in extra):
                        return False
                    return wl.canonical(repo) == wl.canonical(worktree)

                with mock.patch.object(lw, "git", self.failing_git(matches, replacement)):
                    with self.assertRaises(wl.LifecycleError) as caught:
                        wl.archive(self.repo, self.state, job)
                self.assertIsInstance(caught.exception, expected)
                self.assertIn(needle, str(caught.exception))
                self.assert_preserved(worktree, job, before)


class DirectoryLinkInventory(unittest.TestCase):
    """Directory symlinks are payload: recorded as links, never traversed, always verified."""

    def setUp(self):
        self.root = sp.tmpdir()
        self.addCleanup(sp.cleanup, self.root)
        self.repo = sp.make_repo(self.root)
        self.worktree = self.root / "state" / "worktrees" / "links"
        lw.add_worktree(self.repo, lw.rev_parse(self.repo, "main"), self.worktree)
        self.external = self.root / "external"
        (self.external / "nested").mkdir(parents=True)
        (self.external / "payload.txt").write_text("outside\n", encoding="utf-8")
        (self.external / "nested" / "deep.txt").write_text("deeper\n", encoding="utf-8")
        os.symlink(str(self.external), str(self.worktree / "linked-dir"))
        self.archive = self.root / "archive"

    def test_directory_link_is_archived_and_verified(self):
        inventory = wl.walk_files(self.worktree)
        self.assertEqual(inventory.get("linked-dir"), "link")
        wl.copy_worktree(self.worktree, self.archive)
        digest, count = wl.verify_archive(self.worktree, self.archive)
        self.assertTrue(os.path.islink(str(self.archive / "linked-dir")))
        self.assertEqual(wl.recompute_archive(self.archive), (digest, count))

    def test_nested_directory_link_is_recorded_and_verified(self):
        (self.worktree / "pack").mkdir()
        os.symlink(str(self.external), str(self.worktree / "pack" / "inner"))
        wl.copy_worktree(self.worktree, self.archive)
        self.assertEqual(wl.verify_archive(self.worktree, self.archive),
                         wl.recompute_archive(self.archive))
        self.assertEqual(wl.walk_files(self.archive).get("pack/inner"), "link")
        self.assertTrue(os.path.islink(str(self.archive / "pack" / "inner")))

    def test_walk_does_not_traverse_the_link_target(self):
        wl.copy_worktree(self.worktree, self.archive)
        for inventory in (wl.walk_files(self.worktree), wl.walk_files(self.archive)):
            self.assertIn("linked-dir", inventory)
            self.assertEqual({name for name in inventory if name.startswith("linked-dir/")},
                             set())
            self.assertNotIn("payload.txt", inventory)
            self.assertNotIn("nested/deep.txt", inventory)

    def test_missing_directory_link_is_detected(self):
        wl.copy_worktree(self.worktree, self.archive)
        digest, count = wl.recompute_archive(self.archive)
        os.unlink(str(self.archive / "linked-dir"))
        with self.assertRaises(wl.LifecycleError) as caught:
            wl.verify_archive(self.worktree, self.archive)
        self.assertIn("linked-dir", str(caught.exception))
        after, total = wl.recompute_archive(self.archive)
        self.assertNotEqual(after, digest)
        self.assertEqual(total, count - 1)

    def test_retargeted_directory_link_is_detected(self):
        other = self.root / "other"
        other.mkdir()
        (other / "impostor.txt").write_text("nope\n", encoding="utf-8")
        wl.copy_worktree(self.worktree, self.archive)
        digest, count = wl.recompute_archive(self.archive)
        os.unlink(str(self.archive / "linked-dir"))
        os.symlink(str(other), str(self.archive / "linked-dir"))
        with self.assertRaises(wl.LifecycleError) as caught:
            wl.verify_archive(self.worktree, self.archive)
        self.assertIn("linked-dir", str(caught.exception))
        self.assertEqual(wl.recompute_archive(self.archive)[1], count)
        self.assertNotEqual(wl.recompute_archive(self.archive)[0], digest)

    def test_only_the_root_git_metadata_is_excluded(self):
        inventory = wl.walk_files(self.worktree)
        self.assertFalse(any(name == ".git" or name.startswith(".git/") for name in inventory))
        (self.worktree / "fixture-repo").mkdir()
        (self.worktree / "fixture-repo" / ".git").write_text("gitdir: /somewhere/.git\n",
                                                            encoding="utf-8")
        self.assertEqual(wl.walk_files(self.worktree).get("fixture-repo/.git"), "file")
        wl.copy_worktree(self.worktree, self.archive)
        digest, _ = wl.verify_archive(self.worktree, self.archive)
        (self.archive / "fixture-repo" / ".git").write_text("gitdir: /elsewhere/.git\n",
                                                           encoding="utf-8")
        with self.assertRaises(wl.LifecycleError) as caught:
            wl.verify_archive(self.worktree, self.archive)
        self.assertIn("fixture-repo/.git", str(caught.exception))
        self.assertNotEqual(wl.recompute_archive(self.archive)[0], digest)


if __name__ == "__main__":
    unittest.main()
