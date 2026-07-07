"""Offline tests for `pull` slide reconstruction from native Slides JSON.

Regression coverage for the two bugs fixed in `_slide_from_native`:
multi-text-box collapse, and the positional-arg slip that spilled `notes` into
the `table` slot (rendered as a one-char-per-row table).
"""

from slidesync._sync import _slide_from_native, to_slidev

IN = 914400  # EMU per inch


def _shape(y, x, font, paras, placeholder=None):
    """A page element holding a text box. `paras` = [(nestingLevel|None, text)]."""
    els = []
    for depth, text in paras:
        marker = {"bullet": {"nestingLevel": depth}} if depth is not None else {}
        els.append({"paragraphMarker": marker})
        els.append({"textRun": {"content": text, "style": {"fontSize": {"magnitude": font}}}})
    shape = {"text": {"textElements": els}}
    if placeholder:
        shape["placeholder"] = {"type": placeholder}
    return {"transform": {"translateY": y * IN, "translateX": x * IN}, "shape": shape}


def _notes_page(text):
    return {"slideProperties": {"notesPage": {
        "notesProperties": {"speakerNotesObjectId": "n"},
        "pageElements": [{"objectId": "n", "shape": {"text": {"textElements": [
            {"paragraphMarker": {}}, {"textRun": {"content": text}},
        ]}}}],
    }}}


def _slide(elements, notes=""):
    s = {"objectId": "slide1", "pageElements": elements}
    s.update(_notes_page(notes))
    return s


def test_multiple_text_boxes_are_merged_not_collapsed():
    s = _slide([
        _shape(0.5, 0.7, 36, [(None, "My Title")]),       # biggest font -> title
        _shape(2.0, 0.7, 18, [(None, "Box A line")]),     # body box 1
        _shape(3.0, 0.7, 18, [(None, "Box B line")]),     # body box 2 (below)
    ])
    slide = _slide_from_native(s)
    assert slide.title == "My Title"
    texts = [p.text for p in slide.paras if p.text]
    assert texts == ["Box A line", "Box B line"]  # both boxes survive, in reading order


def test_notes_do_not_become_a_table():
    s = _slide([_shape(0.5, 0.7, 28, [(None, "Title")])], notes="these are speaker notes")
    slide = _slide_from_native(s)
    assert slide.notes == "these are speaker notes"
    assert slide.table is None  # regression: notes must not land in the table slot
    assert "| " not in to_slidev(slide)  # and definitely not render as a table


def test_bullet_nesting_is_preserved():
    s = _slide([
        _shape(0.5, 0.7, 30, [(None, "Plan")], placeholder="TITLE"),
        _shape(2.0, 0.7, 18, [(0, "Parent"), (1, "Child"), (2, "Grandchild")]),
    ])
    slide = _slide_from_native(s)
    assert slide.title == "Plan"
    assert [(p.depth, p.text) for p in slide.paras] == [
        (0, "Parent"), (1, "Child"), (2, "Grandchild")]
    md = to_slidev(slide)
    assert "- Parent" in md and "\n  - Child" in md and "\n    - Grandchild" in md


def test_bullets_after_bold_paragraph_header_survive_pull():
    """Interleaved `**Header**` paragraphs + bullet groups must keep every bullet.

    The terminology/glossary deck pattern is a non-bulleted (bold) sub-header
    followed by a bullet list, repeated. Regression guard: pull must not flatten
    the bullets that follow a header into plain paragraphs or drop them.
    """
    s = _slide([
        _shape(0.4, 0.34, 18, [(None, "TERMS")], placeholder="TITLE"),
        _shape(1.0, 0.34, 18, [
            (None, "Prompt calibration"),            # bold header (non-bullet)
            (0, "length-matched"), (0, "score vs boolean"),
            (None, ""),                              # blank spacer line
            (None, "Attack library"),                # bold header (non-bullet)
            (0, "HONLY"), (0, "refuse-then-fall-back"),
        ]),
    ])
    slide = _slide_from_native(s)
    assert [(p.depth >= 0, p.text) for p in slide.paras if p.text] == [
        (False, "Prompt calibration"),
        (True, "length-matched"), (True, "score vs boolean"),
        (False, "Attack library"),
        (True, "HONLY"), (True, "refuse-then-fall-back"),
    ]
    md = to_slidev(slide)
    for bullet in ("length-matched", "score vs boolean", "HONLY",
                   "refuse-then-fall-back"):
        assert f"- {bullet}" in md, f"bullet dropped on pull: {bullet}"


def test_push_bullets_exclude_an_interleaved_bold_header():
    """The push side must not pull a bold header into an adjacent bullet range."""
    from slidesync._sync import _body, parse_body

    _h, paras, *_ = parse_body(
        "**Prompt calibration**\n- a\n- b\n\n**Attack library**\n- c\n- d\n")
    reqs = _body("B", paras)
    text = next(r["insertText"]["text"] for r in reqs if "insertText" in r)
    units = text.encode("utf-16-le")
    covered = "".join(
        units[r["createParagraphBullets"]["textRange"]["startIndex"] * 2:
              r["createParagraphBullets"]["textRange"]["endIndex"] * 2].decode("utf-16-le")
        for r in reqs if "createParagraphBullets" in r)
    assert "Prompt calibration" not in covered
    assert "Attack library" not in covered
    for bullet in ("a", "b", "c", "d"):
        assert bullet in covered


def test_highlight_wash_pulls_back_to_double_equals():
    bg = {"backgroundColor": {"opaqueColor": {"rgbColor": {
        "red": 1.0, "green": 0.8784314, "blue": 0.5411765}}}}
    s = _slide([
        _shape(0.5, 0.7, 30, [(None, "Title")], placeholder="TITLE"),
        {"transform": {"translateY": 2.0 * IN, "translateX": 0.7 * IN},
         "shape": {"text": {"textElements": [
             {"paragraphMarker": {}},
             {"textRun": {"content": "the ", "style": {}}},
             {"textRun": {"content": "headline effect", "style": bg}},
             {"textRun": {"content": " survives\n", "style": {}}}]}}},
    ])
    slide = _slide_from_native(s)
    [run] = [r for p in slide.paras for r in p.runs]
    assert run.style == "highlight"
    assert "the ==headline effect== survives" in to_slidev(slide)


def test_code_span_with_a_wash_stays_code_not_highlight():
    # A foreign code span often carries a grey background — the Mono font must
    # win, or pulled decks would rewrite `code` as ==code==.
    style = {"fontFamily": "Roboto Mono",
             "backgroundColor": {"opaqueColor": {"rgbColor": {"red": 0.9}}}}
    s = _slide([
        _shape(0.5, 0.7, 30, [(None, "Title")], placeholder="TITLE"),
        {"transform": {"translateY": 2.0 * IN, "translateX": 0.7 * IN},
         "shape": {"text": {"textElements": [
             {"paragraphMarker": {}},
             {"textRun": {"content": "pip install\n", "style": style}}]}}},
    ])
    slide = _slide_from_native(s)
    runs = [r for p in slide.paras for r in p.runs]
    assert [r.style for r in runs] == ["code"]


def test_title_placeholder_wins_over_font_size():
    s = _slide([
        _shape(2.0, 0.7, 48, [(None, "Huge body")]),               # bigger font...
        _shape(0.5, 0.7, 18, [(None, "Real title")], placeholder="TITLE"),  # ...but this is TITLE
    ])
    slide = _slide_from_native(s)
    assert slide.title == "Real title"
    assert [p.text for p in slide.paras if p.text] == ["Huge body"]
