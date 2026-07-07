"""Plan-level tests for the mass re-key guard (no live API needed).

An id-scheme change (or key-computation bug) makes every local slide look
brand-new while every live `s2g_` slide matches no local key; pushing that
plan recreates the deck from markdown and destroys live styling/edits on the
old copies (the 0.10.2 incident: a routine sync saw all 391 managed slides as
missing and wiped live text highlights applied minutes earlier). `mass_rekey`
detects the shape of that plan from (source, managed) alone; `push` refuses it
without --allow-rekey — the end-to-end refusal lives in test_e2e_scenarios.
"""

from slidesync._sync import REKEY_MIN_SLIDES, Slide, _finalize, mass_rekey


def _slides(keys, body=""):
    out = []
    for key in keys:
        slide = Slide(key, "content", title=f"Title {key}{body}")
        out.append(_finalize(slide))
    return out


def _managed(slides):
    return {s.key_hash: (s.object_id, s.content_hash) for s in slides}


def test_full_rekey_trips_the_guard():
    managed = _managed(_slides([f"old{i}" for i in range(20)]))
    source = _slides([f"new{i}" for i in range(20)])
    assert mass_rekey(source, managed) == (20, 20)


def test_mass_content_edits_with_stable_keys_are_safe():
    keys = [f"k{i}" for i in range(40)]
    managed = _managed(_slides(keys))
    edited = _slides(keys, body=" — every content hash moves")
    assert mass_rekey(edited, managed) is None


def test_small_decks_never_trip():
    n = REKEY_MIN_SLIDES - 1
    managed = _managed(_slides([f"old{i}" for i in range(n)]))
    source = _slides([f"new{i}" for i in range(n)])
    assert mass_rekey(source, managed) is None


def test_a_few_new_slides_are_safe():
    keys = [f"k{i}" for i in range(30)]
    managed = _managed(_slides(keys))
    source = _slides(keys + [f"extra{i}" for i in range(9)])
    assert mass_rekey(source, managed) is None  # nothing live is orphaned


def test_both_conditions_must_hold():
    # 9 renamed slides orphan 9 live copies — below the 10-slide floor on a
    # 30-slide deck (threshold = max(10, ceil(0.3 * 30)) = 10), so no trip;
    # renaming one more crosses the floor on both counts and trips.
    keys = [f"k{i}" for i in range(30)]
    managed = _managed(_slides(keys))
    renamed_9 = _slides([f"r{i}" for i in range(9)] + keys[9:])
    assert mass_rekey(renamed_9, managed) is None
    renamed_10 = _slides([f"r{i}" for i in range(10)] + keys[10:])
    assert mass_rekey(renamed_10, managed) == (10, 10)


def test_threshold_scales_with_deck_size():
    # On a 100-slide deck the threshold is ceil(0.3 * 100) = 30, not the floor.
    keys = [f"k{i}" for i in range(100)]
    managed = _managed(_slides(keys))
    renamed_29 = _slides([f"r{i}" for i in range(29)] + keys[29:])
    assert mass_rekey(renamed_29, managed) is None
    renamed_30 = _slides([f"r{i}" for i in range(30)] + keys[30:])
    assert mass_rekey(renamed_30, managed) == (30, 30)


def test_brand_new_deck_is_safe():
    assert mass_rekey(_slides([f"k{i}" for i in range(50)]), {}) is None
