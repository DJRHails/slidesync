"""Tests for ```mermaid``` block detection, rendering, and caching."""

import slidesync._mermaid as mermaid
from slidesync._sync import build_slides, split_slides, to_slidev

DECK = """---
id: arch
---

## Architecture

```mermaid
graph TD
  A[Source] --> B[slidesync]
  B --> C[Google Slides]
```

<!-- walk through the data flow -->
"""


def _slide():
    return build_slides(split_slides(DECK))[0]


def test_mermaid_block_extracted_to_layout_image():
    slide = _slide()
    assert slide.layout == "image"  # routes through the image-insert path on push
    assert slide.image is None      # not a file path — rendered lazily on push
    assert "graph TD" in slide.mermaid
    assert "A[Source] --> B[slidesync]" in slide.mermaid


def test_mermaid_source_kept_out_of_visible_body():
    slide = _slide()
    assert slide.title == "Architecture"
    assert all("graph TD" not in p.text for p in slide.paras)


def test_mermaid_source_survives_round_trip_through_authored_src():
    # `to_slidev` emits the authored markdown verbatim, so the fenced block (and
    # thus the content hash) tracks the diagram source, not the rendered PNG.
    md = to_slidev(_slide())
    assert "```mermaid" in md
    assert "graph TD" in md


def test_render_caches_by_block_hash(tmp_path, monkeypatch):
    monkeypatch.setattr(mermaid, "MERMAID_CACHE_DIR", tmp_path)
    calls = []
    monkeypatch.setattr(mermaid.shutil, "which", lambda _name: None)  # force kroki
    monkeypatch.setattr(
        mermaid, "_render_kroki",
        lambda src: calls.append(src) or b"\x89PNG\r\n\x1a\nFAKE",
    )
    first = mermaid.render_mermaid("graph TD\n A --> B")
    second = mermaid.render_mermaid("graph TD\n A --> B")
    assert first == second  # same path
    assert first.exists()
    assert len(calls) == 1  # second call served from the on-disk cache


def test_render_failure_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(mermaid, "MERMAID_CACHE_DIR", tmp_path)
    monkeypatch.setattr(mermaid.shutil, "which", lambda _name: None)
    monkeypatch.setattr(mermaid, "_render_kroki", lambda src: None)
    assert mermaid.render_mermaid("graph TD\n A --> B") is None
