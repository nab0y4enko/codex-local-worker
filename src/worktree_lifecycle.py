#!/usr/bin/env python3
"""Parent-operated, recoverable cleanup of one finished local-worker worktree.

The parent owns integration, so a finished job's linked worktree is disposable only once
every byte of it is recoverable somewhere else. This helper therefore archives first and
removes second, and refuses loudly instead of touching anything it does not own:

* the job needs a terminal ``<state-dir>/reports/<job-id>/report.json`` whose ``worktree``
  is one direct child of ``<state-dir>/worktrees``, registered with the repository's common
  git directory - never the primary checkout, never a symlink, never an unrelated path;
* ``<state-dir>/worker.lock`` is held exclusively and non-blocking for the whole operation,
  so a running default job or any parallel job blocks the removal;
* committed, dirty, untracked and ignored content is preserved on a recovery ref through a
  snapshot commit whose second parent carries the staged index, plus a verified ``git bundle``
  and a hash-verified copy of the worktree; the receipt is written as ``prepared`` before
  removal and only becomes ``archived`` once the worktree is really gone;
* only then runs ``git worktree remove`` for that one path. Nothing is force-removed before
  verification, no other path is deleted, and an existing archive is never overwritten.

Usage: python3 src/worktree_lifecycle.py archive --repo PATH --state-dir PATH --job-id ID
Stdlib only. Exit codes: 0 archived (or idempotent replay), 2 refusal, 3 blocked, 4 failed.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import local_worker as lw  # noqa: E402  (git helpers, terminal status table)
import worker_pool as pool  # noqa: E402  (parallel slot leases share the same state dir)

JOB_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}")
REF_PREFIX = "refs/local-worker/archive"
RECEIPT_NAME = "receipt.json"
BUNDLE_NAME = "recovery.bundle"
FILES_NAME = "files"
REPORT_NAME = "report.json"
PREPARED_STATUS = "prepared"
ARCHIVED_STATUS = "archived"
# ``update-ref <ref> <new> <old>`` with the all-zero oid means "this ref must not exist".
ZERO_OID = "0" * 40
# Any git object id we accept as evidence must be a full lowercase sha1, never prose or empty.
OID = re.compile(r"[0-9a-f]{40}")
INDEX_NAME = "staged.index"
# A dirty tree may only be force-removed while all of this evidence is present and verified.
REQUIRED_EVIDENCE = ("recovery_ref", "recovery_commit", "index_commit", "bundle",
                     "bundle_sha256", "archive", "archive_sha256", "archived_files")
# A snapshot commit needs an identity, and it must not depend on whoever ran the cleanup.
SNAPSHOT_IDENTITY = {
    "GIT_AUTHOR_NAME": "local-worker-archive",
    "GIT_AUTHOR_EMAIL": "local-worker-archive@localhost",
    "GIT_COMMITTER_NAME": "local-worker-archive",
    "GIT_COMMITTER_EMAIL": "local-worker-archive@localhost",
}
# ``lw.EXIT_CODES`` is exactly the set of terminal report statuses the launcher can write.
TERMINAL_STATUSES = frozenset(lw.EXIT_CODES)

EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_BLOCKED = 3
EXIT_FAILED = 4


class LifecycleError(Exception):
    """Cleanup did not happen; the worktree is exactly as it was."""

    exit_code = EXIT_FAILED


class Refusal(LifecycleError):
    """The request itself is not acceptable (wrong owner, active job, existing archive)."""

    exit_code = EXIT_REFUSED


class Blocked(LifecycleError):
    """Another worker holds the state directory."""

    exit_code = EXIT_BLOCKED


def canonical(path: Any) -> str:
    """Absolute real path: symlinks and ``..`` cannot smuggle another directory in."""
    return os.path.realpath(os.path.abspath(str(path)))


def text(process: Any) -> str:
    return process.stdout.decode("utf-8", "replace").strip()


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(str(path), "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def common_dir(path: Path) -> str:
    """Common git directory of a checkout: shared by every linked worktree of a repository."""
    process = lw.git(path, "--no-optional-locks", "rev-parse", "--git-common-dir", check=False)
    if process.returncode != 0:
        raise Refusal(f"{path} is not a git worktree")
    raw = text(process)
    if not raw or "\n" in raw:
        raise Refusal(f"cannot determine the git common directory of {path}")
    return canonical(raw if os.path.isabs(raw) else Path(path) / raw)


def registered_worktrees(repo: Path) -> List[str]:
    """Every worktree path registered in this repository's common directory."""
    process = lw.git(repo, "--no-optional-locks", "worktree", "list", "--porcelain", check=False)
    if process.returncode != 0:
        raise Refusal(f"cannot list worktrees of {repo}: {text(process)}")
    paths = []
    for line in process.stdout.decode("utf-8", "replace").splitlines():
        if line.startswith("worktree "):
            paths.append(canonical(line[len("worktree "):]))
    return paths


# --------------------------------------------------------------------------------------
# Target validation: exactly one linked worktree of this repository, owned by this state dir
# --------------------------------------------------------------------------------------
def read_report(state_dir: Path, job_id: str) -> Dict[str, Any]:
    if not JOB_ID_PATTERN.fullmatch(job_id) or job_id in (".", ".."):
        raise Refusal(f"job id {job_id!r} is not a single safe path component")
    report_path = state_dir / "reports" / job_id / REPORT_NAME
    if not report_path.is_file():
        raise Refusal(f"no report at {report_path}: the job was never finished here")
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise Refusal(f"report {report_path} is not readable JSON: {error}") from error
    if not isinstance(report, dict):
        raise Refusal(f"report {report_path} is not a JSON object")
    if str(report.get("job_id") or "") != job_id:
        raise Refusal(f"report {report_path} is for job {report.get('job_id')!r}, not {job_id!r}")
    status = str(report.get("status") or "")
    if status not in TERMINAL_STATUSES:
        raise Refusal(f"job {job_id} is not terminal (status {status or 'missing'!r}); "
                      "a running worker's worktree is never archived")
    return report


def resolve_worktree(repo: Path, state_dir: Path, report: Dict[str, Any], *,
                     must_exist: bool = True) -> Tuple[Path, str]:
    """The report's worktree, as a canonical path this state directory and repo own.

    ``must_exist=False`` keeps the ownership checks for an already-removed path, so a durable
    receipt can be replayed without pretending the worktree is still there.
    """
    declared = report.get("worktree")
    if not isinstance(declared, str) or not declared.strip():
        raise Refusal("report has no worktree path to archive")
    candidate = Path(declared)
    if candidate.is_symlink():
        raise Refusal(f"{candidate} is a symlink; only a real worktree directory is removable")
    target = canonical(candidate)
    repo_root = canonical(repo)
    if target == repo_root or target.startswith(repo_root + os.sep):
        raise Refusal(f"{target} is the primary checkout of {repo}, not a worker worktree")
    root = canonical(state_dir / "worktrees")
    if os.path.dirname(target) != root:
        raise Refusal(f"{target} is not a direct child of {root}: unrelated paths are never "
                      "archived")
    primary = registered_worktrees(repo)
    if target == repo_root or (primary and target == primary[0]):
        raise Refusal(f"{target} is the main worktree of {repo}")
    report_path = canonical(state_dir / "reports" / str(report.get("job_id")) / REPORT_NAME)
    if report_path == target or report_path.startswith(target + os.sep):
        raise Refusal("the original report must stay outside the worktree")
    if must_exist:
        if not os.path.isdir(target):
            raise Refusal(f"{target} no longer exists; nothing to archive")
        if target not in primary:
            raise Refusal(f"{target} is not a registered worktree of {repo}")
        if common_dir(Path(target)) != common_dir(repo):
            raise Refusal(f"{target} does not belong to the git common directory of {repo}")
    return Path(target), declared


# --------------------------------------------------------------------------------------
# Recovery evidence: snapshot ref, verified bundle, hash-verified copy
# --------------------------------------------------------------------------------------
def snapshot_commit(worktree: Path, scratch: Path, message: str, *,
                    index_commit: Optional[str] = None) -> Tuple[str, str]:
    """Commit the whole worktree - tracked edits, untracked and ignored files - on top of HEAD.

    The real index is never touched: the snapshot lives in a private temporary index, and the
    resulting object goes to the shared object directory, so it outlives the worktree.
    ``index_commit``, when given, becomes a second parent so that one ref and one bundle carry
    both the working tree and the staged content that ``write-tree`` captured separately.
    """
    head = lw.rev_parse(worktree, "HEAD")
    if not head:
        raise Refusal(f"{worktree} has no HEAD commit to build recovery history on")
    scratch.mkdir(parents=True, exist_ok=True)
    index = scratch / "snapshot.index"
    if index.exists():
        index.unlink()
    env = dict(SNAPSHOT_IDENTITY)
    env["GIT_INDEX_FILE"] = str(index)
    lw.git(worktree, "read-tree", "HEAD", env=env)
    lw.git(worktree, "add", "--force", "--all", "--", ".", env=env)
    # Names come from git itself and are matched literally, so spaces and glob characters in
    # untracked or ignored paths cannot widen the scope of the snapshot.
    paths = [path for status, path in lw.status_entries(worktree, ignored=True)
             if status in ("??", "!!")]
    if paths:
        payload = b"".join((":(literal)" + path).encode("utf-8", "surrogateescape") + b"\0"
                           for path in paths)
        lw.git(worktree, "add", "--force", "--pathspec-from-file=-", "--pathspec-file-nul",
               env=env, stdin=payload)
    tree = text(lw.git(worktree, "write-tree", env=env))
    if not tree:
        raise LifecycleError("could not write the recovery tree for " + str(worktree))
    arguments = ["commit-tree", tree, "-p", head]
    if index_commit and index_commit != head:
        arguments += ["-p", index_commit]
    arguments += ["-m", message]
    commit = text(lw.git(worktree, *arguments, env=env))
    if not commit:
        raise LifecycleError("could not create the recovery snapshot commit")
    with contextlib.suppress(OSError):
        index.unlink()
    return commit, head


def index_snapshot(worktree: Path, message: str) -> Optional[str]:
    """Commit the worker's real index so staged-but-never-committed content survives removal.

    ``write-tree`` reads the index that the worker itself left behind; when that tree differs
    from ``HEAD`` the commit is kept as the second parent of the snapshot, so one ref and one
    bundle carry both the working tree and the staged content. The fixed identity keeps the
    object independent of whoever ran the cleanup.

    Fail closed: ``None`` means only that the index was read successfully and its tree already
    equals ``HEAD``'s tree, so there is nothing extra to preserve. An unresolvable HEAD, a
    ``write-tree`` that fails or answers with anything but an object id (an unmerged index does
    exactly that), an unresolvable HEAD tree, and a ``commit-tree`` that fails or answers with
    anything but an object id all abort the archive before anything is removed - a missing
    second parent would silently drop staged content that no other artifact carries.
    """
    resolved = lw.git(worktree, "rev-parse", "--verify", "--quiet", "HEAD", check=False)
    head = text(resolved)
    if resolved.returncode != 0 or not OID.fullmatch(head):
        raise Refusal(f"{worktree} has no resolvable HEAD commit to attach a staged-index "
                      "snapshot to")
    written = lw.git(worktree, "write-tree", check=False)
    if written.returncode != 0 or not OID.fullmatch(text(written)):
        detail = (written.stderr.decode("utf-8", "replace").strip() or text(written)
                    or "no detail")
        raise LifecycleError(f"could not snapshot the staged index of {worktree}: git "
                             f"write-tree failed (exit {written.returncode}): {detail}")
    tree = text(written)
    head_tree = lw.git(worktree, "rev-parse", "--verify", "--quiet", f"{head}^{{tree}}",
                       check=False)
    if head_tree.returncode != 0 or not OID.fullmatch(text(head_tree)):
        raise LifecycleError(f"could not read the tree of {head} in {worktree}, so the staged "
                             "index cannot be compared with HEAD")
    if tree == text(head_tree):
        return None
    committed = lw.git(worktree, "commit-tree", tree, "-p", head, "-m", message,
                       env=dict(SNAPSHOT_IDENTITY), check=False)
    if committed.returncode != 0 or not OID.fullmatch(text(committed)):
        detail = (committed.stderr.decode("utf-8", "replace").strip() or text(committed)
                    or "no detail")
        raise LifecycleError(f"could not commit the staged index tree {tree} of {worktree}: "
                             f"git commit-tree failed (exit {committed.returncode}): {detail}")
    return text(committed)


def create_bundle(repo: Path, ref: str, destination: Path) -> Path:
    """Write and verify a bundle before it is published under its final name."""
    partial = destination.with_name(destination.name + ".partial")
    with contextlib.suppress(OSError):
        partial.unlink()
    process = lw.git(repo, "bundle", "create", str(partial), ref, check=False)
    if process.returncode != 0:
        raise LifecycleError(f"git bundle create failed: {text(process) or process.stderr}")
    verified = lw.git(repo, "bundle", "verify", str(partial), check=False)
    message = verified.stderr.decode("utf-8", "replace") + verified.stdout.decode("utf-8",
                                                                                  "replace")
    if verified.returncode != 0:
        raise LifecycleError(f"git bundle verify failed: {message.strip() or 'no detail'}")
    if not partial.is_file() or partial.stat().st_size == 0:
        raise LifecycleError("git bundle verify produced an empty bundle")
    os.replace(str(partial), str(destination))
    return destination


def walk_files(root: Path) -> Dict[str, str]:
    """Relative path -> kind.

    Only the ``.git`` metadata at the root is skipped (the pointer file or directory of a
    linked worktree); a nested ``.git`` is payload and gets verified like any other entry.
    Symlinks to directories are recorded as ``link`` and never traversed, so a link target
    outside the tree cannot smuggle entries into the inventory.
    """
    found: Dict[str, str] = {}
    for base, directories, files in os.walk(str(root)):
        kept: List[str] = []
        for name in directories:
            full = Path(base) / name
            relative = os.path.relpath(str(full), str(root)).replace(os.sep, "/")
            if relative == ".git":
                continue
            if os.path.islink(str(full)):
                found[relative] = "link"
            else:
                kept.append(name)
        directories[:] = kept
        for name in files:
            full = Path(base) / name
            relative = os.path.relpath(str(full), str(root))
            if relative == ".git":
                continue
            found[relative.replace(os.sep, "/")] = "link" if os.path.islink(str(full)) else "file"
    return found


def copy_worktree(worktree: Path, destination: Path) -> None:
    """Full-fidelity copy including ignored and untracked data; only ``.git`` stays behind."""
    destination.mkdir(parents=True, exist_ok=True)
    for entry in sorted(worktree.iterdir(), key=lambda item: item.name):
        if entry.name == ".git":
            continue
        target = destination / entry.name
        if entry.is_symlink() or entry.is_file():
            shutil.copy2(str(entry), str(target), follow_symlinks=False)
        else:
            shutil.copytree(str(entry), str(target), symlinks=True)


def verify_archive(worktree: Path, destination: Path) -> Tuple[str, int]:
    """Every archived byte must match the worktree it came from; returns (digest, count)."""
    source = walk_files(worktree)
    archived = walk_files(destination)
    if not source:
        raise LifecycleError("refusing to archive an empty worktree")
    missing = sorted(set(source) - set(archived))
    extra = sorted(set(archived) - set(source))
    if missing or extra:
        raise LifecycleError(f"archive does not match the worktree (missing {missing[:5]}, "
                             f"unexpected {extra[:5]})")
    records: List[str] = []
    for relative in sorted(source):
        original = worktree / relative
        copy = destination / relative
        if source[relative] == "link":
            if not os.path.islink(str(copy)):
                raise LifecycleError(f"{relative} was archived as a regular file")
            digest = hashlib.sha256(os.readlink(str(original)).encode("utf-8")).hexdigest()
            if os.readlink(str(copy)) != os.readlink(str(original)):
                raise LifecycleError(f"{relative} symlink target differs from the worktree")
        else:
            original_hash = hash_file(original)
            if hash_file(copy) != original_hash:
                raise LifecycleError(f"{relative} hash differs from the worktree")
            digest = original_hash
        records.append(f"{relative}\t{digest}\n")
    return hashlib.sha256("".join(records).encode("utf-8")).hexdigest(), len(records)


# --------------------------------------------------------------------------------------
# Receipt: durable evidence that removal was safe
# --------------------------------------------------------------------------------------
def write_receipt(path: Path, receipt: Dict[str, Any]) -> None:
    temporary = path.with_name(path.name + ".partial")
    temporary.write_text(json.dumps(receipt, indent=2, sort_keys=False) + "\n",
                         encoding="utf-8")
    os.replace(str(temporary), str(path))


def verify_evidence(repo: Path, state_dir: Path, job_id: str,
                    receipt: Dict[str, Any]) -> Optional[str]:
    """Re-check the recovery evidence a prior run claimed. ``None`` means it still holds."""
    ref = str(receipt.get("recovery_ref") or "")
    commit = str(receipt.get("recovery_commit") or "")
    if not ref.startswith(REF_PREFIX + "/") or not re.fullmatch(r"[0-9a-f]{40}", commit):
        return "receipt names no valid recovery ref/commit"
    if text(lw.git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}",
                   check=False)) != commit:
        return f"recovery ref {ref} no longer resolves to {commit}"
    index_commit = str(receipt.get("index_commit") or "")
    if index_commit:
        if not re.fullmatch(r"[0-9a-f]{40}", index_commit):
            return "receipt names an index snapshot commit that is not an object id"
        if text(lw.git(repo, "rev-parse", "--verify", "--quiet", f"{index_commit}^{{commit}}",
                       check=False)) != index_commit:
            return f"index snapshot commit {index_commit} is no longer reachable"
    bundle = Path(str(receipt.get("bundle") or ""))
    if canonical(bundle) != canonical(state_dir / "archives" / job_id / BUNDLE_NAME):
        return "receipt names a bundle outside this job's archive directory"
    if not bundle.is_file():
        return f"recovery bundle {bundle} is missing"
    if hash_file(bundle) != str(receipt.get("bundle_sha256") or ""):
        return f"recovery bundle {bundle} hash no longer matches the receipt"
    archived = Path(str(receipt.get("archive") or ""))
    if canonical(archived) != canonical(state_dir / "archives" / job_id / FILES_NAME):
        return "receipt names an archive directory outside this job's archive directory"
    if not archived.is_dir():
        return f"archive directory {archived} is missing"
    try:
        digest, count = recompute_archive(archived)
    except (OSError, ValueError) as error:
        return f"archive {archived} is not readable: {error}"
    if digest != str(receipt.get("archive_sha256") or ""):
        return f"archive {archived} contents no longer match the receipt"
    if count != int(receipt.get("archived_files") or -1):
        return f"archive {archived} file count no longer matches the receipt"
    return None


def recompute_archive(archived: Path) -> Tuple[str, int]:
    """Digest over the archived tree alone, so evidence stays verifiable after removal."""
    records: List[str] = []
    for relative in sorted(walk_files(archived)):
        full = archived / relative
        if os.path.islink(str(full)):
            digest = hashlib.sha256(os.readlink(str(full)).encode("utf-8")).hexdigest()
        else:
            digest = hash_file(full)
        records.append(f"{relative}\t{digest}\n")
    return hashlib.sha256("".join(records).encode("utf-8")).hexdigest(), len(records)


# --------------------------------------------------------------------------------------
# Lock: the state directory stays owned by whoever is running a job
# --------------------------------------------------------------------------------------
@contextlib.contextmanager
def archive_lock(state_dir: Path):
    """Exclusive non-blocking ``worker.lock`` plus a parallel-lease check, held throughout."""
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / "worker.lock"
    handle = open(lock_path, "a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise Blocked(f"{lock_path} is held by another local worker (errno {error.errno}); "
                          "an active default or parallel job blocks archive") from error
        try:
            active = pool.active_leases(state_dir)
        except (OSError, ValueError, lw.LauncherError):
            active = []
        if active:
            tasks = ", ".join(sorted(str(record.get("task_id") or "unknown")
                                     for record in active))
            raise Blocked(f"parallel worker lease(s) are live for: {tasks}; archive waits "
                          "until they finish")
        handle.seek(0)
        handle.truncate(0)
        handle.write(f"archive {os.getpid()}")
        handle.flush()
        yield lock_path
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


# --------------------------------------------------------------------------------------
# The operation
# --------------------------------------------------------------------------------------
def archive(repo_arg: Any, state_dir_arg: Any, job_id: str) -> Dict[str, Any]:
    """Archive every byte of one finished worktree, then remove that worktree."""
    repo = Path(os.path.abspath(str(repo_arg)))
    state_dir = Path(os.path.abspath(str(state_dir_arg)))
    if not repo.is_dir():
        raise Refusal(f"repository {repo} does not exist")
    if canonical(repo) == canonical(state_dir) or canonical(repo).startswith(
            canonical(state_dir) + os.sep):
        raise Refusal("the repository must live outside the state directory")
    with archive_lock(state_dir):
        report = read_report(state_dir, job_id)
        worktree, declared = resolve_worktree(repo, state_dir, report, must_exist=False)
        archive_dir = state_dir / "archives" / job_id
        receipt_path = archive_dir / RECEIPT_NAME
        original_report = state_dir / "reports" / job_id / REPORT_NAME

        if receipt_path.is_file():
            try:
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                raise Refusal(f"receipt {receipt_path} is not readable JSON: {error}") from error
            if not isinstance(receipt, dict):
                raise Refusal(f"receipt {receipt_path} is not a JSON object")
            problem = verify_evidence(repo, state_dir, job_id, receipt)
            if problem:
                raise Refusal(f"existing receipt for {job_id} is unverifiable: {problem}; "
                              "archives are never overwritten")
            if str(receipt.get("status") or "") != ARCHIVED_STATUS or not receipt.get("removed"):
                raise Refusal(
                    f"existing receipt for {job_id} is {str(receipt.get('status') or 'unknown')!r} "
                    "with removal unrecorded; its evidence verifies at "
                    f"{archive_dir} and nothing here is repeated or deleted. Confirm "
                    f"{worktree} is absent (otherwise run 'git -C {repo} worktree remove "
                    f"{worktree}' once that evidence is reviewed), then finish the receipt by "
                    "setting its status to \"archived\" and \"removed\" to true")
            if worktree.exists():
                raise Refusal(f"{worktree} still exists although {receipt_path} records it "
                              "as removed; refusing to repeat a completed archive")
            replayed = dict(receipt)
            replayed["removed"] = True
            replayed["idempotent"] = True
            return replayed

        if archive_dir.exists() and any(archive_dir.iterdir()):
            raise Refusal(f"{archive_dir} already holds entries without a receipt; nothing is "
                          "overwritten or removed")
        worktree, _ = resolve_worktree(repo, state_dir, report)

        try:
            archive_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError as error:
            raise Refusal(f"{archive_dir} already exists") from error
        try:
            ref = f"{REF_PREFIX}/{job_id}"
            if text(lw.git(repo, "show-ref", "--verify", "--quiet", ref, check=False)):
                raise Refusal(f"recovery ref {ref} already exists; archives are never "
                              "overwritten")
            staged = index_snapshot(worktree, f"local-worker staged index {job_id}")
            commit, parent = snapshot_commit(worktree, archive_dir / "tmp",
                                             f"local-worker archive {job_id}",
                                             index_commit=staged)
            # ``<old-value> = ZERO_OID`` makes this create-only: a ref that appeared in the
            # meantime makes git refuse instead of moving someone else's evidence.
            published = lw.git(repo, "update-ref", ref, commit, ZERO_OID, check=False)
            if published.returncode != 0:
                detail = (text(published)
                          or published.stderr.decode("utf-8", "replace").strip())
                raise Refusal(f"recovery ref {ref} already exists ({detail}); an existing ref "
                              "is never moved and archives are never overwritten")
            bundle = create_bundle(repo, ref, archive_dir / BUNDLE_NAME)
            bundle_hash = hash_file(bundle)
            files_dir = archive_dir / FILES_NAME
            copy_worktree(worktree, files_dir)
            archive_hash, count = verify_archive(worktree, files_dir)
            report_copy = archive_dir / REPORT_NAME
            shutil.copy2(str(original_report), str(report_copy))

            # Durable before destructive: the receipt says ``prepared`` while the worktree is
            # still there, so a crash after this point leaves verifiable evidence plus a receipt
            # that the next run refuses to repeat, instead of a silent half-archive.
            receipt = {
                "schema": "local-worker-archive/1",
                "status": PREPARED_STATUS,
                "job_id": job_id,
                "repo": str(repo),
                "git_common_dir": common_dir(repo),
                "source_worktree": declared,
                "report_status": report.get("status"),
                "recovery_ref": ref,
                "recovery_commit": commit,
                "recovery_parent": parent,
                "index_commit": staged or "",
                "bundle": str(bundle),
                "bundle_sha256": bundle_hash,
                "archive": str(files_dir),
                "archive_sha256": archive_hash,
                "archived_files": count,
                "original_report": str(original_report),
                "archived_report": str(report_copy),
                "removed": False,
                "forced_removal": False,
                "prepared_at": utc_now(),
                "archived_at": None,
                "idempotent": False,
            }
            write_receipt(receipt_path, receipt)

            # Everything above verified, so removal is now reversible from the ref, the
            # bundle and the copy. ``git worktree remove`` refuses a dirty tree by design:
            # that dirty content is exactly what was just archived, so git's own --force for
            # this one validated path completes the removal. No path is ever deleted here.
            removal = lw.git(repo, "worktree", "remove", str(worktree), check=False)
            forced = False
            if removal.returncode != 0:
                removal = lw.git(repo, "worktree", "remove", "--force", str(worktree),
                                 check=False)
                if removal.returncode != 0:
                    raise LifecycleError(
                        "git worktree remove failed for "
                        f"{worktree}: {text(removal) or removal.stderr.decode('utf-8', 'replace')}")
                forced = True
            if worktree.exists():
                raise LifecycleError(f"{worktree} still exists after removal")
            if canonical(worktree) in registered_worktrees(repo):
                raise LifecycleError(f"{worktree} is still registered after removal")

            receipt["status"] = ARCHIVED_STATUS
            receipt["removed"] = True
            receipt["forced_removal"] = forced
            receipt["archived_at"] = utc_now()
            write_receipt(receipt_path, receipt)
            with contextlib.suppress(OSError):
                shutil.rmtree(str(archive_dir / "tmp"))
            return receipt
        finally:
            with contextlib.suppress(OSError):
                if archive_dir.is_dir() and not any(archive_dir.iterdir()):
                    archive_dir.rmdir()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="worktree_lifecycle",
        description="Archive one finished local-worker worktree (committed, dirty, untracked "
                    "and ignored work included), verify the evidence, then remove it.")
    sub = parser.add_subparsers(dest="command")
    command = sub.add_parser("archive", help="archive and remove one finished worktree")
    command.add_argument("--repo", required=True, help="repository that owns the worktree")
    command.add_argument("--state-dir", required=True,
                         help="launcher state dir (reports/worktrees/archives/worker.lock)")
    command.add_argument("--job-id", required=True, help="finished job id under <state-dir>/reports")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command != "archive":
        parser.print_help(sys.stderr)
        return EXIT_REFUSED
    try:
        receipt = archive(args.repo, args.state_dir, args.job_id)
    except LifecycleError as error:
        kind = "blocked" if isinstance(error, Blocked) else ("refused" if isinstance(
            error, Refusal) else "failed")
        print(json.dumps({"status": kind, "job_id": args.job_id,
                          "reason": str(error)}, sort_keys=False), file=sys.stderr)
        return error.exit_code
    except (lw.LauncherError, OSError) as error:
        print(json.dumps({"status": "failed", "job_id": args.job_id, "reason": str(error)},
                         sort_keys=False), file=sys.stderr)
        return EXIT_FAILED
    print(json.dumps(receipt, indent=2, sort_keys=False))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
