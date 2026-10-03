#!/usr/bin/env python3
"""Run the frozen agent on every practice case, several times, like the assessment does.

    python tools/harness.py                     # every cases/*.json, 2 runs each
    python tools/harness.py --runs 1 --cases a_attention b_entropy
    python tools/harness.py --jobs 3            # run cases in parallel (faster, same results)

Each run: a fresh output folder, a subprocess with a clean environment (only PATH, HOME
and OPENROUTER_API_KEY), the exact CLI, and a 600 s timeout. Results are read back from
trace.jsonl. Prints a markdown table and saves runs/harness/summary.json + summary.md.
Flags any run with calls > 8, tokens > 20k, time > 300 s, exit != 0, or a critical failure.
"""
import argparse
import concurrent.futures as cf
import glob
import json
import os
import shutil
import statistics
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
from p2p.checks import network_findings  # noqa: E402

DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash"
FLAGS = {"calls": 8, "total_tokens": 20_000, "wall_s": 300}


def one_run(case_path: str, out_dir: str, model: str) -> dict:
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)
    env = {k: os.environ[k] for k in ("PATH", "HOME", "OPENROUTER_API_KEY", "SYSTEMROOT") if k in os.environ}
    t = time.monotonic()
    try:
        p = subprocess.run([sys.executable, os.path.join(ROOT, "agent.py"), "--input", case_path,
                            "--output", out_dir, "--model", model],
                           capture_output=True, text=True, timeout=600, env=env, cwd=ROOT)
        code, err = p.returncode, p.stderr.strip()[-300:]
    except subprocess.TimeoutExpired:
        code, err = "timeout", "killed after 600 s"
    wall = time.monotonic() - t
    row = {"case": os.path.basename(case_path)[:-5], "out": os.path.relpath(out_dir, ROOT),
           "exit": code, "wall_s": round(wall, 1), "stderr": err}
    trace = os.path.join(out_dir, "trace.jsonl")
    recs = []
    if os.path.exists(trace):
        recs = [json.loads(l) for l in open(trace, encoding="utf-8") if l.strip()]
    s = recs[-1] if recs and recs[-1].get("stage") == "summary" else {}
    verdict = next((r for r in reversed(recs) if r.get("action") == "verdict"), {})
    row.update({k: s.get(k) for k in ("calls", "prompt_tokens", "completion_tokens", "reasoning_tokens",
                                      "total_tokens")})
    row.update({"critical": verdict.get("critical"), "major": verdict.get("major"),
                "minor": verdict.get("minor"), "final_version": verdict.get("final_version"),
                "repairs": sum(1 for r in recs if r.get("action") == "revision" and r.get("kind") == "model"),
                "fixes": sum(1 for r in recs if r.get("action") == "revision" and r.get("kind") == "deterministic"),
                "usage_missing": s.get("usage_missing_calls")})
    page = os.path.join(out_dir, "index.html")
    if os.path.exists(page):
        html = open(page, encoding="utf-8").read()
        src = json.load(open(case_path, encoding="utf-8")).get("source_url", "")
        found, stray = network_findings(html, src)
        row["html_kb"] = round(len(html.encode("utf-8")) / 1024)
        row["offline"] = "clean" if not (found or stray) else "; ".join(found + stray)[:80]
    else:
        row["html_kb"], row["offline"] = None, "NO PAGE"
    row["flags"] = flags(row)
    return row


def flags(r: dict) -> list[str]:
    out = []
    if r["exit"] != 0:
        out.append(f"exit={r['exit']}")
    for k, lim in FLAGS.items():
        if r.get(k) is not None and r[k] > lim:
            out.append(f"{k}>{lim}")
    if r.get("critical"):
        out.append("critical")
    if r.get("offline") != "clean":
        out.append("offline")
    if r.get("usage_missing"):
        out.append("usage_missing")
    return out


def table(rows: list[dict]) -> str:
    cols = ["case", "run", "exit", "calls", "total_tokens", "prompt_tokens", "completion_tokens",
            "wall_s", "critical", "major", "minor", "repairs", "fixes", "html_kb", "offline", "flags"]
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in rows:
        lines.append("| " + " | ".join(str(r.get(c) if c != "flags" else ",".join(r["flags"]) or "-")
                                       for c in cols) + " |")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=os.environ.get("MODEL_ID", DEFAULT_MODEL))
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--cases", nargs="*", help="case names (default: all cases/*.json)")
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--out", default=os.path.join(ROOT, "runs", "harness"))
    a = ap.parse_args()
    if not os.environ.get("OPENROUTER_API_KEY"):
        print("OPENROUTER_API_KEY is not set", file=sys.stderr)
        return 2
    paths = sorted(glob.glob(os.path.join(ROOT, "cases", "*.json")))
    if a.cases:
        paths = [p for p in paths if os.path.basename(p)[:-5] in a.cases]
    jobs = [(p, os.path.join(a.out, os.path.basename(p)[:-5], f"run{k + 1}"), k + 1)
            for p in paths for k in range(a.runs)]
    print(f"{len(paths)} cases x {a.runs} runs = {len(jobs)} runs, model {a.model}, jobs={a.jobs}\n")
    rows = []
    with cf.ThreadPoolExecutor(max_workers=max(1, a.jobs)) as ex:
        futs = {ex.submit(one_run, p, out, a.model): k for p, out, k in jobs}
        for f in cf.as_completed(futs):
            r = f.result()
            r["run"] = futs[f]
            rows.append(r)
            print(f"  done {r['case']} run{r['run']}: exit={r['exit']} tokens={r['total_tokens']} "
                  f"time={r['wall_s']}s crit/major/minor={r['critical']}/{r['major']}/{r['minor']} "
                  f"{'FLAGS ' + ','.join(r['flags']) if r['flags'] else ''}")
    rows.sort(key=lambda r: (r["case"], r["run"]))
    print("\n" + table(rows))

    print("\nPER CASE (mean over runs)")
    per_case = {}
    for c in sorted({r["case"] for r in rows}):
        rs = [r for r in rows if r["case"] == c]
        m = lambda k: round(statistics.mean(r[k] for r in rs if r.get(k) is not None), 1) \
            if any(r.get(k) is not None for r in rs) else None
        per_case[c] = {"tokens": m("total_tokens"), "wall_s": m("wall_s"), "calls": m("calls"),
                       "all_exit_0": all(r["exit"] == 0 for r in rs)}
        print(f"  {c:16} tokens={per_case[c]['tokens']}  time={per_case[c]['wall_s']}s  "
              f"calls={per_case[c]['calls']}  all exit 0: {per_case[c]['all_exit_0']}")
    tok = [r["total_tokens"] for r in rows if r.get("total_tokens")]
    wall = [r["wall_s"] for r in rows]
    overall = {"runs": len(rows), "exit_0": sum(1 for r in rows if r["exit"] == 0),
               "median_tokens": statistics.median(tok) if tok else None,
               "median_wall_s": statistics.median(wall) if wall else None,
               "flagged": sum(1 for r in rows if r["flags"])}
    print(f"\nOVERALL {overall}")
    os.makedirs(a.out, exist_ok=True)
    json.dump({"model": a.model, "rows": rows, "per_case": per_case, "overall": overall},
              open(os.path.join(a.out, "summary.json"), "w"), indent=1)
    open(os.path.join(a.out, "summary.md"), "w").write(table(rows) + "\n")
    return 0 if overall["flagged"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
