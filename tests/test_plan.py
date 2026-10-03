import copy
import json

import pytest

from p2p import llm, prompts
from p2p.budget import Budget
from p2p.case import Case
from p2p.plan import PLAN_MAX_TOKENS, PlanError, normalize, plan
from p2p.schemas import PLAN_SCHEMA, validate

GOOD = {
    "concept": "Mean of a list",
    "why_it_matters": "Summarises data.",
    "source": {"paper_title": "Fixture", "section_label": "S1", "equation_label": "Eq. 1",
               "equation_text": "m = (1/n) sum x_i"},
    "grounding_quotes": ["the mean is the sum divided by the count"],
    "symbols": [{"symbol": "m", "meaning": "mean", "shape": "scalar"}],
    "state": [
        {"id": "x", "kind": "vector", "label": "values", "default_json": "[1,2,3]",
         "min": -10, "max": 10, "step": 1, "choices": []},
        {"id": "n", "kind": "integer", "label": "count", "default_json": "3",
         "min": 1, "max": 6, "step": 1, "choices": []},
        {"id": "mode", "kind": "choice", "label": "mode", "default_json": "\"plain\"",
         "min": None, "max": None, "step": None, "choices": ["plain", "weighted"]},
    ],
    "outputs": [{"key": "mean", "meaning": "the mean"}, {"key": "sorted", "meaning": "sorted"}],
    "tests": [
        {"name": "default", "inputs_json": "{}", "expect_json": "{\"mean\": 2}", "tol": 1e-9, "why": "d"},
        {"name": "equal", "inputs_json": "{\"x\": [4,4,4]}", "expect_json": "{\"mean\": 4}", "tol": 1e-9, "why": "e"},
        {"name": "zeros", "inputs_json": "{\"x\": [0,0,0]}", "expect_json": "{\"mean\": 0}", "tol": 0, "why": "z"},
        {"name": "sorted", "inputs_json": "{\"x\": [3,1,2]}", "expect_json": "{\"sorted\": [1,2,3]}", "tol": 1e-9, "why": "s"},
    ],
    "invariants": [{"name": "finite", "js": "isFinite(out.mean)"}],
    "explorations": [{"change": "a", "observe": "b", "why": "c"}] * 2,
    "limitation": {"kind": "assumption", "text": "small lists"},
    "simplifications": ["toy sizes"],
}


class FakeChat:
    def __init__(self, results):
        self.results, self.calls = list(results), []

    def __call__(self, messages, **kw):
        self.calls.append({"messages": messages, **kw})
        kw["budget"].record({"prompt_tokens": 500, "completion_tokens": 800})
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return llm.LLMResult(text=json.dumps(r), data=copy.deepcopy(r), usage={})


class FakeTrace:
    def __init__(self):
        self.events = []

    def event(self, stage, action, result="info", **kw):
        self.events.append((stage, action, result, kw))

    def check(self, name, passed, detail="", stage="check", **kw):
        self.events.append((stage, f"check:{name}", "ok" if passed else "fail", {"detail": detail}))


def run(results, budget=None):
    case = Case(source_url="u", focus="explain the mean", audience="students",
                extra={"excerpt": "the mean is the sum divided by the count"})
    chat, tr = FakeChat(results), FakeTrace()
    p, problems = plan(case, model="m", budget=budget or Budget(), trace=tr, chat_fn=chat)
    return p, problems, chat, tr


def test_schema_accepts_good_plan():
    assert validate(GOOD, PLAN_SCHEMA) == []


def test_good_plan_normalized():
    p, problems, chat, tr = run([GOOD])
    assert problems == []
    assert p["defaults"] == {"x": [1, 2, 3], "n": 3, "mode": "plain"}
    assert p["tests"][1]["inputs"] == {"x": [4, 4, 4]} and p["tests"][1]["expect"] == {"mean": 4}
    assert p["tests"][2]["tol"] == 1e-6          # non-positive tol replaced
    call = chat.calls[0]
    assert call["schema"] is PLAN_SCHEMA and call["max_tokens"] == PLAN_MAX_TOKENS
    assert call["purpose"] == "plan" and "EXCERPT:" in call["messages"][1]["content"]
    checks = {a: r for _, a, r, _ in tr.events if a.startswith("check:")}
    assert checks == {"check:plan_schema": "ok", "check:plan_consistency": "ok"}


def test_inconsistencies_reported():
    bad = copy.deepcopy(GOOD)
    bad["state"][0]["default_json"] = "[1, \"a\"]"          # wrong kind
    bad["state"][2]["default_json"] = "\"other\""          # not in choices
    bad["tests"][0]["inputs_json"] = "{\"y\": 1}"           # unknown input
    bad["tests"][1]["expect_json"] = "{\"median\": 4}"      # unknown output
    bad["tests"][2]["expect_json"] = "not json"
    bad["tests"] = bad["tests"][:3]                          # too few tests
    bad["outputs"].append({"key": "mean", "meaning": "dup"})
    _, problems = normalize(bad)
    text = " | ".join(problems)
    for frag in ("does not match kind vector", "does not match kind choice", "unknown input 'y'",
                 "unknown output 'median'", "not valid JSON", "only 3 tests",
                 "output keys are not unique"):
        assert frag in text, frag


def test_schema_problem_reported_not_fatal():
    bad = copy.deepcopy(GOOD)
    del bad["limitation"]
    bad["extra"] = 1
    p, problems, _, tr = run([bad])
    assert any("limitation: missing" in x for x in problems)
    assert any("extra: unexpected" in x for x in problems)
    assert ("plan", "check:plan_schema", "fail") in [e[:3] for e in tr.events]


def test_truncated_then_retry_more_concise():
    p, problems, chat, tr = run([llm.Truncated("cut"), GOOD])
    assert problems == [] and len(chat.calls) == 2
    assert chat.calls[1]["max_tokens"] > chat.calls[0]["max_tokens"]
    assert prompts.CONCISE_RETRY in chat.calls[1]["messages"][1]["content"]


def test_two_failures_raise():
    with pytest.raises(PlanError):
        run([llm.BadJSON("x"), llm.BadJSON("y")])


def test_api_error_raises_without_retry():
    with pytest.raises(PlanError):
        run([llm.APIError("402", status=402), GOOD])


def test_missing_key_propagates():
    with pytest.raises(llm.MissingKey):
        run([llm.MissingKey("no key")])


def test_prompts_are_generic():
    text = (prompts.PLAN_SYSTEM + prompts.PLAN_USER).lower()
    for word in ("attention", "entropy", "shannon", "transformer", "softmax", "arxiv"):
        assert word not in text, word


def test_boolean_expectations_allowed():
    ok = copy.deepcopy(GOOD)
    ok["outputs"].append({"key": "is_flat", "meaning": "all equal"})
    ok["tests"][1]["expect_json"] = "{\"mean\": 4, \"is_flat\": true}"
    _, problems = normalize(ok)
    assert problems == []
    ok["tests"][1]["expect_json"] = "{\"is_flat\": \"yes\"}"
    _, problems = normalize(ok)
    assert any("not a number, boolean" in p for p in problems)


def test_schema_requires_an_invariant():
    bad = copy.deepcopy(GOOD)
    bad["invariants"] = []
    assert any("invariants" in e for e in validate(bad, PLAN_SCHEMA))


def test_invariant_with_undeclared_output_is_flagged():
    bad = copy.deepcopy(GOOD)
    bad["invariants"] = [{"name": "uses raw", "js": "out.raw_scores.length > 0 && out.mean > 0"}]
    _, problems = normalize(bad)
    assert any("undeclared outputs ['raw_scores']" in p for p in problems)
