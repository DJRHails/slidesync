"""Render display-math ``$$...$$`` blocks to PNG so they can be embedded in Slides.

Google Slides has no native LaTeX renderer, so a paragraph that is exactly a
``$$...$$`` block is rendered to a tight-bbox transparent PNG and inserted
through the same image path as a normal ``![](...)`` embed.

The backend is matplotlib **mathtext** — a pure-Python LaTeX subset, so no TeX
installation is needed. It covers the common presentation constructs
(``\\frac``, ``\\approx``, sub/superscripts, ``\\times``, ``\\max``,
``\\text{}``, greek, operators); full-TeX-only constructs fail the parse and
the graphic is skipped with a warning rather than aborting the push.

Renders are cached on disk keyed by the SHA-1 of (colour, equation source), so
an unchanged equation is never re-rendered (and, downstream, never re-uploaded —
`upload_image` caches the resulting PNG by its own content hash). A render
failure returns `None`; the caller warns and skips the graphic.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from loguru import logger

EQUATION_CACHE_DIR = Path(".data/cache/slidesync_equations")
# Rendered at 300 dpi and placed at natural size, so on the slide the equation
# reads at EQUATION_PT — deliberately above the deck's body text size, since a
# display equation is presentation content, not prose.
EQUATION_DPI = 300
EQUATION_PT = 30
# `template: equation` scales its focal equation up to fill most of the slide
# width, so it renders at double the point size: the placement math sets the
# on-slide size either way — the render pt only sets pixel density, and the
# larger render keeps the blown-up equation crisp.
EQUATION_FOCUS_PT = 60
INK_HEX = "#1E2024"  # matches the deck's BODY_INK


def render_equation(source: str, color: str = INK_HEX,
                    pt: int = EQUATION_PT) -> Path | None:
    """Render LaTeX `source` to a cached transparent PNG, or `None` on failure.

    The PNG is written to `EQUATION_CACHE_DIR/<sha1>.png` (keyed on colour +
    point size + source) and reused on the next call with the same inputs. Any
    failure — matplotlib missing, or a construct outside the mathtext subset —
    is logged as a warning and surfaced as `None`.
    """
    digest = hashlib.sha1(f"{color}\n{pt}\n{source}".encode("utf-8")).hexdigest()
    out = EQUATION_CACHE_DIR / f"{digest}.png"
    if out.exists():
        return out
    png = _render_mathtext(source, color, pt)
    if png is None:
        return None
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(png)
    return out


def _render_mathtext(source: str, color: str, pt: int = EQUATION_PT) -> bytes | None:
    """Render via matplotlib mathtext; `None` on failure.

    mathtext is single-line, so internal newlines (multi-line ``$$`` blocks are
    usually formatted for readability, not line breaks) collapse to spaces.
    """
    try:
        import matplotlib
    except ImportError:
        logger.warning("matplotlib not installed; $$...$$ equation skipped")
        return None
    matplotlib.use("Agg")
    import io

    from matplotlib import pyplot as plt

    tex = " ".join(source.split())
    fig = plt.figure(figsize=(0.01, 0.01))
    try:
        fig.text(x=0, y=0, s=f"${tex}$", fontsize=pt, color=color)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=EQUATION_DPI, transparent=True,
                    bbox_inches="tight", pad_inches=0.02)
        return buf.getvalue()
    except (ValueError, RuntimeError) as exc:  # mathtext parse errors -> ValueError
        logger.warning(f"mathtext failed to render '$${tex}$$': {exc}")
        return None
    finally:
        plt.close(fig)
