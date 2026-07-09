"""[![alt](img)](href) — an image wrapped in a click-through link.

The deck-variant convention embeds a cropped deck figure that links to the
full titled figure. This must parse as an image (not fall through to a body
paragraph, which renders BLANK on a text-free graph template), carry the href
onto the Slides image element as its link, and round-trip through to_slidev.
"""

from slidesync._sync import Slide, build_slides, slide_requests, split_slides, to_slidev

LINKED_MD = """---
theme: seriph
---

---
template: graph
id: fig
---
[![A cropped deck figure](deck.png)](https://example.com/full.png)
"""


def _slide(md):
    return next(s for s in build_slides(split_slides(md)) if s.key == "fig")


def test_linked_image_parses_as_image():
    s = _slide(LINKED_MD)
    assert s.image == "deck.png"
    assert s.image_alt == "A cropped deck figure"
    assert s.image_link == "https://example.com/full.png"
    assert not s.paras  # never a body paragraph


def test_relative_href_keeps_image_but_emits_no_link():
    md = LINKED_MD.replace("https://example.com/full.png", "../figures/full.png")
    s = _slide(md)
    assert s.image == "deck.png"
    assert s.image_link == "../figures/full.png"
    reqs = slide_requests(s, "https://drive/img", (800, 600))
    assert any("createImage" in r for r in reqs)
    assert not any("updateImageProperties" in r for r in reqs)


def test_plain_image_still_parses_without_link():
    s = _slide(LINKED_MD.replace(
        "[![A cropped deck figure](deck.png)](https://example.com/full.png)",
        "![A cropped deck figure](deck.png)"))
    assert s.image == "deck.png"
    assert s.image_link is None


def test_linked_image_round_trips_through_render():
    # non-src render path (pulled slides): the wrapper must be re-emitted
    bare = Slide("fig", "image", image="deck.png",
                 image_alt="A cropped deck figure", image_link="full.png")
    out = to_slidev(bare)
    assert "[![A cropped deck figure](deck.png)](full.png)" in out
    reparsed = _slide("---\ntheme: seriph\n---\n\n---\ntemplate: graph\nid: fig\n---\n"
                      + "[![A cropped deck figure](deck.png)](full.png)\n")
    assert (reparsed.image, reparsed.image_link) == ("deck.png", "full.png")


def test_push_requests_set_the_image_link():
    s = _slide(LINKED_MD)
    reqs = slide_requests(s, "https://drive/img", (800, 600))
    create = next(r for r in reqs if "createImage" in r)
    iid = create["createImage"]["objectId"]
    link = next(r for r in reqs if "updateImageProperties" in r)
    assert link["updateImageProperties"]["objectId"] == iid
    assert (link["updateImageProperties"]["imageProperties"]["link"]["url"]
            == "https://example.com/full.png")
