#!/usr/bin/env python3
"""Install tools/scan_secrets.py as the git pre-commit hook. Run once per clone."""
import os
import stat
import subprocess
import sys

HOOK = """#!/bin/sh
# Installed by tools/install_hooks.py — blocks commits containing OpenRouter keys.
ROOT="$(git rev-parse --show-toplevel)"
if command -v python3 >/dev/null 2>&1; then PY=python3; else PY=python; fi
exec "$PY" "$ROOT/tools/scan_secrets.py"
"""


def main() -> int:
    try:
        hooks_dir = subprocess.run(
            ["git", "rev-parse", "--git-path", "hooks"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        print("install_hooks: not inside a git repository", file=sys.stderr)
        return 1
    os.makedirs(hooks_dir, exist_ok=True)
    path = os.path.join(hooks_dir, "pre-commit")
    with open(path, "w", newline="\n") as f:
        f.write(HOOK)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print(f"install_hooks: pre-commit hook installed at {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
