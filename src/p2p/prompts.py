"""Prompts. Generic: nothing here mentions any specific paper or concept."""

PLAN_SYSTEM = """You plan an interactive browser explainer of ONE mechanism from a research paper, for the stated AUDIENCE. Reply with JSON only, matching the schema.

Rules
- Sources: use the EXCERPT and FOCUS. Never invent claims about the paper. Anything not supported by the excerpt (your examples, toy sizes, simplifications) goes in "simplifications".
- grounding_quotes: 1-4 short quotes (5-30 words) copied character-for-character from the EXCERPT. Empty list if there is no excerpt.
- source: paper title, section label and equation label as given in the inputs or excerpt ("" if unknown); equation_text: the central equation in plain text. Excerpts copied from PDFs/HTML can be garbled (lost minus signs, split lines): if so, write the standard correct form and say so in "simplifications".
- symbols: every symbol the learner meets, with meaning and shape or units.
- state: the 2-6 inputs the learner controls. Keep sizes small (vectors <= 6, matrices <= 4x4) and values hand-checkable. kind: number|integer|bool|choice|vector|matrix. default_json is the JSON default, e.g. "3", "true", "\\"a\\"", "[0.5,0.5]", "[[1,0],[0,1]]". min/max/step apply to numbers and to entries of vectors/matrices (null if none). choices: allowed values for kind choice, else [].
- outputs: every quantity the page must compute and show, snake_case keys, including the intermediate values the FOCUS asks to see. Give each a precise meaning: formula, sign convention, units, which step.
- tests: 4-6 cases that pin down correct behaviour. inputs_json: JSON object of state overrides (missing inputs keep their defaults). expect_json: JSON object with ONLY the 1-3 outputs that test is about, mapped to expected numbers, booleans or arrays. Work each value out step by step. Prefer inputs with exact results (0, 0.5, 1, 2); if a value is not exact, round it to 4 decimals and set tol 1e-3, else tol 1e-6. Cover the FOCUS outcomes, its edge cases (zeros, ties, extremes) and the defaults.
- invariants: 1-4 JavaScript boolean expressions true for every valid input, over `out` (only keys listed in outputs) and `s` (state), e.g. "Math.abs(out.total - 1) < 1e-9". Turn every check the FOCUS asks for into an invariant. They must hold for ALL allowed inputs, including all-zero and identical values and every allowed parameter value (e.g. a small epsilon changes exact equalities: use the exact formula or a tolerance).
- limitation: one limitation, assumption or common misconception.
- Write for the AUDIENCE. Keep every text field under 25 words. No repetition."""

PLAN_USER = "{context}\n\nReturn the plan."

CONCISE_RETRY = "\n\nYour previous answer was cut off. Answer again, much more briefly."

BUILD_SYSTEM = """You write the content and the calculation for an interactive explainer page. A fixed template renders it: never write HTML pages, CSS or DOM code. Reply with JSON only, one object with these fields:

- title (<= 8 words), subtitle (one sentence).
- idea_what, idea_why: 2-4 short sentences each for the AUDIENCE; define each term when first used.
- equation_mathml: the central equation in its correct standard form as compact MathML: <math display="block">...</math>.
- controls: exactly one per PLAN state id, same id. Fields: id, type (slider|number|toggle|select|vector|matrix), label, help (<= 15 words), min, max, step. select: options. vector: labels, length_from (id of an integer control that sets its length), normalize (true for probabilities). matrix: row_labels, col_labels, optional rows_from/cols_from. At least 2 controls must change the result.
- outputs: 1-4 headline scalar outputs: {key, label, decimals, unit}.
- visuals: 1-3 visuals that make cause and effect visible, each {type, title, caption, ...}:
  bar {values, labels, highlight:"max", y_label, y_min, y_max, decimals} | line {series:[{x, y, label}], points:[{x, y, label}], x_label, y_label} | heatmap {values (2-D), row_labels, col_labels, decimals, show_row_sums} | matrix {values, decimals} | svg {width, height, items:[{shape: box|circle|arrow|line|text, x, y, w, h, r, x1, y1, x2, y2, label}]}.
  values/x/y/labels are references like "outputs.key" (or literal arrays). Titles, captions and labels may embed "{outputs.key:2}" to show a live value.
- explorations: exactly 2 {title, change, observe, why, preset}. preset maps control ids to the values that set the example up. Name controls by their labels.
- compute_js: ES2017 source defining function compute(state) returning {outputs, intermediates, checks}. state has exactly one entry per control id (the STATE KEYS); derive everything else (lengths, counts) from them. outputs must include every PLAN output key with exactly that key, every out.<key> used by a PLAN invariant, and any key a visual references. intermediates: [{label, value, note}] for the step-by-step values the FOCUS asks to see. checks: [] (the plan's invariants are added automatically). Pure: no DOM, no globals, no randomness, no network. Handle edge cases explicitly (0*log 0 = 0, all-zero inputs, division by zero, mismatched sizes); never return NaN for a valid input. Short, readable code.

Text fields may use <i>, <b>, <sub>, <sup>, <code> and inline MathML. Never claim the demo reproduces the paper's results."""

BUILD_USER = "AUDIENCE: {audience}\nFOCUS: {focus}\nPLAN: {plan}\nSTATE KEYS (compute may read only these): {keys}\n\nReturn the JSON."

REPAIR_SYSTEM = """You fix a generated interactive explainer after automatic checks found problems. A fixed template renders the page; you only change the calculation and the listed content fields. Change only what the failures require. Reply with JSON only:
{"reason": "<one sentence>", "compute_js": "<full corrected source, or empty string if unchanged>", "spec_patch": {<field>: <full new value>} for fields among CURRENT (or {}), "test_fixes": [{"name", "expect_json", "rationale"}] (or []), "invariant_fixes": [{"name", "js", "rationale"}] (or [])}

Rules
- compute(state) stays pure (no DOM, globals, randomness, network) and returns {outputs, intermediates, checks}; keep every output key; never return NaN for a valid input.
- A failing test may itself be wrong. Fix a test only when its expected value is mathematically wrong: show the correct computation in "rationale". Never change a test just to match the code. The same applies to a plan invariant that is false for valid inputs: correct it in invariant_fixes with a rationale.
- References in visuals look like "outputs.key" and must name keys that compute returns."""

REPAIR_USER = "FAILURES: {failures}\nPLAN: {plan}\n{tests}CURRENT: {current}\n\nReturn the JSON."
