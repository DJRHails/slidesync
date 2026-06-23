"""Styled-template bullet bodies auto-shrink to fit their fixed-height box.

A long body that would overflow at the desired `BODY_PT` is stepped down to the
largest size that fits (down to a readable floor), and a warning fires whenever
the body is forced below `BODY_PT` — louder when it overflows even at the floor.
Short bodies are untouched (stay at `BODY_PT`, no warning), so existing decks
render unchanged.
"""

import pytest
from loguru import logger

from slidesync._sync import (
    BODY_PT,
    Para,
    STYLES,
    Slide,
    _est_body_lines,
    _fit_paras_pt,
    _styled_requests,
)

OID = "s2g_aaaaaaaaaa_bbbbbbbbbb"


def _bullets(n, text="a short point"):
    return [Para(text=text, depth=0) for _ in range(n)]


def _render(paras):
    slide = Slide(key="terms", layout="content", title="TERMS", paras=paras)
    slide.object_id = OID
    return _styled_requests(slide, STYLES["content"], None, None)


def _body_font_pt(reqs):
    """Explicit fontSize emitted for the body (`_b`) text box, if any."""
    for r in reqs:
        ts = r.get("updateTextStyle")
        if ts and ts["objectId"] == OID + "_b" and "fontSize" in ts["style"]:
            return ts["style"]["fontSize"]["magnitude"]
    return None


def _warnings(fn):
    msgs = []
    sink = logger.add(lambda m: msgs.append(str(m)), level="WARNING")
    try:
        fn()
    finally:
        logger.remove(sink)
    return msgs


def test_short_body_keeps_desired_size_and_is_silent():
    captured = {}
    msgs = _warnings(lambda: captured.setdefault("reqs", _render(_bullets(4))))
    assert _body_font_pt(captured["reqs"]) == BODY_PT
    assert msgs == []


def test_long_body_shrinks_below_desired():
    reqs = _render(_bullets(18))
    pt = _body_font_pt(reqs)
    assert pt is not None and pt < BODY_PT


def test_warns_when_shrunk():
    msgs = _warnings(lambda: _render(_bullets(18)))
    assert any("auto-shrunk" in m and "terms" in m for m in msgs)


def test_huge_body_overflowing_the_floor_raises():
    with pytest.raises(SystemExit, match="overflows the slide even at"):
        _render(_bullets(200))


def test_fit_paras_pt_spans_base_to_floor():
    assert _fit_paras_pt(_bullets(1)) == BODY_PT          # trivially fits
    assert _fit_paras_pt(_bullets(200)) < BODY_PT          # forced down
    # Monotonic: more content never fits at a larger size than less content.
    assert _fit_paras_pt(_bullets(40)) <= _fit_paras_pt(_bullets(10))


def test_nested_bullets_wrap_in_a_narrower_column():
    long = "word " * 40
    flat = [Para(text=long, depth=0)]
    nested = [Para(text=long, depth=3)]
    assert _est_body_lines(nested, BODY_PT) > _est_body_lines(flat, BODY_PT)


def test_empty_paragraph_counts_as_a_line():
    assert _est_body_lines([Para(text="", depth=-1)], BODY_PT) == 1
