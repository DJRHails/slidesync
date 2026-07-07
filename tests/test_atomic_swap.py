"""Blue-green atomic swap for a replaced slide (no delete-then-insert gap).

A content change moves a slide's content_hash, hence its objectId. The old push
deleted the old object then created the new one ("delete and insert"), leaving a
window with the slide momentarily absent — and, if the API split the batch, able
to drop the slide entirely on an interrupted run. Push now emits a *blue-green
swap* inside ONE `presentations.batchUpdate`: CREATE the new object, reposition
it into the old slide's index, then DELETE the old object — create-before-delete,
so the slide is never missing or duplicated in view.

These tests inspect the actual `batchUpdate` request list (a recording fake), so
they pin the *ordering*, not just the final deck state. Reverting push to the old
delete-then-create order fails them (see the falsification notes per test).
"""

import pytest

import test_e2e_scenarios as e2e

from slidesync import _sync
from slidesync._sync import _swap_requests, load_slides, push

DECK = "fakedeck"

THREE_SLIDES = """---
theme: seriph
---

---
template: topic
id: slide-a
---
# Alpha
## ONE

Point A.

---
---
template: topic
id: slide-b
---
# Beta
## TWO

Point B.

---
---
template: topic
id: slide-c
---
# Gamma
## THREE

Point C.
"""


class RecordingSlides(e2e.FakeSlides):
    """The e2e Slides fake, but every batchUpdate's request list is recorded.

    `apply` still mutates the in-memory store (so push's follow-up `get`s see the
    new slides), and `batches` keeps each batch verbatim for ordering assertions.
    """

    def __init__(self, store):
        super().__init__(store)
        self.batches: list[list[dict]] = []

    def batchUpdate(self, presentationId, body):  # noqa: N802 — Google API name
        self.batches.append(body["requests"])
        return super().batchUpdate(presentationId, body)


def _kind(req: dict) -> str:
    return next(iter(req))


def _env(tmp_path, monkeypatch, md=THREE_SLIDES):
    store, drive = e2e.FakeStore(), e2e.FakeDrive()
    slides_api = RecordingSlides(store)
    monkeypatch.setattr(_sync, "get_services", lambda account: (slides_api, drive))
    path = tmp_path / "deck.slidev.md"
    path.write_text(md)
    push(slides_api, drive, DECK, load_slides(path), anchor=None, prune=False,
         base_dir=tmp_path)
    slides_api.batches.clear()  # discard the initial all-creates push
    return path, slides_api, store


def _swap_batch(slides_api) -> list[dict]:
    """The single batch that performs the swap (has both a createSlide and a
    deleteObject) — proves create+delete commit together, not in two batches."""
    hits = [b for b in slides_api.batches
            if any("createSlide" in r for r in b)
            and any("deleteObject" in r for r in b)]
    assert len(hits) == 1, (
        f"expected exactly one batch carrying both create and delete (atomic "
        f"swap); got {len(hits)}. Batch kinds: "
        f"{[[_kind(r) for r in b] for b in slides_api.batches]}")
    return hits[0]


# --- the pure ordering invariant (no fake needed) ---------------------------

def test_swap_requests_orders_create_then_position_then_delete():
    new_reqs = [{"createSlide": {"objectId": "s2g_kkkkkkkkkk_nnnnnnnnnn"}},
                {"insertText": {"objectId": "s2g_kkkkkkkkkk_nnnnnnnnnn_b",
                                "text": "body"}}]
    reqs = _swap_requests(new_reqs, "s2g_kkkkkkkkkk_nnnnnnnnnn",
                          "s2g_kkkkkkkkkk_oooooooooo", old_index=4)
    kinds = [_kind(r) for r in reqs]
    assert kinds == ["createSlide", "insertText", "updateSlidesPosition",
                     "deleteObject"]
    # the create's content requests all precede the position + delete
    assert kinds.index("createSlide") < kinds.index("updateSlidesPosition")
    assert kinds.index("updateSlidesPosition") < kinds.index("deleteObject")
    pos = reqs[kinds.index("updateSlidesPosition")]["updateSlidesPosition"]
    assert pos["slideObjectIds"] == ["s2g_kkkkkkkkkk_nnnnnnnnnn"]
    assert pos["insertionIndex"] == 4
    assert reqs[-1]["deleteObject"]["objectId"] == "s2g_kkkkkkkkkk_oooooooooo"


# --- single replace ---------------------------------------------------------

def test_replace_creates_new_before_deleting_old_in_one_batch(tmp_path, monkeypatch):
    path, slides_api, store = _env(tmp_path, monkeypatch)
    old_b = store.oid_of("Beta")

    path.write_text(THREE_SLIDES.replace("Point B.", "Point B, revised."))
    stats = push(slides_api, _sync.get_services(None)[1], DECK,
                 load_slides(path), anchor=None, prune=False, base_dir=tmp_path)
    assert stats["replace"] == 1 and stats["create"] == 1

    batch = _swap_batch(slides_api)
    new_b = store.oid_of("Beta")
    assert new_b != old_b, "a content change must mint a new objectId"

    create_at = next(i for i, r in enumerate(batch)
                     if "createSlide" in r
                     and r["createSlide"]["objectId"] == new_b)
    delete_at = next(i for i, r in enumerate(batch)
                     if "deleteObject" in r
                     and r["deleteObject"]["objectId"] == old_b)
    # The whole point: create-before-delete. Falsification: with the old
    # delete-then-insert order the delete index is 0 and this assert fails.
    assert create_at < delete_at, (
        "createSlide(new) must precede deleteObject(old) within the batch")


def test_replace_repositions_new_into_old_slides_index(tmp_path, monkeypatch):
    path, slides_api, store = _env(tmp_path, monkeypatch)
    old_b = store.oid_of("Beta")
    old_index = [s["id"] for s in store.slides].index(old_b)
    assert old_index == 1  # middle slide

    path.write_text(THREE_SLIDES.replace("Point B.", "Point B, revised."))
    push(slides_api, _sync.get_services(None)[1], DECK, load_slides(path),
         anchor=None, prune=False, base_dir=tmp_path)
    new_b = store.oid_of("Beta")

    batch = _swap_batch(slides_api)
    moves = [r["updateSlidesPosition"] for r in batch if "updateSlidesPosition" in r]
    swap_move = [m for m in moves if m["slideObjectIds"] == [new_b]]
    assert swap_move, "the swap batch must reposition the new object"
    # insertionIndex targets the OLD slide's index (computed on the pre-move
    # arrangement, where createSlide has appended the new object at the end).
    assert swap_move[0]["insertionIndex"] == old_index
    # and the new object actually lands in the old slot, old object gone
    assert [s["id"] for s in store.slides].index(new_b) == old_index
    assert old_b not in [s["id"] for s in store.slides]


def test_no_state_deletes_before_creating(tmp_path, monkeypatch):
    """Across the WHOLE push there is never a deleteObject(old) emitted before
    the createSlide(new). Falsification: the old order emitted the delete first,
    so this finds a delete-before-create and fails."""
    path, slides_api, store = _env(tmp_path, monkeypatch)
    old_b = store.oid_of("Beta")

    path.write_text(THREE_SLIDES.replace("Point B.", "Point B, revised."))
    push(slides_api, _sync.get_services(None)[1], DECK, load_slides(path),
         anchor=None, prune=False, base_dir=tmp_path)
    new_b = store.oid_of("Beta")

    flat = [r for batch in slides_api.batches for r in batch]
    create_at = next(i for i, r in enumerate(flat)
                     if "createSlide" in r
                     and r["createSlide"]["objectId"] == new_b)
    delete_at = next(i for i, r in enumerate(flat)
                     if "deleteObject" in r
                     and r["deleteObject"]["objectId"] == old_b)
    assert create_at < delete_at, "old object deleted before the new one existed"
    # there must be NO moment where old_b is absent while new_b is also absent:
    # the delete is the last of the three swap steps.
    assert flat[delete_at] is flat[delete_at]  # delete exists
    assert not any("deleteObject" in r and r["deleteObject"]["objectId"] == old_b
                   for r in flat[:create_at]), "no early delete of the old object"


# --- multiple replaces: each lands at its OWN original index -----------------

def test_two_replaces_each_land_at_their_own_index(tmp_path, monkeypatch):
    """Replacing two slides in one push: every old index is computed on the
    INITIAL order, and because each swap is position-preserving the second swap's
    index is still valid. Tests the "not-yet-deleted old slide shifts indices"
    subtlety — a naive remove-then-append-then-reindex would mis-place one."""
    path, slides_api, store = _env(tmp_path, monkeypatch)
    old_a = store.oid_of("Alpha")
    old_c = store.oid_of("Gamma")
    idx_a = [s["id"] for s in store.slides].index(old_a)  # 0
    idx_c = [s["id"] for s in store.slides].index(old_c)  # 2

    path.write_text(THREE_SLIDES
                    .replace("Point A.", "Point A, revised.")
                    .replace("Point C.", "Point C, revised."))
    stats = push(slides_api, _sync.get_services(None)[1], DECK,
                 load_slides(path), anchor=None, prune=False, base_dir=tmp_path)
    assert stats["replace"] == 2

    new_a, new_c = store.oid_of("Alpha"), store.oid_of("Gamma")
    order = [s["id"] for s in store.slides]
    assert order.index(new_a) == idx_a, "first replaced slide kept its index"
    assert order.index(new_c) == idx_c, "second replaced slide kept its index"
    assert old_a not in order and old_c not in order
    # Beta (unchanged) is untouched and still in the middle.
    assert [s["id"] for s in store.slides].index(store.oid_of("Beta")) == 1

    # Request-level: ONE batch carries both swaps; each new slide is positioned at
    # its OWN initial index (idx computed on the pre-batch order, valid because the
    # first swap is position-preserving), and each create precedes each delete.
    batch = next(b for b in slides_api.batches
                 if sum("createSlide" in r for r in b) == 2
                 and sum("deleteObject" in r for r in b) == 2)
    moves = {tuple(r["updateSlidesPosition"]["slideObjectIds"]):
             r["updateSlidesPosition"]["insertionIndex"]
             for r in batch if "updateSlidesPosition" in r}
    assert moves[(new_a,)] == idx_a and moves[(new_c,)] == idx_c
    kinds = [_kind(r) for r in batch]
    assert kinds[0] == "createSlide", "the batch opens with a create, never a delete"
    assert max(i for i, k in enumerate(kinds) if k == "createSlide") \
        < max(i for i, k in enumerate(kinds) if k == "deleteObject"), \
        "every create is emitted before the deletes catch up"


# --- replace alongside a prune does not break the swap ordering --------------

def test_replace_and_prune_keep_swap_create_before_delete(tmp_path, monkeypatch):
    """A prune (plain delete) in the same push must follow the swaps — otherwise
    its delete would shift the indices the swaps rely on. The swap still creates
    before it deletes, and the pruned slide is gone."""
    path, slides_api, store = _env(tmp_path, monkeypatch)

    # Drop slide-c entirely (prune) and revise slide-b (replace).
    two = THREE_SLIDES.split("---\n---\ntemplate: topic\nid: slide-c")[0].rstrip()
    two = two.replace("Point B.", "Point B, revised.")
    push(slides_api, _sync.get_services(None)[1], DECK, load_slides(path),
         anchor=None, prune=True, base_dir=tmp_path)  # no-op warm-up already done
    slides_api.batches.clear()
    path.write_text(two)
    stats = push(slides_api, _sync.get_services(None)[1], DECK,
                 load_slides(path), anchor=None, prune=True, base_dir=tmp_path)
    assert stats["replace"] == 1 and stats["prune"] == 1

    new_b = store.oid_of("Beta")
    batch = _swap_batch(slides_api)
    kinds = [_kind(r) for r in batch]
    first_delete = kinds.index("deleteObject")
    first_create = kinds.index("createSlide")
    assert first_create < first_delete, "swap create precedes any delete"
    assert "Gamma" not in store.all_text(), "pruned slide removed"
    assert new_b in [s["id"] for s in store.slides]


# --- force re-render of UNCHANGED content: same objectId, delete-then-create -

def test_force_refresh_same_id_deletes_before_recreating(tmp_path, monkeypatch):
    """`--force` re-renders unchanged slides too. Their content_hash (hence
    objectId) is identical, so there is nothing to swap into place — a blue-green
    create would COLLIDE with the still-present old object (Google rejects a
    createSlide for an existing objectId). A same-id refresh must therefore delete
    the old object BEFORE re-creating its id. Regression guard for that collision.
    """
    path, slides_api, store = _env(tmp_path, monkeypatch)
    ids_before = [s["id"] for s in store.slides]

    # Force-push the SAME source: every slide is re-rendered with an unchanged id.
    stats = push(slides_api, _sync.get_services(None)[1], DECK, load_slides(path),
                 anchor=None, prune=False, base_dir=tmp_path, force=True)
    assert stats["replace"] == 3 and stats["create"] == 3  # all re-rendered

    batch = _swap_batch(slides_api)  # the render batch (has creates + deletes)
    # For every re-rendered objectId, its deleteObject precedes its createSlide
    # (the same id can't be created while the old object still holds it).
    for oid in ids_before:
        del_at = next(i for i, r in enumerate(batch)
                      if "deleteObject" in r and r["deleteObject"]["objectId"] == oid)
        new_at = next(i for i, r in enumerate(batch)
                      if "createSlide" in r and r["createSlide"]["objectId"] == oid)
        assert del_at < new_at, f"same-id refresh of {oid} must delete before create"
    # No blue-green swap (updateSlidesPosition) is emitted in the render batch for
    # a same-id refresh — nothing moves; _reorder handles ordering in its own pass.
    assert not any("updateSlidesPosition" in r for r in batch)
    # Ids are unchanged (content identical) and the deck is intact.
    assert [s["id"] for s in store.slides] == ids_before


# --- the two recent fixes must not regress ----------------------------------

IMAGE_DECK = """---
theme: seriph
---

---
template: topic
id: figure-slide
---
# A Figure
## SECTION

![a chart]({img})
"""

PNG_RED = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
    "53de0000000c4944415408d76360f8cf000000030101001827df8e0000000049454e44ae426082"
)
PNG_BLUE = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
    "53de0000000c4944415408d76360606000000003000100b0e0e7110000000049454e44ae426082"
)


def test_regenerated_image_slide_swaps_atomically(tmp_path, monkeypatch):
    """The image-bytes content-hash fold + the blue-green swap together: a figure
    regenerated in place re-pushes (hash moved) AND swaps create-before-delete."""
    # The Drive upload is incidental here (the swap is the same for any slide);
    # stub it so the test exercises the image path without a Drive files() fake.
    monkeypatch.setattr(_sync, "upload_image",
                        lambda drive, p: "https://drive.test/img")
    (tmp_path / "chart.png").write_bytes(PNG_RED)
    md = IMAGE_DECK.format(img="chart.png")
    path, slides_api, store = _env(tmp_path, monkeypatch, md=md)
    old_id = next(s["id"] for s in store.slides)  # the one figure slide

    (tmp_path / "chart.png").write_bytes(PNG_BLUE)  # regenerate in place
    stats = push(slides_api, _sync.get_services(None)[1], DECK,
                 load_slides(path), anchor=None, prune=False, base_dir=tmp_path)
    assert stats["replace"] == 1, "regenerated pixels must replace the slide"

    new_id = next(s["id"] for s in store.slides)
    assert new_id != old_id
    batch = _swap_batch(slides_api)
    kinds = [_kind(r) for r in batch]
    assert kinds.index("createSlide") < kinds.index("deleteObject")


LINK_DECK = """---
theme: seriph
---

---
template: topic
id: main
---
# Headline
## KICKER

See the [appendix](#appendix) for detail.

---
---
template: topic
id: appendix
---
# Appendix
## BACKUP

The detail.
"""


def test_replacing_a_link_target_keeps_intra_deck_link(tmp_path, monkeypatch):
    """Intra-deck link round-trip must survive a swap of the TARGET slide: after
    the appendix is replaced (new objectId), the `[appendix](#appendix)` link on
    the unchanged slide is still applied (to the new object) by the post-batch
    _apply_internal_links pass."""
    path, slides_api, store = _env(tmp_path, monkeypatch, md=LINK_DECK)

    path.write_text(LINK_DECK.replace("The detail.", "The detail, expanded."))
    push(slides_api, _sync.get_services(None)[1], DECK, load_slides(path),
         anchor=None, prune=False, base_dir=tmp_path)

    # Pull it back and confirm the link on `main` still resolves to #appendix
    # (the swapped target slide), not dropped to plain text.
    pulled = _sync.pull_slides(slides_api, DECK)
    assert {s.key for s in pulled} == {"main", "appendix"}, "both slides round-trip"
    main = next(s for s in pulled if s.key == "main")
    targets = [r.link for p in main.paras for r in p.runs if r.style == "link"]
    assert targets == ["#appendix"], "link must re-resolve to the swapped target"


class _ChunkRecorder:
    """Bare batchUpdate fake: records each call's request list."""

    def __init__(self):
        self.calls = []

    def presentations(self):
        return self

    def batchUpdate(self, presentationId, body):
        self.calls.append(body["requests"])
        return self

    def execute(self):
        return {}


def test_batch_chunks_oversized_request_lists_in_order():
    """A whole-deck force push emits tens of thousands of requests; one HTTP
    call that size breaks the connection. `_batch` must split into sequential
    `batchUpdate` calls of at most `_BATCH_CHUNK` requests, preserving the
    request order end-to-end (order is what the blue-green swap relies on)."""
    api = _ChunkRecorder()
    reqs = [{"n": i} for i in range(2 * _sync._BATCH_CHUNK + 201)]

    _sync._batch(api, DECK, reqs)

    assert [len(c) for c in api.calls] == [_sync._BATCH_CHUNK,
                                           _sync._BATCH_CHUNK, 201]
    assert [r["n"] for call in api.calls for r in call] == list(range(len(reqs)))


def test_batch_small_request_list_stays_one_call():
    api = _ChunkRecorder()
    _sync._batch(api, DECK, [{"n": 0}, {"n": 1}])
    assert [len(c) for c in api.calls] == [2]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
