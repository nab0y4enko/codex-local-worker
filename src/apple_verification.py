"""Opt-in macOS Seatbelt backend for simulator-dependent verification commands.

The default launcher policy is untouched: this module is only consulted when a job
explicitly opts in with ``--apple-simulator-udid``. Ordinary worker tools keep the
``workspace-write`` Codex sandbox and the pinned local model, and generic verification
keeps running inside ``codex sandbox -P <profile>``.

Design rules:

* Independent native verification is wrapped in ``/usr/bin/sandbox-exec -f <profile>``
  with a **fixed-template** Seatbelt profile. Only argv is passed to the wrapper: there is
  no user-supplied profile text, no shell, and no unsandboxed path.
* The profile is a plain ``(deny default)`` decorated with ``(debug deny)``, the deny-logging
  modifier the installed ``/System/Library/Sandbox/Profiles`` use, so a refusal is logged with the
  offending path instead of surfacing as a pathless ``permissionDenied``. ``(debug deny)`` grants
  nothing and changes no decision; ``(with report)`` is still never used because ``sandbox-exec``
  refuses it on a deny action -- ``report modifier does not apply to deny action``, exit 65.
  Reads stay allowed, writes are bounded to the worker
  worktree, the report-owned per-job scratch, exactly one validated simulator device
  directory, the fixed Apple log/cache directories and exactly one OS-derived per-user
  Darwin temporary root, plus the single literal ``/dev/null`` device node that ``xcrun``
  and friends redirect diagnostic streams to (a standard device exception, never a subpath
  or a filesystem root). Native runner PTYs additionally use a literal /dev/ptmx data-write
  rule and extension-bound slave data writes/ioctls; no other device writes are granted.
  That temporary root is what the OS reports for the current user
  (``confstr CS_DARWIN_USER_TEMP_DIR`` or ``getconf DARWIN_USER_TEMP_DIR``) because
  ``xcodebuild`` and ``xcrun`` reset ``TMPDIR`` to it. That query consults the environment, so
  the grant comes from validating its answer rather than from ignoring the caller: the root is
  canonicalized, must keep the ``/private/var/folders/<bucket>/<user>/T`` shape and belong to
  the current uid, and an unavailable, malformed, over-broad, symlinked or foreign-uid answer
  blocks the job instead of widening to ``/tmp`` or a bare ``/var/folders``. A decoy ``TMPDIR``
  is therefore refused, never granted. It is a shared per-user directory, so the grant is not
  job-only temporary confinement (see ``trust_limits``). The primary checkout, the
  worktree's ``.git``/``.agents``/``.codex`` paths and the resolved gitdir are explicit
  deny-write entries. TCP/UDP access, loopback included, is denied; one validated exact testmanagerd Unix
  socket may be connected outbound for native verification only.
* Apple services are reached through named Mach lookups plus narrow anchored
  ``com.apple.CoreSimulator``/``com.apple.dt`` patterns, never a blanket allowance. The fixed
  list also names the macOS platform helpers whose lookups were observed to be refused while
  Apple resolved simulator runtime metadata (``_dirhelper_userdir``/``coreservicesd``/cfprefsd),
  and grants read-only preference access so those helpers can answer without any preference
  write being possible.
  Because those anchored patterns stop at ``[A-Za-z0-9]+``, the hyphenated
  ``SimLaunchHost-arm64``/``SimLaunchHost-x86`` runtime library loaders are named exactly in
  ``MACH_SERVICES`` instead of widening a pattern.
  The list also names ``com.apple.PowerManagement.control`` exactly: XCTest's test-session
  housekeeping creates its user-idle sleep assertion through that powerd service, which runs
  outside this profile with its own privileges.
* A readiness probe runs inside the *same* profile before any contract command is
  trusted: ``xcodebuild -version``, ``xcrun simctl list devices available --json`` with
  the selected device resolved, a positive bounded write, and two negative checks (a write
  outside the bounded roots -- chosen so it is outside *every* bound, including the per-user
  temporary root -- and a loopback TCP connect) that must be *refused*. A probe
  that does not pass blocks the run instead of degrading to an echo-only pass.
* The wrapped command's Foundation home is redirected with ``CFFIXED_USER_HOME`` to
  ``<scratch>/foundation-home``, created empty inside the report-owned scratch, so the Xcode
  SwiftPM configuration, security and cache lookups that resolve through ``$HOME/Library``
  land in job-owned paths. ``HOME`` and ``CODEX_HOME`` stay exactly as the job passed them
  and nothing is copied out of the real account.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

BACKEND = "apple-seatbelt"
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
XCODEBUILD = "/usr/bin/xcodebuild"
XCRUN = "/usr/bin/xcrun"
TOUCH = "/usr/bin/touch"
PROFILE_NAME = "apple-verify.sb"
DEVICE_FAMILY = "tvos"
MIN_PROBE_BUDGET_SECONDS = 5.0
PROBE_COMMAND_TIMEOUT_SECONDS = 120.0
PROBE_TCP_PORT = 59013

UDID_RE = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
                     r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$")
XCODE_VERSION_RE = re.compile(r"Xcode\s+\d+(?:\.\d+)*")
DENIAL_RE = re.compile(r"operation not permitted|permission denied|\bsandbox(ed| deny)?\b",
                       re.I)

# Fixed profile inputs: nothing here is derived from the task contract or the worker.
DEVICES_RELATIVE = "Library/Developer/CoreSimulator/Devices"
APPLE_WRITE_RELATIVES = (
    "Library/Developer/CoreSimulator/Caches/dyld",
    "Library/Logs/CoreSimulator",
    "Library/Caches/com.apple.dt.Xcode",
)
PROTECTED_NAMES = (".git", ".agents", ".codex")
DEVICE_IO = ("/dev/null", "/dev/zero", "/dev/random", "/dev/urandom", "/dev/tty",
             "/dev/ptmx", "/dev/dtracehelper")
# ``xcrun simctl`` locates its utility with ``xcodebuild ... -find simctl 2> /dev/null``, so
# deny-default must still allow that one standard device node to be opened for writing.
DEVICE_WRITE_PATHS = ("/dev/null",)
# The one writable root the backend cannot name in advance: ``xcodebuild``/``xcrun`` reset
# ``TMPDIR`` to the per-user Darwin temporary directory, so a job-local ``TMPDIR`` cannot
# contain native temporary writes. The value is asked of the OS and then has to survive the
# validation in ``darwin_temp_root`` before it can appear in a profile. The OS query itself
# consults an exported ``TMPDIR``, so safety comes from that validation, not from ignoring
# the environment: any answer that is not this uid's canonical temporary root is refused.
GETCONF = "/usr/bin/getconf"
DARWIN_USER_TEMP_NAME = "CS_DARWIN_USER_TEMP_DIR"
# ``getconf`` takes the name without the ``CS_`` prefix that libc uses: ``getconf
# DARWIN_USER_TEMP_DIR`` prints the root, while ``getconf CS_DARWIN_USER_TEMP_DIR`` exits 64
# with "no such configuration parameter". The fallback argv has to carry the spelling the
# program actually accepts, so the two names are kept apart instead of sharing one constant.
GETCONF_NAME = "DARWIN_USER_TEMP_DIR"
DARWIN_USER_TEMP_CONFSTR = 65537  # sysctl _CS_DARWIN_USER_TEMP_DIR
GETCONF_TIMEOUT_SECONDS = 10.0
DARWIN_TEMP_SHAPE_RE = re.compile(r"^/private/var/folders/[A-Za-z0-9][A-Za-z0-9._-]*/"
                                  r"[A-Za-z0-9][A-Za-z0-9._-]*/T$")
# Foundation resolves ``~/Library`` through this override, which is what actually moves the
# Xcode SwiftPM configuration/security/cache lookups into the report-owned scratch.
FOUNDATION_HOME_ENV = "CFFIXED_USER_HOME"
FOUNDATION_HOME_RELATIVE = "foundation-home"
# The negative write probe must land outside every bound. The scratch's parent is the natural
# place, and the canonical host temp dir is the fallback for the case where the scratch itself
# sits under a granted root (the per-user temporary directory is a granted root).
NEGATIVE_PROBE_MARKER = "seatbelt-negative-write"
HOST_TEMP_FALLBACK = "/private/tmp"
MACH_SERVICES = (
    "com.apple.CoreSimulator.CoreSimulatorService",
    "com.apple.CoreSimulator.simdiskimaged",
    # exact names, never a widened pattern: ``launchctl list`` and the CoreSimulator binary's
    # own strings (alongside ``SimLaunchHostConnection.loadLibLaunchAtURL``) identify these XPC
    # services as the runtime library loaders, and the anchored ``[A-Za-z0-9]+`` pattern below
    # excludes their hyphens - which is why ``simctl`` reported runtimes unavailable with
    # "liblaunch_sim.dylib could not be opened" while ctypes could open that dylib directly.
    "com.apple.CoreSimulator.SimLaunchHost-arm64",
    "com.apple.CoreSimulator.SimLaunchHost-x86",
    "com.apple.dt.XCBuildService",
    "com.apple.dt.simulaunchd",
    "com.apple.cfprefsd",
    "com.apple.notifyd.register",
    "com.apple.notifyd.match",
    "com.apple.logd",
    "com.apple.logd.events",
    "com.apple.system.logger",
    "com.apple.system.opendirectoryd.libinfo",
    "com.apple.SecurityServer",
    "com.apple.trustd",
    "com.apple.coreservices.launchservicesd",
    "com.apple.lsd.mapdb",
    "com.apple.xpc.launchd",
    "com.apple.fonts",
    # observed refusals behind "liblaunch_sim.dylib could not be opened" while host metadata
    # proved the runtimes installed: _dirhelper_userdir and coreservicesd bootstrap lookups,
    # plus the cfprefsd pair that resolves the preference reads those helpers perform.
    "com.apple.bsd.dirhelper",
    "com.apple.CoreServices.coreservicesd",
    "com.apple.cfprefsd.daemon",
    "com.apple.cfprefsd.agent",
    # exact global name, never a pattern: XCTest test-session housekeeping calls
    # IOPMAssertionCreateWithName to hold a user-idle sleep assertion, and that request is
    # delegated to powerd outside this profile. The refusal surfaced before any test ran as
    # "Failed to prevent system sleep during UI testing. Error code: -536870199", with the live
    # kernel line "deny(1) mach-lookup com.apple.PowerManagement.control" as the only denial at
    # that instant. Nothing else is granted here: no IOKit, job-creation, preference or
    # power-domain operation, and no power preference is read, written or changed.
    "com.apple.PowerManagement.control",
    # Observed during XCTest bootstrap: the host test daemon provides diagnostic
    # collection; DTServiceHub needs its bundled arbiter to establish simulator
    # process-control transport. Multi-component XPC name does not match the
    # existing anchored com.apple.dt.<single-component> pattern. No job-creation,
    # registration, network or namespace-wide permission is added.
    "com.apple.testmanagerd.control",
    "com.apple.dt.instruments.dtarbiter.xpc",
    "com.apple.dt.instruments.dtsecurity.xpc",
)
MACH_NAME_PATTERNS = (r"^com\.apple\.CoreSimulator\.[A-Za-z0-9]+$",
                      r"^com\.apple\.dt\.[A-Za-z0-9]+$")
# ``com.apple.cfprefsd.agent`` is registered in the per-session Mach namespace, so Seatbelt has
# to name it with ``local-name`` as well; these stay exact names, never a namespace wildcard.
MACH_LOCAL_NAMES = ("com.apple.cfprefsd.agent",)
# the same helpers only need to read preferences; user-preference-write is never granted
PREFERENCE_READ_OPERATIONS = ("user-preference-read",)

# Fixed argv for the negative loopback check: it distinguishes a sandbox refusal from an
# unused port, so a merely closed port can never be reported as confinement evidence.
CONNECT_PROBE = (
    "import errno, socket, sys\n"
    "sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
    "sock.settimeout(2.0)\n"
    "try:\n"
    "    sock.connect(('127.0.0.1', int(sys.argv[1])))\n"
    "except OSError as error:\n"
    "    print('DENIED' if error.errno in (errno.EPERM, errno.EACCES) else 'REFUSED')\n"
    "else:\n"
    "    print('CONNECTED')\n"
    "finally:\n"
    "    sock.close()\n"
)


class ApplePolicyError(ValueError):
    """The requested Apple verification setup is unsafe or unusable; nothing was run."""


def validate_simulator_udid(value: str) -> str:
    """Accept only a bare UUID: it becomes a path component of the writable roots."""
    if not isinstance(value, str):
        raise ApplePolicyError(f"simulator UDID must be a string, got {type(value).__name__}")
    candidate = value.strip()
    if not UDID_RE.match(candidate):
        raise ApplePolicyError(
            f"simulator UDID {value!r} is not a bare UUID like "
            "1E3A2B4C-5D6E-4F70-8A9B-0C1D2E3F4A5B")
    return candidate


def _redact(text: str, secrets: Sequence[str]) -> str:
    for secret in secrets:
        if secret and len(secret) >= 4:
            text = text.replace(secret, "[REDACTED]")
    return text


def _tail(text: str, limit: int = 300) -> str:
    text = " ".join(text.split())
    return text[-limit:] if len(text) > limit else text


def _scheme_string(value: str) -> str:
    """Encode one absolute path as a Seatbelt string literal, refusing control bytes."""
    if "\x00" in value or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise ApplePolicyError(f"path contains a control character and cannot be "
                               f"placed in a profile: {value!r}")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _within(child: Any, parent: Any) -> bool:
    try:
        Path(os.path.realpath(str(child))).relative_to(os.path.realpath(str(parent)))
        return True
    except ValueError:
        return False


def _canonical_dir(path: Any, label: str, *, must_exist: bool = True) -> Path:
    candidate = Path(str(path))
    if not candidate.is_absolute():
        raise ApplePolicyError(f"{label} must be an absolute path, got {path}")
    if candidate.is_symlink():
        raise ApplePolicyError(f"{label} must not be a symlink: {candidate}")
    resolved = Path(os.path.realpath(str(candidate)))
    if must_exist and not resolved.is_dir():
        raise ApplePolicyError(f"{label} is not an existing directory: {candidate}")
    return resolved


def _confstr_darwin_user_temp_dir() -> Optional[str]:
    """Ask libc for ``_CS_DARWIN_USER_TEMP_DIR``; both spellings are OS queries.

    The numeric constant comes first because some CPython builds reject the Apple-only string
    name (``ValueError: unrecognized configuration name``) while ``getconf`` still answers.
    """
    for name in (DARWIN_USER_TEMP_CONFSTR, DARWIN_USER_TEMP_NAME):
        try:
            value = os.confstr(name)
        except (OSError, ValueError, TypeError):
            continue
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _getconf_darwin_user_temp_dir() -> Optional[str]:
    """``/usr/bin/getconf DARWIN_USER_TEMP_DIR``: the same query with fixed argv."""
    if not os.path.isfile(GETCONF):
        return None
    try:
        outcome = subprocess.run([GETCONF, GETCONF_NAME], capture_output=True,
                                 text=True, timeout=GETCONF_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if outcome.returncode != 0:
        return None
    reported = str(outcome.stdout or "").strip()
    return reported or None


def darwin_user_temp_dir() -> Optional[str]:
    """What the OS reports as this user's temporary root, or ``None`` when it cannot say."""
    return _confstr_darwin_user_temp_dir() or _getconf_darwin_user_temp_dir()


def darwin_temp_root(*, resolver: Optional[Callable[[], Optional[str]]] = None) -> Path:
    """The one OS-derived writable root, validated before it may enter a profile.

    At runtime the resolver is ``darwin_user_temp_dir``; a test may inject a different
    *resolver*, never a caller-supplied path. The answer is canonicalized, must keep the
    fixed ``/private/var/folders/<bucket>/<user>/T`` shape, must be an existing real directory
    owned by the current uid and must not be a symlink, so ``/``, a bare ``/var/folders``
    bucket, another user's root and a planted link are all refused. Canonicalizing before the
    shape check is what makes an ancestor symlink harmless: the grant is the canonical root
    only. When the OS cannot report the root the backend fails closed -- it never widens to
    ``/tmp`` or a bare ``/var/folders`` to keep going.
    """
    resolve = resolver or darwin_user_temp_dir
    try:
        reported = resolve()
    except (OSError, ValueError, TypeError) as error:
        raise ApplePolicyError(f"the current-user Darwin temporary directory could not be "
                               f"resolved: {error}") from error
    if not isinstance(reported, str) or not reported.strip():
        raise ApplePolicyError(
            "the OS did not report a current-user Darwin temporary directory "
            f"(confstr {DARWIN_USER_TEMP_NAME} #{DARWIN_USER_TEMP_CONFSTR} or "
            f"{GETCONF} {GETCONF_NAME}), so the Apple backend fails closed "
            "instead of widening /tmp or a bare /var/folders: native temporary writes follow "
            "that root whether the job likes it or not")
    candidate = reported.strip()
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in candidate):
        raise ApplePolicyError("the current-user Darwin temporary directory contains a control "
                               f"character and cannot be placed in a profile: {candidate!r}")
    root = _canonical_dir(candidate, "current-user Darwin temporary directory")
    if not DARWIN_TEMP_SHAPE_RE.match(str(root)):
        raise ApplePolicyError(
            "the current-user Darwin temporary directory must be the canonical "
            "/private/var/folders/<bucket>/<user>/T root; /tmp, /private/tmp, a bare bucket or "
            f"user directory and any wider root are never granted: {str(root)!r}")
    try:
        owner = os.stat(str(root)).st_uid
    except OSError as error:
        raise ApplePolicyError(f"cannot inspect the current-user Darwin temporary directory "
                               f"{root}: {error}") from error
    if owner != os.getuid():
        raise ApplePolicyError(f"the current-user Darwin temporary directory {root} is owned by "
                               f"uid {owner}, not the current uid {os.getuid()}, so it is "
                               "someone else's temporary root")
    return root


def simulator_device_dir(home: Path, simulator_udid: str) -> Path:
    """The one device directory the profile allows, validated against the device root."""
    udid = validate_simulator_udid(simulator_udid)
    devices = _canonical_dir(Path(str(home)) / DEVICES_RELATIVE, "simulator device root")
    device = devices / udid
    if device.is_symlink():
        raise ApplePolicyError(f"simulator device directory must not be a symlink: {device}")
    resolved = Path(os.path.realpath(str(device)))
    if resolved.parent != devices or resolved.name.lower() != udid.lower():
        raise ApplePolicyError(f"simulator device {device} does not resolve inside {devices}")
    if not resolved.is_dir():
        raise ApplePolicyError(
            f"selected simulator {udid} has no device directory under {devices}; create or "
            "boot it first, because the profile never grants the whole device root")
    return resolved


def resolved_gitdir(worktree: Path) -> Optional[Path]:
    """Resolve a worktree ``.git`` file or directory so it can be denied explicitly."""
    marker = Path(str(worktree)) / ".git"
    if marker.is_dir():
        return Path(os.path.realpath(str(marker)))
    try:
        text = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    raw = text[len("gitdir:"):].strip()
    if not raw:
        return None
    candidate = Path(raw) if os.path.isabs(raw) else Path(str(worktree)) / raw
    return Path(os.path.realpath(str(candidate)))


TESTMANAGERD_SOCKET_ROOT = Path("/private/var/tmp")
TESTMANAGERD_SOCKET_NAME = "com.apple.testmanagerd.unix-domain.socket"


def testmanagerd_socket(simulator_udid=None, *, candidates=None, root=TESTMANAGERD_SOCKET_ROOT) -> Optional[Path]:
    """Discover one current-user private launchd socket; never accept task/env paths.

    No match adds no permission. Ambiguous, symlinked, broad or non-socket
    candidates fail closed. This validates the local endpoint, not daemon code identity.
    ``root``/``candidates`` are deterministic filesystem test seams, not CLI inputs.
    """
    root = Path(root).resolve(strict=True)
    if candidates is None:
        if simulator_udid is None:
            return None
        validate_simulator_udid(simulator_udid)
        try:
            result = subprocess.run(
                [XCRUN, "simctl", "getenv", simulator_udid, "TESTMANAGERD_SIM_SOCK"],
                capture_output=True, text=True, timeout=10,
                env={"PATH": "/usr/bin:/bin", "HOME": os.path.expanduser("~")})
        except (OSError, subprocess.SubprocessError) as error:
            raise ApplePolicyError(f"cannot discover selected-simulator testmanagerd socket: {error}") from error
        if result.returncode != 0 or not result.stdout.strip():
            return None  # No authoritative endpoint: no permission, never a directory scan.
        candidates = [Path(line) for line in result.stdout.strip().splitlines()]
    if len(candidates) > 1:
        raise ApplePolicyError("ambiguous current-user testmanagerd socket; no IPC grant")
    if not candidates:
        return None
    endpoint = Path(candidates[0])
    parent = endpoint.parent
    if (endpoint.name != TESTMANAGERD_SOCKET_NAME or parent.parent != root or
            not re.fullmatch(r"com\.apple\.launchd\.[A-Za-z0-9]+", parent.name)):
        raise ApplePolicyError("testmanagerd socket is outside the exact launchd endpoint shape")
    try:
        directory_info, socket_info = parent.lstat(), endpoint.lstat()
        if (not stat.S_ISDIR(directory_info.st_mode) or directory_info.st_uid != os.getuid()
                or stat.S_IMODE(directory_info.st_mode) & 0o077
                or not stat.S_ISSOCK(socket_info.st_mode) or socket_info.st_uid != os.getuid()
                or endpoint.resolve(strict=True) != endpoint):
            raise ApplePolicyError("testmanagerd requires a real current-user socket in a private unsymlinked directory")
    except OSError as error:
        raise ApplePolicyError(f"cannot validate testmanagerd socket: {error}") from error
    return endpoint


def testmanagerd_grant(endpoint: Optional[Path]) -> str:
    return f"(allow network-outbound (literal {_scheme_string(str(endpoint))}))" if endpoint else ""


def _plan(worktree: Path, primary: Path, job_temp: Path, simulator_udid: str,
          home: Path,
          darwin_temp_resolver: Optional[Callable[[], Optional[str]]] = None
          ) -> Dict[str, Any]:
    """Canonicalize every root once, then refuse anything wider than the policy allows."""
    home_dir = _canonical_dir(home, "home directory")
    primary_dir = _canonical_dir(primary, "primary checkout")
    worktree_dir = _canonical_dir(worktree, "worker worktree")
    scratch_dir = _canonical_dir(job_temp, "per-job verification scratch")
    device = simulator_device_dir(home_dir, simulator_udid)

    if _within(worktree_dir, primary_dir):
        raise ApplePolicyError(f"the worker worktree {worktree_dir} must live outside the "
                               f"primary checkout {primary_dir}")
    if _within(scratch_dir, worktree_dir):
        raise ApplePolicyError(f"the verification scratch {scratch_dir} must live outside the "
                               f"worker-writable worktree {worktree_dir}")
    if _within(scratch_dir, primary_dir):
        raise ApplePolicyError(f"the verification scratch {scratch_dir} must live outside the "
                               f"primary checkout {primary_dir}")

    allowed = [worktree_dir, scratch_dir, device]
    allowed += [home_dir / relative for relative in APPLE_WRITE_RELATIVES]
    for root in allowed:
        resolved = _canonical_dir(root, f"writable root {root}", must_exist=False)
        if str(resolved) == os.sep:
            raise ApplePolicyError("the filesystem root is never a writable root")
        if _within(home_dir, resolved):
            raise ApplePolicyError(f"{root} would grant writes to the whole home directory")
        if _within(primary_dir, resolved):
            raise ApplePolicyError(f"{root} would grant writes to the primary checkout")
        if not _within(resolved, home_dir) and resolved not in (worktree_dir, scratch_dir):
            raise ApplePolicyError(f"{root} escapes both the home anchors and the job roots")

    # The per-user Darwin temporary root is resolved after that loop on purpose: it is the one
    # granted root that legitimately lives outside the home anchors and the job roots, and it
    # carries its own stricter validation (OS-reported, fixed shape, current-uid owned). The
    # deny-after-allow ordering below still wins, so a primary checkout or a worktree `.git`
    # nested inside this shared temporary directory stays denied.
    darwin_temp = darwin_temp_root(resolver=darwin_temp_resolver)
    if darwin_temp not in allowed:
        allowed.append(darwin_temp)

    # Seatbelt keeps the last matching rule, so a denied worktree would silence its own
    # allow entry and make the job unable to build. Only the primary checkout, the protected
    # metadata names inside the worktree and the resolved gitdir are denied by name.
    denied = [primary_dir]
    denied += [worktree_dir / name for name in PROTECTED_NAMES]
    gitdir = resolved_gitdir(worktree_dir)
    if gitdir is not None:
        denied.append(gitdir)
    unique: List[Path] = []
    for entry in denied:
        if entry not in unique:
            unique.append(entry)
    return {"home": home_dir, "primary": primary_dir, "worktree": worktree_dir,
            "scratch": scratch_dir, "device": device, "allowed": allowed, "denied": unique,
            "darwin_temp": darwin_temp, "testmanagerd_socket": testmanagerd_socket(simulator_udid),
            "udid": validate_simulator_udid(simulator_udid)}


def writable_roots(worktree: Path, primary: Path, job_temp: Path, simulator_udid: str,
                   home: Path,
                   darwin_temp_resolver: Optional[Callable[[], Optional[str]]] = None
                   ) -> List[Path]:
    return list(_plan(worktree, primary, job_temp, simulator_udid, home,
                      darwin_temp_resolver=darwin_temp_resolver)["allowed"])


def _write_statement(action: str, roots: Sequence[Path]) -> str:
    entries = " ".join(f"(subpath {_scheme_string(str(root))})" for root in roots)
    return f"({action} file-write* {entries})"


def _device_write_entries() -> List[str]:
    """Validate the device write exception: one canonical character device under /dev.

    A directory, a filesystem root, an uncanonicalized path or anything that is not an
    existing device node is refused, so the exception can never widen into a writable subtree.
    """
    entries: List[str] = []
    for path in DEVICE_WRITE_PATHS:
        candidate = str(path)
        try:
            is_character_device = stat.S_ISCHR(os.stat(candidate).st_mode)
        except OSError:
            is_character_device = False
        if (not is_character_device or Path(candidate).parent != Path("/dev")
                or candidate != os.path.realpath(candidate)):
            raise ApplePolicyError(
                "the device write exception accepts only a canonical character device node "
                f"directly under /dev, never a directory, subpath or filesystem root: {path!r}")
        entries.append(f"(literal {_scheme_string(candidate)})")
    return entries


def pty_rules() -> str:
    """Native stdio: a literal PTY master plus kernel-extension-bound slave access.

    The extension is issued by macOS to the allocating sandbox, as used by Apple's
    application.sb. The anchored name alone never authorizes another session's PTY.
    No device-directory writes or metadata modification are permitted by these rules.
    """
    try:
        master = os.lstat("/dev/ptmx")
    except OSError as error:
        raise ApplePolicyError(f"native PTY master is unavailable: {error}") from error
    if not stat.S_ISCHR(master.st_mode):
        raise ApplePolicyError("native PTY master must be the real /dev/ptmx character device")
    return '\n'.join([
        '(allow pseudo-tty)',
        '(allow file-write-data (literal "/dev/ptmx"))',
        '(allow file-write-data file-ioctl '
        '(require-all (regex #"^/dev/ttys[0-9]+$") (extension "com.apple.sandbox.pty")))',
    ])


def build_profile(worktree: Path, primary: Path, job_temp: Path, simulator_udid: str,
                  home: Path,
                  darwin_temp_resolver: Optional[Callable[[], Optional[str]]] = None) -> str:
    """Fixed-template deny-default profile; only validated absolute paths are encoded."""
    plan = _plan(worktree, primary, job_temp, simulator_udid, home,
                 darwin_temp_resolver=darwin_temp_resolver)
    mach_names = " ".join(f'(global-name {_scheme_string(name)})' for name in MACH_SERVICES)
    mach_patterns = " ".join(f'(global-name #"{pattern}")' for pattern in MACH_NAME_PATTERNS)
    mach_local = " ".join(f'(local-name {_scheme_string(name)})' for name in MACH_LOCAL_NAMES)
    preference_ops = " ".join(PREFERENCE_READ_OPERATIONS)
    # an empty fixed list has to narrow the profile, never yield a filter-less rule that
    # Seatbelt reads as a blanket mach-lookup or preference grant
    helper_grants = [
        "; named platform helpers only: the per-session cfprefsd agent is looked up in the",
        "; local Mach namespace too, and the operations below are READ-only, so simulator",
        "; runtime metadata can be resolved with no broader grant",
    ]
    if mach_local:
        helper_grants.append(f"(allow mach-lookup {mach_local})")
    if preference_ops:
        helper_grants.append(f"(allow {preference_ops})")
    devices = " ".join(f'(literal {_scheme_string(path)})' for path in DEVICE_IO)
    device_writes = _device_write_entries()
    lines = [
        "; local-implement Apple Seatbelt verification profile.",
        "; Generated by src/apple_verification.py from a fixed template: regenerate, never",
        "; edit, and never feed caller-supplied text into this file. Every path below is a",
        "; canonicalized absolute literal that the backend validated before writing.",
        "(version 1)",
        "(deny default)",
        "; deny diagnostics only, the same modifier the installed Apple profiles use: a denied",
        "; operation is logged with its path, and nothing here is allowed or widened",
        "(debug deny)",
        "",
        "; untrusted commands may run their own build/test subprocesses",
        "(allow process-fork process-exec process-info* signal)",
        "(allow file-map-executable)",
        "(allow sysctl-read)",
        "",
        "; reads are unrestricted: this profile bounds writes, it is not a read-secrecy fix",
        "(allow file-read* file-test-existence file-search)",
        "",
        "; minimal standard device I/O (dtracehelper is the log/dirhelper channel)",
        f"(allow file-ioctl {devices})",
        "",
        "; shared memory and the named Apple IPC that CoreSimulator and XCBuild require",
        "(allow ipc-posix-shm-read* ipc-posix-shm-write-create ipc-posix-shm-write-data)",
        f"(allow mach-lookup {mach_names})",
        f"(allow mach-lookup {mach_patterns})",
        "",
        *helper_grants,
        "",
        "; deny TCP/UDP, loopback and unrelated Unix sockets; one exact local IPC follows",
        "(deny network* network-bind network-inbound network-outbound)",
        testmanagerd_grant(plan["testmanagerd_socket"]),
        "",
        "; writes: bounded to the job roots, one validated device, the fixed Apple dirs and",
        "; the single OS-reported per-user temporary root (shared with that user's other",
        "; temporary files - it is a platform-managed root, not a job-private directory)",
        _write_statement("allow", plan["allowed"]),
        "",
        "; never the primary checkout, never git or agent metadata",
        _write_statement("deny", plan["denied"]),
    ]
    if device_writes:
        lines += [
            "",
            "; one standard device exception, kept last so Seatbelt's last-match order holds it",
            "; above the denies: compilers, xcodebuild and simctl all redirect streams to",
            "; /dev/null. It is one literal device node - no subpath, no directory and no new",
            "; filesystem root becomes writable, and every other bound above still applies.",
            f"(allow file-write* {' '.join(device_writes)})",
        ]
    lines += ["", "; native test-runner stdio: PTY slaves require their kernel-issued extension", pty_rules()]
    return "\n".join(lines) + "\n"


def _negative_probe_target(plan: Dict[str, Any], token: str) -> Path:
    """Choose the negative-write path so it is outside *every* bound, or refuse the run.

    The scratch's parent is the natural place, but the per-user Darwin temporary root is now a
    granted root, so a scratch kept under the host temp would otherwise probe a path the
    profile allows -- and a refusal there would be reported as confinement that does not exist.
    Every candidate that lies inside a granted or denied root is skipped, and the path has to
    sit in an existing writable directory so a broken profile really would create it. With no
    usable candidate the backend refuses to claim write confinement at all.
    """
    marker = f"{NEGATIVE_PROBE_MARKER}-{token}"
    bounds = list(plan["allowed"]) + list(plan["denied"])
    for base in (Path(str(plan["worktree"])).parent, Path(HOST_TEMP_FALLBACK)):
        candidate = base / marker
        if any(_within(candidate, bound) for bound in bounds):
            continue
        if candidate.is_symlink() or not base.is_dir() or not os.access(str(base), os.W_OK):
            continue
        return candidate
    raise ApplePolicyError(
        "no out-of-bounds path is available for the negative write check, so this backend "
        "cannot prove it confines writes; refusing to claim write confinement rather than "
        "probing a path the profile allows")


def profile_sha256(profile: str) -> str:
    return hashlib.sha256(profile.encode("utf-8")).hexdigest()


def sandbox_argv(profile_path: Path, argv: Sequence[str],
                 *, sandbox_exec: str = SANDBOX_EXEC) -> List[str]:
    """Wrap a contract command in the profile. There is no path that returns bare argv."""
    command = [str(item) for item in argv]
    if not command or not command[0].strip():
        raise ApplePolicyError("an empty command is never run, sandboxed or not")
    raw = str(profile_path).strip()
    if not raw or Path(raw).name in ("", ".", ".."):
        raise ApplePolicyError(f"the Seatbelt profile must name a real profile file, got "
                               f"{str(profile_path)!r}")
    profile = Path(raw)
    return [str(sandbox_exec), "-f", str(profile), *command]


def sandbox_argv_template(*, sandbox_exec: str = SANDBOX_EXEC) -> List[str]:
    return [str(sandbox_exec), "-f", "<profile>", "<command>", "..."]


def verification_env(env: Dict[str, str], job_temp: Path) -> Dict[str, str]:
    """Bounded temp, module-cache and Foundation locations; HOME/CODEX_HOME untouched.

    ``TMPDIR`` alone does not contain the toolchain: ``xcodebuild``/``xcrun`` reset it to the
    per-user Darwin root, which is why the profile grants that root explicitly. Foundation
    resolves ``~/Library`` through ``CFFIXED_USER_HOME`` instead, so that is what moves the
    Xcode SwiftPM configuration, security and cache lookups into job-owned paths. The
    directory is created empty inside the report-owned scratch and nothing is copied from the
    real account; ``HOME`` and ``CODEX_HOME`` are passed through as the job had them.
    """
    scratch = Path(os.path.realpath(str(job_temp)))
    tmp_dir = scratch / "tmp"
    module_cache = scratch / "ModuleCache"
    foundation_home = scratch / FOUNDATION_HOME_RELATIVE
    for path in (tmp_dir, module_cache, foundation_home):
        path.mkdir(parents=True, exist_ok=True)
    bounded = dict(env)
    bounded["TMPDIR"] = str(tmp_dir)
    bounded["TMP"] = str(tmp_dir)
    bounded["TEMP"] = str(tmp_dir)
    bounded["CLANG_MODULE_CACHE_PATH"] = str(module_cache)
    bounded[FOUNDATION_HOME_ENV] = str(foundation_home)
    # actool/ibtool pools otherwise reuse an ibtoold confined to an earlier job's
    # scratch paths. Run each compiler in-process under this command's profile.
    bounded["IBToolNeverDeque"] = "1"
    return bounded


def trust_limits() -> Tuple[str, ...]:
    """What the Apple backend does *not* promise, reported with every opted-in job."""
    return (
        "Apple backend: the Seatbelt profile is deny-default for writes but file-read* is "
        "host-wide, so anything a native verification command reads is disclosed.",
        "Apple backend: the allowed Mach services (CoreSimulator, simdiskimaged, XCBuild, "
        "securityd/trustd, launchservicesd, ...) run outside this profile and act with their "
        "own privileges, so work delegated to them is not confined by these write limits.",
        "Apple backend: writes are bounded to the worktree, the report-owned scratch, one "
        "simulator device directory, the fixed Apple log/cache directories, this user's "
        "OS-reported Darwin temporary root, /dev/null and the separately extension-bound PTYs; "
        "Xcode state outside those paths (for example "
        "~/Library/Developer/Xcode/DerivedData) is denied rather than redirected, so a "
        "command that needs it fails instead of escaping.",
        "Apple backend: that per-user Darwin temporary root is the shared, platform-managed "
        "directory the OS reports (xcodebuild and xcrun reset TMPDIR to the confstr value, so "
        "a job-local TMPDIR cannot contain their temporary writes). It holds that user's other "
        "temporary files too, so writes there are NOT confined to this job's files: job-only "
        "temporary confinement is not claimed.",
        "Apple backend: CFFIXED_USER_HOME redirects Foundation - including the Xcode SwiftPM "
        "configuration, security and cache lookups - into <scratch>/foundation-home, created "
        "empty inside the report-owned scratch with nothing copied from the real account. "
        "HOME and CODEX_HOME are passed through unchanged, so the real account stays readable.",
        "Apple backend: the wrapped command cannot open TCP sockets, loopback included, but "
        "the simulator services it talks to are not confined by this profile.",
        "Apple backend: native verification may connect outbound to one exact validated "
        "current-user testmanagerd Unix socket. This does not grant bind, inbound, other "
        "Unix sockets, socket writes or TCP/UDP; testmanagerd runs outside this profile. "
        "Endpoint ownership is validated, not cryptographic daemon identity.",
        "Apple backend: native commands may allocate PTYs via the literal /dev/ptmx. Slave "
        "data writes and ioctls require both an anchored /dev/ttys<digits> path and the "
        "macOS-issued com.apple.sandbox.pty extension. No terminal metadata or broad device "
        "writes are granted. The native command may allocate multiple PTYs and hand their "
        "descriptors to its test-runner subprocesses; this is not a single-pair quota.",
        "Apple backend: the profile comes from a fixed template and the report records its "
        "SHA-256 and exact writable roots, so a reviewer can confirm what actually ran.",
    )


def _decode(raw: Any) -> str:
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return str(raw or "")


def _write_profile(profile_path: Path, text: str, *, worktree: Path) -> Path:
    profile = Path(str(profile_path))
    if not profile.is_absolute():
        profile = Path(os.path.abspath(str(profile)))
    if _within(profile, worktree):
        raise ApplePolicyError(f"the Seatbelt profile must not live inside the "
                               f"worker-writable worktree: {profile}")
    try:
        profile.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(str(profile), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(str(profile), 0o600)
    except OSError as error:
        raise ApplePolicyError(f"cannot store the Seatbelt profile under "
                               f"{profile.parent}: {error}") from error
    return profile


def _check(name: str, argv: Sequence[str], ok: bool, returncode: Optional[int],
           detail: str) -> Dict[str, Any]:
    return {"name": name, "argv": [str(item) for item in argv], "ok": bool(ok),
            "returncode": returncode, "detail": detail}


def is_family_runtime(runtime: Any) -> bool:
    """True when the device-family token appears in a simctl runtime identifier.

    ``simctl`` keys are either short (``tvOS-18.0``) or full identifiers
    (``com.apple.CoreSimulator.SimRuntime.tvOS-18-0``), so the family is matched as a whole
    alphanumeric token: ``iOS``, ``xROS`` and ``watchOS`` must never be read as ``tvOS``.
    """
    tokens = [token.lower() for token in re.split(r"[^0-9A-Za-z]+", str(runtime)) if token]
    return DEVICE_FAMILY in tokens


def _family_devices(payload: Any, udid: str) -> Tuple[List[Dict[str, Any]],
                                                       Optional[Dict[str, Any]],
                                                       List[str]]:
    devices = payload.get("devices") if isinstance(payload, dict) else None
    if not isinstance(devices, dict):
        return [], [], []
    family: List[Dict[str, Any]] = []
    selected: Optional[Dict[str, Any]] = None
    runtimes: List[str] = []
    for runtime, entries in devices.items():
        if not is_family_runtime(runtime):
            continue
        runtimes.append(str(runtime))
        for entry in (entries if isinstance(entries, list) else []):
            if not isinstance(entry, dict) or not entry.get("udid"):
                continue
            if not entry.get("isAvailable"):
                continue
            family.append(entry)
            if str(entry["udid"]).strip().lower() == udid.lower():
                selected = entry
    return family, selected, sorted(runtimes)


def probe_apple(worktree: Path, primary: Path, job_temp: Path, profile_path: Path,
                simulator_udid: str, env: Dict[str, str], secrets: Sequence[str] = (),
                deadline: Any = None, *, runner: Callable[..., Dict[str, Any]],
                home: Optional[Path] = None,
                python_executable: Optional[str] = None,
                darwin_temp_resolver: Optional[Callable[[], Optional[str]]] = None
                ) -> Dict[str, Any]:
    """Run the Apple readiness probe *inside the profile itself* and report the evidence.

    ``runner`` is the launcher's bounded subprocess helper, injected so the probe can be
    driven by tests without nesting a real Seatbelt apply.
    """
    home_dir = Path(str(home)) if home is not None else Path(os.path.expanduser("~"))
    plan = _plan(worktree, primary, job_temp, simulator_udid, home_dir,
                 darwin_temp_resolver=darwin_temp_resolver)
    text = build_profile(plan["worktree"], plan["primary"], plan["scratch"], plan["udid"],
                         plan["home"], darwin_temp_resolver=darwin_temp_resolver)
    expected_ipc = testmanagerd_grant(plan["testmanagerd_socket"])
    if re.findall(r"^\(allow network-outbound.*$", text, re.M) != ([expected_ipc] if expected_ipc else []):
        raise ApplePolicyError("testmanagerd socket changed during profile preparation; retry discovery")
    profile = _write_profile(profile_path, text, worktree=plan["worktree"])
    prefix = [SANDBOX_EXEC, "-f", str(profile)]
    probe_env = verification_env(dict(env), plan["scratch"])
    negative_target = _negative_probe_target(plan, profile_sha256(text)[:12])
    positive_target = plan["scratch"] / "seatbelt-write-marker"
    python = str(python_executable or sys.executable or "/usr/bin/python3")
    checks: List[Dict[str, Any]] = []
    devices: Dict[str, Any] = {"family": DEVICE_FAMILY, "available_count": 0,
                               "selected_udid": plan["udid"], "selected_present": False,
                               "selected_name": None, "runtimes": []}

    def stdout_of(outcome: Dict[str, Any]) -> str:
        return _redact(_decode(outcome.get("stdout")), secrets)

    def stderr_of(outcome: Dict[str, Any]) -> str:
        return _redact(_decode(outcome.get("stderr")), secrets)

    def run(name: str, command: Sequence[str], evaluate: Callable[[str, Dict[str, Any]],
                                                                  Tuple[bool, str]],
            *, expect_refusal: bool = False) -> Tuple[bool, str]:
        argv = [*prefix, *command]
        left = deadline.cap(PROBE_COMMAND_TIMEOUT_SECONDS) if deadline is not None \
            else PROBE_COMMAND_TIMEOUT_SECONDS
        if left < MIN_PROBE_BUDGET_SECONDS:
            checks.append(_check(name, argv, False, None,
                                 f"the shared job budget has only {left:.1f}s left, so this "
                                 f"probe was not run"))
            return False, ""
        try:
            outcome = runner(argv, cwd=plan["worktree"], env=probe_env, timeout=left)
        except OSError as error:
            checks.append(_check(name, argv, False, None,
                                 f"cannot invoke {SANDBOX_EXEC}: {error}"))
            return False, ""
        # Diagnostic streams stay joined for refusals and generic failures, but JSON is parsed
        # from stdout alone (see ``evaluate_devices``).
        output = stdout_of(outcome) + "\n" + stderr_of(outcome)
        returncode = outcome.get("returncode")
        if outcome.get("timed_out"):
            ok, detail = False, (f"{command[0]} timed out after {left:.1f}s of the shared job "
                                 f"budget: {_tail(output)}")
        elif expect_refusal:
            ok, detail = evaluate(output, outcome)
        elif returncode != 0:
            ok, detail = False, (f"{command[0]} exited {returncode} inside the sandbox: "
                                 f"{_tail(output)}")
        else:
            ok, detail = evaluate(output, outcome)
        checks.append(_check(name, argv, ok, returncode, detail))
        return ok, output

    def evaluate_xcode(output: str, outcome: Dict[str, Any]) -> Tuple[bool, str]:
        match = XCODE_VERSION_RE.search(output)
        if match is None:
            return False, f"xcodebuild -version reported no version: {_tail(output)}"
        return True, f"xcodebuild reports {match.group(0)}"

    def evaluate_devices(output: str, outcome: Dict[str, Any]) -> Tuple[bool, str]:
        # ``simctl`` writes the JSON document on stdout and diagnostic warnings on stderr. Only
        # stdout is parsed; stderr is kept for the failure message, never appended to the JSON.
        stdout = stdout_of(outcome)
        try:
            payload = json.loads(stdout)
        except (ValueError, TypeError):
            detail = (f"simctl did not return JSON on stdout: "
                      f"{_tail(stdout) or '(empty stdout)'}")
            stderr = stderr_of(outcome)
            if stderr.strip():
                detail += f"; stderr: {_tail(stderr)}"
            return False, detail
        family, selected, runtimes = _family_devices(payload, plan["udid"])
        devices["available_count"] = len(family)
        devices["runtimes"] = runtimes
        devices["selected_present"] = selected is not None
        devices["selected_name"] = (selected or {}).get("name")
        listed = ", ".join(f"{entry.get('name')} ({entry.get('udid')})" for entry in family[:5])
        if not family:
            return False, (f"no available {DEVICE_FAMILY} simulator is listed, so native "
                           "verification has nothing to run against")
        if selected is None:
            return False, (f"selected simulator {plan['udid']} is not an available "
                           f"{DEVICE_FAMILY} device (available: {listed or 'none'})")
        return True, (f"selected {DEVICE_FAMILY} device {devices['selected_name']} "
                      f"({plan['udid']}) is available")

    def evaluate_positive_write(output: str, outcome: Dict[str, Any]) -> Tuple[bool, str]:
        return True, f"wrote the bounded scratch marker {positive_target}"

    def evaluate_write_refusal(output: str, outcome: Dict[str, Any]) -> Tuple[bool, str]:
        created = negative_target.exists()
        if created:
            with contextlib.suppress(OSError):
                negative_target.unlink()
            return False, (f"{negative_target} was created outside the bounded writable "
                           "roots, so the profile does not confine writes")
        if outcome.get("returncode") == 0:
            return False, (f"a write to {negative_target} succeeded inside the sandbox, so "
                           "the profile does not confine writes")
        if not DENIAL_RE.search(output) or str(negative_target) not in output:
            return False, (f"the outside-root write failed with exit "
                           f"{outcome.get('returncode')} without a permission refusal naming "
                           f"{negative_target}: {_tail(output)}")
        return True, f"write to {negative_target} was refused by the sandbox"

    def evaluate_connect_refusal(output: str, outcome: Dict[str, Any]) -> Tuple[bool, str]:
        if outcome.get("returncode") != 0:
            return False, (f"the loopback probe exited {outcome.get('returncode')} without a "
                           f"verdict: {_tail(output)}")
        verdict = output.upper()
        if "DENIED" in verdict:
            return True, "loopback TCP connect was refused by the sandbox"
        if "CONNECTED" in verdict:
            return False, (f"the wrapped command reached 127.0.0.1:{PROBE_TCP_PORT}, so it is "
                           "not network-confined")
        if "REFUSED" in verdict:
            return False, ("the loopback connect only hit a closed port, which is not proof "
                           "of a network deny")
        return False, f"the loopback probe reported no verdict: {_tail(output)}"

    run("xcodebuild-version", [XCODEBUILD, "-version"], evaluate_xcode)
    run("simctl-available-devices", [XCRUN, "simctl", "list", "devices", "available", "--json"],
        evaluate_devices)
    run("allow-bounded-scratch-write", [TOUCH, str(positive_target)],
        evaluate_positive_write)
    run("deny-write-outside-worktree", [TOUCH, str(negative_target)],
        evaluate_write_refusal, expect_refusal=True)
    run("deny-loopback-tcp", [python, "-c", CONNECT_PROBE, str(PROBE_TCP_PORT)],
        evaluate_connect_refusal, expect_refusal=True)

    failed = [entry["name"] for entry in checks if not entry["ok"]]
    available = bool(checks) and not failed
    if available:
        detail = "apple seatbelt probe passed: bounded writes, no TCP, selected device present"
    else:
        reasons = "; ".join(f"{entry['name']}: {entry['detail']}" for entry in checks
                            if not entry["ok"])
        detail = _redact(reasons or "the Apple probe produced no evidence", secrets)[:900]
    return {
        "available": available,
        "backend": BACKEND,
        "network": "disabled",
        "detail": detail,
        "failed_checks": failed,
        "checks": checks,
        "sandbox_exec": SANDBOX_EXEC,
        "profile_path": str(profile),
        "testmanagerd_socket": str(plan["testmanagerd_socket"]) if plan["testmanagerd_socket"] else None,
        "profile_sha256": profile_sha256(text),
        "profile_bytes": len(text.encode("utf-8")),
        "simulator_udid": plan["udid"],
        "device_dir": str(plan["device"]),
        "devices": devices,
        "writable_roots": [str(entry) for entry in plan["allowed"]],
        "darwin_temp_dir": str(plan["darwin_temp"]),
        "darwin_temp_policy": ("the per-user Darwin temporary root is asked of the OS "
                               "(confstr CS_DARWIN_USER_TEMP_DIR or getconf "
                               "DARWIN_USER_TEMP_DIR) rather than taken from a "
                               "caller-supplied path; that query does consult a TMPDIR naming an "
                               "accessible directory, so the grant comes from validating its "
                               "answer, not from ignoring the environment - a decoy TMPDIR, an "
                               "over-broad or malformed root, a symlink or a foreign-uid "
                               "directory is refused and blocks the job instead of being granted. "
                               "The accepted root is canonicalized, must keep the "
                               "/private/var/folders/<bucket>/<user>/T shape and must be owned "
                               "by the current uid, and it is a shared per-user directory - "
                               "writes there are not confined to this job's files"),
        "foundation_home": str(plan["scratch"] / FOUNDATION_HOME_RELATIVE),
        "negative_write_target": str(negative_target),
        "denied_write_roots": [str(entry) for entry in plan["denied"]],
        "mach_lookup_policy": "named services - including the exact hyphenated "
                              "com.apple.CoreSimulator.SimLaunchHost-arm64/-x86 runtime "
                              "library loaders - plus anchored com.apple.CoreSimulator/"
                              "com.apple.dt patterns plus the named platform helpers, the "
                              "cfprefsd agent also bound by local-name and preferences read "
                              "only (user-preference-read, never write) and the exact "
                              "com.apple.PowerManagement.control name for the user-idle sleep "
                              "assertion that XCTest housekeeping resolves through powerd "
                              "outside the profile, plus exact testmanagerd.control and "
                              "Instruments arbiter/security XPC names for runner bootstrap "
                              "and diagnostics; never a blanket allowance",
        "mach_services": list(MACH_SERVICES),
        "mach_local_names": list(MACH_LOCAL_NAMES),
        "preference_operations": list(PREFERENCE_READ_OPERATIONS),
        "mach_name_patterns": list(MACH_NAME_PATTERNS),
        "argv_prefix": list(prefix),
        "argv_template": sandbox_argv_template(),
        "scratch": str(plan["scratch"]),
        "env": {name: probe_env[name] for name in ("TMPDIR", "TMP", "TEMP",
                                                   "CLANG_MODULE_CACHE_PATH",
                                                   FOUNDATION_HOME_ENV, "IBToolNeverDeque")},
        "env_preserved": [name for name in ("HOME", "CODEX_HOME") if name in probe_env],
        "trust_limits": list(trust_limits()),
    }
