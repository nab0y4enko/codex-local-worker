"""Planner-owned acceptance checks whose inputs are pinned across worker execution."""
from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


class AcceptanceCheckError(ValueError):
    """A trusted check definition is invalid or changed after it was pinned."""


@dataclass(frozen=True)
class FilePin:
    path: Path
    sha256: str
    device: int
    inode: int
    content: bytes


@dataclass(frozen=True)
class SnapshotResult:
    ok: bool
    changed_paths: List[str]


@dataclass(frozen=True)
class Manifest:
    path: Path
    sha256: str
    checks: List[Dict[str, Any]]
    pins: List[FilePin]

    @property
    def protected_paths(self) -> List[Path]:
        return [pin.path for pin in self.pins]


def _read_pin(path: Path, label: str) -> FilePin:
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise AcceptanceCheckError("%s must be a regular non-symlink file: %s" % (label, path))
        resolved = path.resolve(strict=True)
        data = path.read_bytes()
        after = path.lstat()
    except OSError as error:
        raise AcceptanceCheckError("cannot read %s %s: %s" % (label, path, error)) from None
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise AcceptanceCheckError("%s changed while it was being pinned: %s" % (label, path))
    return FilePin(path, hashlib.sha256(data).hexdigest(), before.st_dev, before.st_ino, data)


def _under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def load_manifest(path: Path, *, repo: Path,
                  writable_roots: Sequence[Path] = ()) -> Manifest:
    """Load a v1 manifest and pin it plus every explicitly declared script.

    Paths must be outside the worker checkout and any caller-supplied worker-writable roots.
    The launcher additionally denies these paths in its existing Seatbelt wrapper.
    """
    supplied_manifest = Path(os.path.abspath(str(path)))
    if supplied_manifest.is_symlink():
        raise AcceptanceCheckError("acceptance manifest must not be a symlink: %s" % supplied_manifest)
    manifest_path = Path(os.path.realpath(str(supplied_manifest)))
    repo_root = Path(os.path.realpath(str(repo)))
    roots = [Path(os.path.realpath(str(root))) for root in writable_roots]
    if not manifest_path.is_absolute():
        raise AcceptanceCheckError("manifest path must be absolute")
    pin = _read_pin(manifest_path, "acceptance manifest")
    if _under(pin.path, repo_root) or any(_under(pin.path, root) for root in roots):
        raise AcceptanceCheckError("acceptance manifest is inside a worker-writable location")
    try:
        payload = json.loads(pin.content.decode("utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise AcceptanceCheckError("invalid acceptance manifest: %s" % error) from None
    if not isinstance(payload, dict) or set(payload) != {"version", "checks"}:
        raise AcceptanceCheckError("manifest must contain only version and checks")
    if payload["version"] != 1 or isinstance(payload["version"], bool):
        raise AcceptanceCheckError("manifest version must be 1")
    checks = payload["checks"]
    if not isinstance(checks, list) or not checks:
        raise AcceptanceCheckError("manifest checks must be a non-empty array")
    normalized: List[Dict[str, Any]] = []
    pins = [pin]
    names = set()
    for index, check in enumerate(checks):
        where = "checks[%d]" % index
        if not isinstance(check, dict) or set(check) != {"name", "argv", "scripts"}:
            raise AcceptanceCheckError("%s must contain name, argv and scripts" % where)
        name, argv, scripts = check["name"], check["argv"], check["scripts"]
        if not isinstance(name, str) or not name.strip() or name in names:
            raise AcceptanceCheckError("%s.name must be non-empty and unique" % where)
        if not isinstance(argv, list) or not argv or any(
                not isinstance(item, str) or not item or "\x00" in item for item in argv):
            raise AcceptanceCheckError("%s.argv must be a non-empty array of strings" % where)
        if not isinstance(scripts, list) or any(not isinstance(item, str) or not item for item in scripts):
            raise AcceptanceCheckError("%s.scripts must be an array of absolute paths" % where)
        names.add(name)
        script_paths = []
        normalized_argv = list(argv)
        for raw in scripts:
            supplied_script = Path(raw)
            if not supplied_script.is_absolute():
                raise AcceptanceCheckError("%s script paths must be absolute" % where)
            supplied_script = Path(os.path.abspath(str(supplied_script)))
            if supplied_script.is_symlink():
                raise AcceptanceCheckError("%s script must not be a symlink: %s" % (where, supplied_script))
            script = Path(os.path.realpath(str(supplied_script)))
            script_pin = _read_pin(script, "%s script" % where)
            if _under(script_pin.path, repo_root) or any(_under(script_pin.path, root) for root in roots):
                raise AcceptanceCheckError("declared check script is inside a worker-writable location: %s" % script)
            script_paths.append(str(script))
            try:
                argv_index = normalized_argv.index(raw)
            except ValueError:
                raise AcceptanceCheckError("%s declared script must appear as an argv item" % where) from None
            normalized_argv[argv_index] = str(script)
            if all(existing.path != script_pin.path for existing in pins):
                pins.append(script_pin)
        normalized.append({"name": name, "argv": normalized_argv, "scripts": script_paths})
    return Manifest(manifest_path, pin.sha256, normalized, pins)


def verify_snapshot(manifest: Manifest) -> SnapshotResult:
    changed = []
    for pin in manifest.pins:
        try:
            current = _read_pin(pin.path, "pinned acceptance input")
        except AcceptanceCheckError:
            changed.append(str(pin.path))
            continue
        if (current.sha256, current.device, current.inode) != (pin.sha256, pin.device, pin.inode):
            changed.append(str(pin.path))
    return SnapshotResult(not changed, changed)


def matches_reported_pins(manifest: Manifest, reported: Any) -> bool:
    """Whether a freshly loaded manifest has the same files as the original job report."""
    if not isinstance(reported, list):
        return False
    expected = [{"path": str(pin.path), "sha256": pin.sha256,
                 "device": pin.device, "inode": pin.inode} for pin in manifest.pins]
    return reported == expected
