"""Offline tests for the markdown parse/render path (no network/auth)."""

from slidesync._sync import (
    SAMPLE,
    Run,
    _coalesce_runs,
    build_slides,
    render_inline,
    split_slides,
    to_slidev,
)


def _slide(key):
    return next(s for s in build_slides(split_slides(SAMPLE)) if s.key == key)


def test_sample_parses_all_slides():
    slides = build_slides(split_slides(SAMPLE))
    assert [s.key for s in slides] == ["intro", "findings", "data", "ask", "titlecard"]


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


def test_coalesce_adjacent_same_style_runs():
    runs = [Run(0, 1, "bold"), Run(1, 5, "bold"), Run(5, 9, "italic")]
    merged = _coalesce_runs(runs)
    assert [(r.start, r.end, r.style) for r in merged] == [(0, 5, "bold"), (5, 9, "italic")]


def test_render_inline_keeps_whitespace_outside_marks():
    # a bold run that includes trailing spaces must not emit `**ok  **` (invalid)
    assert render_inline("ok  done", [Run(0, 4, "bold")]) == "**ok**  done"
