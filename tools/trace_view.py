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
    for r in recs:
        if r["stage"] != "summary" and (r["action"].startswith("check:") or (r["result"] == "fail" and not r["action"].startswith("call:"))):
            mark = {"ok": "PASS", "fail": "FAIL", "skip": "skip"}.get(r["result"], r["result"])
            print(f"  {mark:4} {r['stage']}/{r['action']}: {str(r.get('detail') or r.get('error') or '')[:160]}")

    plan_path = os.path.join(folder, "plan.json") if os.path.isdir(folder) else None
    if plan_path and os.path.exists(plan_path):
        p = json.load(open(plan_path, encoding="utf-8"))
        src = p.get("source", {})
        print("\nPLAN")
        print(f"  concept:   {p.get('concept')}")
        print(f"  source:    {src.get('paper_title')} | {src.get('section_label')} | "
              f"{src.get('equation_label')}")
        print(f"  equation:  {src.get('equation_text')}")
        print(f"  inputs:    " + ", ".join(f"{s['id']}={s.get('default_json')}" for s in p.get("state", [])))
        print(f"  outputs:   " + ", ".join(o["key"] for o in p.get("outputs", [])))
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
