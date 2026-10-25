"""The user-owned permissions list and the permissive Seatbelt profile built from it.

``<state>/permissions.json`` is the single place that says what a background worker may not do.
Workers keep full host access (network, simulators, every cache); a macOS Seatbelt profile of
``(allow default)`` plus a short list of denials hides secrets from them and stops them writing
the user's primary checkout (spec §6.2, §6.14):

* ``deny_read`` paths cannot be read at all (``file-read*``: contents and metadata);
* with ``protect_primary_checkout`` the primary checkout's files and its resolved git common dir
  (wherever it is: a plain ``.git``, a ``--separate-git-dir``, or the main repository of a linked
  worktree used as the primary) cannot be written, except ``objects/`` (a commit in the job's
  worktree writes there; ``objects/info`` stays shut so no alternates file is planted) and the
  job's own linked-worktree admin dir under ``worktrees/<name>``; the primary's own admin dir, when
  it is itself a linked worktree, is shut too. That is an allowlist, so nothing that would later run
  when the user uses git there (config, hooks, ``info/attributes``, refs, ``MERGE_HEAD``,
  ``index.lock``, ``shallow``, ...) can be planted. The job's admin dir keeps its
  ``commondir``/``gitdir``/``config.worktree`` and the worktree's ``.git`` gitfile denied so they
  cannot repoint the launcher's own git;
* ``deny_write`` paths (persistence vectors: ``~/.gitconfig``, shell rc files,
  ``~/Library/LaunchAgents`` by default) cannot be written, so nothing planted there runs the
  next time the user opens a shell or runs git;
* given the launcher's state directory, nothing in it can be written except the job's own paths
  (its worktree, its Codex home, its last-message files, its scratch), so a worker cannot rewrite
  this list, the profile it runs under, reports, leases, locks or another job's worktree.

Seatbelt applies the last matching rule, so the profile order below is the policy. Every path is
``expanduser`` then ``realpath``: Seatbelt matches the resolved path (``/private/var``, never
``/var``). It also matches case- and normalisation-insensitively on a default APFS volume, so
every comparison here (conflicts, add/remove, reading denials) folds case and Unicode form.

Seatbelt matches *paths*, and a rename is a write that ``(allow default)`` permits, so a denial
on a path is void if the path, or any parent of it, can be moved away. Every hidden path, the
primary checkout and the state directory are therefore also guarded against being renamed,
replaced or deleted (``_rename_protection``). For the same reason a hidden path, and every parent
of one, cannot be cloned (``clonefile(2)`` checks only the directory it copies, so cloning a
parent would copy a hidden file out readable).

The file is the user's. A malformed file, an unknown or missing key, or a mode that is not built
is a ``PermissionsError`` that names the problem: nothing here ever falls back to the defaults
for a file that exists, and the defaults are written only when there is no file at all.

Standard library only; importable without ``local_worker`` so that module can use it.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import sys
import tempfile
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Sequence, Tuple

__all__ = [
    "DEFAULT_DENY_READ", "DEFAULT_DENY_WRITE", "GIT_ADMIN_REDIRECTS", "LOG_FILE", "MODE_FULL_HOST",
    "PERMISSIONS_FILE", "SANDBOX_EXEC", "SCHEMA_VERSION", "Permissions", "PermissionsError",
    "add_deny_read", "add_deny_write", "check_conflicts", "default_permissions", "describe",
    "edit_lock", "find_denials", "load_permissions", "normalize_entry", "permissions_payload", "record_change",
    "remove_deny_read", "remove_deny_write", "resolve_git_dirs", "save_permissions",
    "seatbelt_profile", "wrap_argv", "wrapper_available",
]

PERMISSIONS_FILE = "permissions.json"
LOG_FILE = "permissions.log"
LOCK_FILE = "permissions.lock"
SCHEMA_VERSION = 1
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
MODE_FULL_HOST = "full_host"

# Credentials and private data a worker has no business reading. Keychains are left out on
# purpose (code signing may need them); the user can add them with ``deny-read``.
DEFAULT_DENY_READ = (
    "~/.codex", "~/.ssh", "~/.omlx/settings.json", "~/.aws", "~/.config/gh", "~/.config/gcloud",
    "~/.kube", "~/.docker/config.json", "~/.netrc", "~/.npmrc", "~/.pypirc", "~/.gnupg",
    "~/Library/Application Support/Google/Chrome", "~/Library/Application Support/Firefox",
    "~/Library/Safari", "~/Library/Group Containers/2BUA8C4S2C.com.1password",
)

# Persistence vectors a worker must not write even though they live outside the primary checkout:
# a planted entry here runs, unwrapped, the next time the user runs git or opens a shell (R19).
# User-editable: ``deny-write``/``allow-write`` change this list. A file missing the key keeps
# these defaults, so a file written before the key existed stays valid and still protected.
DEFAULT_DENY_WRITE = (
    "~/.gitconfig", "~/.config/git", "~/.zshrc", "~/.zprofile", "~/.zshenv", "~/.zlogin",
    "~/.bashrc", "~/.bash_profile", "~/.profile", "~/Library/LaunchAgents",
)

# The primary's complete git common directory is read-only. Job commits write to a private object
# store with the primary object directory configured as a read-only alternate.
GIT_WRITABLE_DIRS = ()
GIT_SHUT_BELOW = ()
# The admin files of a linked worktree that redirect where its git dir, common dir or config
# come from. Writable they would point the launcher's own (unwrapped) git at a directory the
# worker controls; the rest of the admin dir (index, HEAD, logs, ORIG_HEAD, …) stays writable so
# a commit in the worktree still works.
GIT_ADMIN_REDIRECTS = ("commondir", "gitdir", "config.worktree")

_KEYS = ("schema_version", "mode", "deny_read", "deny_write", "protect_primary_checkout",
         "writable_roots", "network")
# Keys a file may omit and still be valid; the loader fills each from the defaults. ``deny_write``
# is new (R19), so a file written before it existed keeps the default persistence-vector list.
_OPTIONAL_KEYS = ("deny_write",)
_NETWORK_KEYS = ("allowed_domains",)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class PermissionsError(Exception):
    """The permissions list (or a change to it) cannot be used; the message names why."""


@dataclass
class Permissions:
    mode: str
    deny_read: List[str]
    protect_primary_checkout: bool
    # Paths a worker must not write even outside the primary checkout (persistence vectors, R19).
    deny_write: List[str] = field(default_factory=lambda: list(DEFAULT_DENY_WRITE))
    # Reserved for the sandboxed mode: stored and shown, not applied.
    writable_roots: List[str] = field(default_factory=list)
    allowed_domains: List[str] = field(default_factory=list)


def default_permissions() -> Permissions:
    """A fresh copy of the defaults: full host access, the secrets list, primary checkout safe."""
    return Permissions(mode=MODE_FULL_HOST, deny_read=list(DEFAULT_DENY_READ),
                       protect_primary_checkout=True, deny_write=list(DEFAULT_DENY_WRITE),
                       writable_roots=[], allowed_domains=[])


# --------------------------------------------------------------------------- validation

def _is_string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _check_text(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PermissionsError(f"{what} must be a non-empty string, got {value!r}")
    if _CONTROL.search(value):
        raise PermissionsError(f"{what} contains a control character and cannot be placed in a "
                               f"profile: {value!r}")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        # A file name that is not UTF-8 reaches Python as a lone surrogate; a profile (UTF-8
        # text) cannot name it.
        raise PermissionsError(f"{what} is not valid UTF-8 text, so a profile cannot name it: "
                               f"{value!r}") from None
    return value


def _expand(entry: str, label: str = "deny_read entry") -> str:
    """``~`` expanded; the result must be absolute, so a relative entry never means "the cwd"."""
    expanded = os.path.expanduser(_check_text(entry, label))
    if not os.path.isabs(expanded):
        raise PermissionsError(f"{label} {entry!r} must be an absolute path or start "
                               f"with ~ (and the ~ must resolve)")
    return expanded


def _real(entry: str) -> str:
    return os.path.realpath(_expand(entry))


def _forms(entry: str) -> List[str]:
    """The real path first, then the spelled (``~``-expanded, normalized) one when it differs."""
    forms = [_real(entry)]
    spelled = os.path.normpath(_expand(entry))
    if spelled not in forms:
        forms.append(spelled)
    return forms


def _path_forms(path: Any) -> List[str]:
    forms = [os.path.realpath(str(path))]
    spelled = os.path.normpath(os.path.abspath(str(path)))
    if spelled not in forms:
        forms.append(spelled)
    return forms


def _fold(path: str) -> str:
    """The comparison key of a path: Unicode-decomposed and case-folded.

    Seatbelt and the default (case-insensitive, normalisation-insensitive) APFS volume treat
    ``~/.SSH`` and ``~/.ssh``, or an accent typed as one character or two, as one path, so a
    comparison that does not fold would miss a variant that names the same directory. On a
    case-sensitive volume this errs towards more matches (extra refusals, a case variant counted
    as the same entry), never fewer.
    """
    return unicodedata.normalize("NFD", path).casefold()


def _covers(root: str, path: str) -> bool:
    """``path`` is ``root`` itself or lives under it (whole path components only)."""
    root, path = _fold(root), _fold(path)
    return path == root or path.startswith(root.rstrip("/") + "/")


def _same(first: str, second: str) -> bool:
    return _fold(first) == _fold(second)


def _validate(perms: Permissions, source: str) -> None:
    if not isinstance(perms, Permissions):
        raise PermissionsError(f"{source}: not a Permissions value")
    if perms.mode != MODE_FULL_HOST:
        raise PermissionsError(
            f"{source}: mode {perms.mode!r} is not available: only {MODE_FULL_HOST!r} is built, "
            f"sandboxed mode is reserved")
    for attribute, label in (("deny_read", "deny_read entry"), ("deny_write", "deny_write entry")):
        entries = getattr(perms, attribute)
        if not isinstance(entries, list):
            raise PermissionsError(f"{source}: {attribute} must be a list of paths")
        for entry in entries:
            try:
                _expand(entry, label)
            except PermissionsError as error:
                raise PermissionsError(f"{source}: {error}") from None
    if not isinstance(perms.protect_primary_checkout, bool):
        raise PermissionsError(f"{source}: protect_primary_checkout must be true or false, got "
                               f"{perms.protect_primary_checkout!r}")
    if not _is_string_list(perms.writable_roots):
        raise PermissionsError(f"{source}: writable_roots must be a list of strings")
    if not _is_string_list(perms.allowed_domains):
        raise PermissionsError(f"{source}: network.allowed_domains must be a list of strings")


# --------------------------------------------------------------------------- the file

def permissions_payload(perms: Permissions) -> Dict[str, Any]:
    """The JSON document stored for ``perms``."""
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": perms.mode,
        "deny_read": list(perms.deny_read),
        "deny_write": list(perms.deny_write),
        "protect_primary_checkout": perms.protect_primary_checkout,
        "writable_roots": list(perms.writable_roots),
        "network": {"allowed_domains": list(perms.allowed_domains)},
    }


def _parse(raw: bytes, source: str) -> Permissions:
    def refuse_repeats(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
        seen: Dict[str, Any] = {}
        for key, value in pairs:
            if key in seen:
                raise PermissionsError(f"{source} repeats the key {key!r}; the last copy would "
                                       f"silently win")
            seen[key] = value
        return seen

    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=refuse_repeats)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PermissionsError(f"{source} is not valid JSON: {error}") from None
    if not isinstance(payload, dict):
        raise PermissionsError(f"{source} must hold one JSON object")
    unknown = sorted(set(payload) - set(_KEYS))
    if unknown:
        raise PermissionsError(f"{source} has unknown key(s): {', '.join(unknown)}")
    missing = [key for key in _KEYS if key not in payload and key not in _OPTIONAL_KEYS]
    if missing:
        raise PermissionsError(f"{source} is missing key(s): {', '.join(missing)}")
    version = payload["schema_version"]
    if type(version) is not int or version != SCHEMA_VERSION:
        raise PermissionsError(f"{source}: schema_version {version!r} is not supported "
                               f"(this launcher reads {SCHEMA_VERSION})")
    network = payload["network"]
    if not isinstance(network, dict):
        raise PermissionsError(f"{source}: network must be an object")
    unknown = sorted(set(network) - set(_NETWORK_KEYS))
    if unknown:
        raise PermissionsError(f"{source} has unknown network key(s): {', '.join(unknown)}")
    if "allowed_domains" not in network:
        raise PermissionsError(f"{source}: network is missing allowed_domains")
    perms = Permissions(mode=payload["mode"], deny_read=payload["deny_read"],
                        protect_primary_checkout=payload["protect_primary_checkout"],
                        deny_write=payload.get("deny_write", list(DEFAULT_DENY_WRITE)),
                        writable_roots=payload["writable_roots"],
                        allowed_domains=network["allowed_domains"])
    _validate(perms, source)
    return perms


def _write(path: Path, text: str, *, replace: bool) -> None:
    """Write ``text`` to ``path`` through a same-directory temp file, fsynced first.

    ``replace=True`` swaps the file in atomically. ``replace=False`` creates it only when absent
    (``link`` fails with ``FileExistsError`` instead of replacing), so default creation can never
    overwrite a file somebody else wrote first.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if replace:
            os.replace(temporary, str(path))
        else:
            os.link(temporary, str(path))
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def _dump(perms: Permissions) -> str:
    # UTF-8, not \u escapes: the file is the user's and a path with an accent should read as one.
    return json.dumps(permissions_payload(perms), indent=2, ensure_ascii=False) + "\n"


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        raise
    except OSError as error:
        raise PermissionsError(f"cannot read {path}: {error}") from None


def load_permissions(state_dir: Path) -> Permissions:
    """The user's permissions list; written with the defaults only when there is no file yet.

    Raises ``PermissionsError`` for anything wrong with an existing file, leaving it untouched.
    """
    path = Path(state_dir) / PERMISSIONS_FILE
    try:
        raw = _read(path)
    except FileNotFoundError:
        try:
            _write(path, _dump(default_permissions()), replace=False)
        except FileExistsError:
            pass  # somebody else created it first: theirs is the file
        except OSError as error:
            raise PermissionsError(f"cannot create {path}: {error}") from None
        try:
            raw = _read(path)
        except FileNotFoundError:
            raise PermissionsError(f"cannot read {path}: it is a dangling link") from None
    return _parse(raw, str(path))


def save_permissions(state_dir: Path, perms: Permissions) -> Path:
    """Validate ``perms`` and replace ``<state>/permissions.json`` atomically; return the path."""
    _validate(perms, "the permissions list")
    path = Path(state_dir) / PERMISSIONS_FILE
    try:
        _write(path, _dump(perms), replace=True)
    except OSError as error:
        raise PermissionsError(f"cannot write {path}: {error}") from None
    return path


# --------------------------------------------------------------------------- conflicts

def check_conflicts(perms: Permissions, *, repo: Path, worktree_root: Path, state_dir: Path,
                    codex_bin: Path, report_dir: Any = None, codex_home: Any = None) -> List[str]:
    """What in this list would break a job, one sentence each (empty when it is safe to run).

    A deny entry that is, or sits above, the repository, the worktree root, the state directory
    or the Codex executable would hide something the job needs. An entry inside the repository
    (``<repo>/.env``) is fine: hiding one file is what the list is for. With
    ``protect_primary_checkout`` the worktree root and state directory must also live outside
    the repository, or the profile's write denial would cover the job's own worktrees.

    A ``deny_write`` entry is applied after the job's own re-openings, so one that is, or sits
    above, the state directory, the job's report directory, the worktree root or the Codex home
    (``report_dir``/``codex_home`` when given) would leave the worker unable to write what it
    needs; it fails silently at run time as ``Operation not permitted``, so it is refused here.
    """
    protected = (("repository", repo), ("worktree root", worktree_root),
                 ("state directory", state_dir), ("Codex executable", codex_bin))
    problems: List[str] = []
    for entry in perms.deny_read:
        roots = _forms(entry)
        for label, path in protected:
            if any(_covers(root, form) for root in roots for form in _path_forms(path)):
                problems.append(f"deny_read entry {entry!r} would hide the {label} {path}; "
                                f"remove it with 'permissions allow-read'")
    job_writes = [("state directory", state_dir), ("report directory", report_dir),
                  ("worktree root", worktree_root), ("Codex home", codex_home)]
    for entry in perms.deny_write:
        roots = _forms(entry)
        for label, path in job_writes:
            if path is not None and any(_covers(root, form) for root in roots
                                        for form in _path_forms(path)):
                problems.append(f"deny_write entry {entry!r} would stop the job writing the {label} "
                                f"{path}; remove it with 'permissions allow-write'")
    if perms.protect_primary_checkout:
        repo_forms = _path_forms(repo)
        for label, path in protected[1:3]:
            if any(_covers(root, form) for root in repo_forms for form in _path_forms(path)):
                problems.append(f"protect_primary_checkout would deny writes to the {label} "
                                f"{path}, which is inside the repository {repo}; keep it outside "
                                f"the repository")
    return problems


# --------------------------------------------------------------------------- the profile

def _quote(value: str) -> str:
    """One path as an SBPL string literal; ``\\`` and ``"`` are the escapes, control bytes refused."""
    _check_text(value, "profile path")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _ancestors(path: str) -> List[str]:
    """Every parent of the absolute ``path``, nearest first, up to and including ``/``."""
    if not os.path.isabs(path):
        raise PermissionsError(f"cannot guard the parents of a relative path: {path!r}")
    parents: List[str] = []
    while True:
        parent = os.path.dirname(path)
        if parent == path:
            return parents
        parents.append(parent)
        path = parent


def _rename_protection(paths: Iterable[str]) -> List[str]:
    """The statement that keeps every real path in ``paths``, and every parent of each, in place.

    Seatbelt matches paths, so renaming (or deleting and replacing) a denied path, or a directory
    above it, moves the thing somewhere the denial no longer reaches. Renaming ``D`` is a write
    to ``D``'s own path, so a literal write denial on the path and on each of its parents (up to
    ``/``) stops that while leaving everything *inside* those parents writable. Returns one
    ``(deny file-write* (literal ...) ...)`` statement, or nothing for no paths. It must come after
    any rule it has to outrank. Anything that must not be moved but is not denied wholesale (the
    state directory's ``permissions.json``) is protected by passing it here.
    """
    guarded: Dict[str, str] = {}
    for path in paths:
        for item in [path, *_ancestors(path)]:
            guarded.setdefault(_fold(item), item)
    if not guarded:
        return []
    return ["(deny file-write* " + " ".join(f"(literal {_quote(item)})"
                                            for item in sorted(guarded.values())) + ")"]


def resolve_git_dirs(repo: Any) -> Tuple[str, str]:
    """``(git_dir, common_dir)`` of the checkout at ``repo``, as real paths.

    Read from the files git itself reads, no subprocess: a ``.git`` directory is both; a ``.git``
    *file* (``gitdir: <path>``, relative to the checkout) names the git dir, which is also the
    common dir unless it holds a ``commondir`` file (a linked worktree: ``git_dir`` is its admin dir
    under ``<main>/.git/worktrees/<name>`` and ``common_dir`` the main repository's ``.git``). So a
    plain checkout, a ``--separate-git-dir`` one and a linked worktree used as the primary each
    resolve to where their refs, config and hooks really live. A directory with no ``.git`` at all
    has nothing to resolve and keeps the nominal ``<repo>/.git`` path. A ``.git`` file that does not
    say ``gitdir:`` is refused rather than guessed, since the wrong guess would leave the real git
    dir writable.
    """
    root = os.path.realpath(str(repo))
    dot_git = os.path.join(root, ".git")
    if os.path.isdir(dot_git):
        git_dir = os.path.realpath(dot_git)
    elif os.path.isfile(dot_git):
        try:
            with open(dot_git, "rb") as handle:
                first = handle.readline(8192).decode("utf-8", "surrogateescape").rstrip("\r\n")
        except OSError as error:
            raise PermissionsError(f"cannot read the gitfile {dot_git}: {error}") from None
        if not first.startswith("gitdir:") or not first[len("gitdir:"):].strip():
            raise PermissionsError(f"{dot_git} is not a gitfile (no 'gitdir: <path>' first line), so "
                                   "the primary checkout's git directory cannot be protected")
        git_dir = os.path.realpath(os.path.join(root, first[len("gitdir:"):].strip()))
    else:
        return dot_git, dot_git
    common = git_dir
    try:
        with open(os.path.join(git_dir, "commondir"), "rb") as handle:
            named = handle.readline(8192).decode("utf-8", "surrogateescape").strip()
    except OSError:
        named = ""
    if named:
        common = os.path.realpath(os.path.join(git_dir, named))
    return git_dir, common


def _inside_state(state: str, allowed_writes: Iterable[Any]) -> List[str]:
    """The real paths of ``allowed_writes`` that lie strictly inside ``state``, each once.

    Only those need re-opening after the state directory is shut: anything outside it is
    writable already (``allow default``), and re-opening a path that *holds* the state (a
    ``TMPDIR`` above it, say) would re-open the whole state directory.
    """
    inside: List[str] = []
    for path in allowed_writes:
        real = os.path.realpath(_check_text(str(path), "writable job path"))
        if _covers(state, real) and not _same(state, real) and not any(_same(real, seen) for seen in inside):
            inside.append(real)
    return inside


def _reals(entries: Iterable[str]) -> List[str]:
    """Real paths of ``entries`` (expanding ``~``), deduplicated by fold, order preserved."""
    out: List[str] = []
    for entry in entries:
        real = _real(entry)
        if not any(_same(real, seen) for seen in out):
            out.append(real)
    return out


def seatbelt_profile(perms: Permissions, *, primary_repo: Path, state_dir: Any = None,
                     allowed_writes: Iterable[Any] = (), protect_write_roots: Iterable[Any] = (),
                     job_git_admin: Any = None, job_worktree: Any = None,
                     git_common_dir: Any = None,
                     additional_deny_read: Iterable[Any] = (),
                     allow_read_metadata: Iterable[Any] = (),
                     allow_read: Iterable[Any] = (),
                     allow_read_subpaths: Iterable[Any] = (),
                     allow_write: Iterable[Any] = (),
                     allow_write_subpaths: Iterable[Any] = (),
                     deny_write_subpaths: Iterable[Any] = ()) -> str:
    """The Seatbelt profile text for ``perms`` (write it as UTF-8).

    ``(allow default)`` first, then denials (and the re-openings named below), in the order that
    makes Seatbelt's last-match rule do the right thing:

    1. the read denials;
    2. with a ``state_dir`` (the launcher's state: this list, its log, reports, ledger, leases,
       locks, every job's worktree), the whole state directory is write-denied, then only the
       job's own ``allowed_writes`` that lie inside it are re-opened (its worktree, Codex home,
       last-message files, scratch). This comes before the rules below, so a job path can never
       re-open the primary checkout or a hidden path;
    3. the primary checkout is made read-only as a whole, then its git directories are shut:
       the *resolved* common dir (``resolve_git_dirs``, plus ``git_common_dir`` when the caller
       knows one -- the launcher passes the job worktree's own ``GIT_COMMON_DIR``) and, when the
       primary is itself a linked worktree, its own admin dir. Never a hardcoded ``<repo>/.git``:
       a ``--separate-git-dir`` primary keeps its refs, config and hooks elsewhere. The common
       directory remains write-denied, including ``objects/``; job commits use a private object
       store with the primary as a read-only alternate. Thus config, hooks, ``info/``, refs,
       ``MERGE_HEAD``, ``index.lock``, ``shallow``, ``reftable/`` and every other job's admin dir
       are refused without a list of planting places to keep complete. When the job's own linked
       worktree is known, its admin dir under ``worktrees/<name>`` is re-opened (so its commit
       still writes index/HEAD/logs) but its ``commondir``, ``gitdir`` and ``config.worktree``
       stay denied, and the worktree's ``.git`` gitfile is denied -- otherwise a worker could
       repoint the launcher's own (unwrapped) ``read-tree``/``add``/``diff`` at a git directory it
       controls, whose config/attributes then run arbitrary filters;
    4. the ``deny_write`` persistence vectors (``~/.gitconfig``, shell rc files,
       ``~/Library/LaunchAgents`` by default) are write-denied;
    5. everything hidden is also write-denied, so a secret cannot be rewritten or replaced, and
       cannot be cloned: ``clonefile(2)`` of a directory checks only that directory, so every
       parent of a hidden path is clone-denied too;
    6. every hidden path, the checkout, its git directories, ``objects/``, the admin redirects,
       the deny_write paths, the state directory, its ``permissions.json``/``permissions.log``,
       and the re-opened job roots (``protect_write_roots``: the worktree, Codex home, scratch)
       -- and every parent of each, up to ``/`` -- are guarded against being renamed, replaced or
       deleted (``_rename_protection``). Their *contents* stay as writable as they were, except
       where an earlier rule says otherwise, so the re-opened roots cannot be swapped for a
       symlink the launcher would then follow. A guarded root has to exist when the profile is
       used: the launcher creates the native scratch and screenshots directories first.

    The job-specific ``additional_deny_read`` and exception arguments are narrowly scoped
    reopenings for integrations such as native Codex cloud auth. ``allow_read_metadata`` may name
    only a directory that is itself denied; it permits metadata on that directory, not reads of
    files below it. Other read/write exceptions must be strictly below a hidden path.

    Without ``state_dir`` there is no state rule (``allowed_writes`` is then unused).

    A side effect of 6: a parent of a hidden path that does not exist yet cannot be created
    inside a job, because the guard denies writing that exact path. With ``~/.docker`` absent,
    for example (``~/.docker/config.json`` is hidden by default), ``mkdir ~/.docker`` and so
    ``docker login`` fail under the wrapper; create the directory outside a job, or lift the
    entry with ``allow-read``.

    Git objects are job-private and the primary objects are a read-only alternate; a feature that
    tries to maintain the primary common directory (``gc --auto``, ``packed-refs``) is refused.
    """
    _validate(perms, "the permissions list")
    lines = ["(version 1)", "(allow default)"]
    hidden = _reals([*perms.deny_read,
                     *[_check_text(str(path), "additional deny-read path")
                       for path in additional_deny_read]])
    read_metadata = _reals([_check_text(str(path), "read-metadata exception")
                            for path in allow_read_metadata])
    read_files = _reals([_check_text(str(path), "read exception") for path in allow_read])
    read_subtrees = _reals([_check_text(str(path), "read exception subtree")
                            for path in allow_read_subpaths])
    write_files = _reals([_check_text(str(path), "write exception") for path in allow_write])
    write_subtrees = _reals([_check_text(str(path), "write exception subtree")
                             for path in allow_write_subpaths])
    for exception in read_metadata:
        is_denied_home = any(_same(root, exception) and Path(exception).is_dir() for root in hidden)
        if not is_denied_home:
            raise PermissionsError("a read-metadata exception must name a denied directory")
    for exception in [*read_files, *read_subtrees, *write_files, *write_subtrees]:
        if not any(_covers(root, exception) and not _same(root, exception) for root in hidden):
            raise PermissionsError("an exception path must be strictly inside a denied path")
    if hidden:
        # Never emitted empty: ``(deny file-read*)`` without a filter would hide every file.
        lines.append("(deny file-read* " + " ".join(f"(subpath {_quote(path)})" for path in hidden) + ")")
        if read_metadata:
            lines.append("(allow file-read-metadata "
                         + " ".join(f"(literal {_quote(path)})" for path in read_metadata) + ")")
        if read_files or read_subtrees:
            filters = ([f"(literal {_quote(path)})" for path in read_files]
                       + [f"(subpath {_quote(path)})" for path in read_subtrees])
            lines.append("(allow file-read* " + " ".join(filters) + ")")
    guarded = list(hidden)
    if state_dir is not None:
        state = os.path.realpath(_check_text(str(state_dir), "state directory"))
        lines.append(f"(deny file-write* (subpath {_quote(state)}))")
        reopened = _inside_state(state, allowed_writes)
        if reopened:
            lines.append("(allow file-write* " + " ".join(f"(subpath {_quote(path)})" for path in reopened) + ")")
        guarded += [state, os.path.join(state, PERMISSIONS_FILE), os.path.join(state, LOG_FILE)]
    for root in protect_write_roots:
        real = os.path.realpath(_check_text(str(root), "write-protected job root"))
        if not any(_same(real, seen) for seen in guarded):
            guarded.append(real)
    if perms.protect_primary_checkout:
        repo = os.path.realpath(str(primary_repo))
        own_git_dir, resolved_common = resolve_git_dirs(repo)
        commons = _reals([resolved_common] + ([str(git_common_dir)] if git_common_dir is not None else []))
        # The primary's own admin dir is a separate directory only when it is a linked worktree.
        own_admin = [] if any(_same(own_git_dir, common) for common in commons) else [own_git_dir]
        shut = [*commons, *own_admin]
        reopened_git = [os.path.join(common, name) for common in commons for name in GIT_WRITABLE_DIRS]
        reshut_git = [os.path.join(common, name) for common in commons for name in GIT_SHUT_BELOW]
        lines.append(f"(deny file-write* (subpath {_quote(repo)}))")
        lines.append("(deny file-write* " + " ".join(f"(subpath {_quote(path)})" for path in shut) + ")")
        if reopened_git:
            lines.append("(allow file-write* " + " ".join(f"(subpath {_quote(path)})" for path in reopened_git) + ")")
        if reshut_git:
            lines.append("(deny file-write* " + " ".join(f"(subpath {_quote(path)})" for path in reshut_git) + ")")
        guarded += [repo, *shut, *reopened_git, *reshut_git]
        if job_git_admin is not None:
            admin = os.path.realpath(str(job_git_admin))
            # The job's own linked-worktree admin dir is re-opened (its commit needs index/HEAD/
            # logs) but the files that redirect where git reads its dir/common-dir/config from
            # stay denied, and cannot be renamed away.
            redirects = [os.path.join(admin, name) for name in GIT_ADMIN_REDIRECTS]
            lines.append(f"(allow file-write* (subpath {_quote(admin)}))")
            lines.append("(deny file-write* " + " ".join(f"(literal {_quote(path)})" for path in redirects) + ")")
            guarded += [admin, *redirects]
        if job_worktree is not None:
            gitfile = os.path.join(os.path.realpath(str(job_worktree)), ".git")
            lines.append(f"(deny file-write* (literal {_quote(gitfile)}))")
            guarded.append(gitfile)
    denied_writes = _reals(perms.deny_write)
    if denied_writes:
        lines.append("(deny file-write* " + " ".join(f"(subpath {_quote(path)})" for path in denied_writes) + ")")
        guarded += denied_writes
    private_write_denials = _reals(deny_write_subpaths)
    if private_write_denials:
        lines.append("(deny file-write* " + " ".join(
            f"(subpath {_quote(path)})" for path in private_write_denials) + ")")
        guarded += private_write_denials
    if hidden:
        lines.append("(deny file-write* " + " ".join(f"(subpath {_quote(path)})" for path in hidden) + ")")
        parents: Dict[str, str] = {}
        for path in hidden:
            for parent in _ancestors(path):
                parents.setdefault(_fold(parent), parent)
        lines.append("(deny file-clone "
                     + " ".join(f"(literal {_quote(parent)})" for parent in sorted(parents.values()))
                     + " " + " ".join(f"(subpath {_quote(path)})" for path in hidden) + ")")
    if write_files or write_subtrees:
        filters = ([f"(literal {_quote(path)})" for path in write_files]
                   + [f"(subpath {_quote(path)})" for path in write_subtrees])
        lines.append("(allow file-write* " + " ".join(filters) + ")")
    lines.extend(_rename_protection(guarded))
    return "\n".join(lines) + "\n"


def wrapper_available() -> bool:
    """``sandbox-exec`` exists and can be run here (cheap: nothing is executed)."""
    return (sys.platform == "darwin" and os.path.isfile(SANDBOX_EXEC)
            and os.access(SANDBOX_EXEC, os.X_OK))


def wrap_argv(argv: Sequence[str], profile_path: Path) -> List[str]:
    """``argv`` run under the profile file. There is no path here that returns a bare command."""
    command = [str(item) for item in argv]
    if not command or not command[0].strip():
        raise PermissionsError("an empty command is never run, wrapped or not")
    profile = str(profile_path)
    if not os.path.isabs(profile) or Path(profile).name in ("", ".", ".."):
        raise PermissionsError(f"the Seatbelt profile must be an absolute file path, got {profile!r}")
    return [SANDBOX_EXEC, "-f", profile, *command]


# --------------------------------------------------------------------------- reading denials

_PHRASE = "operation not permitted"
_NAME = "\\w\\u0300-\\u036f~"  # what continues a path component's name (folded, combining marks too)
_CLOSERS = {")": "(", "]": "[", "}": "{", ">": "<"}
_TRAILING = " \t.,;:"  # punctuation that follows a path in a sentence, never ends one
# Words of the sentence around a path in tool messages (``mv: rename <a> to <b>``, ``could not
# open <a> for writing``, ``open <a> failed``); after a space in a final name they end the path.
_PROSE = frozenset(("to", "for", "from", "into", "onto", "failed", "as", "with", "while",
                    "because", "and", "or"))


def _fold_with_origin(text: str) -> Tuple[str, List[int]]:
    """``text`` folded character by character, and for each folded character the index of the
    character it came from, so a match found in the folded text can be cut from the original."""
    folded: List[str] = []
    origin: List[int] = []
    for index, char in enumerate(text):
        piece = unicodedata.normalize("NFD", char).casefold()
        folded.append(piece)
        origin.extend([index] * len(piece))
    return "".join(folded), origin


def _mention_pattern(root: str) -> "re.Pattern[str]":
    """``root`` (already folded) as a whole path: not the tail of another name, not the head of a
    longer one."""
    return re.compile(r"(?<![" + _NAME + r".-])" + re.escape(root)
                      + r"(?![" + _NAME + r"-]|\.[" + _NAME + r"-])")


def _hard_end(line: str, start: int) -> int:
    """Where the text that can still belong to the path from ``start`` in ``line`` stops.

    It stops at a quote, at ``:`` before a space or the end (``<path>: Operation not
    permitted``), at the message itself, at a space that begins another absolute path, and at a
    closing bracket that nothing in the path opened.
    """
    openers: List[str] = []
    index = start
    while index < len(line):
        char = line[index]
        if char in "'\"`" or line.startswith(_PHRASE, index):
            break
        if char == ":" and (index + 1 == len(line) or line[index + 1].isspace()):
            break
        if char.isspace():
            after = index
            while after < len(line) and line[after].isspace():
                after += 1
            if line.startswith("/", after):
                break
        if char in _CLOSERS.values():
            openers.append(char)
        elif char in _CLOSERS:
            if not openers or openers[-1] != _CLOSERS[char]:
                break
            openers.pop()
        index += 1
    return index


def _path_end(line: str, start: int) -> int:
    """Where the path that continues from ``start`` in the (folded) ``line`` stops.

    Spaces belong to a path (``Application Support``, ``Login Data``), so a path does not simply
    end at one; within the text ``_hard_end`` allows, a space ends it when what follows reads as
    the sentence around the path rather than more of it:

    * the space follows ``,`` or ``;`` (a list: ``NSFilePath=<path>, NSUnderlyingError=...``);
    * otherwise, when a later word still holds a ``/``, more path components follow, so the
      space is inside a name (``Application Support/Google``, ``notes to self/x.txt``);
    * otherwise it ends the path when the next word is nothing but punctuation (``<path> (``,
      ``<a> -> <b>``) or a word of the surrounding sentence (``rename <a> to <b>``, ``open <a>
      for writing``, ``open <a> failed``): see ``_PROSE``.

    A final name made of a prose word and more words (``notes for me.txt``) is therefore cut
    short; the path reported is then the prefix, which still names where the denial happened.
    """
    end = _hard_end(line, start)
    index = start
    while index < end:
        if not line[index].isspace():
            index += 1
            continue
        run = index
        while index < end and line[index].isspace():
            index += 1
        rest = line[index:end].split()
        if line[run - 1] in ",;":
            return run
        if any("/" in word for word in rest):
            continue
        word = rest[0].strip(_TRAILING + "()[]{}<>") if rest else ""
        if not any(char.isalnum() for char in word) or word in _PROSE:
            return run
    return end


def _mentions(line: str, folded: str, origin: Sequence[int],
              patterns: Sequence["re.Pattern[str]"]) -> List[Tuple[int, str]]:
    """``(start, path)`` for every place ``folded`` names a root (a ``_mention_pattern``) or
    something under one.

    The path is cut from the original ``line`` (its own spelling): the known root, then whatever
    continues below it.
    """
    found: List[Tuple[int, str]] = []
    for pattern in patterns:
        for match in pattern.finditer(folded):
            end = match.end()
            below_end = _path_end(folded, end) if folded.startswith("/", end) else end
            head = line[origin[match.start()]:origin[end - 1] + 1]
            below = line[origin[end]:origin[below_end - 1] + 1].rstrip(_TRAILING + "([{<") if below_end > end else ""
            found.append((origin[match.start()], head + below))
    return found


def find_denials(text: str, perms: Permissions, primary_repo: Path, *,
                 state_dir: Any = None, worktree: Any = None,
                 git_dirs: Iterable[Any] = ()) -> List[Dict[str, str]]:
    """The wrapper denials visible in ``text``: ``{"path", "kind"}`` in order of appearance.

    A line saying ``Operation not permitted`` that names a denied-read entry (or a path under
    one) is a ``"read"`` denial; one naming the primary checkout, a ``deny_write`` entry, or
    the launcher's ``state_dir`` (when given) is a ``"write"`` denial. A path that is both (a
    hidden file inside the checkout) is reported once, as a read. ``path`` is the path as the
    line spells it, real or ``~``-expanded, whole even when it contains spaces, and without the
    words of the message around it (``mv: rename <a> to <b>`` gives both paths). A longer sibling
    name (``~/.ssh-backup``) never matches, and case and Unicode form do not matter.

    When ``worktree`` is given, every entry also carries a ``cause``: ``deny_read``,
    ``primary_checkout``, ``persistence_vector`` or ``state_dir`` for a definite protected path,
    and ``unknown`` for a refused write whose message names only a destination inside the job's
    own worktree -- a refused hard link or rename *into* the worktree, where the tool reports the
    (allowed) destination, not the protected source it was refused. Without ``worktree`` the
    entries are the bare ``{"path", "kind"}`` and worktree-only denials are not reported. This is
    a reading of tool output, not proof: ``EPERM`` has other causes, so callers word it as a
    likely cause.

    ``git_dirs`` are the primary's git directories that live outside the checkout (a
    ``--separate-git-dir`` primary, the main repository of a linked-worktree primary): a denial
    naming one is a ``primary_checkout`` write like a denial inside the checkout itself.
    """
    # Write groups in precedence order (first match wins the cause): the job's own worktree is
    # the most specific (it sits inside the state dir), then the primary checkout, the deny_write
    # vectors, and the state directory as a whole.
    write_groups: List[Tuple[str, List[str]]] = []
    if worktree is not None:
        write_groups.append(("unknown", _path_forms(worktree)))
    if perms.protect_primary_checkout:
        primary_forms = _path_forms(primary_repo)
        for git_dir in git_dirs:
            primary_forms.extend(form for form in _path_forms(git_dir) if form not in primary_forms)
        write_groups.append(("primary_checkout", primary_forms))
    denied_writes: List[str] = []
    for entry in perms.deny_write:
        denied_writes.extend(form for form in _forms(entry) if form not in denied_writes)
    if denied_writes:
        write_groups.append(("persistence_vector", denied_writes))
    if state_dir is not None:
        write_groups.append(("state_dir", _path_forms(state_dir)))
    reads: List[str] = []
    for entry in perms.deny_read:
        reads.extend(form for form in _forms(entry) if form not in reads)

    read_patterns = [_mention_pattern(_fold_with_origin(root)[0]) for root in reads]
    group_patterns = [(cause, [_mention_pattern(_fold_with_origin(root)[0]) for root in roots])
                      for cause, roots in write_groups]
    found: List[Dict[str, str]] = []
    seen = set()

    def emit(path: str, kind: str, cause: str) -> None:
        if (path, kind) in seen:
            return
        seen.add((path, kind))
        entry = {"path": path, "kind": kind}
        if worktree is not None:
            entry["cause"] = cause
        found.append(entry)

    for line in text.splitlines():
        folded, origin = _fold_with_origin(line)
        if _PHRASE not in folded:
            continue
        read_hits = _mentions(line, folded, origin, read_patterns)
        read_starts = {start for start, _ in read_hits}
        # Each write start gets the cause of the first group (by precedence) that matched it.
        write_cause: Dict[int, Tuple[str, str]] = {}
        for cause, patterns in group_patterns:
            for start, path in _mentions(line, folded, origin, patterns):
                if start not in read_starts and start not in write_cause:
                    write_cause[start] = (cause, path)
        ordered = [(start, "read", path, "deny_read") for start, path in read_hits]
        ordered += [(start, "write", path, cause) for start, (cause, path) in write_cause.items()]
        for _, kind, path, cause in sorted(ordered, key=lambda hit: hit[0]):
            emit(path, kind, cause)
    return found


# --------------------------------------------------------------------------- the CLI's changes

def normalize_entry(raw: str) -> str:
    """The deny_read spelling of a path the user typed: ``~`` forms kept, others absolute."""
    text = _check_text(raw, "path").strip()
    if text.startswith("~"):
        text = text.rstrip("/") or "~"
    elif os.path.isabs(text):
        text = os.path.normpath(text)
    else:
        text = os.path.abspath(text)  # relative to where the command was run
    _expand(text)  # refuses a ~user that does not exist
    return text


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def record_change(state_dir: Path, action: str, path: str) -> None:
    """Append ``{"at", "action", "path"}`` to ``<state>/permissions.log`` (the ledger reads it)."""
    log = Path(state_dir) / LOG_FILE
    line = json.dumps({"at": _now(), "action": action, "path": path}) + "\n"
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(str(log), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(descriptor, line.encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise PermissionsError(f"the change was saved to {Path(state_dir) / PERMISSIONS_FILE} but "
                               f"{log} could not be written: {error}") from None


@contextlib.contextmanager
def edit_lock(state_dir: Path) -> Iterator[None]:
    """Hold ``<state>/permissions.lock`` (``flock``) across one read-modify-write of the list, so
    two simultaneous ``permissions`` edits both land instead of the last writer winning."""
    directory = Path(state_dir)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(str(directory / LOCK_FILE), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as error:
        raise PermissionsError(f"cannot lock {directory / LOCK_FILE}: {error}") from None
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)   # closing releases the lock


def _add_entry(state_dir: Path, raw: str, attribute: str, action: str) -> Permissions:
    """Add ``raw`` to ``perms.<attribute>`` (a path already there is left as is, not logged)."""
    entry = normalize_entry(raw)
    with edit_lock(state_dir):
        perms = load_permissions(state_dir)
        listing = getattr(perms, attribute)
        if any(_same(_real(existing), _real(entry)) for existing in listing):
            return perms
        listing.append(entry)
        save_permissions(state_dir, perms)
        record_change(state_dir, action, entry)
        return perms


def _remove_entry(state_dir: Path, raw: str, attribute: str, action: str, label: str) -> Permissions:
    """Take ``raw`` off ``perms.<attribute>``, whichever way it is spelled. Defaults are
    removable (the file is the user's); a path only *under* a listed entry is refused with the
    entry to lift, since the list is whole entries."""
    entry = normalize_entry(raw)
    with edit_lock(state_dir):
        perms = load_permissions(state_dir)
        listing = getattr(perms, attribute)
        target = _real(entry)
        kept = [existing for existing in listing if not _same(_real(existing), target)]
        if len(kept) == len(listing):
            covering = [existing for existing in listing if _covers(_real(existing), target)]
            if covering:
                raise PermissionsError(
                    f"{entry} is not listed itself; it is hidden by {attribute} entry "
                    f"{', '.join(repr(item) for item in covering)}: run {action} on that entry to "
                    f"lift it")
            raise PermissionsError(f"{entry} is not in the {attribute} list")
        setattr(perms, attribute, kept)
        save_permissions(state_dir, perms)
        record_change(state_dir, action, entry)
        return perms


def add_deny_read(state_dir: Path, raw: str) -> Permissions:
    """Add ``raw`` to the deny-read list (a path already denied is left as it is and not logged)."""
    return _add_entry(state_dir, raw, "deny_read", "deny-read")


def remove_deny_read(state_dir: Path, raw: str) -> Permissions:
    """Take ``raw`` off the deny-read list, whichever way it is spelled (``~/.ssh`` or full)."""
    return _remove_entry(state_dir, raw, "deny_read", "allow-read", "deny_read")


def add_deny_write(state_dir: Path, raw: str) -> Permissions:
    """Add ``raw`` to the deny-write list (a path already denied is left as it is, not logged)."""
    return _add_entry(state_dir, raw, "deny_write", "deny-write")


def remove_deny_write(state_dir: Path, raw: str) -> Permissions:
    """Take ``raw`` off the deny-write list, whichever way it is spelled."""
    return _remove_entry(state_dir, raw, "deny_write", "allow-write", "deny_write")


def describe(perms: Permissions) -> Dict[str, Any]:
    """What ``permissions show`` prints: the effective list plus whether the wrapper can run."""
    payload = permissions_payload(perms)
    payload["wrapper_available"] = wrapper_available()
    return payload
