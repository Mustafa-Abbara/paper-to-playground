"""Targeted repair: send ONLY the failing checks and the parts they point at.

The model may return corrected compute_js, a patch for the listed content fields, and
corrections to plan tests it can show are mathematically wrong (with a rationale that
is written to the trace). Nothing else is sent or changed.
"""
from __future__ import annotations

import copy
import json

from . import llm
from .prompts import REPAIR_SYSTEM, REPAIR_USER

REPAIR_MAX_TOKENS = 4000
MIN_TOKENS_FOR_REPAIR = 2000
MIN_SECONDS_FOR_REPAIR = 90.0
MAX_ROUNDS = 2

# spec_patch field -> where it lives ("build" output or "plan")
PATCHABLE = {"controls": "build", "outputs": "build", "visuals": "build", "explorations": "build",
             "idea_what": "build", "idea_why": "build", "equation_mathml": "build",
             "title": "build", "subtitle": "build",
             "symbols": "plan", "limitation": "plan", "simplifications": "plan"}
TARGET_FIELDS = {"spec.visuals": ["visuals", "outputs"], "spec.controls": ["controls"],
                 "spec.sections.explorations": ["explorations"],
                 "spec.sections.idea": ["idea_what", "idea_why"],
                 "spec.sections.equation": ["equation_mathml"],
                 "spec.sections.symbols": ["symbols"], "spec.sections.limitation": ["limitation"],
                 "spec.grounding": ["simplifications"]}

REPAIR_SCHEMA = {"type": "object",
                 "properties": {"reason": {"type": "string"}, "compute_js": {"type": "string"},
                                "spec_patch": {"type": "object"}, "test_fixes": {"type": "array"}},
                 "required": ["reason", "compute_js", "spec_patch", "test_fixes"]}


class RepairError(Exception):
    pass


def needs_repair(results) -> list:
    return [r for r in results if r.failed and r.severity in ("critical", "major")]


def can_repair(budget) -> bool:
    return (budget.can_call(min_tokens=MIN_TOKENS_FOR_REPAIR)
            and budget.time_left() >= MIN_SECONDS_FOR_REPAIR)


def build_request(failures, plan: dict, b: dict) -> tuple[str, list[str]]:
    fields: list[str] = []
    for f in failures:
        for t in (f.target or "").split("|"):
            for fld in TARGET_FIELDS.get(t, []):
                if fld not in fields:
                    fields.append(fld)
    current = {"compute_js": b.get("compute_js", "")}
    for fld in fields:
        current[fld] = (b if PATCHABLE[fld] == "build" else plan).get(fld)
    slim_plan = {k: plan.get(k) for k in ("state", "outputs", "invariants") if k in plan}
    failing_tests = {f.name[5:] for f in failures if f.name.startswith("test:")}
    tests = [{"name": t.get("name"), "inputs": t.get("inputs"), "expect": t.get("expect")}
             for t in plan.get("tests") or [] if t.get("name") in failing_tests]
    fail_list = [{"check": f.name, "severity": f.severity, "detail": f.detail[:400]} for f in failures]
    dump = lambda x: json.dumps(x, ensure_ascii=False, separators=(",", ":"))
    user = REPAIR_USER.format(failures=dump(fail_list), plan=dump(slim_plan),
                              tests=f"FAILING TESTS: {dump(tests)}\n" if tests else "",
                              current=dump(current))
    return user, fields


def repair(failures, plan: dict, b: dict, *, round_no: int, model: str, budget, trace,
           chat_fn=None) -> tuple[dict, dict, dict]:
    """Returns (new_plan, new_build, info). Raises RepairError if no usable patch."""
    chat_fn = chat_fn or llm.chat
    user, fields = build_request(failures, plan, b)
    try:
        res = chat_fn([{"role": "system", "content": REPAIR_SYSTEM}, {"role": "user", "content": user}],
                      model=model, max_tokens=REPAIR_MAX_TOKENS, schema=REPAIR_SCHEMA, strict=False,
                      purpose=f"repair{round_no}", budget=budget, trace=trace, stage="repair")
    except llm.LLMError as e:
        raise RepairError(f"{type(e).__name__}: {e}") from None
    data = res.data if isinstance(res.data, dict) else {}
    new_plan, new_b = copy.deepcopy(plan), copy.deepcopy(b)
    changed: list[str] = []
    js = data.get("compute_js")
    if isinstance(js, str) and js.strip() and "function compute" in js and js.strip() != b.get("compute_js", "").strip():
        new_b["compute_js"] = js
        changed.append("compute_js")
    for fld, val in (data.get("spec_patch") or {}).items():
        if fld in PATCHABLE and val not in (None, "", [], {}):
            (new_b if PATCHABLE[fld] == "build" else new_plan)[fld] = val
            changed.append(f"spec.{fld}")
    test_changes = []
    by_name = {t.get("name"): t for t in new_plan.get("tests") or []}
    for fix in data.get("test_fixes") or []:
        if not isinstance(fix, dict) or fix.get("name") not in by_name or not str(fix.get("rationale", "")).strip():
            continue
        try:
            exp = json.loads(fix["expect_json"]) if isinstance(fix.get("expect_json"), str) else fix.get("expect_json")
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(exp, dict) and exp:
            t = by_name[fix["name"]]
            test_changes.append({"test": fix["name"], "before": t.get("expect"), "after": exp,
                                 "rationale": str(fix["rationale"])[:300]})
            t["expect"] = exp
            t["expect_json"] = json.dumps(exp)
    if test_changes:
        changed.append("plan.tests")
    if not changed:
        raise RepairError("model returned no applicable change")
    return new_plan, new_b, {"reason": str(data.get("reason", ""))[:300], "changed": changed,
                             "test_changes": test_changes, "fields_sent": ["compute_js"] + fields}
