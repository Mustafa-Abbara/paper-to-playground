import copy
import json
import os
import subprocess
import sys
import textwrap

import pytest

from p2p import llm
from p2p.checks import CheckResult
from p2p.repair import RepairError, build_request, repair

from .test_build import BUILD_OUT, COMPUTE, plan as good_plan
from .test_plan import GOOD, FakeChat, FakeTrace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS = [CheckResult("compute_loads", "fail", "critical", "syntax error: unexpected token", "compute_js"),
         CheckResult("visual_references", "fail", "major", "missing ['outputs.zzz']", "spec.visuals"),
         CheckResult("test:equal", "fail", "major", "mean: expected 4 got 3", "compute_js|plan.tests")]


def test_request_contains_only_failures_and_targeted_fields():
    p = good_plan()
    user, fields = build_request(FAILS, p, copy.deepcopy(BUILD_OUT))
    assert fields == ["visuals", "outputs"]
    cur = json.loads(user.split("CURRENT: ")[1].split("\n")[0])
    assert set(cur) == {"compute_js", "visuals", "outputs"}       # not controls, not idea text
    assert '"name":"equal"' in user and '"name":"zeros"' not in user  # only the failing test
    assert "EXCERPT" not in user


def test_repair_applies_code_patch_and_reasoned_test_fix():
    p = good_plan()
    reply = {"reason": "fixed", "compute_js": COMPUTE.replace("sum / n", "(sum / n)"),
             "spec_patch": {"visuals": [{"type": "bar", "values": "outputs.sorted"}], "bogus": 1},
             "test_fixes": [{"name": "equal", "expect_json": "{\"mean\": 4.0}", "rationale": "(4+4+4)/3 = 4"},
                            {"name": "zeros", "expect_json": "{\"mean\": 9}", "rationale": ""}]}
    new_p, new_b, info = repair(FAILS, p, BUILD_OUT, round_no=1, model="m", budget=_budget(),
                                trace=FakeTrace(), chat_fn=FakeChat([reply]))
    assert "compute_js" in info["changed"] and "spec.visuals" in info["changed"]
    assert new_b["visuals"] == [{"type": "bar", "values": "outputs.sorted"}] and "bogus" not in new_b
    assert [c["test"] for c in info["test_changes"]] == ["equal"]   # no rationale -> ignored
    assert {t["name"]: t["expect"] for t in new_p["tests"]}["zeros"] == {"mean": 0}


def test_repair_with_no_change_is_an_error():
    with pytest.raises(RepairError):
        repair(FAILS, good_plan(), BUILD_OUT, round_no=1, model="m", budget=_budget(), trace=FakeTrace(),
               chat_fn=FakeChat([{"reason": "nothing", "compute_js": "", "spec_patch": {}, "test_fixes": []}]))


def _budget():
    from p2p.budget import Budget
    return Budget()


# --- full agent loop, in-process, with a scripted fake model -----------------------
def run_agent(tmp_path, monkeypatch, replies, extra_args=()):
    import agent  # noqa: WPS433 (repo root is on sys.path via pytest rootdir)
    chat = FakeChat(replies)
    monkeypatch.setattr(llm, "chat", chat)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-not-real")
    case = tmp_path / "case.json"
    case.write_text(json.dumps({"source_url": "https://example.org/p", "focus": "explain the mean",
                                "audience": "students",
                                "excerpt": "the mean is the sum divided by the count"}))
    out = tmp_path / "out"
    code = agent.main(["--input", str(case), "--output", str(out), "--model", "m", *extra_args])
    recs = [json.loads(l) for l in (out / "trace.jsonl").read_text("utf-8").splitlines()]
    return code, recs, out, chat


@pytest.fixture(autouse=True)
def _root_on_path(monkeypatch):
    monkeypatch.syspath_prepend(ROOT)


def test_agent_repairs_broken_code(tmp_path, monkeypatch):
    broken = dict(BUILD_OUT, compute_js=COMPUTE + "\n}}}")
    fixed = {"reason": "removed stray braces", "compute_js": COMPUTE, "spec_patch": {}, "test_fixes": []}
    code, recs, out, chat = run_agent(tmp_path, monkeypatch, [GOOD, broken, fixed])
    assert code == 0 and len(chat.calls) == 3
    assert [c["purpose"] for c in chat.calls] == ["plan", "build", "repair1"]
    rev = [r for r in recs if r["action"] == "revision" and r.get("kind") == "model"][0]
    assert rev["before"]["critical"] >= 1 and rev["after"]["critical"] == 0
    assert "compute_loads" in rev["failures_sent"]
    verdict = [r for r in recs if r["action"] == "verdict"][0]
    assert verdict["final_version"] == 1 and verdict["critical"] == 0
    assert "function compute" in (out / "index.html").read_text("utf-8")
    assert recs[-1]["stage"] == "summary" and recs[-1]["calls"] == 3


def test_healthy_build_makes_no_repair_call(tmp_path, monkeypatch):
    code, recs, out, chat = run_agent(tmp_path, monkeypatch, [GOOD, BUILD_OUT])
    assert code == 0 and len(chat.calls) == 2
    assert any(r["action"] == "not_needed" for r in recs)


def test_failed_repairs_keep_the_best_version(tmp_path, monkeypatch):
    broken = dict(BUILD_OUT, compute_js=COMPUTE + "\n}}}")
    worse = {"reason": "try", "compute_js": "function compute(s) { throw new Error('x'); }",
             "spec_patch": {}, "test_fixes": []}
    code, recs, out, chat = run_agent(tmp_path, monkeypatch, [GOOD, broken, worse, worse])
    assert len(chat.calls) == 4                                   # never more than 2 repair rounds
    assert code == 1                                              # still critical
    assert (out / "index.html").exists()                          # but a page is always written
    assert recs[-1]["exit_code"] == 1


def test_inject_fault_flag_is_repaired(tmp_path, monkeypatch):
    fixed = {"reason": "restored", "compute_js": COMPUTE, "spec_patch": {}, "test_fixes": []}
    code, recs, _, chat = run_agent(tmp_path, monkeypatch, [GOOD, BUILD_OUT, fixed],
                                    ["--inject-fault", "throw"])
    assert code == 0 and any(r["action"] == "inject_fault" for r in recs)


def test_watchdog_writes_page_and_exits(tmp_path):
    """A hanging model call must not exceed the time limit: the watchdog writes the best
    page, the summary line, and exits."""
    case = tmp_path / "case.json"
    case.write_text(json.dumps({"source_url": "u", "focus": "f", "audience": "a"}))
    script = textwrap.dedent(f"""
        import sys, time, json
        sys.path.insert(0, {ROOT!r}); sys.path.insert(0, {os.path.join(ROOT, 'src')!r})
        import agent
        from p2p import llm
        agent.WATCHDOG_S = 2.0
        llm.chat = lambda *a, **k: time.sleep(30)
        import os; os.environ["OPENROUTER_API_KEY"] = "x"
        sys.exit(agent.main(["--input", {str(case)!r}, "--output", {str(tmp_path / 'o')!r}, "--model", "m"]))
    """)
    r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=20)
    recs = [json.loads(l) for l in (tmp_path / "o" / "trace.jsonl").read_text().splitlines()]
    assert r.returncode == 1                                      # no page could be produced in time
    assert any(x["action"] == "watchdog" for x in recs)
    assert recs[-1]["stage"] == "summary" and recs[-1]["note"] == "stopped by watchdog"


def test_repair_can_correct_a_wrong_invariant_only_with_rationale():
    p = good_plan()
    reply = {"reason": "invariant ignores epsilon", "compute_js": "", "spec_patch": {}, "test_fixes": [],
             "invariant_fixes": [{"name": "finite", "js": "isFinite(out.mean) || true", "rationale": "eps changes variance"},
                                 {"name": "finite", "js": "true", "rationale": ""}]}
    new_p, _, info = repair(FAILS, p, BUILD_OUT, round_no=1, model="m", budget=_budget(),
                            trace=FakeTrace(), chat_fn=FakeChat([reply]))
    assert info["changed"] == ["plan.invariants"]
    assert new_p["invariants"][0]["js"] == "isFinite(out.mean) || true"
    assert info["invariant_changes"][0]["before"] == "isFinite(out.mean)"
