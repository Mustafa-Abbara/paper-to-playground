#!/usr/bin/env python3
"""Paper to Playground agent - entry point.

    python agent.py --input case.json --output out --model <MODEL_ID>

Pipeline: load case -> PLAN call -> BUILD call -> assemble -> token-free checks
-> deterministic fixes -> up to 2 targeted REPAIR calls -> write the best version.
Writes <output>/index.html and <output>/trace.jsonl.
Exit codes: 0 success (page written, no critical failures), 1 generation failed,
2 bad input/usage.
"""
import time

T0 = time.monotonic()  # latency is measured from process start

import argparse  # noqa: E402
import html  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import platform  # noqa: E402
import sys  # noqa: E402
import threading  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from p2p import llm  # noqa: E402
from p2p.assemble import assemble  # noqa: E402
from p2p.budget import Budget  # noqa: E402
from p2p.build import BuildError, build, compose_spec  # noqa: E402
from p2p.case import CaseError, load_case  # noqa: E402
from p2p.checks import deterministic_fixes, run_checks, summarize  # noqa: E402
from p2p.plan import PlanError, plan  # noqa: E402
from p2p.repair import MAX_ROUNDS, RepairError, can_repair, needs_repair, repair  # noqa: E402
from p2p.trace import Trace  # noqa: E402

EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2
WATCHDOG_S = 570.0   # hard limit is 600 s; the soft deadline for API calls is 540 s
FAULTS = {
    # dev-only: corrupt compute_js after the build to prove the repair loop works
    "syntax": lambda js: js + "\n}}} // injected syntax error",
    "throw": lambda js: js.replace("function compute(", "function __orig(", 1)
    + "\nfunction compute(s) { throw new Error('injected fault'); }",
    "nan": lambda js: js.replace("function compute(", "function __orig(", 1)
    + "\nfunction compute(s) { var r = __orig(s); for (var k in r.outputs) "
      "{ if (typeof r.outputs[k] === 'number') r.outputs[k] = NaN; } return r; }",
}


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="agent.py",
        description="Turn a paper excerpt (case.json) into an offline interactive HTML playground.",
    )
    p.add_argument("--input", required=True, help="path to case.json")
    p.add_argument("--output", required=True, help="output directory (index.html + trace.jsonl)")
    p.add_argument("--model", required=True, help="OpenRouter MODEL_ID")
    p.add_argument("--dry-run", action="store_true",
                   help="dev: validate input and write a placeholder page; no API calls")
    p.add_argument("--no-checks", action="store_true", help="dev: skip checks and repair")
    p.add_argument("--no-repair", action="store_true", help="dev: run checks but never repair")
    p.add_argument("--stop-after", choices=["plan", "build"],
                   help="dev: stop after this stage and save its JSON in the output folder")
    p.add_argument("--save-intermediate", action="store_true",
                   help="dev: also save plan.json and build.json in the output folder")
    p.add_argument("--inject-fault", choices=sorted(FAULTS),
                   help="dev: corrupt compute_js after the build to exercise repair")
    return p.parse_args(argv)


class Best:
    """The best page so far, shared with the watchdog. Every write goes through here."""

    def __init__(self, out_dir: str, trace: Trace):
        self.out_dir, self.trace = out_dir, trace
        self.lock = threading.RLock()
        self.version = None          # dict(round, html, summary, ...)
        self.finished = False

    @staticmethod
    def key(summary: dict):
        return (summary["critical"], summary["major"], summary["minor"])

    def offer(self, v: dict) -> bool:
        with self.lock:
            if self.version is None or self.key(v["summary"]) < self.key(self.version["summary"]):
                self.version = v
                return True
            return False

    def write(self, reason: str) -> bool:
        with self.lock:
            if self.version is None:
                return False
            path = os.path.join(self.out_dir, "index.html")
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.version["html"])
            self.trace.event("output", "write_page", "ok", path="index.html", version=self.version["round"],
                             bytes=len(self.version["html"].encode("utf-8")), reason=reason)
            return True


def write_placeholder(out_dir: str, case) -> str:
    """Dev-only page proving the output path works. Fully offline."""
    path = os.path.join(out_dir, "index.html")
    body = (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>Dry run</title></head><body style=\"font-family:system-ui,sans-serif;"
        "max-width:40rem;margin:2rem auto;padding:0 1rem\">"
        "<h1>Dry run</h1><p>Input validated. No content generated.</p>"
        f"<p><b>Focus:</b> {html.escape(case.focus)}</p>"
        f"<p><b>Audience:</b> {html.escape(case.audience)}</p>"
        "</body></html>\n"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    return path


def evaluate(case, the_plan, b, trace, round_no: int) -> dict:
    """Compose -> assemble -> check -> deterministic fixes -> re-check. Logs everything."""
    spec, compute_js, notes = compose_spec(case, the_plan, b)
    for n in notes:
        trace.event("build", "normalize", "info", note=n, round=round_no)
    page = assemble(spec, compute_js)
    results, facts = run_checks(case, the_plan, spec, compute_js, page)
    if round_no == 0:
        trace.event("check", "engine", "info" if facts.get("engine") else "skip",
                    engine=facts.get("engine") or "none")
    spec2, fixes = deterministic_fixes(case, spec, facts, the_plan)
    if fixes:
        before = summarize(results)
        spec, page = spec2, assemble(spec2, compute_js)
        results, facts = run_checks(case, the_plan, spec, compute_js, page)
        for f in fixes:
            trace.revision(round_no, f["targets"], f["reason"], kind="deterministic",
                           before=before, after=summarize(results))
    for r in results:
        trace.check(r.name, None if r.status == "skip" else r.status == "pass", r.detail,
                    severity=r.severity, target=r.target, round=round_no)
    s = summarize(results)
    trace.event("check", "summary", "ok" if not (s["critical"] or s["major"]) else "fail",
                round=round_no, **s)
    return {"round": round_no, "plan": the_plan, "build": b, "spec": spec,
            "compute_js": compute_js, "html": page, "results": results, "summary": s}


def run(args, trace: Trace, budget: Budget, best: Best) -> int:
    trace.event("setup", "start", "info", model=args.model, dry_run=args.dry_run,
                python=platform.python_version())
    try:
        case = load_case(args.input)
    except CaseError as e:
        trace.event("input", "load_case", "fail", error=str(e))
        print(f"agent: bad input: {e}", file=sys.stderr)
        return EXIT_USAGE

    context = case.context_block()
    trace.event("input", "load_case", "ok", extra_fields=list(case.extra),
                has_excerpt=case.excerpt is not None, context_chars=len(context))
    for t in case.truncated:
        trace.event("input", "truncate_field", "info", **t)
    if case.excerpt is None:
        trace.event("input", "no_excerpt", "info",
                    note="grounding checks will be skipped; page will say so")

    if args.dry_run:
        path = write_placeholder(args.output, case)
        trace.event("output", "write_placeholder", "ok", path=os.path.basename(path))
        return EXIT_OK

    # ---- PLAN ---------------------------------------------------------------
    try:
        the_plan, problems = plan(case, model=args.model, budget=budget, trace=trace)
    except llm.MissingKey as e:
        trace.event("plan", "missing_key", "fail", error=str(e))
        print(f"agent: {e}", file=sys.stderr)
        return EXIT_FAIL
    except PlanError as e:
        trace.event("plan", "plan_failed", "fail", error=str(e))
        print(f"agent: {e}", file=sys.stderr)
        return EXIT_FAIL
    if args.save_intermediate or args.stop_after:
        save_json(args.output, "plan.json", the_plan)
    if args.stop_after == "plan":
        trace.event("plan", "stop_after", "info", problems=len(problems))
        return EXIT_OK

    # ---- BUILD --------------------------------------------------------------
    try:
        b = build(case, the_plan, model=args.model, budget=budget, trace=trace)
    except BuildError as e:
        trace.event("build", "build_failed", "fail", error=str(e))
        print(f"agent: {e}", file=sys.stderr)
        return EXIT_FAIL
    if args.inject_fault:
        b["compute_js"] = FAULTS[args.inject_fault](b["compute_js"])
        trace.event("build", "inject_fault", "info", fault=args.inject_fault, note="dev flag")
    if args.save_intermediate or args.stop_after:
        spec0, js0, _ = compose_spec(case, the_plan, b)
        save_json(args.output, "build.json", {"spec": spec0, "compute_js": js0})

    if args.no_checks or args.stop_after == "build":
        spec0, js0, _ = compose_spec(case, the_plan, b)
        best.offer({"round": 0, "html": assemble(spec0, js0),
                    "summary": {"critical": 0, "major": 0, "minor": 0}})
        best.write("checks disabled")
        return EXIT_OK

    # ---- CHECK + REPAIR LOOP ------------------------------------------------
    current = evaluate(case, the_plan, b, trace, 0)
    best.offer(current)
    best.write("first checked version")   # a usable page exists from here on
    for round_no in range(1, MAX_ROUNDS + 1):
        failures = needs_repair(current["results"])
        if not failures:
            trace.event("repair", "not_needed", "skip", round=round_no,
                        note="no critical or major failures")
            break
        if args.no_repair:
            trace.event("repair", "disabled", "skip", round=round_no, failures=len(failures))
            break
        if not can_repair(budget):
            trace.event("repair", "budget_stop", "skip", round=round_no, calls=budget.calls,
                        completion_left=budget.completion_left(),
                        time_left_s=round(budget.time_left(), 1))
            break
        try:
            new_plan, new_b, info = repair(failures, current["plan"], current["build"],
                                           round_no=round_no, model=args.model, budget=budget,
                                           trace=trace)
        except RepairError as e:
            trace.event("repair", "repair_failed", "fail", round=round_no, error=str(e))
            break
        for tc in info["test_changes"]:
            trace.event("repair", "test_corrected", "info", round=round_no, **tc)
        for ic in info.get("invariant_changes", []):
            trace.event("repair", "invariant_corrected", "info", round=round_no, **ic)
        nxt = evaluate(case, new_plan, new_b, trace, round_no)
        still_failing = {r.name[5:] for r in nxt["results"] if r.name.startswith("test:") and r.failed}
        rejected = [tc for tc in info["test_changes"] if tc["test"] in still_failing]
        if rejected:
            # the model claimed the test was wrong, but its corrected value does not hold either:
            # keep the original test (verified mechanically, no extra tokens)
            for tc in rejected:
                for t in new_plan.get("tests") or []:
                    if t.get("name") == tc["test"]:
                        t["expect"], t["expect_json"] = tc["before"], json.dumps(tc["before"])
                trace.event("repair", "test_correction_rejected", "info", round=round_no, test=tc["test"],
                            note="corrected expectation still fails; original test kept")
            info["changed"] = [c for c in info["changed"] if c != "plan.tests" or
                               len(rejected) < len(info["test_changes"])]
            nxt = evaluate(case, new_plan, new_b, trace, round_no)
        trace.revision(round_no, info["changed"], info["reason"], kind="model",
                       failures_sent=[f.name for f in failures], fields_sent=info["fields_sent"],
                       before=current["summary"], after=nxt["summary"])
        if best.offer(nxt):
            best.write(f"repair round {round_no} improved the page")
        current = nxt

    final = best.version
    trace.event("check", "verdict", "ok" if not final["summary"]["critical"] else "fail",
                final_version=final["round"], **final["summary"])
    if args.save_intermediate and "spec" in final:
        save_json(args.output, "final.json", {"spec": final["spec"], "compute_js": final["compute_js"]})
    return EXIT_OK if not final["summary"]["critical"] else EXIT_FAIL


def save_json(out_dir: str, name: str, data) -> None:
    with open(os.path.join(out_dir, name), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def finish(trace: Trace, budget: Budget, best: Best, code: int, note: str = "") -> None:
    with best.lock:
        if best.finished:
            return
        best.finished = True
        totals = budget.totals()
        totals["exit_code"] = code
        if best.version is not None:
            totals["final_version"] = best.version["round"]
        if note:
            totals["note"] = note
        trace.summary(totals, result="ok" if code == EXIT_OK else "fail")
        trace.close()


def start_watchdog(trace: Trace, budget: Budget, best: Best) -> threading.Timer:
    def fire():
        with best.lock:
            if best.finished:
                return
            trace.event("setup", "watchdog", "fail", note=f"{WATCHDOG_S:.0f} s reached; writing best page")
            wrote = best.write("watchdog")
            code = EXIT_OK if wrote and not best.version["summary"]["critical"] else EXIT_FAIL
            finish(trace, budget, best, code, note="stopped by watchdog")
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(code)

    timer = threading.Timer(max(1.0, WATCHDOG_S - (time.monotonic() - T0)), fire)
    timer.daemon = True
    timer.start()
    return timer


def main(argv=None) -> int:
    args = parse_args(argv)  # argparse exits 2 on bad usage
    try:
        trace = Trace(args.output, t0=T0)
    except OSError as e:
        print(f"agent: cannot create output directory: {e}", file=sys.stderr)
        return EXIT_USAGE
    budget = Budget(t0=T0)
    best = Best(args.output, trace)
    timer = start_watchdog(trace, budget, best)

    code = EXIT_FAIL
    try:
        code = run(args, trace, budget, best)
    except KeyboardInterrupt:
        trace.event("setup", "interrupted", "fail")
        code = EXIT_FAIL
    except Exception as e:  # never die without a page (if any) and a summary line
        trace.event("setup", "crash", "fail", error=f"{type(e).__name__}: {e}")
        print(f"agent: unexpected error: {type(e).__name__}: {e}", file=sys.stderr)
        best.write("after crash")
        code = EXIT_FAIL
    finally:
        timer.cancel()
        finish(trace, budget, best, code)
    return code


if __name__ == "__main__":
    sys.exit(main())
