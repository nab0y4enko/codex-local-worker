#!/usr/bin/env python3
"""Create a clean, citation-checkable read-job fixture outside the source checkout."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

FILES = {
    "README.md": "# Read pilot\n\nThe local worker qualification fixture contains a widget module.\n",
    "widget.py": '"""Small fixture for a structured read job."""\n\nTITLE = "Qualification Widget"\n\ndef render():\n    return TITLE\n',
}


def run(argv, cwd):
    subprocess.run(argv, cwd=str(cwd), check=True, stdout=subprocess.PIPE,
                   stderr=subprocess.PIPE, text=True, timeout=30)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="new absolute fixture directory")
    args = parser.parse_args()
    root = Path(args.root).expanduser()
    if not root.is_absolute():
        raise SystemExit("--root must be absolute")
    root = root.resolve()
    if root.exists():
        raise SystemExit("refusing to overwrite existing path: %s" % root)
    repo = root / "repo"
    repo.mkdir(parents=True)
    for name, content in FILES.items():
        (repo / name).write_text(content, encoding="utf-8")
    run(["git", "init", "-q"], repo)
    run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], repo)
    run(["git", "-c", "user.name=Task26 Read Pilot", "-c",
         "user.email=task26-read@localhost", "add", "-A"], repo)
    run(["git", "-c", "user.name=Task26 Read Pilot", "-c",
         "user.email=task26-read@localhost", "commit", "-m", "read pilot baseline"], repo)
    task = {
        "task_id": "task26-read-pilot", "kind": "read", "repo_path": str(repo),
        "base_ref": "main", "goal": "Read widget.py as text and report the exact title returned by render().",
        "timeout_seconds": 300, "workstream": "task26-read",
        "question": "Use cat widget.py to inspect the source. Return one concise title field containing exactly the string returned by render(), with no explanation. Include exactly one citation to the line that defines TITLE, quoting that line verbatim.",
        "scope_paths": ["widget.py"],
        "answer_schema": {"type": "object", "required": ["title"],
                           "additionalProperties": False,
                           "properties": {"title": {"type": "string"}}},
        "max_answer_bytes": 4096,
    }
    (root / "task.json").write_text(json.dumps(task, indent=2, sort_keys=True) + "\n",
                                     encoding="utf-8")
    print(json.dumps({"root": str(root), "repo": str(repo), "task": str(root / "task.json")},
                     sort_keys=True))


if __name__ == "__main__":
    main()
