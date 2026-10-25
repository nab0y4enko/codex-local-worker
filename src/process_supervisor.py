"""Keep a verifiable process-group leader alive while a launcher-owned command has descendants.

The supervisor stays as the session/process-group leader until the command exits and all of its
children have left the group. A launcher killed between polls can therefore validate the leader's
birth identity before signalling the entire owned group.
"""
from __future__ import annotations

import contextlib
import os
import signal
import ctypes
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Sequence

_PS = "/bin/ps" if os.path.exists("/bin/ps") else shutil.which("ps") or "/usr/bin/ps"


class _ProcBsdInfo(ctypes.Structure):
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32), ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32), ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32), ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32), ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32), ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32), ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16), ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32), ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32), ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32), ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64), ("pbi_start_tvusec", ctypes.c_uint64),
    ]


def process_start_identity(pid: int) -> Optional[str]:
    """Return an OS-derived process birth token precise enough to reject PID reuse."""
    if sys.platform == "darwin":
        try:
            lib = ctypes.CDLL("/usr/lib/libproc.dylib")
            call = lib.proc_pidinfo
            call.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                             ctypes.c_void_p, ctypes.c_int]
            call.restype = ctypes.c_int
            info = _ProcBsdInfo()
            size = call(int(pid), 3, 0, ctypes.byref(info), ctypes.sizeof(info))
            if size != ctypes.sizeof(info) or info.pbi_pid != int(pid):
                return None
            return "%d:%d" % (info.pbi_start_tvsec, info.pbi_start_tvusec)
        except (OSError, AttributeError, ValueError):
            return None
    try:
        with open("/proc/%d/stat" % int(pid), "r", encoding="ascii") as handle:
            raw = handle.read()
        close = raw.rfind(")")
        fields = raw[close + 2:].split()
        return fields[19] if close >= 0 and len(fields) > 19 else None
    except (OSError, ValueError, IndexError):
        return None


def process_group_members(pgid: int) -> Optional[List[Dict[str, Any]]]:
    """Enumerate PID/PGID/SID/state tuples using the host process table."""
    try:
        result = subprocess.run([_PS, "-axo", "pid=,pgid=,stat="], capture_output=True,
                                text=True, timeout=2, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    members = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        try:
            pid, group = map(int, fields[:2])
        except ValueError:
            continue
        if group == int(pgid):
            try:
                session = os.getsid(pid)
            except (ProcessLookupError, PermissionError):
                continue
            members.append({"pid": pid, "pgid": group, "sid": session,
                            "state": fields[2]})
    return members


def popen(argv: Sequence[str], **kwargs: Any) -> subprocess.Popen:
    """Start a kept group leader around ``argv`` while preserving its inherited stdio/env/cwd."""
    ready_read, ready_write = os.pipe()
    command = [sys.executable, os.path.abspath(__file__), "--ready-fd", str(ready_read),
               "--", *[str(item) for item in argv]]
    kwargs.pop("start_new_session", None)
    kwargs["start_new_session"] = True
    inherited = set(kwargs.pop("pass_fds", ()))
    inherited.add(ready_read)
    kwargs["pass_fds"] = tuple(inherited)
    try:
        process = subprocess.Popen(command, **kwargs)
    except BaseException:
        os.close(ready_read)
        os.close(ready_write)
        raise
    os.close(ready_read)
    process._local_worker_ready_fd = ready_write
    identity = process_start_identity(process.pid)
    try:
        owner_sid = os.getsid(process.pid)
    except (ProcessLookupError, PermissionError):
        owner_sid = None
    if not identity or owner_sid != process.pid:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        with contextlib.suppress(Exception):
            process.wait(timeout=2)
        with contextlib.suppress(OSError):
            os.close(ready_write)
        process._local_worker_ready_fd = None
        raise OSError("cannot prove the supervised process birth and session identity")
    process._local_worker_start_identity = identity
    process._local_worker_owner_sid = owner_sid
    process._local_worker_supervisor_protocol = 1
    return process


def release(process: subprocess.Popen) -> None:
    """Allow the supervised command to start only after its owner record is durable."""
    fd = getattr(process, "_local_worker_ready_fd", None)
    if fd is None:
        return
    try:
        os.write(fd, b"1")
    finally:
        os.close(fd)
        process._local_worker_ready_fd = None


def _group_has_other_members(pgid: int, owner: int) -> bool:
    try:
        result = subprocess.run([_PS, "-axo", "pid=,pgid="], capture_output=True,
                                text=True, timeout=1, check=False, start_new_session=True)
    except (OSError, subprocess.SubprocessError):
        # Failure to prove the group empty keeps the owner alive, so the parent can still reap it.
        return True
    if result.returncode != 0:
        return True
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2:
            continue
        try:
            pid, group = int(fields[0]), int(fields[1])
        except ValueError:
            continue
        if group == pgid and pid != owner:
            return True
    return False


def _wait_for_release(is_released) -> None:
    """Poll a signal-handler flag; bounded sleep avoids both lost wakeups and handler locks."""
    while not is_released():
        time.sleep(.025)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    ready_fd = None
    if len(args) >= 2 and args[0] == "--ready-fd":
        ready_fd = int(args[1])
        args = args[2:]
    if args and args[0] == "--":
        args = args[1:]
    if ready_fd is not None:
        try:
            ready = os.read(ready_fd, 1)
        finally:
            os.close(ready_fd)
        if not ready:
            return 125
    if not args:
        return 127
    termination_requested = False
    release_requested = False

    def request_termination(_signum, _frame):
        nonlocal termination_requested
        termination_requested = True

    def release_owner(_signum, _frame):
        nonlocal release_requested
        release_requested = True

    # Install only in the supervisor before exec. Caught handlers reset to defaults across
    # exec, so the user command keeps its ordinary TERM behavior while the owner cannot vanish
    # between group membership proof and escalation.
    signal.signal(signal.SIGTERM, request_termination)
    signal.signal(signal.SIGUSR1, release_owner)
    if termination_requested:
        status = -signal.SIGTERM
    else:
        command = subprocess.Popen(args)
        status = command.wait()
    owner = os.getpid()
    while _group_has_other_members(owner, owner):
        time.sleep(.025)
    # On a termination path, retain the session/process-group leader until the parent has
    # verified that descendants are gone or has safely escalated against this exact owner.
    # This prevents a numeric PGID from being reused between the TERM poll and SIGKILL.
    if termination_requested:
        _wait_for_release(lambda: release_requested)
    if status < 0:
        # Restore the conventional negative signal exit status after descendants have left.
        # The supervisor ignored TERM while holding ownership proof, so it can now terminate
        # itself with the command's signal without releasing a live group early.
        terminating_signal = -status
        signal.signal(terminating_signal, signal.SIG_DFL)
        os.kill(owner, terminating_signal)
        return 128 + terminating_signal
    return status


if __name__ == "__main__":
    raise SystemExit(main())
