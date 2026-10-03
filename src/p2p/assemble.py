"""Assemble template + SPEC + compute.js into ONE self-contained index.html.

The model never writes the page. It supplies:
  * SPEC        a JSON object (text, controls, visuals) consumed by runtime.js
  * compute_js  a pure JS function compute(state) -> {outputs, intermediates, checks}

Every display string in SPEC is sanitized here with a strict allowlist (simple
formatting + MathML Core). No attributes that can load anything, no URLs, no event
handlers, no script/style survive.
"""
from __future__ import annotations

import html
import json
import os
import re
from html.parser import HTMLParser

TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "template")

TEXT_TAGS = {"b", "i", "em", "strong", "sub", "sup", "code", "br", "span", "small", "p",
             "ul", "ol", "li"}
MATH_TAGS = {"math", "mi", "mn", "mo", "mrow", "msub", "msup", "msubsup", "mfrac", "msqrt",
             "mroot", "mover", "munder", "munderover", "mtext", "mtable", "mtr", "mtd",
             "mspace", "mstyle", "mpadded", "mphantom", "semantics"}
ALLOWED_TAGS = TEXT_TAGS | MATH_TAGS
VOID_TAGS = {"br", "mspace"}
# Content of these is dropped entirely (not just the tag).
DROP_CONTENT = {"script", "style", "iframe", "object", "embed", "template", "noscript",
                "svg", "title", "textarea", "annotation", "annotation-xml", "head", "select"}
ALLOWED_ATTRS = {
    "math": {"display"},
    "mo": {"stretchy", "fence", "separator", "lspace", "rspace", "form", "largeop",
           "movablelimits", "symmetric"},
    "mi": {"mathvariant"},
    "mn": {"mathvariant"},
    "mtext": {"mathvariant"},
    "mover": {"accent"},
    "munder": {"accentunder"},
    "munderover": {"accent", "accentunder"},
    "mfrac": {"linethickness"},
    "mspace": {"width"},
    "mstyle": {"displaystyle", "scriptlevel", "mathvariant"},
    "mtd": {"columnspan", "rowspan"},
}
SAFE_ATTR_VALUE = re.compile(r"^[A-Za-z0-9 .\-%]{1,40}$")

# SPEC keys whose string values are identifiers/data, not display text.
RAW_KEYS = {"id", "key", "type", "source", "value", "values", "default", "preset", "x", "y",
            "x1", "y1", "x2", "y2", "w", "h", "r", "width", "height", "rows_from", "cols_from",
            "length_from", "shape", "kind", "scale", "style", "anchor", "highlight",
            "stroke_width", "size", "dx", "dy", "series_key", "fill", "source_url"}
RAW_SUBTREES = {"preset", "default"}


class _Sanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.stack: list[str] = []
        self.drop_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in DROP_CONTENT:
            self.drop_depth += 1
            return
        if self.drop_depth or tag not in ALLOWED_TAGS:
            return
        allowed = ALLOWED_ATTRS.get(tag, set())
        parts = [tag]
        for name, value in attrs:
            name = (name or "").lower()
            if name in allowed and value is not None and SAFE_ATTR_VALUE.match(value):
                parts.append(f'{name}="{html.escape(value, quote=True)}"')
        self.out.append("<" + " ".join(parts) + ">")
        if tag not in VOID_TAGS:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        tag = tag.lower()
        if tag in DROP_CONTENT:
            return
        self.handle_starttag(tag, attrs)
        if tag in ALLOWED_TAGS and tag not in VOID_TAGS and not self.drop_depth:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in DROP_CONTENT:
            self.drop_depth = max(0, self.drop_depth - 1)
            return
        if self.drop_depth or tag not in self.stack:
            return
        while self.stack:
            t = self.stack.pop()
            self.out.append(f"</{t}>")
            if t == tag:
                break

    def handle_data(self, data):
        if not self.drop_depth:
            self.out.append(html.escape(data, quote=False))

    def result(self) -> str:
        self.close()
        while self.stack:
            self.out.append(f"</{self.stack.pop()}>")
        return "".join(self.out)


def sanitize_html(s: str) -> str:
    """Allowlist sanitizer for display strings (HTML-lite + MathML Core)."""
    p = _Sanitizer()
    p.feed(str(s))
    return p.result()


def plain_text(s: str) -> str:
    """Strip all tags (used for <title>)."""
    p = _Sanitizer()
    p.feed(str(s))
    text = re.sub(r"<[^>]+>", "", p.result())
    return html.unescape(text).strip()


def sanitize_spec(obj, key: str | None = None, raw: bool = False):
    if isinstance(obj, dict):
        return {str(k): sanitize_spec(v, str(k), raw or k in RAW_SUBTREES) for k, v in obj.items()}
    if isinstance(obj, list):
        if key == "options":  # plain-string options are values: keep raw (shown as text only)
            return [v if isinstance(v, str) else sanitize_spec(v, key, raw) for v in obj]
        return [sanitize_spec(v, key, raw) for v in obj]
    if isinstance(obj, str):
        if raw or key in RAW_KEYS:
            return obj
        return sanitize_html(obj)
    if isinstance(obj, float) and (obj != obj or obj in (float("inf"), float("-inf"))):
        return None
    return obj


# --- compute.js wrapper (shared with the numeric checks so both run the same code) ---
COMPUTE_PREFIX = '(function () {\n"use strict";\ntry {\n'
COMPUTE_SUFFIX = ('\n;window.__P2P_COMPUTE = (typeof compute === "function") ? compute : null;\n'
                  '} catch (e) { window.__P2P_COMPUTE_ERROR = String(e && e.message || e); }\n'
                  '})();')


def wrap_compute(js: str) -> str:
    return COMPUTE_PREFIX + js + COMPUTE_SUFFIX


def _escape_script(js: str) -> str:
    js = re.sub(r"</(script)", r"<\\/\1", js, flags=re.I)
    return js.replace("<!--", "<\\!--")


def _escape_json(spec: dict) -> str:
    text = json.dumps(spec, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return text.replace("</", "<\\/").replace("<!--", "<\\u0021--")


def _read(name: str) -> str:
    with open(os.path.join(TEMPLATE_DIR, name), encoding="utf-8") as f:
        return f.read()


def assemble(spec: dict, compute_js: str) -> str:
    """Return the complete, self-contained HTML page as a string."""
    clean = sanitize_spec(spec if isinstance(spec, dict) else {})
    parts = {
        "TITLE": html.escape(plain_text(clean.get("title") or "Interactive explainer"), quote=False),
        "STYLE": _read("style.css"),
        "SPEC_JSON": _escape_json(clean),
        "COMPUTE_JS": _escape_script(wrap_compute(compute_js or "")),
        "RUNTIME_JS": _escape_script(_read("runtime.js")),
    }
    page = _read("page.html")
    # One pass: substituted text is never re-scanned for placeholders.
    return re.sub(r"\{\{(TITLE|STYLE|SPEC_JSON|COMPUTE_JS|RUNTIME_JS)\}\}",
                  lambda m: parts[m.group(1)], page)


def write_page(out_dir: str, spec: dict, compute_js: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "index.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(assemble(spec, compute_js))
    return path
