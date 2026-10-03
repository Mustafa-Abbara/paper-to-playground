# Paper to Playground

An agent that turns a focused research-paper excerpt and a learning brief into a single,
offline, interactive HTML explainer for an engineering undergraduate, and checks its own
work before handing it over.

**Team:** Mustafa Abbara, Zeinab Bassam, Yasmina Mansour

**MODEL_ID:** `deepseek/deepseek-v4.1-flash`

## Run it

```bash
python -m pip install -r requirements.txt
export OPENROUTER_API_KEY=...            # read from the environment only, never stored
python agent.py --input case.json --output out --model deepseek/deepseek-v4.1-flash
```

Outputs: `out/index.html` (one self-contained file, works offline in Chromium) and
`out/trace.jsonl`. Exit code `0` = page written with no critical failures, `1` = generation
failed (the best page so far is still written whenever one exists), `2` = bad input or usage.

Python 3.11. Every dependency is pinned in `requirements.txt` (pip wheels only: no browser,
system package, GPU or build step).

## How it works

```
case.json ─► load + validate (only source_url, focus, audience required; other fields kept)
   │
   ├─(1) PLAN call ── JSON schema ──► concept, equation, symbols, learner inputs, outputs,
   │                                  4-6 TEST CASES with expected values, invariants,
   │                                  limitation, verbatim quotes, simplifications
   ├─(2) BUILD call ── sees the plan WITHOUT its tests ──► page text, controls, visuals,
   │                                  2 guided explorations, pure JS compute(state)
   ├─ assemble: fixed generic template + sanitised spec + compute() ─► index.html
   │            (written to disk immediately as a safety copy)
   ├─ CHECKS (no tokens): ~33 static + executed checks, compute() run in QuickJS
   ├─ deterministic fixes (no tokens): drop non-verbatim quotes, unusable invariants,
   │            fix chart references to inputs
   └─(3,4) REPAIR calls, only for critical/major failures, max 2 rounds:
              send only the failing checks + the parts they point at; re-check;
              keep the best version (fewest critical, then major, then minor)
```

- **The model never writes the page.** It writes content and one pure `compute(state)`
  function. The template (`src/p2p/template/`) owns all HTML, CSS and interaction code: generic
  sliders, toggles, selects, editable vectors and matrices, bar/line/heatmap/matrix/diagram
  visuals, step-by-step values and live checks. It contains no paper-specific code. This
  keeps tokens low and the controls reliable.
- **Every number comes from executed code.** Values, charts and live checks are computed by
  `compute()` in the browser; the plan's invariants are evaluated live on the page.
- **Independent answer key.** The plan writes test cases before any code exists and the
  build call never sees them, so passing tests is genuine evidence.

## Limits and safeguards

| Limit (per case) | Hard (brief) | We plan against |
|---|---|---|
| API requests incl. retries | 10 | 8 (every HTTP attempt counts) |
| Completion tokens | 30,000 | 26,000; `max_tokens` clamped to what is left |
| Wall time | 600 s | API calls stop at 540 s; watchdog at 570 s writes the best page and exits |

- Retries only on 408/429/502/503/timeouts, at most 2, honouring `Retry-After` (cap 20 s).
  Errors inside an HTTP 200 body are detected. 400/401/402/403 are never retried.
- Hidden reasoning is disabled for this model (`reasoning: {effort: "none"}`): measured with
  `tools/ping.py`, the provider default spent ~130 reasoning tokens on a trivial prompt, `none`
  spent 0 with the same answer. Providers are sorted by throughput (25 vs 146 tok/s observed).
- Token usage is taken only from each response's `usage` object (plus the generation `id`);
  never estimated. No response caching (cached hits report zero usage).
- Network at run time: OpenRouter only. `source_url` is shown as text and never fetched.

## Input

`case.json`, UTF-8. Required strings: `source_url`, `focus`, `audience`. Any other fields
(e.g. `excerpt`, `title`, `section`) are kept and passed to the model as labelled context,
excerpt first; non-string values are JSON-encoded; very long fields are truncated (logged).
With no excerpt, the quote check is skipped (never reported as passed) and the page says so.

## Trace (`out/trace.jsonl`)

One JSON object per line, flushed immediately. Every line has `ts`, `t` (seconds since
process start), `stage`, `action`, `result` (`ok`/`fail`/`skip`/`info`).

| Event | Key fields |
|---|---|
| `call:<purpose>` (plan, build, repair1, repair2) | `prompt_tokens`, `completion_tokens`, `reasoning_tokens`, `cached_tokens`, `total_tokens`, `elapsed_s`, `finish_reason`, `generation_id`, `http_status`, `attempt`, `retry_reason`, `prompt_chars`, `prompt_sha256` |
| `check:<name>` | `severity` (critical/major/minor), `detail`, `target`, `round` |
| `revision` | `kind` (deterministic/model), `targets`, `reason`, `failures_sent`, `before`/`after` failure counts |
| `test_corrected`, `invariant_corrected`, `test_correction_rejected` | before/after values and the model's written rationale |
| `verdict` | final version and its critical/major/minor counts |
| `summary` (last line) | calls, prompt/completion/reasoning/cached/total tokens, wall time, checks passed/failed/skipped, revisions, exit code |

Never logged: the API key, request headers, prompt or response text, model reasoning.
Prompts are recorded only as length plus a SHA-256 prefix.

## Checks (token-free)

- **Static:** no network APIs or external URLs, single file under 400 KB, all required
  sections (idea, symbols, 2 explorations, limitation, grounding, simplifications), citation,
  quotes found in the excerpt (word order preserved; garbled PDF math tolerated), at least
  2 controls and 1 visual, the fixed "does not reproduce the paper's results" disclaimer.
- **Executed (QuickJS, same wrapper as the page):** compute loads and runs on defaults; no
  NaN/Infinity; every plan output exists; compute reads only real inputs; every chart reference
  resolves; every plan test (classified as *code wrong* vs *suspect test*: if its own expected
  values break an invariant, or a hand-rounded value disagrees while all invariants hold);
  invariants on defaults and systematic edge inputs (min, max, zeros, ties, one-hot, seeded
  random); at least 2 controls change the result; both exploration presets run cleanly.
- If no JavaScript engine is available, numeric checks are reported as **skipped**.
- A repair may correct a plan test or invariant only with a written rationale; a corrected
  test is kept only if it then passes, otherwise the original is restored.

## Measured results

`tools/harness.py` runs every case in `cases/` twice in fresh folders with a clean
environment and the 600 s timeout. Final run (`docs/harness_final.md`), 6 cases × 2 runs:

| Case | Mean tokens | Mean time | Calls | Exit 0 |
|---|---|---|---|---|
| a_attention (public example A) | 9.5k | 17.5 s | 2-4 | 2/2 |
| b_entropy (public example B) | 6.7k | 12.7 s | 2 | 2/2 |
| c_softmax_temperature | 7.8k | 16.5 s | 2-3 | 2/2 |
| d_batchnorm | 8.8k | 17.4 s | 2-3 | 2/2 |
| e_adam | 11.0k | 22.6 s | 2-4 | 2/2 |
| z_no_excerpt (no excerpt, extra fields) | 5.4k | 15.2 s | 2 | 2/2 |

Median 7.5k total tokens and 16 s per run; 12/12 exit 0; every page offline-clean.
Earlier rounds (`docs/harness_baseline.md`, `docs/harness_tuned*.md`) and the git log show
each tuning change and its measured effect.

## Example

`examples/entropy/`: `case.json`, `index.html`, `trace.jsonl`, copied unchanged from
harness run `b_entropy/run1` (2 calls, 6,576 tokens, 12.3 s, 0 critical/major/minor).

## Limitations

- About 1 run in 6 ends with one *major* check still failing. In the final harness this was
  always wrong plan content (a miscalculated test value or a false invariant), not broken
  code; the trace records it, and the page is still written.
- The model's mental arithmetic for non-exact values (e.g. `exp`) is unreliable, so such test
  values are treated as evidence only together with the invariants.
- Phone-width layout and console errors are verified with the optional browser smoke test
  (`tools/smoke_browser.py`, dev only), not inside the agent.

## Repository

| Path | Contents |
|---|---|
| `agent.py` | CLI and orchestration (versions, best-version selection, watchdog) |
| `src/p2p/` | `case`, `trace`, `budget`, `llm` (OpenRouter client), `config` (per-model settings), `schemas`, `prompts`, `plan`, `build`, `assemble`, `jsengine`, `checks`, `repair` |
| `src/p2p/template/` | generic page template, styles and runtime |
| `tests/` | 114 unit and integration tests (fake model; no key or network needed): `python -m pytest -q` |
| `tools/` | `harness.py`, `ping.py`, `trace_view.py`, `offline_scan.py`, `make_case.py`, `render_fixture.py`, `smoke_browser.py` (dev only), secret scanner + git hook |
| `cases/` | practice cases; `cases/src/` holds their briefs and pasted excerpts |
| `docs/` | assignment text, page contract, harness results |

## Reuse credits

- [requests](https://pypi.org/project/requests/) (Apache-2.0) and its dependencies
  certifi (MPL-2.0), charset-normalizer (MIT), idna (BSD-3-Clause), urllib3 (MIT).
- [quickjs-ng](https://pypi.org/project/quickjs-ng/) Python bindings (MIT) for the QuickJS-ng
  engine (MIT), used to execute generated `compute()` code in the checks.
- pytest (MIT) and Playwright (Apache-2.0), development only.
- Chart colours: the Okabe–Ito colour-blind-safe palette.
- Practice-case excerpts are short passages from the cited papers, used only as test inputs.
- AI coding assistant: Claude (Anthropic) was used to write and review this code. The
  generic template and scaffolding were prepared before the session; no paper-specific
  answers or pages are stored or prompted.
