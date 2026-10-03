"""Append-only JSONL execution trace (out/trace.jsonl).

Every line: {"ts", "t", "stage", "action", "result", ...}. ``result`` is one of
ok | fail | skip | info. Lines are flushed immediately so a crash or watchdog
exit still leaves a valid trace.

Never logged: credentials, message bodies, model reasoning. ``redact`` enforces
this on every event regardless of what the caller passes.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone

RESULTS = ("ok", "fail", "skip", "info")
SECRET_KEYS = {"authorization", "api_key", "apikey", "openrouter_api_key", "headers"}
CONTENT_KEYS = {"messages", "reasoning", "reasoning_details", "reasoning_content"}
REDACTED = "[REDACTED]"

LLM_FIELDS = (
    "call_index", "purpose", "model", "prompt_tokens", "completion_tokens",
    "reasoning_tokens", "cached_tokens", "total_tokens", "elapsed_s", "finish_reason",
    "generation_id", "http_status", "attempt", "retry_reason", "max_tokens",
    "prompt_chars", "prompt_sha256", "usage_missing", "error", "cost",
)


def _secret_values() -> list[str]:
    key = os.environ.get("OPENROUTER_API_KEY", "")
    return [key] if len(key) >= 8 else []


def redact(obj, _secrets=None):
    """Return a copy of obj with secrets and message/reasoning content removed."""
    secrets = _secret_values() if _secrets is None else _secrets
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            kl = str(k).lower()
            if kl in SECRET_KEYS or kl in CONTENT_KEYS:
                continue
            out[k] = redact(v, secrets)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact(v, secrets) for v in obj]
    if isinstance(obj, str):
        if "sk-or-" in obj or any(s in obj for s in secrets):
            return REDACTED
        return obj
    return obj


def fingerprint(text: str) -> dict:
    """Describe a prompt without logging it: length + sha256 prefix."""
    return {
        "prompt_chars": len(text),
        "prompt_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()[:12],
    }


class Trace:
    def __init__(self, out_dir: str, t0: float | None = None):
        os.makedirs(out_dir, exist_ok=True)
        self.path = os.path.join(out_dir, "trace.jsonl")
        self.t0 = time.monotonic() if t0 is None else t0
        self._f = open(self.path, "w", encoding="utf-8")  # fresh trace per run
        self.counts = {"checks_passed": 0, "checks_failed": 0, "checks_skipped": 0,
                       "revisions": 0, "failures": 0}

    def elapsed(self) -> float:
        return round(time.monotonic() - self.t0, 3)

    def event(self, stage: str, action: str, result: str = "info", **extra) -> dict:
        if result not in RESULTS:
            result = "info"
        if result == "fail":
            self.counts["failures"] += 1
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "t": self.elapsed(),
            "stage": stage,
            "action": action,
            "result": result,
        }
        rec.update(redact(extra))
        if not self._f.closed:
            self._f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            self._f.flush()
        return rec

    def llm_call(self, *, ok: bool, stage: str = "llm", **fields) -> dict:
        data = {k: fields[k] for k in LLM_FIELDS if k in fields}
        action = f"call:{fields.get('purpose', 'unknown')}"
        return self.event(stage, action, "ok" if ok else "fail", **data)

    def check(self, name: str, passed: bool | None, detail: str = "", stage: str = "check",
              **extra) -> dict:
        """passed=None means the check could not run (recorded as skip, never as pass)."""
        if passed is None:
            result = "skip"
            self.counts["checks_skipped"] += 1
        elif passed:
            result = "ok"
            self.counts["checks_passed"] += 1
        else:
            result = "fail"
            self.counts["checks_failed"] += 1
        return self.event(stage, f"check:{name}", result, detail=detail, **extra)

    def revision(self, round_no: int, targets: list[str], reason: str, **extra) -> dict:
        self.counts["revisions"] += 1
        return self.event("repair", "revision", "info", round=round_no, targets=targets,
                          reason=reason, **extra)

    def summary(self, totals: dict, result: str = "info") -> dict:
        data = dict(self.counts)
        data.update(totals)
        data.setdefault("wall_s", self.elapsed())
        return self.event("summary", "final", result, **data)

    def close(self):
        if not self._f.closed:
            self._f.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False
