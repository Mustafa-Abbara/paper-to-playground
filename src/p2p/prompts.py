"""Prompts. Generic: nothing here mentions any specific paper or concept."""

PLAN_SYSTEM = """You plan an interactive browser explainer of ONE mechanism from a research paper, for the stated AUDIENCE. Reply with JSON only, matching the schema.

Rules
- Sources: use the EXCERPT and FOCUS. Never invent claims about the paper. Anything not supported by the excerpt (your examples, toy sizes, simplifications) goes in "simplifications".
- grounding_quotes: 1-4 short quotes (5-30 words) copied character-for-character from the EXCERPT. Empty list if there is no excerpt.
- source: paper title, section label and equation label as given in the inputs or excerpt ("" if unknown); equation_text: the central equation in plain text.
- symbols: every symbol the learner meets, with meaning and shape or units.
- state: the 2-6 inputs the learner controls. Keep sizes small (vectors <= 6, matrices <= 4x4) and values hand-checkable. kind: number|integer|bool|choice|vector|matrix. default_json is the JSON default, e.g. "3", "true", "\\"a\\"", "[0.5,0.5]", "[[1,0],[0,1]]". min/max/step apply to numbers and to entries of vectors/matrices (null if none). choices: allowed values for kind choice, else [].
- outputs: every quantity the page must compute and show, snake_case keys, including the intermediate values the FOCUS asks to see.
- tests: 4-8 cases that pin down correct behaviour. inputs_json: JSON object of state overrides (missing inputs keep their defaults). expect_json: JSON object mapping output keys to the exact expected number or array. Work each value out carefully; prefer inputs with exact results (0, 0.5, 1, 2). Cover the FOCUS outcomes, its edge cases (zeros, ties, extremes) and the defaults. tol: absolute tolerance, e.g. 1e-6.
- invariants: 2-5 JavaScript boolean expressions true for every valid input, over `out` (outputs) and `s` (state), e.g. "Math.abs(out.total - 1) < 1e-9".
- explorations: exactly 2: what to change, what to observe, why it happens. Follow the FOCUS guidance.
- limitation: one limitation, assumption or common misconception.
- Write for the AUDIENCE. Short phrases, no repetition."""

PLAN_USER = "{context}\n\nReturn the plan."

CONCISE_RETRY = "\n\nYour previous answer was cut off. Answer again, much more briefly."
