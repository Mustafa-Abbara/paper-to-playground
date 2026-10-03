#!/usr/bin/env python3
"""Static offline scan of any generated page (same rules as the agent's own check).

    python tools/offline_scan.py out/index.html [--source-url URL]

Exit 0 = no network APIs and no URLs other than the SVG namespace / the source URL.
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from p2p.checks import network_findings  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("html")
    ap.add_argument("--source-url", default=None, help="default: read meta.source_url from the page")
    a = ap.parse_args()
    page = open(a.html, encoding="utf-8").read()
    src = a.source_url
    if src is None:
        m = re.search(r'<script type="application/json" id="spec">(.*?)</script>', page, re.S)
        try:
            src = json.loads(m.group(1).replace("<\\/", "</"))["meta"]["source_url"] if m else ""
        except (ValueError, KeyError, TypeError):
            src = ""
    found, stray = network_findings(page, src or "")
    for f in found:
        print(f"NETWORK: {f}")
    for u in stray:
        print(f"URL: {u}")
    size = len(page.encode("utf-8"))
    print(f"{'CLEAN' if not (found or stray) else 'FINDINGS'}: {a.html} ({size / 1024:.0f} KB)")
    return 0 if not (found or stray) else 1


if __name__ == "__main__":
    sys.exit(main())
