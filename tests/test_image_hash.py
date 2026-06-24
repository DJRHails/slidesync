"""Offline tests for the image-bytes content-hash fold (no network/auth).

Regression for the headline diff bug: a figure regenerated *in place* (same
path + alt text, new pixels) left the canonical markdown byte-identical, so
`content_hash` never moved and the slide was skipped — the new figure never
reached Slides. `_finalize` now folds a hash of the image FILE bytes into
`content_hash`, so regenerated pixels move the hash and the slide is replaced.
"""

from pathlib import Path

from slidesync._sync import (
    Slide,
    _finalize,
    _image_bytes_hash,
    load_slides,
    managed_slides,
    plan_sync,
)

# Two distinct 1x1 PNGs (different pixel payloads, both valid PNG files).
PNG_RED = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
    "53de0000000c4944415408d76360f8cf000000030101001827df8e0000000049454e44ae426082"
)
PNG_BLUE = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
    "53de0000000c4944415408d76360606000000003000100b0e0e7110000000049454e44ae426082"
)

DECK = """---
theme: seriph
---

---
id: figure-slide
---
## A Figure

![a chart](chart.png)
"""


def _write_deck(tmp_path: Path, png: bytes) -> Path:
    (tmp_path / "chart.png").write_bytes(png)
    path = tmp_path / "deck.slidev.md"
    path.write_text(DECK)
    return path


def test_image_bytes_change_moves_content_hash(tmp_path):
    path = _write_deck(tmp_path, PNG_RED)
    before = load_slides(path)[0].content_hash

    (tmp_path / "chart.png").write_bytes(PNG_BLUE)  # regenerate the figure in place
    after = load_slides(path)[0].content_hash

    assert before != after, "regenerated image bytes must change content_hash"


def test_identical_image_bytes_keep_content_hash_stable(tmp_path):
    path = _write_deck(tmp_path, PNG_RED)
    first = load_slides(path)[0].content_hash
    second = load_slides(path)[0].content_hash  # same bytes, no edit
    assert first == second, "unchanged bytes must keep the hash stable (no churn)"


def test_regenerated_image_is_planned_for_replace(tmp_path):
    """End-to-end of the bug: with the OLD behaviour this slide was SKIPPED."""
    path = _write_deck(tmp_path, PNG_RED)
    old = load_slides(path)[0]
    # Simulate the deck as last pushed (old object_id is what Slides holds).
    pres = {"slides": [{"objectId": old.object_id}]}
    managed = managed_slides(slides_api=None, deck=DECK, pres=pres)

    (tmp_path / "chart.png").write_bytes(PNG_BLUE)  # new pixels, same path/alt
    new = load_slides(path)
    creates, deletes, skips, _pruned = plan_sync(new, managed, prune=False)

    assert [s.key for s in creates] == ["figure-slide"], "must re-create the slide"
    assert deletes == [old.object_id], "must delete the stale-pixel slide"
    assert skips == [], "must NOT skip a regenerated figure"


def test_missing_image_file_does_not_crash(tmp_path):
    path = tmp_path / "deck.slidev.md"
    path.write_text(DECK)  # no chart.png on disk
    slides = load_slides(path)  # must not raise
    assert slides[0].key == "figure-slide"
    assert slides[0].content_hash  # still finalized, hash falls back to the path


def test_image_bytes_hash_resolves_relative_to_source_file(tmp_path):
    sub = tmp_path / "figs"
    sub.mkdir()
    (sub / "chart.png").write_bytes(PNG_RED)
    slide = Slide(key="s", layout="image", image="figs/chart.png")
    slide.src_path = tmp_path / "deck.slidev.md"
    assert _image_bytes_hash(slide) is not None  # found via src_path.parent


def test_slide_without_image_has_no_bytes_hash():
    slide = _finalize(Slide(key="s", layout="content", title="No image"))
    assert _image_bytes_hash(slide) is None
    assert slide.content_hash  # still finalized normally
