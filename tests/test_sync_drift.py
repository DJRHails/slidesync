"""Offline tests for drift detection: comment shaping, text-line extraction
parity (markdown vs native), and the three-way classification."""

from slidesync._sync import (
    classify_drift,
    shape_comments,
    text_lines_md,
    text_lines_native,
)

RAW_COMMENTS = [
    {"id": "c1",
     "anchor": '{"type":"page","uid":1,"pages":["s2g_aaaaaaaaaa_bbbbbbbbbb"]}',
     "author": {"displayName": "Daniel Hails"},
     "content": "This is obviously dodgy. To debug",
     "modifiedTime": "2026-06-12T15:00:00Z",
     "replies": [
         {"author": {"displayName": "Ted"}, "content": "agree"},
         {"author": {"displayName": "Daniel Hails"}, "content": ""},  # resolve action
     ]},
    {"id": "c2", "anchor": "not json", "author": {}, "content": "file-level",
     "resolved": True},
]


def test_shape_comments_extracts_page_author_replies():
    a, b = shape_comments(RAW_COMMENTS)
    assert a["page"] == "s2g_aaaaaaaaaa_bbbbbbbbbb"
    assert a["author"] == "Daniel Hails" and not a["resolved"]
    assert a["replies"] == [{"author": "Ted", "content": "agree"}]
    assert b["page"] is None and b["resolved"] is True


def _native(texts):
    """A native slide with one text box per entry, stacked top to bottom."""
    els = []
    for i, text in enumerate(texts):
        els.append({"transform": {"translateY": i * 914400, "translateX": 0},
                    "shape": {"text": {"textElements": [
                        {"paragraphMarker": {}},
                        {"textRun": {"content": text, "style": {}}}]}}})
    return {"objectId": "s", "pageElements": els}


def test_md_and_native_text_lines_agree_for_a_styled_slide():
    md = "# 2026/06/15\n## RELIABLE MONITORS\n\nWeekly Update\n<!-- a note -->"
    # live render stacks kicker above headline — order differs, content matches
    native = _native(["RELIABLE MONITORS", "2026/06/15", "Weekly Update"])
    assert text_lines_md(md) == text_lines_native(native)


def test_md_text_lines_include_verbatim_fences_and_skip_comments():
    md = "## PROMPT\n```text\nYou are a monitor.\n```\n<!-- hidden -->"
    lines = text_lines_md(md)
    assert "You are a monitor." in lines
    assert not any("hidden" in line for line in lines)


def test_classify_drift_three_way():
    base, local, live = ["a"], ["a"], ["a"]
    assert classify_drift(base, local, live) == "clean"
    assert classify_drift(base, ["b"], live) == "local-edit"
    assert classify_drift(base, local, ["b"]) == "live-drift"
    assert classify_drift(base, ["b"], ["c"]) == "conflict"
    assert classify_drift(base, ["b"], ["b"]) == "converged"
    assert classify_drift(None, ["a"], ["a"]) == "clean"
    assert classify_drift(None, ["a"], ["b"]) == "drift-no-base"
