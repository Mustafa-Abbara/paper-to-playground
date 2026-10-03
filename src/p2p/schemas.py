"""JSON Schemas for the model's structured outputs, plus a minimal validator.

Schemas are 'strict'-compatible (every property required, additionalProperties false).
Values whose type varies (defaults, test inputs, expected outputs) are carried as JSON
*strings* (e.g. default_json="[0.5, 0.5]") so the schema stays simple enough for every
provider; Python parses and checks them.
"""
from __future__ import annotations

S = {"type": "string"}
NUM_OR_NULL = {"type": ["number", "null"]}


def obj(props: dict) -> dict:
    return {"type": "object", "properties": props, "required": list(props),
            "additionalProperties": False}


def arr(items: dict, min_items: int | None = None, max_items: int | None = None) -> dict:
    a = {"type": "array", "items": items}
    if min_items is not None:
        a["minItems"] = min_items
    if max_items is not None:
        a["maxItems"] = max_items
    return a


STATE_KINDS = ["number", "integer", "bool", "choice", "vector", "matrix"]
LIMIT_KINDS = ["limitation", "assumption", "misconception"]

PLAN_SCHEMA = obj({
    "concept": S,
    "why_it_matters": S,
    "source": obj({"paper_title": S, "section_label": S, "equation_label": S,
                   "equation_text": S}),
    "grounding_quotes": arr(S, 0, 4),
    "symbols": arr(obj({"symbol": S, "meaning": S, "shape": S}), 1, 10),
    "state": arr(obj({"id": S, "kind": {"type": "string", "enum": STATE_KINDS}, "label": S,
                      "default_json": S, "min": NUM_OR_NULL, "max": NUM_OR_NULL,
                      "step": NUM_OR_NULL, "choices": arr(S)}), 1, 8),
    "outputs": arr(obj({"key": S, "meaning": S}), 1, 12),
    "tests": arr(obj({"name": S, "inputs_json": S, "expect_json": S,
                      "tol": {"type": "number"}, "why": S}), 1, 10),
    "invariants": arr(obj({"name": S, "js": S}), 0, 6),
    "explorations": arr(obj({"change": S, "observe": S, "why": S}), 2, 2),
    "limitation": obj({"kind": {"type": "string", "enum": LIMIT_KINDS}, "text": S}),
    "simplifications": arr(S, 0, 6),
})


# --- minimal validator (subset of JSON Schema used above) -------------------
_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}


def _type_ok(value, t) -> bool:
    if isinstance(t, list):
        return any(_type_ok(value, x) for x in t)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, _TYPES[t])


def validate(value, schema: dict, path: str = "$") -> list[str]:
    """Return a list of human-readable problems (empty = valid)."""
    errs: list[str] = []
    t = schema.get("type")
    if t is not None and not _type_ok(value, t):
        return [f"{path}: expected {t}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{path}: {value!r} not in {schema['enum']}")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for k in schema.get("required", []):
            if k not in value:
                errs.append(f"{path}.{k}: missing")
        for k, v in value.items():
            if k in props:
                errs += validate(v, props[k], f"{path}.{k}")
            elif schema.get("additionalProperties") is False:
                errs.append(f"{path}.{k}: unexpected field")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errs.append(f"{path}: needs at least {schema['minItems']} items, has {len(value)}")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errs.append(f"{path}: allows at most {schema['maxItems']} items, has {len(value)}")
        if "items" in schema:
            for i, v in enumerate(value):
                errs += validate(v, schema["items"], f"{path}[{i}]")
    return errs
