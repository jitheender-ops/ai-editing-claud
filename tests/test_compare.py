"""
Style similarity tests.

The behaviour that matters most is the refusal to score. A similarity number gets
quoted without its caveats, so two *unmeasured* sections — which are identical,
because both hold the same defaults — must never be reported as a perfect match.
"""

import pytest

from ave.style.compare import COMPARABLE_CONFIDENCE, compare
from ave.style.models import Audio, Color, EditDNA, Motion, Pacing


def dna(name, *, asd=2.0, cpm=30.0, dead_air=0.35, lufs=-14.0, confidence=None):
    return EditDNA(
        style_name=name,
        pacing=Pacing(
            average_shot_duration_s=asd, median_shot_duration_s=asd,
            cuts_per_minute=cpm, dead_air_tolerance_s=dead_air,
        ),
        motion=Motion(punch_in_rate_per_minute=5.0, zoom_range=(1.0, 1.15)),
        audio=Audio(integrated_lufs=lufs),
        color=Color(saturation_mean=0.4, contrast=0.5, temperature_bias=0.0),
        confidence=confidence
        if confidence is not None
        else {"pacing": 0.9, "motion": 0.9, "audio": 0.9, "color": 0.9, "captions": 0.9},
    )


# ── refusing to score ────────────────────────────────────────────────────────


def test_two_unmeasured_styles_are_not_reported_as_identical():
    """Both hold the same defaults, so every field matches — which means nothing."""
    report = compare(dna("a", confidence={}), dna("b", confidence={}))

    assert report.overall is None
    assert report.comparable_sections == []
    assert "not comparable" in report.render()


def test_a_section_measured_on_only_one_side_is_not_comparable():
    left = dna("measured")
    right = dna("partial", confidence={"pacing": 0.9})

    report = compare(left, right)
    by_name = {s.name: s for s in report.sections}

    assert by_name["pacing"].comparable
    assert not by_name["motion"].comparable
    assert "partial" in by_name["motion"].reason


def test_unmeasured_sections_do_not_drag_the_overall_score():
    """They are excluded, not scored as zero — an unmeasured section is unknown,
    not different."""
    full = compare(dna("a"), dna("b")).overall
    partial = compare(
        dna("a", confidence={"pacing": 0.9}), dna("b", confidence={"pacing": 0.9})
    ).overall
    assert full == partial == 1.0


def test_the_confidence_threshold_is_respected():
    weak = COMPARABLE_CONFIDENCE / 2
    report = compare(dna("a", confidence={"pacing": weak}), dna("b", confidence={"pacing": weak}))
    assert report.overall is None


# ── scoring ──────────────────────────────────────────────────────────────────


def test_identical_styles_score_perfectly():
    assert compare(dna("a"), dna("b")).overall == 1.0


def test_different_pacing_scores_lower():
    fast = dna("fast", asd=0.5, cpm=110)
    slow = dna("slow", asd=4.0, cpm=15)

    pacing = next(s for s in compare(fast, slow).sections if s.name == "pacing")
    assert pacing.score < 0.5


def test_a_difference_beyond_the_scale_floors_at_zero_not_negative():
    left = dna("a", cpm=0)
    right = dna("b", cpm=500)
    pacing = next(s for s in compare(left, right).sections if s.name == "pacing")
    assert 0.0 <= pacing.score <= 1.0


def test_similarity_is_symmetric():
    a, b = dna("a", asd=1.0), dna("b", asd=3.0)
    assert compare(a, b).overall == compare(b, a).overall


def test_per_field_detail_is_available():
    pacing = next(s for s in compare(dna("a"), dna("b", asd=3.0)).sections if s.name == "pacing")
    assert any("average shot" in line for line in pacing.detail)


# ── no double counting ───────────────────────────────────────────────────────


def test_transitions_are_not_scored_because_they_duplicate_pacing():
    """The analyser's transition density *is* cut density under another name.
    Scoring it would count one signal twice and pull the overall toward pacing."""
    transitions = next(s for s in compare(dna("a"), dna("b")).sections if s.name == "transitions")

    assert not transitions.comparable
    assert "pacing already covers" in transitions.reason


def test_pacing_alone_does_not_dominate_the_overall():
    """With transitions excluded, a big pacing difference moves the overall by
    roughly its share of the comparable sections and no more."""
    report = compare(dna("a", asd=0.5, cpm=110), dna("b", asd=4.0, cpm=15))
    comparable = report.comparable_sections

    assert "transitions" not in [s.name for s in comparable]
    assert report.overall > 0.5, "audio and colour still agree, so it cannot floor"


def test_render_is_readable():
    text = compare(dna("a"), dna("b", asd=3.0)).render()
    assert "Overall:" in text
    assert "pacing" in text
