"""Comments round-trip as comments, in place — not one merged speaker-notes blob.

The authored body of every non-custom slide is stored (base64) in the `s2g`
marker; `pull` re-emits it verbatim, so comment positions, prompt fences, and
formatting quirks all survive. Speaker notes edited live in Slides surface as
one extra trailing comment instead of silently replacing the authored ones.
"""

from slidesync._sync import (
    STYLES,
    _marker,
    _read_marker,
    _slide_from_marker,
    build_slides,
    split_slides,
    to_slidev,
)

DECK = """---
theme: seriph
---

---
template: topic
id: thread-finding
---

# Compression hurts recall
## FINDING

<!-- framing: say this slowly -->

- one solid point

![self-titled figure](../figures/fig.png)

<!-- data-source: scripts/fig.py -->

---
template: prompt
id: appendix-prompt-monitor
---

## MONITOR PROMPT

```text
You are a monitor. Score 0-100 in <verdict></verdict>.
```

---
template: dark
id: weekly
---

# 2026/06/12
## WEEKLY UPDATE

Touchstone · Daniel Hails
"""


def _slide(key):
    return next(s for s in build_slides(split_slides(DECK)) if s.key == key)


def _roundtrip(slide, notes=None):
    marker = _read_marker(_marker(slide))
    return _slide_from_marker(marker, slide.notes if notes is None else notes)


def test_marker_parses_despite_comment_terminators_in_src():
    # The authored body contains comments (`-->`) and braces; base64 keeps the
    # marker JSON immune to `}` + `-->` sequences that would truncate the
    # delimiter-based MARKER_RE match mid-string.
    slide = _slide("thread-finding")
    slide.src += "\n<!-- edge: {brace} -->"
    marker = _read_marker(_marker(slide))
    assert marker.get("src"), "marker should carry the authored source"


def test_comments_survive_in_place_not_merged():
    md = to_slidev(_roundtrip(_slide("thread-finding")))
    assert md.count("<!--") == 2, "each comment stays its own comment"
    assert md.index("framing: say this slowly") < md.index("one solid point")
    assert md.index("data-source: scripts/fig.py") > md.index("../figures/fig.png")


def test_prompt_fence_survives_pull():
    md = to_slidev(_roundtrip(_slide("appendix-prompt-monitor")))
    assert "You are a monitor." in md  # previously lost: marker had no body


def test_unchanged_notes_are_not_duplicated():
    slide = _slide("thread-finding")
    # The notes shape flattens paragraphs to spaces when read back.
    flattened = " ".join(slide.notes.split())
    md = to_slidev(_roundtrip(slide, notes=flattened))
    assert md.count("<!--") == 2


def test_live_notes_edit_appends_trailing_comment():
    slide = _slide("thread-finding")
    md = to_slidev(_roundtrip(slide, notes=slide.notes + "\nadded in slides"))
    assert md.count("<!--") == 3
    assert md.rstrip().endswith("-->") and "added in slides" in md


def _rendered(slide):
    slide.object_id = "s2g_aaaaaaaaaa_bbbbbbbbbb"
    return _styled(slide)


def _styled(slide):
    from slidesync._sync import _styled_requests

    return _styled_requests(slide, STYLES["dark"], None, None)


def test_dark_template_renders_body_as_byline():
    reqs = _rendered(_slide("weekly"))
    boxes = [r["createShape"]["objectId"] for r in reqs if "createShape" in r]
    assert "s2g_aaaaaaaaaa_bbbbbbbbbb_by" in boxes
    texts = [r["insertText"]["text"] for r in reqs if "insertText" in r]
    assert "Touchstone · Daniel Hails" in texts


def test_dark_template_without_body_has_no_byline():
    slide = _slide("weekly")
    slide.paras = []
    reqs = _rendered(slide)
    boxes = [r["createShape"]["objectId"] for r in reqs if "createShape" in r]
    assert not [b for b in boxes if b.endswith("_by")]
