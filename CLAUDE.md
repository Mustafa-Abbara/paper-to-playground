# CLAUDE.md — project rules for "paper-to-playground"

Read this file and `docs/SPEC.md` before every task.

## What we are building
An agent that reads one `case.json` (a paper excerpt + focus + audience) and writes a
single self-contained, offline, interactive HTML explainer ("playground") plus a trace.

CLI (exact):
```
python agent.py --input case.json --output out --model <MODEL_ID>
```
Outputs: `<output>/index.html` and `<output>/trace.jsonl`.
Exit codes: 0 success, 1 generation failed, 2 bad input/usage.

## Hard rules
- Python 3.11. `agent.py` stays at the repo root. Code lives in `src/p2p/`.
- Runtime deps only from `requirements.txt`, every package pinned with `==`.
  No system packages, no browser downloads, no GPU. Dev-only tools go in
  `requirements-dev.txt` and must never be imported by `agent.py`.
- All LLM calls go to OpenRouter: `POST https://openrouter.ai/api/v1/chat/completions`
  with header `Authorization: Bearer $OPENROUTER_API_KEY` (read from the environment only).
- NEVER print, log, trace or commit the API key. No `.env` in git (only `.env.example`).
- Per-case limits (hard): ≤10 API requests including retries, ≤30,000 completion tokens,
  ≤10 minutes wall time. Soft caps we enforce: 8 calls, 26,000 completion tokens, 540 s.
- Token usage comes only from the response `usage` object. Never invent numbers.
  Do not send `usage: {include: true}` (deprecated). Do not use response caching.
- Network at assessment time = OpenRouter only. NEVER fetch `source_url` or anything else.
  Use the excerpt and other fields in `case.json`.
- `index.html` must be ONE self-contained file: no CDN, no remote fonts/images/scripts,
  no `@import`, no external URLs (the only URL allowed is `source_url` shown as plain text).
- The trace never contains message bodies, model reasoning, or credentials.

## Integrity rules
- Generic templates and generic widgets only. NO paper-specific code paths
  (e.g. `if "attention" in focus`), no stored pages/answers, no lookup tables keyed on
  URLs or titles, no full example pages pasted into prompts as few-shot answers.
- Never write text aimed at the grader into pages, prompts or the repo.

## Architecture (target)
plan call → build call (JSON spec + pure JS `compute(state)`) → deterministic checks
(static + executed in QuickJS) → targeted JSON-patch repair (≤2 rounds) → best version.
The template owns all DOM code; the LLM never writes the whole page.

## Workflow
- One commit + one annotated tag per stage (`v0.N-<name>`).
- Run `python -m pytest -q` before committing.
- The pre-commit hook (`tools/scan_secrets.py`) must stay installed:
  `python tools/install_hooks.py`.
