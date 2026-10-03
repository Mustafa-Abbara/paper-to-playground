"""Model-level request settings, keyed ONLY by MODEL_ID (never by paper or topic).

Each entry may set:
  reasoning          dict sent as OpenRouter's `reasoning` parameter, or None to omit it
  require_parameters route only to providers that honour every parameter we send
  response_healing   enable OpenRouter's JSON-syntax repair plugin
  temperature        sampling temperature
  provider           extra OpenRouter provider-routing preferences (merged)

Values for a new model are chosen by measuring with tools/ping.py, not guessed.
"""
from __future__ import annotations

import copy

DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash"

DEFAULTS: dict = {
    "reasoning": None,           # unknown model: do not send the parameter at all
    "require_parameters": True,
    "response_healing": True,
    "temperature": 0.2,
    "provider": {},
}

MODELS: dict[str, dict] = {
    # Fast, cheap, structured outputs supported. Reasoning is disabled so that no
    # hidden "thinking" tokens are billed against the 30k completion budget.
    # Re-verify with: python tools/ping.py --model deepseek/deepseek-v4.1-flash
    "deepseek/deepseek-v4.1-flash": {
        "reasoning": {"effort": "none"},
    },
}


def model_settings(model: str) -> dict:
    """Settings for ``model``: DEFAULTS overlaid with the MODELS entry (deep-copied)."""
    s = copy.deepcopy(DEFAULTS)
    s.update(copy.deepcopy(MODELS.get(model, {})))
    return s
