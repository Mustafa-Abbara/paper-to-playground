#!/usr/bin/env python3
"""Paper to Playground agent — entry point.

    python agent.py --input case.json --output out --model <MODEL_ID>

Writes <output>/index.html and <output>/trace.jsonl.
Exit codes: 0 success, 1 generation failed, 2 bad input/usage.
"""
import time

T0 = time.monotonic()  # latency is measured from process start

import argparse  # noqa: E402
import html  # noqa: E402
import os  # noqa: E402
import platform  # noqa: E402
import sys  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

import json  # noqa: E402

from p2p import llm  # noqa: E402
from p2p.assemble import write_page  # noqa: E402
from p2p.budget import Budget  # noqa: E402
from p2p.build import BuildError, build, compose_spec  # noqa: E402
from p2p.case import CaseError, load_case  # noqa: E402
from p2p.plan import PlanError, plan  # noqa: E402
from p2p.trace import Trace  # noqa: E402

EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2


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
    p.add_argument("--stop-after", choices=["plan", "build"],
                   help="dev: stop after this stage and save its JSON in the output folder")
    p.add_argument("--save-intermediate", action="store_true",
                   help="dev: also save plan.json (and later build.json) in the output folder")
    return p.parse_args(argv)


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


def run(args, trace: Trace, budget: Budget) -> int:
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
    spec, compute_js, notes = compose_spec(case, the_plan, b)
    for n in notes:
        trace.event("build", "normalize", "info", note=n)
    if args.save_intermediate or args.stop_after:
        save_json(args.output, "build.json", {"spec": spec, "compute_js": compute_js})

    # ---- ASSEMBLE -----------------------------------------------------------
    path = write_page(args.output, spec, compute_js)
    trace.event("output", "write_page", "ok", path=os.path.basename(path),
                bytes=os.path.getsize(path))
    return EXIT_OK


def save_json(out_dir: str, name: str, data) -> None:
    with open(os.path.join(out_dir, name), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def main(argv=None) -> int:
    args = parse_args(argv)  # argparse exits 2 on bad usage
    try:
        trace = Trace(args.output, t0=T0)
    except OSError as e:
        print(f"agent: cannot create output directory: {e}", file=sys.stderr)
        return EXIT_USAGE
    budget = Budget(t0=T0)

    code = EXIT_FAIL
    try:
        code = run(args, trace, budget)
    except KeyboardInterrupt:
        trace.event("setup", "interrupted", "fail")
        code = EXIT_FAIL
    except Exception as e:  # never die without a summary line
        trace.event("setup", "crash", "fail", error=f"{type(e).__name__}: {e}")
        print(f"agent: unexpected error: {type(e).__name__}: {e}", file=sys.stderr)
        code = EXIT_FAIL
    finally:
        totals = budget.totals()
        totals["exit_code"] = code
        trace.summary(totals, result="ok" if code == EXIT_OK else "fail")
        trace.close()
    return code


if __name__ == "__main__":
    sys.exit(main())
