"""Each check must catch the problem it exists for. Uses the neutral fixture only."""
import copy
import json
import os

import pytest

from p2p import checks as C
from p2p.assemble import assemble
from p2p.case import Case

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
EXCERPT = "A line maps each input x to an output y. The slope a scales the input and the offset b shifts it."


def fixture():
    spec = json.load(open(os.path.join(FIX, "generic_spec.json"), encoding="utf-8"))
    js = open(os.path.join(FIX, "generic_compute.js"), encoding="utf-8").read()
    spec["grounding"]["from_excerpt"] = ["The slope a scales the input and the offset b shifts it."]
    return spec, js


PLAN = {
    "outputs": [{"key": "mean_y"}, {"key": "ys"}, {"key": "max_y"}],
    "invariants": [{"name": "max >= mean", "js": "out.max_y >= out.mean_y - 1e-9"}],
    "tests": [
        {"name": "defaults", "inputs": {}, "expect": {"ys": [-1, 0.5, 2, 3.5]}, "tol": 1e-6},
        {"name": "flat", "inputs": {"a": 0, "b": 2}, "expect": {"mean_y": 2}, "tol": 1e-6},
        {"name": "zero x", "inputs": {"x": [0, 0, 0, 0]}, "expect": {"max_y": 0.5}, "tol": 1e-6},
    ],
}
CASE = Case(source_url="about:blank", focus="f", audience="a", extra={"excerpt": EXCERPT})


def run(spec, js, plan=PLAN, case=CASE, html=None):
    res, facts = C.run_checks(case, plan, spec, js, html if html is not None else assemble(spec, js))
    return {r.name: r for r in res}, C.summarize(res), facts


def test_good_fixture_has_no_critical_or_major():
    by, s, facts = run(*fixture())
    bad = {n: r.detail for n, r in by.items() if r.failed and r.severity != "minor"}
    assert not bad, bad
    assert facts["engine"] == "quickjs-ng"
    assert by["test:flat"].status == "pass" and by["controls_change_result"].status == "pass"


def test_network_use_is_critical():
    spec, js = fixture()
    html = assemble(spec, js).replace("</head>", '<script src="https://cdn.example/x.js"></script></head>')
    by, s, _ = run(spec, js, html=html)
    assert by["offline_no_network"].failed and s["critical"] >= 1
    by, _, _ = run(spec, js.replace("var a = state.a", "fetch('x'); var a = state.a"))
    assert by["offline_no_network"].failed


def test_syntax_error_and_throwing_compute_are_critical():
    spec, js = fixture()
    by, s, _ = run(spec, "function compute(state) { return {outputs: }; }")
    assert by["compute_loads"].failed and by["compute_loads"].severity == "critical"
    by, s, _ = run(spec, "function compute(state) { throw new Error('boom'); }")
    assert by["compute_defaults"].failed and "boom" in by["compute_defaults"].detail


def test_infinite_loop_times_out_instead_of_hanging():
    spec, _ = fixture()
    by, s, _ = run(spec, "function compute(state) { while (true) {} }")
    assert by["compute_defaults"].failed and s["critical"] == 1


def test_dead_controls_detected():
    spec, _ = fixture()
    js = "function compute(s) { return {outputs: {mean_y: 1, ys: [1], max_y: 1}, intermediates: [], checks: []}; }"
    by, _, _ = run(spec, js)
    assert by["controls_change_result"].failed and "0/8" in by["controls_change_result"].detail


def test_nan_on_edge_input_detected():
    spec, js = fixture()
    js = js.replace("var a = state.a, b = state.b;", "var a = state.a, b = state.b; b = b / (a - 3);")
    by, _, _ = run(spec, js)   # a = 3 is the slider max -> division by zero
    assert by["edge_inputs_finite"].failed and "a=max" in by["edge_inputs_finite"].detail


def test_wrong_exact_test_is_major_but_rounded_one_is_suspect():
    spec, js = fixture()
    plan = copy.deepcopy(PLAN)
    plan["tests"][2]["expect"] = {"max_y": 0.75}                     # exact, wrong, plausible
    by, _, _ = run(spec, js, plan)
    assert by["test:zero x"].failed and by["test:zero x"].severity == "major"
    plan["tests"][2]["expect"] = {"max_y": 0.5}
    plan["tests"][1]["expect"] = {"mean_y": 2.5}                     # mean > max: contradicts invariant
    by, _, _ = run(spec, js, plan)
    assert by["test:flat"].severity == "minor" and "break an invariant" in by["test:flat"].detail
    plan["tests"][1].update(expect={"mean_y": 2.0123}, tol=1e-3)     # rounded, others pass
    by, _, _ = run(spec, js, plan)
    assert by["test:flat"].severity == "minor" and "suspect" in by["test:flat"].detail


def test_broken_invariant_and_missing_output_are_major():
    spec, js = fixture()
    plan = copy.deepcopy(PLAN)
    plan["invariants"].append({"name": "always false", "js": "out.mean_y > 1e9"})
    plan["outputs"].append({"key": "nonexistent"})
    by, _, _ = run(spec, js, plan)
    assert by["invariants_hold"].failed and by["plan_outputs_present"].failed


def test_invalid_invariant_is_removed_without_a_model():
    spec, js = fixture()
    plan = copy.deepcopy(PLAN)
    plan["invariants"].append({"name": "uses missing", "js": "out.raw.length > 0"})
    spec["invariants"] = copy.deepcopy(plan["invariants"])
    by, _, facts = run(spec, js, plan)
    assert facts["invalid_invariants"] == ["uses missing"]
    fixed, revs = C.deterministic_fixes(CASE, spec, facts, plan)
    assert [i["name"] for i in fixed["invariants"]] == ["max >= mean"]
    assert "cannot be evaluated" in revs[0]["reason"]


def test_quotes_must_come_from_the_excerpt():
    spec, js = fixture()
    spec["grounding"]["from_excerpt"] = ["The slope a scales the input and the offset b shifts it.",
                                         "Lines were invented by the ancient Greeks in 300 BC."]
    by, _, facts = run(spec, js)
    assert by["grounding_quotes_verbatim"].failed and "1/2" in by["grounding_quotes_verbatim"].detail
    fixed, revs = C.deterministic_fixes(CASE, spec, facts, {"concept": "slope offset input"})
    assert fixed["grounding"]["from_excerpt"] == spec["grounding"]["from_excerpt"][:1]
    spec["grounding"]["from_excerpt"] = ["Completely invented sentence about nothing in the text."]
    fixed, revs = C.deterministic_fixes(CASE, spec, facts, {"concept": "slope offset input"})
    assert fixed["grounding"]["from_excerpt"] and all(C.quote_in_excerpt(q, EXCERPT)
                                                      for q in fixed["grounding"]["from_excerpt"])


def test_quote_matching_tolerates_garbled_math_only():
    ex = "we scale the dot products by\n1\n√\nd\nk\n. To counteract this effect"
    assert C.quote_in_excerpt("we scale the dot products by 1/sqrt(d_k). To counteract this effect", ex)
    assert not C.quote_in_excerpt("we scale the dot products by two before adding them", ex)


def test_visual_reference_to_missing_output():
    spec, js = fixture()
    spec["visuals"][0]["series"][0]["y"] = "outputs.not_there"
    by, _, _ = run(spec, js)
    assert by["visual_references"].failed and "outputs.not_there" in by["visual_references"].detail


def test_missing_sections_and_no_excerpt_skip():
    spec, js = fixture()
    spec["sections"]["explorations"] = spec["sections"]["explorations"][:1]
    spec["sections"]["limitation"] = {}
    by, _, _ = run(spec, js, case=Case(source_url="about:blank", focus="f", audience="a"))
    assert by["section_explorations"].failed and by["section_limitation"].failed
    assert by["grounding_quotes_verbatim"].status == "skip"     # never reported as pass
    spec["meta"] = {"source_url": "about:blank"}
    by, _, _ = run(spec, js, case=Case(source_url="about:blank", focus="f", audience="a"))
    assert by["grounding_citation"].status == "skip"
    by, _, _ = run(spec, js)                                    # excerpt given -> citation required
    assert by["grounding_citation"].failed


def test_no_engine_means_skip_not_pass(monkeypatch):
    monkeypatch.setattr(C, "load_engine", lambda: None)
    spec, js = fixture()
    by, s, facts = run(spec, js)
    assert facts["engine"] is None
    assert by["compute_loads"].status == "skip" and by["plan_tests"].status == "skip"
    assert s["skipped"] >= 5


def test_compute_reading_a_nonexistent_input_is_flagged():
    spec, js = fixture()
    by, _, _ = run(spec, js)
    assert by["compute_reads_real_inputs"].status == "pass"
    bad = js.replace("var n = ys.length;", "var n = state.count || ys.length; var k = state['T']; var { a, zz } = state;")
    by, _, _ = run(spec, bad)
    r = by["compute_reads_real_inputs"]
    assert r.failed and r.severity == "major" and "['T', 'count', 'zz']" in r.detail


def test_state_reads_uses_the_parameter_name():
    assert C.state_reads("function compute(s) { return s.a + s['b'] + other.c; }") == {"a", "b"}


def test_explicit_vector_keeps_its_length():
    ctl = [{"id": "n", "type": "slider", "default": 2}, {"id": "p", "type": "vector", "length_from": "n", "default": [0.5, 0.5]}]
    st = C.apply_bindings({"n": 2, "p": [0.1, 0.2, 0.3, 0.4]}, ctl, {"p"})
    assert st == {"n": 4, "p": [0.1, 0.2, 0.3, 0.4]}
    st = C.apply_bindings({"n": 3, "p": [0.5, 0.5]}, ctl)          # count changed by the learner
    assert st["p"] == [0.5, 0.5, 0]


def test_bad_size_link_does_not_crash_the_checker():
    ctl = [{"id": "g", "type": "vector", "default": [1, 2]}, {"id": "h", "type": "vector", "length_from": "g", "default": [0]}]
    assert C.apply_bindings({"g": [1, 2], "h": [0]}, ctl)["h"] == [0]     # list as a count: ignored


def test_run_checks_never_raises(monkeypatch):
    spec, js = fixture()
    def boom(*a, **k):
        raise TypeError("float() argument must be a string or a real number, not 'list'")
    monkeypatch.setattr(C, "numeric_checks", boom)
    res, facts = C.run_checks(CASE, PLAN, spec, js, assemble(spec, js))
    err = [r for r in res if r.name == "numeric_checks_error"][0]
    assert err.status == "skip" and "could not run" in err.detail
