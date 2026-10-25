#!/usr/bin/env python3
"""Install the committed package into a disposable Codex home and check skill discovery."""
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXPECTED = ("codex-local-worker:local-worker", "codex-local-worker:setup",
            "codex-local-worker:doctor")


def run(argv, cwd, env):
    result = subprocess.run(argv, cwd=str(cwd), env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, timeout=180)
    if result.returncode:
        raise RuntimeError("command failed (%s):\n%s" % (" ".join(argv), result.stdout))
    return result.stdout


def main():
    codex = os.environ.get("LOCAL_WORKER_CODEX_BIN") or shutil.which("codex")
    if not codex:
        raise RuntimeError("Codex CLI not found; set LOCAL_WORKER_CODEX_BIN")
    with tempfile.TemporaryDirectory(prefix="local-worker-install-check-") as scratch:
        temp = Path(scratch)
        archive = temp / "package.zip"
        subprocess.run(["git", "archive", "--format=zip", "--output", str(archive), "HEAD"],
                       cwd=str(ROOT), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        package = temp / "marketplace"
        package.mkdir()
        with zipfile.ZipFile(str(archive)) as bundle:
            bundle.extractall(str(package))
        home = temp / "home"
        codex_home = temp / "codex-home"
        home.mkdir()
        codex_home.mkdir()
        env = dict(os.environ)
        env.update({"HOME": str(home), "CODEX_HOME": str(codex_home),
                    "XDG_CONFIG_HOME": str(temp / "xdg")})
        run([codex, "plugin", "marketplace", "add", str(package)], temp, env)
        run([codex, "plugin", "add", "codex-local-worker@codex-local-worker", "--json"],
            temp, env)
        prompts = run([codex, "debug", "prompt-input"], temp, env)
        missing = [skill for skill in EXPECTED if skill not in prompts]
        if missing:
            raise RuntimeError("installed plugin skills missing from prompt-input: "
                               + ", ".join(missing) + "\n" + prompts)
    print("ok")


if __name__ == "__main__":
    try:
        main()
    except (OSError, subprocess.SubprocessError, RuntimeError, zipfile.BadZipFile) as error:
        print("plugin install check failed: %s" % error, file=sys.stderr)
        raise SystemExit(1)
