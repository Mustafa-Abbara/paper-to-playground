"""PLAN call: one short, schema-constrained request that fixes the 'answer key'.

Before any page or code exists, the plan pins down the equation, the learner's inputs,
the outputs to compute, and test cases with expected values. Later stages run the
generated compute() against these tests, so the code is checked against an
expectation that was written independently of it.
"""
from __future__ import annotations

import json
import re

from . import llm
from .prompts import CONCISE_RETRY, PLAN_SYSTEM, PLAN_USER
from .schemas import PLAN_SCHEMA, validate

PLAN_MAX_TOKENS = 3200   # a cap, not a cost: we pay only for tokens produced
RETRY_EXTRA_TOKENS = 800
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class PlanError(Exception):
    pass


def _parse_json_field(text, where, problems):
    try:
        return json.loads(text) if isinstance(text, str) else text
    except (json.JSONDecodeError, TypeError):
        problems.append(f"{where}: not valid JSON: {str(text)[:60]!r}")
        return None


def _is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _numeric_tree(x):
    """Expected values: numbers, booleans, or (nested) arrays of them."""
    if _is_num(x) or isinstance(x, bool):
        return True
    return isinstance(x, list) and len(x) > 0 and all(_numeric_tree(v) for v in x)


def _kind_ok(kind, v, choices):
    if kind == "number":
        return _is_num(v)
    if kind == "integer":
        return _is_num(v) and float(v).is_integer()
    if kind == "bool":
        return isinstance(v, bool)
    if kind == "choice":
        return isinstance(v, str) and (not choices or v in choices)
    if kind == "vector":
        return isinstance(v, list) and len(v) > 0 and all(_is_num(x) for x in v)
    if kind == "matrix":
        return (isinstance(v, list) and len(v) > 0 and all(isinstance(r, list) for r in v)
                and len({len(r) for r in v}) == 1 and len(v[0]) > 0
                and all(_is_num(x) for r in v for x in r))
    return False


def normalize(plan: dict) -> tuple[dict, list[str]]:
    """Parse the *_json string fields and check cross-references.
    Returns (plan with added 'defaults', tests[i].inputs/expect), problems)."""
    problems: list[str] = []
    state = plan.get("state") or []
    ids = [s.get("id") for s in state]
    defaults = {}
    for s in state:
        sid = s.get("id", "?")
        if not IDENT.match(str(sid)):
            problems.append(f"state id {sid!r} is not a valid identifier")
        v = _parse_json_field(s.get("default_json"), f"state[{sid}].default_json", problems)
        if v is not None and not _kind_ok(s.get("kind"), v, s.get("choices")):
            problems.append(f"state[{sid}]: default {v!r} does not match kind {s.get('kind')}")
        defaults[sid] = v
    if len(set(ids)) != len(ids):
        problems.append("state ids are not unique")

    out_keys = [o.get("key") for o in plan.get("outputs") or []]
    for k in out_keys:
        if not IDENT.match(str(k)):
            problems.append(f"output key {k!r} is not a valid identifier")
    if len(set(out_keys)) != len(out_keys):
        problems.append("output keys are not unique")

    tests = plan.get("tests") or []
    if len(tests) < 4:
        problems.append(f"only {len(tests)} tests (need at least 4)")
    for i, t in enumerate(tests):
        name = t.get("name") or f"test {i + 1}"
        inputs = _parse_json_field(t.get("inputs_json", "{}") or "{}", f"tests[{name}].inputs_json", problems)
        expect = _parse_json_field(t.get("expect_json"), f"tests[{name}].expect_json", problems)
        if inputs is not None and not isinstance(inputs, dict):
            problems.append(f"tests[{name}].inputs_json must be an object")
            inputs = None
        if expect is not None and (not isinstance(expect, dict) or not expect):
            problems.append(f"tests[{name}].expect_json must be a non-empty object")
            expect = None
        for k in (inputs or {}):
            if k not in ids:
                problems.append(f"tests[{name}]: unknown input {k!r}")
        for k, v in (expect or {}).items():
            if k not in out_keys:
                problems.append(f"tests[{name}]: unknown output {k!r}")
            if not _numeric_tree(v):
                problems.append(f"tests[{name}]: expected {k} is not a number, boolean or array of them")
        t["inputs"] = inputs or {}
        t["expect"] = expect or {}
        if not _is_num(t.get("tol")) or t["tol"] <= 0:
            t["tol"] = 1e-6
    for inv in plan.get("invariants") or []:
        used = set(re.findall(r"\bout\.([A-Za-z_][A-Za-z0-9_]*)", str(inv.get("js", ""))))
        unknown = sorted(used - set(out_keys))
        if unknown:
            problems.append(f"invariant {inv.get('name', '?')!r} uses undeclared outputs {unknown}")
    plan["defaults"] = defaults
    return plan, problems


def plan(case, *, model: str, budget, trace, chat_fn=None) -> tuple[dict, list[str]]:
    """Run the PLAN call. Returns (plan, problems). Raises PlanError if no usable
    plan could be obtained within the budget. llm.MissingKey propagates."""
    chat_fn = chat_fn or llm.chat
    context = case.context_block()
    system, user = PLAN_SYSTEM, PLAN_USER.format(context=context)
    max_tokens = PLAN_MAX_TOKENS
    last: Exception | None = None

    for attempt in (1, 2):
        try:
            res = chat_fn([{"role": "system", "content": system},
                           {"role": "user", "content": user}],
                          model=model, max_tokens=max_tokens, schema=PLAN_SCHEMA,
                          purpose="plan", budget=budget, trace=trace, stage="plan")
        except (llm.Truncated, llm.BadJSON) as e:
            last = e
            trace.event("plan", "retry_decision", "info", reason=type(e).__name__,
                        will_retry=attempt == 1)
            if attempt == 1 and budget.can_call(min_tokens=max_tokens + RETRY_EXTRA_TOKENS // 2):
                user = PLAN_USER.format(context=context) + CONCISE_RETRY
                max_tokens += RETRY_EXTRA_TOKENS
                continue
            break
        except (llm.APIError, llm.BudgetExhausted) as e:
            last = e
            break

        data = res.data
        problems = validate(data, PLAN_SCHEMA)
        trace.check("plan_schema", not problems, "; ".join(problems[:8]) or "matches schema",
                    stage="plan")
        if not isinstance(data, dict):
            last = PlanError("plan is not a JSON object")
            break
        data, semantic = normalize(data)
        trace.check("plan_consistency", not semantic, "; ".join(semantic[:8]) or
                    "inputs, outputs and tests are consistent", stage="plan")
        trace.event("plan", "summary", "info", concept=str(data.get("concept", ""))[:120],
                    n_state=len(data.get("state") or []), n_outputs=len(data.get("outputs") or []),
                    n_tests=len(data.get("tests") or []),
                    n_invariants=len(data.get("invariants") or []),
                    n_quotes=len(data.get("grounding_quotes") or []),
                    section=data.get("source", {}).get("section_label", ""),
                    equation=data.get("source", {}).get("equation_label", ""))
        return data, problems + semantic

    raise PlanError(f"no usable plan: {type(last).__name__ if last else 'unknown'}: {last}")
