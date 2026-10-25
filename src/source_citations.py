#!/usr/bin/env python3
"""Emit bounded, line-numbered source excerpts and ready-to-copy citations."""

import argparse
import json
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Sequence, Tuple


DEFAULT_COUNT = 40
MAX_COUNT = 80
MAX_SCOPES = 32
MAX_PATHS = 16
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024


def _relative(value: str, label: str) -> PurePosixPath:
    if (not isinstance(value, str) or not value or "\x00" in value
            or value.startswith(("/", "~", "\\"))
            or "\\" in value or re.match(r"^[A-Za-z]:", value)):
        raise ValueError("%s must be a safe worktree-relative path" % label)
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() in ("", "."):
        raise ValueError("%s must be a safe worktree-relative path" % label)
    return path


def _resolve_inside(root: Path, relative: PurePosixPath, label: str) -> Path:
    try:
        target = (root / Path(*relative.parts)).resolve(strict=True)
        target.relative_to(root)
    except ValueError:
        raise ValueError("%s resolves outside the repository" % label)
    except (OSError, RuntimeError):
        raise ValueError("%s does not exist or cannot be resolved" % label)
    return target


def _within(path: Path, scope: Path) -> bool:
    try:
        path.relative_to(scope)
        return True
    except ValueError:
        return False


def _scopes(root: Path, values: Sequence[str]) -> List[Path]:
    resolved = []
    for value in values:
        relative = _relative(value, "scope")
        target = _resolve_inside(root, relative, "scope")
        if not target.is_file() and not target.is_dir():
            raise ValueError("scope must be a file or directory")
        resolved.append(target)
    return resolved


def _file_result(root: Path, scopes: Sequence[Path], value: str,
                 start: int, count: int) -> Dict[str, Any]:
    relative = _relative(value, "path")
    target = _resolve_inside(root, relative, "path")
    if not target.is_file():
        raise ValueError("path must name a regular file")
    if not any(target == scope if scope.is_file() else _within(target, scope)
               for scope in scopes):
        raise ValueError("path is outside the declared scope")
    try:
        with target.open("rb") as source:
            data = source.read(MAX_FILE_BYTES + 1)
    except OSError:
        raise ValueError("path cannot be read")
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("file exceeds the source read limit")
    try:
        lines = data.decode("utf-8").splitlines()
    except UnicodeError:
        raise ValueError("path is not UTF-8 text")
    if not lines or start > len(lines):
        raise ValueError("start line is outside the file")
    end = min(start + count - 1, len(lines))
    selected = lines[start - 1:end]
    display = [{"number": start + offset, "text": line}
               for offset, line in enumerate(selected)]
    # The reported path is the caller's canonical relative spelling; the verifier resolves
    # symlinks independently and still enforces repository containment.
    canonical = relative.as_posix()
    citations = [
        {"path": canonical, "line_start": item["number"], "line_end": item["number"],
         "quote": item["text"]}
        for item in display if item["text"].strip()
    ]
    return {"path": canonical, "line_start": start, "line_end": end,
            "lines": display, "citations": citations}


def build_payload(repo: str, scopes: Sequence[str], paths: Sequence[str],
                  start: int = 1, count: int = DEFAULT_COUNT) -> Dict[str, Any]:
    """Read requested ranges only when every path resolves inside the declared scope."""
    if isinstance(start, bool) or not isinstance(start, int) or start < 1:
        raise ValueError("start must be a positive line number")
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_COUNT:
        raise ValueError("count must be between 1 and %d" % MAX_COUNT)
    if not scopes:
        raise ValueError("at least one scope is required")
    if len(scopes) > MAX_SCOPES:
        raise ValueError("at most %d scopes may be read at once" % MAX_SCOPES)
    if not paths:
        raise ValueError("at least one path is required")
    if len(paths) > MAX_PATHS:
        raise ValueError("at most %d paths may be read at once" % MAX_PATHS)
    try:
        root = Path(repo).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise ValueError("repository root does not exist or cannot be resolved")
    if not root.is_dir():
        raise ValueError("repository root must be a directory")
    resolved_scopes = _scopes(root, scopes)
    payload = {"files": []}
    for path in paths:
        payload["files"].append(_file_result(root, resolved_scopes, path, start, count))
        encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
        if len(encoded) > MAX_OUTPUT_BYTES:
            raise ValueError("requested source exceeds the output limit; reduce --count or choose a smaller range")
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="the read job worktree root")
    parser.add_argument("--scope", action="append", required=True,
                        help="contract scope path (repeat for each scope)")
    parser.add_argument("--path", action="append", required=True,
                        help="file to read within the declared scope (repeatable)")
    parser.add_argument("--start", type=int, default=1, help="first source line, starting at 1")
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT,
                        help="maximum number of lines per file (1-%d)" % MAX_COUNT)
    return parser


def main(argv: Sequence[str] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        payload = build_payload(args.repo, args.scope, args.path, args.start, args.count)
    except ValueError as error:
        print("source-citations: %s" % error, file=sys.stderr)
        return 2
    encoded = json.dumps(payload, ensure_ascii=False, indent=2)
    sys.stdout.write(encoded + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
