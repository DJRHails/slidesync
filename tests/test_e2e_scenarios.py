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
from slidesync._sync import cmd_sync, load_slides, push

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


class FakeDrive:
    def __init__(self):
        self.threads = []  # raw Drive comment dicts

    def comments(self):
        return self

    def list(self, fileId, pageSize=None, pageToken=None, fields=None):
        return _Req(lambda: {"comments": self.threads})

    def add(self, page_oid, content, author="Daniel Hails"):
        self.threads.append({
            "id": f"c{len(self.threads)}",
            "anchor": json.dumps({"type": "page", "pages": [page_oid]}),
            "author": {"displayName": author},
            "content": content, "replies": []})


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
    cmd_sync(SimpleNamespace(source=env.path, deck=DECK, account=None))


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
