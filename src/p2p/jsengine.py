"""Run the generated JavaScript in an embedded engine (no browser, no network).

Primary: quickjs-ng (pip wheel, ~0.5 MB). Fallback: mini-racer (V8) if installed.
If neither is importable, ``load_engine`` returns None and every numeric check is
reported as SKIPPED - never as passed.

compute() is loaded through exactly the same wrapper the page uses
(assemble.wrap_compute), so what we test is what the browser runs.
"""
from __future__ import annotations

import json

from .assemble import wrap_compute

TIME_LIMIT_S = 2
MEMORY_LIMIT = 64 * 1024 * 1024

# JSON.stringify turns NaN/Infinity into null; mark them so Python can see them.
_REPLACER = ("function (k, v) { return (typeof v === 'number' && !isFinite(v)) "
             "? '__nonfinite__' : v; }")


class JSError(Exception):
    pass


class _QuickJS:
    name = "quickjs-ng"

    def __init__(self):
        import quickjs
        self._mod = quickjs
        self.ctx = quickjs.Context()
        self.ctx.set_time_limit(TIME_LIMIT_S)
        self.ctx.set_memory_limit(MEMORY_LIMIT)

    def eval(self, code: str):
        try:
            return self.ctx.eval(code)
        except Exception as e:  # quickjs.JSException and friends
            raise JSError(str(e).splitlines()[0][:300]) from None


class _MiniRacer:
    name = "mini-racer"

    def __init__(self):
        from py_mini_racer import MiniRacer
        self.ctx = MiniRacer()

    def eval(self, code: str):
        try:
            return self.ctx.eval(code, timeout_sec=TIME_LIMIT_S)
        except Exception as e:
            raise JSError(str(e).splitlines()[0][:300]) from None


def load_engine():
    for cls in (_QuickJS, _MiniRacer):
        try:
            return cls()
        except Exception:
            continue
    return None


class ComputeRunner:
    """Loads compute_js once; runs compute(state) and invariant expressions."""

    def __init__(self, engine, compute_js: str):
        self.engine = engine
        self.load_error = None
        engine.eval("var window = {};")
        try:
            engine.eval(wrap_compute(compute_js))
        except JSError as e:
            self.load_error = f"syntax error: {e}"
            return
        err = engine.eval("window.__P2P_COMPUTE_ERROR || ''")
        ok = engine.eval("typeof window.__P2P_COMPUTE === 'function'")
        if err:
            self.load_error = f"error while loading: {err}"
        elif not ok:
            self.load_error = "compute_js does not define function compute(state)"

    def run(self, state: dict):
        """Return the result object; raises JSError on exceptions/timeouts."""
        arg = json.dumps(json.dumps(state))
        text = self.engine.eval(
            f"JSON.stringify(window.__P2P_COMPUTE(JSON.parse({arg})), {_REPLACER})")
        if text is None:
            raise JSError("compute returned undefined")
        return json.loads(text)

    def compile_invariant(self, idx: int, js: str) -> str | None:
        """Compile an invariant once; returns an error message or None."""
        try:
            self.engine.eval(f'window.__inv{idx} = function (out, s) {{ "use strict"; return ({js}); }};')
            return None
        except JSError as e:
            return str(e)

    def invariant(self, idx: int, out: dict, state: dict):
        """True/False, or a string error if the expression throws."""
        a, b = json.dumps(json.dumps(out)), json.dumps(json.dumps(state))
        try:
            return bool(self.engine.eval(f"!!window.__inv{idx}(JSON.parse({a}), JSON.parse({b}))"))
        except JSError as e:
            return f"error: {e}"
