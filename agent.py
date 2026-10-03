#!/usr/bin/env python3
"""Paper to Playground agent — entry point.

Usage:
    python agent.py --input case.json --output out --model <MODEL_ID>

Stage 0 stub: parses the CLI and exits with code 2 ("not implemented").
"""
import argparse
import sys


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="agent.py",
        description="Turn a paper excerpt (case.json) into an offline interactive HTML playground.",
    )
    p.add_argument("--input", required=True, help="path to case.json")
    p.add_argument("--output", required=True, help="output directory (index.html + trace.jsonl)")
    p.add_argument("--model", required=True, help="OpenRouter MODEL_ID")
    return p.parse_args(argv)


def main(argv=None) -> int:
    parse_args(argv)
    print("not implemented")
    return 2


if __name__ == "__main__":
    sys.exit(main())
