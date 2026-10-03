import json

from p2p.trace import REDACTED, Trace, fingerprint, redact

FAKE = "sk-or-v1-" + "0123456789abcdef"


def lines(tr):
    with open(tr.path, encoding="utf-8") as f:
        return [json.loads(l) for l in f]


def test_redact_keys_and_values(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "custom-secret-value-123")
    obj = {
        "Authorization": "Bearer x", "api_key": "k", "headers": {"a": 1},
        "messages": [{"role": "user", "content": "hi"}], "reasoning": "thoughts",
        "note": f"key {FAKE}", "other": "has custom-secret-value-123 inside",
        "nested": [{"ok": "fine", "token": FAKE}],
    }
    r = redact(obj)
    for k in ("Authorization", "api_key", "headers", "messages", "reasoning"):
        assert k not in r
    assert r["note"] == REDACTED and r["other"] == REDACTED
    assert r["nested"] == [{"ok": "fine", "token": REDACTED}]


def test_every_line_valid_and_has_required_keys(tmp_path):
    tr = Trace(str(tmp_path))
    tr.event("setup", "start")
    tr.llm_call(ok=True, purpose="plan", model="m", prompt_tokens=10, completion_tokens=5,
                messages=["secret body"], api_key=FAKE)
    tr.check("rows_sum_to_one", True, "ok")
    tr.check("engine", None, "quickjs missing")
    tr.check("offline", False, "found http://x")
    tr.revision(1, ["compute_js"], "test failed")
    tr.event("x", "y", "weird-result")
    tr.summary({"calls": 1})
    tr.close()
    recs = lines(tr)
    for rec in recs:
        assert {"ts", "t", "stage", "action", "result"} <= rec.keys()
        assert rec["result"] in ("ok", "fail", "skip", "info")
    llm = recs[1]
    assert llm["action"] == "call:plan" and llm["prompt_tokens"] == 10
    assert "messages" not in llm and "api_key" not in llm
    assert [r["result"] for r in recs[2:5]] == ["ok", "skip", "fail"]
    s = recs[-1]
    assert s["stage"] == "summary" and s["checks_passed"] == 1 and s["checks_failed"] == 1
    assert s["checks_skipped"] == 1 and s["revisions"] == 1 and s["calls"] == 1
    assert "sk-or-" not in open(tr.path).read()


def test_trace_overwrites_previous_run(tmp_path):
    Trace(str(tmp_path)).event("a", "b")
    tr = Trace(str(tmp_path))
    tr.event("c", "d")
    tr.close()
    assert [r["stage"] for r in lines(tr)] == ["c"]


def test_fingerprint_does_not_contain_text():
    fp = fingerprint("very private prompt")
    assert fp["prompt_chars"] == 19 and len(fp["prompt_sha256"]) == 12
    assert "private" not in json.dumps(fp)
