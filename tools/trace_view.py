#!/usr/bin/env python3
"""Readable summary of a run: model calls, checks, plan, totals. Never prints secrets
(the trace contains none).

    python tools/trace_view.py runs/a            # folder with trace.jsonl (+ plan.json)
"""
import json
import os
import sys


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    folder = sys.argv[1]
    path = folder if folder.endswith(".jsonl") else os.path.join(folder, "trace.jsonl")
    recs = [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]

    print("MODEL CALLS")
    print(f"  {'#':>2} {'purpose':10} {'ok':4} {'prompt':>6} {'compl':>6} {'reason':>6} "
          f"{'cached':>6} {'secs':>6} finish / error")
    for r in recs:
        if r["action"].startswith("call:"):
            print(f"  {r.get('call_index', '?'):>2} {r['action'][5:]:10} "
                  f"{'yes' if r['result'] == 'ok' else 'NO':4} {r.get('prompt_tokens', '-'):>6} "
                  f"{r.get('completion_tokens', '-'):>6} {r.get('reasoning_tokens', '-'):>6} "
                  f"{r.get('cached_tokens', '-'):>6} {r.get('elapsed_s', '-'):>6} "
                  f"{r.get('finish_reason') or r.get('error') or ''}")

    print("\nCHECKS AND FAILURES")
    rounds = sorted({r.get("round", 0) for r in recs if r["action"].startswith("check:")})
    for rd in rounds:
        n = sum(1 for r in recs if r["action"].startswith("check:") and r["result"] == "ok"
                and r.get("round", 0) == rd)
        print(f"  round {rd}: {n} checks passed")
    for r in recs:
        if r["stage"] == "summary" or r["action"] == "summary" or r["result"] == "ok" or r["action"].startswith("call:"):
            continue
        if r["action"].startswith("check:") or r["result"] == "fail":
            mark = {"fail": "FAIL", "skip": "skip"}.get(r["result"], r["result"])
            sev = f"[{r['severity']}]" if r.get("severity") else ""
            sev = (f"r{r['round']} " if "round" in r else "") + sev
            print(f"  {mark:4} {sev:10} {r['stage']}/{r['action']}: {str(r.get('detail') or r.get('error') or '')[:150]}")
    for r in recs:
        if r["action"] == "revision":
            ba = ""
            if r.get("before") and r.get("after"):
                b, a = r["before"], r["after"]
                ba = f"  [crit/major/minor {b['critical']}/{b['major']}/{b['minor']} -> {a['critical']}/{a['major']}/{a['minor']}]"
            print(f"  REVISION r{r.get('round')} ({r.get('kind', 'model')}): {r.get('targets')} - {r.get('reason')}{ba}")
        if r["action"] == "test_corrected":
            print(f"  TEST CORRECTED {r.get('test')}: {r.get('before')} -> {r.get('after')} because {r.get('rationale')}")
        if r["action"] == "verdict":
            print(f"  VERDICT (version {r.get('final_version')}): critical={r.get('critical')} major={r.get('major')} minor={r.get('minor')}")

    plan_path = os.path.join(folder, "plan.json") if os.path.isdir(folder) else None
    if plan_path and os.path.exists(plan_path):
        p = json.load(open(plan_path, encoding="utf-8"))
        src = p.get("source", {})
        print("\nPLAN")
        print(f"  concept:   {p.get('concept')}")
        print(f"  source:    {src.get('paper_title')} | {src.get('section_label')} | "
              f"{src.get('equation_label')}")
        print(f"  equation:  {src.get('equation_text')}")
        print("  inputs:    " + ", ".join(f"{s['id']}={s.get('default_json')}" for s in p.get("state", [])))
        print("  outputs:   " + ", ".join(o["key"] for o in p.get("outputs", [])))
        print(f"  quotes:    {len(p.get('grounding_quotes', []))}")
        print("  tests:")
        for t in p.get("tests", []):
            print(f"    - {t.get('name')}: inputs {t.get('inputs_json')} -> expect {t.get('expect_json')}")
        print("  invariants: " + "; ".join(i.get("js", "") for i in p.get("invariants", [])))
        print(f"  limitation ({p.get('limitation', {}).get('kind')}): {p.get('limitation', {}).get('text')}")

    s = recs[-1] if recs and recs[-1]["stage"] == "summary" else {}
    print("\nTOTALS")
    print(f"  calls={s.get('calls')} prompt={s.get('prompt_tokens')} completion={s.get('completion_tokens')}"
          f" reasoning={s.get('reasoning_tokens')} total={s.get('total_tokens')}"
          f" wall={s.get('wall_s')}s exit={s.get('exit_code')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
