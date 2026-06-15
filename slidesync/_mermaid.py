"""Render fenced ```mermaid blocks to PNG so they can be embedded in Slides.

Google Slides has no native Mermaid renderer, so a ```mermaid``` block is turned
into a PNG and inserted through the same image path as a normal `![](...)` embed.

Two render backends, in preference order:

- `mmdc` (mermaid-cli) if it is on `PATH` — fully offline, highest fidelity.
- the `kroki.io` HTTP API otherwise — dependency-light (stdlib `urllib` only,
  no Node/Chromium install), the default for a fresh `uvx` invocation.

Renders are cached on disk keyed by the SHA-1 of the diagram source, so an
unchanged diagram is never re-rendered (and, downstream, never re-uploaded —
`upload_image` caches the resulting PNG by its own content hash). A render
failure returns `None`; the caller warns and skips the graphic rather than
aborting the whole push.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from loguru import logger

MERMAID_CACHE_DIR = Path(".data/cache/slidesync_mermaid")
KROKI_URL = "https://kroki.io/mermaid/png"
KROKI_TIMEOUT = 30  # seconds
# kroki.io sits behind Cloudflare, which 403s the default `Python-urllib` agent.
USER_AGENT = "slidesync (+https://github.com/DJRHails/slidesync)"


def render_mermaid(source: str) -> Path | None:
    """Render Mermaid `source` to a cached PNG, or `None` if rendering failed.

    The PNG is written to `MERMAID_CACHE_DIR/<sha1>.png` and reused on the next
    call with the same source. `mmdc` is preferred when available; otherwise the
    kroki.io HTTP API is used. Any failure (no backend reachable, bad diagram,
    network error) is logged as a warning and surfaced as `None`.
    """
    digest = hashlib.sha1(source.encode("utf-8")).hexdigest()
    out = MERMAID_CACHE_DIR / f"{digest}.png"
    if out.exists():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    png = _render_mmdc(source) if shutil.which("mmdc") else _render_kroki(source)
    if png is None:
        return None
    out.write_bytes(png)
    return out


def _render_mmdc(source: str) -> bytes | None:
    """Render via the local `mmdc` (mermaid-cli) binary; `None` on failure."""
    with _temp_diagram(source) as src_path:
        png_path = src_path.with_suffix(".png")
        try:
            subprocess.run(
                ["mmdc", "--input", str(src_path), "--output", str(png_path),
                 "--backgroundColor", "transparent"],
                check=True, capture_output=True, text=True, timeout=KROKI_TIMEOUT,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
                OSError) as exc:
            detail = getattr(exc, "stderr", "") or str(exc)
            logger.warning(f"mmdc failed to render a mermaid diagram: {detail.strip()}")
            return None
        return png_path.read_bytes() if png_path.exists() else None


def _render_kroki(source: str) -> bytes | None:
    """Render via the kroki.io HTTP API; `None` on failure.

    The raw diagram is POSTed as `text/plain` (simpler than the GET deflate+base64
    encoding and not subject to URL-length limits).
    """
    try:
        req = urllib.request.Request(
            KROKI_URL, data=source.encode("utf-8"),
            headers={"Content-Type": "text/plain", "Accept": "image/png",
                     "User-Agent": USER_AGENT},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=KROKI_TIMEOUT) as resp:
            return resp.read()
    except (urllib.error.URLError, OSError) as exc:
        logger.warning(f"kroki.io failed to render a mermaid diagram: {exc}")
        return None


class _temp_diagram:
    """Context manager for a temporary `.mmd` file holding the diagram source."""

    def __init__(self, source: str):
        self._source = source
        self._path: Path | None = None

    def __enter__(self) -> Path:
        import tempfile

        fd, name = tempfile.mkstemp(suffix=".mmd")
        self._path = Path(name)
        with open(fd, "w", encoding="utf-8") as handle:
            handle.write(self._source)
        return self._path

    def __exit__(self, *exc) -> None:
        if self._path is not None:
            self._path.unlink(missing_ok=True)
            self._path.with_suffix(".png").unlink(missing_ok=True)
