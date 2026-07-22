"""Bidirectional sync between a Slidev markdown deck and Google Slides.

`push` builds **native** Slides objects (title/body placeholders, bullets,
tables, positioned images) via `presentations.batchUpdate` — so text stays
editable, not a flat image. `pull` reconstructs Slidev markdown from those
native objects. `roundtrip` pushes a sample into a fresh deck, pulls it back,
and asserts the two are semantically identical.

Auth is borrowed from `gog` (no separate OAuth client): the client id/secret
live in gog's credentials file — `~/.config/gogcli/credentials.json` (Linux/XDG)
or `~/Library/Application Support/gogcli/credentials.json` (macOS) — and the
refresh token is exported via `gog auth tokens export`. The stored token already
carries the `slides`+`drive` scopes.

Idempotent sync (upsert), never a blind append:

- Each managed slide is created with `objectId = s2g_<keyHash>_<contentHash>`.
- `keyHash` identifies *which* slide (per-slide `id:` frontmatter, else the
  `#` headline slug, else the `##` title slug, else the figure filename stem,
  else index) and survives edits/reorders; `contentHash` is over a
  canonical render, so push->pull->push is a no-op.
- Diff: identical hash -> skip; same key, new content -> replace; new key ->
  create. Removed slides are kept by default (`--prune` to delete).
- Only `s2g_`-prefixed slides are ever touched; hand-authored slides are
  invisible to the sync. A tiny `<!-- s2g {...} -->` marker in speaker notes
  carries the human id + image path — and, for template slides, the authored
  body markdown (base64) — so `pull` recovers the source verbatim: comments
  stay comments, in place, instead of collapsing into one speaker-notes blob.
- ID-SCHEME STABILITY: the `s2g_<keyHash>_<contentHash>` format and every input
  to `keyHash` (digest, hash length, key derivation, multi-file namespacing)
  are a compatibility contract with every existing deck. Changing any of them
  re-keys every live slide: a routine push then sees a brand-new deck, recreates
  all slides from markdown, and destroys live styling/edits on the old copies.
  Any scheme change MUST ship a migration path — match old-scheme ids on read
  (the notes-marker `id` is the scheme-independent handle `sync` already uses)
  — and a CHANGELOG note. `mass_rekey` is the backstop: a push that would
  recreate a deck-scale number of slides under new ids is refused without
  `--allow-rekey`.

Usage:
    bin/slidesync.py push deck.slidev.md --deck <id> [--anchor <slideId>] [--prune]
    bin/slidesync.py push deck.slidev.md --new "My Talk"
    bin/slidesync.py pull <id> --out deck.slidev.md
    bin/slidesync.py roundtrip [--keep]
"""

from __future__ import annotations

import argparse
import base64
import copy
import datetime
import difflib
import hashlib
import html
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple, TypedDict

import frontmatter
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
from loguru import logger

from slidesync._equation import (
    EQUATION_DPI,
    EQUATION_FOCUS_PT,
    EQUATION_PT,
    INK_HEX,
    render_equation,
)
from slidesync._mermaid import render_mermaid

# gog's OAuth client file — Linux/XDG (`~/.config/gogcli`) or macOS App Support;
# mirrors the keyring-password lookup so slidesync auths on both platforms.
GOGCLI_CRED = next(
    (
        p
        for p in (
            Path.home() / ".config/gogcli/credentials.json",
            Path.home() / "Library/Application Support/gogcli/credentials.json",
        )
        if p.exists()
    ),
    Path.home() / "Library/Application Support/gogcli/credentials.json",
)
TOKEN_URI = "https://oauth2.googleapis.com/token"
SCOPES = [
    "https://www.googleapis.com/auth/presentations",
    "https://www.googleapis.com/auth/drive",
]
DEFAULT_ACCOUNT = None  # resolved from gog (or $SLIDESYNC_ACCOUNT)
IMAGE_CACHE = Path(".data/cache/slidesync_images.json")

MANAGED_RE = re.compile(r"^s2g_[0-9a-f]{10}_[0-9a-f]{10}$")
_EQ_IMG_RE = re.compile(r"_eq\d+$")  # element id of a rendered $$-equation image
MARKER_RE = re.compile(r"<!--\s*s2g\s*(?P<json>\{.*?\})\s*-->", re.S)
TEMPLATE_TAG_RE = re.compile(r"<!--\s*s2g:template\s+(?P<name>\S+)\s*-->")
EMU_PER_PX = 9525  # 96 dpi
EMU_PER_IN = 914400
SLIDE_W = 9144000
SLIDE_H = 5143500  # 16:9 slide height (5.625in)
BODY_X, BODY_Y = 457200, 1143000
BODY_W, BODY_H = 8229600, 3771900

SECTION_LAYOUTS = {"section", "center", "cover", "intro"}
RESERVED_KEYS = {"id", "template", "layout", "hidden", "hide"}

# Brand palette extracted from the Reliable Monitors deck (IBM Plex Sans).
BRAND_FONT = "IBM Plex Sans"
RED = {"red": 0.7529412, "green": 0.22352941, "blue": 0.16862746}   # #C0392B
INK = {"red": 0.011764706, "green": 0.02745098, "blue": 0.07058824}  # #03070F
BODY_INK = {"red": 0.11764706, "green": 0.1254902, "blue": 0.14117648}  # #1E2024
PAPER = {"red": 0.98039216, "green": 0.98039216, "blue": 0.98039216}  # #FAFAFA
WHITE = {"red": 1.0, "green": 1.0, "blue": 1.0}                       # #FFFFFF
MUTED = {"red": 0.62, "green": 0.65, "blue": 0.69}  # dimmed byline on dark cards
# ==highlight== runs: a warm amber wash (#FFE08A) behind the text. The run's
# text is forced to INK (see STYLE), so the mark stays legible on the dark
# templates too — dark ink on a light accent patch, rather than paper-white
# text disappearing into a light wash.
HIGHLIGHT = {"red": 1.0, "green": 0.8784314, "blue": 0.5411765}  # #FFE08A
LIGHT_BG, DARK_BG = PAPER, BODY_INK

# Desired body size for styled-template bullet bodies. Set explicitly (rather
# than inheriting the Slides text-box default) so rendering is deterministic and
# so `_fit_paras_pt` has a known ceiling to shrink down from when content is long.
# Sized for sparse, presentation-style slides (big numbers + a few bullets); long
# bodies still auto-shrink toward the 12pt floor via `_fit_paras_pt`.
BODY_PT = 24

# ---------------------------------------------------------------------------
# Auth (borrowed from gog)
# ---------------------------------------------------------------------------


def _ensure_gog_keyring_password() -> None:
    """Load gog's file-keyring password lazily for our gog subprocesses.

    Shells no longer export GOG_KEYRING_PASSWORD globally; read it from the
    600-mode password file only when slidesync actually invokes gog.
    """
    if os.environ.get("GOG_KEYRING_PASSWORD"):
        return
    for p in (
        Path.home() / ".config/gogcli/keyring-password",
        Path.home() / "Library/Application Support/gogcli/keyring-password",
    ):
        if p.exists():
            os.environ["GOG_KEYRING_PASSWORD"] = p.read_text().strip()
            return


def get_services(account: str | None):
    _ensure_gog_keyring_password()
    creds = _credentials(account or _default_account())
    slides = build("slides", "v1", credentials=creds, cache_discovery=False)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    return slides, drive


def _default_account() -> str:
    """Resolve the gog account: $SLIDESYNC_ACCOUNT, else gog's default account."""
    env = os.environ.get("SLIDESYNC_ACCOUNT")
    if env:
        return env
    out = subprocess.run(["gog", "auth", "list", "-p"], capture_output=True,
                         text=True).stdout
    rows = [ln.split("\t") for ln in out.splitlines() if ln.strip()]
    for cols in rows:
        if len(cols) >= 2 and cols[1] == "default":
            return cols[0]
    if rows:
        return rows[0][0]
    sys.exit("no gog account found; run `gog login <email>` or set "
             "$SLIDESYNC_ACCOUNT")


def _credentials(account: str) -> Credentials:
    if not GOGCLI_CRED.exists():
        sys.exit(f"gog OAuth client not found at {GOGCLI_CRED}")
    client = json.loads(GOGCLI_CRED.read_text())
    with tempfile.NamedTemporaryFile(suffix=".json") as tmp:
        subprocess.run(
            ["gog", "auth", "tokens", "export", account, "--out", tmp.name,
             "--overwrite"],
            check=True, capture_output=True, text=True,
        )
        token = json.loads(Path(tmp.name).read_text())
    creds = Credentials(
        token=None, refresh_token=token["refresh_token"],
        client_id=client["client_id"], client_secret=client["client_secret"],
        token_uri=TOKEN_URI, scopes=SCOPES,
    )
    creds.refresh(Request())
    return creds


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


@dataclass
class Run:
    start: int  # codepoint offset into Para.text
    end: int
    style: str  # bold | italic | code | link
    link: str | None = None


@dataclass
class Para:
    text: str  # clean text, no leading tabs
    runs: list[Run] = field(default_factory=list)
    depth: int = -1  # >=0 bullet nesting; -1 plain paragraph
    ordered: bool = False  # numbered list item (1.) vs bullet (-)


@dataclass
class Slide:
    key: str
    layout: str  # section | content | image | table (generative path)
    title: str = ""
    paras: list[Para] = field(default_factory=list)
    image: str | None = None
    image_alt: str = ""  # ![alt](path) -> image description / accessibility alt text
    image_link: str | None = None  # [![alt](img)](href) -> click-through target
    mermaid: str | None = None  # ```mermaid``` source, rendered to a PNG on push
    table: list[list[str]] | None = None
    notes: str = ""
    equations: list[str] = field(default_factory=list)  # $$...$$ LaTeX, PNG on push
    kicker: str = ""  # h2 above an h1 -> {{h2}} kicker
    layout_name: str | None = None  # explicit `layout:` — section kw or theme layout
    template_name: str | None = None  # explicit `template:` — tagged styled slide
    vars: dict = field(default_factory=dict)  # extra frontmatter -> {{token}} values
    custom: str | None = None  # ```gslides``` literal Slides API requests (JSON)
    overlay: str | None = None  # ```gslides-overlay``` requests replayed on the render
    verbatim: str | None = None  # ``` ``` fenced body for prompt/code slides
    src: str | None = None  # body markdown as authored (comments in place)
    src_path: Path | None = None  # source file this slide was loaded from
    src_key: str = ""  # id as written in that file (un-namespaced)
    key_hash: str = ""
    content_hash: str = ""
    object_id: str = ""
    hidden: bool = False  # `hidden:`/`hide:` frontmatter -> slide skipped in Slides

    def semantic(self) -> tuple:
        def runs(p):
            return tuple((r.start, r.end, r.style, r.link) for r in p.runs)
        paras = tuple((p.depth, p.ordered, p.text, runs(p)) for p in self.paras)
        table = tuple(map(tuple, self.table)) if self.table else None
        return (self.key, self.layout_name, self.template_name,
                tuple(sorted(self.vars.items())), self.layout, self.title,
                self.kicker, paras, table, self.image, self.image_alt,
                self.notes, self.hidden, tuple(self.equations), self.overlay,
                self.image_link)


def _u16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _sha10(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:10]


def _is_truthy(val) -> bool:
    """A frontmatter flag is on for `true`/`1`/`yes`/`on` (the YAML shim parses
    values as bare strings); anything else — including absent or `false` — is off."""
    return str(val).strip().lower() in {"true", "1", "yes", "on"}


def _image_bytes_hash(slide: Slide, base_dir: Path = Path(".")) -> str | None:
    """sha10 of a slide's image FILE bytes — folded into `content_hash`.

    Without this, the canonical render only carries the image *path* + alt text,
    so a figure regenerated in place (same path, new pixels) leaves the hash
    unchanged and the slide is skipped — the new figure never reaches Slides.
    Hashing the bytes makes regenerated pixels move the hash, so the slide is
    replaced and the image re-uploaded. A missing/unreadable file falls back to
    `None` (the path string still distinguishes slides) instead of crashing.
    """
    p = _image_path(slide, base_dir)
    if p is None or not p.exists():
        # No image, or a non-local ref (e.g. a pulled Drive URL) / missing file:
        # the path string in the canonical render still distinguishes slides, and
        # a genuinely missing local file is reported at push time.
        return None
    try:
        return hashlib.sha1(p.read_bytes()).hexdigest()[:10]
    except OSError as exc:
        logger.warning(f"image unreadable, hashing path only: {p} ({exc})")
        return None


# ---------------------------------------------------------------------------
# Importer:  markdown -> Slide
# ---------------------------------------------------------------------------

VCLICK_RE = re.compile(r"</?v-clicks?\b[^>]*>", re.I)
DIV_RE = re.compile(r"</?(?:div|span)\b[^>]*>", re.I)
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
LIST_RE = re.compile(r"^(?P<indent>\s*)(?P<marker>[-*]|\d+\.)\s+(?P<text>.*)$")
# alt text may contain a `]` (e.g. a Wilson CI "[1.7–3.1]"); greedy `.*` binds to the FINAL
# `](url)` so a bracket in the caption doesn't truncate the match and silently drop the image
# into the slide body (which renders blank on a text-free `graph` template).
IMAGE_RE = re.compile(r"^!\[(?P<alt>.*)\]\((?P<url>[^)]+)\)\s*$")
# The deck-variant convention wraps the embedded image in a link to the full
# figure — [![alt](deck.png)](full.png) — an image plus a click-through href.
LINKED_IMAGE_RE = re.compile(
    r"""(?x)
    ^\[                  # open of the wrapping [ ... ](href) link
    !\[(?P<alt>.*)\]     # the image's alt text (greedy: alt may contain `]`)
    \((?P<url>[^)]+)\)   # the image url
    \]
    \((?P<href>[^)]+)\)  # the click-through target
    \s*$
    """
)
COMMENT_RE = re.compile(r"<!--(?P<body>.*?)-->", re.S)
CUSTOM_RE = re.compile(
    r"""(?msx)              # multiline, dotall, verbose
    ^```\ *g?slides\ *\n    # fence opening with a gslides/slides lang tag
    (?P<json>.*?)           # the literal Slides API requests (JSON)
    \n```\ *$               # fence close
    """
)
OVERLAY_RE = re.compile(
    r"""(?msx)                     # multiline, dotall, verbose
    ^```\ *gslides-overlay\ *\n    # fence opening with a gslides-overlay lang tag
    (?P<json>.*?)                  # literal Slides API requests (JSON)
    \n```\ *$                      # fence close
    """
)
MERMAID_RE = re.compile(
    r"""(?msx)              # multiline, dotall, verbose
    ^```\ *mermaid\ *\n     # fence opening with a mermaid lang tag
    (?P<diagram>.*?)        # the diagram source (rendered to a PNG on push)
    \n```\ *$               # fence close
    """
)
EQUATION_RE = re.compile(
    r"""(?msx)              # multiline, dotall, verbose
    ^\$\$                   # display-math opener at line start
    (?P<tex>.+?)            # the LaTeX source (single- or multi-line)
    \$\$[ \t]*$             # closer ending a line
    """
)
TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")
INLINE_RE = re.compile(
    r"""(?x)
    (\*\*.+?\*\* | __.+?__
     | \*[^*]+?\* | _[^_]+?_
     | ==(?:\S.*?\S|\S)==    # ==highlight== (markdown-it-mark); non-space-adjacent
                             # delimiters, so a bare `a == b == c` stays plain text
     | `[^`]+?`
     | \[[^\]]+?\]\([^)]+?\))
    """
)


def load_slides(path: Path) -> list[Slide]:
    text = path.read_text()
    slides = build_slides(split_slides(text), infer=_file_infer(text))
    for s in slides:
        # Record the origin and re-finalize now that it's known, so a relative
        # `![](fig.png)` resolves against this file's dir and its bytes fold into
        # content_hash (an in-place figure regen then re-pushes — see _finalize).
        s.src_path, s.src_key = path, s.key
        _finalize(s, path.parent)
    return slides


LOCAL_LINK_RE = re.compile(
    r"""(?x)
    \]\(\#              # close of [text], open of (#...
    (?P<target>[\w-]+)  # the slide id being linked to
    \)                  # close paren
    """
)


def load_deck(paths: list[Path]) -> list[Slide]:
    """Load one or many source files into a single slide list.

    With several files (e.g. `slidesync sync meetings/*.slidev.md`, one file per
    meeting), deck order follows the argument order and every slide id is
    namespaced with its file's stem (`2026-06-15-overview`) so files can reuse
    ids without colliding. Intra-file `[text](#id)` links are rewritten to the
    namespaced target; already-qualified cross-file links pass through. Each
    slide remembers its origin (`src_path`/`src_key`) so `sync` writes captures
    and live edits back into the right file. Duplicate keys are an error.
    """
    if len(paths) == 1:
        slides = load_slides(paths[0])  # sets src_path + folds image bytes
    else:
        slides = []
        for path in paths:
            prefix = path.name.split(".")[0]
            text = path.read_text()
            infer = _file_infer(text)
            chunks = split_slides(text)
            keys = [m.get("id") or _derive_key(b) or f"slide{i}"
                    for i, (m, b) in enumerate(chunks)]
            local = set(keys)

            def relink(m, prefix=prefix, local=local):
                t = m.group("target")
                return f"](#{prefix}-{t})" if t in local else m.group(0)

            for i, (meta, body) in enumerate(chunks):
                src_key = keys[i]
                meta = {**meta, "id": f"{prefix}-{src_key}"}
                slide = build_slide(meta, LOCAL_LINK_RE.sub(relink, body), i,
                                    infer=infer)
                slide.src_path, slide.src_key = path, src_key
                _finalize(slide, path.parent)  # fold image bytes now src_path is set
                slides.append(slide)
    seen, dupes = set(), set()
    for s in slides:
        (dupes if s.key in seen else seen).add(s.key)
    if dupes:
        sys.exit(f"duplicate slide ids across sources: {sorted(dupes)}")
    return slides


def split_slides(text: str) -> list[tuple[dict, str]]:
    post = frontmatter.loads(text)
    chunks = re.split(r"(?m)^---[ \t]*$", post.content)
    out: list[tuple[dict, str]] = []
    i = 0
    while i < len(chunks):
        meta, chunk = {}, chunks[i]
        if i + 1 < len(chunks) and _is_yaml_block(chunk):
            meta = _parse_yaml(chunk)
            i += 1
            chunk = chunks[i] if i < len(chunks) else ""
        if chunk.strip():
            out.append((meta, chunk))
        i += 1
    return out


def _is_yaml_block(chunk: str) -> bool:
    lines = [ln for ln in chunk.splitlines() if ln.strip()]
    return bool(lines) and all(re.match(r"^\s*[\w-]+\s*:", ln) for ln in lines)


def _parse_yaml(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        m = re.match(r"^\s*([\w-]+)\s*:\s*(.*)$", line)
        if m:
            out[m.group(1)] = m.group(2).strip().strip("'\"")
    return out


def _file_infer(text: str) -> bool:
    """Template inference is on by default; `infer: false` in the file-level
    frontmatter opts a file out (an explicit `infer: true` remains a no-op)."""
    val = frontmatter.loads(text).metadata.get("infer")
    return True if val is None else _is_truthy(val)


_FENCED_RE = re.compile(
    r"""(?msx)               # multiline, dotall, verbose
    ^```(?P<info>[^\n]*)\n   # opening fence with its info string
    .*?                      # literal block body
    \n```[ \t]*$             # closing fence
    """
)


class _Skim(NamedTuple):
    """Structural skim of a slide body — just enough for template/id inference."""

    levels: list[int]         # h1/h2 levels in authored order (leading block only)
    headings: dict[int, str]  # first h1 / first h2 text
    image: str                # first image url ("" when none)
    table: bool               # any markdown table
    fences: list[str]         # fenced-block info strings (```info)


def _skim(body: str) -> _Skim:
    """Skim a slide body the way `parse_body` reads it: comments, fenced blocks
    and `$$` equations are invisible, `#`/`##` headings count only until the
    first body paragraph, and only the first image matters."""
    text = COMMENT_RE.sub("", body)
    fences = [m.group("info").strip() for m in _FENCED_RE.finditer(text)]
    text = EQUATION_RE.sub("", _FENCED_RE.sub("", text))
    levels: list[int] = []
    headings: dict[int, str] = {}
    image, table, leading = "", False, True
    lines = text.splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        hm = HEADING_RE.match(line)
        if hm and len(hm.group(1)) in (1, 2) and len(hm.group(1)) not in headings and leading:
            levels.append(len(hm.group(1)))
            headings[len(hm.group(1))] = hm.group(2).strip()
            continue
        if im := (LINKED_IMAGE_RE.match(stripped) or IMAGE_RE.match(stripped)):
            image = image or im.group("url")
            continue
        if "|" in line and i + 1 < len(lines) and TABLE_SEP_RE.match(lines[i + 1]):
            table = True
            continue
        leading = False  # a body paragraph ends the heading block
    return _Skim(levels, headings, image, table, fences)


def _derive_key(body: str) -> str:
    """Implicit slide id: the `#` headline slug, else the `##` title slug, else
    the figure filename stem (with a `_deck` variant suffix stripped)."""
    sk = _skim(body)
    if slug := _slug(sk.headings.get(1) or sk.headings.get(2) or ""):
        return slug
    stem = sk.image.rsplit("/", 1)[-1].rsplit(".", 1)[0] if sk.image else ""
    return _slug(re.sub(r"_deck$", "", stem))


def _infer_template(body: str, index: int) -> str | None:
    """Default `template:` for an untagged slide, from its shape alone.

    The first slide of a file is its title card (`dark`). A fenced block is a
    verbatim `prompt`; a figure with no headings is a self-titled `graph`; a
    `##` kicker above the `#` headline is a `dark` section divider; an `#`
    headline is a `topic`; a lone `##` title is `content`. Slides with tables
    or only a mermaid block stay untagged — the styled templates have no slot
    for them, the generative layouts do.
    """
    if index == 0:
        return "dark"
    sk = _skim(body)
    if sk.table:
        return None
    if any(info != "mermaid" for info in sk.fences):
        return "prompt"
    if sk.image and not sk.levels:
        return "graph"
    if sk.levels[:2] == [2, 1]:
        return "dark"
    if 1 in sk.levels:
        return "topic"
    if 2 in sk.levels:
        return "content"
    return None


def build_slides(chunks: list[tuple[dict, str]], infer: bool = False) -> list[Slide]:
    return [build_slide(meta, body, i, infer=infer)
            for i, (meta, body) in enumerate(chunks)]


def build_slide(meta: dict, body: str, index: int, infer: bool = False) -> Slide:
    authored = body.strip("\n")
    # overlay first: VERBATIM_RE (prompt/code) grabs whichever fence comes first
    # in the body, so the overlay block must already be gone by then
    overlay, body = _extract_overlay(body)
    custom, body = _extract_custom(body)
    template = meta.get("template")
    if infer and not template and custom is None and not meta.get("layout"):
        template = _infer_template(body, index)
    verbatim = None
    if (template or "").lower() in ("prompt", "code"):
        verbatim, body = _extract_verbatim(body)
    (headings, paras, image, image_alt, mermaid, table, notes, equations,
     image_link) = parse_body(body)
    h1, h2 = headings.get(1), headings.get(2)
    title = h1 or h2 or ""           # h1 is the headline; a lone h2 is the title
    kicker = h2 if (h1 and h2) else ""  # an h2 above an h1 is the kicker
    key = meta.get("id") or _derive_key(authored) or f"slide{index}"
    slide = Slide(key, _layout_of(meta, image or mermaid, table), title, paras,
                  image, image_alt, image_link, mermaid, table, notes, equations)
    slide.kicker = kicker
    slide.layout_name = meta.get("layout")
    slide.template_name = "custom" if custom is not None else template
    slide.vars = {k: v for k, v in meta.items() if k not in RESERVED_KEYS}
    slide.custom = custom
    if custom is not None and overlay is not None:
        logger.warning(f"slide '{key}': ```gslides-overlay``` ignored — a "
                       "```gslides``` slide already carries raw requests")
        overlay = None
    slide.overlay = overlay
    slide.verbatim = verbatim
    slide.hidden = _is_truthy(meta.get("hidden") or meta.get("hide"))
    if custom is None:  # custom slides are pull-authoritative; their source goes stale
        slide.src = authored
    return _finalize(slide)


def _finalize(slide: Slide, base_dir: Path = Path(".")) -> Slide:
    # The objectId scheme below is a compatibility contract with every existing
    # deck — changing it re-keys every live slide (see ID-SCHEME STABILITY in
    # the module docstring). Any change needs a read-side migration path, not
    # just a version bump.
    canonical = to_slidev(slide, include_id=False)
    img_hash = _image_bytes_hash(slide, base_dir)
    if img_hash is not None:  # fold the image bytes in so regenerated pixels re-push
        canonical += f"\n<!-- img-bytes {img_hash} -->"
    slide.key_hash = _sha10(slide.key)
    slide.content_hash = _sha10(canonical)
    if slide.custom is not None:
        # Stable id keyed only on `id:` — native drawing edits (which never touch
        # the markdown) must not orphan the slide, since custom slides are
        # pull-authoritative and only (re)pushed when missing.
        slide.content_hash = slide.key_hash
    slide.object_id = f"s2g_{slide.key_hash}_{slide.content_hash}"
    return slide


def _layout_of(meta: dict, image, table) -> str:
    if meta.get("layout") in SECTION_LAYOUTS:
        return "section"
    if image:
        return "image"
    if table:
        return "table"
    return "content"


def parse_body(body: str):
    """Return (headings{level:text}, paras, image, image_alt, mermaid, table,
    notes, equations, image_link)."""
    notes = _extract_notes(body)
    body = COMMENT_RE.sub("", body)
    mermaid, body = _extract_mermaid(body)
    equations, body = _extract_equations(body)
    body = VCLICK_RE.sub("", DIV_RE.sub("", body))
    lines = body.splitlines()
    headings, paras, image, table, image_alt = {}, [], None, None, ""
    image_link = None
    i = 0
    while i < len(lines):
        line, stripped = lines[i], lines[i].strip()
        if not stripped:
            if paras and (paras[-1].text or paras[-1].depth >= 0):
                paras.append(Para("", [], -1))  # keep one blank line for spacing
            i += 1
            continue
        hm = HEADING_RE.match(line)
        level = len(hm.group(1)) if hm else 0
        if hm and level in (1, 2) and level not in headings and not paras:
            headings[level] = parse_inline(hm.group(2).strip())[0]
            i += 1
        elif (lk := LINKED_IMAGE_RE.match(stripped)):
            image, image_alt = lk.group("url"), lk.group("alt")
            image_link, i = lk.group("href"), i + 1
        elif (im := IMAGE_RE.match(stripped)):
            image, image_alt, i = im.group("url"), im.group("alt"), i + 1
        elif "|" in line and i + 1 < len(lines) and TABLE_SEP_RE.match(lines[i + 1]):
            table, i = _parse_table(lines, i)
        else:
            paras.append(_parse_para(line, hm))
            i += 1
    while paras and not paras[-1].text and paras[-1].depth < 0 \
            and not paras[-1].runs:
        paras.pop()
    return (headings, paras, image, image_alt, mermaid, table, notes, equations,
            image_link)


def _is_thread(comment_body: str) -> bool:
    """Captured comment-thread mirrors (`<!-- @Author: text -->`) are Slides
    comments, not presenter notes — they never enter the speaker-notes pane."""
    return bool(re.match(r"\s*@\S", comment_body))


def _extract_notes(body: str) -> str:
    parts = [m.group("body").strip() for m in COMMENT_RE.finditer(body)]
    joined = "\n".join(p for p in parts if p and not _is_thread(p))
    return MARKER_RE.sub("", joined).strip()


def _notes_variants(src: str | None) -> set[str]:
    """Normalised speaker-notes text under both conventions — with and without
    thread mirrors — so decks pushed before threads left the pane still compare
    as untouched."""
    parts = [m.group("body").strip() for m in COMMENT_RE.finditer(src or "")]
    legacy = MARKER_RE.sub("", "\n".join(p for p in parts if p)).strip()
    return {" ".join(_extract_notes(src or "").split()), " ".join(legacy.split())}


def _thread_blocks(src: str | None) -> list[list[tuple[str, str]]]:
    """[(author, text), ...] per `<!-- @Author: ... -->` thread mirror in src.

    The first entry is the thread head; later `@Author:` lines are replies;
    unprefixed lines continue the previous entry (multi-line comments).
    """
    out = []
    for m in COMMENT_RE.finditer(src or ""):
        body = m.group("body").strip()
        if not _is_thread(body):
            continue
        entries: list[tuple[str, str]] = []
        for line in body.splitlines():
            if am := re.match(r"@(?P<author>[^:@][^:]*):\s?(?P<text>.*)$", line):
                entries.append((am.group("author"), am.group("text")))
            elif entries:
                author, text = entries[-1]
                entries[-1] = (author, text + "\n" + line)
        if entries:
            out.append(entries)
    return out


def _extract_custom(body: str) -> tuple[str | None, str]:
    """Pull a ```gslides``` literal-requests block out of the body, if present."""
    m = CUSTOM_RE.search(body)
    if not m:
        return None, body
    return m.group("json").strip(), body[:m.start()] + body[m.end():]


def _extract_overlay(body: str) -> tuple[str | None, str]:
    """Pull a ```gslides-overlay``` literal-requests block out of the body, if present."""
    m = OVERLAY_RE.search(body)
    if not m:
        return None, body
    return m.group("json").strip(), body[:m.start()] + body[m.end():]


def _extract_mermaid(body: str) -> tuple[str | None, str]:
    """Pull a ```mermaid``` block out of the body (rendered to a PNG on push)."""
    m = MERMAID_RE.search(body)
    if not m:
        return None, body
    return m.group("diagram").strip(), body[:m.start()] + body[m.end():]


def _extract_equations(body: str) -> tuple[list[str], str]:
    """Pull every display-math `$$...$$` paragraph out of the body.

    Each is rendered to a transparent PNG on push. The inner LaTeX is kept
    verbatim (including internal newlines) so `to_slidev` can re-emit the block
    byte-identically. Inline `$x$` maths is out of scope and passes through as
    plain text.
    """
    eqs = [m.group("tex") for m in EQUATION_RE.finditer(body)]
    return (eqs, EQUATION_RE.sub("", body)) if eqs else (eqs, body)


VERBATIM_RE = re.compile(  # any fenced block — captures the literal body, no parsing
    r"""(?msx)
    ^```[^\n]*\n
    (?P<text>.*?)
    \n```[ ]*$
    """
)


def _extract_verbatim(body: str) -> tuple[str | None, str]:
    """Pull a fenced block out of the body verbatim (for prompt/code slides)."""
    m = VERBATIM_RE.search(body)
    if not m:
        return None, body
    return m.group("text"), body[:m.start()] + body[m.end():]


def _parse_para(line: str, hm) -> Para:
    lm = LIST_RE.match(line)
    if lm:
        depth = len(lm.group("indent").replace("\t", "  ")) // 2
        clean, runs = parse_inline(lm.group("text").strip())
        return Para(clean, runs, depth, ordered=lm.group("marker")[0].isdigit())
    if hm:
        clean, _ = parse_inline(hm.group(2).strip())
        return Para(clean, [Run(0, len(clean), "bold")], -1)
    clean, runs = parse_inline(line.strip())
    return Para(clean, runs, -1)


def _parse_table(lines, i):
    rows = []
    while i < len(lines) and "|" in lines[i]:
        if TABLE_SEP_RE.match(lines[i]):
            i += 1
            continue
        cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
        rows.append([parse_inline(c)[0] for c in cells])
        i += 1
    return rows, i


WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
BR_RE = re.compile(r"<br\s*/?>", re.I)


def _clean_text(text: str) -> str:
    text = BR_RE.sub(" ", text)
    text = WIKILINK_RE.sub(lambda m: m.group(2) or m.group(1), text)
    return html.unescape(text)


def parse_inline(text: str) -> tuple[str, list[Run]]:
    text = _clean_text(text)
    clean, runs, pos = "", [], 0
    for m in INLINE_RE.finditer(text):
        clean += text[pos:m.start()]
        inner, style, link = _inline_inner(m.group(0))
        runs.append(Run(len(clean), len(clean) + len(inner), style, link))
        clean += inner
        pos = m.end()
    clean += text[pos:]
    return clean, runs


def _inline_inner(tok: str):
    if tok.startswith(("**", "__")):
        return tok[2:-2], "bold", None
    if tok.startswith("=="):
        return tok[2:-2], "highlight", None
    if tok.startswith("`"):
        return tok[1:-1], "code", None
    if tok.startswith("["):
        m = re.match(r"\[([^\]]+)\]\(([^)]+)\)", tok)
        assert m is not None  # tok already matched INLINE_RE's link alternative
        return m.group(1), "link", m.group(2)
    return tok[1:-1], "italic", None


# ---------------------------------------------------------------------------
# Canonical render:  Slide -> markdown
# ---------------------------------------------------------------------------

MARKS = {"bold": ("**", "**"), "italic": ("*", "*"), "code": ("`", "`"),
         "highlight": ("==", "==")}


def to_slidev(slide: Slide, include_id: bool = True) -> str:
    fm = {}
    if slide.template_name:
        fm["template"] = slide.template_name
        for k in sorted(slide.vars):
            fm[k] = slide.vars[k]
    elif slide.layout_name:
        fm["layout"] = slide.layout_name
    if slide.hidden:  # skipped-in-Slides flag; independent of template/layout
        fm["hidden"] = "true"
    if include_id:
        fm["id"] = slide.key
    out = []
    if fm:
        out += ["---"] + [f"{k}: {v}" for k, v in fm.items()] + ["---"]
    if slide.src is not None:
        # Authored source is the canonical render: comments stay comments, in
        # place. Speaker notes are emitted only when they no longer match the
        # authored comments (i.e. someone edited the notes pane in Slides) —
        # compared whitespace-normalised, since the notes shape flattens
        # paragraphs when read back.
        out.append(slide.src)
        extra = slide.notes.strip()
        if extra and " ".join(extra.split()) not in _notes_variants(slide.src):
            out.append(f"<!-- {extra} -->")
        return "\n".join(out).strip() + "\n"
    if slide.custom is not None:
        out += ["```gslides", slide.custom, "```"]
        if slide.notes:
            out.append(f"<!-- {slide.notes} -->")
        return "\n".join(out).strip() + "\n"
    if slide.verbatim is not None:
        if slide.title:
            out.append(("# " if slide.kicker else "## ") + slide.title)
        if slide.kicker:
            out.append("## " + slide.kicker)
        out += ["```text", slide.verbatim, "```"]
        if slide.notes:
            out.append(f"<!-- {slide.notes} -->")
        return "\n".join(out).strip() + "\n"
    if slide.kicker:  # h1 headline + h2 kicker
        out.append("# " + slide.title)
        out.append("## " + slide.kicker)
    elif slide.title:
        out.append(("# " if slide.layout == "section" else "## ") + slide.title)
    out += [_render_para(p) for p in slide.paras]
    # the stored LaTeX is verbatim (delimiters stripped at parse time), so the
    # re-emitted block is byte-identical to what was authored
    out += [f"$${eq}$$" for eq in slide.equations]
    if slide.overlay is not None:
        out += ["```gslides-overlay", slide.overlay, "```"]
    if slide.image:
        img_md = f"![{slide.image_alt}]({slide.image})"
        out.append(f"[{img_md}]({slide.image_link})" if slide.image_link else img_md)
    if slide.table:
        out += _render_table(slide.table)
    if slide.notes:
        out.append(f"<!-- {slide.notes} -->")
    return "\n".join(out).strip() + "\n"


def _render_para(p: Para) -> str:
    body = render_inline(p.text, p.runs)
    if p.depth >= 0:
        return "  " * p.depth + ("1. " if p.ordered else "- ") + body
    return body


def render_inline(text: str, runs: list[Run]) -> str:
    edits = []
    for r in runs:
        start, end = r.start, r.end  # don't wrap surrounding whitespace in marks
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        if start >= end:
            continue
        if r.style == "link":
            edits.append((start, "["))
            edits.append((end, f"]({r.link})"))
        else:
            o, c = MARKS[r.style]
            edits.append((start, o))
            edits.append((end, c))
    out, last = [], 0
    for pos, mark in sorted(edits, key=lambda e: (e[0], e[1] in (")",))):
        out.append(text[last:pos])
        out.append(mark)
        last = pos
    out.append(text[last:])
    return "".join(out)


def _render_table(table) -> list[str]:
    head = "| " + " | ".join(table[0]) + " |"
    sep = "| " + " | ".join("---" for _ in table[0]) + " |"
    rows = ["| " + " | ".join(r) + " |" for r in table[1:]]
    return [head, sep, *rows]


def _marker(slide: Slide) -> str:
    data = {"id": slide.key}
    if slide.image:
        data["img"] = slide.image
        if slide.image_alt:
            data["alt"] = slide.image_alt
    if slide.equations:
        # base64 like `src` below: LaTeX braces mean a `}` could land right
        # before `-->` and truncate the delimiter-based MARKER_RE JSON. This is
        # what lets `pull` reconstruct the `$$...$$` source verbatim.
        data["eq"] = [base64.b64encode(e.encode()).decode()
                      for e in slide.equations]
    if slide.template_name:
        data["template"] = slide.template_name
        if slide.title:
            data["h1"] = slide.title
        if slide.kicker:
            data["h2"] = slide.kicker
        body_md = "\n".join(_render_para(p) for p in slide.paras)
        if body_md:
            data["body"] = body_md
        if slide.vars:
            data["vars"] = slide.vars
        if slide.src is not None:
            # base64: MARKER_RE is delimiter-based, so a `}` + `-->` sequence
            # in raw authored markdown would truncate the JSON mid-string;
            # encoding also keeps the visible notes pane free of a giant blob.
            data["src"] = base64.b64encode(slide.src.encode()).decode()
    elif slide.layout_name and slide.layout_name not in SECTION_LAYOUTS:
        data["tpl"] = slide.layout_name
    # Last-push stamp: `sync` reports it alongside drift. The Slides/Drive APIs
    # have no per-slide edit times (file-level modifiedTime only), so this is
    # the only per-slide timestamp that exists.
    data["at"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"<!-- s2g {json.dumps(data, separators=(',', ':'))} -->"


# ---------------------------------------------------------------------------
# Push:  Slide -> batchUpdate
# ---------------------------------------------------------------------------

STYLE = {
    "bold": ({"bold": True}, "bold"),
    "italic": ({"italic": True}, "italic"),
    "code": ({"fontFamily": "Roboto Mono"}, "fontFamily"),
    # ==highlight==: warm amber wash behind the run; the text is pinned to INK
    # so the mark reads as dark-on-accent everywhere, dark templates included.
    "highlight": ({"backgroundColor": {"opaqueColor": {"rgbColor": HIGHLIGHT}},
                   "foregroundColor": {"opaqueColor": {"rgbColor": INK}}},
                  "backgroundColor,foregroundColor"),
}


FILLABLE = {"TITLE", "CENTERED_TITLE", "SUBTITLE", "BODY"}


def _font_all(obj_id, rgb, bold=None, cell=None) -> dict:
    """Set the brand font + colour over all text in a shape or table cell."""
    style = {"fontFamily": BRAND_FONT,
             "foregroundColor": {"opaqueColor": {"rgbColor": rgb}}}
    fields = "fontFamily,foregroundColor"
    if bold is not None:
        style["bold"] = bold
        fields += ",bold"
    req = {"objectId": obj_id, "textRange": {"type": "ALL"},
           "style": style, "fields": fields}
    if cell is not None:
        req["cellLocation"] = {"rowIndex": cell[0], "columnIndex": cell[1]}
    return {"updateTextStyle": req}


def _kicker(tid) -> list[dict]:
    """Style a slide title as the deck's red, centered kicker label."""
    return [
        {"updateTextStyle": {"objectId": tid, "textRange": {"type": "ALL"},
            "style": {"fontFamily": BRAND_FONT, "bold": False,
                      "fontSize": {"magnitude": 18, "unit": "PT"},
                      "foregroundColor": {"opaqueColor": {"rgbColor": RED}}},
            "fields": "fontFamily,bold,fontSize,foregroundColor"}},
        {"updateParagraphStyle": {"objectId": tid, "textRange": {"type": "ALL"},
            "style": {"alignment": "CENTER"}, "fields": "alignment"}},
    ]


@dataclass
class Style:
    bg: dict                  # slide background
    headline_pt: int | None   # None -> no separate headline (kicker is the title)
    headline_rgb: dict
    body_align: str | None    # CENTER | START | None (no body region)
    top: float                # starting y (inches) of the kicker/headline block
    head_lines: int = 2       # max headline lines before auto-fit shrinks the font


# Built-in brand kit matching the Reliable Monitors deck (no in-deck templates).
STYLES = {
    "dark":     Style(DARK_BG, 72, PAPER, None, 2.0),      # dark title card
    "title":    Style(DARK_BG, 72, PAPER, None, 2.0),
    "appendix": Style(LIGHT_BG, 72, INK, None, 2.0),       # light title card
    "label":    Style(LIGHT_BG, 50, INK, "CENTER", 1.5),   # question + centered body
    "question": Style(LIGHT_BG, 50, INK, "CENTER", 1.5),
    "topic":    Style(LIGHT_BG, 40, INK, "START", 0.6, head_lines=1),  # 1-line headline
    "content":  Style(LIGHT_BG, None, INK, "START", 0.4),  # kicker-as-title + left body
}


def _est_lines(text: str, pt: int, width_in: float = 9.32,
               glyph_w: float = 0.64) -> int:
    """Estimate wrapped line count for a heading (font-metric free).

    The default 0.64 average-glyph-width factor is measured from IBM Plex Bold
    (headlines) and is deliberately conservative: it slightly over-counts lines
    so the reserved height never falls short of what renders (which would
    overlap the element below it). The regular-weight all-caps kicker measures
    ~0.556; its callers pass 0.58 so a one-line kicker isn't misread as two.
    """
    chars_per_line = max(1, int(width_in * 72 / (pt * glyph_w)))
    return max(1, math.ceil(len(text) / chars_per_line))


def _fit_headline_pt(text: str, base_pt: int, max_lines: int = 2,
                     width_in: float = 9.32) -> int:
    """Shrink a headline's font so it wraps to at most `max_lines` lines.

    Steps down from `base_pt` (never below ~55% of it) until the estimated
    wrapped line count fits — so a long title shrinks to stay on the slide
    instead of overflowing or pushing the body off the page.
    """
    floor = 20 if max_lines == 1 else max(20, int(base_pt * 0.55))
    pt = base_pt
    while pt > floor and _est_lines(text, pt, width_in) > max_lines:
        pt -= 2
    return pt


def _est_body_lines(paras: list[Para], pt: int, width_in: float = 9.32) -> int:
    """Estimate the wrapped line count of a bulleted/paragraph body at `pt`.

    Nested bullets wrap in a narrower column (~0.375in indent per level); the
    0.52 glyph-width factor (IBM Plex Sans regular) slightly over-counts so the
    fitted size errs toward not overflowing. An empty paragraph counts as one
    line — it reproduces the blank-line spacing authored between sections.
    """
    rows = 0
    for p in paras:
        indent = (p.depth + 1) * 0.375 if p.depth >= 0 else 0.0
        cpl = max(1, int(max(width_in - indent, 2.0) * 72 / (pt * 0.52)))
        rows += max(1, math.ceil(len(p.text) / cpl)) if p.text else 1
    return rows


def _body_height_in(paras: list[Para], pt: int, width_in: float = 9.32) -> float:
    """Estimated rendered height (inches) of a body at `pt` — ~1.2x line spacing."""
    return _est_body_lines(paras, pt, width_in) * pt * 1.2 / 72


def _fit_paras_pt(paras: list[Para], base: int = BODY_PT, floor: int = 12,
                  width_in: float = 9.32, height_in: float = 4.8) -> int:
    """Largest body size (base..floor) at which the paragraphs fit the box.

    Mirrors `_fit_headline_pt`/`_fit_body_pt`: steps down 1pt at a time from the
    desired `base` until the estimated height fits, never below a readable
    `floor`. Returns `floor` when even that overflows (the caller warns).
    """
    for pt in range(base, floor - 1, -1):
        if _body_height_in(paras, pt, width_in) <= height_in:
            return pt
    return floor


EQ_GAP_IN = 0.15  # vertical gap between stacked display equations


def _eq_sizes_in(equations) -> list[tuple[float, float]]:
    """Natural (w, h) inches of each rendered equation at its render DPI."""
    return [(px[0] / EQUATION_DPI, px[1] / EQUATION_DPI) if px else (3.0, 0.8)
            for _src, _url, px in equations]


def _eq_stack_h(sizes: list[tuple[float, float]], width_in: float = 9.32) -> float:
    """Height (inches) of the equation stack after the per-equation width clamp."""
    if not sizes:
        return 0.0
    heights = [h * min(1.0, width_in / w) for w, h in sizes]
    return sum(heights) + EQ_GAP_IN * (len(heights) - 1)


def _equation_requests(sid: str, equations, top_in: float, bottom_in: float,
                       x_in: float = 0.34, width_in: float = 9.32) -> list[dict]:
    """createImage requests for a slide's `$$...$$` display-equation stack.

    Each equation is placed at its natural rendered size (EQUATION_PT at
    EQUATION_DPI — presentation-equation scale, larger than body text), clamped
    to the region's width; the whole stack shrinks uniformly if it overflows
    [top_in, bottom_in]. Centred horizontally, and vertically within the region.
    The LaTeX source doubles as the image's accessibility description.
    """
    clamped = [(w * min(1.0, width_in / w), h * min(1.0, width_in / w))
               for w, h in _eq_sizes_in(equations)]
    total = sum(h for _, h in clamped) + EQ_GAP_IN * (len(clamped) - 1)
    avail = max(bottom_in - top_in, 0.5)
    scale = min(1.0, avail / total) if total > 0 else 1.0
    y = top_in + max(0.0, (avail - total * scale) / 2)
    reqs = []
    for i, ((w, h), (src, url, _px)) in enumerate(zip(clamped, equations)):
        w, h = w * scale, h * scale
        eq_id = f"{sid}_eq{i}"
        reqs.append({"createImage": {
            "objectId": eq_id, "url": url,
            "elementProperties": {"pageObjectId": sid,
                "size": {"width": {"magnitude": _emu(w), "unit": "EMU"},
                         "height": {"magnitude": _emu(h), "unit": "EMU"}},
                "transform": {"scaleX": 1, "scaleY": 1, "unit": "EMU",
                              "translateX": _emu(x_in + (width_in - w) / 2),
                              "translateY": _emu(y)}}}})
        reqs.append(_alt_req(eq_id, f"$${' '.join(src.split())}$$"))
        y += h + EQ_GAP_IN * scale
    return reqs


# `template: equation` — the focal equation fills this fraction of the region's
# width (the LARGE part), clamped so no single equation exceeds the height cap
# (a trivially short `$$x$$` should read big, not fill the page).
EQ_FOCUS_WIDTH_FRACTION = 0.85
EQ_FOCUS_MAX_EQ_H_IN = 2.4


def _equation_focus_requests(sid: str, equations, top_in: float, bottom_in: float,
                             x_in: float = 0.34, width_in: float = 9.32) -> list[dict]:
    """createImage requests for the equation template's focal `$$...$$` stack.

    Unlike `_equation_requests` (natural size, downscale-only), the stack is
    scaled UP so the widest equation fills `EQ_FOCUS_WIDTH_FRACTION` of the
    region's width — clamped so the whole stack fits the region's height and no
    single equation exceeds `EQ_FOCUS_MAX_EQ_H_IN`. Centred both ways; the
    LaTeX source doubles as each image's accessibility description.
    """
    sizes = _eq_sizes_in(equations)
    total = sum(h for _, h in sizes) + EQ_GAP_IN * (len(sizes) - 1)
    avail = max(bottom_in - top_in, 0.5)
    scale = min(EQ_FOCUS_WIDTH_FRACTION * width_in / max(w for w, _ in sizes),
                avail / total,
                EQ_FOCUS_MAX_EQ_H_IN / max(h for _, h in sizes))
    y = top_in + max(0.0, (avail - total * scale) / 2)
    reqs = []
    for i, ((w, h), (src, url, _px)) in enumerate(zip(sizes, equations)):
        w, h = w * scale, h * scale
        eq_id = f"{sid}_eq{i}"
        reqs.append({"createImage": {
            "objectId": eq_id, "url": url,
            "elementProperties": {"pageObjectId": sid,
                "size": {"width": {"magnitude": _emu(w), "unit": "EMU"},
                         "height": {"magnitude": _emu(h), "unit": "EMU"}},
                "transform": {"scaleX": 1, "scaleY": 1, "unit": "EMU",
                              "translateX": _emu(x_in + (width_in - w) / 2),
                              "translateY": _emu(y)}}}})
        reqs.append(_alt_req(eq_id, f"$${' '.join(src.split())}$$"))
        y += h + EQ_GAP_IN * scale
    return reqs


def _equation_template_requests(slide: Slide, equations) -> list[dict]:
    """`template: equation` — a full-slide focal display equation.

    Layout: one red all-caps kicker at the top (the `##`, styled like
    `content`'s kicker-as-title), the `$$...$$` stack rendered centred and
    LARGE (`_equation_focus_requests`), and any body text as a small centred
    caption line under the equation (the title-card byline treatment). With
    both `# h1` and `## h2` authored, the h1 is parsed but NOT rendered — the
    same way `graph` ignores title/body; it still round-trips via the marker.
    """
    sid = slide.object_id
    reqs = [{"createSlide": {"objectId": sid,
                             "slideLayoutReference": {"predefinedLayout": "BLANK"}}},
            _bg(sid, LIGHT_BG)]
    kicker_text = slide.kicker or slide.title  # h1 dropped when a kicker exists
    y = 0.4
    if kicker_text:
        reqs += _text_box(sid, sid + "_k", (0.34, y, 9.32, 0.5),
                          kicker_text, 18, RED, False)
        y += 0.6
    caption = "\n".join(p.text for p in slide.paras if p.text)
    cap_h = (caption.count("\n") + 1) * 0.32 if caption else 0.0
    eq_bottom = 5.35 - (cap_h + 0.15 if caption else 0.0)
    if equations:
        reqs += _equation_focus_requests(sid, equations, top_in=y,
                                         bottom_in=eq_bottom)
    else:
        logger.warning(f"template: equation on '{slide.key}' has no $$...$$ block")
    if caption:  # `_by` so a live caption edit writes back (_slide_from_live_boxes)
        reqs += _text_box(sid, sid + "_by", (0.34, eq_bottom + 0.15, 9.32, cap_h),
                          caption, 14, BODY_INK, False)
    if slide.image:
        logger.warning(f"image on '{slide.key}' ignored: the equation template "
                       "renders only the kicker, equation, and caption")
    return reqs


def _styled_requests(slide: Slide, style: Style, image_url, image_px,
                     equations=()) -> list[dict]:
    sid = slide.object_id
    reqs = [{"createSlide": {"objectId": sid,
                             "slideLayoutReference": {"predefinedLayout": "BLANK"}}},
            _bg(sid, style.bg)]
    if style.headline_pt is None:           # content: the kicker IS the title
        kicker_text, headline_text = slide.title, None
    else:
        kicker_text, headline_text = slide.kicker, slide.title
    head_h = 0.0
    head_pt = style.headline_pt
    if headline_text:
        assert style.headline_pt is not None  # headline_text is truthy only here
        head_pt = _fit_headline_pt(headline_text, style.headline_pt,
                                   max_lines=style.head_lines)
        head_h = _est_lines(headline_text, head_pt) * head_pt * 1.25 / 72 + 0.1
    # A long kicker wraps: reserve its true height so it never overlaps the
    # headline. One line is ~0.31in at 18pt; the legacy single-line numbers
    # (0.5in box, 0.36 advance) are reproduced exactly by text_h + 0.19/0.05.
    kick_lines = (_est_lines(kicker_text, 18, glyph_w=0.58) if kicker_text else 0)
    kick_text_h = kick_lines * 18 * 1.25 / 72
    # Title cards have no body region; body lines render as a small dimmed
    # byline beneath the headline (e.g. "Project · Presenter" on a title slide).
    byline = ""
    if style.body_align is None and slide.paras:
        byline = "\n".join(p.text for p in slide.paras if p.text)
    by_h = (byline.count("\n") + 1) * 0.32 if byline else 0.0
    # Title cards (no body, no image) vertically centre kicker+headline+byline.
    if (style.body_align is None or not slide.paras) and not slide.image:
        block = ((kick_text_h + 0.05 if kicker_text else 0) + head_h
                 + (by_h + 0.1 if byline else 0))
        y = max(0.4, (5.63 - block) / 2)
    else:
        y = style.top
    # Headings align with the body: left for START-body templates (topic/content),
    # centred for centred-body templates (question/label) and title cards.
    head_align = "START" if style.body_align == "START" else "CENTER"
    if kicker_text:
        reqs += _text_box(sid, sid + "_k", (0.34, y, 9.32, kick_text_h + 0.19),
                          kicker_text, 18, RED, False, halign=head_align)
        y += kick_text_h + 0.05  # tight kicker -> headline gap
    if headline_text:
        reqs += _text_box(sid, sid + "_h", (0.34, y, 9.32, head_h),
                          headline_text, head_pt, style.headline_rgb, True,
                          halign=head_align)
        y += head_h + 0.15
    if byline:
        rgb = MUTED if style.bg == DARK_BG else BODY_INK
        reqs += _text_box(sid, sid + "_by", (0.34, y, 9.32, by_h),
                          byline, 14, rgb, False)
        y += by_h + 0.1
    eq_top = None  # top of the display-equation stack, when one renders
    if style.body_align and slide.paras:
        bid = sid + "_b"
        # reserve the equation stack's natural height at the bottom of the
        # body region; the body text auto-fits into what remains
        eq_h = _eq_stack_h(_eq_sizes_in(equations))
        box_h = max(5.2 - y - (eq_h + 0.1 if equations else 0.0), 1.0)
        eq_top = y + box_h + 0.1
        reqs.append({"createShape": {"objectId": bid, "shapeType": "TEXT_BOX",
            "elementProperties": {"pageObjectId": sid,
                "size": {"width": {"magnitude": _emu(9.32), "unit": "EMU"},
                         "height": {"magnitude": _emu(box_h), "unit": "EMU"}},
                "transform": {"scaleX": 1, "scaleY": 1, "translateX": _emu(0.34),
                              "translateY": _emu(y), "unit": "EMU"}}}})
        body_pt = _fit_paras_pt(slide.paras, height_in=box_h)
        if _body_height_in(slide.paras, body_pt) > box_h:
            sys.exit(f"slide '{slide.key}': body overflows the slide even at "
                     f"{body_pt}pt (the minimum readable size) — split it across "
                     "slides or trim the content")
        if body_pt < BODY_PT:
            logger.warning(f"slide '{slide.key}': body auto-shrunk "
                           f"{BODY_PT}->{body_pt}pt to fit")
        reqs += _body(bid, slide.paras, align=style.body_align, size=body_pt)
    elif equations:
        eq_top = y  # no body text: centre the stack in the remaining region
    if equations and eq_top is not None:
        reqs += _equation_requests(sid, equations, top_in=eq_top, bottom_in=5.35)
    if slide.image and image_url:
        avail = _emu(max(5.4 - y, 1.5))
        w, h = _fit2(image_px, _emu(9.32), avail)
        reqs.append({"createImage": {
            "objectId": sid + "_img", "url": image_url,
            "elementProperties": {"pageObjectId": sid,
                "size": {"width": {"magnitude": w, "unit": "EMU"},
                         "height": {"magnitude": h, "unit": "EMU"}},
                "transform": {"scaleX": 1, "scaleY": 1, "unit": "EMU",
                              "translateX": (SLIDE_W - w) // 2,
                              "translateY": _emu(y)}}}})
        reqs += _image_meta_reqs(sid + "_img", slide)
    return reqs


def _fit2(px, max_w, max_h):
    if not px:
        return max_w, max_h
    w, h = px[0] * EMU_PER_PX, px[1] * EMU_PER_PX
    scale = min(max_w / w, max_h / h, 1.0)
    return int(w * scale), int(h * scale)


# The single link-only line a graph slide may carry (the trace-link
# convention) renders as a discreet right-aligned footer strip.
GRAPH_FOOTER_PT = 11
GRAPH_FOOTER_H = 0.28   # one 11pt line + padding, inches
GRAPH_FOOTER_GAP = 0.06  # clearance between image and footer, inches


def _graph_footer_para(slide: Slide) -> Para | None:
    return next((p for p in slide.paras if p.text and _link_only_para(p)), None)


def _graph_requests(slide: Slide, image_url, image_px) -> list[dict]:
    """Full-bleed graph: a single image maximised to fill the page, no text —
    except one optional link-only line, rendered as a right-aligned footer.

    For `template: graph` / `full` slides — the figure is self-titled, so the
    slide carries no kicker, headline, or body. The image is scaled to fit the
    slide (aspect preserved) with a thin margin and centred both ways. With a
    footer, a wide image whose centred fit already leaves the footer strip
    free keeps its full size; only an image tall enough to collide shrinks.
    """
    sid = slide.object_id
    reqs = [{"createSlide": {"objectId": sid,
                             "slideLayoutReference": {"predefinedLayout": "BLANK"}}},
            _bg(sid, WHITE)]
    footer = _graph_footer_para(slide)
    if slide.image and image_url:
        margin = _emu(0.1)
        avail_h = SLIDE_H - 2 * margin
        w, h = _fit2(image_px, SLIDE_W - 2 * margin, avail_h)
        ty = (SLIDE_H - h) // 2
        if footer is not None:
            strip = _emu(GRAPH_FOOTER_H + GRAPH_FOOTER_GAP)
            if (SLIDE_H - h) // 2 < strip:  # aspect ratio leaves no room: shrink
                w, h = _fit2(image_px, SLIDE_W - 2 * margin, avail_h - strip)
                ty = margin + (avail_h - strip - h) // 2
        reqs.append({"createImage": {
            "objectId": sid + "_img", "url": image_url,
            "elementProperties": {"pageObjectId": sid,
                "size": {"width": {"magnitude": w, "unit": "EMU"},
                         "height": {"magnitude": h, "unit": "EMU"}},
                "transform": {"scaleX": 1, "scaleY": 1, "unit": "EMU",
                              "translateX": (SLIDE_W - w) // 2,
                              "translateY": ty}}}})
        reqs += _image_meta_reqs(sid + "_img", slide)
    if footer is not None:
        fid = sid + "_links"
        y = SLIDE_H / EMU_PER_IN - 0.1 - GRAPH_FOOTER_H
        reqs.append({"createShape": {"objectId": fid, "shapeType": "TEXT_BOX",
            "elementProperties": {"pageObjectId": sid,
                "size": {"width": {"magnitude": _emu(9.32), "unit": "EMU"},
                         "height": {"magnitude": _emu(GRAPH_FOOTER_H), "unit": "EMU"}},
                "transform": {"scaleX": 1, "scaleY": 1, "translateX": _emu(0.34),
                              "translateY": _emu(y), "unit": "EMU"}}}})
        reqs += _body(fid, [footer], align="END", size=GRAPH_FOOTER_PT)
    return reqs


def _literal_requests(raw: str, sid: str, where: str) -> list[dict] | None:
    """Parse a literal Slides-API-requests block; `__PAGE__` -> `sid`.

    Accepts a JSON list of requests or `{"requests": [...]}`. Returns `None`
    (with the cause logged) on malformed input so callers can degrade —
    render what they can — instead of aborting the push.
    """
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error(f"{where}: invalid JSON ({exc})")
        return None
    body = payload.get("requests", payload) if isinstance(payload, dict) else payload
    if not isinstance(body, list):
        logger.error(f"{where}: expected a list of requests or {{'requests': [...]}}")
        return None
    return json.loads(json.dumps(body).replace("__PAGE__", sid))


def _custom_requests(slide: Slide) -> list[dict]:
    """Build a custom slide by replaying its literal Slides API requests.

    The ```gslides``` block holds either a list of requests or `{"requests":
    [...]}`. The page is created blank; `__PAGE__` in the JSON is substituted
    with this slide's objectId (so element ids embedding it stay unique).
    """
    assert slide.custom is not None  # only called for custom (```gslides```) slides
    sid = slide.object_id
    reqs = [{"createSlide": {"objectId": sid,
                             "slideLayoutReference": {"predefinedLayout": "BLANK"}}}]
    body = _literal_requests(slide.custom, sid, f"custom slide '{slide.key}'")
    if body is None:
        logger.error(f"custom slide '{slide.key}': left blank")
        return reqs
    return reqs + body


def _overlay_requests(slide: Slide) -> list[dict]:
    """Replay a ```gslides-overlay``` block on top of a templated slide's render.

    Unlike ```gslides``` (whole-slide custom, pull-authoritative), an overlay
    rides on a normal templated/generative slide: its requests are appended
    after the slide's own render on every (re)push, with `__PAGE__` substituted
    by the slide's objectId. The markdown is the source of truth — editing the
    drawn elements natively in Slides is not written back, and a
    content-changing push recreates them from the block.
    """
    if slide.overlay is None:
        return []
    body = _literal_requests(slide.overlay, slide.object_id,
                             f"overlay on '{slide.key}'")
    if body is None:
        logger.error(f"overlay on '{slide.key}': skipped")
        return []
    return body


def _fit_body_pt(text: str, base: int = 11, floor: int = 6,
                 width_in: float = 9.32, height_in: float = 4.65) -> int:
    """Largest mono size (base..floor) at which the verbatim text fits the box."""
    src = text.split("\n")
    for pt in range(base, floor - 1, -1):
        cpl = max(1, int(width_in * 72 / (pt * 0.62)))   # mono glyph ~0.62*pt wide
        rows = sum(max(1, math.ceil(len(ln) / cpl)) for ln in src)
        if rows * pt * 1.18 / 72 <= height_in:  # ~90% line spacing (see _prompt_requests)
            return pt
    return floor


def _prompt_requests(slide: Slide) -> list[dict]:
    """Verbatim prompt/code slide: red kicker title + the fenced body in mono.

    For `template: prompt` / `code`. The ``` ``` block is rendered byte-for-byte
    (no markdown parsing, so numbered lists / bullets survive) at the largest
    Roboto Mono size that fits the slide, so even a long system prompt stays on
    one slide.
    """
    sid = slide.object_id
    reqs = [{"createSlide": {"objectId": sid,
                             "slideLayoutReference": {"predefinedLayout": "BLANK"}}},
            _bg(sid, LIGHT_BG)]
    if slide.title:
        reqs += _text_box(sid, sid + "_k", (0.34, 0.28, 9.32, 0.5),
                          slide.title, 16, RED, True)
    body = slide.verbatim if slide.verbatim is not None else \
        "\n\n".join(p.text for p in slide.paras if p.text)
    if body:
        bid = sid + "_b"
        reqs.append({"createShape": {"objectId": bid, "shapeType": "TEXT_BOX",
            "elementProperties": {"pageObjectId": sid,
                "size": {"width": {"magnitude": _emu(9.32), "unit": "EMU"},
                         "height": {"magnitude": _emu(4.65), "unit": "EMU"}},
                "transform": {"scaleX": 1, "scaleY": 1, "translateX": _emu(0.34),
                              "translateY": _emu(0.85), "unit": "EMU"}}}})
        reqs.append({"insertText": {"objectId": bid, "text": body}})
        reqs.append({"updateTextStyle": {"objectId": bid, "textRange": {"type": "ALL"},
            "style": {"fontFamily": "Roboto Mono",
                      "fontSize": {"magnitude": _fit_body_pt(body), "unit": "PT"},
                      "foregroundColor": {"opaqueColor": {"rgbColor": BODY_INK}}},
            "fields": "fontFamily,fontSize,foregroundColor"}})
        reqs.append({"updateParagraphStyle": {"objectId": bid, "textRange": {"type": "ALL"},
            "style": {"lineSpacing": 90, "spaceAbove": {"magnitude": 0, "unit": "PT"},
                      "spaceBelow": {"magnitude": 0, "unit": "PT"}},
            "fields": "lineSpacing,spaceAbove,spaceBelow"}})
    return reqs


def _warn_dropped_equations(slide: Slide, where: str) -> None:
    if slide.equations:
        logger.warning(f"$$...$$ equations on '{slide.key}' ignored: "
                       f"{where} slides have no equation region")


def _link_only_para(p: Para) -> bool:
    """A paragraph that is nothing but links and separators (the crop →
    full-figure trace-link convention) — markdown-side navigation with nothing
    the slide is expected to render."""
    covered = [False] * len(p.text)
    for r in p.runs:
        if r.style == "link":
            for i in range(r.start, min(r.end, len(p.text))):
                covered[i] = True
    outside = "".join(c for i, c in enumerate(p.text) if not covered[i])
    return bool(p.runs) and not re.sub(r"[\s·|/,;&+–—-]", "", outside)


def validate_slots(slides: list[Slide]) -> list[str]:
    """Authored content a slide's template has no slot for, one entry each.

    A mismatch is content the render silently DROPS — a headline or prose
    caption on a text-free `graph`/`full` template, an `# h1` on an `equation`
    slide (only the `##` kicker renders), an image on a `prompt`/`code` or
    `equation` slide. Callers fail the push up front rather than publish a
    deck missing what the author wrote. Exempt: link-only paragraphs on
    text-free templates (the trace-link convention) and comments (they become
    speaker notes everywhere).
    """
    out: list[str] = []

    def bad(slide: Slide, what: str, fix: str) -> None:
        out.append(f"'{slide.key}' (template: {slide.template_name}): "
                   f"{what} — {fix}")

    for s in slides:
        if s.custom is not None:
            continue
        tpl = (s.template_name or "").lower()
        if tpl in ("graph", "full"):
            if s.title or s.kicker:
                bad(s, f"heading {(s.title or s.kicker)[:60]!r} never renders "
                       "(text-free template)",
                    "drop it, move it into a <!-- comment --> (speaker notes), "
                    "or use a template with a text slot (e.g. topic)")
            if s.table:
                bad(s, "a table never renders (text-free template)",
                    "move it to a content/topic slide")
            prose = [p for p in s.paras if p.text and not _link_only_para(p)]
            if prose:
                bad(s, f"body text {prose[0].text[:60]!r} never renders "
                       "(text-free template; link-only lines render as the footer)",
                    "move it into a <!-- comment --> (speaker notes)")
            links = [p for p in s.paras if p.text and _link_only_para(p)]
            if len(links) > 1:
                bad(s, f"{len(links)} link-only lines, but only ONE footer "
                       "renders (right-aligned, bottom)",
                    "merge them into a single ` · `-separated line")
        elif tpl == "equation":
            if s.title and s.kicker:
                bad(s, f"`# {s.title[:60]}` never renders (only the `##` "
                       "kicker does)",
                    "fold it into the `##` line or a comment")
            if s.image:
                bad(s, "an image never renders on an equation slide",
                    "move it to a graph slide")
        elif tpl in ("prompt", "code"):
            if s.image:
                bad(s, "an image never renders on a prompt/code slide",
                    "move it to a graph slide")
            if s.table:
                bad(s, "a table never renders on a prompt/code slide",
                    "move it to a content slide")
    return out


def slide_requests(slide: Slide, image_url, image_px,
                   layouts=None, templates=None, equations=()) -> list[dict]:
    """Full request list for one slide: its template/generative render, plus any
    ```gslides-overlay``` block replayed on top (custom slides carry no overlay)."""
    reqs = _templated_requests(slide, image_url, image_px, layouts=layouts,
                               templates=templates, equations=equations)
    return reqs + _overlay_requests(slide)


def _templated_requests(slide: Slide, image_url, image_px,
                        layouts=None, templates=None, equations=()) -> list[dict]:
    if slide.custom is not None:
        _warn_dropped_equations(slide, "custom (```gslides```)")
        return _custom_requests(slide)
    tpl = (slide.template_name or "").lower()
    if tpl in ("graph", "full"):
        _warn_dropped_equations(slide, "graph/full")
        return _graph_requests(slide, image_url, image_px)
    if tpl in ("prompt", "code"):
        _warn_dropped_equations(slide, "prompt/code")
        return _prompt_requests(slide)
    if tpl == "equation":
        return _equation_template_requests(slide, equations)
    if tpl in STYLES:
        return _styled_requests(slide, STYLES[tpl], image_url, image_px,
                                equations=equations)
    if templates and tpl in templates:
        _warn_dropped_equations(slide, "tagged-template")
        return _tagged_requests(slide, templates[tpl])
    name = (slide.layout_name or "").lower()
    if layouts and name in layouts and slide.layout_name not in SECTION_LAYOUTS:
        _warn_dropped_equations(slide, "themed-layout")
        return _layout_requests(slide, layouts[name])
    tid = slide.object_id + "_t"
    # Styling an empty TITLE box is a Slides API 400 ("The object ... has no
    # text"), and _insert skips empty text — so every title style below is
    # gated on the title actually existing.
    title_style = _title_style_gate(slide.title)
    if slide.layout == "section":
        reqs = _create(slide, "TITLE", [("CENTERED_TITLE", tid)])
        reqs += _insert(tid, slide.title)
        reqs += [_bg(slide.object_id, DARK_BG)]
        reqs += title_style([_font_all(tid, PAPER, bold=True)])
    elif slide.layout == "content":
        bid = slide.object_id + "_b"
        reqs = _create(slide, "TITLE_AND_BODY", [("TITLE", tid), ("BODY", bid)])
        reqs += _insert(tid, slide.title) + _body(bid, slide.paras)
        reqs += [_bg(slide.object_id, LIGHT_BG)] + title_style(_kicker(tid))
    else:
        reqs = _create(slide, "TITLE_ONLY", [("TITLE", tid)])
        reqs += _insert(tid, slide.title)
        reqs += [_bg(slide.object_id, LIGHT_BG)] + title_style(_kicker(tid))
        if slide.layout == "image" and image_url:
            reqs.append(_image(slide, image_url, image_px))
            reqs += _image_meta_reqs(slide.object_id + "_img", slide)
        elif slide.layout == "table" and slide.table:
            reqs += _table(slide)
    if equations:
        body_x, body_w = BODY_X / EMU_PER_IN, BODY_W / EMU_PER_IN
        top, bottom = BODY_Y / EMU_PER_IN, (BODY_Y + BODY_H) / EMU_PER_IN
        if slide.paras:  # text above: anchor the stack to the region's bottom
            top = bottom - _eq_stack_h(_eq_sizes_in(equations), width_in=body_w)
        reqs += _equation_requests(slide.object_id, equations, top_in=top,
                                   bottom_in=bottom, x_in=body_x, width_in=body_w)
    return reqs


def _title_style_gate(title: str):
    """Requests pass through only when the slide has a title to style.

    A style request against an empty text box 400s the whole batchUpdate,
    so title-less slides (comment-only placeholders, bare equations,
    bullets-only bodies) must skip their title styling entirely.
    """
    def gate(reqs: list[dict]) -> list[dict]:
        return reqs if title else []
    return gate


def _plain_body(paras: list[Para]) -> str:
    return "\n".join(p.text for p in paras)


def _tagged_requests(slide: Slide, template_id: str) -> list[dict]:
    """Duplicate a styled template slide and interpolate {{token}} text."""
    reqs = [{"duplicateObject": {"objectId": template_id,
                                 "objectIds": {template_id: slide.object_id}}}]
    tokens = {"h1": slide.title, "title": slide.title, "h2": slide.kicker,
              "body": _plain_body(slide.paras), **slide.vars}
    for tok, val in tokens.items():
        reqs.append({"replaceAllText": {
            "containsText": {"text": "{{" + tok + "}}", "matchCase": True},
            "replaceText": str(val), "pageObjectIds": [slide.object_id]}})
    return reqs


def _layout_requests(slide: Slide, lay: dict) -> list[dict]:
    """Fill a themed master layout's placeholders from the slide's structure."""
    ids, mappings = {}, []
    for t, idx in lay["ph"]:
        if t not in FILLABLE:
            continue
        oid = f"{slide.object_id}_{t}{idx}"
        ids[(t, idx)] = oid
        mappings.append({"layoutPlaceholder": {"type": t, "index": idx},
                         "objectId": oid})
    reqs = [{"createSlide": {
        "objectId": slide.object_id,
        "slideLayoutReference": {"layoutId": lay["id"]},
        "placeholderIdMappings": mappings,
    }}]
    title_key = ("TITLE", 0) if ("TITLE", 0) in ids else ("CENTERED_TITLE", 0)
    if title_key in ids:
        reqs += _insert(ids[title_key], slide.title)
    if ("BODY", 0) in ids:
        reqs += _body(ids[("BODY", 0)], slide.paras)
    return reqs


def _create(slide, layout, placeholders) -> list[dict]:
    mappings = [{"layoutPlaceholder": {"type": t, "index": 0}, "objectId": oid}
                for t, oid in placeholders]
    return [{"createSlide": {
        "objectId": slide.object_id,
        "slideLayoutReference": {"predefinedLayout": layout},
        "placeholderIdMappings": mappings,
    }}]


def _insert(obj_id, text) -> list[dict]:
    return [{"insertText": {"objectId": obj_id, "text": text}}] if text else []


def _body(bid: str, paras: list[Para], align: str = "START",
          size: int | None = None) -> list[dict]:
    lines = ["\t" * max(p.depth, 0) + p.text for p in paras]
    full = "\n".join(lines)
    if not full:
        return []
    reqs = [{"insertText": {"objectId": bid, "text": full}},
            _font_all(bid, BODY_INK)]  # base brand font; inline runs override below
    # Styled templates pass an explicit size (auto-fit to the box); placeholder
    # paths leave it None so the layout's own autofit governs the body.
    if size is not None:
        reqs.append({"updateTextStyle": {"objectId": bid,
            "textRange": {"type": "ALL"},
            "style": {"fontSize": {"magnitude": size, "unit": "PT"}},
            "fields": "fontSize"}})
    bullets, off = [], 0
    for line, p in zip(lines, paras):
        cbase = off + _u16("\t" * max(p.depth, 0))
        for r in p.runs:
            if r.style == "link" and r.link is not None and r.link.startswith("#"):
                continue  # internal slide links: resolved in _apply_internal_links
            s = cbase + _u16(p.text[:r.start])
            e = cbase + _u16(p.text[:r.end])
            reqs.append(_style(bid, s, e, r))
        if p.depth >= 0:
            bullets.append((off, off + _u16(line), p.ordered))
        off += _u16(line) + 1
    total = _u16(full)
    bullets = [(s, min(e, total), o) for s, e, o in bullets if s < min(e, total)]
    reqs += _bullets(bid, bullets)
    reqs.append({"updateParagraphStyle": {"objectId": bid,
        "textRange": {"type": "ALL"}, "style": {"alignment": align},
        "fields": "alignment"}})
    return reqs


_LINK_FIELDS = "link,foregroundColor,underline"


def _link_style(link: dict) -> dict:
    """Brand-red, underlined link styling — overrides the theme hyperlink colour."""
    return {"link": link,
            "foregroundColor": {"opaqueColor": {"rgbColor": RED}},
            "underline": True}


def _style(obj_id, start, end, run: Run) -> dict:
    rng = {"type": "FIXED_RANGE", "startIndex": start, "endIndex": end}
    if run.style == "link":
        return {"updateTextStyle": {"objectId": obj_id, "textRange": rng,
                                    "style": _link_style({"url": run.link}),
                                    "fields": _LINK_FIELDS}}
    style, fields = STYLE[run.style]
    return {"updateTextStyle": {"objectId": obj_id, "textRange": rng,
                                "style": style, "fields": fields}}


def _bullets(bid, spans) -> list[dict]:
    out = []
    # Apply right-to-left: createParagraphBullets removes the leading tabs used
    # for nesting, shrinking the text, which would shift later groups' indices.
    for s, e, ordered in reversed(_merge(spans)):
        preset = "NUMBERED_DIGIT_ALPHA_ROMAN" if ordered \
            else "BULLET_DISC_CIRCLE_SQUARE"
        out.append({"createParagraphBullets": {
            "objectId": bid,
            "textRange": {"type": "FIXED_RANGE", "startIndex": s, "endIndex": e},
            "bulletPreset": preset,
        }})
    return out


def _merge(spans):
    merged = []
    for s, e, ordered in sorted(spans):
        if merged and ordered == merged[-1][2] and s <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], e, ordered)
        else:
            merged.append((s, e, ordered))
    return merged


def _alt_req(object_id: str, alt: str) -> dict:
    """Set an image element's accessibility alt text (its description)."""
    return {"updatePageElementAltText": {"objectId": object_id, "description": alt}}


def _image_meta_reqs(object_id: str, slide: Slide) -> list[dict]:
    """Alt text + click-through link for a slide's image element, when set.

    A relative href (the repo-file convention, `[![...](deck.png)](full.png)`)
    is meaningful in the markdown but not a valid Slides link target, so only
    absolute http(s) hrefs are applied to the live element; the wrapper still
    round-trips through the authored source either way.
    """
    reqs = []
    if slide.image_alt:
        reqs.append(_alt_req(object_id, slide.image_alt))
    if slide.image_link and re.match(r"^https?://", slide.image_link):
        reqs.append({"updateImageProperties": {"objectId": object_id,
            "imageProperties": {"link": {"url": slide.image_link}},
            "fields": "link"}})
    return reqs


def _image(slide, url, px) -> dict:
    w, h = _fit(px)
    return {"createImage": {
        "objectId": slide.object_id + "_img", "url": url,
        "elementProperties": {
            "pageObjectId": slide.object_id,
            "size": {"width": {"magnitude": w, "unit": "EMU"},
                     "height": {"magnitude": h, "unit": "EMU"}},
            "transform": {"scaleX": 1, "scaleY": 1, "unit": "EMU",
                          "translateX": (SLIDE_W - w) // 2, "translateY": BODY_Y},
        },
    }}


def _fit(px):
    if not px:
        return BODY_W, BODY_H
    w, h = px[0] * EMU_PER_PX, px[1] * EMU_PER_PX
    scale = min(BODY_W / w, BODY_H / h, 1.0)
    return int(w * scale), int(h * scale)


def _table(slide) -> list[dict]:
    rows, cols = len(slide.table), len(slide.table[0])
    tid = slide.object_id + "_tbl"
    reqs = [{"createTable": {
        "objectId": tid,
        "elementProperties": {
            "pageObjectId": slide.object_id,
            "transform": {"scaleX": 1, "scaleY": 1, "unit": "EMU",
                          "translateX": BODY_X, "translateY": BODY_Y},
        },
        "rows": rows, "columns": cols,
    }}]
    for r, row in enumerate(slide.table):
        for c, cell in enumerate(row):
            if cell:
                reqs.append({"insertText": {
                    "objectId": tid, "text": cell,
                    "cellLocation": {"rowIndex": r, "columnIndex": c}}})
                reqs.append(_font_all(tid, INK if r == 0 else BODY_INK,
                                      bold=(r == 0), cell=(r, c)))
    return reqs


# ---------------------------------------------------------------------------
# Push:  diff + execute
# ---------------------------------------------------------------------------


def managed_slides(slides_api, deck, pres=None) -> dict[str, tuple[str, str]]:
    pres = pres or slides_api.presentations().get(presentationId=deck).execute()
    out = {}
    for s in pres.get("slides", []):
        if MANAGED_RE.match(s["objectId"]):
            _, kh, ch = s["objectId"].split("_")
            out[kh] = (s["objectId"], ch)
    return out


def _template_index(slides_api, deck, pres=None) -> dict[str, str]:
    """name(lower) -> objectId for slides tagged `<!-- s2g:template NAME -->`."""
    pres = pres or slides_api.presentations().get(presentationId=deck).execute()
    out = {}
    for s in pres.get("slides", []):
        m = TEMPLATE_TAG_RE.search(_read_notes(s))
        if m:
            out[m.group("name").lower()] = s["objectId"]
    return out


def _layout_map(slides_api, deck, pres=None) -> dict[str, dict]:
    """displayName(lower) -> {id, ph:[(type,index)]} for the deck's master layouts."""
    pres = pres or slides_api.presentations().get(presentationId=deck).execute()
    out = {}
    for lay in pres.get("layouts", []):
        name = lay.get("layoutProperties", {}).get("displayName")
        if not name:
            continue
        ph = [(el["shape"]["placeholder"].get("type"),
               el["shape"]["placeholder"].get("index", 0))
              for el in lay.get("pageElements", [])
              if el.get("shape", {}).get("placeholder")]
        out[name.lower()] = {"id": lay["objectId"], "ph": ph}
    return out


def plan_sync(source: list[Slide], managed: dict, prune: bool, force: bool = False):
    creates, deletes, skips = [], [], []
    for s in source:
        if s.custom is not None:
            # Pull-authoritative: keep the live (hand-drawn) slide if it exists;
            # only (re)push when it's missing. Never clobbered, even with --force.
            (skips if s.key_hash in managed else creates).append(s)
            continue
        if s.key_hash in managed:
            old_id, old_ch = managed[s.key_hash]
            if old_ch == s.content_hash and not force:
                skips.append(s)
            else:                       # changed, or force-re-render
                creates.append(s)
                deletes.append(old_id)
        else:
            creates.append(s)
    keys = {s.key_hash for s in source}
    pruned = [oid for kh, (oid, _) in managed.items() if kh not in keys and prune]
    return creates, deletes, skips, pruned


# Mass re-key guard: floor and deck-fraction for refusing a push that would
# recreate the deck wholesale under new ids (see mass_rekey). The threshold is
# max(floor, fraction * managed), so small decks (< REKEY_MIN_SLIDES managed
# slides) never trip it and big decks need a deck-scale volume of re-keyed
# slides, not just a handful of stale ones.
REKEY_MIN_SLIDES = 10
REKEY_MANAGED_FRACTION = 0.30


def mass_rekey(source: list[Slide], managed: dict) -> tuple[int, int] | None:
    """Detect a plan that would recreate the deck wholesale under new objectIds.

    When the objectId/keyHash scheme changes (or a bug perturbs key
    computation), every local slide suddenly matches no live slide: the plan
    sees a brand-new deck to create, while every existing `s2g_` slide on the
    deck matches no local key. Pushing that plan recreates all slides from
    markdown, so live styling/edits on the old copies are lost (`--prune`
    deletes them outright; without it they linger as duplicates). This
    happened on the 0.10.2 upgrade: a routine sync saw all 391 managed slides
    as missing, recreated them, and wiped live text highlights applied minutes
    earlier.

    Returns (new_key_creates, unmatched_live) when BOTH counts reach
    max(REKEY_MIN_SLIDES, REKEY_MANAGED_FRACTION * len(managed)) — the caller
    refuses unless --allow-rekey — else None. A normal push never triggers: a
    few new slides leave unmatched_live near zero, and mass *content* edits
    keep every key_hash matched, so new_key_creates stays near zero.
    """
    if not managed:
        return None  # brand-new/empty deck: nothing live to lose
    local_khs = {s.key_hash for s in source}
    new_key_creates = sum(1 for s in source if s.key_hash not in managed)
    unmatched_live = sum(1 for kh in managed if kh not in local_khs)
    threshold = max(REKEY_MIN_SLIDES,
                    math.ceil(REKEY_MANAGED_FRACTION * len(managed)))
    if new_key_creates >= threshold and unmatched_live >= threshold:
        return new_key_creates, unmatched_live
    return None


def _swap_requests(new_reqs: list[dict], new_id: str, old_id: str,
                   old_index: int) -> list[dict]:
    """Blue-green swap for one replaced slide, as a single ordered run.

    A content change gives a slide a new objectId (the content_hash moves). Rather
    than delete the old object then re-create the new one — which leaves a window
    where the slide is momentarily absent (and, on a batch the API splits, can drop
    it) — emit, in order: CREATE the new object (its `createSlide` + content/notes
    requests), reposition it into the OLD slide's index, then DELETE the old object.
    Google applies a batch's requests in order, so the create necessarily precedes
    the position+delete and the slide is never missing or duplicated in view.

    `old_index` is the old slide's index in the deck *before* this batch runs.
    `createSlide` appends the new object at the end, so at the `updateSlidesPosition`
    step the order is `[…, old@old_index, …, new]`; an `insertionIndex` of
    `old_index` (computed, per the API, on the pre-move arrangement) drops the new
    object just before the old one, and the trailing delete leaves it exactly in the
    old slide's slot. Each swap is position-preserving for every other slide, so a
    batch with several swaps can use each old slide's *initial* index unadjusted.

    Limitation: Google Slides comments anchored to the old object's page cannot be
    moved by the API, so this preserves the slide's POSITION, content, and speaker
    notes and removes the gap, but does NOT carry comments over to the new object
    (captured comment *threads* are re-anchored separately by `_restore_threads`).
    """
    return [
        *new_reqs,
        {"updateSlidesPosition": {"slideObjectIds": [new_id],
                                  "insertionIndex": old_index}},
        {"deleteObject": {"objectId": old_id}},
    ]


def push(slides_api, drive, deck, source, anchor, prune, base_dir=Path("."),
         force=False, allow_rekey=False) -> dict:
    pres = slides_api.presentations().get(presentationId=deck).execute()
    managed = managed_slides(slides_api, deck, pres)
    creates, deletes, skips, pruned = plan_sync(source, managed, prune, force)
    if not (creates or deletes or pruned):  # nothing changed — skip reorder/links/gets
        return {"create": 0, "skip": len(skips), "replace": 0, "prune": 0}
    rekey = mass_rekey(source, managed)
    if rekey and not allow_rekey:
        # Deliberately NOT bypassed by --force: force answers "overwrite live
        # edits on slides I still match"; a mass re-key means matching itself
        # broke, and force-retry loops are exactly how the wipe happens.
        new_keys, orphaned = rekey
        sys.exit(f"push rejected: mass re-key detected — {new_keys} slides would "
                 f"be recreated under new ids while {orphaned} live s2g slides "
                 "match no local slide; live edits/styling on the old copies "
                 "would be lost. Run `slidesync pull`/`slidesync sync` to capture "
                 "the live deck first, then re-push with --allow-rekey.")
    if not force:
        # Non-fast-forward guard: replacing or pruning a slide that was edited
        # in Google Slides since its last push would silently discard that edit.
        risks = _clobber_risks(pres, managed, source, pruned)
        if risks:
            for key in risks:
                logger.error(f"live edit would be lost: {key}")
            sys.exit("push rejected: the deck changed in Google Slides since the "
                     "last push (slides above). Run `slidesync sync` to reconcile, "
                     "or `push --force` to overwrite.")
    layouts = _layout_map(slides_api, deck, pres)
    templates = _template_index(slides_api, deck, pres)
    # A replace's old objectId carries the slide's key_hash, which pairs it with
    # the freshly-built slide that supersedes it (same key). A *content* change
    # moves the content_hash, so the new objectId differs — a blue-green swap.
    # `--force` also re-renders unchanged slides; there the content_hash (hence
    # objectId) is identical, so there is nothing to swap into place — the old
    # object must be deleted before its id can be re-created (a same-id refresh).
    new_by_kh = {s.key_hash: s for s in creates}
    swaps = {old: new_by_kh[old.split("_")[1]] for old in deletes
             if new_by_kh[old.split("_")[1]].object_id != old}
    refresh_ids = {old for old in deletes if old not in swaps}  # force, same id
    swapped_in = {new.object_id for new in swaps.values()}
    order = [s["objectId"] for s in pres.get("slides", [])]
    create_set = set(id(s) for s in creates)

    def build(slide: Slide) -> list[dict]:
        url, px = _resolve_image(drive, slide, base_dir)
        return slide_requests(slide, url, px, layouts, templates,
                              equations=_resolve_equations(drive, slide))

    # Order within the single batch: blue-green swaps FIRST (each position-
    # preserving, so every old index stays valid across the run), then plain
    # deletes — same-id refreshes and prunes — which shift indices and so must
    # follow the swaps (a same-id refresh's create is appended in the loop below,
    # after its delete frees the id), then brand-new slides (appended and ordered
    # by the _reorder pass — a new slide has nothing to lose to a momentary gap).
    reqs: list[dict] = []
    for old_id, new in swaps.items():
        reqs += _swap_requests(build(new), new.object_id, old_id,
                               order.index(old_id))
    reqs += [{"deleteObject": {"objectId": oid}}
             for oid in list(refresh_ids) + pruned]
    for s in source:
        if id(s) in create_set and s.object_id not in swapped_in:
            reqs += build(s)  # brand-new slides + force same-id refreshes
    if reqs:
        _batch(slides_api, deck, reqs)
    _apply_notes(slides_api, deck, creates)
    _apply_skip(slides_api, deck, creates)
    _restore_threads(drive, deck, creates)
    _reorder(slides_api, deck, source, anchor)
    _apply_internal_links(slides_api, deck, source)
    return {"create": len(creates), "skip": len(skips),
            "replace": len(deletes), "prune": len(pruned)}


def _restore_threads(drive, deck, creates) -> None:
    """Keep captured comment threads alive as real Slides comments.

    A replace gives a slide a new objectId, orphaning any thread anchored to
    the old page. For every re-created slide whose source mirrors a thread
    (`<!-- @Author: ... -->`), find the matching unresolved live thread and, if
    it dangles, re-create it anchored to the new page (replies as replies) and
    delete the dangling original. API constraint: the re-created thread is
    authored by the authenticated account. Threads resolved in Slides are NOT
    revived — resolution is how you retire a captured comment.
    """
    slides_with = [(s, _thread_blocks(s.src)) for s in creates
                   if s.src and s.custom is None and "@" in s.src]
    slides_with = [(s, blocks) for s, blocks in slides_with if blocks]
    if not slides_with:
        return
    live = shape_comments(list_comments(drive, deck))
    me = drive.about().get(
        fields="user(displayName)").execute()["user"]["displayName"]

    def bare(content: str) -> str:
        # A re-created foreign thread carries its attribution in-content
        # ("@Fabien: ..."); strip it so every generation of a thread matches.
        return " ".join(re.sub(r"^@[^:]+:\s*", "", content).split())

    for s, blocks in slides_with:
        for entries in blocks:
            _author, head = entries[0]
            norm = " ".join(head.split())
            matches = [c for c in live if not c["resolved"]
                       and bare(c["content"]) == norm]
            if not matches or any(c["page"] == s.object_id for c in matches):
                continue  # resolved/deleted (don't revive) or already anchored
            content = matches[0]["content"]
            if matches[0]["author"] not in ("", me) \
                    and not content.lstrip().startswith("@"):
                content = f"@{matches[0]['author']}: {content}"
            anchor_json = json.dumps({"type": "page", "pages": [s.object_id]})
            new = drive.comments().create(
                fileId=deck, body={"content": content, "anchor": anchor_json},
                fields="id").execute()
            for r in matches[0]["replies"]:
                drive.replies().create(
                    fileId=deck, commentId=new["id"],
                    body={"content": f"@{r['author']}: {r['content']}"
                          if r["author"] not in ("", me) else r["content"]},
                    fields="id").execute()
            for c in matches:  # retire every stale copy we are allowed to
                try:
                    drive.comments().delete(fileId=deck,
                                            commentId=c["id"]).execute()
                except Exception as exc:  # noqa: BLE001 — foreign-authored
                    logger.warning(f"left dangling thread {c['id']} by "
                                   f"{c['author']} in place (not deletable): {exc}")
            logger.info(f"re-anchored comment thread on '{s.key}'")


# Templates with no body region, so they cannot host an internal-link run
# (the equation template's caption renders as plain text, runs dropped).
NOBODY_TEMPLATES = {"dark", "title", "appendix", "graph", "full", "equation"}


def _apply_internal_links(slides_api, deck, source) -> None:
    """Resolve `[text](#key)` body links to native Slides slide links.

    Runs over ALL source slides on every push (not only created ones) so a link
    stays valid even when its *target* slide's content — hence objectId —
    changes while the linking slide is unchanged. Title links are dropped at
    parse time, so only body (`_b`) runs carry internal links.
    """
    key_to_oid = {s.key: s.object_id for s in source}
    reqs = []
    for s in source:
        if (s.template_name or "").lower() in NOBODY_TEMPLATES:
            _warn_orphan_links(s)
            continue
        bid = s.object_id + "_b"
        off = 0
        for p in s.paras:
            for r in p.runs:
                if r.style == "link" and r.link.startswith("#"):
                    oid = key_to_oid.get(r.link[1:])
                    if not oid:
                        logger.warning(f"internal link {r.link} on '{s.key}' "
                                       "has no matching slide id")
                        continue
                    reqs.append({"updateTextStyle": {"objectId": bid,
                        "textRange": {"type": "FIXED_RANGE",
                                      "startIndex": off + _u16(p.text[:r.start]),
                                      "endIndex": off + _u16(p.text[:r.end])},
                        "style": _link_style({"pageObjectId": oid}),
                        "fields": _LINK_FIELDS}})
            off += _u16(p.text) + 1
    if reqs:
        _batch(slides_api, deck, reqs)


def _warn_orphan_links(slide: Slide) -> None:
    if any(r.style == "link" and r.link is not None and r.link.startswith("#")
           for p in slide.paras for r in p.runs):
        logger.warning(f"internal link on '{slide.key}' ignored: template "
                       f"'{slide.template_name}' has no body region")


def _image_path(slide: Slide, base_dir: Path = Path(".")) -> Path | None:
    """Filesystem path of a slide's `![alt](path)` image, or None if it has none.

    Resolves a relative path against the slide's own source file (multi-file
    decks) and otherwise against `base_dir` — the single resolution rule shared
    by the content-hash byte-fold and the push-time upload, so both read the
    same file.
    """
    if not slide.image:
        return None
    p = Path(slide.image)
    if p.is_absolute():
        return p
    return (slide.src_path.parent if slide.src_path else base_dir) / p


def _equation_ink(slide: Slide) -> str:
    """Hex colour for a slide's rendered equations — paper on dark templates."""
    style = STYLES.get((slide.template_name or "").lower())
    if style is not None and style.bg == DARK_BG:
        return "#FAFAFA"
    return INK_HEX


def _resolve_equations(drive, slide: Slide) -> list[tuple[str, str, tuple]]:
    """Render + upload a slide's `$$...$$` blocks -> [(source, url, (w, h) px)].

    A render failure (construct outside the mathtext subset) is warned about in
    `render_equation` and that equation is skipped, not the whole push.
    """
    focal = (slide.template_name or "").lower() == "equation"
    pt = EQUATION_FOCUS_PT if focal else EQUATION_PT
    out = []
    for src in slide.equations:
        png = render_equation(src, color=_equation_ink(slide), pt=pt)
        if png is None:
            continue
        out.append((src, upload_image(drive, png), png_size(png)))
    return out


def _resolve_image(drive, slide: Slide, base_dir=Path(".")):
    if slide.layout != "image":
        return None, None
    if slide.mermaid:
        png = render_mermaid(slide.mermaid)
        if png is None:  # render failed — warn (in render_mermaid) and skip the graphic
            return None, None
        return upload_image(drive, png), png_size(png)
    p = _image_path(slide, base_dir)
    if p is None:
        return None, None
    if not p.exists():
        logger.warning(f"image not found, graphic skipped: {p}")
        return None, None
    return upload_image(drive, p), png_size(p)


# One batchUpdate per _BATCH_CHUNK requests. Google applies a call's requests
# in order, and sequential calls preserve that order, so the final deck state
# is identical to a single call — but a whole-deck force push in one call
# overflows the HTTP request itself (broken pipe at ~16k requests). The cost is
# atomicity: a failure between chunks leaves a partially-applied push, which a
# plain sync reconciles (same recovery as any failed push).
_BATCH_CHUNK = 500


def _created_ids(chunk) -> list[str]:
    """Explicit object ids a chunk brings into existence."""
    ids = []
    for req in chunk:
        for kind, body in req.items():
            if kind == "duplicateObject":
                ids.append(body["objectIds"][body["objectId"]])
            elif (kind.startswith("create") and "objectId" in body
                  and kind != "createParagraphBullets"):
                # createParagraphBullets is the one create* request whose
                # objectId is an existing element, not a new object.
                ids.append(body["objectId"])
    return ids


def _live_ids(slides_api, deck) -> set[str]:
    pres = slides_api.presentations().get(presentationId=deck).execute()
    ids = set()
    for page in pres.get("slides", []):
        ids.add(page["objectId"])
        for el in page.get("pageElements", []):
            ids.add(el["objectId"])
    return ids


def _chunk_already_applied(slides_api, deck, chunk) -> bool:
    """True when a rejected chunk's effects are already live — a replayed call.

    httplib2 silently re-sends a request whose response was lost mid-read; the
    replayed batchUpdate then rejects its own first create as a duplicate id
    (or its first delete as not-found). Each call is atomic, so the whole
    chunk either applied or it didn't: on a replay every created id is live
    and every net-deleted id is gone; on a genuine bad request the rest of the
    chunk's creates never happened.
    """
    created = _created_ids(chunk)
    deleted = [r["deleteObject"]["objectId"] for r in chunk if "deleteObject" in r]
    if not created and not deleted:
        return False  # text-only chunks replay harmlessly; an error is genuine
    live = _live_ids(slides_api, deck)
    # A same-id refresh deletes then re-creates an id within one chunk; the
    # re-create wins, so judge deletes only for ids the chunk doesn't recreate.
    gone = set(deleted) - set(created)
    return set(created) <= live and not (gone & live)


def _batch(slides_api, deck, requests):
    for start in range(0, len(requests), _BATCH_CHUNK):
        chunk = requests[start:start + _BATCH_CHUNK]
        try:
            slides_api.presentations().batchUpdate(
                presentationId=deck, body={"requests": chunk}).execute()
        except HttpError:
            if not _chunk_already_applied(slides_api, deck, chunk):
                raise
            logger.warning(f"batch chunk at request {start} had already applied "
                           "(lost response replayed by the transport); continuing")


def _apply_notes(slides_api, deck, creates):
    want = {s.object_id: (s.notes + "\n\n\n" + _marker(s)).strip()
            for s in creates}
    if not want:
        return
    pres = slides_api.presentations().get(presentationId=deck).execute()
    reqs = []
    for s in pres.get("slides", []):
        if s["objectId"] not in want:
            continue
        nid = _notes_shape_id(s)
        if not nid:
            continue
        if _read_notes(s):  # clear any notes inherited from a duplicated template
            reqs.append({"deleteText": {"objectId": nid,
                                        "textRange": {"type": "ALL"}}})
        reqs.append({"insertText": {"objectId": nid, "text": want[s["objectId"]]}})
    if reqs:
        _batch(slides_api, deck, reqs)


def _apply_skip(slides_api, deck, creates) -> None:
    """Mark `hidden:` slides as skipped (hidden in present mode) — the Slides
    analogue of Slidev's `hidden` frontmatter.

    Applied only to freshly created/replaced slides: `hidden` is part of the
    content hash, so toggling it re-creates the slide (defaulting to un-skipped)
    and re-runs this pass, which re-skips it iff it is still hidden. Best-effort,
    like `_hide_templates` — an older API surface may not expose `isSkipped`.
    """
    hidden = [s.object_id for s in creates if s.hidden]
    if not hidden:
        return
    try:
        _batch(slides_api, deck, [{"updateSlideProperties": {
            "objectId": oid, "slideProperties": {"isSkipped": True},
            "fields": "isSkipped"}} for oid in hidden])
    except Exception as e:  # noqa: BLE001 - API may not expose isSkipped
        logger.warning(f"could not mark slides hidden (skipped): {e}")


def _notes_shape_id(slide):
    notes_page = slide.get("slideProperties", {}).get("notesPage", {})
    nid = notes_page.get("notesProperties", {}).get("speakerNotesObjectId")
    if nid:
        return nid
    for el in notes_page.get("pageElements", []):
        if el.get("shape", {}).get("placeholder", {}).get("type") == "BODY":
            return el["objectId"]
    return None


def _reorder(slides_api, deck, source, anchor):
    pres = slides_api.presentations().get(presentationId=deck).execute()
    order = [s["objectId"] for s in pres.get("slides", [])]
    want = [s.object_id for s in source if s.object_id in order]
    if not want:
        return
    # Managed slides sit after any hand-built slides but BEFORE trailing
    # template (s2gtpl_) slides, which stay parked at the end.
    tpl_at = next((i for i, o in enumerate(order) if o.startswith("s2gtpl_")),
                  len(order))
    if anchor and anchor in order:
        base = order.index(anchor) + 1
    else:
        base = sum(1 for o in order[:tpl_at] if not MANAGED_RE.match(o))
    # updateSlidesPosition can't reorder slides relative to each other, so move
    # one at a time into position (sequential requests act like insertion sort).
    reqs = [{"updateSlidesPosition": {"slideObjectIds": [oid],
                                      "insertionIndex": base + i}}
            for i, oid in enumerate(want)]
    _batch(slides_api, deck, reqs)


# ---------------------------------------------------------------------------
# Image hosting
# ---------------------------------------------------------------------------


def png_size(path: Path):
    head = path.read_bytes()[:24]
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")
    return None


def upload_image(drive, path: Path) -> str:
    cache = json.loads(IMAGE_CACHE.read_text()) if IMAGE_CACHE.exists() else {}
    digest = hashlib.sha1(path.read_bytes()).hexdigest()
    if digest in cache:
        return cache[digest]
    media = MediaFileUpload(str(path), mimetype="image/png")
    f = drive.files().create(body={"name": path.name}, media_body=media,
                             fields="id").execute()
    drive.permissions().create(
        fileId=f["id"], body={"type": "anyone", "role": "reader"}).execute()
    url = f"https://drive.google.com/uc?export=download&id={f['id']}"
    cache[digest] = url
    IMAGE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    IMAGE_CACHE.write_text(json.dumps(cache, indent=2))
    return url


# ---------------------------------------------------------------------------
# Pull:  native objects -> Slide
# ---------------------------------------------------------------------------


def _oid_to_key(pres_slides: list[dict]) -> dict[str, str]:
    """objectId -> slide key, so an intra-deck page-link can pull back to `#key`.

    Mirrors the key every slidesync slide carries in its `<!-- s2g {...} -->`
    marker; foreign slides (no marker id) map to their objectId.
    """
    out = {}
    for s in pres_slides:
        oid = s["objectId"]
        out[oid] = _read_marker(_read_notes(s)).get("id") or oid
    return out


def pull_slides(slides_api, deck, managed_only=True) -> list[Slide]:
    pres = slides_api.presentations().get(presentationId=deck).execute()
    pres_slides = pres.get("slides", [])
    links = _oid_to_key(pres_slides)  # resolve native page-links back to `#key`
    out = []
    for s in pres_slides:
        if managed_only and not MANAGED_RE.match(s["objectId"]):
            continue
        slide = _slide_from_native(s, links)
        # A skipped slide round-trips as `hidden: true` — the live isSkipped is
        # authoritative, so a native skip toggle in Slides pulls back too. Set it
        # before _finalize so `hidden` lands in the canonical hash.
        slide.hidden = bool(s.get("slideProperties", {}).get("isSkipped"))
        out.append(_finalize(slide))
    return out


def _el_y(el) -> float:
    return el.get("transform", {}).get("translateY", 0.0)


def _el_x(el) -> float:
    return el.get("transform", {}).get("translateX", 0.0)


def _first_font_pt(shape) -> float:
    for el in shape.get("text", {}).get("textElements", []):
        sz = (el.get("textRun") or {}).get("style", {}).get("fontSize", {})
        if sz.get("magnitude"):
            return sz["magnitude"]
    return 0.0


def _slide_from_native(s, links: dict[str, str] | None = None) -> Slide:
    notes_raw = _read_notes(s)
    marker = _read_marker(notes_raw)
    notes = MARKER_RE.sub("", notes_raw).strip()
    if marker.get("template") == "custom":
        return _custom_slide_from_native(s, marker, notes)
    if marker.get("template"):
        return _slide_from_marker(marker, notes)

    # Decks slidesync didn't author have no TITLE placeholder and often many
    # independent text boxes, so we can't assume one title + one body: collect
    # every non-empty text box, the first image, and any table.
    title_el, text_shapes, image_el, table = None, [], None, None
    for el in s.get("pageElements", []):
        if "table" in el:
            table = _table_from_native(el["table"])
        elif "image" in el:
            if _EQ_IMG_RE.search(el.get("objectId", "")):
                continue  # a rendered $$-equation; its source round-trips via the marker
            image_el = image_el or el
        elif el.get("shape", {}).get("text"):
            paras = _paras_from_shape(el["shape"], links)
            if not paras:
                continue
            ph = el["shape"].get("placeholder", {}).get("type")
            if ph in ("TITLE", "CENTERED_TITLE") and title_el is None:
                title_el = (el, paras)
            else:
                text_shapes.append((el, paras))

    if title_el is None and text_shapes:  # no placeholder: the biggest-font box is the title
        title_el = max(text_shapes,
                       key=lambda t: (_first_font_pt(t[0]["shape"]), -_el_y(t[0])))
        text_shapes.remove(title_el)
    title = _flatten(title_el[1]).strip() if title_el else ""

    text_shapes.sort(key=lambda t: (_el_y(t[0]), _el_x(t[0])))  # reading order
    body: list[Para] = []
    for _, paras in text_shapes:
        if body:  # blank line between merged boxes
            body.append(Para("", [], -1))
        body.extend(paras)

    image, image_alt, image_link = marker.get("img"), "", None
    if image is None and image_el is not None:  # foreign image: keep its live URL + alt
        image = image_el["image"].get("contentUrl") or image_el["image"].get("sourceUrl")
        image_alt = image_el.get("description") or image_el.get("title") or ""
        image_link = (image_el["image"].get("imageProperties", {})
                      .get("link", {}).get("url"))

    layout = _infer_layout(body, image, table)
    key = marker.get("id") or _slug(title) or s["objectId"]
    slide = Slide(key, layout, title=title, paras=body, image=image,
                  image_alt=image_alt, image_link=image_link, table=table,
                  notes=notes)
    slide.equations = _marker_equations(marker)
    slide.layout_name = marker.get("tpl") or ("section" if layout == "section"
                                              else None)
    return slide


def _custom_slide_from_native(s, marker: dict, notes: str) -> Slide:
    """Capture a hand-drawn slide's live elements into a ```gslides``` block.

    The Google Slides copy is authoritative; this snapshot lets the slide be
    recreated if it is ever deleted. Geometry, text (with first-run style),
    shape fill/outline, images and lines are captured; richer styling is
    approximate (and irrelevant while the live slide exists, which is the norm).
    """
    reqs = _elements_to_requests(s.get("pageElements", []))
    slide = Slide(marker["id"], "custom", notes=notes)
    slide.template_name = "custom"
    slide.custom = json.dumps({"requests": reqs}, indent=2)
    return slide


# Writable subfields copied verbatim from the get-response back into update
# requests (the two schemas share these). Connections are intentionally dropped
# (they reference sibling element ids we renumber) — see _line_prop_requests.
# `shadow` and `autofit` carry read-only/computed subfields (fontScale,
# lineSpacingReduction) the API refuses in an update mask, so they are not
# captured — recreated slides inherit the defaults. The rest is writable and
# copied verbatim from the get-response.
_SHAPE_PROP_FIELDS = ("shapeBackgroundFill", "outline",
                      "contentAlignment", "link")
_LINE_PROP_FIELDS = ("lineFill", "weight", "dashStyle", "startArrow",
                     "endArrow", "link")
_IMAGE_PROP_FIELDS = ("cropProperties", "outline", "brightness",
                      "contrast", "transparency", "recolor", "link")
_TEXT_STYLE_FIELDS = ("bold", "italic", "underline", "strikethrough", "smallCaps",
                      "backgroundColor", "foregroundColor", "weightedFontFamily",
                      "fontFamily", "fontSize", "baselineOffset", "link")
_PARA_STYLE_FIELDS = ("alignment", "lineSpacing", "direction", "spacingMode",
                      "spaceAbove", "spaceBelow", "indentStart", "indentEnd",
                      "indentFirstLine")


def _present(obj: dict, fields: tuple[str, ...]) -> dict:
    return {k: obj[k] for k in fields if k in obj}


def _update(req: str, eid: str, prop_key: str, props: dict) -> list[dict]:
    if not props:
        return []
    return [{req: {"objectId": eid, prop_key: props, "fields": ",".join(props)}}]


def _elements_to_requests(elements: list[dict]) -> list[dict]:
    """Convert a slide's live page elements into faithful create+update requests.

    Captures geometry, the full writable property set (fills, outline, shadow,
    crop, line weight/arrows/dash), and per-run + per-paragraph text styling, so
    `pull -> push -> pull` is a fixed point. Element connections and unsupported
    element kinds (groups, video, etc.) are dropped with a warning.
    """
    reqs: list[dict] = []
    for i, el in enumerate(elements):
        eid = f"__PAGE___el{i}"
        props = {"pageObjectId": "__PAGE__"}
        if el.get("size"):
            props["size"] = el["size"]
        if el.get("transform"):
            # get can omit a scale component; create needs both.
            props["transform"] = {"scaleX": 1, "scaleY": 1, **el["transform"]}
        if "shape" in el:
            sh = el["shape"]
            reqs.append({"createShape": {"objectId": eid,
                "shapeType": sh.get("shapeType", "TEXT_BOX"),
                "elementProperties": props}})
            reqs += _text_requests(eid, sh.get("text", {}))
            reqs += _update("updateShapeProperties", eid, "shapeProperties",
                            _present(sh.get("shapeProperties", {}), _SHAPE_PROP_FIELDS))
        elif "image" in el:
            url = el["image"].get("contentUrl") or el["image"].get("sourceUrl")
            if not url:
                logger.warning(f"custom pull: image {eid} has no URL; skipped")
                continue
            reqs.append({"createImage": {"objectId": eid, "url": url,
                                         "elementProperties": props}})
            reqs += _update("updateImageProperties", eid, "imageProperties",
                            _present(el["image"].get("imageProperties", {}),
                                     _IMAGE_PROP_FIELDS))
        elif "line" in el:
            reqs.append({"createLine": {"objectId": eid,
                "category": el["line"].get("lineCategory", "STRAIGHT"),
                "elementProperties": props}})
            reqs += _line_prop_requests(eid, el["line"])
        else:
            logger.warning(f"custom pull: unsupported element {list(el)}; skipped")
    return reqs


def _line_prop_requests(eid: str, line: dict) -> list[dict]:
    return _update("updateLineProperties", eid, "lineProperties",
                   _present(line.get("lineProperties", {}), _LINE_PROP_FIELDS))


def _text_requests(eid: str, text: dict) -> list[dict]:
    """Reconstruct shape text exactly: content, per-paragraph and per-run styles."""
    els = text.get("textElements", [])
    content = "".join((te.get("textRun") or {}).get("content", "") for te in els)
    body = content.rstrip("\n")  # shapes carry an implicit final paragraph
    if not body:
        return []
    total = _u16(body)
    reqs = [{"insertText": {"objectId": eid, "text": body}}]

    def clamp(te):  # range intersected with the inserted text
        s, e = te.get("startIndex", 0), te.get("endIndex", te.get("startIndex", 0) + 1)
        return s, min(e, total)

    for te in els:  # paragraph styles + bullets
        pm = te.get("paragraphMarker")
        if not pm:
            continue
        s, e = clamp(te)
        if s >= e:
            continue
        rng = {"type": "FIXED_RANGE", "startIndex": s, "endIndex": e}
        ps = _present(pm.get("style", {}), _PARA_STYLE_FIELDS)
        if ps:
            reqs.append({"updateParagraphStyle": {"objectId": eid, "textRange": rng,
                                                  "style": ps, "fields": ",".join(ps)}})
        if pm.get("bullet"):
            reqs.append({"createParagraphBullets": {"objectId": eid, "textRange": rng,
                "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE"}})
    for te in els:  # run styles
        tr = te.get("textRun")
        if not tr:
            continue
        s, e = clamp(te)
        st = _present(tr.get("style", {}), _TEXT_STYLE_FIELDS)
        if s < e and st:
            reqs.append({"updateTextStyle": {"objectId": eid,
                "textRange": {"type": "FIXED_RANGE", "startIndex": s, "endIndex": e},
                "style": st, "fields": ",".join(st)}})
    return reqs


def _slide_from_marker(marker: dict, notes: str) -> Slide:
    """Reconstruct a tagged-template slide from its notes marker (source of truth)."""
    _headings, paras, *_ = parse_body(marker.get("body", ""))
    img = marker.get("img")
    slide = Slide(marker["id"], "image" if img else "content",
                  marker.get("h1", ""), paras, image=img, notes=notes)
    slide.equations = _marker_equations(marker)
    slide.image_alt = marker.get("alt", "")
    slide.kicker = marker.get("h2", "")
    slide.template_name = marker["template"]
    slide.vars = marker.get("vars", {})
    if "src" in marker:
        slide.src = base64.b64decode(marker["src"]).decode()
        # the authored source carries any ```gslides-overlay``` fence; surface it
        # on the field too so hashing / write-back see the same slide as authoring
        slide.overlay, _ = _extract_overlay(slide.src)
    return slide


def _infer_layout(paras, image, table):
    if image:
        return "image"
    if table:
        return "table"
    if not paras:
        return "section"
    return "content"


def _flatten(paras: list[Para]) -> str:
    return " ".join(p.text for p in paras)


def _paras_from_shape(shape, links: dict[str, str] | None = None) -> list[Para]:
    """Native shape text -> [Para]. `links` (objectId -> slide key) lets a native
    page-link round-trip back to an intra-deck `[text](#key)` link; without it
    page-links read as plain text (the foreign / link-free callers)."""
    elements = shape.get("text", {}).get("textElements", [])
    paras, cur, depth, base = [], None, -1, 0
    for el in elements:
        if "paragraphMarker" in el:
            if cur is not None:
                paras.append(_finish_para(cur, depth))
            bullet = el["paragraphMarker"].get("bullet")
            depth = bullet.get("nestingLevel", 0) if bullet is not None else -1
            cur, base = {"text": "", "runs": []}, 0
        elif "textRun" in el and cur is not None:
            base = _consume_run(el["textRun"], cur, base, links)
    if cur is not None:
        paras.append(_finish_para(cur, depth))
    while paras and not paras[0].text and not paras[0].runs:
        paras.pop(0)
    while paras and not paras[-1].text and not paras[-1].runs:
        paras.pop()
    return paras  # keep internal blank paragraphs for spacing


def _consume_run(tr, cur, base, links: dict[str, str] | None = None) -> int:
    content = tr.get("content", "").replace("\n", "")
    style = tr.get("style", {})
    name = _style_name(style, links)
    start = base
    cur["text"] += content
    if name:
        cur["runs"].append(Run(start, start + len(content), name,
                               _run_link(style, name, links)))
    return start + len(content)


def _run_link(style: dict, name: str, links: dict[str, str] | None) -> str | None:
    """Markdown link target for a styled run: a `url` link verbatim, or an
    intra-deck page-link mapped back to `#<key>` via `links` (objectId -> key)."""
    if name != "link":
        return None
    link = style.get("link", {})
    if link.get("url"):
        return link["url"]
    key = (links or {}).get(link.get("pageObjectId", ""))
    return f"#{key}" if key else None


def _style_name(style, links: dict[str, str] | None = None) -> str | None:
    link = style.get("link", {})
    if link.get("url"):
        return "link"
    # An intra-deck page-link reads as a link only when its target resolves to a
    # known slide key — otherwise it falls through to plain text (no broken ref).
    if link.get("pageObjectId") and (links or {}).get(link["pageObjectId"]):
        return "link"
    if "Mono" in style.get("fontFamily", ""):
        return "code"
    # After code (a foreign code span often carries a grey wash — keep it code),
    # before bold/italic (our own pushed highlights are never bold/italic).
    if style.get("backgroundColor", {}).get("opaqueColor"):
        return "highlight"
    if style.get("bold"):
        return "bold"
    if style.get("italic"):
        return "italic"
    return None


def _coalesce_runs(runs: list[Run]) -> list[Run]:
    """Merge adjacent same-style runs — Google often splits one styled span into
    several textRuns, which would otherwise render as `**a****b**`."""
    merged: list[Run] = []
    for r in runs:
        prev = merged[-1] if merged else None
        if prev and prev.style == r.style and prev.link == r.link and prev.end == r.start:
            merged[-1] = Run(prev.start, r.end, r.style, r.link)
        else:
            merged.append(r)
    return merged


def _finish_para(cur, depth) -> Para:
    text = cur["text"].lstrip("\t")
    shift = len(cur["text"]) - len(text)
    runs = [Run(r.start - shift, r.end - shift, r.style, r.link) for r in cur["runs"]]
    return Para(text, _coalesce_runs([r for r in runs if r.end > r.start]), depth)


def _table_from_native(table) -> list[list[str]]:
    rows = []
    for row in table.get("tableRows", []):
        cells = []
        for cell in row.get("tableCells", []):
            cells.append(_flatten(_paras_from_shape(cell)))
        rows.append(cells)
    return rows


def _read_notes(s) -> str:
    notes_page = s.get("slideProperties", {}).get("notesPage", {})
    nid = notes_page.get("notesProperties", {}).get("speakerNotesObjectId")
    for el in notes_page.get("pageElements", []):
        if el.get("objectId") == nid or \
                el.get("shape", {}).get("placeholder", {}).get("type") == "BODY":
            return _flatten(_paras_from_shape(el.get("shape", {})))
    return ""


def _read_marker(notes: str) -> dict:
    m = MARKER_RE.search(notes)
    return json.loads(m.group("json")) if m else {}


def _marker_equations(marker: dict) -> list[str]:
    """Verbatim `$$...$$` LaTeX sources stashed (base64) in the notes marker."""
    return [base64.b64decode(e).decode() for e in marker.get("eq", [])]


def write_slidev(slides: list[Slide], path: Path):
    fm = ["theme: seriph"]
    if path.exists():
        deck = frontmatter.loads(path.read_text()).metadata.get("deck")
        if deck:
            fm.append(f"deck: {deck}")
    body = "\n---\n".join(to_slidev(s) for s in slides)
    path.write_text("---\n" + "\n".join(fm) + "\n---\n\n" + body)


# ---------------------------------------------------------------------------
# Branded templates (match the Reliable Monitors deck)
# ---------------------------------------------------------------------------


def _emu(inches: float) -> int:
    return int(inches * EMU_PER_IN)


def _text_box(slide_id, box_id, box, text, size, rgb, bold, valign=None,
              halign="CENTER") -> list[dict]:
    x, y, w, h = box
    reqs = [
        {"createShape": {"objectId": box_id, "shapeType": "TEXT_BOX",
            "elementProperties": {"pageObjectId": slide_id,
                "size": {"width": {"magnitude": _emu(w), "unit": "EMU"},
                         "height": {"magnitude": _emu(h), "unit": "EMU"}},
                "transform": {"scaleX": 1, "scaleY": 1, "translateX": _emu(x),
                              "translateY": _emu(y), "unit": "EMU"}}}},
        {"insertText": {"objectId": box_id, "text": text}},
        {"updateTextStyle": {"objectId": box_id, "textRange": {"type": "ALL"},
            "style": {"fontFamily": BRAND_FONT, "bold": bold,
                      "fontSize": {"magnitude": size, "unit": "PT"},
                      "foregroundColor": {"opaqueColor": {"rgbColor": rgb}}},
            "fields": "fontFamily,bold,fontSize,foregroundColor"}},
        {"updateParagraphStyle": {"objectId": box_id, "textRange": {"type": "ALL"},
            "style": {"alignment": halign}, "fields": "alignment"}},
    ]
    if valign:
        reqs.append({"updateShapeProperties": {"objectId": box_id,
            "shapeProperties": {"contentAlignment": valign},
            "fields": "contentAlignment"}})
    return reqs


def _bg(slide_id, rgb) -> dict:
    return {"updatePageProperties": {"objectId": slide_id,
        "pageProperties": {"pageBackgroundFill": {"solidFill": {
            "color": {"rgbColor": rgb}}}},
        "fields": "pageBackgroundFill.solidFill.color"}}


def _branded_template(name, bg, headline_rgb, body_rgb, hsize, with_body) -> list[dict]:
    """Centered kicker + headline (+ body) template, matching the deck's style."""
    sid = f"s2gtpl_{name}"
    reqs = [{"createSlide": {"objectId": sid,
                             "slideLayoutReference": {"predefinedLayout": "BLANK"}}},
            _bg(sid, bg)]
    reqs += _text_box(sid, sid + "_k", (0.34, 1.6, 9.32, 0.5),
                      "{{h2}}", 17, RED, False)
    reqs += _text_box(sid, sid + "_h", (0.34, 2.05, 9.32, 1.7),
                      "{{h1}}", hsize, headline_rgb, True, valign="MIDDLE")
    if with_body:
        reqs += _text_box(sid, sid + "_b", (0.34, 3.85, 9.32, 1.4),
                          "{{body}}", 24, body_rgb, False)
    return reqs


# (name, background, headline colour, body colour, headline pt, has-body)
TEMPLATE_SPECS = [
    ("label", LIGHT_BG, INK, BODY_INK, 50, True),
    ("dark", DARK_BG, PAPER, PAPER, 72, False),
]


def make_templates(slides_api, deck):
    reqs = []
    for spec in TEMPLATE_SPECS:
        reqs += _branded_template(*spec)
    _batch(slides_api, deck, reqs)
    names = [s[0] for s in TEMPLATE_SPECS]
    pres = slides_api.presentations().get(presentationId=deck).execute()
    tag = []
    for s in pres.get("slides", []):
        if s["objectId"] in {f"s2gtpl_{n}" for n in names}:
            nid = _notes_shape_id(s)
            if nid:
                tag.append({"insertText": {"objectId": nid,
                            "text": f"<!-- s2g:template {s['objectId'][7:]} -->"}})
    if tag:
        _batch(slides_api, deck, tag)
    _hide_templates(slides_api, deck, [f"s2gtpl_{n}" for n in names])
    return names


def _hide_templates(slides_api, deck, ids):
    """Skip template slides in the slideshow (best effort)."""
    pres = slides_api.presentations().get(
        presentationId=deck, fields="slides.objectId").execute()
    present = {s["objectId"] for s in pres.get("slides", [])} & set(ids)
    if not present:
        return
    try:
        _batch(slides_api, deck, [{"updateSlideProperties": {
            "objectId": i, "slideProperties": {"isSkipped": True},
            "fields": "isSkipped"}} for i in present])
    except Exception as e:  # noqa: BLE001 - API may not expose isSkipped
        logger.warning(f"could not mark templates skipped: {e}")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

SAMPLE = """---
theme: seriph
---

---
layout: section
id: intro
---

# Round-trip check

<!-- opening remarks for the section -->

---
id: findings
---

## Key findings

- First **bold** point
  - nested with `code`
  - nested with *italic*
- Second point with a [link](https://example.com)
- The ==headline effect== survives, mixed with **bold**

<!-- talk through each finding -->

---
id: data
---

## The numbers

| Metric | Value |
| --- | --- |
| AUROC | 0.93 |
| Gap | small |

---
id: maths
---

## Display maths

Monitor effectiveness factors:

$$
E = p_{mon} \\times r_{mon} \\times (1 - FPR) \\times r_{hum}
$$

---
template: equation
id: objective
---

## THE OBJECTIVE

# This headline is parsed but not rendered

$$
\\max_{\\pi} \\; E_{\\tau}[r(\\tau)]
$$

Maximise expected reward under the monitor budget.

---
template: label
id: ask
---

## QUESTION

# What should we prioritise?

- summarisation first
- collusion is characterise-only

<!-- the ask -->

---
template: dark
id: titlecard
---

## MEETING

# 2026/06/01
"""


def new_deck(slides_api, title: str) -> str:
    """Create a deck and remove the default blank slide Google inserts."""
    deck = slides_api.presentations().create(
        body={"title": title}).execute()["presentationId"]
    pres = slides_api.presentations().get(presentationId=deck).execute()
    reqs = [{"deleteObject": {"objectId": s["objectId"]}}
            for s in pres.get("slides", [])]
    if reqs:
        _batch(slides_api, deck, reqs)
    return deck


DECK_ID_RE = re.compile(r"/presentation/d/(?P<id>[A-Za-z0-9_-]+)")


def deck_from_source(path: Path) -> str | None:
    """Read the target deck id from the file's top-level `deck:` frontmatter."""
    val = frontmatter.loads(path.read_text()).metadata.get("deck")
    if not val:
        return None
    m = DECK_ID_RE.search(str(val))
    return m.group("id") if m else str(val)


def _source_paths(args) -> list[Path]:
    paths = args.source if isinstance(args.source, list) else [args.source]
    return [Path(p) for p in paths]


def _deck_of(args, paths: list[Path]) -> str | None:
    if args.deck:
        return args.deck
    for p in paths:
        if d := deck_from_source(p):
            return d
    return None


def _exit_on_slot_mismatches(source: list[Slide]) -> None:
    problems = validate_slots(source)
    if not problems:
        return
    for p in problems:
        logger.error(f"[slot] {p}")
    sys.exit(f"{len(problems)} template-slot mismatch(es) above — authored "
             "content the template would silently drop; nothing pushed")


def cmd_push(args):
    paths = _source_paths(args)
    source = load_deck(paths)
    logger.info(f"parsed {len(source)} slides from {len(paths)} file(s)")
    _exit_on_slot_mismatches(source)
    slides_api, drive = get_services(args.account)
    deck = _deck_of(args, paths)
    if args.new:
        deck = new_deck(slides_api, args.new)
        logger.info(f"created https://docs.google.com/presentation/d/{deck}/edit")
    if not deck:
        sys.exit("no target deck: pass --deck/--new or add `deck:` frontmatter")
    stats = push(slides_api, drive, deck, source, args.anchor, args.prune,
                 base_dir=paths[0].parent, force=args.force,
                 allow_rekey=args.allow_rekey)
    logger.success(f"{stats} -> https://docs.google.com/presentation/d/{deck}/edit")


def cmd_pull(args):
    slides_api, _ = get_services(args.account)
    slides = pull_slides(slides_api, args.deck, managed_only=not args.all)
    write_slidev(slides, args.out)
    logger.success(f"pulled {len(slides)} slides -> {args.out}")


def cmd_make_templates(args):
    slides_api, _ = get_services(args.account)
    names = make_templates(slides_api, args.deck)
    logger.success(f"created templates {names} in "
                   f"https://docs.google.com/presentation/d/{args.deck}/edit")


def cmd_layouts(args):
    slides_api, _ = get_services(args.account)
    pres = slides_api.presentations().get(presentationId=args.deck).execute()
    for lay in pres.get("layouts", []):
        name = lay.get("layoutProperties", {}).get("displayName", "?")
        phs = []
        for el in lay.get("pageElements", []):
            ph = el.get("shape", {}).get("placeholder")
            if ph:
                phs.append(f"{ph.get('type')}[{ph.get('index', 0)}]")
        logger.info(f"{name:<24} {', '.join(phs) or '(no placeholders)'}")


# ---------------------------------------------------------------------------
# Comments + sync (drift detection)
# ---------------------------------------------------------------------------


def shape_comments(raw: list[dict]) -> list[dict]:
    """Drive comment threads -> [{id, page, author, content, resolved, replies}].

    `page` is the anchored slide objectId (None for file-level comments) —
    note an objectId outlives its slide, so the anchor may point at a deleted
    page after a re-render. Resolve-action replies with no text are dropped.
    """
    out = []
    for c in raw:
        try:
            anchor = json.loads(c.get("anchor") or "{}")
        except json.JSONDecodeError:
            anchor = {}
        pages = anchor.get("pages") or []
        out.append({
            "id": c.get("id", ""),
            "page": pages[0] if pages else None,
            "author": (c.get("author") or {}).get("displayName", ""),
            "content": (c.get("content") or "").strip(),
            "resolved": bool(c.get("resolved")),
            "modified": c.get("modifiedTime", ""),
            "replies": [
                {"author": (r.get("author") or {}).get("displayName", ""),
                 "content": (r.get("content") or "").strip()}
                for r in c.get("replies", []) if (r.get("content") or "").strip()
            ],
        })
    return out


_COMMENT_FIELDS = ("nextPageToken,comments(id,content,author(displayName),"
                   "anchor,resolved,modifiedTime,replies(content,author(displayName)))")


def list_comments(drive, deck: str) -> list[dict]:
    raw, token = [], None
    while True:
        resp = drive.comments().list(fileId=deck, pageSize=100, pageToken=token,
                                     fields=_COMMENT_FIELDS).execute()
        raw += resp.get("comments", [])
        token = resp.get("nextPageToken")
        if not token:
            return raw


def cmd_comments(args):
    _, drive = get_services(args.account)
    print(json.dumps(shape_comments(list_comments(drive, args.deck)), indent=2))


def text_lines_md(src: str) -> list[str]:
    """Markdown slide body -> normalised visible text lines (comments excluded)."""
    fences = [m.group("text") for m in VERBATIM_RE.finditer(src)]
    headings, paras, _img, _alt, _mmd, table, _, _eq, _lnk = parse_body(
        VERBATIM_RE.sub("", src))
    lines = [headings[k] for k in sorted(headings)]
    lines += [p.text for p in paras]
    if table:
        lines += [" | ".join(row) for row in table]
    for f in fences:
        lines += f.splitlines()
    return _norm_lines(lines)


def text_lines_native(s: dict) -> list[str]:
    """Native slide JSON -> normalised visible text lines (notes excluded)."""
    boxes = []
    for el in s.get("pageElements", []):
        if el.get("shape", {}).get("text"):
            paras = _paras_from_shape(el["shape"])
            if paras:
                boxes.append((_el_y(el), _el_x(el), paras))
        elif el.get("table"):
            rows = _table_from_native(el["table"])
            boxes.append((_el_y(el), _el_x(el),
                          [Para(" | ".join(r)) for r in rows]))
    boxes.sort(key=lambda t: (t[0], t[1]))
    return _norm_lines([p.text for _, _, paras in boxes for p in paras])


def _norm_lines(lines: list[str]) -> list[str]:
    # Sorted: box reading-order vs markdown order differ legitimately (e.g. a
    # kicker renders above the headline but is written below it).
    return sorted(" ".join(line.split()) for line in lines if line.strip())


def classify_drift(base: list[str] | None, local: list[str],
                   live: list[str]) -> str:
    """Three-way status for one slide; base is the last-pushed source (marker)."""
    if base is None:
        return "clean" if local == live else "drift-no-base"
    if local == base and live == base:
        return "clean"
    if local != base and live == base:
        return "local-edit"
    if local == base and live != base:
        return "live-drift"
    return "converged" if local == live else "conflict"


def _diff(a: list[str], b: list[str], a_name: str, b_name: str) -> str:
    return "\n".join(difflib.unified_diff(a, b, a_name, b_name, lineterm=""))


def _content_lines(src: str | None, template: str | None) -> list[str] | None:
    """Markdown text lines as they would actually RENDER for this template.

    graph/full slides are text-free apart from the image, the optional
    link-only footer line, and any overlay — other markdown body text never
    reaches the deck, so comparing it against the live slide would report
    drift forever. The equation template drops its `# h1` (only the `##`
    kicker renders), so that heading is removed from the comparison for the
    same reason. A ```gslides-overlay``` block renders on ANY template, so its
    insertText lines count as visible (and its raw JSON does not).
    """
    if src is None:
        return None
    overlay, src = _extract_overlay(src)
    tpl = (template or "").lower()
    if tpl in ("graph", "full"):
        _h, paras, *_ = parse_body(VERBATIM_RE.sub("", src))
        links = [p.text for p in paras if p.text and _link_only_para(p)][:1]
        return _norm_lines(links + _overlay_texts(overlay))
    lines = text_lines_md(src)
    if tpl == "equation":
        headings, *_ = parse_body(VERBATIM_RE.sub("", src))
        if 1 in headings and 2 in headings:  # h1 parsed but never rendered
            h1 = " ".join(headings[1].split())
            if h1 in lines:
                lines.remove(h1)
    return _norm_lines(lines + _overlay_texts(overlay))


def _overlay_texts(overlay: str | None) -> list[str]:
    """Visible text an overlay block inserts — its insertText payloads' lines."""
    if overlay is None:
        return []
    try:
        payload = json.loads(overlay)
    except json.JSONDecodeError:
        return []  # push logs the parse error; drift just sees no overlay text
    body = payload.get("requests", payload) if isinstance(payload, dict) else payload
    if not isinstance(body, list):
        return []
    return [ln for req in body if isinstance(req, dict)
            for ln in req.get("insertText", {}).get("text", "").splitlines()]


def _styled_runs(paras: list[Para]) -> list[tuple]:
    """(text, run signature) per non-blank para — the styling layer that
    text-line drift comparison is blind to. A live highlight/bold applied in
    the Slides UI changes runs but not text, so `classify_drift` reads the
    slide as clean; comparing these signatures catches it."""
    return [(p.text, tuple((r.start, r.end, r.style, r.link) for r in p.runs))
            for p in paras if p.text]


def _wash_texts(paras: list[Para]) -> list[str]:
    """Highlighted run texts, tightened to the ==mark== dialect (non-space-
    adjacent delimiters)."""
    return [t for p in paras for r in p.runs if r.style == "highlight"
            if (t := p.text[r.start:r.end].strip())]


def _capture_washes(text: str, sl, restyled) -> str | None:
    """Wrap live-washed run texts as ==...== inside the slide's authored source.

    The conservative style capture for a CLEAN slide: only washes new to the
    live copy are written back, by exact-text substitution in the authored
    block — never a body re-render, which would normalise away authored
    formatting the read-back can't reproduce (comments, captions, fences).
    A wash is substituted only where its text occurs exactly once in the
    block, so an ambiguous match (e.g. the same words inside a note comment)
    can never wrap the wrong occurrence. Returns the updated file text, or
    None when nothing could be mapped.
    """
    have = set(_wash_texts(sl.paras))
    new = [t for t in dict.fromkeys(_wash_texts(restyled.paras)) if t not in have]
    if not new:
        return None
    if not sl.src or text.count(sl.src) != 1:
        logger.warning(f"[washed    ] {sl.key} — live highlight(s) {new!r} can't "
                       "be captured: the slide's source block isn't unique in "
                       "the file; add ==...== by hand")
        return None
    src_new = sl.src
    for t in new:
        if f"=={t}==" in src_new:
            continue
        if src_new.count(t) != 1:
            logger.warning(f"[washed    ] {sl.key} — live highlight on {t!r} has "
                           "no unique verbatim source text; add ==...== by hand "
                           "(the live wash won't survive a re-render)")
            continue
        src_new = src_new.replace(t, f"=={t}==", 1)
    return text.replace(sl.src, src_new, 1) if src_new != sl.src else None


def _live_state(s) -> tuple[list[str], str, str | None, dict]:
    """(text lines, normalised notes, base source, marker) of a live slide."""
    notes_raw = _read_notes(s)
    marker = _read_marker(notes_raw)
    base_src = base64.b64decode(marker["src"]).decode() if "src" in marker else None
    notes = " ".join(MARKER_RE.sub("", notes_raw).split())
    return text_lines_native(s), notes, base_src, marker


def _clobber_risks(pres, managed, source, pruned) -> list[str]:
    """Keys of slides whose live edits a push would silently discard.

    A replace (or prune) deletes the live slide, which loses information only
    when the live copy drifted from its merge base AND the local markdown does
    not already contain the live content (as it does right after `sync`).
    Legacy slides without a src marker can't be checked and are allowed.
    """
    live = {s["objectId"]: s for s in pres.get("slides", [])}
    by_kh = {s.key_hash: s for s in source}
    by_key = {s.key: s for s in source}
    out = []
    for kh, (oid, _ch) in managed.items():
        sl = by_kh.get(kh)
        replacing = sl is not None and sl.custom is None and sl.object_id != oid
        if not (replacing or oid in pruned) or live.get(oid) is None:
            continue
        live_lines, live_notes, base_src, marker = _live_state(live[oid])
        if sl is None:
            # Re-key: the hash no longer matches, but the notes marker carries
            # the human id — so a live edit that `sync` already captured into
            # the markdown is still recognised as carried, not clobbered.
            sl = by_key.get(marker.get("id", ""))
        if base_src is None:
            continue
        if (live_lines == _content_lines(base_src, marker.get("template"))
                and live_notes in _notes_variants(base_src)):
            continue  # deck untouched since last push
        if (sl is not None
                and live_lines == _content_lines(sl.src or "", sl.template_name)
                and live_notes in _notes_variants(sl.src or "")):
            continue  # local already carries the live edit
        out.append(sl.key if sl is not None else oid)
    return out


def _slide_from_live_boxes(s, marker: dict,
                           links: dict[str, str] | None = None) -> Slide:
    """Rebuild a template slide's content from its deterministically-named boxes
    (`_k` kicker, `_h` headline, `_by` byline, `_b` body), formatting runs
    included — so live text edits can be written back into the markdown. `links`
    (objectId -> key) preserves intra-deck `[text](#key)` links on write-back."""
    sid = s["objectId"]
    shapes = {el.get("objectId", ""): el["shape"]
              for el in s.get("pageElements", [])
              if el.get("shape", {}).get("text")}

    def paras(suffix: str) -> list[Para]:
        shape = shapes.get(sid + suffix)
        return _paras_from_shape(shape, links) if shape else []

    slide = Slide(marker.get("id", sid), "content")
    slide.template_name = marker.get("template")
    slide.vars = marker.get("vars", {})
    # live text edits never touch the equation images; keep the $$ blocks so a
    # live-drift write-back doesn't drop them from the markdown
    slide.equations = _marker_equations(marker)
    if (slide.template_name or "").lower() in ("prompt", "code"):
        slide.title = _flatten(paras("_k")).strip()
        slide.verbatim = "\n".join(p.text for p in paras("_b"))
        return slide
    headline = _flatten(paras("_h")).strip()
    kicker = _flatten(paras("_k")).strip()
    if headline:
        slide.title, slide.kicker = headline, kicker
    else:
        slide.title = kicker  # content template: the kicker IS the title
    slide.paras = paras("_b") or paras("_by")
    if marker.get("img"):
        slide.layout = "image"
        slide.image, slide.image_alt = marker["img"], marker.get("alt", "")
    return slide


def _render_body(slide: Slide) -> str:
    """Slide -> markdown body only (the slide's frontmatter stays as authored)."""
    bare = copy.copy(slide)
    bare.template_name, bare.layout_name, bare.vars = None, None, {}
    return to_slidev(bare, include_id=False).strip()


_SEP_RE = re.compile(r"(?m)^---[ \t]*$")


def _slide_chunks(text: str) -> list[tuple[dict, int, int]]:
    """`split_slides` with offsets — (frontmatter, body_start, body_end) per slide."""
    bounds, pos = [], 0
    for m in _SEP_RE.finditer(text):
        bounds.append((pos, m.start()))
        pos = m.end()
    bounds.append((pos, len(text)))
    out, i = [], 0
    while i < len(bounds):
        meta, (start, end) = {}, bounds[i]
        chunk = text[start:end]
        if i + 1 < len(bounds) and _is_yaml_block(chunk):
            meta = _parse_yaml(chunk)
            i += 1
            start, end = bounds[i]
            chunk = text[start:end]
        if chunk.strip():
            out.append((meta, start, end))
        i += 1
    return out


def _slide_span(text: str, key: str) -> tuple[int, int] | None:
    """(start, end) of the body of the slide whose id is `key` — explicit `id:`
    frontmatter, else the derived id (`_derive_key`) of an id-less slide."""
    m = re.search(rf"(?m)^id:\s*{re.escape(key)}\s*$", text)
    if m:
        closer = _SEP_RE.search(text, m.end())
        if not closer:
            return None
        nxt = _SEP_RE.search(text, closer.end())
        return closer.end(), nxt.start() if nxt else len(text)
    for meta, start, end in _slide_chunks(text):
        if not meta.get("id") and _derive_key(text[start:end]) == key:
            return start, end
    return None


def _replace_slide_body(text: str, key: str, body: str) -> str:
    span = _slide_span(text, key)
    if span is None:
        return text
    return text[:span[0]] + "\n" + body.strip("\n") + "\n\n" + text[span[1]:]


def _append_to_slide_body(text: str, key: str, block: str) -> str:
    span = _slide_span(text, key)
    if span is None:
        return text
    return text[:span[1]].rstrip("\n") + "\n" + block + "\n\n" + text[span[1]:]


class _SyncState(TypedDict):
    dirty: set[Path]   # source files mutated by captures/write-backs
    pushable: bool     # any local-vs-live difference that a push would apply


def cmd_sync(args):
    """Reconcile a markdown deck with its live Slides copy — applying what's safe.

    Pull side: unresolved comment threads are appended to their slide as
    `<!-- @Author: text -->` blocks (orphaned threads are re-anchored via the
    objectId's key-hash), and live-drift slides — edited in Slides, untouched
    locally — are written back into the markdown, reconstructed from their
    styled boxes. Conflict slides (both sides changed) are never touched: their
    diffs print for a human/LLM to resolve, and the push step is skipped.
    Push side: when no conflicts remain, the (updated) file is pushed — safe,
    because local now matches live wherever the deck had drifted. Exits 1 when
    conflicts remain.

    Re-keyed decks (an id-scheme change orphaning every hash-derived objectId)
    are still matched through the notes-marker human id, so the capture pass —
    including styling-only live edits the text-line drift check can't see —
    writes back BEFORE the re-key push, which itself stays refused at deck
    scale without --allow-rekey (see mass_rekey).
    """
    slides_api, drive = get_services(args.account)
    paths = _source_paths(args)
    source = load_deck(paths)
    _exit_on_slot_mismatches(source)
    deck = _deck_of(args, paths)
    if not deck:
        sys.exit("no target deck: pass --deck or add `deck:` frontmatter")
    pres = slides_api.presentations().get(presentationId=deck).execute()
    live = {s["objectId"].split("_")[1]: s for s in pres.get("slides", [])
            if MANAGED_RE.match(s["objectId"])}
    links = _oid_to_key(pres.get("slides", []))  # page-links -> `#key` on write-back
    by_page = {}
    for c in shape_comments(list_comments(drive, deck)):
        if not c["resolved"] and c["page"]:
            by_page.setdefault(c["page"], []).append(c)

    texts = {p: p.read_text() for p in paths}
    state: _SyncState = {"dirty": set(), "pushable": False}
    origin_by_kh: dict[str, tuple[Path, str]] = {
        sl.key_hash: (sl.src_path, sl.src_key)
        for sl in source if sl.src_path is not None}  # src_path always set by load_deck
    # Re-key fallback index: the notes marker carries each slide's human id
    # verbatim, so live copies stay matchable even when the objectId/keyHash
    # scheme changes and the hash-keyed `live` lookup goes dark (the mass
    # re-key case — see mass_rekey). Maps marker id -> live key_hash.
    marker_kh: dict[str, str] = {}
    for kh, s in live.items():
        mid = _read_marker(_read_notes(s)).get("id")
        if mid:
            marker_kh.setdefault(mid, kh)

    def capture(origin: tuple[Path, str] | None, c: dict, page: str) -> None:
        lines = [f"@{c['author']}: {c['content']}"]
        lines += [f"@{r['author']}: {r['content']}" for r in c["replies"]]
        block = "<!-- " + "\n".join(lines) + " -->"
        if origin is not None:
            path, key = origin
            if " ".join(c["content"].split()) in " ".join(texts[path].split()):
                return  # already captured
            new = _append_to_slide_body(texts[path], key, block)
            if new != texts[path]:
                texts[path] = new
                state["dirty"].add(path)
                state["pushable"] = True
                logger.info(f"[comment   ] {key} — captured thread by {c['author']} "
                            f"-> {path.name}")
                return
        key = origin[1] if origin is not None else None
        logger.warning(f"[comment   ] thread by {c['author']} on {key or page} "
                       f"couldn't be placed; paste manually:\n{block}")

    conflicts = []
    for sl in source:
        assert sl.src_path is not None  # load_deck sets src_path on every slide
        src_path = sl.src_path
        origin = (src_path, sl.src_key)
        s = live.pop(sl.key_hash, None)
        rekeyed = False
        if s is None and (old_kh := marker_kh.get(sl.key)) in live:
            # Old-scheme live copy: same human id, different key_hash. Matching
            # it keeps the capture/drift machinery working across a re-key, so
            # live edits are written back BEFORE any re-key push recreates the
            # slide (the push itself still needs --allow-rekey at deck scale).
            s = live.pop(old_kh)
            rekeyed = True
            state["pushable"] = True  # the id must migrate even if content matches
            logger.info(f"[re-keyed  ] {sl.key} — matched live copy "
                        f"{s['objectId']} via its notes-marker id")
        if s is None:
            logger.info(f"[missing   ] {sl.key} — push will create it")
            state["pushable"] = True
            continue
        for c in by_page.pop(s["objectId"], []):
            capture(origin, c, s["objectId"])
        if sl.custom is not None:
            # Pull-authoritative: refresh the ```gslides``` block from the live
            # drawing so hand-drawn edits land back in the source file.
            marker = _read_marker(_read_notes(s))
            live_json = (_custom_slide_from_native(s, marker, "").custom
                         if "id" in marker else None)
            if live_json and live_json.strip() != (sl.custom or "").strip():
                new = texts[src_path].replace(sl.custom, live_json, 1)
                if new != texts[src_path]:
                    texts[src_path] = new
                    state["dirty"].add(src_path)
                    logger.info(f"[drawing   ] {sl.key} — captured live drawing "
                                f"-> {src_path.name}")
            continue
        live_lines, live_notes, base_src, marker = _live_state(s)
        base = _content_lines(base_src, marker.get("template"))
        local = _content_lines(sl.src or "", sl.template_name) or []
        status = classify_drift(base, local, live_lines)
        if status in ("clean", "converged"):
            restyled = (_slide_from_live_boxes(s, marker, links)
                        if marker.get("template") else None)
            if restyled and _styled_runs(restyled.paras) != _styled_runs(sl.paras):
                if rekeyed:
                    # About to be recreated: write the full live styling back
                    # first, or the re-key push would rebuild the slide bare.
                    restyled.overlay = sl.overlay  # keep the authored block
                    restyled.notes = " ".join(
                        MARKER_RE.sub("", _read_notes(s)).split())
                    new = _replace_slide_body(texts[src_path], sl.src_key,
                                              _render_body(restyled))
                    if new != texts[src_path]:
                        texts[src_path] = new
                        state["dirty"].add(src_path)
                        logger.info(f"[restyled  ] {sl.key} — live styling "
                                    f"captured -> {src_path.name}")
                    continue
                # Style-only drift on a clean slide: capture ONLY new washes
                # (ANY background colour reads as ==highlight==, not just our
                # amber — presenters pick theme colours) by wrapping the washed
                # text in the authored source. A full body re-render here would
                # normalise away authored formatting (comments, captions,
                # fences) — the 32-slide churn of 2026-07-07.
                new = _capture_washes(texts[src_path], sl, restyled)
                if new is not None and new != texts[src_path]:
                    texts[src_path] = new
                    state["dirty"].add(src_path)
                    state["pushable"] = True  # re-render in canonical amber now
                    logger.info(f"[washed    ] {sl.key} — live highlight "
                                f"captured -> {src_path.name}")
                    continue
                # other style-only drift (live bold/italic) stays uncaptured
            if s["objectId"] != sl.object_id:
                # Rendered text matches, but the content hash moved — a
                # comment/notes-level local change that still needs a push.
                logger.info(f"[notes-edit] {sl.key} — push will update it")
                state["pushable"] = True
            continue
        if status == "local-edit":
            logger.info(f"[local-edit] {sl.key} — push will update it")
            state["pushable"] = True
            continue
        rebuilt = (_slide_from_live_boxes(s, marker, links)
                   if status in ("live-drift", "drift-no-base")
                   and marker.get("template") else None)
        if rebuilt and (rebuilt.title or rebuilt.paras or rebuilt.verbatim):
            rebuilt.overlay = sl.overlay  # a body re-render must not drop the block
            rebuilt.notes = " ".join(MARKER_RE.sub("", _read_notes(s)).split())
            new = _replace_slide_body(texts[src_path], sl.src_key,
                                      _render_body(rebuilt))
            if new != texts[src_path]:
                texts[src_path] = new
                state["dirty"].add(src_path)
                state["pushable"] = True
                logger.info(f"[pulled    ] {sl.key} — live edit written back "
                            f"-> {src_path.name}")
                continue
        conflicts.append(sl.key)
        logger.error(f"[conflict  ] {sl.key} (pushed {marker.get('at', '?')}) — "
                     f"resolve in the markdown, then push:\n"
                     + _diff(base or [], local, "last-pushed", "local") + "\n"
                     + _diff(base or [], live_lines, "last-pushed", "live"))
    for page, cs in by_page.items():  # threads on re-rendered (deleted) pages
        m = re.match(r"s2g_(?P<kh>[0-9a-f]{10})_", page)
        origin = origin_by_kh.get(m.group("kh")) if m else None
        for c in cs:
            capture(origin, c, page)
    for _kh, s in live.items():
        logger.warning(f"[unmanaged ] {s['objectId']} has no local slide "
                       "(push --prune would delete it)")

    for p in sorted(state["dirty"]):
        p.write_text(texts[p])
        logger.success(f"updated {p}")
    if conflicts:
        sys.exit(f"sync stopped: {len(conflicts)} conflict(s) above — resolve in "
                 "the markdown, then `slidesync push` (its guard protects the rest).")
    if state["pushable"]:
        stats = push(slides_api, drive, deck, load_deck(paths), anchor=None,
                     prune=args.prune, base_dir=paths[0].parent,
                     allow_rekey=args.allow_rekey)
        logger.success(f"{stats} -> "
                       f"https://docs.google.com/presentation/d/{deck}/edit")
    else:
        logger.success("deck matches source — nothing to do")


def _loop_hop(slides_api, drive, title, slides):
    """One md->slides hop: build a deck, push, pull back."""
    deck = new_deck(slides_api, title)
    push(slides_api, drive, deck, slides, anchor=None, prune=False)
    return deck, pull_slides(slides_api, deck)


def cmd_roundtrip(args):
    slides_api, drive = get_services(args.account)
    src = build_slides(split_slides(SAMPLE))
    # md -> slides -> md -> slides
    deck_a, got_a = _loop_hop(slides_api, drive, "slidesync roundtrip A", src)
    deck_b, got_b = _loop_hop(slides_api, drive, "slidesync roundtrip B", got_a)
    logger.info(f"hop A https://docs.google.com/presentation/d/{deck_a}/edit")
    logger.info(f"hop B https://docs.google.com/presentation/d/{deck_b}/edit")
    ok = _compare(src, got_a) and _compare(got_a, got_b)
    if not args.keep:
        drive.files().delete(fileId=deck_a).execute()
        drive.files().delete(fileId=deck_b).execute()
        logger.info("scratch decks deleted")
    logger.log("SUCCESS" if ok else "ERROR",
               "loop stable" if ok else "loop DIVERGED")
    sys.exit(0 if ok else 1)


def _compare(src: list[Slide], got: list[Slide]) -> bool:
    if len(src) != len(got):
        logger.error(f"slide count {len(src)} != {len(got)}")
        return False
    ok = True
    for a, b in zip(src, got):
        if a.semantic() == b.semantic():
            logger.success(f"  [match] {a.key}")
            continue
        ok = False
        logger.error(f"  [DIFF]  {a.key}")
        for fa, fb, name in zip(a.semantic(), b.semantic(),
                                ["key", "layout_name", "template_name", "vars",
                                 "layout", "title", "kicker", "paras", "table",
                                 "image", "image_alt", "notes", "hidden",
                                 "equations"]):
            if fa != fb:
                logger.error(f"    {name}: {fa!r} != {fb!r}")
    logger.log("SUCCESS" if ok else "ERROR",
               "round-trip PASS" if ok else "round-trip FAIL")
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--account", default=DEFAULT_ACCOUNT)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("push")
    p.add_argument("source", type=Path, nargs="+",
                   help="one or more .slidev.md files; several files merge into "
                        "one deck with ids namespaced by file stem")
    p.add_argument("--deck")
    p.add_argument("--new")
    p.add_argument("--anchor")
    p.add_argument("--prune", action="store_true")
    p.add_argument("--force", action="store_true",
                   help="re-render all slides, ignoring the skip optimisation")
    p.add_argument("--allow-rekey", action="store_true",
                   help="permit a push that recreates a deck-scale number of "
                        "slides under new ids (normally refused, even with "
                        "--force: live edits/styling on the old copies would "
                        "be lost — capture with pull/sync first)")
    p.set_defaults(func=cmd_push)

    p = sub.add_parser("pull")
    p.add_argument("deck")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--all", action="store_true", help="export non-managed slides too")
    p.set_defaults(func=cmd_pull)

    p = sub.add_parser("roundtrip")
    p.add_argument("--keep", action="store_true", help="keep the scratch deck")
    p.set_defaults(func=cmd_roundtrip)

    p = sub.add_parser("layouts", help="list a deck's master layouts + placeholders")
    p.add_argument("deck")
    p.set_defaults(func=cmd_layouts)

    p = sub.add_parser("comments",
                       help="list comment threads as JSON (page anchor, author, replies)")
    p.add_argument("deck")
    p.set_defaults(func=cmd_comments)

    p = sub.add_parser("sync",
                       help="reconcile with the live deck (comments, live edits, conflicts)")
    p.add_argument("source", type=Path, nargs="+",
                   help="one or more .slidev.md files (ids namespaced by stem when several)")
    p.add_argument("--deck")
    p.add_argument("--prune", action="store_true",
                   help="the final push deletes managed slides missing from the sources")
    p.add_argument("--allow-rekey", action="store_true",
                   help="permit the push half to recreate a deck-scale number "
                        "of slides under new ids (sync's capture pass has "
                        "already written live edits/styling back by then)")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("make-templates",
                       help="add branded tagged template slides to a deck")
    p.add_argument("deck")
    p.set_defaults(func=cmd_make_templates)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
