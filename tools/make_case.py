#!/usr/bin/env python3
"""Build cases/<name>.json from cases/src/<name>.json + cases/src/<name>.excerpt.txt.

Paste the paper passage, as plain text, into the .excerpt.txt file (no JSON escaping
needed), then run:

    python tools/make_case.py            # builds every case that has an excerpt file
    python tools/make_case.py a_attention
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "cases", "src")
OUT = os.path.join(ROOT, "cases")


def build(name: str) -> bool:
    meta_path = os.path.join(SRC, f"{name}.json")
    ex_path = os.path.join(SRC, f"{name}.excerpt.txt")
    with open(meta_path, encoding="utf-8") as f:
        case = json.load(f)
    with open(ex_path, encoding="utf-8") as f:
        excerpt = f.read().strip()
    if not excerpt or excerpt.startswith("PASTE THE EXCERPT HERE"):
        print(f"{name}: skipped - paste the excerpt into {os.path.relpath(ex_path, ROOT)} first")
        return False
    # Excerpt first, as the hidden cases are described ("each contains an excerpt").
    out = {"source_url": case["source_url"], "title": case.get("title", ""),
           "section": case.get("section", ""), "excerpt": excerpt,
           "focus": case["focus"], "audience": case["audience"]}
    path = os.path.join(OUT, f"{name}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"{name}: wrote {os.path.relpath(path, ROOT)} ({len(excerpt)} excerpt characters)")
    return True


def main() -> int:
    names = sys.argv[1:] or sorted(n[:-len(".excerpt.txt")] for n in os.listdir(SRC)
                                   if n.endswith(".excerpt.txt"))
    ok = [build(n) for n in names]
    return 0 if all(ok) else 1


if __name__ == "__main__":
    sys.exit(main())
