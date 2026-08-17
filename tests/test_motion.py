"""
Motion estimation and punch-in generation.

The estimator is checked against synthetic clips built with a known transform, so
"is a 12% zoom measured as a 12% zoom" has an actual answer rather than a
plausible-looking number. Real footage cannot do that — there is no ground truth
in a video of a person talking.
"""

import numpy as np
import pytest

from ave.lib.rng import rng
from ave.media.motion import analyse_motion
from ave.plan.models import EDL, Clip, Summary, Timebase, Track
from ave.plan.planner import add_punch_ins
from ave.policies.validate import validate_edl
from ave.style.models import EditDNA, Motion, default_dna

FPS = 30
FRAMES = 150
PER_FRAME_ZOOM = 1.004  # ground truth: 12.72% per second


def _write_clip(path, per_frame_zoom=1.0, pan_px=0.0):
    """A rich static texture transformed by a known affine, frame by frame."""
    cv2 = pytest.importorskip("cv2")
    generator = np.random.default_rng(7)
    base = cv2.GaussianBlur(generator.integers(0, 255, (720, 1280), dtype=np.uint8), (5, 5), 0)

    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), FPS, (640, 360))
    for index in range(FRAMES):
        scale = per_frame_zoom**index
        matrix = np.float32([
            [scale, 0, (1 - scale) * base.shape[1] / 2 - pan_px * index],
            [0, scale, (1 - scale) * base.shape[0] / 2],
        ])
        warped = cv2.warpAffine(base, matrix, (base.shape[1], base.shape[0]))
        writer.write(cv2.cvtColor(cv2.resize(warped, (640, 360)), cv2.COLOR_GRAY2BGR))
    writer.release()
    return path


@pytest.fixture
def static_clip(tmp_path):
    return _write_clip(tmp_path / "static.mp4")


@pytest.fixture
def zoom_clip(tmp_path):
    return _write_clip(tmp_path / "zoom.mp4", per_frame_zoom=PER_FRAME_ZOOM)


@pytest.fixture
def pan_clip(tmp_path):
    return _write_clip(tmp_path / "pan.mp4", pan_px=1.5)


# ── estimation against ground truth ──────────────────────────────────────────


def test_a_still_clip_reports_no_motion(static_clip):
    profile = analyse_motion(static_clip)
    assert profile.measured
    assert profile.zoom_frequency == 0.0
    assert profile.pan_frequency == 0.0
    assert profile.static_ratio == 1.0


def test_a_zoom_is_measured_at_the_right_rate(zoom_clip):
    expected = PER_FRAME_ZOOM**FPS - 1  # per-second scale change
    profile = analyse_motion(zoom_clip)

    assert profile.zoom_frequency == 1.0
    assert profile.zoom_magnitude == pytest.approx(expected, rel=0.15)


def test_a_zoom_is_not_also_counted_as_a_pan(zoom_clip):
    """A punch-in is anchored at the frame centre, which puts a translation of
    (1-s)·centre into the affine even though nothing panned. Without subtracting
    it, every zoom inflates pan_frequency to 1.0."""
    assert analyse_motion(zoom_clip).pan_frequency == 0.0


def test_a_pan_is_measured_without_a_phantom_zoom(pan_clip):
    profile = analyse_motion(pan_clip)
    assert profile.pan_frequency == 1.0
    assert profile.zoom_frequency == 0.0


def test_untrackable_footage_is_reported_as_unmeasured(tmp_path):
    """A solid colour has no corners to track. Reporting 'no motion' would be a
    guess dressed as an observation."""
    cv2 = pytest.importorskip("cv2")
    path = tmp_path / "flat.mp4"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), FPS, (640, 360))
    for _ in range(FRAMES):
        writer.write(np.zeros((360, 640, 3), dtype=np.uint8))
    writer.release()

    assert not analyse_motion(path).measured


# ── punch-in generation ──────────────────────────────────────────────────────


def build_edl(tmp_path, clip_count=4):
    source = tmp_path / "src.mov"
    source.write_bytes(b"0")
    clips = [
        Clip(
            id=f"clip_{i}", source_media_id="m", source_path=str(source),
            source_in_frames=i * 200, source_out_frames=i * 200 + 90 + i,
            timeline_start_frames=i * 90,
        )
        for i in range(clip_count)
    ]
    return EDL(
        project="p",
        timebase=Timebase(fps_num=30, fps_den=1, width=1920, height=1080),
        tracks=[Track(name="V1", clips=clips)],
        summary=Summary(source_duration_s=60, output_duration_s=12, clip_count=clip_count),
    )


def punchy_dna(confidence=0.9, rate=20.0):
    dna = EditDNA(
        style_name="punchy",
        motion=Motion(punch_in_rate_per_minute=rate, zoom_range=(1.05, 1.25)),
        confidence={"motion": confidence},
    )
    return dna


def test_no_punch_ins_when_the_style_has_none(tmp_path):
    edl = build_edl(tmp_path)
    assert add_punch_ins(edl, default_dna(), seed=1) == 0
    assert edl.all_ops() == []


def test_punch_ins_are_generated_within_the_styles_zoom_range(tmp_path):
    edl = build_edl(tmp_path)
    added = add_punch_ins(edl, punchy_dna(), seed=1)

    assert added > 0
    for op in edl.all_ops():
        assert op.type == "zoom"
        assert 1.05 <= op.params["value"] <= 1.25


def test_generation_is_reproducible_from_the_seed(tmp_path):
    """Same seed must give the same zooms, or regenerating a version would
    silently produce a different edit."""
    first, second = build_edl(tmp_path), build_edl(tmp_path)
    add_punch_ins(first, punchy_dna(), seed=42)
    add_punch_ins(second, punchy_dna(), seed=42)

    assert [op.params["value"] for op in first.all_ops()] == [
        op.params["value"] for op in second.all_ops()
    ]


def test_a_different_seed_gives_a_different_edit(tmp_path):
    first, second = build_edl(tmp_path), build_edl(tmp_path)
    add_punch_ins(first, punchy_dna(), seed=1)
    add_punch_ins(second, punchy_dna(), seed=2)

    assert [op.params["value"] for op in first.all_ops()] != [
        op.params["value"] for op in second.all_ops()
    ]


def test_zooms_are_jittered_rather_than_identical(tmp_path):
    """A run of identical punch-ins reads as an automated effect, not an edit."""
    edl = build_edl(tmp_path, clip_count=6)
    add_punch_ins(edl, punchy_dna(rate=60.0), seed=3)
    values = [op.params["value"] for op in edl.all_ops()]

    assert len(set(values)) > 1


def test_longer_clips_are_preferred(tmp_path):
    """A punch-in needs room to read as a choice rather than a glitch."""
    edl = build_edl(tmp_path, clip_count=4)
    add_punch_ins(edl, punchy_dna(rate=5.0), seed=1)

    with_ops = [c for c in edl.all_clips() if c.ops]
    longest = max(edl.all_clips(), key=lambda c: c.duration_frames)
    assert with_ops and with_ops[0].id == longest.id


# ── confidence inherited from the measurement ────────────────────────────────


def test_a_well_measured_style_applies_its_punch_ins(tmp_path):
    edl = build_edl(tmp_path)
    add_punch_ins(edl, punchy_dna(confidence=0.9), seed=1)
    validate_edl(edl, punchy_dna(confidence=0.9))

    assert all(op.decision == "ALLOW" for op in edl.all_ops())


def test_a_poorly_measured_style_queues_them_instead(tmp_path):
    """The safety here is emergent rather than special-cased: operations inherit
    the confidence of the measurement behind them, and the validation gate
    already refuses to apply anything under the floor unreviewed."""
    dna = punchy_dna(confidence=0.3)
    edl = build_edl(tmp_path)
    add_punch_ins(edl, dna, seed=1)
    validate_edl(edl, dna)

    ops = edl.all_ops()
    assert ops and all(op.decision == "REQUIRE_APPROVAL" for op in ops)
    assert edl.pending_approval() == ops


def test_seeded_rng_underpins_all_of_this():
    assert [rng(5).random() for _ in range(4)] == [rng(5).random() for _ in range(4)]
