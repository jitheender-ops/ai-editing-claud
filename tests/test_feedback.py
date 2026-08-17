"""
Feedback tests.

Two behaviours carry the feature. A patch must leave the cuts alone — iterating
should feel like adjusting, not rerolling. And an instruction that is not
understood must be reported as such, because guessing at an unclear request and
doing something adjacent is worse than saying so.
"""

import pytest

from ave.plan.feedback import apply_feedback
from ave.plan.models import EDL, Clip, Op, Summary, Timebase, Track
from ave.style.models import default_dna


def build_edl(tmp_path, zooms=(1.20,)):
    source = tmp_path / "src.mov"
    source.write_bytes(b"0")
    clips = [
        Clip(
            id=f"clip_{i}", source_media_id="m", source_path=str(source),
            source_in_frames=0, source_out_frames=90, timeline_start_frames=i * 90,
            ops=[Op(id=f"op_{i}", type="zoom", params={"value": value})],
        )
        for i, value in enumerate(zooms)
    ]
    return EDL(
        project="p", dna=default_dna(),
        timebase=Timebase(fps_num=30, fps_den=1, width=1920, height=1080),
        tracks=[Track(name="V1", clips=clips)],
        summary=Summary(source_duration_s=10, output_duration_s=3, clip_count=len(clips)),
    )


# ── zoom scaling ─────────────────────────────────────────────────────────────


def test_reducing_zooms_scales_the_travel_not_the_raw_value(tmp_path):
    """A zoom of 1.20 reduced by 50% is 1.10 — the travel is the 0.20 above an
    untouched frame. Halving the value itself would give 0.60, turning every
    punch-in into a zoom *out*: a bug that looks perfectly correct in a diff."""
    edl = build_edl(tmp_path, zooms=(1.20,))
    result = apply_feedback("reduce zooms by 50%", edl, default_dna())

    assert result.ok
    assert edl.all_ops()[0].params["value"] == pytest.approx(1.10)


def test_a_zoom_can_never_be_inverted_by_a_reduction(tmp_path):
    edl = build_edl(tmp_path, zooms=(1.05, 1.40))
    apply_feedback("reduce zooms by 90%", edl, default_dna())
    assert all(op.params["value"] >= 1.0 for op in edl.all_ops())


def test_increasing_zooms(tmp_path):
    edl = build_edl(tmp_path, zooms=(1.10,))
    apply_feedback("increase zooms by 50%", edl, default_dna())
    assert edl.all_ops()[0].params["value"] == pytest.approx(1.15)


def test_the_percentage_is_read_from_the_command(tmp_path):
    edl = build_edl(tmp_path, zooms=(1.20,))
    apply_feedback("reduce zooms by 25%", edl, default_dna())
    assert edl.all_ops()[0].params["value"] == pytest.approx(1.15)


def test_a_reduction_without_a_percentage_defaults_to_half(tmp_path):
    edl = build_edl(tmp_path, zooms=(1.20,))
    apply_feedback("less zoom please", edl, default_dna())
    assert edl.all_ops()[0].params["value"] == pytest.approx(1.10)


def test_removing_zooms(tmp_path):
    edl = build_edl(tmp_path, zooms=(1.2, 1.3))
    result = apply_feedback("remove the zooms", edl, default_dna())

    assert edl.all_ops() == []
    assert result.changes[0].touched_ops == 2


# ── patch versus replan ──────────────────────────────────────────────────────


def test_a_zoom_change_is_a_patch_that_preserves_the_cuts(tmp_path):
    edl = build_edl(tmp_path, zooms=(1.2, 1.3))
    before = [c.id for c in edl.all_clips()]

    result = apply_feedback("reduce zooms by 50%", edl, default_dna())

    assert result.changes[0].kind == "patch"
    assert not result.needs_replan
    assert [c.id for c in edl.all_clips()] == before, "a patch must not disturb the cuts"


def test_a_pacing_change_requires_a_replan(tmp_path):
    """Where the cuts fall *is* the pacing, so this one cannot be a patch."""
    result = apply_feedback("make it faster", build_edl(tmp_path), default_dna())
    assert result.needs_replan
    assert result.changes[0].kind == "replan"


def test_faster_tightens_the_dead_air_tolerance(tmp_path):
    dna = default_dna()
    before = dna.pacing.dead_air_tolerance_s
    result = apply_feedback("make it faster", build_edl(tmp_path), dna)

    assert result.changes[0].dna.pacing.dead_air_tolerance_s < before


def test_slower_loosens_it(tmp_path):
    dna = default_dna()
    before = dna.pacing.dead_air_tolerance_s
    result = apply_feedback("make it slower", build_edl(tmp_path), dna)

    assert result.changes[0].dna.pacing.dead_air_tolerance_s > before


def test_feedback_never_mutates_the_original_style(tmp_path):
    """The stored plan's style must stay exactly as it was, or earlier versions
    stop being reproducible."""
    dna = default_dna()
    before = dna.pacing.dead_air_tolerance_s
    apply_feedback("make it faster", build_edl(tmp_path), dna)
    assert dna.pacing.dead_air_tolerance_s == before


def test_punchier_raises_the_punch_in_rate(tmp_path):
    result = apply_feedback("make it punchier", build_edl(tmp_path), default_dna())
    assert result.changes[0].dna.motion.punch_in_rate_per_minute > 0


# ── unrecognised input ───────────────────────────────────────────────────────


@pytest.mark.parametrize("command", ["", "   ", "add a unicorn", "colour grade it teal and orange"])
def test_an_unclear_instruction_is_reported_rather_than_guessed(tmp_path, command):
    edl = build_edl(tmp_path, zooms=(1.2,))
    result = apply_feedback(command, edl, default_dna())

    assert not result.ok
    assert result.unrecognised == [command]
    assert edl.all_ops()[0].params["value"] == 1.2, "nothing may change on a miss"


def test_remove_wins_over_reduce_when_both_could_match(tmp_path):
    """Rule order matters: 'remove' is more specific than 'less/reduce'."""
    edl = build_edl(tmp_path, zooms=(1.2,))
    apply_feedback("remove all the zooms", edl, default_dna())
    assert edl.all_ops() == []
