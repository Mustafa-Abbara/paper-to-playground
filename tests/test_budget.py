import time

from p2p.budget import Budget


def test_clamp_and_record():
    b = Budget()
    assert b.clamp_max_tokens(7000) == 7000
    b.record({"prompt_tokens": 1000, "completion_tokens": 24000, "reasoning_tokens": 300,
              "cached_tokens": 50})
    assert b.completion_left() == 2000
    assert b.clamp_max_tokens(7000) == 2000
    assert b.can_call(min_tokens=1500) and not b.can_call(min_tokens=2500)
    t = b.totals()
    assert t["total_tokens"] == 25000 and t["reasoning_tokens"] == 300 and t["calls"] == 1


def test_every_attempt_counts_even_without_usage():
    b = Budget()
    for _ in range(8):
        b.record(None)
    assert b.calls == 8 and b.usage_missing == 8
    assert not b.can_call()          # soft cap of 8 calls reached
    assert not b.hard_exceeded()     # still within the hard cap of 10


def test_deadline():
    b = Budget(t0=time.monotonic() - 1000)  # pretend the process started 1000 s ago
    assert b.time_left() < 0
    assert not b.can_call()
    assert b.hard_exceeded()


def test_soft_caps_never_exceed_hard():
    b = Budget(max_calls=5, soft_calls=8, max_completion=1000, soft_completion=5000)
    assert b.soft_calls == 5 and b.soft_completion == 1000
