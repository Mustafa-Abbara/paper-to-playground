#!/usr/bin/env python3
"""Measure a model on OpenRouter with one tiny schema-constrained request per variant.

    export OPENROUTER_API_KEY=...        # shell only, never in a file
    python tools/ping.py --model deepseek/deepseek-v4.1-flash

Variants (the `reasoning` parameter we send):
  config  = what src/p2p/config.py uses for this model
  omit    = no reasoning parameter at all (provider default)
  none    = {"effort": "none"}
  low     = {"effort": "low", "exclude": true}

Prints prompt/completion/reasoning/cached tokens, finish_reason, latency and whether
the JSON matched the schema. Use it to pick MODEL_ID and its reasoning setting.
The key is never printed. Trace goes to runs/ping/trace.jsonl.
"""
import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from p2p.budget import Budget  # noqa: E402
from p2p.config import DEFAULT_MODEL, model_settings  # noqa: E402
from p2p.llm import LLMError, chat  # noqa: E402
from p2p.trace import Trace  # noqa: E402

VARIANTS = {
    "omit": None,
    "none": {"effort": "none"},
    "low": {"effort": "low", "exclude": True},
}

SCHEMA = {
    "type": "object",
    "properties": {
        "product": {"type": "integer"},
        "entropy_bits": {"type": "number"},
        "word": {"type": "string"},
    },
    "required": ["product", "entropy_bits", "word"],
    "additionalProperties": False,
}
MESSAGES = [
    {"role": "system", "content": "Reply with JSON only, matching the schema."},
    {"role": "user", "content": "product = 17*3. entropy_bits = Shannon entropy in bits of "
                                "four equally likely outcomes. word = a synonym of 'fast'."},
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=os.environ.get("MODEL_ID", DEFAULT_MODEL))
    ap.add_argument("--variants", default="config,omit,none",
                    help="comma list from: config,omit,none,low")
    ap.add_argument("--max-tokens", type=int, default=300)
    args = ap.parse_args()

    if not os.environ.get("OPENROUTER_API_KEY"):
        print("OPENROUTER_API_KEY is not set. Run: export OPENROUTER_API_KEY=...", file=sys.stderr)
        return 1

    out = os.path.join(ROOT, "runs", "ping")
    trace = Trace(out)
    base = model_settings(args.model)
    print(f"model: {args.model}   config reasoning: {base.get('reasoning')}\n")
    hdr = f"{'variant':8} {'ok':4} {'prompt':>6} {'compl':>6} {'reason':>6} {'cached':>6} " \
          f"{'finish':8} {'secs':>5}  answer / error"
    print(hdr)
    print("-" * len(hdr))

    worst = 0
    for name in [v.strip() for v in args.variants.split(",") if v.strip()]:
        settings = dict(base)
        if name != "config":
            if name not in VARIANTS:
                print(f"{name:8} unknown variant")
                continue
            settings["reasoning"] = VARIANTS[name]
        budget = Budget()
        t = time.monotonic()
        try:
            r = chat(MESSAGES, model=args.model, max_tokens=args.max_tokens, schema=SCHEMA,
                     purpose=f"ping_{name}", budget=budget, trace=trace, settings=settings)
            u, d = r.usage, r.data or {}
            correct = d.get("product") == 51 and abs(float(d.get("entropy_bits", -1)) - 2) < 1e-9
            print(f"{name:8} {'yes' if correct else 'BAD':4} {u.get('prompt_tokens', '?'):>6} "
                  f"{u.get('completion_tokens', '?'):>6} {u.get('reasoning_tokens', 0):>6} "
                  f"{u.get('cached_tokens', 0):>6} {str(r.finish_reason):8} "
                  f"{time.monotonic() - t:5.1f}  {d}  (calls={budget.calls})")
            if not correct:
                worst = max(worst, 1)
        except LLMError as e:
            u = e.usage or {}
            print(f"{name:8} {'FAIL':4} {u.get('prompt_tokens', '-'):>6} "
                  f"{u.get('completion_tokens', '-'):>6} {u.get('reasoning_tokens', '-'):>6} "
                  f"{'-':>6} {'-':8} {time.monotonic() - t:5.1f}  "
                  f"{type(e).__name__}: {e}  (calls={budget.calls})")
            worst = 1
    trace.close()
    print("\nPick the variant with ok=yes, reasoning ~0 and the lowest completion tokens/latency;")
    print("set it in src/p2p/config.py. Trace: runs/ping/trace.jsonl")
    return worst


if __name__ == "__main__":
    sys.exit(main())
