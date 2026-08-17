"""
Reference analyser tests.

Built on videos with a known cut pattern, so the measurements have a ground
truth to be checked against rather than merely being self-consistent.

The other half of what is tested here is honesty: a profile must report low
confidence on a thin sample, and must say in `notes` what it did not measure. A
silently-confident wrong style is worse than no style, because the planner would
apply it with full conviction.
"""

import shutil
import subprocess

import pytest

from ave.media.ffmpeg import probe, summarise
from ave.media.scenes import detect_shots, shot_durations
from ave.reference.analyze import _pacing_curve, analyse_reference

#: Six visually distinct shots. Durations sum to 10s, mean 10/6 = 1.667.
SHOTS = [("red", 1.0), ("blue", 2.0), ("green", 1.5), ("yellow", 3.0), ("black", 1.0), ("white", 1.5)]


def _require_ffmpeg():
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")


@pytest.fixture
def cut_reference(tmp_path):
    """A video whose shot boundaries and durations are known exactly."""
    _require_ffmpeg()
    listing = tmp_path / "list.txt"
    lines = []
    for index, (colour, duration) in enumerate(SHOTS):
        segment = tmp_path / f"seg{index}.mp4"
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", f"color=c={colour}:s=320x180:r=30:d={duration}",
                "-f", "lavfi", "-i", f"sine=frequency={300 + index * 90}:duration={duration}",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
                str(segment),
            ],
            check=True, capture_output=True,
        )
        lines.append(f"file '{segment.name}'")
    listing.write_text("\n".join(lines) + "\n")

    out = tmp_path / "ref.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(out)],
        check=True, capture_output=True, cwd=tmp_path,
    )
    return out


@pytest.fixture
def paused_reference(tmp_path):
    """Continuous picture, speech with pauses of 0.3s, 0.5s and 0.8s — so a
    dead-air tolerance can actually be inferred."""
    _require_ffmpeg()
    out = tmp_path / "paused.mp4"
    mutes = "between(t,2,2.3)+between(t,4,4.5)+between(t,6,6.8)+between(t,8,8.4)"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30:duration=10",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=10",
            "-af", f"volume=0:enable='{mutes}'",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
            str(out),
        ],
        check=True, capture_output=True,
    )
    return out


def analyse(path, **kwargs):
    probed = probe(path)
    probed["summary"] = summarise(probed)
    return analyse_reference(
        source_path=path, proxy_path=None, probe=probed,
        style_name=kwargs.pop("style_name", "test"), **kwargs,
    )


# ── shot detection ───────────────────────────────────────────────────────────


def test_shot_boundaries_match_ground_truth(cut_reference):
    shots = detect_shots(cut_reference)
    assert len(shots) == len(SHOTS)
    measured = [round(d, 1) for d in shot_durations(shots)]
    assert measured == [duration for _, duration in SHOTS]


def test_a_single_shot_video_returns_one_shot(tmp_path):
    """Not an empty list — that would be indistinguishable from a failure."""
    _require_ffmpeg()
    out = tmp_path / "one.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "color=c=red:s=320x180:r=30:d=3",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out)],
        check=True, capture_output=True,
    )
    assert len(detect_shots(out)) == 1


# ── pacing ───────────────────────────────────────────────────────────────────


def test_pacing_statistics_are_correct(cut_reference):
    pacing = analyse(cut_reference).pacing
    assert pacing.average_shot_duration_s == pytest.approx(10 / 6, abs=0.05)
    assert pacing.median_shot_duration_s == pytest.approx(1.5, abs=0.05)
    assert pacing.cuts_per_minute == pytest.approx(36, abs=1)


def test_confidence_is_low_on_a_thin_sample(cut_reference):
    """Six shots is not a distribution, and the profile must say so rather than
    presenting the mean with full conviction."""
    dna = analyse(cut_reference)
    assert dna.confidence_of("pacing") < 0.3
    assert any("6 shots" in note for note in dna.notes)


def test_pacing_curve_is_relative_to_the_average():
    """1.0 is average density; higher means faster cutting in that stretch."""
    curve = _pacing_curve([1.0] * 10)
    assert all(value == pytest.approx(1.0) for value in curve)

    faster_at_the_start = _pacing_curve([0.5] * 5 + [2.0] * 5, buckets=2)
    assert faster_at_the_start[0] > faster_at_the_start[1]


def test_pacing_curve_of_nothing_is_empty():
    assert _pacing_curve([]) == []


# ── dead-air tolerance ───────────────────────────────────────────────────────


def test_dead_air_tolerance_is_inferred_from_pauses_that_survived(paused_reference):
    """Any pause longer than the editor's tolerance would have been cut, so the
    longest pauses still present sit just under it."""
    pacing = analyse(paused_reference).pacing
    # Pauses present are 0.3, 0.4, 0.5, 0.8 — the 90th percentile lands near the top.
    assert 0.3 <= pacing.dead_air_tolerance_s <= 1.0


def test_a_reference_without_pauses_says_so_instead_of_guessing(cut_reference):
    dna = analyse(cut_reference)
    assert any("dead-air tolerance" in note for note in dna.notes)


# ── honesty ──────────────────────────────────────────────────────────────────


def test_unmeasurable_characteristics_are_declared(cut_reference):
    dna = analyse(cut_reference)
    joined = " ".join(dna.notes).lower()
    assert "font" in joined, "font family is not recoverable and must be declared"
    assert "motion" in joined
    assert "transition" in joined


def test_motion_confidence_is_zero_because_it_is_not_measured(cut_reference):
    assert analyse(cut_reference).confidence_of("motion") == 0.0


def test_an_unmeasured_section_reports_zero_confidence(cut_reference):
    dna = analyse(cut_reference)
    assert dna.confidence_of("captions") == 0.0, "absent means unmeasured, not perfect"


def test_colour_is_measured(cut_reference):
    colour = analyse(cut_reference).color
    assert 0.0 <= colour.saturation_mean <= 1.0


def test_loudness_is_measured(cut_reference):
    dna = analyse(cut_reference)
    assert -40 < dna.audio.integrated_lufs < 0
    assert dna.confidence_of("audio") > 0.5


def test_provenance_is_recorded(cut_reference):
    dna = analyse(cut_reference, content_hash="abc123")
    assert dna.derived_from == ["abc123"]


def test_the_dna_round_trips_through_json(cut_reference):
    """It is stored as JSON in the styles table, so this is the real contract."""
    from ave.style.models import EditDNA

    dna = analyse(cut_reference)
    assert EditDNA.model_validate(dna.model_dump(mode="json")) == dna
