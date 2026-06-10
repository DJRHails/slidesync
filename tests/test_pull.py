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


def test_title_placeholder_wins_over_font_size():
    s = _slide([
        _shape(2.0, 0.7, 48, [(None, "Huge body")]),               # bigger font...
        _shape(0.5, 0.7, 18, [(None, "Real title")], placeholder="TITLE"),  # ...but this is TITLE
    ])
    slide = _slide_from_native(s)
    assert slide.title == "Real title"
    assert [p.text for p in slide.paras if p.text] == ["Huge body"]
