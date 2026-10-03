"""Deterministic, token-free checks of a generated page.

Static checks look at the spec and the final HTML (offline, size, required sections,
grounding quotes, visual references). Numeric checks EXECUTE compute() in QuickJS on
the defaults, on every plan test, on systematic edge inputs, and evaluate the plan's
invariants on all of them.

Each result has a severity:
  critical  page unusable (code does not load / crashes on defaults / network use)
  major     page wrong (broken invariant, failing exact test, dead controls, ...)
  minor     worth recording, not worth tokens (e.g. a suspect hand-rounded test)
Status is "pass", "fail" or "skip". A check that could not run is "skip", never "pass".
"""
from __future__ import annotations

import copy
import json
import random
import re
import unicodedata
from dataclasses import asdict, dataclass

from .jsengine import ComputeRunner, JSError, load_engine

MAX_HTML_BYTES = 400_000
SVG_NS = "http://www.w3.org/2000/svg"
DISCLAIMER_MARK = "does not reproduce the paper"
NONFINITE = "__nonfinite__"


@dataclass
class CheckResult:
    name: str
    status: str          # pass | fail | skip
    severity: str        # critical | major | minor
    detail: str = ""
    target: str = ""     # what a repair would change: compute_js, spec.<field>, plan.tests ...

    @property
    def failed(self):
        return self.status == "fail"


def _r(results, name, ok, severity, detail="", target=""):
    status = "skip" if ok is None else ("pass" if ok else "fail")
    results.append(CheckResult(name, status, severity, detail, target))


# --- helpers -------------------------------------------------------------------
def norm_text(s: str) -> str:
    """Normalise for quote matching: Unicode NFKC, unify quotes/dashes/minus, collapse
    whitespace, lower-case."""
    s = unicodedata.normalize("NFKC", str(s))
    s = re.sub(r"[‘’‛′`]", "'", s)
    s = re.sub(r"[“”‟″]", '"', s)
    s = re.sub(r"[‐-―−]", "-", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip().lower()


WORD = re.compile(r"[a-z]{2,}")
# words that are really math notation (sqrt, log, softmax...) are ignored when matching
MATH_WORDS = {"sqrt", "log", "exp", "frac", "sum", "softmax", "max", "min", "dk", "dv"}


def _words(s: str) -> list[str]:
    return [w for w in WORD.findall(norm_text(re.sub(r"<[^>]+>", " ", s))) if w not in MATH_WORDS]


def quote_in_excerpt(quote: str, excerpt: str) -> bool:
    """True if the quote's words appear in the excerpt in the same order, contiguously,
    ignoring punctuation and math notation (PDF/HTML copies scramble formulas: a model
    writing sqrt(d_k) where the excerpt has a broken-up square root is still quoting)."""
    q = norm_text(re.sub(r"<[^>]+>", "", quote)).strip(" .\"'")
    if len(q) >= 8 and q in norm_text(excerpt):
        return True
    qw, ew = _words(quote), _words(excerpt)
    if len(qw) < 5:
        return False
    n = len(qw)
    for i in range(len(ew) - n + 1):
        if ew[i:i + n] == qw:
            return True
    return False


def pick_excerpt_sentences(excerpt: str, plan: dict, k: int = 2) -> list[str]:
    """Deterministic fallback quotes: real excerpt sentences sharing most words with
    the plan's concept, equation and symbols."""
    key = set(_words(" ".join([str(plan.get("concept", "")),
                              str((plan.get("source") or {}).get("equation_text", ""))]
                             + [str(s.get("meaning", "")) for s in plan.get("symbols") or []])))
    flat = re.sub(r"\s+", " ", excerpt)
    sents = [x.strip() for x in re.split(r"(?<=[.!?])\s+", flat)]
    scored = []
    for i, x in enumerate(sents):
        w = _words(x)
        if 8 <= len(w) <= 45 and not re.search(r"[\u2211\u221a]|\b\w\n", x):
            scored.append((len(key & set(w)), -i, x))
    return [x for _, _, x in sorted(scored, reverse=True)[:k]]


def nonfinite_paths(obj, path="") -> list[str]:
    if obj == NONFINITE:
        return [path or "(value)"]
    if isinstance(obj, dict):
        return [p for k, v in obj.items() for p in nonfinite_paths(v, f"{path}.{k}" if path else k)]
    if isinstance(obj, list):
        return [p for i, v in enumerate(obj) for p in nonfinite_paths(v, f"{path}[{i}]")]
    return []


def _rounding_slack(exp: float) -> float:
    """Half a unit in the last written decimal of a hand-rounded expectation (1.3863 -> 5e-5).
    Only for values written with 1-5 decimals; exact values (0, 1, 2.5) get no slack."""
    text = repr(float(exp))
    if "e" in text or "." not in text:
        return 0.0
    d = len(text.split(".")[1].rstrip("0"))
    return 0.5 * 10 ** -d + 1e-12 if 1 <= d <= 5 and exp != round(exp, 1) else 0.0


def close(got, exp, tol) -> bool:
    if isinstance(exp, bool) or isinstance(got, bool):
        return got == exp
    if isinstance(exp, (int, float)):
        return isinstance(got, (int, float)) and abs(got - exp) <= max(tol, _rounding_slack(exp))
    if isinstance(exp, list):
        return isinstance(got, list) and len(got) == len(exp) and all(close(g, e, tol) for g, e in zip(got, exp))
    return got == exp


def _short(v, n=90):
    s = json.dumps(v, separators=(",", ":"))
    s = re.sub(r"(\d\.\d{4})\d+", r"\1", s)
    return s if len(s) <= n else s[:n] + "..."


# --- state construction (mirrors runtime.js bindings) ------------------------
def apply_bindings(state: dict, controls: list[dict], explicit: set | None = None) -> dict:
    """Resize bound vectors/matrices like the page does. ``explicit`` = keys the caller set
    on purpose (test inputs, presets): an explicitly given vector keeps its length and its
    count control follows it, instead of being cut to the default count."""
    s = copy.deepcopy(state)
    explicit = explicit or set()
    for c in controls:
        v = s.get(c["id"])
        if c["id"] in explicit and isinstance(v, list) and v:
            if c.get("type") == "vector" and c.get("length_from") and c["length_from"] not in explicit:
                s[c["length_from"]] = len(v)
            if c.get("type") == "matrix" and isinstance(v[0], list):
                if c.get("rows_from") and c["rows_from"] not in explicit:
                    s[c["rows_from"]] = len(v)
                if c.get("cols_from") and c["cols_from"] not in explicit:
                    s[c["cols_from"]] = len(v[0])
    def count(key):
        v = s.get(key)
        return max(1, int(round(v))) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    for c in controls:
        fill = c.get("fill", 0) if isinstance(c.get("fill"), (int, float)) else 0
        if c.get("type") == "vector" and count(c.get("length_from")) is not None:
            n = count(c["length_from"])
            v = list(s.get(c["id"]) or [])[:n]
            s[c["id"]] = v + [fill] * (n - len(v))
        if c.get("type") == "matrix" and (count(c.get("rows_from")) or count(c.get("cols_from"))):
            m = [list(r) if isinstance(r, list) else [r] for r in (s.get(c["id"]) or [])]
            r = count(c.get("rows_from")) or len(m)
            k = count(c.get("cols_from")) or (len(m[0]) if m else 1)
            m = m[:max(1, r)] + [[] for _ in range(max(1, r) - len(m))]
            s[c["id"]] = [row[:max(1, k)] + [fill] * (max(1, k) - len(row[:max(1, k)])) for row in m]
    return s


def defaults_of(controls: list[dict]) -> dict:
    return apply_bindings({c["id"]: copy.deepcopy(c.get("default")) for c in controls}, controls)


def variants(c: dict, default, rng: random.Random) -> list[tuple[str, object]]:
    """Systematic edge inputs for one control: (label, value)."""
    t, lo, hi = c.get("type"), c.get("min"), c.get("max")
    num = lambda x: isinstance(x, (int, float)) and not isinstance(x, bool)
    out = []
    if t in ("slider", "number"):
        for lab, v in (("min", lo), ("max", hi)):
            if num(v):
                out.append((lab, v))
        if num(default):
            out.append(("double", default * 2 if not num(hi) else min(hi, default * 2 or 1)))
    elif t == "toggle":
        out.append(("flipped", not bool(default)))
    elif t == "select":
        for o in c.get("options") or []:
            v = o.get("value") if isinstance(o, dict) else o
            if v != default:
                out.append((f"option {v}", v))
    elif t == "vector" and isinstance(default, list) and default:
        n = len(default)
        a, b = (lo if num(lo) else -2), (hi if num(hi) else 2)
        if c.get("normalize"):
            out += [("uniform", [1 / n] * n), ("one-hot", [1.0] + [0.0] * (n - 1)),
                    ("with zero", ([0.5, 0.5] + [0.0] * (n - 2)) if n >= 2 else [1.0])]
        else:
            if a <= 0 <= b:
                out.append(("zeros", [0] * n))
            out.append(("ties", [min(max(1, a), b)] * n))
        out.append(("random", [round(rng.uniform(max(a, 0) if c.get("normalize") else a, b), 2) for _ in range(n)]))
    elif t == "matrix" and isinstance(default, list) and default and isinstance(default[0], list):
        r, k = len(default), len(default[0])
        a, b = (lo if num(lo) else -3), (hi if num(hi) else 3)
        if a <= 0 <= b:
            out.append(("zeros", [[0] * k for _ in range(r)]))
        out.append(("ties", [[min(max(1, a), b)] * k for _ in range(r)]))
        out.append(("random", [[round(rng.uniform(a, b), 2) for _ in range(k)] for _ in range(r)]))
    return out


# --- static checks -----------------------------------------------------------------
NETWORK_PATTERNS = [
    (r"<script[^>]+\bsrc\s*=", "external <script src>"),
    (r"<link\b[^>]*\bhref\s*=", "<link href>"),
    (r"<(iframe|object|embed)\b", "embedded frame/object"),
    (r"@import\b", "CSS @import"),
    (r"url\(\s*['\"]?(?!#|data:)", "CSS url() to a resource"),
    (r"\b(src|href|action|poster)\s*=\s*['\"]?(?!#)[a-z]+:", "attribute pointing at a URL"),
    (r"\bfetch\s*\(", "fetch()"),
    (r"\bXMLHttpRequest\b", "XMLHttpRequest"),
    (r"\bWebSocket\b", "WebSocket"),
    (r"\bEventSource\b", "EventSource"),
    (r"\bsendBeacon\b", "navigator.sendBeacon"),
    (r"\bimport\s*\(", "dynamic import()"),
]


def network_findings(html: str, source_url: str = "") -> tuple[list[str], list[str]]:
    """(network constructs found, URLs other than the SVG namespace and the source URL)."""
    found = [label for pat, label in NETWORK_PATTERNS if re.search(pat, html, re.I)]
    urls = set(re.findall(r"https?://[^\s\"'<>)\\]+", html))
    allowed = {SVG_NS, source_url.rstrip("\\")}
    stray = sorted(u for u in urls if u not in allowed and not (source_url and source_url.startswith(u)))
    return found, stray


def static_checks(case, spec: dict, html: str, results: list):
    # offline
    found, stray = network_findings(html, case.source_url)
    _r(results, "offline_no_network", not found and not stray, "critical",
       "; ".join(found + [f"URL {u}" for u in stray[:5]]) or "no network APIs or external URLs",
       "compute_js" if found else "spec")
    size = len(html.encode("utf-8"))
    _r(results, "single_file_size", size < MAX_HTML_BYTES, "critical", f"{size / 1024:.0f} KB")
    _r(results, "disclaimer_present", DISCLAIMER_MARK in html, "major", "fixed template disclaimer")

    sec = spec.get("sections") or {}
    idea = sec.get("idea") or {}
    missing = [k for k in ("what", "why") if not str(idea.get(k, "")).strip()]
    _r(results, "section_idea", not missing, "major", f"missing idea.{missing}" if missing else "what + why",
       "spec.sections.idea")
    n_sym = len(sec.get("symbols") or [])
    _r(results, "section_symbols", n_sym >= 2, "major", f"{n_sym} symbols", "spec.sections.symbols")
    ex = [e for e in sec.get("explorations") or [] if isinstance(e, dict)]
    ex_ok = len(ex) == 2 and all(str(e.get(k, "")).strip() for e in ex for k in ("change", "observe", "why"))
    _r(results, "section_explorations", ex_ok, "major", f"{len(ex)} explorations with change/observe/why",
       "spec.sections.explorations")
    lim = sec.get("limitation") or {}
    _r(results, "section_limitation", bool(str(lim.get("text", "")).strip()), "major",
       lim.get("kind", "missing"), "spec.sections.limitation")
    _r(results, "section_equation", bool(str(sec.get("equation", "")).strip()), "minor", "", "spec.sections.equation")
    gr = spec.get("grounding") or {}
    _r(results, "grounding_simplifications", bool(gr.get("our_simplifications")), "major",
       f"{len(gr.get('our_simplifications') or [])} listed", "spec.grounding")
    meta = spec.get("meta") or {}
    citable = bool(case.excerpt or case.get("title") or case.get("section"))
    has_cite = bool(meta.get("paper_title") or meta.get("section_label"))
    _r(results, "grounding_citation", has_cite if citable or has_cite else None, "major",
       f"{meta.get('paper_title', '')} | {meta.get('section_label', '')} | {meta.get('equation_label', '')}"
       if citable or has_cite else "input names no paper, section or excerpt: only the source URL is cited",
       "spec.meta")
    quotes = gr.get("from_excerpt") or []
    if not case.excerpt:
        _r(results, "grounding_quotes_verbatim", None, "major", "no excerpt supplied: nothing to verify")
    else:
        bad = [q for q in quotes if not quote_in_excerpt(q, case.excerpt)]
        _r(results, "grounding_quotes_verbatim", not bad and bool(quotes), "major",
           f"{len(quotes) - len(bad)}/{len(quotes)} quotes found in the excerpt"
           + (f"; not found: {bad[0][:60]!r}" if bad else ""), "spec.grounding.from_excerpt")
    n_ctl = len(spec.get("controls") or [])
    _r(results, "controls_count", n_ctl >= 2, "major", f"{n_ctl} controls", "spec.controls")
    n_vis = len(spec.get("visuals") or [])
    _r(results, "visuals_present", n_vis >= 1, "major", f"{n_vis} visuals", "spec.visuals")


# --- numeric checks ----------------------------------------------------------------
def _refs(obj) -> set[str]:
    out = set()
    if isinstance(obj, str):
        if re.fullmatch(r"(outputs|state)\.[A-Za-z_][\w.]*", obj):
            out.add(obj)
        out |= {m for m in re.findall(r"\{((?:outputs|state)\.[A-Za-z_][\w.]*)(?::\d)?\}", obj)}
    elif isinstance(obj, dict):
        for v in obj.values():
            out |= _refs(v)
    elif isinstance(obj, list):
        for v in obj:
            out |= _refs(v)
    return out


def _lookup(path: str, outputs: dict, state: dict):
    root, *rest = path.split(".")
    cur = outputs if root == "outputs" else state
    for k in rest:
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        elif isinstance(cur, list) and k.isdigit() and int(k) < len(cur):
            cur = cur[int(k)]
        else:
            return None
    return cur


def state_reads(compute_js: str) -> set[str]:
    """Input names compute() reads from its state parameter (static scan)."""
    m = re.search(r"function\s+compute\s*\(\s*([A-Za-z_$][\w$]*)", compute_js or "")
    if not m:
        return set()
    p = re.escape(m.group(1))
    names = set(re.findall(rf"\b{p}\s*\.\s*([A-Za-z_$][\w$]*)", compute_js))
    names |= set(re.findall(rf"\b{p}\s*\[\s*['\"]([^'\"]+)['\"]\s*\]", compute_js))
    for body in re.findall(rf"(?:const|let|var)\s*\{{([^}}]*)\}}\s*=\s*{p}\b", compute_js):
        names |= {re.split(r"[:=\s]", x.strip())[0] for x in body.split(",") if x.strip()}
    return {n for n in names if n and n not in ("hasOwnProperty", "length")}


def resolve_ref(ref, outputs: dict, state: dict):
    """Same rule as runtime.js: 'outputs.k' / 'state.k' paths, or a bare name meaning an
    output of that name, else an input of that name; anything else is a literal."""
    if not isinstance(ref, str):
        return ref
    if re.fullmatch(r"(outputs|state)\.[A-Za-z_][\w.]*", ref):
        return _lookup(ref, outputs, state)
    if re.fullmatch(r"[A-Za-z_]\w*", ref):
        if ref in outputs:
            return outputs[ref]
        if ref in state:
            return state[ref]
    return ref


def _finite_numbers(v) -> list:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return [v]
    if isinstance(v, list):
        return [x for x in v if isinstance(x, (int, float)) and not isinstance(x, bool)]
    return []


def visual_data_problems(visuals: list, outputs: dict, state: dict) -> list[str]:
    """Which visuals would render empty ('No values to show.') in the page."""
    bad = []
    for i, v in enumerate(visuals or []):
        if not isinstance(v, dict):
            continue
        t, name = v.get("type"), f"visual {i + 1} ({v.get('type')}: {str(v.get('title', ''))[:40]})"
        R = lambda r: resolve_ref(r, outputs, state)
        if t == "bar":
            series = v.get("series") or [{"values": v.get("values", v.get("source"))}]
            ok = any(_finite_numbers(R(sv.get("values", sv.get("y", sv.get("source"))))) for sv in series if isinstance(sv, dict))
        elif t == "line":
            series = v.get("series") or [{"x": v.get("x"), "y": v.get("y", v.get("values"))}]
            ok = False
            for sv in series:
                if not isinstance(sv, dict):
                    continue
                ys = R(sv.get("y", sv.get("values")))
                xs = R(sv.get("x", v.get("x"))) if (sv.get("x") is not None or v.get("x") is not None) else None
                if isinstance(ys, list) and _finite_numbers(ys) and (xs is None or (isinstance(xs, list) and _finite_numbers(xs))):
                    ok = True
            for pt in v.get("points") or []:
                if isinstance(pt, dict) and _finite_numbers(R(pt.get("x"))) and _finite_numbers(R(pt.get("y"))):
                    ok = True
        elif t in ("heatmap", "matrix"):
            d = R(v.get("values", v.get("source")))
            ok = isinstance(d, list) and len(d) > 0
        elif t == "svg":
            ok = bool(v.get("items"))
        else:
            ok = False
        if not ok:
            bad.append(name)
    return bad


def numeric_checks(plan: dict, spec: dict, compute_js: str, results: list, engine=None) -> dict:
    """Runs compute(); returns facts used by deterministic fixes (e.g. invalid invariants)."""
    facts = {"invalid_invariants": [], "failing_invariants": [], "engine": None}
    engine = engine or load_engine()
    if engine is None:
        for n in ("compute_loads", "compute_defaults", "plan_tests", "invariants", "edge_inputs"):
            _r(results, n, None, "critical", "no JavaScript engine available: numeric checks skipped")
        return facts
    facts["engine"] = engine.name
    runner = ComputeRunner(engine, compute_js)
    _r(results, "compute_loads", runner.load_error is None, "critical",
       runner.load_error or f"compiled in {engine.name}", "compute_js")
    if runner.load_error:
        return facts

    controls = spec.get("controls") or []
    ids = {c.get("id") for c in controls}
    unknown = sorted(state_reads(compute_js) - ids)
    _r(results, "compute_reads_real_inputs", not unknown, "major",
       f"compute reads inputs that do not exist: {unknown}; the inputs are {sorted(ids)}" if unknown
       else "every input compute reads is a control", "compute_js")
    defaults = defaults_of(controls)
    try:
        base = runner.run(defaults)
        outputs = base.get("outputs") if isinstance(base, dict) else None
        ok = isinstance(outputs, dict)
        _r(results, "compute_defaults", ok, "critical",
           "returns {outputs, intermediates, checks}" if ok else "result has no outputs object", "compute_js")
    except JSError as e:
        _r(results, "compute_defaults", False, "critical", f"throws on default inputs: {e}", "compute_js")
        return facts
    if not isinstance(outputs, dict):
        return facts
    nf = nonfinite_paths({"outputs": outputs, "intermediates": base.get("intermediates")})
    _r(results, "defaults_finite", not nf, "major", f"NaN/Infinity at {nf[:4]}" if nf else "all finite",
       "compute_js")
    missing = [o["key"] for o in plan.get("outputs") or [] if o.get("key") not in outputs]
    _r(results, "plan_outputs_present", not missing, "major",
       f"missing outputs {missing}" if missing else f"all {len(plan.get('outputs') or [])} plan outputs",
       "compute_js")
    bad_refs = sorted(r for r in _refs(spec.get("visuals")) | _refs(spec.get("outputs"))
                      if _lookup(r, outputs, defaults) is None)
    for o in spec.get("outputs") or []:
        if o.get("key") and o["key"] not in outputs:
            bad_refs.append(f"outputs.{o['key']}")
    _r(results, "visual_references", not bad_refs, "major",
       f"missing {sorted(set(bad_refs))[:6]}" if bad_refs else "every chart and readout reference resolves",
       "spec.visuals")
    empty = visual_data_problems(spec.get("visuals") or [], outputs, defaults)
    _r(results, "visuals_have_data", not empty, "major",
       f"would render empty: {empty}; data fields must name outputs as \"outputs.<key>\" "
       f"(outputs available: {sorted(outputs)[:12]})" if empty else "every visual has data to draw",
       "spec.visuals")

    # invariants: compile, then classify ones that cannot be evaluated on the defaults
    invs = [i for i in plan.get("invariants") or [] if isinstance(i, dict) and i.get("js")]
    live = []
    for idx, inv in enumerate(invs):
        err = runner.compile_invariant(idx, inv["js"])
        val = None if err else runner.invariant(idx, outputs, defaults)
        if err or isinstance(val, str):
            facts["invalid_invariants"].append(inv.get("name") or inv["js"])
        else:
            live.append((idx, inv))
    _r(results, "invariants_valid", not facts["invalid_invariants"], "minor",
       f"cannot evaluate: {facts['invalid_invariants']}" if facts["invalid_invariants"]
       else f"{len(invs)} invariants evaluate", "plan.invariants")

    def check_invs(out, st):
        return [inv.get("name") or inv["js"] for idx, inv in live if runner.invariant(idx, out, st) is not True]

    # plan tests
    tests = plan.get("tests") or []
    per_test = []
    for t in tests:
        st = apply_bindings({**defaults, **copy.deepcopy(t.get("inputs") or {})}, controls,
                            set(t.get("inputs") or {}))
        try:
            got = runner.run(st).get("outputs") or {}
        except JSError as e:
            per_test.append((t, "crash", str(e), [], {}))
            continue
        diffs = {k: (v, got.get(k)) for k, v in (t.get("expect") or {}).items()
                 if not close(got.get(k), v, t.get("tol", 1e-6))}
        inv_fail = check_invs(got, st)
        # does the test's own expectation contradict an invariant? (expected values overlaid)
        overlay = {**got, **(t.get("expect") or {})}
        self_inconsistent = bool(diffs) and bool(check_invs(overlay, st)) and not inv_fail
        per_test.append((t, "pass" if not diffs else "fail", "", inv_fail, diffs, self_inconsistent))
    n_pass = sum(1 for p in per_test if p[1] == "pass")
    for p in per_test:
        t, status = p[0], p[1]
        name = f"test:{t.get('name', '?')}"
        if status == "crash":
            _r(results, name, False, "major", f"compute throws: {p[2]}", "compute_js")
            continue
        _, _, _, inv_fail, diffs, *rest = p
        self_inconsistent = rest[0] if rest else False
        if status == "pass":
            _r(results, name, True, "major", f"{len(t.get('expect') or {})} outputs match")
            continue
        detail = "; ".join(f"{k}: expected {_short(e)} got {_short(g)}" for k, (e, g) in diffs.items())
        rounded = t.get("tol", 1e-6) >= 1e-4
        mostly_ok = n_pass >= (len(per_test) + 1) // 2
        if inv_fail:
            _r(results, name, False, "major", f"{detail}; invariants broken: {inv_fail}", "compute_js")
        elif self_inconsistent:
            _r(results, name, False, "minor", f"suspect test (its expected values break an invariant): {detail}",
               "plan.tests")
        elif rounded and mostly_ok:
            _r(results, name, False, "minor",
               f"suspect test (hand-rounded expectation; all invariants hold; {n_pass}/{len(per_test)} tests pass): {detail}",
               "plan.tests")
        else:
            _r(results, name, False, "major", f"{detail} (code or test is wrong)", "compute_js|plan.tests")

    # invariants + finiteness on systematic edge inputs
    rng = random.Random(0)
    edge_fail, crash, nonfin, sensitive = [], [], [], set()
    base_sig = json.dumps(outputs, sort_keys=True)
    for c in controls:
        for lab, val in variants(c, defaults.get(c["id"]), rng):
            st = apply_bindings({**defaults, c["id"]: val}, controls)
            try:
                res = runner.run(st)
            except JSError as e:
                crash.append(f"{c['id']}={lab}: {e}")
                continue
            out = res.get("outputs") or {}
            if json.dumps(out, sort_keys=True) != base_sig:
                sensitive.add(c["id"])
            nf = nonfinite_paths(out)
            if nf:
                nonfin.append(f"{c['id']}={lab}: {nf[:2]}")
            for name in check_invs(out, st):
                edge_fail.append(f"{name} @ {c['id']}={lab}")
    _r(results, "edge_inputs_no_crash", not crash, "major", "; ".join(crash[:4]) or "no exceptions", "compute_js")
    _r(results, "edge_inputs_finite", not nonfin, "major", "; ".join(nonfin[:4]) or "no NaN/Infinity",
       "compute_js")
    broken = sorted({e.split(" @ ")[0] for e in edge_fail})
    facts["failing_invariants"] = broken
    code_of = {(inv.get("name") or inv["js"]): inv["js"] for _, inv in live}
    _r(results, "invariants_hold", not edge_fail, "major",
       ("; ".join(edge_fail[:4]) + " | invariant code: "
        + "; ".join(f"{b}: {code_of.get(b, '')}" for b in broken[:3])) if edge_fail
       else f"{len(live)} invariants hold on defaults and edge inputs", "compute_js|plan.invariants")
    _r(results, "controls_change_result", len(sensitive) >= 2, "major",
       f"{len(sensitive)}/{len(controls)} controls change the outputs: {sorted(sensitive)}", "compute_js")

    # guided explorations must set up without errors
    bad_ex = []
    for i, e in enumerate((spec.get("sections") or {}).get("explorations") or []):
        if isinstance(e, dict) and isinstance(e.get("preset"), dict) and e["preset"]:
            st = apply_bindings({**defaults, **copy.deepcopy(e["preset"])}, controls, set(e["preset"]))
            try:
                out = runner.run(st).get("outputs") or {}
                if nonfinite_paths(out):
                    bad_ex.append(f"exploration {i + 1}: NaN/Infinity")
            except JSError as err:
                bad_ex.append(f"exploration {i + 1}: {err}")
    _r(results, "exploration_presets_run", not bad_ex, "major", "; ".join(bad_ex) or "presets run cleanly",
       "spec.sections.explorations")
    return facts


def run_checks(case, plan: dict, spec: dict, compute_js: str, html: str, engine=None):
    """Never raises: a bug in a check is reported as a skipped check, not a crash."""
    results: list[CheckResult] = []
    facts = {"invalid_invariants": [], "engine": None}
    for name, fn in (("static_checks", lambda: static_checks(case, spec, html, results)),
                     ("numeric_checks", lambda: numeric_checks(plan, spec, compute_js, results, engine=engine))):
        try:
            out = fn()
            if isinstance(out, dict):
                facts = out
        except Exception as e:  # noqa: BLE001 - checks must not take the run down
            _r(results, f"{name}_error", None, "major", f"checks could not run: {type(e).__name__}: {e}")
    return results, facts


# --- deterministic fixes (no model call) -------------------------------------------
def deterministic_fixes(case, spec: dict, facts: dict, plan: dict | None = None) -> tuple[dict, list[dict]]:
    """Fix what needs no model: drop non-verbatim quotes and invariants that cannot be
    evaluated (they would show as a false failure on the page). Returns (spec, revisions)."""
    spec = copy.deepcopy(spec)
    revisions = []
    gr = spec.setdefault("grounding", {})
    if case.excerpt and gr.get("from_excerpt"):
        keep = [q for q in gr["from_excerpt"] if quote_in_excerpt(q, case.excerpt)]
        dropped = len(gr["from_excerpt"]) - len(keep)
        if dropped:
            gr["from_excerpt"] = keep
            revisions.append({"targets": ["spec.grounding.from_excerpt"],
                              "reason": f"removed {dropped} quote(s) not found verbatim in the excerpt"})
    if case.excerpt and not gr.get("from_excerpt"):
        picked = pick_excerpt_sentences(case.excerpt, plan or {})
        if picked:
            gr["from_excerpt"] = picked
            revisions.append({"targets": ["spec.grounding.from_excerpt"],
                              "reason": f"no usable quotes: copied {len(picked)} sentence(s) from the excerpt"})
    bad = set(facts.get("invalid_invariants") or [])
    if bad:
        spec["invariants"] = [i for i in spec.get("invariants") or [] if (i.get("name") or i.get("js")) not in bad]
        revisions.append({"targets": ["spec.invariants"],
                          "reason": f"removed invariant(s) that cannot be evaluated: {sorted(bad)}"})
    return spec, revisions


def summarize(results: list[CheckResult]) -> dict:
    def count(sev):
        return sum(1 for r in results if r.failed and r.severity == sev)
    return {"critical": count("critical"), "major": count("major"), "minor": count("minor"),
            "passed": sum(1 for r in results if r.status == "pass"),
            "skipped": sum(1 for r in results if r.status == "skip"), "total": len(results)}


def as_dicts(results):
    return [asdict(r) for r in results]
