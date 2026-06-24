# slidesync

Bidirectional sync between a [Slidev](https://sli.dev) markdown deck and **Google
Slides** — as native, editable objects (title/body/bullets/tables/positioned
images, brand-styled text boxes), not pasted screenshots.

Version: 0.8.1

```bash
uvx slidesync --help            # run without installing
pip install slidesync           # or install the CLI + library
```

## Why

Exporting a deck to images gives you something you can't edit; pasting markdown
by hand gives you something you can't version. `slidesync` keeps a `.slidev.md`
file as the source of truth and renders it into **real** Slides objects, so the
result stays fully editable in Google Slides — and `pull` reconstructs the
markdown back from those objects, so the loop is reversible.

- **`push`** — markdown → Slides (idempotent upsert, never a blind append).
- **`pull`** — Slides → markdown (handles multi-text-box and externally-authored
  decks, bullet nesting, tables, images, and speaker notes).
- **`roundtrip`** — push a sample to a scratch deck, pull it back, assert the two
  are semantically identical, delete the scratch deck.

## Auth (no setup)

Auth is **borrowed from the [`gog`](https://github.com/) CLI** — no separate
OAuth client. `slidesync` reads the client id/secret from
`~/Library/Application Support/gogcli/credentials.json` and the refresh token via
`gog auth tokens export`, then mints a short-lived access token. The stored token
already carries the `slides` + `drive` scopes; the Slides API must be enabled on
the gog Cloud project. Override the account with `--account` or
`$SLIDESYNC_ACCOUNT`. (Currently macOS-only — it reads gog's macOS Application
Support path.)

## Commands

| Command | Purpose |
|---------|---------|
| `slidesync push <file.slidev.md>... [--deck ID] [--new "Title"] [--anchor SLIDE] [--prune] [--force]` | markdown → Slides (rejected if it would discard live edits; `--force` overrides) |
| `slidesync pull <deckId> --out <file.md> [--all]` | Slides → markdown (`--all` includes non-managed slides) |
| `slidesync roundtrip [--keep]` | self-test: push a sample, pull, assert identical |
| `slidesync layouts <deckId>` | list a deck's theme layouts + placeholders |
| `slidesync make-templates <deckId>` | inject branded `{{token}}` template slides |
| `slidesync comments <deckId>` | list comment threads as JSON (page anchor, author, content, replies) |
| `slidesync sync <file.slidev.md>... [--deck ID] [--prune]` | reconcile with the live deck: pull comments + live edits into the markdown, push local changes; conflicts stop it (exit 1) |

`push` resolves the target deck from (in order) `--deck`, `--new`, or a top-level
`deck:` frontmatter key. Relative image paths resolve against each slide's own
source file.

**Multi-file decks**: `push`/`sync` accept several files (e.g.
`slidesync sync $(ls -r meetings/*.slidev.md)` — one file per meeting, newest
first). Deck order follows the argument order; slide ids namespace as
`<file-stem>-<id>` (`2026-06-15-overview`) so files can reuse ids; intra-file
`[text](#id)` links rewrite to the namespaced target, while fully-qualified
cross-file targets pass through. `sync` routes comment capture and live-edit
write-backs into the right source file under its local id.

```bash
slidesync push deck.slidev.md            # targets `deck:` frontmatter
slidesync push deck.slidev.md --new "Talk"
slidesync pull <id> --out deck.slidev.md
slidesync roundtrip
```

## Idempotent sync (upsert)

Each managed slide is created with `objectId = s2g_<keyHash>_<contentHash>`.
`keyHash` = per-slide `id:` frontmatter, else title slug, else index (survives
edits/reorders); `contentHash` is over a canonical render, so push → pull → push
is a no-op. Diff per run: identical hash → skip; same key, new content → replace;
new key → create. Removed slides are **kept** unless `--prune`. **Only `s2g_`
slides are ever touched** — hand-authored slides are invisible to the sync. A
hidden `<!-- s2g {...} -->` marker in speaker notes carries the human id, image
path, template vars — and, for template slides, the authored body markdown
(base64) — so `pull` recovers the source verbatim.

## Sync & drift

`push` is guarded like a non-fast-forward git push: if a slide it would replace
(or prune) was edited in Google Slides since the last push — and the local
markdown doesn't already carry that edit — the push is **rejected** with no
changes made (`--force` overwrites). Live edits on slides the push wouldn't
touch are left alone.

`sync` reconciles the two sides, applying whatever is safe. The marker's
last-pushed source is a true per-slide **merge base**, so each slide classifies
three-way without timestamps (the APIs expose no per-slide edit times — only
file-level `modifiedTime`; the marker's `at` stamp records our last push):

| status | meaning | sync does |
|--------|---------|-----------|
| `clean` / `converged` | nothing changed, or both sides made the same change | nothing |
| `local-edit` | markdown changed, deck untouched | pushes it |
| `live-drift` | slide edited in Google Slides | writes the live content back into the markdown (reconstructed from its styled boxes, formatting runs included), then pushes |
| `conflict` | both changed since last push | prints both diffs vs the base for a human/LLM to resolve; skips the push; exits 1 |

Unresolved comment threads are appended to their slide as
`<!-- @Author: text -->` blocks (replies as extra `@Author:` lines). These
mirrors are **comments, not presenter notes**: they stay out of the
speaker-notes pane, and when a re-render orphans the live thread, push
re-creates it anchored to the slide's new objectId (replies preserved; the
re-created thread is authored by the authenticated account). Resolving a
thread in Slides retires it — sync stops capturing and push won't revive it.
Write-back caveat: a slide edited live is rewritten canonically, so its
authored comments collapse into one trailing block (untouched slides keep
comments in place).

## Markdown dialect

Top-level frontmatter: `theme:`, `deck:`. Slides separated by `---`; each slide
may have its own frontmatter (`id:`, `template:`, `layout:`).

- `# h1` = headline, `## h2` above an `# h1` = kicker; a lone `##` is the title.
- Bullets `-`/`*`; ordered `1.` (nest with 2-space indent). Inline
  `**bold**` / `*italic*` / `` `code` `` / `[link](url)`. GFM tables.
  `![alt](path)` images (uploaded to Drive; `alt` becomes the accessibility
  description, round-tripped on pull). Blank lines preserved as spacing.
  `<!-- notes -->` become speaker notes — and round-trip as **comments, in
  place**: template slides carry their authored source in the marker, so `pull`
  re-emits each comment where it was written instead of one merged trailing
  blob. Speaker notes edited live in Slides come back as one extra trailing
  comment.
- **Internal links:** `[text](#slide-id)` becomes a native Slides link to the
  slide whose `id:` (or title slug) is `slide-id`, and round-trips: `pull` reads
  the native page-link back to `[text](#slide-id)` (so it no longer churns).
- **Mermaid diagrams:** a fenced ```` ```mermaid ```` block is rendered to a PNG
  and embedded as an image (Slides has no native Mermaid renderer). Renders are
  cached by diagram hash, so an unchanged diagram is never re-rendered or
  re-uploaded; a render failure logs a warning and skips the graphic rather than
  aborting the push. Backend: `mmdc` (mermaid-cli) if it's on `PATH` (offline),
  else the [kroki.io](https://kroki.io) HTTP API (no extra dependency). The
  diagram source lives in the markdown, so it's the source of truth — `pull`
  recovers the rendered image, not the Mermaid source.

### Built-in brand kit (IBM Plex; red `#C0392B` kicker)

Select per slide via `template:` — native styled boxes, no in-deck templates:
`dark`/`title`, `appendix`, `question`/`label`, `topic`, `content`,
`graph`/`full` (single full-bleed image), `prompt`/`code` (verbatim monospace).
Title cards (`dark`/`title`/`appendix`) render body lines as a small dimmed
**byline** beneath the headline (e.g. `Project · Presenter`) — they still have
no linkable body region.
Slides with no `template:` fall back to a generative path (section /
title+body / table / image) that also brands the background + IBM Plex.

### Custom slides (diagrams) — pull-authoritative

Give a slide a fenced ```` ```gslides ```` block holding literal Slides API
requests (use `__PAGE__` for the slide page id). Sync is **pull-authoritative /
push-if-missing**: the Slides copy is the source of truth — `push` only creates
the slide when missing, `pull` captures the live drawing back into the block.

## Development

```bash
uv sync
uv run pytest -q          # offline tests (no network/auth)
```

Releases publish to PyPI via Trusted Publishing (OIDC) on a `v*.*.*` tag — see
`.github/workflows/release.yml`. Bump with `uvx bumpver update --patch`.

## Caveats

- Slidev-only constructs (`<v-clicks>`, `<div grid>`, CSS) are flattened/stripped
  — this is a content mapper, not a CSS renderer.
- On `pull`, the slide model holds a single image, so a slide with multiple
  images keeps the first; image `contentUrl`s from foreign decks are ephemeral.
- Verbatim-source markers are seeded at push time, so comment preservation
  applies from the first push with v0.2+ (older slides re-render once: the
  content hash is now over the authored source). Generative-path slides (no
  `template:`) still merge comments into a single trailing comment on pull,
  since their live Slides edits — not the marker — are the source of truth.

## License

MIT © Daniel Hails
