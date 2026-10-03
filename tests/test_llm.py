"""Client tests with a fake HTTP session: no network, no key needed."""
import json

import pytest
import requests

from p2p import llm
from p2p.budget import Budget
from p2p.trace import Trace

FAKE_KEY = "sk-or-v1-" + "testtesttesttest"
MSGS = [{"role": "user", "content": "hello secret prompt"}]
SCHEMA = {"type": "object", "properties": {"x": {"type": "integer"}},
          "required": ["x"], "additionalProperties": False}


class Resp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeSession:
    def __init__(self, items):
        self.items, self.payloads, self.timeouts = list(items), [], []

    def post(self, url, headers, json, timeout):
        assert url == llm.URL and headers["Authorization"] == f"Bearer {FAKE_KEY}"
        self.payloads.append(json)
        self.timeouts.append(timeout)
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def ok_body(content='{"x": 1}', finish="stop", completion=20, reasoning=0, usage=True):
    b = {"id": "gen-123", "choices": [{"message": {"content": content},
                                       "finish_reason": finish}]}
    if usage:
        b["usage"] = {"prompt_tokens": 100, "completion_tokens": completion,
                      "total_tokens": 100 + completion,
                      "completion_tokens_details": {"reasoning_tokens": reasoning},
                      "prompt_tokens_details": {"cached_tokens": 7}}
    return b


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    tr = Trace(str(tmp_path))
    sleeps = []
    return tr, Budget(), sleeps


def call(env, items, **kw):
    tr, budget, sleeps = env
    sess = FakeSession(items)
    kw.setdefault("schema", SCHEMA)
    kw.setdefault("max_tokens", 1000)
    res = llm.chat(MSGS, model="deepseek/deepseek-v4.1-flash", purpose="plan", budget=budget,
                   trace=tr, session=sess, sleep=sleeps.append, **kw)
    return res, sess


def trace_lines(tr):
    tr.close()
    return [json.loads(l) for l in open(tr.path, encoding="utf-8")]


def test_success_parses_usage_and_json(env):
    res, sess = call(env, [Resp(200, ok_body())])
    tr, budget, _ = env
    assert res.data == {"x": 1} and res.generation_id == "gen-123"
    assert res.usage == {"prompt_tokens": 100, "completion_tokens": 20, "reasoning_tokens": 0,
                         "cached_tokens": 7, "total_tokens": 120}
    assert budget.calls == 1 and budget.completion_tokens == 20
    p = sess.payloads[0]
    assert p["response_format"]["type"] == "json_schema"
    assert p["response_format"]["json_schema"]["strict"] is True
    assert p["provider"]["require_parameters"] is True
    assert p["reasoning"] == {"effort": "none"}
    assert "usage" not in p and p["stream"] is False
    rec = [r for r in trace_lines(tr) if r["action"] == "call:plan"][0]
    assert rec["prompt_tokens"] == 100 and rec["completion_tokens"] == 20
    assert rec["generation_id"] == "gen-123" and rec["result"] == "ok"
    raw = open(tr.path).read()
    assert "secret prompt" not in raw and FAKE_KEY not in raw and "sk-or-" not in raw


def test_401_not_retried(env):
    with pytest.raises(llm.APIError) as ei:
        call(env, [Resp(401, {"error": {"code": 401, "message": "bad key"}})])
    assert ei.value.status == 401 and env[1].calls == 1 and env[2] == []


@pytest.mark.parametrize("status", [400, 402, 403])
def test_non_transient_not_retried(env, status):
    with pytest.raises(llm.APIError):
        call(env, [Resp(status, {"error": {"code": status, "message": "x"}})])
    assert env[1].calls == 1


def test_429_retried_at_most_twice_honouring_retry_after(env):
    r = Resp(429, {"error": {"code": 429, "message": "slow down"}}, {"Retry-After": "60"})
    with pytest.raises(llm.APIError):
        call(env, [r, r, r, Resp(200, ok_body())])
    tr, budget, sleeps = env
    assert budget.calls == 3                 # 1 try + 2 retries, never a 4th
    assert sleeps == [20.0, 20.0]            # Retry-After capped at 20 s
    fails = [x for x in trace_lines(tr) if x["action"] == "call:plan"]
    assert len(fails) == 3 and all(x["result"] == "fail" for x in fails)
    assert [x["attempt"] for x in fails] == [1, 2, 3]


def test_retry_then_success_both_counted(env):
    res, _ = call(env, [Resp(503, None), Resp(200, ok_body())])
    tr, budget, sleeps = env
    assert res.data == {"x": 1} and res.attempts == 2
    assert budget.calls == 2 and sleeps == [2.0]
    assert budget.usage_missing == 1         # the failed attempt had no usage


def test_error_inside_http_200(env):
    bad = Resp(200, {"error": {"code": 502, "message": "provider down"}})
    res, _ = call(env, [bad, Resp(200, ok_body())])
    assert res.data == {"x": 1} and env[1].calls == 2


def test_timeout_retried_and_counted(env):
    res, _ = call(env, [requests.Timeout("slow"), Resp(200, ok_body())])
    assert res.data == {"x": 1} and env[1].calls == 2 and env[2] == [2.0]


def test_reasoning_starved(env):
    body = ok_body(content="", finish="length", completion=1000, reasoning=990)
    with pytest.raises(llm.ReasoningStarved) as ei:
        call(env, [Resp(200, body)])
    assert ei.value.usage["reasoning_tokens"] == 990


def test_truncated(env):
    body = ok_body(content='{"x": ', finish="length", completion=1000, reasoning=0)
    with pytest.raises(llm.Truncated) as ei:
        call(env, [Resp(200, body)])
    assert not isinstance(ei.value, llm.ReasoningStarved)
    assert ei.value.text == '{"x": '


def test_fenced_json_and_bad_json(env):
    res, _ = call(env, [Resp(200, ok_body(content='```json\n{"x": 5}\n```'))])
    assert res.data == {"x": 5}
    with pytest.raises(llm.BadJSON) as ei:
        call(env, [Resp(200, ok_body(content="not json at all"))])
    assert ei.value.usage["completion_tokens"] == 20


def test_missing_usage_is_not_invented(env):
    res, _ = call(env, [Resp(200, ok_body(usage=False))])
    tr, budget, _ = env
    assert res.usage == {} and budget.completion_tokens == 0 and budget.usage_missing == 1
    rec = [r for r in trace_lines(tr) if r["action"] == "call:plan"][0]
    assert rec["usage_missing"] is True and "completion_tokens" not in rec


def test_max_tokens_clamped_to_budget(env):
    tr, budget, _ = env
    budget.record({"completion_tokens": 25_000})
    _, sess = call(env, [Resp(200, ok_body())], max_tokens=7000)
    assert sess.payloads[0]["max_tokens"] == 1000


def test_budget_exhausted_sends_nothing(env):
    tr, budget, _ = env
    for _ in range(8):
        budget.record(None)
    sess = FakeSession([Resp(200, ok_body())])
    with pytest.raises(llm.BudgetExhausted):
        llm.chat(MSGS, model="m", max_tokens=100, purpose="p", budget=budget, trace=tr,
                 session=sess, sleep=lambda s: None)
    assert sess.payloads == []


def test_missing_key(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    sess = FakeSession([])
    with pytest.raises(llm.MissingKey):
        llm.chat(MSGS, model="m", max_tokens=10, purpose="p", budget=Budget(),
                 trace=Trace(str(tmp_path)), session=sess)
    assert sess.payloads == []


def test_unknown_model_omits_reasoning_and_plain_text_mode():
    p = llm.build_payload(MSGS, model="some/other-model", max_tokens=50)
    assert "reasoning" not in p and "response_format" not in p and "plugins" not in p
