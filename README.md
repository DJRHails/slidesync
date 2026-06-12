# slidesync

Bidirectional sync between a [Slidev](https://sli.dev) markdown deck and **Google
Slides** — as native, editable objects (title/body/bullets/tables/positioned
images, brand-styled text boxes), not pasted screenshots.

Version: 0.2.0

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
| `slidesync push <file.slidev.md> [--deck ID] [--new "Title"] [--anchor SLIDE] [--prune] [--force]` | markdown → Slides |
| `slidesync pull <deckId> --out <file.md> [--all]` | Slides → markdown (`--all` includes non-managed slides) |
| `slidesync roundtrip [--keep]` | self-test: push a sample, pull, assert identical |
| `slidesync layouts <deckId>` | list a deck's theme layouts + placeholders |
| `slidesync make-templates <deckId>` | inject branded `{{token}}` template slides |

`push` resolves the target deck from (in order) `--deck`, `--new`, or a top-level
`deck:` frontmatter key. Relative image paths resolve against the markdown file's
directory.

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
  slide whose `id:` (or title slug) is `slide-id`.

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
