#!/usr/bin/env python3
"""Assemble the neutral fixture into one HTML file to inspect the template.

    python tools/render_fixture.py            # -> runs/fixture/index.html
    python -m http.server -d runs/fixture 8000   # open http://localhost:8000
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from p2p.assemble import write_page  # noqa: E402

FIX = os.path.join(ROOT, "tests", "fixtures")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "runs", "fixture"))
    ap.add_argument("--break-compute", action="store_true",
                    help="inject a runtime error to see the error box")
    args = ap.parse_args()
    with open(os.path.join(FIX, "generic_spec.json"), encoding="utf-8") as f:
        spec = json.load(f)
    with open(os.path.join(FIX, "generic_compute.js"), encoding="utf-8") as f:
        js = f.read()
    if args.break_compute:
        js = js.replace("var a = state.a", "var a = state.a.does.not.exist")
    path = write_page(args.out, spec, js)
    print(f"wrote {path} ({os.path.getsize(path) / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
