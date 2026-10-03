"""Load and validate case.json.

Only three fields are required (source_url, focus, audience): the brief says
"five required string fields" but names only these three. Every other field is
kept in ``case.extra`` and forwarded to the model as labelled context.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

REQUIRED = ("source_url", "focus", "audience")
MAX_FIELD_CHARS = 12_000
TRUNC_MARKER = "\n[... truncated: {dropped} of {total} characters omitted ...]"

# Extra fields shown first in the context block, in this order (case-insensitive).
PRIORITY = ("excerpt", "title", "paper_title", "section")


class CaseError(Exception):
    """Bad input: missing file, invalid JSON, or a missing/non-string required field."""


@dataclass
class Case:
    source_url: str
    focus: str
    audience: str
    extra: dict[str, str] = field(default_factory=dict)  # insertion-ordered
    truncated: list[dict] = field(default_factory=list)  # filled by context_block()

    def get(self, name: str) -> str | None:
        """Case-insensitive lookup of an extra field (e.g. 'excerpt')."""
        for k, v in self.extra.items():
            if k.lower() == name.lower():
                return v
        return None

    @property
    def excerpt(self) -> str | None:
        return self.get("excerpt")

    def context_block(self, max_field_chars: int = MAX_FIELD_CHARS) -> str:
        """Compact labelled text for the model: EXCERPT first, then other extras,
        then SOURCE_URL / FOCUS / AUDIENCE. Over-long fields are truncated and
        recorded in ``self.truncated``."""
        self.truncated = []
        ordered: list[tuple[str, str]] = []
        lower = {k.lower(): k for k in self.extra}
        for p in PRIORITY:
            if p in lower:
                ordered.append((lower[p], self.extra[lower[p]]))
        seen = {k for k, _ in ordered}
        ordered += [(k, v) for k, v in self.extra.items() if k not in seen]
        ordered += [("source_url", self.source_url), ("focus", self.focus),
                    ("audience", self.audience)]

        parts = []
        for key, value in ordered:
            text = value.strip()
            if len(text) > max_field_chars:
                dropped = len(text) - max_field_chars
                self.truncated.append({"field": key, "chars": len(text), "kept": max_field_chars})
                text = text[:max_field_chars] + TRUNC_MARKER.format(dropped=dropped, total=len(text))
            label = key.upper()
            sep = "\n" if "\n" in text or len(text) > 80 else " "
            parts.append(f"{label}:{sep}{text}")
        return "\n\n".join(parts)


def load_case(path: str) -> Case:
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        raise CaseError(f"cannot read input file: {e.strerror or e}") from None
    try:
        data = json.loads(raw.decode("utf-8-sig"))  # tolerate a UTF-8 BOM
    except UnicodeDecodeError:
        raise CaseError("input is not valid UTF-8") from None
    except json.JSONDecodeError as e:
        raise CaseError(f"input is not valid JSON: {e.msg} (line {e.lineno}, col {e.colno})") from None
    if not isinstance(data, dict):
        raise CaseError("input JSON must be an object")

    req = {}
    for name in REQUIRED:
        if name not in data:
            raise CaseError(f"missing required field: {name}")
        value = data[name]
        if not isinstance(value, str):
            raise CaseError(f"field '{name}' must be a string, got {type(value).__name__}")
        if not value.strip():
            raise CaseError(f"field '{name}' is empty")
        req[name] = value

    extra: dict[str, str] = {}
    for k, v in data.items():
        if k in REQUIRED:
            continue
        if isinstance(v, str):
            extra[str(k)] = v
        else:
            extra[str(k)] = json.dumps(v, ensure_ascii=False)
    return Case(extra=extra, **req)
