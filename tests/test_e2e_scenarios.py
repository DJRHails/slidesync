"""End-to-end scenarios for push/sync against an in-memory Slides+Drive fake.

The fake interprets exactly the request types slidesync emits (createSlide,
deleteObject, createShape, insertText, deleteText, updateSlidesPosition,
updateSlideProperties, and updateTextStyle for washes and links) and renders
state back in the wire format `presentations.get` returns — so push, the
non-fast-forward guard, and sync's pull-back/capture all run unmodified.
"""

import json
import re
from types import SimpleNamespace

import pytest

from slidesync import _sync
from slidesync._sync import cmd_sync, load_deck, load_slides, push

DECK = "fakedeck"

MD = """---
theme: seriph
---

---
template: topic
id: thread-a
---
# Takeaway A
## FINDING · DATA

One solid point.

<!-- presenter note A -->

---
---
template: content
id: overview
---
## OVERVIEW

First result line.
"""


def _text_els(text, mark=None, links=None):
    """textElements for `text`; `mark` washes that substring with a background
    colour — a styling-only live edit, like a presenter highlighting words —
    and `links` ({substring: link dict}) renders pushed link runs back, as the
    real API does. At most one styled substring per line (all the tests need)."""
    els = []
    wash = {"backgroundColor": {"opaqueColor": {"rgbColor": {"red": 1.0}}}}
    styled = {sub: {"link": link} for sub, link in (links or {}).items()}
    if mark:
        styled[mark] = wash
    for line in text.split("\n"):
        els.append({"paragraphMarker": {}})
        sub = next((s for s in styled if s in line), None)
        if sub:
            pre, post = line.split(sub, 1)
            for chunk, style in ((pre, {}), (sub, styled[sub]), (post + "\n", {})):
                if chunk:
                    els.append({"textRun": {"content": chunk, "style": style}})
        else:
            els.append({"textRun": {"content": line + "\n", "style": {}}})
    return els if text else []


class FakeStore:
    """In-memory deck: ordered slides, each with text shapes + speaker notes."""

    def __init__(self):
        self.slides = []  # {"id", "shapes": {sid: {"y", "x", "text"}}, "notes"}

    def _slide(self, oid):
        return next(s for s in self.slides if s["id"] == oid)

    def _owner_of_shape(self, sid):
        for s in self.slides:
            if sid in s["shapes"] or sid == s["id"] + "_n":
                return s
        raise KeyError(sid)

    def apply(self, reqs):
        for r in reqs:
            if "createSlide" in r:
                self.slides.append({"id": r["createSlide"]["objectId"],
                                    "shapes": {}, "notes": ""})
            elif "deleteObject" in r:
                self.slides = [s for s in self.slides
                               if s["id"] != r["deleteObject"]["objectId"]]
            elif "createShape" in r:
                ep = r["createShape"]["elementProperties"]
                tr = ep.get("transform", {})
                self._slide(ep["pageObjectId"])["shapes"][
                    r["createShape"]["objectId"]] = {
                        "y": tr.get("translateY", 0), "x": tr.get("translateX", 0),
                        "text": ""}
            elif "insertText" in r:
                sid = r["insertText"]["objectId"]
                s = self._owner_of_shape(sid)
                if sid == s["id"] + "_n":
                    s["notes"] += r["insertText"]["text"]
                else:
                    s["shapes"][sid]["text"] += r["insertText"]["text"]
            elif "deleteText" in r:
                sid = r["deleteText"]["objectId"]
                s = self._owner_of_shape(sid)
                if sid == s["id"] + "_n":
                    s["notes"] = ""
                else:
                    s["shapes"][sid]["text"] = ""
            elif "updateSlidesPosition" in r:
                oid = r["updateSlidesPosition"]["slideObjectIds"][0]
                moved = self._slide(oid)
                rest = [s for s in self.slides if s["id"] != oid]
                rest.insert(r["updateSlidesPosition"]["insertionIndex"], moved)
                self.slides = rest
            elif "updateSlideProperties" in r:
                up = r["updateSlideProperties"]
                self._slide(up["objectId"])["skipped"] = \
                    up["slideProperties"].get("isSkipped", False)
            elif "updateTextStyle" in r:
                # Pushed ==highlight== washes and [text](#key) links must show
                # back up in render() — the restyle-capture path compares live
                # run styling against the source, so the fake has to reflect
                # them as the real API does. Other styling is ignored.
                up = r["updateTextStyle"]
                style, rng = up.get("style", {}), up.get("textRange", {})
                s = self._owner_of_shape(up["objectId"])
                sh = s["shapes"].get(up["objectId"])
                if sh and rng.get("type") == "FIXED_RANGE":
                    run = sh["text"][rng["startIndex"]:rng["endIndex"]]
                    if style.get("backgroundColor"):
                        sh["mark"] = run
                    elif style.get("link"):
                        sh.setdefault("links", {})[run] = style["link"]
        return {}

    def render(self):
        out = []
        for s in self.slides:
            els = [{"objectId": sid,
                    "transform": {"translateY": sh["y"], "translateX": sh["x"]},
                    "shape": {"text": {"textElements":
                                       _text_els(sh["text"], sh.get("mark"),
                                                 sh.get("links"))}}}
                   for sid, sh in s["shapes"].items() if sh["text"]]
            nid = s["id"] + "_n"
            out.append({
                "objectId": s["id"], "pageElements": els,
                "slideProperties": {"isSkipped": s.get("skipped", False),
                                    "notesPage": {
                    "notesProperties": {"speakerNotesObjectId": nid},
                    "pageElements": [{"objectId": nid, "shape": {
                        "text": {"textElements": _text_els(s["notes"])}}}]}}})
        return {"slides": out, "layouts": []}

    # test helpers ---------------------------------------------------------
    def oid_of(self, needle):
        for s in self.slides:
            if any(needle in sh["text"] for sh in s["shapes"].values()):
                return s["id"]
        raise AssertionError(f"no live slide contains {needle!r}")

    def edit_text(self, old, new):
        for s in self.slides:
            for sh in s["shapes"].values():
                if old in sh["text"]:
                    sh["text"] = sh["text"].replace(old, new)
                    return
        raise AssertionError(f"no live shape contains {old!r}")

    def mark_text(self, needle):
        """Wash `needle` with a background colour — text lines unchanged."""
        for s in self.slides:
            for sh in s["shapes"].values():
                if needle in sh["text"]:
                    sh["mark"] = needle
                    return
        raise AssertionError(f"no live shape contains {needle!r}")

    def all_text(self):
        return " ".join(sh["text"] for s in self.slides
                        for sh in s["shapes"].values())


class _Req:
    def __init__(self, fn):
        self.execute = fn


class FakeSlides:
    def __init__(self, store):
        self.store = store

    def presentations(self):
        return self

    def get(self, presentationId, fields=None):
        return _Req(lambda: self.store.render())

    def batchUpdate(self, presentationId, body):
        return _Req(lambda: self.store.apply(body["requests"]))


class _FakeComments:
    def __init__(self, drive):
        self.d = drive

    def list(self, fileId, pageSize=None, pageToken=None, fields=None):
        return _Req(lambda: {"comments": self.d.threads})

    def create(self, fileId, body=None, fields=None):
        def go():
            self.d.add_raw(body.get("anchor"), body["content"], author=self.d.me)
            return {"id": self.d.threads[-1]["id"]}
        return _Req(go)

    def delete(self, fileId, commentId):
        def go():
            t = next(t for t in self.d.threads if t["id"] == commentId)
            if t["author"]["displayName"] != self.d.me:
                raise RuntimeError("403: insufficient permissions")  # like Drive
            self.d.threads.remove(t)
            return {}
        return _Req(go)


class _FakeReplies:
    def __init__(self, drive):
        self.d = drive

    def create(self, fileId, commentId, body=None, fields=None):
        def go():
            t = next(t for t in self.d.threads if t["id"] == commentId)
            t["replies"].append({"author": {"displayName": "Daniel Hails"},
                                 "content": body["content"]})
            return {"id": f"r{len(t['replies'])}"}
        return _Req(go)


class FakeDrive:
    me = "Daniel Hails"  # the authenticated account's display name

    def __init__(self):
        self.threads = []  # raw Drive comment dicts
        self.n = 0

    def comments(self):
        return _FakeComments(self)

    def replies(self):
        return _FakeReplies(self)

    def about(self):
        return SimpleNamespace(get=lambda fields=None: _Req(
            lambda: {"user": {"displayName": self.me}}))

    def add_raw(self, anchor, content, author="Daniel Hails"):
        self.n += 1
        self.threads.append({
            "id": f"c{self.n}", "anchor": anchor,
            "author": {"displayName": author},
            "content": content, "replies": []})

    def add(self, page_oid, content, author="Daniel Hails"):
        self.add_raw(json.dumps({"type": "page", "pages": [page_oid]}),
                     content, author)

    def thread_pages(self):
        return {t["id"]: json.loads(t["anchor"])["pages"][0]
                for t in self.threads}


@pytest.fixture
def env(tmp_path, monkeypatch):
    store, drive = FakeStore(), FakeDrive()
    slides_api = FakeSlides(store)
    monkeypatch.setattr(_sync, "get_services", lambda account: (slides_api, drive))
    path = tmp_path / "deck.slidev.md"
    path.write_text(MD)
    push(slides_api, drive, DECK, load_slides(path), anchor=None, prune=False,
         base_dir=tmp_path)
    return SimpleNamespace(store=store, drive=drive, slides=slides_api, path=path)


def _sync_cmd(env):
    cmd_sync(SimpleNamespace(source=env.path, deck=DECK, account=None,
                             prune=False, allow_rekey=False))


def _push(env, force=False, prune=False):
    return push(env.slides, env.drive, DECK, load_slides(env.path), anchor=None,
                prune=prune, base_dir=env.path.parent, force=force)


def _local_edit(env):
    env.path.write_text(env.path.read_text().replace(
        "One solid point.", "One solid point, sharpened."))


def _live_edit(env):
    env.store.edit_text("Takeaway A", "Takeaway A, live-edited")


SCENARIOS = {
    "clean": dict(setup=[], sync_conflicts=False,
                  file_has=[], live_has=["Takeaway A"]),
    "local-edit": dict(setup=[_local_edit], sync_conflicts=False,
                       file_has=["sharpened"], live_has=["sharpened"]),
    "live-drift": dict(setup=[_live_edit], sync_conflicts=False,
                       file_has=["live-edited"], live_has=["live-edited"]),
    "conflict": dict(setup=[_live_edit,
                            lambda e: e.path.write_text(e.path.read_text().replace(
                                "Takeaway A", "Takeaway A, locally-reworded"))],
                     sync_conflicts=True,
                     file_has=["locally-reworded"], live_has=["live-edited"]),
}


@pytest.mark.parametrize("name", SCENARIOS)
def test_sync_scenarios(env, name):
    sc = SCENARIOS[name]
    for step in sc["setup"]:
        step(env)
    if sc["sync_conflicts"]:
        with pytest.raises(SystemExit):
            _sync_cmd(env)
    else:
        _sync_cmd(env)
    text, live = env.path.read_text(), env.store.all_text()
    for needle in sc["file_has"]:
        assert needle in text, f"{name}: {needle!r} missing from markdown"
    for needle in sc["live_has"]:
        assert needle in live, f"{name}: {needle!r} missing from deck"
    if not sc["sync_conflicts"]:  # reconciled: a follow-up sync is a no-op
        before = env.path.read_text()
        _sync_cmd(env)
        assert env.path.read_text() == before


def test_push_rejects_conflicting_replace_and_force_overrides(env):
    _live_edit(env)
    env.path.write_text(env.path.read_text().replace(
        "Takeaway A", "Takeaway A, locally-reworded"))
    with pytest.raises(SystemExit):
        _push(env)
    assert "live-edited" in env.store.all_text()  # guard left the deck intact
    _push(env, force=True)
    assert "locally-reworded" in env.store.all_text()
    assert "live-edited" not in env.store.all_text()


def test_push_rejects_pruning_a_live_edited_slide(env):
    _live_edit(env)
    env.path.write_text(re.sub(r"(?ms)^---\ntemplate: topic\n.*?(?=^---$\n---\n)",
                               "", env.path.read_text(), count=1))
    assert "thread-a" not in env.path.read_text()
    with pytest.raises(SystemExit):
        _push(env, prune=True)
    _push(env, prune=True, force=True)
    assert "Takeaway A" not in env.store.all_text()


def test_push_ignores_live_drift_on_untouched_slides(env):
    # No local change -> nothing is replaced -> nothing can be lost -> no guard.
    _live_edit(env)
    stats = _push(env)
    assert stats["replace"] == 0
    assert "live-edited" in env.store.all_text()


def test_sync_captures_comment_onto_its_slide(env):
    env.drive.add(env.store.oid_of("Takeaway A"), "This is obviously dodgy. To debug")
    _sync_cmd(env)
    text = env.path.read_text()
    assert "<!-- @Daniel Hails: This is obviously dodgy. To debug -->" in text
    assert text.index("Takeaway A") < text.index("obviously dodgy") < text.index("OVERVIEW")
    _sync_cmd(env)  # idempotent: captured text is not duplicated
    assert env.path.read_text().count("obviously dodgy") == 1


def test_sync_recaptures_orphaned_comment_via_key_hash(env):
    env.drive.add(env.store.oid_of("Takeaway A"), "anchored before the re-render")
    _local_edit(env)
    _push(env)  # re-render replaces the slide; the thread's page id now dangles
    _sync_cmd(env)
    assert "<!-- @Daniel Hails: anchored before the re-render -->" in env.path.read_text()


def test_live_wash_on_a_clean_slide_is_captured_as_highlight(env):
    """A presenter highlights words in the Slides UI and nothing else changes:
    text-line drift reads the slide as clean, but the wash (ANY background
    colour, not just slidesync's own amber — the fake washes with pure red)
    must come back as ==...== and re-push in the canonical amber."""
    env.store.mark_text("solid point")
    _sync_cmd(env)
    text = env.path.read_text()
    assert "One ==solid point==." in text
    # The same sync re-pushes the slide, so the canonical wash is already live.
    marks = [sh.get("mark") for s in env.store.slides for sh in s["shapes"].values()]
    assert "solid point" in marks
    _sync_cmd(env)  # idempotent: the re-pushed amber wash matches the source
    assert env.path.read_text().count("==solid point==") == 1


@pytest.fixture
def warnings_log():
    """Warnings emitted through slidesync's loguru logger, as plain strings —
    pytest's capfd/caplog can't see loguru's pre-captured stderr sink."""
    msgs = []
    handle = _sync.logger.add(msgs.append, format="{message}", level="WARNING")
    yield msgs
    _sync.logger.remove(handle)


def _pushed_env(md, tmp_path, monkeypatch):
    """A deck built from `md`, pushed to a fresh fake — for tests whose
    scenario needs a source the shared MD fixture doesn't have."""
    store, drive = FakeStore(), FakeDrive()
    slides_api = FakeSlides(store)
    monkeypatch.setattr(_sync, "get_services", lambda account: (slides_api, drive))
    path = tmp_path / "deck.slidev.md"
    path.write_text(md)
    push(slides_api, drive, DECK, load_slides(path), anchor=None, prune=False,
         base_dir=tmp_path)
    return SimpleNamespace(store=store, drive=drive, slides=slides_api, path=path)


def test_live_wash_without_verbatim_source_text_warns_and_stays(
        tmp_path, monkeypatch, warnings_log):
    """A wash spanning a formatting boundary ("One solid" over the source's
    `One **solid** point.`) has no verbatim source text to wrap: sync must
    warn asking for a hand edit and leave the source alone."""
    env = _pushed_env(MD.replace("One solid point.", "One **solid** point."),
                      tmp_path, monkeypatch)
    before = env.path.read_text()
    env.store.mark_text("One solid")
    _sync_cmd(env)
    assert env.path.read_text() == before
    assert any("add ==...== by hand" in m for m in warnings_log)


def test_live_wash_on_ambiguous_source_text_warns_instead_of_guessing(
        tmp_path, monkeypatch, warnings_log):
    """The washed text occurs twice in the slide's source — first in a note
    comment, then in the body. First-occurrence substitution would wrap the
    comment, where the wash never renders and so never converges. Sync must
    warn and leave the source alone instead of guessing."""
    env = _pushed_env(MD.replace("One solid point.\n\n<!-- presenter note A -->",
                                 "<!-- a solid point, noted -->\n\nOne solid point."),
                      tmp_path, monkeypatch)
    before = env.path.read_text()
    env.store.mark_text("solid point")
    _sync_cmd(env)
    assert env.path.read_text() == before
    assert any("add ==...== by hand" in m for m in warnings_log)


GRAPH_MD = MD + """
---
template: graph
id: fig-placeholder
---
![[Bracketed placeholder alt that defeats IMAGE_RE]](../figures/missing.png)

<!-- caption comment -->
"""


def test_graph_slides_with_unrenderable_text_stay_clean(tmp_path, monkeypatch):
    # A graph/full slide is text-free: body text in the markdown (e.g. an image
    # line whose bracketed alt breaks IMAGE_RE) can never render, so it must
    # not register as drift. Regression: the live probe flagged placeholder
    # graph slides as live-drift forever.
    store, drive = FakeStore(), FakeDrive()
    slides_api = FakeSlides(store)
    monkeypatch.setattr(_sync, "get_services", lambda account: (slides_api, drive))
    path = tmp_path / "deck.slidev.md"
    path.write_text(GRAPH_MD)
    push(slides_api, drive, DECK, load_slides(path), anchor=None, prune=False,
         base_dir=tmp_path)
    before = path.read_text()
    cmd_sync(SimpleNamespace(source=path, deck=DECK, account=None, prune=False, allow_rekey=False))
    assert path.read_text() == before  # no write-back, no conflict, no churn


EQUATION_MD = MD + """
---
template: equation
id: objective
---
## THE OBJECTIVE

# Headline parsed but never rendered

$$
\\max_{\\pi} \\; E[r]
$$

Maximise reward under the monitor budget.
"""


def test_equation_slide_with_h1_fails_sync_up_front(tmp_path, monkeypatch):
    # The equation template renders only the kicker + equation + caption; an
    # `# h1` alongside the kicker never reaches the deck. That used to be
    # tolerated silently (drift kept clean via _content_lines); slot
    # validation now refuses it at the CLI layer before any API call — the
    # direct push() API still accepts it for live decks pushed historically.
    store, drive = FakeStore(), FakeDrive()
    slides_api = FakeSlides(store)
    monkeypatch.setattr(_sync, "get_services", lambda account: (slides_api, drive))
    monkeypatch.setattr(  # the fake Drive can't host the rendered PNG
        _sync, "_resolve_equations",
        lambda drive, slide: [(src, f"https://img/{i}", (1800, 600))
                              for i, src in enumerate(slide.equations)])
    path = tmp_path / "deck.slidev.md"
    path.write_text(EQUATION_MD)
    push(slides_api, drive, DECK, load_slides(path), anchor=None, prune=False,
         base_dir=tmp_path)
    live = store.all_text()
    assert "THE OBJECTIVE" in live and "Maximise reward" in live
    assert "Headline parsed but never rendered" not in live
    before = path.read_text()
    with pytest.raises(SystemExit, match="slot mismatch"):
        cmd_sync(SimpleNamespace(source=path, deck=DECK, account=None,
                                 prune=False, allow_rekey=False))
    assert path.read_text() == before  # refused up front: nothing touched


WEEK_A = """---
theme: seriph
---

---
template: topic
id: thread-x
---
# Newer week takeaway
## FINDING

Newer point one.

[appendix link](#thread-y)
[cross week](#2026-06-01-thread-x)

---
---
template: topic
id: thread-y
---
# Newer week appendix
## BACKUP

Appendix detail.
"""

WEEK_B = """---
theme: seriph
---

---
template: topic
id: thread-x
---
# Older week takeaway
## FINDING

Older point one.
"""


@pytest.fixture
def multi(tmp_path, monkeypatch):
    store, drive = FakeStore(), FakeDrive()
    slides_api = FakeSlides(store)
    monkeypatch.setattr(_sync, "get_services", lambda account: (slides_api, drive))
    a = tmp_path / "2026-06-08.slidev.md"
    b = tmp_path / "2026-06-01.slidev.md"
    a.write_text(WEEK_A)
    b.write_text(WEEK_B)
    push(slides_api, drive, DECK, load_deck([a, b]), anchor=None, prune=False,
         base_dir=tmp_path)
    return SimpleNamespace(store=store, drive=drive, slides=slides_api, a=a, b=b)


def _multi_sync(env):
    cmd_sync(SimpleNamespace(source=[env.a, env.b], deck=DECK, account=None,
                             prune=False, allow_rekey=False))


def test_multi_file_namespaces_ids_and_intra_file_links(multi):
    slides = load_deck([multi.a, multi.b])
    assert [s.key for s in slides] == ["2026-06-08-thread-x", "2026-06-08-thread-y",
                                       "2026-06-01-thread-x"]
    newer = slides[0]
    links = [r.link for p in newer.paras for r in p.runs if r.style == "link"]
    assert "#2026-06-08-thread-y" in links     # intra-file: namespaced
    assert "#2026-06-01-thread-x" in links     # cross-file: already qualified
    assert (newer.src_path, newer.src_key) == (multi.a, "thread-x")


def test_multi_file_comment_capture_routes_to_origin_file(multi):
    multi.drive.add(multi.store.oid_of("Older point one"), "comment on the old week")
    _multi_sync(multi)
    assert "<!-- @Daniel Hails: comment on the old week -->" in multi.b.read_text()
    assert "comment on the old week" not in multi.a.read_text()


def test_multi_file_live_drift_writes_back_to_origin_file(multi):
    multi.store.edit_text("Older point one", "Older point one, live-edited")
    _multi_sync(multi)
    assert "live-edited" in multi.b.read_text()
    assert "live-edited" not in multi.a.read_text()
    # the source file keeps its LOCAL id, not the namespaced one
    assert "id: thread-x" in multi.b.read_text()
    assert "id: 2026-06-01-thread-x" not in multi.b.read_text()


def test_duplicate_keys_across_files_are_rejected(tmp_path):
    a = tmp_path / "same.slidev.md"
    b = tmp_path / "same2.slidev.md"
    a.write_text(WEEK_B)
    b.write_text(WEEK_B.replace("same", "same"))
    # same stem prefix collision: copy a file so namespaced keys collide
    c = tmp_path / "same.copy.slidev.md"  # stem "same" again (split on first dot)
    c.write_text(WEEK_B)
    with pytest.raises(SystemExit):
        load_deck([a, c])


def test_captured_thread_stays_a_comment_not_speaker_notes(env):
    env.drive.add(env.store.oid_of("Takeaway A"), "still a real comment?")
    _sync_cmd(env)  # capture -> re-render -> re-anchor
    # 1. mirrored into the markdown
    assert "<!-- @Daniel Hails: still a real comment? -->" in env.path.read_text()
    # 2. NOT in the slide's speaker notes
    slide = next(s for s in env.store.slides
                 if any("Takeaway A" in sh["text"] for sh in s["shapes"].values()))
    assert "still a real comment" not in slide["notes"]
    # 3. still a live thread, re-anchored to the slide's NEW objectId
    [thread] = env.drive.threads
    page = json.loads(thread["anchor"])["pages"][0]
    assert page == slide["id"], "thread must follow the re-rendered slide"
    # 4. stable: a second sync changes nothing
    before_threads = env.drive.threads.copy()
    before = env.path.read_text()
    _sync_cmd(env)
    assert env.path.read_text() == before and env.drive.threads == before_threads


def test_resolved_threads_are_not_revived(env):
    env.drive.add(env.store.oid_of("Takeaway A"), "old decision, settled")
    _sync_cmd(env)  # captured + re-anchored
    for t in env.drive.threads:
        t["resolved"] = True
    _local_edit(env)  # force a re-render of the commented slide's file
    _sync_cmd(env)
    assert all(t.get("resolved") for t in env.drive.threads), \
        "push must not re-create resolved threads"


def test_foreign_thread_mirrors_with_attribution_and_survives_rerenders(env):
    # Fabien (not the authenticated account) comments on a slide.
    env.drive.add(env.store.oid_of("Takeaway A"), "have you controlled for length?",
                  author="Fabien")
    _sync_cmd(env)
    # Mirrored with HIS name, kept out of the speaker notes.
    text = env.path.read_text()
    assert "<!-- @Fabien: have you controlled for length? -->" in text
    slide = next(s for s in env.store.slides
                 if any("Takeaway A" in sh["text"] for sh in s["shapes"].values()))
    assert "controlled for length" not in slide["notes"]
    # Re-anchored copy exists on the current page, attributed in-content; the
    # undeletable foreign original may dangle, but the count must stay BOUNDED
    # across further re-render cycles (no duplication).
    def live_pages():
        return {json.loads(t["anchor"])["pages"][0] for t in env.drive.threads}
    assert slide["id"] in live_pages()
    n_after_first = len(env.drive.threads)
    for marker in ("cycle two", "cycle three"):
        env.path.write_text(env.path.read_text().replace(
            "One solid point", f"One solid point ({marker})"))
        _sync_cmd(env)
    assert len(env.drive.threads) == n_after_first, "threads must not duplicate"
    current = next(s for s in env.store.slides
                   if any("Takeaway A" in sh["text"] for sh in s["shapes"].values()))
    assert current["id"] in live_pages(), "thread follows the slide across renders"
    anchored = [t for t in env.drive.threads
                if json.loads(t["anchor"])["pages"][0] == current["id"]]
    assert any("@Fabien:" in t["content"] for t in anchored), \
        "re-created head keeps Fabien's attribution in-content"


def test_plain_attribution_notes_are_speaker_notes_not_threads(tmp_path, monkeypatch):
    # The 2026-06-01 deck's mentor annotations are unprefixed attribution notes
    # ("Fabien: ..."), not @-mirrors — they must stay presenter notes.
    store, drive = FakeStore(), FakeDrive()
    slides_api = FakeSlides(store)
    monkeypatch.setattr(_sync, "get_services", lambda account: (slides_api, drive))
    path = tmp_path / "deck.slidev.md"
    path.write_text(MD.replace(
        "<!-- presenter note A -->",
        '<!-- Fabien: over-triggering is "the classic one." Stack-rank fixes '
        "by difficulty / importance / access. -->"))
    push(slides_api, drive, DECK, load_slides(path), anchor=None, prune=False,
         base_dir=tmp_path)
    slide = next(s for s in store.slides
                 if any("Takeaway A" in sh["text"] for sh in s["shapes"].values()))
    assert "over-triggering" in slide["notes"], "plain notes stay in the pane"
    assert not drive.threads, "no comment thread is fabricated from notes"


def test_force_push_does_not_churn_already_anchored_threads(env):
    env.drive.add(env.store.oid_of("Takeaway A"), "anchored and settled")
    _sync_cmd(env)  # capture + re-anchor onto the current page
    stable_ids = {t["id"] for t in env.drive.threads}
    _push(env, force=True)  # re-render with UNCHANGED content -> same objectIds
    assert {t["id"] for t in env.drive.threads} == stable_ids, \
        "an already-anchored thread must not be deleted/recreated"


def test_comment_only_local_change_still_syncs(env):
    # Converting a presenter note to an @-annotation changes no rendered text,
    # but it changes the speaker notes + marker — sync must still push it.
    env.path.write_text(env.path.read_text().replace(
        "<!-- presenter note A -->", "<!-- @Ted: presenter note A -->"))
    _sync_cmd(env)
    slide = next(s for s in env.store.slides
                 if any("Takeaway A" in sh["text"] for sh in s["shapes"].values()))
    assert "presenter note A" not in slide["notes"], \
        "the @-annotation must leave the speaker-notes pane on the next sync"


# ---------------------------------------------------------------------------
# Mass re-key guard: an id-scheme change (or key bug) must never silently
# delete-and-recreate the whole live deck (the 0.10.2 incident: a routine sync
# saw all 391 managed slides as missing and wiped live text highlights).
# ---------------------------------------------------------------------------


def _bulk_md(ids):
    out = ["---\ntheme: seriph\n---\n"]
    for i in ids:
        out.append(f"---\ntemplate: content\nid: {i}\n---\n"
                   f"## SLIDE {i}\n\nPoint {i}.\n")
    return "\n".join(out)


@pytest.fixture
def bulk(tmp_path, monkeypatch):
    """A deck big enough (12 slides) to clear the guard's 10-slide floor."""
    store, drive = FakeStore(), FakeDrive()
    slides_api = FakeSlides(store)
    monkeypatch.setattr(_sync, "get_services", lambda account: (slides_api, drive))
    path = tmp_path / "deck.slidev.md"
    path.write_text(_bulk_md([f"s{i}" for i in range(12)]))
    push(slides_api, drive, DECK, load_slides(path), anchor=None, prune=False,
         base_dir=tmp_path)
    return SimpleNamespace(store=store, drive=drive, slides=slides_api, path=path)


def test_push_refuses_a_mass_rekey_even_with_force(bulk):
    bulk.path.write_text(_bulk_md([f"renamed{i}" for i in range(12)]))
    source = load_slides(bulk.path)
    with pytest.raises(SystemExit, match="mass re-key detected"):
        push(bulk.slides, bulk.drive, DECK, source, anchor=None, prune=True,
             base_dir=bulk.path.parent)
    with pytest.raises(SystemExit, match="mass re-key detected"):
        push(bulk.slides, bulk.drive, DECK, source, anchor=None, prune=True,
             base_dir=bulk.path.parent, force=True)
    assert len(bulk.store.slides) == 12  # deck untouched by refused pushes
    assert "Point s0." in bulk.store.all_text()


def test_allow_rekey_lets_the_push_through(bulk):
    bulk.path.write_text(_bulk_md([f"renamed{i}" for i in range(12)]))
    stats = push(bulk.slides, bulk.drive, DECK, load_slides(bulk.path),
                 anchor=None, prune=True, base_dir=bulk.path.parent,
                 allow_rekey=True)
    assert stats["create"] == 12 and stats["prune"] == 12
    assert len(bulk.store.slides) == 12


def test_sync_push_half_refuses_a_mass_rekey(bulk):
    bulk.path.write_text(_bulk_md([f"renamed{i}" for i in range(12)]))
    with pytest.raises(SystemExit, match="mass re-key detected"):
        cmd_sync(SimpleNamespace(source=bulk.path, deck=DECK, account=None,
                                 prune=True, allow_rekey=False))
    assert len(bulk.store.slides) == 12  # deck untouched


def test_normal_pushes_never_trip_the_guard(bulk):
    # A few new slides + a content edit is everyday traffic, not a re-key.
    ids = [f"s{i}" for i in range(12)] + ["brand-new"]
    bulk.path.write_text(_bulk_md(ids).replace("Point s3.", "Point s3, updated."))
    stats = push(bulk.slides, bulk.drive, DECK, load_slides(bulk.path),
                 anchor=None, prune=False, base_dir=bulk.path.parent)
    assert stats["create"] == 2 and stats["skip"] == 11


def test_sync_captures_live_edits_across_a_hash_scheme_change(env, monkeypatch):
    # Simulate an id-scheme change: same markdown, same human ids, but every
    # key_hash moves. The notes-marker id must keep the live copies matched so
    # BOTH kinds of live edit are captured before the deck is recreated:
    # a text edit (drift) and a styling-only highlight (invisible to text
    # lines). 2 managed slides is far below the guard threshold, so the
    # follow-up push runs without --allow-rekey.
    _live_edit(env)                          # text edit on one live slide
    env.store.mark_text("First result")      # highlight wash on the other
    orig_sha10 = _sync._sha10
    monkeypatch.setattr(_sync, "_sha10", lambda text: orig_sha10(f"v2|{text}"))
    _sync_cmd(env)
    text = env.path.read_text()
    assert "live-edited" in text, "text drift must be captured across a re-key"
    assert "==First result==" in text, \
        "styling-only live edits must be captured across a re-key"
    assert "live-edited" in env.store.all_text()
