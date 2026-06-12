"""End-to-end scenarios for push/sync against an in-memory Slides+Drive fake.

The fake interprets exactly the request types slidesync emits (createSlide,
deleteObject, createShape, insertText, deleteText, updateSlidesPosition) and
renders state back in the wire format `presentations.get` returns — so push,
the non-fast-forward guard, and sync's pull-back/capture all run unmodified.
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


def _text_els(text):
    els = []
    for line in text.split("\n"):
        els.append({"paragraphMarker": {}})
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
            # styling-only requests are ignored
        return {}

    def render(self):
        out = []
        for s in self.slides:
            els = [{"objectId": sid,
                    "transform": {"translateY": sh["y"], "translateX": sh["x"]},
                    "shape": {"text": {"textElements": _text_els(sh["text"])}}}
                   for sid, sh in s["shapes"].items() if sh["text"]]
            nid = s["id"] + "_n"
            out.append({
                "objectId": s["id"], "pageElements": els,
                "slideProperties": {"notesPage": {
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
            self.d.add_raw(body.get("anchor"), body["content"])
            return {"id": self.d.threads[-1]["id"]}
        return _Req(go)

    def delete(self, fileId, commentId):
        def go():
            self.d.threads = [t for t in self.d.threads if t["id"] != commentId]
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
    def __init__(self):
        self.threads = []  # raw Drive comment dicts
        self.n = 0

    def comments(self):
        return _FakeComments(self)

    def replies(self):
        return _FakeReplies(self)

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
    cmd_sync(SimpleNamespace(source=env.path, deck=DECK, account=None, prune=False))


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
    cmd_sync(SimpleNamespace(source=path, deck=DECK, account=None, prune=False))
    assert path.read_text() == before  # no write-back, no conflict, no churn


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
                             prune=False))


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
