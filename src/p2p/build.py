"""BUILD call: page content + compute() in one compact request, then SPEC assembly.

Independence: the model sees the plan's state, outputs and invariants, but NOT the
plan's test cases. The code is therefore written without the answer key, and the
tests in Stage 6 are a genuine check rather than something the code was fitted to.

Everything that can be copied from the plan or the case (citation, symbols,
limitation, verbatim quotes, simplifications, control defaults) is filled in by Python,
so it costs no completion tokens and cannot drift from the plan.
"""
from __future__ import annotations

import copy
import json

from . import llm
from .prompts import BUILD_SYSTEM, BUILD_USER, CONCISE_RETRY

BUILD_MAX_TOKENS = 7000   # a cap; observed output is far smaller
RETRY_EXTRA_TOKENS = 1500

S = {"type": "string"}
A = {"type": "array", "items": {"type": "object"}}
BUILD_FIELDS = ["title", "subtitle", "idea_what", "idea_why", "equation_mathml", "controls",
                "outputs", "visuals", "explorations", "compute_js"]
# Loose schema (strict=False): the controls/visuals vocabulary is too rich for strict mode on
# every provider. Python validates and normalizes instead.
BUILD_SCHEMA = {
    "type": "object",
    "properties": {"title": S, "subtitle": S, "idea_what": S, "idea_why": S,
                   "equation_mathml": S, "controls": A, "outputs": A, "visuals": A,
                   "explorations": A, "compute_js": S},
    "required": BUILD_FIELDS,
}
PLAN_FIELDS_FOR_BUILD = ("concept", "why_it_matters", "source", "symbols", "state", "outputs",
                         "invariants", "explorations", "limitation", "simplifications")
KIND_TO_TYPES = {"bool": {"toggle"}, "choice": {"select"}, "vector": {"vector"},
                 "matrix": {"matrix"}, "number": {"slider", "number"},
                 "integer": {"slider", "number"}}


class BuildError(Exception):
    pass


def plan_for_build(plan: dict) -> str:
    """Minified plan WITHOUT tests (the answer key) and without bulky quotes."""
    slim = {k: plan[k] for k in PLAN_FIELDS_FOR_BUILD if k in plan}
    return json.dumps(slim, ensure_ascii=False, separators=(",", ":"))


def build(case, plan: dict, *, model: str, budget, trace, chat_fn=None) -> dict:
    chat_fn = chat_fn or llm.chat
    user = BUILD_USER.format(audience=case.audience, focus=case.focus, plan=plan_for_build(plan))
    max_tokens = BUILD_MAX_TOKENS
    last = None
    for attempt in (1, 2):
        try:
            res = chat_fn([{"role": "system", "content": BUILD_SYSTEM},
                           {"role": "user", "content": user}],
                          model=model, max_tokens=max_tokens, schema=BUILD_SCHEMA, strict=False,
                          purpose="build", budget=budget, trace=trace, stage="build")
        except (llm.Truncated, llm.BadJSON) as e:
            last = e
            retry = attempt == 1 and budget.can_call(min_tokens=max_tokens)
            trace.event("build", "retry_decision", "info", reason=type(e).__name__, will_retry=retry)
            if retry:
                user = user + CONCISE_RETRY
                max_tokens += RETRY_EXTRA_TOKENS
                continue
            break
        except (llm.APIError, llm.BudgetExhausted) as e:
            last = e
            break
        data = res.data
        problems = shape_problems(data)
        trace.check("build_shape", not problems, "; ".join(problems[:8]) or "all fields present",
                    stage="build")
        if not isinstance(data, dict) or not str(data.get("compute_js", "")).strip():
            last = BuildError("build has no compute_js")
            break
        trace.event("build", "summary", "info", n_controls=len(data.get("controls") or []),
                    n_visuals=len(data.get("visuals") or []),
                    visual_types=[v.get("type") for v in data.get("visuals") or [] if isinstance(v, dict)],
                    compute_chars=len(data.get("compute_js", "")))
        return data
    raise BuildError(f"no usable build: {type(last).__name__ if last else 'unknown'}: {last}")


def shape_problems(data) -> list[str]:
    if not isinstance(data, dict):
        return ["build is not a JSON object"]
    out = []
    for f in BUILD_FIELDS:
        if f not in data:
            out.append(f"missing {f}")
    for f in ("controls", "outputs", "visuals", "explorations"):
        if f in data and not isinstance(data[f], list):
            out.append(f"{f} must be a list")
    if isinstance(data.get("explorations"), list) and len(data["explorations"]) != 2:
        out.append(f"explorations: expected 2, got {len(data['explorations'])}")
    if "compute_js" in data and "function compute" not in str(data["compute_js"]):
        out.append("compute_js does not define function compute")
    return out


# --- SPEC composition ---------------------------------------------------------
def _auto_control(s: dict, default) -> dict:
    kind = s.get("kind")
    c = {"id": s["id"], "label": s.get("label") or s["id"]}
    if kind == "bool":
        c["type"] = "toggle"
    elif kind == "choice":
        c["type"] = "select"
        c["options"] = s.get("choices") or ([default] if default is not None else [])
    elif kind in ("vector", "matrix"):
        c["type"] = kind
    else:
        c["type"] = "slider" if s.get("min") is not None and s.get("max") is not None else "number"
    return c


def normalize_controls(build_controls, plan: dict, notes: list[str]) -> list[dict]:
    """One control per plan state id, defaults taken from the plan (the tests use them)."""
    by_id = {}
    for c in build_controls or []:
        if isinstance(c, dict) and c.get("id"):
            by_id.setdefault(str(c["id"]), c)
    defaults = plan.get("defaults", {})
    out = []
    for s in plan.get("state") or []:
        sid = s.get("id")
        default = defaults.get(sid)
        c = copy.deepcopy(by_id.get(sid)) if sid in by_id else None
        if c is None:
            c = _auto_control(s, default)
            notes.append(f"control {sid}: generated from plan")
        allowed = KIND_TO_TYPES.get(s.get("kind"), set())
        if allowed and c.get("type") not in allowed:
            notes.append(f"control {sid}: type {c.get('type')} -> {sorted(allowed)[0]} (plan kind {s.get('kind')})")
            c["type"] = _auto_control(s, default)["type"]
        if default is not None:
            c["default"] = default
        for k in ("min", "max", "step"):
            if c.get(k) is None and s.get(k) is not None:
                c[k] = s[k]
        if s.get("kind") == "integer" and c.get("step") is None:
            c["step"] = 1
        if c.get("type") == "select" and not c.get("options"):
            c["options"] = s.get("choices") or [default]
        c.setdefault("label", s.get("label") or sid)
        out.append(c)
    ids = {c["id"] for c in out}
    for c in out:
        for k in ("length_from", "rows_from", "cols_from"):
            if k in c and c[k] not in ids:
                notes.append(f"control {c['id']}: dropped {k}={c[k]!r} (no such control)")
                del c[k]
    extra = [i for i in by_id if i not in ids]
    if extra:
        notes.append(f"dropped controls not in plan: {extra}")
    return out


def compose_spec(case, plan: dict, b: dict) -> tuple[dict, str, list[str]]:
    """Return (spec, compute_js, notes)."""
    notes: list[str] = []
    src = plan.get("source") or {}
    explorations = [e for e in (b.get("explorations") or []) if isinstance(e, dict)][:2]
    if len(explorations) < 2:
        notes.append("explorations taken from plan")
        explorations = [dict(e) for e in (plan.get("explorations") or [])][:2]
    controls = normalize_controls(b.get("controls"), plan, notes)
    ids = {c["id"] for c in controls}
    for e in explorations:
        if isinstance(e.get("preset"), dict):
            e["preset"] = {k: v for k, v in e["preset"].items() if k in ids}
    quotes = list(plan.get("grounding_quotes") or []) if case.excerpt else []
    spec = {
        "title": b.get("title") or plan.get("concept") or "Interactive explainer",
        "subtitle": b.get("subtitle") or "",
        "meta": {
            "paper_title": src.get("paper_title") or case.get("title") or "",
            "section_label": src.get("section_label") or case.get("section") or "",
            "equation_label": src.get("equation_label") or "",
            "source_url": case.source_url,
            "audience": case.audience,
        },
        "sections": {
            "idea": {"what": b.get("idea_what") or plan.get("concept", ""),
                     "why": b.get("idea_why") or plan.get("why_it_matters", "")},
            "equation": b.get("equation_mathml") or "",
            "symbols": plan.get("symbols") or [],
            "explorations": explorations,
            "limitation": plan.get("limitation") or {},
        },
        "controls": controls,
        "outputs": [o for o in (b.get("outputs") or []) if isinstance(o, dict)][:4],
        "visuals": [v for v in (b.get("visuals") or []) if isinstance(v, dict)][:3],
        "invariants": [i for i in (plan.get("invariants") or []) if isinstance(i, dict)],
        "grounding": {
            "from_excerpt": quotes,
            "our_simplifications": list(plan.get("simplifications") or []),
        },
    }
    if not case.excerpt:
        spec["grounding"]["no_excerpt_note"] = ("No excerpt was supplied with this brief; the "
                                                "explanation is based on the learning brief only.")
    return spec, str(b.get("compute_js") or ""), notes
