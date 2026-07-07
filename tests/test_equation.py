"""Tests for display-math `$$...$$` detection, round-trip, and rendering.

Offline except the render tests, which exercise the real matplotlib mathtext
backend (a hard dependency, no network needed).
"""

import base64
import json

import slidesync._equation as equation
from slidesync._equation import render_equation
from slidesync._sync import (
    _marker,
    _read_marker,
    _slide_from_native,
    build_slides,
    slide_requests,
    split_slides,
    to_slidev,
)

DECK = r"""---
theme: seriph
---

---
id: effectiveness
---

## Monitor effectiveness

Offline monitoring is a product of four numbers:

$$
E = p_{mon} \times r_{mon} \times (1 - FPR) \times r_{hum}
$$

<!-- talk through each factor -->

---
template: content
id: pair
---

## Two equations

$$e^{i\pi} + 1 = 0$$

$$
\frac{a}{b} \approx \max_i x_i
$$
"""


def _slides():
    return build_slides(split_slides(DECK))


def _slide(key):
    return next(s for s in _slides() if s.key == key)


def test_equation_block_extracted_verbatim():
    slide = _slide("effectiveness")
    assert slide.equations == ["\nE = p_{mon} \\times r_{mon} \\times (1 - FPR)"
                               " \\times r_{hum}\n"]
    assert slide.title == "Monitor effectiveness"
    assert all("$$" not in p.text and "p_{mon}" not in p.text
               for p in slide.paras)  # LaTeX kept out of the visible body


def test_multiple_blocks_single_and_multi_line():
    slide = _slide("pair")
    assert slide.equations == [
        "e^{i\\pi} + 1 = 0",                       # single-line form
        "\n\\frac{a}{b} \\approx \\max_i x_i\n",   # multi-line form
    ]


def test_source_survives_round_trip_through_authored_src():
    # `to_slidev` emits the authored markdown verbatim, so the `$$` block (and
    # thus the content hash) tracks the LaTeX source, not the rendered PNG.
    md = to_slidev(_slide("effectiveness"))
    assert "$$\nE = p_{mon}" in md
    assert md.count("$$") == 2


def test_reconstructed_render_is_byte_identical():
    # A pulled slide has no authored src; emission must still reproduce the
    # exact authored block — single-line stays single-line, multi-line multi.
    slide = _slide("pair")
    slide.src = None
    md = to_slidev(slide)
    assert "$$e^{i\\pi} + 1 = 0$$" in md
    assert "$$\n\\frac{a}{b} \\approx \\max_i x_i\n$$" in md


def test_editing_an_equation_moves_the_content_hash():
    a = _slide("pair")
    b = build_slides(split_slides(DECK.replace("+ 1 = 0", "+ 1 = 0.001")))[1]
    assert a.key == b.key and a.key_hash == b.key_hash
    assert a.content_hash != b.content_hash  # an equation edit replaces the slide


def test_marker_stashes_sources_base64():
    marker = _read_marker(_marker(_slide("pair")))
    decoded = [base64.b64decode(e).decode() for e in marker["eq"]]
    assert decoded == _slide("pair").equations


def test_marker_json_survives_braces_in_latex():
    # `}` runs in LaTeX must not truncate the delimiter-based marker regex —
    # that's why the sources are base64: the marker must stay parseable JSON.
    marker = _read_marker(_marker(_slide("effectiveness")))
    assert marker["id"] == "effectiveness" and "at" in marker
    assert json.dumps(marker)  # fully-formed, not truncated mid-string


def test_pull_restores_equations_and_skips_their_images():
    slide = _slide("effectiveness")
    native = {
        "objectId": slide.object_id,
        "pageElements": [
            {"objectId": slide.object_id + "_eq0",
             "image": {"contentUrl": "https://ephemeral/eq.png"}},
        ],
        "slideProperties": {"notesPage": {
            "notesProperties": {"speakerNotesObjectId": "n"},
            "pageElements": [{"objectId": "n", "shape": {"text": {"textElements": [
                {"paragraphMarker": {}},
                {"textRun": {"content": _marker(slide)}},
            ]}}}],
        }},
    }
    pulled = _slide_from_native(native)
    assert pulled.equations == slide.equations       # verbatim from the marker
    assert pulled.image is None                      # eq image != slide image
    assert "$$" in to_slidev(pulled)


def test_semantic_includes_equations():
    a, b = _slide("pair"), _slide("pair")
    b.equations = list(b.equations) + ["x"]
    assert a.semantic() != b.semantic()


def _fake_resolved(slide):
    return [(src, f"https://img/{i}", (900, 300))
            for i, src in enumerate(slide.equations)]


def test_generative_path_places_centred_equation_stack():
    slide = _slide("effectiveness")
    reqs = slide_requests(slide, None, None,
                          equations=_fake_resolved(slide))
    imgs = [r["createImage"] for r in reqs if "createImage" in r]
    assert [i["objectId"] for i in imgs] == [slide.object_id + "_eq0"]
    alt = next(r["updatePageElementAltText"] for r in reqs
               if "updatePageElementAltText" in r)
    assert alt["description"].startswith("$$E = p_{mon}")


def test_styled_template_places_equations_below_body():
    slide = _slide("pair")
    reqs = slide_requests(slide, None, None,
                          equations=_fake_resolved(slide))
    ids = [r["createImage"]["objectId"] for r in reqs if "createImage" in r]
    assert ids == [slide.object_id + "_eq0", slide.object_id + "_eq1"]
    ys = [r["createImage"]["elementProperties"]["transform"]["translateY"]
          for r in reqs if "createImage" in r]
    assert ys[0] < ys[1]  # stacked, not overlapping


def test_render_produces_transparent_png(tmp_path, monkeypatch):
    monkeypatch.setattr(equation, "EQUATION_CACHE_DIR", tmp_path)
    out = render_equation(r"\frac{a}{b} \approx x_i \times \max(y)"
                          r" \; \text{per transcript}")
    assert out is not None and out.exists()
    head = out.read_bytes()[:26]
    assert head[:8] == b"\x89PNG\r\n\x1a\n"
    assert head[25] == 6  # colour type RGBA -> transparent background


def test_render_caches_by_source_hash(tmp_path, monkeypatch):
    monkeypatch.setattr(equation, "EQUATION_CACHE_DIR", tmp_path)
    calls = []
    real = equation._render_mathtext
    monkeypatch.setattr(equation, "_render_mathtext",
                        lambda src, color: calls.append(src) or real(src, color))
    first = render_equation("x^2 + y^2 = z^2")
    second = render_equation("x^2 + y^2 = z^2")
    assert first == second and first.exists()
    assert len(calls) == 1  # second call served from the on-disk cache


def test_render_failure_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(equation, "EQUATION_CACHE_DIR", tmp_path)
    assert render_equation(r"\notamathtextcommand{x}") is None
