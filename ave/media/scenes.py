"""
Shot boundary detection.

PySceneDetect's ContentDetector already solves this well, so there is no
detection algorithm here — only the decision to run it on the 480p proxy rather
than the original. That is the thermal budget: content detection compares whole
decoded frames, and doing it at 1440p on a fanless Air is exactly the sustained
load that throttles the machine, for a boundary list that comes out the same.

Shot boundaries are the raw material for every pacing number in the Edit DNA, so
a systematic error here biases the whole style profile. The threshold is exposed
for that reason: 27 is PySceneDetect's default and suits ordinary cuts, but
high-contrast content over-triggers and needs it raised.
"""

from __future__ import annotations

from pathlib import Path

Shot = tuple[float, float]

DETECTOR_VERSION = "scenedetect-0.7-content"


def detect_shots(path: Path | str, *, threshold: float = 27.0, min_shot_s: float = 0.2) -> list[Shot]:
    """Shot boundaries as (start, end) seconds.

    PySceneDetect reports scene *changes*, so an uncut video comes back as an
    empty list. That is the wrong answer for our purposes and a dangerous one: a
    talking head, an interview or a screen recording is a single unbroken shot,
    and reporting zero shots would silently produce a pacing profile made
    entirely of defaults. One shot spanning the whole runtime is the truth, so
    that is what an empty result is converted into.
    """
    from scenedetect import ContentDetector, detect, open_video

    scenes = detect(str(path), ContentDetector(threshold=threshold, min_scene_len=1))
    shots = [(start.seconds, end.seconds) for start, end in scenes]

    if not shots:
        video = open_video(str(path))
        duration = video.duration.seconds if video.duration else 0.0
        return [(0.0, duration)] if duration >= min_shot_s else []

    # Sub-frame slivers are detector artefacts around hard flashes, not shots.
    return [(s, e) for s, e in shots if e - s >= min_shot_s]


def shot_durations(shots: list[Shot]) -> list[float]:
    return [end - start for start, end in shots]
