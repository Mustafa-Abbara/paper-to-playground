"""Per-case budget guard.

Hard limits from the brief: 10 API requests (incl. retries), 30,000 completion
tokens, 10 minutes. We plan against stricter soft caps so a final call or the
assembly step can never push us over a hard limit.

Every HTTP attempt counts as a call, including retries and failed attempts.
"""
from __future__ import annotations

import time


class Budget:
    def __init__(self, max_calls: int = 10, max_completion: int = 30_000,
                 deadline_s: float = 600.0, soft_calls: int = 8,
                 soft_completion: int = 26_000, soft_deadline_s: float = 540.0,
                 t0: float | None = None):
        self.max_calls = max_calls
        self.max_completion = max_completion
        self.deadline_s = deadline_s
        self.soft_calls = min(soft_calls, max_calls)
        self.soft_completion = min(soft_completion, max_completion)
        self.soft_deadline_s = min(soft_deadline_s, deadline_s)
        self.t0 = time.monotonic() if t0 is None else t0

        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.reasoning_tokens = 0
        self.cached_tokens = 0
        self.usage_missing = 0

    # --- time -----------------------------------------------------------
    def elapsed(self) -> float:
        return time.monotonic() - self.t0

    def time_left(self) -> float:
        """Seconds left before the SOFT deadline (can be negative)."""
        return self.soft_deadline_s - self.elapsed()

    # --- tokens / calls -------------------------------------------------
    def completion_left(self) -> int:
        return max(0, self.soft_completion - self.completion_tokens)

    def calls_left(self) -> int:
        return max(0, self.soft_calls - self.calls)

    def clamp_max_tokens(self, requested: int) -> int:
        return max(0, min(int(requested), self.completion_left()))

    def can_call(self, min_tokens: int = 256, min_time_s: float = 15.0) -> bool:
        """True if one more request fits: a call slot is free, at least
        ``min_tokens`` completion tokens remain, and ``min_time_s`` seconds remain."""
        return (self.calls_left() > 0
                and self.completion_left() >= min_tokens
                and self.time_left() >= min_time_s)

    def record(self, usage: dict | None) -> None:
        """Count one HTTP attempt. ``usage`` is the parsed usage dict
        (prompt_tokens, completion_tokens, reasoning_tokens, cached_tokens) or
        None when the attempt returned no usage (e.g. network error)."""
        self.calls += 1
        if not usage:
            self.usage_missing += 1
            return
        self.prompt_tokens += int(usage.get("prompt_tokens") or 0)
        self.completion_tokens += int(usage.get("completion_tokens") or 0)
        self.reasoning_tokens += int(usage.get("reasoning_tokens") or 0)
        self.cached_tokens += int(usage.get("cached_tokens") or 0)

    def hard_exceeded(self) -> bool:
        return (self.calls > self.max_calls
                or self.completion_tokens > self.max_completion
                or self.elapsed() > self.deadline_s)

    def totals(self) -> dict:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cached_tokens": self.cached_tokens,
            # reasoning is already inside completion_tokens; cached inside prompt_tokens
            "total_tokens": self.prompt_tokens + self.completion_tokens,
            "usage_missing_calls": self.usage_missing,
            "wall_s": round(self.elapsed(), 3),
        }
