"""A slide with no title must not emit style requests against its empty TITLE box.

The Slides API rejects `updateTextStyle` on a shape holding no text ("The object
(..._t) has no text", HTTP 400) and the whole batchUpdate aborts — one comment-only
placeholder slide, bare `$$` equation, or bullets-only body used to kill the entire
push. `_insert` already skips empty text; the title styling must skip with it.
"""

from slidesync._sync import build_slides, slide_requests, split_slides

DECK = r"""---
theme: seriph
infer: true
---

---
id: todo-placeholder
---

<!-- a comment-only placeholder slide: no title, no body -->

---
id: bullets-only
---

- first caveat, no heading anywhere
- second caveat

---
id: bare-equation
---

$$z = 0.55 \, z_{a} + 0.45 \, z_{b}$$

---
id: titled-control
---

## A titled slide

Body text.
"""


def _slides():
    return build_slides(split_slides(DECK))


def _title_style_targets(slide):
    reqs = slide_requests(slide, None, None)
    tid = slide.object_id + "_t"
    return [
        r
        for r in reqs
        for kind in ("updateTextStyle", "updateParagraphStyle")
        if kind in r and r[kind].get("objectId") == tid
    ]


def test_titleless_slides_emit_no_title_style_requests():
    for key in ("todo-placeholder", "bullets-only", "bare-equation"):
        slide = next(s for s in _slides() if s.key == key)
        assert slide.title == "", f"{key} should parse title-less"
        assert _title_style_targets(slide) == [], (
            f"{key}: style request against the empty TITLE box would 400 the batchUpdate"
        )


def test_titled_slide_still_styles_its_title():
    slide = next(s for s in _slides() if s.key == "titled-control")
    assert slide.title
    assert _title_style_targets(slide), "titled slides must keep their kicker styling"
