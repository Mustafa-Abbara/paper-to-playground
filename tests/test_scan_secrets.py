import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from scan_secrets import find_secrets  # noqa: E402

FAKE = "sk-or-v1-" + "abcdef0123456789"


def test_flags_key_token():
    assert find_secrets(f"key = '{FAKE}'")


def test_flags_env_assignment():
    assert find_secrets("OPENROUTER_API_KEY=" + "hunter2value")
    assert find_secrets('OPENROUTER_API_KEY: "' + "realvalue" + '"')


def test_allows_placeholders():
    for line in [
        "OPENROUTER_API_KEY=",
        "export OPENROUTER_API_KEY=...",
        "OPENROUTER_API_KEY=<your key>",
        'OPENROUTER_API_KEY="$OPENROUTER_API_KEY"',
        "OPENROUTER_API_KEY=${OPENROUTER_API_KEY}",
        "headers Authorization: Bearer $OPENROUTER_API_KEY",
    ]:
        assert not find_secrets(line), line


def test_reports_line_numbers_not_secret():
    hits = find_secrets("ok\n" + FAKE + "\n")
    assert hits == [(2, "OpenRouter key token (sk-or-...)")]
