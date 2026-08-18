"""
Transcript refinement tests.

Pure functions over fixture data — no model, no audio, no network. The
behaviours here are the difference between a cut that sounds automated and one
that sounds deliberate, so each is pinned individually.
"""

import pytest

from ave.media.transcribe import Segment, Transcript, Word
from ave.plan.refine import SENTENCE_SNAP_S, merge_overlaps, refine, subtract


def transcript(words, sentence_ends=None):
    """`words` is (text, start, end)."""
    objects = [Word(text, start, end) for text, start, end in words]
    if sentence_ends is None:
        segments = [Segment(text=" ".join(w[0] for w in words),
                            start=objects[0].start, end=objects[-1].end, words=objects)]
    else:
        segments = [Segment(text="s", start=0.0, end=end, words=objects) for end in sentence_ends]
    return Transcript(segments=segments)


# ── interval subtraction ─────────────────────────────────────────────────────


def test_a_hole_in_the_middle_splits_a_range():
    assert subtract([(0.0, 10.0)], [(4.0, 6.0)]) == [(0.0, 4.0), (6.0, 10.0)]


def test_a_hole_at_the_edge_trims_rather_than_splits():
    assert subtract([(0.0, 10.0)], [(0.0, 2.0)]) == [(2.0, 10.0)]
    assert subtract([(0.0, 10.0)], [(8.0, 10.0)]) == [(0.0, 8.0)]


def test_a_hole_covering_everything_removes_the_range():
    assert subtract([(2.0, 5.0)], [(0.0, 10.0)]) == []


def test_a_hole_outside_the_range_changes_nothing():
    assert subtract([(0.0, 5.0)], [(8.0, 9.0)]) == [(0.0, 5.0)]


def test_multiple_holes_apply_cumulatively():
    assert subtract([(0.0, 10.0)], [(2.0, 3.0), (6.0, 7.0)]) == [
        (0.0, 2.0), (3.0, 6.0), (7.0, 10.0)
    ]


# ── filler removal ───────────────────────────────────────────────────────────


def test_a_filler_is_excised_exactly():
    """This is the case a real clip produced: silence detection kept a fragment
    of the 'um' as its own clip, because the filler contains quiet moments."""
    words = transcript([("dog,", 2.16, 3.40), ("U.M.", 3.40, 4.60), ("This", 4.60, 4.81)])
    result = refine([(0.0, 6.2)], words, min_duration_s=0.1)

    assert result.fillers_removed == 1
    assert (3.40, 4.60) not in result.ranges
    assert any(end == pytest.approx(3.40) for _, end in result.ranges)
    assert any(start == pytest.approx(4.60) for start, _ in result.ranges)


def test_filler_removal_can_be_turned_off():
    words = transcript([("um", 1.0, 2.0), ("word", 2.0, 3.0)])
    result = refine([(0.0, 3.0)], words, remove_fillers=False, min_duration_s=0.1)

    assert result.fillers_removed == 0
    assert result.ranges == [(0.0, 3.0)]


def test_ordinary_words_are_never_excised():
    words = transcript([("umbrella", 1.0, 2.0), ("summary", 2.0, 3.0)])
    result = refine([(0.0, 3.0)], words, min_duration_s=0.1)
    assert result.fillers_removed == 0


# ── never cut inside a word ──────────────────────────────────────────────────


def test_a_cut_landing_mid_word_moves_outward_to_keep_the_word():
    """Truncating a word is the most obvious artefact of an automated edit. The
    choice is between keeping a whole word and losing one, and keeping wins."""
    words = transcript([("hello", 1.0, 2.0), ("there", 2.0, 3.0)])
    result = refine([(1.5, 2.5)], words, min_duration_s=0.1)

    assert result.ranges == [(1.0, 3.0)]
    assert result.words_rescued == 2


def test_a_cut_already_on_a_word_boundary_is_untouched():
    words = transcript([("hello", 1.0, 2.0), ("there", 2.0, 3.0)])
    result = refine([(1.0, 2.0)], words, min_duration_s=0.1)

    assert result.words_rescued == 0
    assert result.ranges == [(1.0, 2.0)]


def test_snapping_does_not_drag_a_boundary_back_over_a_removed_filler():
    """Order matters: excise first, then snap. Snapping against the filler would
    widen the range straight back over the word just removed."""
    words = transcript([("word", 0.0, 1.0), ("um", 1.0, 2.0), ("next", 2.0, 3.0)])
    result = refine([(0.0, 3.0)], words, min_duration_s=0.1)

    for start, end in result.ranges:
        assert not (start < 1.5 < end), "the filler must not be re-covered"


# ── sentence alignment ───────────────────────────────────────────────────────


def test_a_cut_near_a_sentence_end_is_moved_onto_it():
    """The realistic case: silence detection ran the range past the last word
    into trailing room tone, so the cut sits in a gap rather than inside a word.
    Pulling it back onto the sentence end makes the edit sound deliberate."""
    spoken = [Word("one", 0.0, 1.0), Word("two.", 1.0, 2.0)]
    words = Transcript(segments=[Segment(text="one two.", start=0.0, end=2.0, words=spoken)])

    result = refine([(0.0, 2.0 + SENTENCE_SNAP_S / 2)], words, min_duration_s=0.1)

    assert result.words_rescued == 0, "the cut was in a gap, not inside a word"
    assert result.sentences_snapped == 1
    assert result.ranges[0][1] == pytest.approx(2.0)


def test_a_distant_sentence_end_is_left_alone():
    """Dragging a cut across half a sentence would change the content, not tidy
    the boundary."""
    words = Transcript(
        segments=[Segment(text="one.", start=0.0, end=10.0, words=[Word("one.", 0.0, 10.0)])]
    )
    result = refine([(0.0, 3.0)], words, min_duration_s=0.1)
    assert result.sentences_snapped == 0


# ── no transcript ────────────────────────────────────────────────────────────


def test_without_a_transcript_nothing_changes():
    original = [(0.0, 2.0), (3.0, 5.0)]
    result = refine(original, None)

    assert result.ranges == original
    assert result.notes == []


def test_an_empty_transcript_changes_nothing():
    result = refine([(0.0, 2.0)], Transcript(segments=[]))
    assert result.ranges == [(0.0, 2.0)]


# ── housekeeping ─────────────────────────────────────────────────────────────


def test_overlaps_created_by_snapping_are_merged():
    assert merge_overlaps([(0.0, 2.0), (1.5, 3.0)]) == [(0.0, 3.0)]


def test_ranges_below_the_minimum_are_dropped():
    words = transcript([("a", 0.0, 0.1), ("b", 5.0, 6.0)])
    result = refine([(0.0, 0.1), (5.0, 6.0)], words, min_duration_s=0.4)

    assert (0.0, 0.1) not in result.ranges


def test_notes_describe_what_was_done():
    words = transcript([("um", 1.0, 2.0), ("word", 2.0, 3.0)])
    notes = " ".join(refine([(0.0, 3.0)], words, min_duration_s=0.1).notes)
    assert "filler" in notes
