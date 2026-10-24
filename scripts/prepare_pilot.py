#!/usr/bin/env python3
"""Prepare a throwaway pilot repository for a fresh run.

This script performs no inference and talks to no model. It only lays down a
tiny git repository, commits it, then deliberately dirties a file that is not
in the task's allowed paths, so that a later run can be checked against the
"forbidden edits / dirty worktree" gate.

Layout produced under ROOT:
    ROOT/
      repo/            git repository the agent will be pointed at
        add.py         returns a - b  (the bug the agent must fix)
        test_add.py    unittest asserting add(2, 3) == 5
        .gitignore     ignores __pycache__/
        sentinel.txt   committed clean, dirtied after the commit
      task.json        task spec consumed by the runner
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ADD_PY = '''"""Tiny module with an intentionally wrong implementation."""


def add(a, b):
    return a - b
'''

TEST_ADD_PY = '''import unittest

from add import add


class TestAdd(unittest.TestCase):
    def test_add_two_and_three_is_five(self):
        self.assertEqual(add(2, 3), 5)


if __name__ == "__main__":
    unittest.main()
'''

GITIGNORE = "__pycache__/\n*.pyc\n"

SENTINEL = "clean\n"

DIRTY_SENTINEL = "clean\ndirty after commit\n"

USER_NAME = "Pilot Fixture"
USER_EMAIL = "pilot-fixture@localhost"


def run(argv, cwd, *, check=True):
    proc = subprocess.run(
        argv,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if check and proc.returncode != 0:
        sys.stderr.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        raise SystemExit("command failed: {0}".format(" ".join(argv)))
    return proc.stdout.strip()


def init_repo(repo):
    """Initialize on branch 'main' whatever the installed git defaults to."""
    if not (repo / ".git").is_dir():
        # Installed git is too old for --initial-branch: init, then point HEAD at main.
        run(["git", "init", "-q"], cwd=repo)
        run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], cwd=repo)
        return
    if run(["git", "symbolic-ref", "-q", "HEAD"], cwd=repo, check=False) != "refs/heads/main":
        run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], cwd=repo)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root",
        required=True,
        help="Absolute directory to create. Must not exist yet.",
    )
    args = parser.parse_args()

    root_text = args.root
    if not os.path.isabs(root_text):
        raise SystemExit("--root must be an absolute path, got: {0}".format(root_text))
    root = Path(root_text)
    if root.exists():
        raise SystemExit("refusing to overwrite existing path: {0}".format(root))

    repo = root / "repo"
    repo.mkdir(parents=True)

    (repo / "add.py").write_text(ADD_PY, encoding="utf-8")
    (repo / "test_add.py").write_text(TEST_ADD_PY, encoding="utf-8")
    (repo / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    (repo / "sentinel.txt").write_text(SENTINEL, encoding="utf-8")

    init_repo(repo)
    run(["git", "config", "user.name", USER_NAME], cwd=repo)
    run(["git", "config", "user.email", USER_EMAIL], cwd=repo)
    run(["git", "add", "-A"], cwd=repo)
    run(["git", "commit", "-m", "pilot fixture baseline"], cwd=repo)

    # Dirty a file that is NOT in allowed_paths, after the commit is on disk.
    (repo / "sentinel.txt").write_text(DIRTY_SENTINEL, encoding="utf-8")

    task = {
        "task_id": "pilot-add-fix",
        "repo_path": str(repo),
        "base_ref": "main",
        "goal": "Make the test suite pass by fixing add() in add.py so that add(2, 3) returns 5. "
                "Edit add.py only; every other path is out of scope.",
        "acceptance_criteria": ["python3 -m unittest -v exits 0", "add(2, 3) returns 5"],
        "allowed_paths": ["add.py"],
        "test_commands": [["python3", "-m", "unittest", "-v"]],
        "timeout_seconds": 180,
        "max_attempts": 1,
    }
    task_path = root / "task.json"
    task_path.write_text(json.dumps(task, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print("root:      {0}".format(root))
    print("repo:      {0}".format(repo))
    print("task:      {0}".format(task_path))
    print("sentinel:  {0}".format(repo / "sentinel.txt"))
    print("git head:  {0}".format(run(["git", "rev-parse", "--short", "HEAD"], cwd=repo)))
    print("status:")
    print(run(["git", "status", "--porcelain"], cwd=repo))


if __name__ == "__main__":
    main()
