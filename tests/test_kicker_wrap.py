"""A wrapping kicker must reserve its true height, never overlap the headline.

The brand-kit kicker used to be a fixed 0.5in box with a one-line 0.36in
advance; a long kicker (e.g. `EXPERIMENT · ...` thread eyebrows) wrapped to a
second line that collided with the headline below. The layout now estimates
the kicker's wrapped line count and advances by its real height — one-line
kickers keep (approximately) the legacy geometry.
"""

from slidesync._sync import build_slides, slide_requests, split_slides

EMU_PER_IN = 914400

SHORT_KICKER = "EXPERIMENT · SHORT"
LONG_KICKER = ("EXPERIMENT · ONE CLUSTER-MEMBER HARM PRESENT, PROBE TIGHT / "
               "MID / ISOLATED ABSENT LABELS")


def _topic_requests(kicker):
    md = f"""---
theme: seriph
---

---
template: topic
id: probe
---
# Cross-firing lands wherever guilt exists, blind to label distance
## {kicker}
"""
    slide = next(s for s in build_slides(split_slides(md)) if s.key == "probe")
    return slide, slide_requests(slide, None, None)


def _box_geom(reqs, suffix, sid):
    shape = next(r["createShape"] for r in reqs if "createShape" in r
                 and r["createShape"]["objectId"] == sid + suffix)
    ep = shape["elementProperties"]
    return (ep["transform"]["translateY"] / EMU_PER_IN,
            ep["size"]["height"]["magnitude"] / EMU_PER_IN)


def test_single_line_kicker_keeps_legacy_geometry():
    slide, reqs = _topic_requests(SHORT_KICKER)
    k_y, k_h = _box_geom(reqs, "_k", slide.object_id)
    h_y, _ = _box_geom(reqs, "_h", slide.object_id)
    assert abs(k_h - 0.5) < 0.01
    assert abs((h_y - k_y) - 0.36) < 0.01


def test_wrapping_kicker_pushes_the_headline_below_it():
    slide, reqs = _topic_requests(LONG_KICKER)
    k_y, k_h = _box_geom(reqs, "_k", slide.object_id)
    h_y, _ = _box_geom(reqs, "_h", slide.object_id)
    # two 18pt lines are ~0.625in of text; the headline must start after them
    assert h_y - k_y > 0.625
    assert k_h > 0.625
    # and the reserved box tracks the advance (constant slack, no overlap)
    assert h_y - k_y > k_h - 0.19
