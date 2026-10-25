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
import uuid
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
TRANSACTION_NAME = "transaction.json"
DEFAULT_IGNORED_CAP = 536870912
# A dirty tree may only be force-removed while all of this evidence is present and verified.
REQUIRED_EVIDENCE = ("recovery_ref", "recovery_commit", "index_commit", "bundle",
                     "bundle_sha256", "bundle_basis", "archive", "archive_sha256",
                     "archived_files", "ignored_files")
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
    """Commit tracked edits and non-ignored untracked files on top of HEAD.

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
    # The absence of --force is deliberate: ignored build outputs stay in the verified file
    # copy and never enter the repository's object database.
    lw.git(worktree, "add", "--all", "--", ".", env=env)
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


def create_bundle(repo: Path, ref: str, destination: Path, base_commit: str) -> Path:
    """Write and verify a bundle before it is published under its final name."""
    partial = destination.with_name(destination.name + ".partial")
    with contextlib.suppress(OSError):
        partial.unlink()
    process = lw.git(repo, "bundle", "create", str(partial), ref, "^" + base_commit,
                     check=False)
    if process.returncode != 0:
        raise LifecycleError(f"git bundle create failed: {text(process) or process.stderr}")
    verified = lw.git(repo, "bundle", "verify", str(partial), check=False)
    message = verified.stderr.decode("utf-8", "replace") + verified.stdout.decode("utf-8",
                                                                                  "replace")
    if verified.returncode != 0:
        raise LifecycleError(f"git bundle verify failed: {message.strip() or 'no detail'}")
    if not partial.is_file() or partial.stat().st_size == 0:
        raise LifecycleError("git bundle verify produced an empty bundle")
    heads = lw.git(repo, "bundle", "list-heads", str(partial), check=False)
    expected = f"{text(lw.git(repo, 'rev-parse', '--verify', ref + '^{commit}'))} {ref}"
    if heads.returncode != 0 or text(heads) != expected:
        raise LifecycleError("git bundle heads do not match the requested recovery ref")
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


def copy_worktree(worktree: Path, destination: Path,
                  skip_paths: Optional[Set[str]] = None) -> None:
    """Copy worktree payload, optionally omitting known ignored paths; root ``.git`` stays out."""
    omitted = set(skip_paths or ())
    destination.mkdir(parents=True, exist_ok=True)

    def copy_directory(source: Path, target: Path, relative: str) -> None:
        target.mkdir(parents=True, exist_ok=True)
        shutil.copystat(str(source), str(target), follow_symlinks=False)
        for entry in sorted(source.iterdir(), key=lambda item: item.name):
            rel = (relative + "/" + entry.name).lstrip("/")
            if rel == ".git" or rel in omitted:
                continue
            out = target / entry.name
            if entry.is_symlink() or entry.is_file():
                shutil.copy2(str(entry), str(out), follow_symlinks=False)
            elif entry.is_dir():
                copy_directory(entry, out, rel)

    for entry in sorted(worktree.iterdir(), key=lambda item: item.name):
        if entry.name == ".git" or entry.name in omitted:
            continue
        target = destination / entry.name
        if entry.is_symlink() or entry.is_file():
            shutil.copy2(str(entry), str(target), follow_symlinks=False)
        elif entry.is_dir():
            copy_directory(entry, target, entry.name)


def ignored_inventory(worktree: Path) -> Tuple[List[str], int]:
    """List ignored untracked files and their stored byte size without following symlinks."""
    process = lw.git(worktree, "ls-files", "--others", "--ignored", "--exclude-standard",
                     "-z", check=False)
    if process.returncode != 0:
        raise LifecycleError("could not inventory ignored files: " +
                             (text(process) or process.stderr.decode("utf-8", "replace").strip()))
    relative_paths: Set[str] = set()
    for raw in process.stdout.split(b"\0"):
        if not raw:
            continue
        relative = os.fsdecode(raw).replace(os.sep, "/").rstrip("/")
        if not relative or relative.startswith("/") or ".." in relative.split("/"):
            raise Refusal("git returned an unsafe ignored path")
        source = worktree / relative
        if source.is_symlink() or source.is_file():
            relative_paths.add(relative)
        elif source.is_dir():
            for base, directories, files in os.walk(str(source), followlinks=False):
                kept = []
                for name in directories:
                    item = Path(base) / name
                    if item.is_symlink():
                        relative_paths.add(os.path.relpath(str(item), str(worktree)).replace(os.sep, "/"))
                    else:
                        kept.append(name)
                directories[:] = kept
                for name in files:
                    item = Path(base) / name
                    relative_paths.add(os.path.relpath(str(item), str(worktree)).replace(os.sep, "/"))
        else:
            raise LifecycleError("ignored path disappeared during inventory: " + relative)
    total = 0
    for relative in relative_paths:
        try:
            total += (worktree / relative).lstat().st_size
        except OSError as error:
            raise LifecycleError("could not size ignored file " + relative) from error
    return sorted(relative_paths), total


def ref_exists(repo: Path, ref: str) -> bool:
    """Use show-ref's return code: its quiet success intentionally has no stdout."""
    process = lw.git(repo, "show-ref", "--verify", "--quiet", ref, check=False)
    if process.returncode == 0:
        return True
    if process.returncode == 1:
        return False
    raise LifecycleError("could not check recovery ref " + ref + ": " +
                         (text(process) or process.stderr.decode("utf-8", "replace").strip()))


def read_transaction_marker(path: Path) -> Dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise Refusal("partial archive has no verifiable transaction ownership marker")
    try:
        marker = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise Refusal("partial archive transaction marker is unreadable") from error
    if not isinstance(marker, dict):
        raise Refusal("partial archive transaction marker is not an object")
    return marker


def _write_transaction_marker(path: Path, marker: Dict[str, Any]) -> None:
    write_receipt(path, marker)


def _valid_owned_partial(archive_dir: Path, repo: Path, state_dir: Path, job_id: str,
                         declared: str, worktree: Path, ref: str) -> Dict[str, Any]:
    marker = read_transaction_marker(archive_dir / TRANSACTION_NAME)
    base = str(marker.get("base_commit") or "")
    if (marker.get("schema") != "local-worker-archive-transaction/1" or
            not marker.get("transaction_id") or marker.get("repo") != canonical(repo) or
            marker.get("state_dir") != canonical(state_dir) or marker.get("job_id") != job_id or
            marker.get("source_worktree") != declared or marker.get("recovery_ref") != ref or
            not OID.fullmatch(base) or marker.get("recovery_commit") is not None and
            not OID.fullmatch(str(marker.get("recovery_commit")))):
        raise Refusal("partial archive marker does not match this repo, job, worktree, base, and ref")
    if lw.rev_parse(worktree, "HEAD") != base:
        raise Refusal("partial archive worktree base changed; refusing to discard its evidence")
    recorded_commit = marker.get("recovery_commit")
    if ref_exists(repo, ref):
        current = text(lw.git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}",
                              check=False))
        if not recorded_commit or current != recorded_commit:
            raise Refusal("recovery ref exists but does not match the partial archive ownership marker")
    elif recorded_commit:
        # The marker binds the expected object and ref. If the ref is already absent, resume
        # has no ref to delete; a same-name ref that appears later is still caught above.
        resolved = text(lw.git(repo, "rev-parse", "--verify", "--quiet",
                               f"{recorded_commit}^{{commit}}", check=False))
        if resolved and resolved != recorded_commit:
            raise Refusal("partial archive recovery commit does not match its ownership marker")
    allowed = {TRANSACTION_NAME, TRANSACTION_NAME + ".partial", "tmp", "recovery.bundle",
               "recovery.bundle.partial", FILES_NAME, REPORT_NAME}
    for child in archive_dir.iterdir():
        if child.name not in allowed:
            raise Refusal("partial archive contains an unexpected entry: " + child.name)
        if child.is_symlink():
            raise Refusal("partial archive contains a symlink: " + child.name)
    return marker


def verify_archive(worktree: Path, destination: Path,
                   skip_paths: Optional[Set[str]] = None) -> Tuple[str, int]:
    """Every archived byte must match the worktree it came from; returns (digest, count)."""
    omitted = set(skip_paths or ())
    source = {relative: kind for relative, kind in walk_files(worktree).items()
              if relative not in omitted}
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
    legacy_receipt = "bundle_basis" not in receipt and "ignored_files" not in receipt
    recorded_repo = receipt.get("repo")
    if (not JOB_ID_PATTERN.fullmatch(job_id) or ref != f"{REF_PREFIX}/{job_id}" or
            receipt.get("job_id") != job_id or not isinstance(recorded_repo, str) or
            canonical(recorded_repo) != canonical(repo) or
            not re.fullmatch(r"[0-9a-f]{40}", commit)):
        return "receipt names no valid recovery ref/commit"
    try:
        original_report = read_report(state_dir, job_id)
        _source_path, declared = resolve_worktree(repo, state_dir, original_report,
                                                  must_exist=False)
    except LifecycleError as error:
        return "original job report is not verifiable: " + str(error)
    if receipt.get("source_worktree") != declared:
        return "receipt source worktree does not match the original job report"
    if text(lw.git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}",
                   check=False)) != commit:
        return f"recovery ref {ref} no longer resolves to {commit}"
    basis = str(receipt.get("bundle_basis") or "")
    if not legacy_receipt:
        if not OID.fullmatch(basis) or receipt.get("recovery_parent") != basis:
            return "receipt has no verified bundle basis matching the snapshot's first parent"
        if text(lw.git(repo, "rev-parse", "--verify", "--quiet", f"{basis}^{{commit}}",
                       check=False)) != basis:
            return f"bundle basis {basis} is no longer available"
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
    if bundle.is_symlink() or not bundle.is_file():
        return f"recovery bundle {bundle} is missing"
    if hash_file(bundle) != str(receipt.get("bundle_sha256") or ""):
        return f"recovery bundle {bundle} hash no longer matches the receipt"
    verified = lw.git(repo, "bundle", "verify", str(bundle), check=False)
    if verified.returncode != 0:
        return "recovery bundle no longer verifies: " + (text(verified) or
            verified.stderr.decode("utf-8", "replace").strip())
    heads = lw.git(repo, "bundle", "list-heads", str(bundle), check=False)
    expected_head = f"{commit} {ref}"
    if heads.returncode != 0 or text(heads) != expected_head:
        return "recovery bundle head does not match its receipt ref and commit"
    archived = Path(str(receipt.get("archive") or ""))
    if canonical(archived) != canonical(state_dir / "archives" / job_id / FILES_NAME):
        return "receipt names an archive directory outside this job's archive directory"
    if archived.is_symlink() or not archived.is_dir():
        return f"archive directory {archived} is missing"
    try:
        digest, count = recompute_archive(archived)
    except (OSError, ValueError) as error:
        return f"archive {archived} is not readable: {error}"
    if digest != str(receipt.get("archive_sha256") or ""):
        return f"archive {archived} contents no longer match the receipt"
    if count != int(receipt.get("archived_files") or -1):
        return f"archive {archived} file count no longer matches the receipt"
    if not legacy_receipt:
        ignored = receipt.get("ignored_files")
        if not isinstance(ignored, dict):
            return "receipt has no ignored-files inventory"
        paths = ignored.get("paths")
        count_expected = ignored.get("count")
        bytes_expected = ignored.get("bytes")
        stored = ignored.get("stored")
        if (not isinstance(paths, list) or type(count_expected) is not int or count_expected < 0 or
                type(bytes_expected) is not int or bytes_expected < 0 or count_expected != len(paths) or
                stored not in ("copy", "skipped") or
                not all(isinstance(path, str) and path and not path.startswith("/") and
                        ".." not in path.split("/") for path in paths) or len(paths) != len(set(paths))):
            return "receipt ignored-files inventory is malformed"
        ignored_bytes = 0
        for relative in paths:
            copied = archived / relative
            if stored == "skipped":
                if copied.exists() or copied.is_symlink():
                    return f"skipped ignored file {relative} is unexpectedly present in the archive"
            else:
                if not copied.exists() and not copied.is_symlink():
                    return f"copied ignored file {relative} is missing from the archive"
                try:
                    ignored_bytes += copied.lstat().st_size
                except OSError:
                    return f"copied ignored file {relative} cannot be inspected"
        if stored == "copy" and ignored_bytes != bytes_expected:
            return "copied ignored-files byte count no longer matches the receipt"
    original_report_path = state_dir / "reports" / job_id / REPORT_NAME
    archived_report = Path(str(receipt.get("archived_report") or ""))
    if (canonical(original_report_path) != canonical(Path(str(receipt.get("original_report") or ""))) or
            canonical(archived_report) != canonical(state_dir / "archives" / job_id / REPORT_NAME) or
            archived_report.is_symlink() or not archived_report.is_file() or
            hash_file(original_report_path) != hash_file(archived_report)):
        return "archived copy of the original report is missing or changed"
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
def archive(repo_arg: Any, state_dir_arg: Any, job_id: str, *, resume: bool = False,
            skip_ignored: bool = False, ignored_cap: int = DEFAULT_IGNORED_CAP) -> Dict[str, Any]:
    """Archive one finished worktree, then remove it only after evidence verifies."""
    repo = Path(os.path.abspath(str(repo_arg)))
    state_dir = Path(os.path.abspath(str(state_dir_arg)))
    if not repo.is_dir():
        raise Refusal(f"repository {repo} does not exist")
    if not isinstance(ignored_cap, int) or isinstance(ignored_cap, bool) or ignored_cap < 0:
        raise Refusal("ignored file size cap must be a non-negative integer")
    if not JOB_ID_PATTERN.fullmatch(job_id) or job_id in (".", ".."):
        raise Refusal(f"job id {job_id!r} is not a single safe path component")
    if canonical(repo) == canonical(state_dir) or canonical(repo).startswith(
            canonical(state_dir) + os.sep):
        raise Refusal("the repository must live outside the state directory")
    with archive_lock(state_dir):
        report = read_report(state_dir, job_id)
        worktree, declared = resolve_worktree(repo, state_dir, report, must_exist=False)
        archive_dir = state_dir / "archives" / job_id
        receipt_path = archive_dir / RECEIPT_NAME
        original_report = state_dir / "reports" / job_id / REPORT_NAME
        ref = f"{REF_PREFIX}/{job_id}"

        if archive_dir.is_symlink():
            raise Refusal(f"{archive_dir} is a symlink; archive directories are never followed")
        if receipt_path.is_symlink():
            raise Refusal(f"{receipt_path} is a symlink; receipts are never followed")
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
                if (str(receipt.get("status") or "") == PREPARED_STATUS and resume and
                        not worktree.exists()):
                    receipt["status"] = ARCHIVED_STATUS
                    receipt["removed"] = True
                    receipt["archived_at"] = utc_now()
                    write_receipt(receipt_path, receipt)
                    return receipt
                raise Refusal(
                    f"existing receipt for {job_id} is {str(receipt.get('status') or 'unknown')!r} "
                    "with removal unrecorded; use --resume only after its worktree is gone and "
                    "its evidence verifies")
            if worktree.exists():
                raise Refusal(f"{worktree} still exists although {receipt_path} records it "
                              "as removed; refusing to repeat a completed archive")
            replayed = dict(receipt)
            replayed["removed"] = True
            replayed["idempotent"] = True
            return replayed

        if archive_dir.is_symlink():
            raise Refusal(f"{archive_dir} is a symlink; archive directories are never followed")
        if archive_dir.exists():
            if not resume:
                raise Refusal(f"{archive_dir} already exists without a receipt; use --resume "
                              "only for a verified partial transaction")
            worktree, _ = resolve_worktree(repo, state_dir, report)
            marker = _valid_owned_partial(archive_dir, repo, state_dir, job_id, declared,
                                          worktree, ref)
            recovery_commit = marker.get("recovery_commit")
            if recovery_commit and ref_exists(repo, ref):
                deleted = lw.git(repo, "update-ref", "-d", ref, recovery_commit, check=False)
                if deleted.returncode != 0:
                    raise Refusal("owned partial recovery ref changed during resume; preserving "
                                  "all evidence")
            shutil.rmtree(str(archive_dir))
        elif resume and ref_exists(repo, ref):
            raise Refusal(f"recovery ref {ref} exists without a transaction marker; it is not "
                          "owned by this archive and will not be removed")

        # A dead-ref collision is checked before the archive directory is created. Quiet
        # show-ref emits no output on success, so its exit code is the only reliable test.
        if ref_exists(repo, ref):
            raise Refusal(f"recovery ref {ref} already exists; archives are never overwritten")
        worktree, _ = resolve_worktree(repo, state_dir, report)
        # Worker-created commits and blobs live outside the shared primary object database.
        # Validate them in isolation and import only hash-verified objects before any archive
        # Git operation can resolve a worker-controlled OID (HEAD, index entries, or refs).
        private_objects = report.get("git_object_store")
        if private_objects:
            expected_objects = state_dir / "reports" / job_id / "git-objects"
            if Path(os.path.abspath(str(private_objects))) != expected_objects:
                raise Refusal("report names an unexpected private object-store path")
            try:
                lw.promote_job_object_store(worktree, expected_objects)
            except (lw.GitError, OSError, ValueError) as error:
                raise Refusal(f"job object store could not be safely promoted: {error}") from error
        ignored_paths, ignored_bytes = ignored_inventory(worktree)
        if ignored_bytes > ignored_cap and not skip_ignored:
            raise Refusal("ignored files total %d bytes, above the %d byte cap; use "
                          "--skip-ignored to archive without them" % (ignored_bytes, ignored_cap))
        base_commit = lw.rev_parse(worktree, "HEAD")
        if not base_commit or not OID.fullmatch(base_commit):
            raise Refusal(f"{worktree} has no resolvable HEAD base commit")

        try:
            archive_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError as error:
            raise Refusal(f"{archive_dir} already exists") from error
        marker = {
            "schema": "local-worker-archive-transaction/1",
            "transaction_id": uuid.uuid4().hex,
            "repo": canonical(repo), "state_dir": canonical(state_dir), "job_id": job_id,
            "source_worktree": declared, "base_commit": base_commit,
            "recovery_ref": ref, "recovery_commit": None,
            "created_at": utc_now(),
        }
        _write_transaction_marker(archive_dir / TRANSACTION_NAME, marker)
        try:
            staged = index_snapshot(worktree, f"local-worker staged index {job_id}")
            commit, parent = snapshot_commit(worktree, archive_dir / "tmp",
                                             f"local-worker archive {job_id}",
                                             index_commit=staged)
            if parent != base_commit:
                raise LifecycleError("worktree base changed while preparing its recovery snapshot")
            marker["recovery_commit"] = commit
            marker["index_commit"] = staged or ""
            _write_transaction_marker(archive_dir / TRANSACTION_NAME, marker)
            # ``<old-value> = ZERO_OID`` makes this create-only: a ref that appeared in the
            # meantime makes git refuse instead of moving someone else's evidence.
            published = lw.git(repo, "update-ref", ref, commit, ZERO_OID, check=False)
            if published.returncode != 0:
                detail = (text(published)
                          or published.stderr.decode("utf-8", "replace").strip())
                raise Refusal(f"recovery ref {ref} already exists ({detail}); an existing ref "
                              "is never moved and archives are never overwritten")
            bundle = create_bundle(repo, ref, archive_dir / BUNDLE_NAME, base_commit)
            bundle_hash = hash_file(bundle)
            files_dir = archive_dir / FILES_NAME
            excluded = set(ignored_paths) if skip_ignored else set()
            copy_worktree(worktree, files_dir, excluded)
            archive_hash, count = verify_archive(worktree, files_dir, excluded)
            report_copy = archive_dir / REPORT_NAME
            shutil.copy2(str(original_report), str(report_copy))

            # Durable before destructive: the receipt says ``prepared`` while the worktree is
            # still there, so a crash after this point leaves verifiable evidence plus a receipt
            # that the next run refuses to repeat, instead of a silent half-archive.
            receipt = {
                "schema": "local-worker-archive/1",
                "status": PREPARED_STATUS,
                "job_id": job_id,
                "repo": canonical(repo),
                "git_common_dir": common_dir(repo),
                "source_worktree": declared,
                "report_status": report.get("status"),
                "recovery_ref": ref,
                "recovery_commit": commit,
                "recovery_parent": parent,
                "index_commit": staged or "",
                "bundle_basis": base_commit,
                "ignored_files": {"count": len(ignored_paths), "bytes": ignored_bytes,
                                  "stored": "skipped" if skip_ignored else "copy",
                                  "paths": ignored_paths},
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

            problem = verify_evidence(repo, state_dir, job_id, receipt)
            if problem:
                raise LifecycleError("recovery evidence failed verification before removal: " +
                                     problem)

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
            problem = verify_evidence(repo, state_dir, job_id, receipt)
            if problem:
                raise LifecycleError("recovery evidence changed after worktree removal: " + problem)
            with contextlib.suppress(OSError):
                shutil.rmtree(str(archive_dir / "tmp"))
            return receipt
        finally:
            with contextlib.suppress(OSError):
                if archive_dir.is_dir() and not any(archive_dir.iterdir()):
                    archive_dir.rmdir()


def prune(repo_arg: Any, state_dir_arg: Any, older_than_days: int,
          dry_run: bool = False) -> List[Dict[str, Any]]:
    """Remove only old, fully verified archived evidence after validating every candidate."""
    repo = Path(os.path.abspath(str(repo_arg)))
    state_dir = Path(os.path.abspath(str(state_dir_arg)))
    if not repo.is_dir():
        raise Refusal(f"repository {repo} does not exist")
    if not isinstance(older_than_days, int) or isinstance(older_than_days, bool) or older_than_days < 0:
        raise Refusal("older-than days must be a non-negative integer")
    if canonical(repo) == canonical(state_dir) or canonical(repo).startswith(
            canonical(state_dir) + os.sep):
        raise Refusal("the repository must live outside the state directory")
    archive_root = state_dir / "archives"
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=older_than_days)
    with archive_lock(state_dir):
        if archive_root.is_symlink():
            raise Refusal(f"{archive_root} is a symlink; archive directories are never followed")
        if not archive_root.is_dir():
            return []
        candidates: List[Tuple[Path, str, Dict[str, Any], str]] = []
        for archive_dir in sorted(archive_root.iterdir(), key=lambda item: item.name):
            if archive_dir.is_symlink() or not archive_dir.is_dir():
                continue
            job_id = archive_dir.name
            if not JOB_ID_PATTERN.fullmatch(job_id):
                continue
            receipt_path = archive_dir / RECEIPT_NAME
            if receipt_path.is_symlink() or not receipt_path.is_file():
                continue
            try:
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                raise Refusal(f"cannot read prune candidate receipt {receipt_path}") from error
            if not isinstance(receipt, dict) or receipt.get("status") != ARCHIVED_STATUS or not receipt.get("removed"):
                continue
            raw_time = receipt.get("archived_at")
            try:
                archived_at = datetime.datetime.fromisoformat(str(raw_time).replace("Z", "+00:00"))
                if archived_at.tzinfo is None:
                    raise ValueError("timestamp has no timezone")
            except (ValueError, TypeError):
                raise Refusal(f"archived receipt {receipt_path} has no valid archived_at timestamp")
            if archived_at > cutoff:
                continue
            problem = verify_evidence(repo, state_dir, job_id, receipt)
            if problem:
                raise Refusal(f"cannot prune unverifiable archive {job_id}: {problem}")
            source = receipt.get("source_worktree")
            if not isinstance(source, str):
                raise Refusal(f"archived receipt {job_id} has no source worktree")
            source_path = Path(source)
            if source_path.exists() or source_path.is_symlink() or canonical(source_path) in registered_worktrees(repo):
                raise Refusal(f"cannot prune {job_id}: source worktree is still present or registered")
            candidates.append((archive_dir, job_id, receipt, str(receipt["recovery_ref"])))

        # Validate all refs one more time before deleting the first item. The caller has
        # explicitly requested pruning; this preflight prevents a partially pruned batch when
        # an old receipt/ref has drifted.
        for _archive_dir, job_id, receipt, ref in candidates:
            current = text(lw.git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}",
                                  check=False))
            if current != receipt.get("recovery_commit"):
                raise Refusal(f"recovery ref for prune candidate {job_id} changed during preflight")

        result = []
        for archive_dir, job_id, receipt, ref in candidates:
            item = {"job_id": job_id, "archived_at": receipt.get("archived_at"),
                    "action": "would_prune" if dry_run else "pruned"}
            result.append(item)
            if dry_run:
                continue
            removed_ref = lw.git(repo, "update-ref", "-d", ref, str(receipt["recovery_commit"]),
                                 check=False)
            if removed_ref.returncode != 0:
                raise Refusal(f"could not safely delete recovery ref {ref}; preserving archive "
                              "directory")
            shutil.rmtree(str(archive_dir))
        return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="worktree_lifecycle",
        description="Archive finished local-worker worktrees and prune old verified evidence.")
    sub = parser.add_subparsers(dest="command")
    command = sub.add_parser("archive", help="archive and remove one finished worktree")
    command.add_argument("--repo", required=True, help="repository that owns the worktree")
    command.add_argument("--state-dir", required=True,
                         help="launcher state dir (reports/worktrees/archives/worker.lock)")
    command.add_argument("--job-id", required=True, help="finished job id under <state-dir>/reports")
    command.add_argument("--resume", action="store_true",
                         help="resume only a transaction whose ownership marker verifies")
    command.add_argument("--skip-ignored", action="store_true",
                         help="omit ignored files from the file copy")
    command.add_argument("--ignored-cap", type=int, default=DEFAULT_IGNORED_CAP,
                         metavar="BYTES", help="maximum ignored-file bytes copied (default 512 MiB)")
    pruning = sub.add_parser("prune", help="remove old verified archive evidence explicitly")
    pruning.add_argument("--repo", required=True, help="repository that owns recovery refs")
    pruning.add_argument("--state-dir", required=True, help="launcher state directory")
    pruning.add_argument("--older-than", type=int, required=True, metavar="DAYS")
    pruning.add_argument("--dry-run", action="store_true", help="list eligible archives without deleting")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command not in ("archive", "prune"):
        parser.print_help(sys.stderr)
        return EXIT_REFUSED
    try:
        if args.command == "archive":
            receipt = archive(args.repo, args.state_dir, args.job_id, resume=args.resume,
                              skip_ignored=args.skip_ignored, ignored_cap=args.ignored_cap)
        else:
            receipt = prune(args.repo, args.state_dir, args.older_than, dry_run=args.dry_run)
    except LifecycleError as error:
        kind = "blocked" if isinstance(error, Blocked) else ("refused" if isinstance(
            error, Refusal) else "failed")
        print(json.dumps({"status": kind, "job_id": getattr(args, "job_id", None),
                          "reason": str(error)}, sort_keys=False), file=sys.stderr)
        return error.exit_code
    except (lw.LauncherError, OSError) as error:
        print(json.dumps({"status": "failed", "job_id": getattr(args, "job_id", None), "reason": str(error)},
                         sort_keys=False), file=sys.stderr)
        return EXIT_FAILED
    print(json.dumps(receipt, indent=2, sort_keys=False))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
