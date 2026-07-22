"""Template inference (on by default, `infer: false` opts out) and derived
slide ids — no network/auth.

Template inference is the default: untagged slides get a `template:` from
their shape (first slide -> dark title card, `##`-over-`#` -> dark divider,
`#` headline -> topic, lone `##` -> content, lone figure -> graph, fenced
block -> prompt); a file opts out with `infer: false` in its file-level
frontmatter. Id derivation is always on: an id-less slide is keyed by its
`#` headline slug, else its `##` title slug, else its figure filename stem.
"""

import pytest

from slidesync._sync import (
    _append_to_slide_body,
    _derive_key,
    _file_infer,
    build_slides,
    load_deck,
    split_slides,
)

INFER_DECK = """---
theme: seriph
infer: true
---

---
---
# 2026/07/23
## RELIABLE MONITORS

Weekly Update

---
---
## PRIMARY WORK
# CROSS-FIRES

---
---
# Prompt optimisation works
## FINDING · PROMPT-OPT

- one body bullet

---
---
## Key for this week

- a content bullet

---
---
[![Cross-fire climbs with density](../figures/overfire_by_density_deck.png)](../figures/overfire_by_density.png)

[trace →](https://example.com)

---
---
## THE PROMPT

```text
You are a monitor. Score the transcript.
```

---
---
## The numbers

| Metric | Value |
| --- | --- |
| AUROC | 0.93 |
"""


def _keyed(md):
    return {s.key: s for s in build_slides(split_slides(md), infer=_file_infer(md))}


def test_infer_flag_reads_file_frontmatter():
    assert _file_infer(INFER_DECK)
    assert _file_infer(INFER_DECK.replace("infer: true\n", ""))  # on by default
    assert not _file_infer(INFER_DECK.replace("infer: true\n", "infer: false\n"))


def test_inferred_templates_by_shape():
    slides = build_slides(split_slides(INFER_DECK), infer=True)
    assert [s.template_name for s in slides] == [
        "dark",     # first slide = title card
        "dark",     # ## kicker above # headline = section divider
        "topic",    # # headline (+ kicker + body)
        "content",  # lone ## title + bullets
        "graph",    # a figure and no headings
        "prompt",   # fenced block
        None,       # table -> generative layout (styled templates drop tables)
    ]


def test_without_infer_untagged_slides_stay_untagged():
    slides = build_slides(split_slides(INFER_DECK), infer=False)
    assert {s.template_name for s in slides} == {None}


def test_explicit_template_and_layout_win_over_inference():
    md = ("---\ntheme: seriph\n---\n\n---\ntemplate: question\n---\n"
          "# Not a topic\n\nbody\n\n"
          "---\nlayout: section\n---\n# Not a topic either\n")
    slides = build_slides(split_slides(md), infer=True)
    # index 0 would infer dark; the explicit tags must win
    assert slides[0].template_name == "question"
    assert slides[1].template_name is None
    assert slides[1].layout_name == "section"


def test_mermaid_fence_is_not_a_prompt():
    md = "# T\n\nbody\n\n---\n---\n```mermaid\ngraph TD; A-->B\n```\n"
    slides = build_slides(split_slides(md), infer=True)
    assert slides[1].template_name is None


def test_derived_key_prefers_h1_then_h2_then_image_stem():
    assert _derive_key("## KICKER\n# Cross-Fires!\n\nbody") == "cross-fires"
    assert _derive_key("## Key for this week\n\n- b") == "key-for-this-week"
    assert _derive_key("![alt](../figs/overfire_by_density_deck.png)") == (
        "overfire-by-density")
    assert _derive_key("just a paragraph") == ""


def test_derived_key_ignores_comments_and_fences():
    body = "<!-- # not me -->\n```text\n# nor me\n```\n## The Real Title\n"
    assert _derive_key(body) == "the-real-title"


def test_slides_are_keyed_by_derived_id():
    slides = _keyed(INFER_DECK)
    assert "cross-fires" in slides
    assert "prompt-optimisation-works" in slides
    assert "key-for-this-week" in slides
    assert "overfire-by-density" in slides  # figure stem, `_deck` stripped


def test_multi_file_namespacing_and_links_use_derived_ids(tmp_path):
    deck = tmp_path / "2026-07-23.slidev.md"
    deck.write_text(
        "---\ninfer: true\n---\n\n---\n---\n# Title Card\n\n---\n---\n"
        "## Overview\n\n- see [the divider](#cross-fires)\n\n---\n---\n"
        "## PRIMARY WORK\n# CROSS-FIRES\n")
    other = tmp_path / "2026-07-16.slidev.md"
    other.write_text("---\n---\n# Old Title\n")
    optout = tmp_path / "2026-07-09.slidev.md"
    optout.write_text("---\ninfer: false\n---\n\n---\n---\n# Older Title\n")
    slides = load_deck([deck, other, optout])
    keys = [s.key for s in slides]
    assert "2026-07-23-cross-fires" in keys
    assert "2026-07-16-old-title" in keys
    overview = next(s for s in slides if s.key == "2026-07-23-overview")
    link = next(r for p in overview.paras for r in p.runs if r.style == "link")
    assert link.link == "#2026-07-23-cross-fires"
    divider = next(s for s in slides if s.key == "2026-07-23-cross-fires")
    assert divider.template_name == "dark"
    old = next(s for s in slides if s.key == "2026-07-16-old-title")
    assert old.template_name == "dark"  # inference is the default per file
    older = next(s for s in slides if s.key == "2026-07-09-older-title")
    assert older.template_name is None  # infer: false opts a file out


def test_duplicate_derived_ids_are_rejected(tmp_path):
    deck = tmp_path / "d.slidev.md"
    deck.write_text("---\n---\n# Same Title\n\n---\n---\n# Same Title\n")
    with pytest.raises(SystemExit):
        load_deck([deck])


def test_capture_anchors_into_an_id_less_slide():
    text = INFER_DECK
    out = _append_to_slide_body(text, "cross-fires", "<!-- @Ted: nice -->")
    assert "<!-- @Ted: nice -->" in out
    before, after = out.split("<!-- @Ted: nice -->")
    assert "# CROSS-FIRES" in before  # appended to the divider's body...
    assert "# Prompt optimisation works" in after  # ...before the next slide


def test_removing_matching_boilerplate_keeps_object_id():
    tagged = ("---\ninfer: true\n---\n\n---\n---\n# Card\n\n---\n"
              "template: topic\nid: prompt-optimisation-works\n---\n"
              "# Prompt optimisation works\n## FINDING\n\n- bullet\n")
    bare = ("---\ninfer: true\n---\n\n---\n---\n# Card\n\n---\n---\n"
            "# Prompt optimisation works\n## FINDING\n\n- bullet\n")
    slide = _keyed(tagged)["prompt-optimisation-works"]
    stripped = _keyed(bare)["prompt-optimisation-works"]
    assert stripped.template_name == "topic"
    assert stripped.object_id == slide.object_id
