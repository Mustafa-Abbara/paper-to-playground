import copy
import json

import pytest
import quickjs

from p2p import llm, prompts
from p2p.assemble import assemble, wrap_compute
from p2p.budget import Budget
from p2p.build import (BUILD_SCHEMA, BuildError, build, compose_spec, normalize_controls,
                       plan_for_build)
from p2p.case import Case
from p2p.plan import normalize

from .test_plan import GOOD, FakeChat, FakeTrace

COMPUTE = """function compute(state) {
  var x = state.x, n = x.length;
  var sum = x.reduce(function (a, b) { return a + b; }, 0);
  var mean = n ? sum / n : 0;
  return { outputs: { mean: mean, sorted: x.slice().sort(function (a, b) { return a - b; }) },
           intermediates: [{ label: "sum", value: sum }], checks: [] };
}"""

BUILD_OUT = {
    "title": "The mean", "subtitle": "Averages in one line.",
    "idea_what": "The <i>mean</i> adds values and divides by the count.",
    "idea_why": "It summarises data.",
    "equation_mathml": "<math display=\"block\"><mi>m</mi><mo>=</mo><mi>s</mi><mo>/</mo><mi>n</mi></math>",
    "controls": [
        {"id": "x", "type": "vector", "label": "Values", "length_from": "n", "default": [9, 9]},
        {"id": "n", "type": "toggle", "label": "Count"},          # wrong type for an integer
        {"id": "ghost", "type": "slider", "label": "not in plan"},
    ],
    "outputs": [{"key": "mean", "label": "Mean", "decimals": 2}],
    "visuals": [{"type": "bar", "title": "Values", "values": "state.x"}],
    "explorations": [
        {"title": "Equal", "change": "c", "observe": "o", "why": "w", "preset": {"x": [4, 4, 4], "bogus": 1}},
        {"title": "Zeros", "change": "c", "observe": "o", "why": "w", "preset": {"x": [0, 0, 0]}},
    ],
    "compute_js": COMPUTE,
}


def plan():
    p, problems = normalize(copy.deepcopy(GOOD))
    assert problems == []
    return p


def case(excerpt=True):
    extra = {"excerpt": "the mean is the sum divided by the count"} if excerpt else {}
    return Case(source_url="https://example.org/paper", focus="explain the mean",
                audience="students", extra=extra)


def test_build_prompt_hides_the_answer_key():
    p = plan()
    text = plan_for_build(p)
    assert "tests" not in json.loads(text) and "expect" not in text
    assert "grounding_quotes" not in json.loads(text)
    chat = FakeChat([BUILD_OUT])
    build(case(), p, model="m", budget=Budget(), trace=FakeTrace(), chat_fn=chat)
    sent = chat.calls[0]["messages"][1]["content"]
    for t in p["tests"]:
        assert t["expect_json"] not in sent
    assert chat.calls[0]["strict"] is False and chat.calls[0]["schema"] is BUILD_SCHEMA
    assert "STATE KEYS (compute may read only these): x, n, mode" in sent


def test_normalize_controls():
    notes = []
    ctl = normalize_controls(BUILD_OUT["controls"], plan(), notes)
    by = {c["id"]: c for c in ctl}
    assert list(by) == ["x", "n", "mode"]                      # plan order, one per state id
    assert by["x"]["default"] == [1, 2, 3]                     # plan default wins
    assert by["n"]["type"] == "slider" and by["n"]["step"] == 1  # fixed to match integer kind
    assert by["mode"]["type"] == "select" and by["mode"]["options"] == ["plain", "weighted"]
    text = " | ".join(notes)
    assert "generated from plan" in text and "ghost" in text and "plan kind integer" in text


def test_compose_spec_takes_grounding_from_plan():
    spec, js, notes = compose_spec(case(), plan(), BUILD_OUT)
    assert spec["meta"]["source_url"] == "https://example.org/paper"
    assert spec["meta"]["section_label"] == "S1"
    assert spec["sections"]["symbols"] == GOOD["symbols"]
    assert spec["sections"]["limitation"] == GOOD["limitation"]
    assert spec["grounding"]["from_excerpt"] == GOOD["grounding_quotes"]
    assert spec["invariants"] == GOOD["invariants"]
    assert spec["sections"]["explorations"][0]["preset"] == {"x": [4, 4, 4]}  # unknown id dropped
    assert js == COMPUTE


def test_no_excerpt_means_no_quotes():
    spec, _, _ = compose_spec(case(excerpt=False), plan(), BUILD_OUT)
    assert spec["grounding"]["from_excerpt"] == []
    assert "No excerpt" in spec["grounding"]["no_excerpt_note"]


def test_assembled_page_compute_runs():
    spec, js, _ = compose_spec(case(), plan(), BUILD_OUT)
    page = assemble(spec, js)
    assert "Math.abs" not in page or "isFinite(out.mean)" in page  # invariant shipped as data
    ctx = quickjs.Context()
    ctx.eval("var window = {};")
    ctx.eval(wrap_compute(js))
    state = {c["id"]: c["default"] for c in spec["controls"]}
    out = json.loads(ctx.eval(f"JSON.stringify(window.__P2P_COMPUTE({json.dumps(state)}))"))
    assert out["outputs"]["mean"] == 2


def test_truncated_build_retries_once_then_fails():
    chat = FakeChat([llm.Truncated("cut"), BUILD_OUT])
    build(case(), plan(), model="m", budget=Budget(), trace=FakeTrace(), chat_fn=chat)
    assert len(chat.calls) == 2 and chat.calls[1]["max_tokens"] > chat.calls[0]["max_tokens"]
    with pytest.raises(BuildError):
        build(case(), plan(), model="m", budget=Budget(), trace=FakeTrace(),
              chat_fn=FakeChat([llm.BadJSON("a"), llm.BadJSON("b")]))


def test_missing_compute_is_an_error():
    bad = dict(BUILD_OUT, compute_js="")
    with pytest.raises(BuildError):
        build(case(), plan(), model="m", budget=Budget(), trace=FakeTrace(), chat_fn=FakeChat([bad]))


def test_build_prompt_is_generic():
    text = (prompts.BUILD_SYSTEM + prompts.BUILD_USER).lower()
    for word in ("attention", "entropy", "shannon", "transformer", "softmax", "arxiv"):
        assert word not in text, word


def test_size_link_to_a_vector_is_dropped_and_input_refs_fixed():
    b = copy.deepcopy(BUILD_OUT)
    b["controls"][0]["length_from"] = "x"           # a vector cannot set a length
    b["visuals"] = [{"type": "bar", "values": "outputs.x", "labels": "outputs.sorted"}]
    spec, _, notes = compose_spec(case(), plan(), b)
    assert "length_from" not in spec["controls"][0]
    assert spec["visuals"][0]["values"] == "state.x" and spec["visuals"][0]["labels"] == "outputs.sorted"
    assert any("not a number control" in n for n in notes) and any("-> state.x" in n for n in notes)
