"""
Reference analysis — turning a video into an Edit DNA.

This is the headline claim of the whole system, so it is also where honesty
matters most. Some editing characteristics are genuinely measurable from a
rendered video; others are not recoverable at any effort, and a profile that
guesses at those is worse than one that admits the gap, because the planner would
then apply the guess with full conviction.

What is measured here, and how:

  pacing               shot boundaries -> durations, cuts per minute, and the
                       distribution. Direct and reliable.
  dead-air tolerance   the neat one. Any pause longer than an editor's tolerance
                       would have been cut, so the *longest silence still present
                       in their finished video* is very close to their tolerance.
                       Taken at the 90th percentile to shrug off outliers.
  loudness             EBU R128, exact by definition.
  colour               saturation, contrast and warmth from sampled proxy frames.
  motion               a coarse index only — see below.

What is *not* measured, and is recorded as such in `notes`: font family (not
recoverable from pixels at any effort), SFX identity (an onset can be classified
by shape but never identified), caption animation style, and the decomposition of
motion into zoom versus pan, which needs the affine estimation that lands with
the rest of M3.

Everything runs on the 480p proxy. Nothing here needs full resolution.
"""

from __future__ import annotations

import statistics
from pathlib import Path
from typing import Any

from ave.media.ffmpeg import summarise
from ave.media.scenes import detect_shots, shot_durations
from ave.media.silence import detect_silence, measure_loudness
from ave.style.models import Audio, Color, EditDNA, Motion, Pacing, Transitions

ANALYZER_VERSION = "1.0"

#: Below this many shots, pacing statistics are describing noise.
MIN_SHOTS_FOR_CONFIDENCE = 8


def analyse_reference(
    *,
    source_path: Path | str,
    proxy_path: Path | str | None,
    probe: dict[str, Any],
    style_name: str,
    content_hash: str = "",
    threshold: float = 27.0,
) -> EditDNA:
    info = probe.get("summary") or summarise(probe)
    duration = info["duration_s"] or 0.0
    # Shots come off the proxy (cheap); audio comes off the original (accurate).
    visual = Path(proxy_path) if proxy_path else Path(source_path)

    shots = detect_shots(visual, threshold=threshold)
    durations = shot_durations(shots)
    notes: list[str] = []
    confidence: dict[str, float] = {}

    pacing, pacing_confidence = _measure_pacing(durations, duration, source_path, notes)
    confidence["pacing"] = pacing_confidence

    audio, audio_confidence = _measure_audio(source_path, notes)
    confidence["audio"] = audio_confidence

    colour, colour_confidence = _measure_colour(visual, notes)
    confidence["color"] = colour_confidence

    motion, motion_confidence = _measure_motion(visual, duration, notes)
    confidence["motion"] = motion_confidence
    notes.append(
        "Caption style, font and SFX identity are not measured. Font family is not "
        "recoverable from a rendered video at any effort; an SFX can be classified by "
        "onset shape but never identified."
    )

    transitions, transitions_confidence = _measure_transitions(durations, duration, notes)
    confidence["transitions"] = transitions_confidence

    return EditDNA(
        style_name=style_name,
        derived_from=[content_hash] if content_hash else [],
        pacing=pacing,
        motion=motion,
        audio=audio,
        color=colour,
        transitions=transitions,
        confidence=confidence,
        notes=notes,
    )


def _measure_pacing(
    durations: list[float], total_s: float, source_path: Path | str, notes: list[str]
) -> tuple[Pacing, float]:
    if not durations:
        notes.append("No shot boundaries were detected; pacing is a default, not a measurement.")
        return Pacing(), 0.0

    ordered = sorted(durations)
    pacing = Pacing(
        average_shot_duration_s=round(statistics.fmean(durations), 3),
        median_shot_duration_s=round(statistics.median(durations), 3),
        cuts_per_minute=round(len(durations) / (total_s / 60), 2) if total_s else 0.0,
        dead_air_tolerance_s=_measure_dead_air_tolerance(source_path, notes),
        pacing_curve=_pacing_curve(durations),
    )
    # Confidence grows with sample size and saturates: eight shots is thin,
    # forty is a real distribution.
    confidence = min(1.0, len(durations) / (MIN_SHOTS_FOR_CONFIDENCE * 5))
    if len(durations) < MIN_SHOTS_FOR_CONFIDENCE:
        notes.append(
            f"Only {len(durations)} shots detected — pacing statistics are indicative at best."
        )
    _ = ordered
    return pacing, round(confidence, 2)


def _measure_dead_air_tolerance(source_path: Path | str, notes: list[str]) -> float:
    """Infer how much silence this editor is willing to leave in.

    Any pause longer than their tolerance would have been cut, so the silences
    still present in the finished video are all *within* it, and the longest of
    them sits just under the threshold. The 90th percentile is used rather than
    the maximum so one held dramatic beat does not set the whole style.
    """
    try:
        silences = detect_silence(source_path, noise_db=-30.0, min_duration_s=0.12)
    except Exception:  # noqa: BLE001 — a reference without usable audio is not fatal
        notes.append("Silence could not be measured; dead-air tolerance is a default.")
        return Pacing().dead_air_tolerance_s

    lengths = sorted(end - start for start, end in silences if end != float("inf"))
    if len(lengths) < 3:
        notes.append(
            "Too few pauses to infer a dead-air tolerance; using the default. "
            "A reference with continuous narration gives this measurement nothing to work with."
        )
        return Pacing().dead_air_tolerance_s

    index = max(0, int(len(lengths) * 0.9) - 1)
    return round(lengths[index], 3)


def _pacing_curve(durations: list[float], buckets: int = 10) -> list[float]:
    """Relative cut density per tenth of the runtime.

    1.0 means average density; a hook-heavy edit shows a high first bucket.
    """
    if not durations:
        return []
    per_bucket = max(1, len(durations) // buckets)
    curve: list[float] = []
    for index in range(buckets):
        chunk = durations[index * per_bucket : (index + 1) * per_bucket]
        if not chunk:
            break
        mean = statistics.fmean(chunk)
        overall = statistics.fmean(durations)
        curve.append(round(overall / mean, 3) if mean else 0.0)
    return curve


def _measure_audio(source_path: Path | str, notes: list[str]) -> tuple[Audio, float]:
    try:
        loudness = measure_loudness(source_path)
    except Exception:  # noqa: BLE001
        notes.append("Loudness could not be measured.")
        return Audio(), 0.0

    integrated = loudness.get("integrated_lufs")
    if integrated is None:
        return Audio(), 0.0
    if integrated < -60:
        notes.append(
            f"Reference audio is effectively silent ({integrated:.0f} LUFS); "
            f"no audio characteristics could be derived from it."
        )
        return Audio(integrated_lufs=integrated), 0.1

    notes.append(
        "Music presence and SFX density are not measured — separating music from "
        "speech needs source separation, which is not implemented."
    )
    return Audio(integrated_lufs=round(integrated, 1)), 0.8


def _measure_colour(visual: Path, notes: list[str], samples: int = 24) -> tuple[Color, float]:
    """Saturation, contrast and warmth from evenly sampled frames."""
    import cv2  # imported lazily: opencv is slow to load and not always needed
    import numpy as np

    capture = cv2.VideoCapture(str(visual))
    if not capture.isOpened():
        notes.append("Colour could not be measured; the proxy would not open.")
        return Color(), 0.0

    total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
    if total <= 0:
        capture.release()
        return Color(), 0.0

    saturations, contrasts, warmths = [], [], []
    for index in range(samples):
        capture.set(cv2.CAP_PROP_POS_FRAMES, int(total * index / samples))
        ok, frame = capture.read()
        if not ok:
            continue
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        saturations.append(float(hsv[:, :, 1].mean()) / 255.0)
        grey = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        contrasts.append(float(grey.std()) / 128.0)
        blue, _, red = (float(frame[:, :, c].mean()) for c in range(3))
        # Positive is warm. Normalised so the scale is readable rather than raw levels.
        warmths.append((red - blue) / 255.0)
    capture.release()

    if not saturations:
        return Color(), 0.0

    return (
        Color(
            saturation_mean=round(float(np.mean(saturations)), 3),
            contrast=round(float(np.mean(contrasts)), 3),
            temperature_bias=round(float(np.mean(warmths)), 3),
        ),
        0.7,
    )


def _measure_motion(visual: Path, duration_s: float, notes: list[str]) -> tuple[Motion, float]:
    """Zoom and pan behaviour, by affine estimation between sampled frames."""
    from ave.media.motion import ZOOM_EPSILON, analyse_motion

    profile = analyse_motion(visual)
    if not profile.measured:
        notes.append(
            "Motion could not be measured — too few frames could be tracked. Flat or "
            "featureless footage (slides, solid backgrounds) genuinely has nothing to "
            "track, so zoom_range is a default rather than an observation."
        )
        return Motion(), 0.0

    # Contiguous runs of zooming samples are one punch-in each, not one per frame.
    events = 0
    inside = False
    for sample in profile.samples:
        zooming = abs(sample.scale - 1.0) > ZOOM_EPSILON
        if zooming and not inside:
            events += 1
        inside = zooming

    minutes = (duration_s / 60) if duration_s else 0.0
    seconds_per_event = (duration_s * profile.zoom_frequency / events) if events else 0.0
    # A punch-in's total travel is its rate times how long it runs. Clamped: an
    # estimate outside this range is telling us about tracking noise, not style.
    travel = min(0.4, max(0.02, profile.zoom_magnitude * seconds_per_event)) if events else 0.0

    motion = Motion(
        punch_in_rate_per_minute=round(events / minutes, 2) if minutes else 0.0,
        zoom_range=(1.0, round(1.0 + travel, 3)) if events else (1.0, 1.0),
        pan_rate_per_minute=round(profile.pan_frequency * len(profile.samples) / minutes, 2)
        if minutes
        else 0.0,
        static_ratio=profile.static_ratio,
    )
    if not events:
        notes.append("No zooms or punch-ins were detected in this reference.")

    # Coverage is how much of the footage the estimate actually rests on.
    return motion, round(min(1.0, profile.coverage * min(1.0, len(profile.samples) / 40)), 2)


def _measure_transitions(
    durations: list[float], total_s: float, notes: list[str]
) -> tuple[Transitions, float]:
    """Only cut *density* is measured, never cut type.

    Distinguishing a dissolve from a hard cut needs the gradual-histogram analysis
    that is not implemented, so claiming a style is 'minimal' here would be an
    assumption wearing a measurement's clothes.
    """
    notes.append(
        "Transition *types* are not detected — dissolves, wipes and whips are not "
        "distinguished from hard cuts. Only cut density is measured."
    )
    density = round(len(durations) / (total_s / 60), 2) if total_s else 0.0
    return Transitions(style="minimal", density_per_minute=density), 0.2
