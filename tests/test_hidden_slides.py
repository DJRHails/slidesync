"""Hidden slides: `hidden:`/`hide:` frontmatter <-> a slide skipped in Slides.

A slide marked `hidden: true` (Slidev's convention; `hide:` is accepted too) is
still pushed as a native, editable slide, but is marked **skipped** in the
presentation (hidden in present mode) via the Slides API's
`updateSlideProperties { isSkipped }`. It round-trips: `pull` reads the live
`isSkipped` back to `hidden: true`, and the flag is part of the content hash so
toggling it re-pushes.

These are offline: the markdown parse/render tests need no API; the push/pull
tests drive the in-memory Slides+Drive fake from `test_e2e_scenarios`.
"""

import pytest

import test_e2e_scenarios as e2e
from test_atomic_swap import RecordingSlides

from slidesync import _sync
from slidesync._sync import build_slides, load_slides, pull_slides, push, split_slides, to_slidev

DECK = "fakedeck"

HIDDEN_MD = """---
theme: seriph
---

---
template: topic
id: shown
---
# Visible
## SECTION

A point everyone sees.

---
template: topic
id: backup
hidden: true
---
# Backup detail
## APPENDIX

Only if asked.
"""


def _slide(md, key):
    return next(s for s in build_slides(split_slides(md)) if s.key == key)


# --- markdown parse / render (no API) ---------------------------------------

def test_hidden_frontmatter_parses_to_flag():
    assert _slide(HIDDEN_MD, "backup").hidden is True
    assert _slide(HIDDEN_MD, "shown").hidden is False


def test_hide_alias_parses():
    md = HIDDEN_MD.replace("hidden: true", "hide: yes")
    assert _slide(md, "backup").hidden is True


def test_hidden_round_trips_through_render():
    backup = _slide(HIDDEN_MD, "backup")
    out = to_slidev(backup)
    assert "hidden: true" in out
    # re-parsing the rendered markdown preserves the flag
    reparsed = next(s for s in build_slides(split_slides(
        "---\ntheme: seriph\n---\n\n---\n" + out)) if s.key == "backup")
    assert reparsed.hidden is True


def test_hidden_moves_the_content_hash():
    # Toggling `hidden` must change object_id so a push re-creates + re-skips.
    shown = _slide(HIDDEN_MD, "backup")
    visible = _slide(HIDDEN_MD.replace("hidden: true\n", ""), "backup")
    assert shown.object_id != visible.object_id
    assert shown.semantic() != visible.semantic()


def test_hidden_survives_a_no_template_slide():
    # A `hidden:` slide with neither template nor layout still emits frontmatter.
    md = """---
theme: seriph
---

---
id: bare
hidden: true
---
## Just a title

Body.
"""
    assert "hidden: true" in to_slidev(_slide(md, "bare"))


# --- push emits the skip request --------------------------------------------

def _env(tmp_path, monkeypatch, md=HIDDEN_MD):
    store, drive = e2e.FakeStore(), e2e.FakeDrive()
    slides_api = RecordingSlides(store)
    monkeypatch.setattr(_sync, "get_services", lambda account: (slides_api, drive))
    path = tmp_path / "deck.slidev.md"
    path.write_text(md)
    push(slides_api, drive, DECK, load_slides(path), anchor=None, prune=False,
         base_dir=tmp_path)
    return path, slides_api, store, drive


def _skip_reqs(slides_api):
    return [r["updateSlideProperties"] for b in slides_api.batches for r in b
            if "updateSlideProperties" in r]


def test_push_skips_only_the_hidden_slide(tmp_path, monkeypatch):
    _path, slides_api, store, _drive = _env(tmp_path, monkeypatch)
    backup_id = store.oid_of("Backup detail")
    shown_id = store.oid_of("A point everyone sees")

    skips = _skip_reqs(slides_api)
    assert [r["objectId"] for r in skips] == [backup_id], \
        "exactly the hidden slide is marked skipped"
    assert skips[0]["slideProperties"]["isSkipped"] is True
    assert store._slide(backup_id).get("skipped") is True
    assert store._slide(shown_id).get("skipped", False) is False


def test_no_skip_request_when_nothing_hidden(tmp_path, monkeypatch):
    md = HIDDEN_MD.replace("hidden: true\n", "")
    _path, slides_api, _store, _drive = _env(tmp_path, monkeypatch, md=md)
    assert _skip_reqs(slides_api) == [], "no hidden slides -> no skip requests"


def test_toggling_hidden_off_repushes_unskipped(tmp_path, monkeypatch):
    path, slides_api, store, drive = _env(tmp_path, monkeypatch)
    assert store._slide(store.oid_of("Backup detail")).get("skipped") is True

    # Un-hide: content hash moves, so the slide is replaced by a fresh (un-skipped)
    # object and no new skip request is emitted for it.
    path.write_text(HIDDEN_MD.replace("hidden: true\n", ""))
    slides_api.batches.clear()
    stats = push(slides_api, drive, DECK, load_slides(path), anchor=None,
                 prune=False, base_dir=path.parent)
    assert stats["replace"] == 1
    assert _skip_reqs(slides_api) == []
    assert store._slide(store.oid_of("Backup detail")).get("skipped", False) is False


# --- pull recovers the flag from live isSkipped -----------------------------

def test_pull_recovers_hidden_from_skipped(tmp_path, monkeypatch):
    _path, slides_api, _store, _drive = _env(tmp_path, monkeypatch)
    pulled = {s.key: s for s in pull_slides(slides_api, DECK)}
    assert pulled["backup"].hidden is True
    assert pulled["shown"].hidden is False
    assert "hidden: true" in to_slidev(pulled["backup"])


def test_native_skip_toggle_pulls_back_as_hidden(tmp_path, monkeypatch):
    # A slide pushed visible, then skipped natively in Slides, pulls as hidden.
    md = HIDDEN_MD.replace("hidden: true\n", "")
    _path, slides_api, store, _drive = _env(tmp_path, monkeypatch, md=md)
    store._slide(store.oid_of("Backup detail"))["skipped"] = True
    pulled = {s.key: s for s in pull_slides(slides_api, DECK)}
    assert pulled["backup"].hidden is True


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
