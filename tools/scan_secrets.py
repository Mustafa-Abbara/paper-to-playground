#!/usr/bin/env python3
"""Block commits that contain an OpenRouter API key.

Default: scans the STAGED content of every staged file (what would actually be committed).
--all:   scans every tracked file in HEAD's working tree (use before final submission).
Exit 0 = clean, 1 = secret found (commit blocked).

Flagged:
  - any 'sk-or-...' token (OpenRouter key prefix)
  - OPENROUTER_API_KEY=<non-empty value>, unless the value is an obvious placeholder
    such as '...', '<your key>', '$VAR' / '${VAR}', or an empty string ''/"".
"""
import re
import subprocess
import sys

KEY_TOKEN = re.compile(r"sk-or-[A-Za-z0-9_\-]{4,}")
ASSIGN = re.compile(r"""OPENROUTER_API_KEY\s*[:=]\s*["']?([^\s"'#]*)""")
PLACEHOLDER = re.compile(r"^(|\.\.\.|<.*|\$.*|xxx+|your[-_].*|changeme)$", re.IGNORECASE)

# Files allowed to mention the patterns themselves.
SELF_EXEMPT = {"tools/scan_secrets.py", "tests/test_scan_secrets.py"}


def _git(*args: str) -> bytes:
    return subprocess.run(["git", *args], check=True, capture_output=True).stdout


def staged_files() -> list[str]:
    out = _git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z")
    return [p for p in out.decode("utf-8", "replace").split("\0") if p]


def tracked_files() -> list[str]:
    out = _git("ls-files", "-z")
    return [p for p in out.decode("utf-8", "replace").split("\0") if p]


def read_staged(path: str) -> bytes:
    return _git("show", f":{path}")


def read_worktree(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def find_secrets(text: str) -> list[tuple[int, str]]:
    """Return (line_number, reason) for each suspicious line. Never returns the secret."""
    hits = []
    for i, line in enumerate(text.splitlines(), 1):
        if KEY_TOKEN.search(line):
            hits.append((i, "OpenRouter key token (sk-or-...)"))
            continue
        m = ASSIGN.search(line)
        if m and not PLACEHOLDER.match(m.group(1)):
            hits.append((i, "OPENROUTER_API_KEY assigned a non-empty value"))
    return hits


def scan(paths, reader) -> int:
    found = 0
    for path in paths:
        if path.replace("\\", "/") in SELF_EXEMPT:
            continue
        try:
            data = reader(path)
        except (OSError, subprocess.CalledProcessError):
            continue
        if b"\0" in data[:8192]:  # binary file
            continue
        for line_no, reason in find_secrets(data.decode("utf-8", "replace")):
            print(f"[scan_secrets] {path}:{line_no}: {reason}", file=sys.stderr)
            found += 1
    return found


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--all" in argv:
        found = scan(tracked_files(), read_worktree)
    else:
        found = scan(staged_files(), read_staged)
    if found:
        print(
            f"[scan_secrets] BLOCKED: {found} possible secret(s). Remove them, keep the key "
            "in your shell environment only. If a real key was ever committed, revoke it on "
            "OpenRouter.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
