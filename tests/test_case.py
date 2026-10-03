import json

import pytest

from p2p.case import MAX_FIELD_CHARS, CaseError, load_case

BASE = {"source_url": "u", "focus": "f", "audience": "a"}


def write(tmp_path, data, raw=None):
    p = tmp_path / "case.json"
    p.write_bytes(raw if raw is not None else json.dumps(data).encode("utf-8"))
    return str(p)


@pytest.mark.parametrize("field", ["source_url", "focus", "audience"])
def test_missing_required_field(tmp_path, field):
    data = {k: v for k, v in BASE.items() if k != field}
    with pytest.raises(CaseError, match=field):
        load_case(write(tmp_path, data))


def test_non_string_required_field(tmp_path):
    with pytest.raises(CaseError, match="must be a string"):
        load_case(write(tmp_path, {**BASE, "focus": 5}))


def test_empty_required_field(tmp_path):
    with pytest.raises(CaseError, match="empty"):
        load_case(write(tmp_path, {**BASE, "audience": "  "}))


def test_bad_json_and_non_object(tmp_path):
    with pytest.raises(CaseError, match="not valid JSON"):
        load_case(write(tmp_path, None, raw=b"{nope"))
    with pytest.raises(CaseError, match="object"):
        load_case(write(tmp_path, [1, 2]))
    with pytest.raises(CaseError, match="cannot read"):
        load_case(str(tmp_path / "missing.json"))


def test_extra_fields_preserved_in_order(tmp_path):
    data = {**BASE, "title": "T", "excerpt": "E", "weird": 5, "nested": {"a": [1]}}
    case = load_case(write(tmp_path, data))
    assert list(case.extra) == ["title", "excerpt", "weird", "nested"]
    assert case.extra["weird"] == "5"
    assert json.loads(case.extra["nested"]) == {"a": [1]}
    assert case.excerpt == "E"


def test_utf8_bom_and_unicode(tmp_path):
    data = {**BASE, "excerpt": "softmax(QKᵀ/√dₖ)V — H = −Σ pᵢ log pᵢ"}
    raw = b"\xef\xbb\xbf" + json.dumps(data, ensure_ascii=False).encode("utf-8")
    case = load_case(write(tmp_path, None, raw=raw))
    assert "√dₖ" in case.excerpt


def test_context_block_excerpt_first_and_truncation(tmp_path):
    long = "x" * (MAX_FIELD_CHARS + 500)
    data = {**BASE, "notes": "n", "title": "T", "excerpt": long}
    case = load_case(write(tmp_path, data))
    block = case.context_block()
    assert block.startswith("EXCERPT:")
    assert block.index("TITLE:") < block.index("NOTES:") < block.index("SOURCE_URL:")
    assert "truncated: 500 of" in block
    assert case.truncated == [{"field": "excerpt", "chars": len(long), "kept": MAX_FIELD_CHARS}]


def test_no_excerpt(tmp_path):
    case = load_case(write(tmp_path, BASE))
    assert case.excerpt is None
    assert "FOCUS: f" in case.context_block()
