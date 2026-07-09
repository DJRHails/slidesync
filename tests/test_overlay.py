"""```gslides-overlay```: literal Slides API requests replayed ON TOP of a
templated slide's render.

Unlike ```gslides``` (whole-slide custom, pull-authoritative), the overlay
rides on a normal templated/generative slide: push appends its requests (with
`__PAGE__` substituted) after the slide's own render, the block is part of the
content hash (edits re-push), and — because the authored source round-trips
through the notes marker — `pull` reconstructs the fence verbatim. Native
edits to the drawn elements are NOT captured back; the markdown is the source
of truth. Drift comparison counts the overlay's insertText lines as visible
text (and never its raw JSON), so an overlaid slide reads as clean.

These are offline: the markdown parse/render tests need no API; the push/pull
tests drive the in-memory Slides+Drive fake from `test_e2e_scenarios`.
"""

import json

import test_e2e_scenarios as e2e
from test_atomic_swap import RecordingSlides

from slidesync import _sync
from slidesync._sync import (
    _content_lines,
    build_slides,
    load_slides,
    pull_slides,
    push,
    slide_requests,
    split_slides,
    to_slidev,
)

DECK = "fakedeck"

OVERLAY_BLOCK = """```gslides-overlay
{"requests": [
  {"createShape": {"objectId": "__PAGE___note", "shapeType": "TEXT_BOX",
    "elementProperties": {"pageObjectId": "__PAGE__",
      "size": {"width": {"magnitude": 3000000, "unit": "EMU"},
               "height": {"magnitude": 400000, "unit": "EMU"}},
      "transform": {"scaleX": 1, "scaleY": 1, "translateX": 400000,
                    "translateY": 300000, "unit": "EMU"}}}},
  {"insertText": {"objectId": "__PAGE___note", "text": "Thinking On"}}
]}
```"""

OVERLAY_MD = f"""---
theme: seriph
---

---
template: topic
id: annotated
---
# A headline
## SECTION

A body point.

{OVERLAY_BLOCK}
"""


def _slide(md, key):
    return next(s for s in build_slides(split_slides(md)) if s.key == key)


# --- markdown parse / render (no API) ---------------------------------------

def test_overlay_parses_without_flipping_to_custom():
    s = _slide(OVERLAY_MD, "annotated")
    assert s.template_name == "topic"
    assert s.custom is None
    assert s.overlay is not None
    assert "Thinking On" in s.overlay
    # the JSON never leaks into the slide's visible paragraphs
    assert all("createShape" not in p.text for p in s.paras)


def test_overlay_on_custom_slide_is_ignored():
    md = OVERLAY_MD.replace("template: topic", "template: whatever").replace(
        "A body point.",
        'A body point.\n\n```gslides\n[{"createLine": {"objectId": "__PAGE___l"}}]\n```')
    s = _slide(md, "annotated")
    assert s.custom is not None
    assert s.overlay is None


def test_overlay_round_trips_through_render():
    s = _slide(OVERLAY_MD, "annotated")
    out = to_slidev(s)
    assert "```gslides-overlay" in out
    reparsed = next(sl for sl in build_slides(split_slides(
        "---\ntheme: seriph\n---\n\n---\n" + out)) if sl.key == "annotated")
    assert reparsed.overlay == s.overlay
    assert reparsed.object_id == s.object_id


def test_overlay_edit_moves_the_content_hash():
    plain = _slide(OVERLAY_MD.replace(OVERLAY_BLOCK, ""), "annotated")
    overlaid = _slide(OVERLAY_MD, "annotated")
    edited = _slide(OVERLAY_MD.replace("Thinking On", "Thinking Off"), "annotated")
    assert len({plain.object_id, overlaid.object_id, edited.object_id}) == 3
    assert overlaid.semantic() != edited.semantic()


def test_overlay_survives_a_prompt_slide_fence():
    # overlay is extracted before the prompt/code verbatim fence grab, so the
    # verbatim body is the ``` block, not the overlay JSON — in either order.
    md = OVERLAY_MD.replace("template: topic", "template: prompt").replace(
        "A body point.", "A body point.\n\n```\nverbatim prompt text\n```")
    s = _slide(md, "annotated")
    assert s.overlay is not None
    assert s.verbatim == "verbatim prompt text"


# --- push requests -----------------------------------------------------------

def test_push_requests_append_substituted_overlay():
    s = _slide(OVERLAY_MD, "annotated")
    reqs = slide_requests(s, None, None)
    tail = json.dumps(reqs[-2:])
    assert "__PAGE__" not in tail
    assert f"{s.object_id}_note" in tail
    assert reqs[-1]["insertText"]["text"] == "Thinking On"
    # the templated render is still there ahead of the overlay
    assert any("createSlide" in r for r in reqs)


def test_invalid_overlay_json_degrades_to_base_render():
    md = OVERLAY_MD.replace('{"requests": [', '{"requests": [oops')
    s = _slide(md, "annotated")
    reqs = slide_requests(s, None, None)
    assert any("createSlide" in r for r in reqs)
    assert "_note" not in json.dumps(reqs)


# --- drift comparison ---------------------------------------------------------

def test_content_lines_count_overlay_text_not_json():
    s = _slide(OVERLAY_MD, "annotated")
    lines = _content_lines(s.src, s.template_name)
    assert "Thinking On" in lines
    assert not any("createShape" in ln for ln in lines)


def test_content_lines_on_text_free_graph_template():
    md = OVERLAY_MD.replace("template: topic", "template: graph")
    s = _slide(md, "annotated")
    # graph slides render no markdown text; only the overlay's text is visible
    assert _content_lines(s.src, s.template_name) == ["Thinking On"]


# --- push / pull round-trip (offline fake) ------------------------------------

def test_overlay_push_pull_round_trip(tmp_path, monkeypatch):
    store, drive = e2e.FakeStore(), e2e.FakeDrive()
    slides_api = RecordingSlides(store)
    monkeypatch.setattr(_sync, "get_services", lambda account: (slides_api, drive))
    path = tmp_path / "deck.slidev.md"
    path.write_text(OVERLAY_MD)
    push(slides_api, drive, DECK, load_slides(path), anchor=None, prune=False,
         base_dir=tmp_path)
    local = _slide(OVERLAY_MD, "annotated")
    assert any(s["id"] == local.object_id for s in store.slides)
    drawn = next(s for s in store.slides if s["id"] == local.object_id)
    assert drawn["shapes"][f"{local.object_id}_note"]["text"] == "Thinking On"
    pulled = next(s for s in pull_slides(slides_api, DECK) if s.key == "annotated")
    assert pulled.overlay == local.overlay
    assert "```gslides-overlay" in to_slidev(pulled)
