"""
Planner and QC tests.

Driven by a generated file with a known speech/silence pattern — 2s tone, 2s
silence, 2s tone, 2s silence, 2s tone — so the expected cut is arithmetic rather
than opinion.
"""

import shutil
import subprocess

import pytest

from ave.media.ffmpeg import probe, summarise
from ave.plan.planner import PlanInputs, pad_and_merge, plan_cut
from ave.qc.validate import run_qc
from ave.style.models import default_dna


@pytest.fixture
def alternating(tmp_path):
    """10s: tone 0-2, silence 2-4, tone 4-6, silence 6-8, tone 8-10."""
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")
    out = tmp_path / "alternating.mp4"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30:duration=10",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=10",
            "-af", "volume=0:enable='between(t,2,4)+between(t,6,8)'",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
            str(out),
        ],
        check=True, capture_output=True,
    )
    return out


@pytest.fixture
def silent(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")
    out = tmp_path / "silent.mp4"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30:duration=4",
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
            str(out),
        ],
        check=True, capture_output=True,
    )
    return out


def make_inputs(path, **overrides):
    probed = probe(path)
    probed["summary"] = summarise(probed)
    return PlanInputs(
        media_id="m", path=str(path), probe=probed,
        dna=overrides.pop("dna", default_dna()), project="t", **overrides,
    )


# ── pad and merge ────────────────────────────────────────────────────────────


def test_padding_extends_both_sides():
    out = pad_and_merge([(2.0, 4.0)], lead_in=0.1, lead_out=0.2, duration=10, min_gap=0.0)
    assert out == [(1.9, 4.2)]


def test_padding_is_clamped_to_the_media():
    out = pad_and_merge([(0.0, 10.0)], lead_in=0.5, lead_out=0.5, duration=10, min_gap=0.0)
    assert out == [(0.0, 10.0)]


def test_ranges_that_padding_pushes_together_are_merged():
    """Without this, a short pause becomes two clips with nothing between them —
    a cut the viewer sees for no reason."""
    out = pad_and_merge(
        [(0.0, 2.0), (2.1, 4.0)], lead_in=0.1, lead_out=0.1, duration=10, min_gap=0.2
    )
    assert out == [(0.0, 4.1)]


def test_distant_ranges_stay_separate():
    out = pad_and_merge(
        [(0.0, 2.0), (6.0, 8.0)], lead_in=0.1, lead_out=0.1, duration=10, min_gap=0.2
    )
    assert len(out) == 2


# ── planning ─────────────────────────────────────────────────────────────────


def test_three_speech_segments_become_three_clips(alternating):
    edl = plan_cut(make_inputs(alternating))
    assert edl.summary.clip_count == 3
    assert 6.0 < edl.summary.output_duration_s < 7.5
    assert edl.summary.source_duration_s == pytest.approx(10.0, abs=0.1)


def test_the_timeline_has_no_gaps_or_overlaps(alternating):
    clips = sorted(plan_cut(make_inputs(alternating)).all_clips(),
                   key=lambda c: c.timeline_start_frames)
    cursor = 0
    for clip in clips:
        assert clip.timeline_start_frames == cursor, "clips must butt up exactly"
        cursor = clip.timeline_end_frames


def test_source_ranges_track_the_actual_speech(alternating):
    """The second clip must start near 4s, where the second tone begins."""
    edl = plan_cut(make_inputs(alternating))
    tb = edl.timebase
    starts = [tb.frames_to_seconds(c.source_in_frames) for c in edl.all_clips()]
    assert starts[0] == pytest.approx(0.0, abs=0.2)
    assert starts[1] == pytest.approx(3.95, abs=0.3)
    assert starts[2] == pytest.approx(7.95, abs=0.3)


def test_every_clip_explains_itself(alternating):
    for clip in plan_cut(make_inputs(alternating)).all_clips():
        assert "dead-air tolerance" in clip.reason


def test_markers_are_emitted_for_each_cut(alternating):
    edl = plan_cut(make_inputs(alternating))
    # One marker per cut, so one fewer than the number of clips.
    assert len(edl.markers) == edl.summary.clip_count - 1


def test_planning_is_deterministic(alternating):
    """Same media, same style, same seed must give the same plan — which is what
    makes regenerating a version reproducible."""
    a = plan_cut(make_inputs(alternating))
    b = plan_cut(make_inputs(alternating))

    assert a.inputs_hash == b.inputs_hash
    assert [(c.source_in_frames, c.source_out_frames) for c in a.all_clips()] == [
        (c.source_in_frames, c.source_out_frames) for c in b.all_clips()
    ]


def test_changing_the_style_changes_the_inputs_hash(alternating):
    dna = default_dna()
    dna.pacing.dead_air_tolerance_s = 1.5
    assert plan_cut(make_inputs(alternating)).inputs_hash != plan_cut(
        make_inputs(alternating, dna=dna)
    ).inputs_hash


def test_a_higher_dead_air_tolerance_keeps_more(alternating):
    """A 2s pause survives a 3s tolerance, so the whole file stays in one piece."""
    dna = default_dna()
    dna.pacing.dead_air_tolerance_s = 3.0
    edl = plan_cut(make_inputs(alternating, dna=dna))
    assert edl.summary.clip_count == 1


def test_tiny_islands_are_dropped(alternating):
    dna = default_dna()
    dna.pacing.min_clip_duration_s = 5.0  # longer than any segment here
    assert plan_cut(make_inputs(alternating, dna=dna)).summary.clip_count == 0


# ── diagnostics ──────────────────────────────────────────────────────────────


def test_a_silent_source_explains_itself(silent):
    """An empty plan is worthless without the reason it came out empty."""
    edl = plan_cut(make_inputs(silent))
    assert edl.summary.clip_count == 0
    assert edl.summary.diagnostics
    assert "silent" in edl.summary.diagnostics[0].lower()
    assert "LUFS" in edl.summary.diagnostics[0]


def test_qc_surfaces_the_diagnosis(silent):
    report = run_qc(plan_cut(make_inputs(silent)))
    assert not report.ok
    assert any("LUFS" in error for error in report.errors), (
        "'no clips' alone is not actionable"
    )


# ── quality control ──────────────────────────────────────────────────────────


def test_a_good_plan_is_clean(alternating):
    report = run_qc(plan_cut(make_inputs(alternating)))
    assert report.ok
    assert report.errors == []
    assert report.confidence > 0.9


def test_qc_detects_a_gap(alternating):
    edl = plan_cut(make_inputs(alternating))
    edl.tracks[0].clips[1].timeline_start_frames += 5  # punch a hole

    report = run_qc(edl)
    assert not report.ok
    assert any("gap" in error for error in report.errors)


def test_qc_detects_an_overlap(alternating):
    edl = plan_cut(make_inputs(alternating))
    edl.tracks[0].clips[1].timeline_start_frames -= 5

    report = run_qc(edl)
    assert not report.ok
    assert any("overlap" in error for error in report.errors)


def test_qc_warns_when_almost_nothing_was_cut(alternating):
    dna = default_dna()
    dna.pacing.dead_air_tolerance_s = 3.0
    report = run_qc(plan_cut(make_inputs(alternating, dna=dna)))
    assert any("kept" in warning for warning in report.warnings)
