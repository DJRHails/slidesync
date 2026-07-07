"""Offline tests for the markdown parse/render path (no network/auth)."""

from slidesync._sync import (
    HIGHLIGHT,
    INK,
    SAMPLE,
    Run,
    _body,
    _coalesce_runs,
    build_slides,
    parse_body,
    parse_inline,
    render_inline,
    split_slides,
    to_slidev,
)


def _slide(key):
    return next(s for s in build_slides(split_slides(SAMPLE)) if s.key == key)


def test_sample_parses_all_slides():
    slides = build_slides(split_slides(SAMPLE))
    assert [s.key for s in slides] == [
        "intro", "findings", "data", "maths", "objective", "ask", "titlecard"]


def test_nested_bullets_round_trip_through_render():
    findings = _slide("findings")
    depths = sorted({p.depth for p in findings.paras if p.text})
    assert depths == [0, 1]  # top-level + one level of nesting
    md = to_slidev(findings)
    assert "\n  - " in md  # 2-space-indented child bullet
    assert "**bold**" in md and "`code`" in md and "[link](https://example.com)" in md


def test_table_round_trips():
    data = _slide("data")
    assert data.table == [["Metric", "Value"], ["AUROC", "0.93"], ["Gap", "small"]]
    assert "| Metric | Value |" in to_slidev(data)


def test_highlight_parses_to_a_run_and_renders_back():
    clean, runs = parse_inline("the ==headline effect== survives")
    assert clean == "the headline effect survives"
    assert [(r.start, r.end, r.style) for r in runs] == [(4, 19, "highlight")]
    assert render_inline(clean, runs) == "the ==headline effect== survives"


def test_highlight_composes_with_other_inline_styles():
    src = "==marked== and **bold** and `code` and [x](https://e.com)"
    clean, runs = parse_inline(src)
    assert [r.style for r in runs] == ["highlight", "bold", "code", "link"]
    assert render_inline(clean, runs) == src  # byte-identical round-trip


def test_bare_double_equals_is_not_a_highlight():
    # markdown-it-mark needs non-space-adjacent delimiters: `a == b == c` is
    # comparison prose, not a highlight of " b ".
    clean, runs = parse_inline("if a == b == c holds")
    assert clean == "if a == b == c holds" and runs == []


def test_highlight_run_pushes_a_background_wash():
    _h, paras, *_ = parse_body("a ==marked== word\n")
    styles = [r["updateTextStyle"] for r in _body("B", paras)
              if "updateTextStyle" in r
              and r["updateTextStyle"]["textRange"]["type"] == "FIXED_RANGE"]
    [st] = styles
    assert st["style"]["backgroundColor"]["opaqueColor"]["rgbColor"] == HIGHLIGHT
    assert st["style"]["foregroundColor"]["opaqueColor"]["rgbColor"] == INK
    assert st["fields"] == "backgroundColor,foregroundColor"


def test_coalesce_adjacent_same_style_runs():
    runs = [Run(0, 1, "bold"), Run(1, 5, "bold"), Run(5, 9, "italic")]
    merged = _coalesce_runs(runs)
    assert [(r.start, r.end, r.style) for r in merged] == [(0, 5, "bold"), (5, 9, "italic")]


def test_render_inline_keeps_whitespace_outside_marks():
    # a bold run that includes trailing spaces must not emit `**ok  **` (invalid)
    assert render_inline("ok  done", [Run(0, 4, "bold")]) == "**ok**  done"


def test_image_alt_with_brackets_parses_as_image():
    # A Wilson-CI bracket in the alt ("[1.7–3.1]") must not truncate the image match.
    # Regression: IMAGE_RE's alt group once forbade ']', so the ![](…) line fell through
    # into the slide body and a text-free `graph` slide rendered blank on push.
    md = (
        "---\ntheme: seriph\n---\n\n"
        "---\ntemplate: graph\nid: fig\n---\n"
        "![a leak at 2.3% [1.7–3.1]; the rest hold](../figures/x.png)\n"
    )
    slide = next(s for s in build_slides(split_slides(md)) if s.key == "fig")
    assert slide.image == "../figures/x.png"
    assert slide.image_alt == "a leak at 2.3% [1.7–3.1]; the rest hold"
    assert not any("![" in p.text for p in slide.paras), "image must not fall through to body"
    assert "![a leak at 2.3% [1.7–3.1]; the rest hold](../figures/x.png)" in to_slidev(slide)
