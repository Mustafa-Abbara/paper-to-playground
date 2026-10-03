"""OpenRouter chat client with honest usage accounting and bounded retries.

One function, ``chat()``, is the ONLY place the program talks to the network.
Guarantees:
  * never exceeds the Budget (calls, completion tokens, time) - it checks before
    every attempt and clamps max_tokens to what is left;
  * every HTTP attempt (including retries and failures) is counted in the Budget
    and written to the trace with its real usage numbers (never invented);
  * retries only transient errors (408/429/502/503, timeouts), at most 2 times;
  * never logs the key, the prompt text, or the model's reasoning.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field

import requests

from .config import model_settings
from .trace import fingerprint

URL = "https://openrouter.ai/api/v1/chat/completions"
RETRYABLE = {408, 429, 502, 503}
MAX_RETRIES = 2
BACKOFF_S = (2.0, 5.0)
RETRY_AFTER_CAP_S = 20.0
REQUEST_TIMEOUT_CAP_S = 150.0
DEADLINE_MARGIN_S = 15.0
MIN_TOKENS = 64


# --- errors ---------------------------------------------------------------
class LLMError(Exception):
    """Base class. ``usage`` holds the last attempt's usage (if any)."""

    def __init__(self, msg: str, *, usage: dict | None = None, text: str = ""):
        super().__init__(msg)
        self.usage = usage
        self.text = text


class MissingKey(LLMError):
    pass


class BudgetExhausted(LLMError):
    pass


class APIError(LLMError):
    def __init__(self, msg: str, *, status: int | None = None, **kw):
        super().__init__(msg, **kw)
        self.status = status


class Truncated(LLMError):
    """finish_reason == 'length' with real output: caller may retry more concisely."""


class ReasoningStarved(Truncated):
    """finish_reason == 'length' and (almost) all tokens went to hidden reasoning."""


class BadJSON(LLMError):
    pass


@dataclass
class LLMResult:
    text: str
    data: object = None            # parsed JSON when a schema was requested
    usage: dict = field(default_factory=dict)
    finish_reason: str | None = None
    generation_id: str | None = None
    elapsed_s: float = 0.0
    attempts: int = 1


# --- helpers --------------------------------------------------------------
def get_api_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        raise MissingKey("OPENROUTER_API_KEY is not set in the environment")
    return key


def parse_usage(body: dict | None) -> dict | None:
    """Extract usage exactly as reported. Returns None if the response has none."""
    u = (body or {}).get("usage")
    if not isinstance(u, dict):
        return None
    cdet = u.get("completion_tokens_details") or {}
    pdet = u.get("prompt_tokens_details") or {}
    out = {
        "prompt_tokens": int(u.get("prompt_tokens") or 0),
        "completion_tokens": int(u.get("completion_tokens") or 0),
        "reasoning_tokens": int(cdet.get("reasoning_tokens") or 0),
        "cached_tokens": int(pdet.get("cached_tokens") or 0),
    }
    out["total_tokens"] = int(u.get("total_tokens") or
                              out["prompt_tokens"] + out["completion_tokens"])
    if u.get("cost") is not None:
        out["cost"] = u["cost"]
    return out


_FENCE = re.compile(r"^\s*```(?:json|JSON)?\s*\n?(.*?)\n?\s*```\s*$", re.S)


def parse_json(text: str):
    """json.loads after stripping ``` fences; falls back to the outermost {...}."""
    s = (text or "").strip()
    m = _FENCE.match(s)
    if m:
        s = m.group(1).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        i, j = s.find("{"), s.rfind("}")
        if 0 <= i < j:
            try:
                return json.loads(s[i:j + 1])
            except json.JSONDecodeError:
                pass
    raise BadJSON("model output is not valid JSON", text=text)


# Models for which a provider rejected an optional parameter: send a plain request instead
# (no reasoning setting, no provider requirement, no plugin; response_format is kept).
_RELAXED: set[str] = set()


def build_payload(messages, *, model, max_tokens, schema=None, purpose="call",
                  settings=None, strict=True) -> dict:
    s = dict(settings if settings is not None else model_settings(model))
    if model in _RELAXED:
        s.update(reasoning=None, require_parameters=False, response_healing=False, provider={})
    body = {
        "model": model,
        "messages": messages,
        "max_tokens": int(max_tokens),
        "temperature": s.get("temperature", 0.2),
        "stream": False,
    }
    if s.get("reasoning") is not None:
        body["reasoning"] = s["reasoning"]
    provider = dict(s.get("provider") or {})
    if schema is not None:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": re.sub(r"[^A-Za-z0-9_-]", "_", purpose)[:64],
                            "strict": bool(strict), "schema": schema},
        }
        if s.get("require_parameters", True):
            provider["require_parameters"] = True
        if s.get("response_healing", True):
            body["plugins"] = [{"id": "response-healing"}]
    if provider:
        body["provider"] = provider
    # Deliberately NOT sent: usage:{include:true} (deprecated, no effect) and any
    # response-cache option (cache hits report zero usage = unverifiable tokens).
    return body


def _retry_wait(resp, attempt: int) -> float:
    ra = resp.headers.get("Retry-After") if resp is not None else None
    if ra:
        try:
            return min(max(float(ra), 0.0), RETRY_AFTER_CAP_S)
        except ValueError:
            pass
    return BACKOFF_S[min(attempt - 1, len(BACKOFF_S) - 1)]


def _error_info(status: int, body) -> tuple[int | None, str]:
    """Find an error in the body even when HTTP status is 200."""
    if not isinstance(body, dict):
        return (status if status != 200 else 502), "response body is not JSON"
    err = body.get("error")
    if err is None:
        choices = body.get("choices") or []
        if choices and isinstance(choices[0], dict):
            err = choices[0].get("error")
    if err is None:
        return (None, "") if status == 200 else (status, f"HTTP {status}")
    if isinstance(err, dict):
        code = err.get("code")
        try:
            code = int(code)
        except (TypeError, ValueError):
            code = status if status != 200 else 502
        return code, str(err.get("message", ""))[:300]
    return (status if status != 200 else 502), str(err)[:300]


_session = None


def _default_session():
    global _session
    if _session is None:
        _session = requests.Session()  # keep-alive: saves a TLS handshake per call
    return _session


# --- main entry -------------------------------------------------------------
def chat(messages, *, model, max_tokens, purpose, budget, trace, schema=None,
         stage="llm", session=None, sleep=time.sleep, settings=None,
         strict=True) -> LLMResult:
    key = get_api_key()
    session = session or _default_session()
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "X-Title": "paper-to-playground",
    }
    fp = fingerprint("\n".join(str(m.get("content", "")) for m in messages))

    last_err: LLMError | None = None
    for attempt in range(1, MAX_RETRIES + 2):
        mt = budget.clamp_max_tokens(max_tokens)
        timeout = min(REQUEST_TIMEOUT_CAP_S, budget.time_left() - DEADLINE_MARGIN_S)
        if budget.calls_left() <= 0 or mt < MIN_TOKENS or timeout < 5:
            trace.event(stage, f"call:{purpose}", "skip", attempt=attempt,
                        reason="budget exhausted", calls=budget.calls,
                        completion_left=budget.completion_left(),
                        time_left_s=round(budget.time_left(), 1))
            raise last_err or BudgetExhausted("budget exhausted before call")

        payload = build_payload(messages, model=model, max_tokens=mt, schema=schema,
                                purpose=purpose, settings=settings, strict=strict)
        base = dict(call_index=budget.calls + 1, purpose=purpose, model=model,
                    attempt=attempt, max_tokens=mt, **fp)
        t = time.monotonic()
        try:
            resp = session.post(URL, headers=headers, json=payload, timeout=timeout)
        except (requests.Timeout, requests.ConnectionError) as e:
            elapsed = round(time.monotonic() - t, 3)
            budget.record(None)
            kind = "timeout" if isinstance(e, requests.Timeout) else "network"
            trace.llm_call(ok=False, stage=stage, elapsed_s=elapsed, usage_missing=True,
                           error=kind, retry_reason=kind if attempt <= MAX_RETRIES else None,
                           **base)
            last_err = APIError(f"{kind} error contacting OpenRouter", status=None)
            if attempt <= MAX_RETRIES:
                sleep(BACKOFF_S[attempt - 1])
                continue
            raise last_err
        elapsed = round(time.monotonic() - t, 3)

        try:
            body = resp.json()
        except ValueError:
            body = None
        usage = parse_usage(body)
        budget.record(usage)
        code, msg = _error_info(resp.status_code, body)
        gen_id = body.get("id") if isinstance(body, dict) else None
        common = dict(base, http_status=resp.status_code, elapsed_s=elapsed,
                      generation_id=gen_id, usage_missing=usage is None, **(usage or {}))

        if code is not None:  # error (HTTP error or error object inside a 200)
            if code in (400, 404) and model not in _RELAXED and attempt <= MAX_RETRIES:
                # likely an unsupported optional parameter for this model/provider:
                # retry once with a plain request (counts as a call, like any retry)
                _RELAXED.add(model)
                trace.llm_call(ok=False, stage=stage, error=f"{code}: {msg}",
                               retry_reason="compatibility fallback (optional parameters dropped)", **common)
                last_err = APIError(f"OpenRouter error {code}: {msg}", status=code, usage=usage)
                continue
            retry = code in RETRYABLE and attempt <= MAX_RETRIES
            trace.llm_call(ok=False, stage=stage, error=f"{code}: {msg}",
                           retry_reason=f"http {code}" if retry else None, **common)
            last_err = APIError(f"OpenRouter error {code}: {msg}", status=code, usage=usage)
            if retry:
                wait = _retry_wait(resp, attempt)
                if wait > budget.time_left() - DEADLINE_MARGIN_S:
                    raise last_err
                sleep(wait)
                continue
            raise last_err

        choice = (body.get("choices") or [{}])[0]
        text = (choice.get("message") or {}).get("content") or ""
        finish = choice.get("finish_reason")
        trace.llm_call(ok=finish != "length" and bool(text), stage=stage,
                       finish_reason=finish, **common)

        if finish == "length":
            u = usage or {}
            visible = u.get("completion_tokens", 0) - u.get("reasoning_tokens", 0)
            if usage is not None and visible < 50:
                raise ReasoningStarved("output budget used up by hidden reasoning",
                                       usage=usage, text=text)
            raise Truncated("output hit max_tokens", usage=usage, text=text)
        if not text.strip():
            raise BadJSON("empty response content", usage=usage, text=text)

        data = None
        if schema is not None:
            try:
                data = parse_json(text)
            except BadJSON as e:
                e.usage = usage
                trace.event(stage, f"parse:{purpose}", "fail", error="invalid JSON",
                            content_chars=len(text))
                raise
        return LLMResult(text=text, data=data, usage=usage or {}, finish_reason=finish,
                         generation_id=gen_id, elapsed_s=elapsed, attempts=attempt)

    raise last_err or APIError("unreachable")  # pragma: no cover
