# Changelog

## 0.11.0

### `gslides-overlay` — raw requests on top of a templated slide

A ```` ```gslides-overlay ```` fenced block on a normal templated/generative
slide replays its literal Slides API requests **after** the slide's own render
on every push (`__PAGE__` substituted by the slide page id) — for annotation
text boxes, arrows, and callouts no template expresses. Markdown is the source
of truth: the block folds into the content hash, round-trips through the notes
marker on `pull`, and a content-changing push recreates the drawn elements
(native edits to them are not written back). Drift detection counts the
overlay's `insertText` lines as visible text — never its raw JSON — so an
overlaid slide reads as clean, and a live-drift write-back preserves the
authored block. On a ```` ```gslides ```` custom slide the overlay is ignored
with a warning (the slide already carries raw requests).

## 0.10.4

### Mass re-key guard

A push (or the push half of `sync`) that would recreate a deck-scale number of
slides under new objectIds — while a matching volume of live `s2g_` slides
matches no local slide — is now **refused**, even with `--force`. Trigger:
both counts reach `max(10, 30% of managed slides)`. Escape hatch:
`--allow-rekey` on `push` and `sync`.

Motivation: on the 0.10.2 upgrade a routine sync saw all 391 managed slides of
a live deck as "missing", recreated them from markdown, and wiped live text
highlights applied minutes earlier. Whatever perturbs the ids — an
objectId/keyHash scheme change or a key-computation bug — the plan it produces
has a recognisable shape (mass creates + mass orphaned live slides), and that
shape is now a hard stop instead of a silent delete-and-recreate.

`sync` additionally matches re-keyed live slides through the scheme-independent
human id carried in each slide's `<!-- s2g {...} -->` notes marker, so its
capture pass still protects them: live text edits are written back as before,
and styling-only edits (highlight washes, bolding — invisible to text-line
drift comparison) are now captured into the markdown as `==highlight==` runs
*before* any re-key push recreates the slides. A refused sync has therefore
already captured; re-running with `--allow-rekey` completes the migration.

### Id-scheme stability (note for maintainers)

The `s2g_<keyHash>_<contentHash>` objectId format and every input to `keyHash`
(digest, hash length, key derivation, multi-file namespacing) are a
compatibility contract with every existing deck. Changing any of them re-keys
every live slide, and a version bump would orphan them all. Any scheme change
MUST ship a read-side migration path — match old-scheme ids when reading the
deck (the notes-marker id is the scheme-independent handle `sync` now uses) —
plus a CHANGELOG entry.

On the specific 0.10.1→0.10.2 pair: the released diff contains **no id-scheme
change** (it only chunked `batchUpdate` requests); `keyHash`/`contentHash`
derivation is byte-identical between the two versions, so there are no "old
vs new keyhash forms" to dual-match for that pair and no hash-form
backward-match is shipped. The marker-id fallback above is the general
backward-match: it survives *any* hash-scheme change, since the human id in
the speaker-notes marker never depends on the hash.

## 0.10.3

- Tolerate transport-replayed batch chunks (duplicate-id abort).

## 0.10.2

- Chunk `batchUpdate` requests (500 per call) so a whole-deck force push no
  longer overflows the HTTP request (broken pipe at ~16k requests).

## 0.10.1

- Render `$$...$$` equations in Computer Modern (matplotlib mathtext `cm`).

## 0.10.0

- `==highlight==` inline mark (amber wash, ink text) and `template: equation`.
