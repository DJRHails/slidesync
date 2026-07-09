"""Graph-slide link footer: the single link-only line renders bottom-right.

The trace-link convention line ([a →](url) · [b →](url)) on a `graph`/`full`
slide renders as one right-aligned 11pt footer strip. A wide image whose
centred fit already leaves the strip free keeps its full size; an image tall
enough to collide shrinks to make room. More than one link-only line is a
slot-validation error.
"""

import json

from slidesync._sync import (
    EMU_PER_IN,
    SLIDE_H,
    _content_lines,
    build_slides,
    slide_requests,
    split_slides,
    validate_slots,
)

LINKS = ("[each harm asked on its own →](https://x/a) · "
         "[all harms in one question →](https://x/b)")


def _slide(body):
    md = f"""---
theme: seriph
---

---
template: graph
id: fig
---
{body}
"""
    return next(s for s in build_slides(split_slides(md)) if s.key == "fig")


def _img_and_footer(reqs):
    img = next((r["createImage"] for r in reqs if "createImage" in r), None)
    box = next((r["createShape"] for r in reqs if "createShape" in r
                and r["createShape"]["objectId"].endswith("_links")), None)
    return img, box


def test_footer_box_renders_bottom_right_aligned():
    s = _slide(f"![f](fig.png)\n\n{LINKS}")
    reqs = slide_requests(s, "https://drive/img", (2000, 1000))
    _img, box = _img_and_footer(reqs)
    assert box is not None
    y = box["elementProperties"]["transform"]["translateY"] / EMU_PER_IN
    assert y > 5.0  # bottom strip
    align = next(r for r in reqs if "updateParagraphStyle" in r
                 and r["updateParagraphStyle"]["objectId"].endswith("_links"))
    assert align["updateParagraphStyle"]["style"]["alignment"] == "END"
    blob = json.dumps(reqs)
    assert "each harm asked on its own" in blob


def test_wide_image_keeps_full_size():
    plain = slide_requests(_slide("![f](fig.png)"), "https://drive/img", (2000, 750))
    with_links = slide_requests(_slide(f"![f](fig.png)\n\n{LINKS}"),
                                "https://drive/img", (2000, 750))
    size = lambda reqs: _img_and_footer(reqs)[0]["elementProperties"]["size"]
    assert size(plain) == size(with_links)  # footer fits in the free strip


def test_tall_image_shrinks_and_clears_the_footer():
    plain = slide_requests(_slide("![f](fig.png)"), "https://drive/img", (1000, 1000))
    with_links = slide_requests(_slide(f"![f](fig.png)\n\n{LINKS}"),
                                "https://drive/img", (1000, 1000))
    h = lambda reqs: _img_and_footer(reqs)[0]["elementProperties"]["size"]["height"]["magnitude"]
    assert h(with_links) < h(plain)
    img, box = _img_and_footer(with_links)
    img_bottom = (img["elementProperties"]["transform"]["translateY"]
                  + img["elementProperties"]["size"]["height"]["magnitude"])
    assert img_bottom <= box["elementProperties"]["transform"]["translateY"]


def test_footer_counts_as_rendered_text_for_drift():
    s = _slide(f"![f](fig.png)\n\n{LINKS}")
    lines = _content_lines(s.src, s.template_name)
    assert any("each harm asked on its own" in ln for ln in lines)


def test_two_link_lines_are_flagged():
    s = _slide(f"![f](fig.png)\n\n{LINKS}\n\n[more →](https://x/c)")
    problems = validate_slots([s])
    assert len(problems) == 1
    assert "only ONE footer" in problems[0]


def test_no_links_means_no_footer_and_no_reserved_space():
    reqs = slide_requests(_slide("![f](fig.png)"), "https://drive/img", (1000, 1000))
    img, box = _img_and_footer(reqs)
    assert box is None  # no footer element at all
    # full-bleed fit: the square image uses the full 0.1in-margin height
    h = img["elementProperties"]["size"]["height"]["magnitude"]
    assert h == SLIDE_H - 2 * int(0.1 * EMU_PER_IN)
