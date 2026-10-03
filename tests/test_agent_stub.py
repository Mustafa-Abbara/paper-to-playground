import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_stub_exits_2():
    r = subprocess.run(
        [sys.executable, str(ROOT / "agent.py"), "--input", "x", "--output", "y", "--model", "z"],
        capture_output=True, text=True,
    )
    assert r.returncode == 2
    assert "not implemented" in r.stdout
