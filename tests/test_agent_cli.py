import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(*args, env=None):
    return subprocess.run([sys.executable, str(ROOT / "agent.py"), *args],
                          capture_output=True, text=True, env=env)


def read_trace(out):
    return [json.loads(l) for l in (out / "trace.jsonl").read_text("utf-8").splitlines()]


def test_dry_run_ok(tmp_path):
    case = tmp_path / "c.json"
    case.write_text(json.dumps({"source_url": "u", "focus": "f <b>", "audience": "a",
                                "excerpt": "e", "weird": 5}))
    out = tmp_path / "o"
    r = run("--input", str(case), "--output", str(out), "--model", "m", "--dry-run")
    assert r.returncode == 0, r.stderr
    page = (out / "index.html").read_text("utf-8")
    assert "f &lt;b&gt;" in page and "http" not in page
    recs = read_trace(out)
    assert recs[-1]["stage"] == "summary" and recs[-1]["exit_code"] == 0
    assert all({"stage", "action", "result"} <= r.keys() for r in recs)


def test_missing_focus_exits_2_with_trace(tmp_path):
    case = tmp_path / "c.json"
    case.write_text(json.dumps({"source_url": "u", "audience": "a"}))
    out = tmp_path / "o"
    r = run("--input", str(case), "--output", str(out), "--model", "m", "--dry-run")
    assert r.returncode == 2
    assert "missing required field: focus" in r.stderr
    recs = read_trace(out)
    assert any(x["action"] == "load_case" and x["result"] == "fail" for x in recs)
    assert recs[-1]["exit_code"] == 2


def test_bad_usage_exits_2():
    assert run("--input", "x").returncode == 2


def test_without_dry_run_exits_1_for_now(tmp_path):
    case = tmp_path / "c.json"
    case.write_text(json.dumps({"source_url": "u", "focus": "f", "audience": "a"}))
    r = run("--input", str(case), "--output", str(tmp_path / "o"), "--model", "m")
    assert r.returncode == 1
