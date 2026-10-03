import json
import os
import re

import quickjs
import pytest

from p2p.assemble import (TEMPLATE_DIR, assemble, plain_text, sanitize_html, sanitize_spec,
                          wrap_compute)

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
NS_URI = "http://www.w3.org/2000/svg"  # SVG namespace identifier (not a network request)


def fixture():
    with open(os.path.join(FIX, "generic_spec.json"), encoding="utf-8") as f:
        spec = json.load(f)
    with open(os.path.join(FIX, "generic_compute.js"), encoding="utf-8") as f:
        js = f.read()
    return spec, js


# --- sanitizer -------------------------------------------------------------
@pytest.mark.parametrize("dirty,clean", [
    ("<b>bold</b> and <i>x</i><sub>i</sub>", "<b>bold</b> and <i>x</i><sub>i</sub>"),
    ("<script>alert(1)</script>ok", "ok"),
    ('<img src="http://evil/x.png" onerror="alert(1)">hi', "hi"),
    ('<a href="https://example.com">link</a>', "link"),
    ('<b onclick="x()" style="color:red">t</b>', "<b>t</b>"),
    ("<style>@import url(http://x)</style>t", "t"),
    ("<svg><script>1</script></svg>after", "after"),
    ("a < b & c > d", "a &lt; b &amp; c &gt; d"),
    ("<b>unclosed", "<b>unclosed</b>"),
    ("<iframe src=x></iframe>z", "z"),
])
def test_sanitize_html(dirty, clean):
    assert sanitize_html(dirty) == clean


def test_mathml_kept_with_safe_attributes_only():
    s = ('<math display="block"><mi mathvariant="normal">H</mi><mo stretchy="false" '
         'href="http://x">(</mo><mfrac><mn>1</mn><msqrt><mi>d</mi></msqrt></mfrac></math>')
    out = sanitize_html(s)
    assert '<math display="block">' in out and '<mo stretchy="false">' in out
    assert "<mfrac>" in out and "<msqrt>" in out and "href" not in out
    assert sanitize_html('<mi mathvariant="url(javascript:x)">x</mi>') == "<mi>x</mi>"


def test_sanitize_spec_keeps_raw_keys():
    spec = {"title": "<script>x</script>T", "controls": [
        {"id": "a<b", "type": "slider", "label": "<b>A</b><img src=x>",
         "default": "<raw>", "options": [{"value": "a&b", "label": "<i>A</i>"}]}],
        "sections": {"explorations": [{"preset": {"s": "<keep>"}}]},
        "meta": {"source_url": "https://arxiv.org/abs/1"}}
    out = sanitize_spec(spec)
    c = out["controls"][0]
    assert out["title"] == "T" and c["id"] == "a<b" and c["default"] == "<raw>"
    assert c["label"] == "<b>A</b>" and c["options"][0] == {"value": "a&b", "label": "<i>A</i>"}
    assert sanitize_spec({"options": ["a<b", {"value": "v", "label": "<img src=x>L"}]}) == \
        {"options": ["a<b", {"value": "v", "label": "L"}]}
    assert out["sections"]["explorations"][0]["preset"]["s"] == "<keep>"
    assert out["meta"]["source_url"] == "https://arxiv.org/abs/1"
    assert sanitize_spec({"x": float("nan")}) == {"x": None}


def test_plain_text():
    assert plain_text("A <i>b</i> &amp; c<script>x</script>") == "A b & c"


# --- assembly ----------------------------------------------------------------
def test_fixture_page_is_self_contained():
    spec, js = fixture()
    page = assemble(spec, js)
    assert page.startswith("<!doctype html>") and len(page) < 150_000
    assert "{{" not in page
    for bad in (r"<script[^>]+src=", r"<link\b", r"@import", r"<iframe", r"\bfetch\(",
                r"XMLHttpRequest", r"WebSocket", r"EventSource", r"\bimport\("):
        assert not re.search(bad, page, re.I), bad
    urls = set(re.findall(r"https?://[^\s\"'<>)]+", page))
    assert urls <= {NS_URI}, urls


def test_script_breakout_is_neutralised():
    spec = {"title": "t", "sections": {"idea": {"what": "x"}},
            "controls": [{"id": "k", "type": "select", "default": "</script><script>alert(1)",
                          "options": ["</script><script>alert(1)"]}]}
    js = 'function compute(s){ var t = "</script><script>alert(2)//"; return {outputs:{}}; }'
    page = assemble(spec, js)
    # only "</script" can end a script block; the three real closing tags must be the only ones
    assert page.lower().count("</script") == 3
    assert '"options":["</script>' not in page and "<\\/script>" in page


def test_placeholder_tokens_in_content_are_not_expanded():
    spec, js = fixture()
    spec["title"] = "{{RUNTIME_JS}}"
    page = assemble(spec, js + "\n// {{STYLE}}")
    assert page.count("Paper to Playground runtime") == 1
    assert page.count("--serif:") == 1


# --- JavaScript compiles / runs in QuickJS ---------------------------------
def js_context():
    ctx = quickjs.Context()
    ctx.set_time_limit(2)
    ctx.set_memory_limit(64 * 1024 * 1024)
    ctx.eval("var window = {};")
    return ctx


def test_runtime_js_parses():
    with open(os.path.join(TEMPLATE_DIR, "runtime.js"), encoding="utf-8") as f:
        src = f.read()
    js_context().eval("(function () {\n" + src + "\n});")  # compile only, do not run


def test_fixture_compute_runs_through_the_same_wrapper():
    spec, js = fixture()
    ctx = js_context()
    ctx.eval(wrap_compute(js))
    state = {c["id"]: c["default"] for c in spec["controls"]}
    out = json.loads(ctx.eval(f"JSON.stringify(window.__P2P_COMPUTE({json.dumps(state)}))"))
    assert out["outputs"]["ys"] == [-1.0, 0.5, 2.0, 3.5]
    assert out["outputs"]["mean_y"] == pytest.approx(1.25)
    assert all(c["pass"] for c in out["checks"])


def test_broken_compute_reports_error_instead_of_crashing():
    ctx = js_context()
    ctx.eval(wrap_compute("throw new Error('boom');"))
    assert ctx.eval("window.__P2P_COMPUTE_ERROR") == "boom"
