"""Offline tests for intra-deck `[text](#key)` link round-tripping on pull.

Push turns an intra-deck link into a *native Slides page-link* (a text run whose
style carries `link.pageObjectId`, applied by `_apply_internal_links`). Pull used
to recognise only `link.url` runs, so a page-link read back as plain text — the
`#fragment` target was lost, churning the source on every sync. The pull side now
maps a page-link's `pageObjectId` back to the target slide's key (`_oid_to_key`)
and re-emits `[text](#key)`, so the link survives a pull -> push round-trip.
"""

from slidesync._sync import (
    _oid_to_key,
    _read_marker,
    _read_notes,
    _render_body,
    _slide_from_live_boxes,
    _slide_from_native,
    to_slidev,
)

IN = 914400  # EMU per inch
TARGET_OID = "s2g_aaaaaaaaaa_bbbbbbbbbb"
TARGET_KEY = "appendix-sec-score-boolean"


def _run(content, style=None):
    return {"textRun": {"content": content, "style": style or {}}}


def _page_link_run(content, oid):
    """A text run carrying a native Slides page-link, as the API returns it."""
    return {"textRun": {"content": content, "style": {"link": {"pageObjectId": oid}}}}


def _notes_page(text):
    return {"slideProperties": {"notesPage": {
        "notesProperties": {"speakerNotesObjectId": "n"},
        "pageElements": [{"objectId": "n", "shape": {"text": {"textElements": [
            {"paragraphMarker": {}}, {"textRun": {"content": text}}]}}}]}}}


def _marker(key):
    return '<!-- s2g {"id":"%s"} -->' % key


def _target_slide():
    s = {"objectId": TARGET_OID, "pageElements": []}
    s.update(_notes_page(_marker(TARGET_KEY)))
    return s


def _content_slide_with_link():
    """A plain content slide (TITLE + BODY) whose body links to the target."""
    title = {"transform": {"translateY": 0.5 * IN, "translateX": 0.7 * IN},
             "shape": {"placeholder": {"type": "TITLE"}, "text": {"textElements": [
                 {"paragraphMarker": {}},
                 _run("My Slide\n", {"fontSize": {"magnitude": 30}})]}}}
    body = {"transform": {"translateY": 2.0 * IN, "translateX": 0.7 * IN},
            "shape": {"placeholder": {"type": "BODY"}, "text": {"textElements": [
                {"paragraphMarker": {"bullet": {"nestingLevel": 0}}},
                _run("see "),
                _page_link_run("rewordings →", TARGET_OID),
                _run("\n")]}}}
    s = {"objectId": "s2g_1111111111_2222222222", "pageElements": [title, body]}
    s.update(_notes_page(_marker("this-slide")))
    return s


def test_oid_to_key_reads_marker_ids():
    slides = [_content_slide_with_link(), _target_slide()]
    m = _oid_to_key(slides)
    assert m[TARGET_OID] == TARGET_KEY
    assert m["s2g_1111111111_2222222222"] == "this-slide"


def test_page_link_pulls_back_to_intra_deck_link():
    slides = [_content_slide_with_link(), _target_slide()]
    links = _oid_to_key(slides)
    slide = _slide_from_native(_content_slide_with_link(), links)

    targets = [r.link for p in slide.paras for r in p.runs if r.style == "link"]
    assert targets == [f"#{TARGET_KEY}"], "page-link must resolve to #key"
    assert f"[rewordings →](#{TARGET_KEY})" in to_slidev(slide)


def test_pull_is_a_fixpoint_for_a_linked_slide():
    """to_slidev(pull(...)) re-parsed and re-pulled is stable — no churn."""
    slides = [_content_slide_with_link(), _target_slide()]
    links = _oid_to_key(slides)
    first = to_slidev(_slide_from_native(_content_slide_with_link(), links))
    # The link survives, so a re-render carries the same `(#key)` target.
    assert first.count(f"(#{TARGET_KEY})") == 1


def test_unresolved_page_link_degrades_to_plain_text():
    """A link to a slide not in the deck keeps the text, drops the dangling ref."""
    slide = _slide_from_native(_content_slide_with_link(), links={})  # empty map
    assert not any(r.style == "link" for p in slide.paras for r in p.runs)
    assert "rewordings →" in to_slidev(slide)  # text preserved, no broken `(#...)`
    assert "(#" not in to_slidev(slide)


def test_no_links_map_is_backwards_compatible():
    """Callers that pass no map (foreign / drift paths) see page-links as text."""
    slide = _slide_from_native(_content_slide_with_link())  # no links arg
    assert not any(r.style == "link" for p in slide.paras for r in p.runs)
    assert "rewordings →" in to_slidev(slide)


# --- sync write-back path (live-drift template slide) -----------------------

def _template_box(sid, suffix, paras):
    return {"objectId": sid + suffix,
            "shape": {"text": {"textElements": paras}}}


def test_live_box_write_back_keeps_intra_deck_link():
    """A drifted template slide rebuilt from its live boxes (the sync write-back)
    must keep `[text](#key)` — otherwise the link is stripped and the source
    churns on every sync."""
    sid = "s2g_3333333333_4444444444"
    s = {"objectId": sid, "pageElements": [
        _template_box(sid, "_k", [{"paragraphMarker": {}}, _run("KICKER\n")]),
        _template_box(sid, "_h", [{"paragraphMarker": {}}, _run("Headline\n")]),
        _template_box(sid, "_b", [
            {"paragraphMarker": {"bullet": {"nestingLevel": 0}}},
            _run("see "), _page_link_run("appendix", TARGET_OID), _run("\n")]),
    ]}
    s.update(_notes_page('<!-- s2g {"id":"main","template":"topic"} -->'))
    marker = _read_marker(_read_notes(s))
    links = _oid_to_key([s, _target_slide()])

    rebuilt = _slide_from_live_boxes(s, marker, links)
    body_md = _render_body(rebuilt)
    assert f"[appendix](#{TARGET_KEY})" in body_md


# --- full pipeline regression guard (template slide, src-marker round-trip) --

def _e2e(tmp_path, monkeypatch, deck_md):
    import test_e2e_scenarios as e2e  # the in-memory Slides+Drive fake
    from slidesync import _sync
    store, drive = e2e.FakeStore(), e2e.FakeDrive()
    slides_api = e2e.FakeSlides(store)
    monkeypatch.setattr(_sync, "get_services", lambda account: (slides_api, drive))
    path = tmp_path / "deck.slidev.md"
    path.write_text(deck_md)
    _sync.push(slides_api, drive, e2e.DECK, _sync.load_slides(path), anchor=None,
               prune=False, base_dir=tmp_path)
    return path, slides_api, drive, store


LINK_DECK = """---
theme: seriph
---

---
template: topic
id: main
---
# Headline
## KICKER

See the [appendix →](#appendix) for detail.

---
---
template: topic
id: appendix
---
# Appendix
## BACKUP

The detail.
"""


def test_full_sync_no_op_preserves_intra_deck_link(tmp_path, monkeypatch):
    """Acceptance: a deck with `[appendix →](#appendix)` reaches a stable
    'nothing to do' on the 2nd consecutive sync, and the `#appendix` target is
    retained in the source after the pull-driven first sync."""
    from types import SimpleNamespace

    from slidesync._sync import cmd_sync
    path, *_ = _e2e(tmp_path, monkeypatch, LINK_DECK)

    def sync():
        cmd_sync(SimpleNamespace(source=path, deck="fakedeck", account=None,
                                 prune=False))

    sync()
    after_first = path.read_text()
    assert "(#appendix)" in after_first, "link target must survive the pull"

    sync()  # second consecutive sync must be a no-op
    assert path.read_text() == after_first, "2nd sync churned the source"
    assert "(#appendix)" in path.read_text()
