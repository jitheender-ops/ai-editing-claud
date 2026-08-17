"""
Silence and loudness, straight from ffmpeg.

`silencedetect` already solves dead-air detection exactly and ships in the ffmpeg
that is already installed, so there is no signal processing to write here — only
parsing. `ebur128` likewise gives broadcast-standard loudness for free.

The one judgement call is the noise floor. -30 dBFS suits clean voice; a noisy
room or a screen recording with fan hum needs it lower, and that is exposed
rather than buried because no single value is right for all footage.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from ave.media.ffmpeg import MediaError, require_ffmpeg

Range = tuple[float, float]

_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
_END = re.compile(r"silence_end:\s*(-?[\d.]+)")
_LOUDNESS = re.compile(r"I:\s*(-?[\d.]+)\s*LUFS")
_RANGE = re.compile(r"LRA:\s*(-?[\d.]+)\s*LU")


def detect_silence(
    path: Path | str, *, noise_db: float = -30.0, min_duration_s: float = 0.35
) -> list[Range]:
    """Silent ranges as (start, end) seconds.

    `min_duration_s` is the style's dead-air tolerance: silence shorter than this
    is a natural beat and must survive, silence longer is dead air.
    """
    require_ffmpeg()
    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
            "-af", f"silencedetect=noise={noise_db}dB:d={min_duration_s}",
            "-f", "null", "-",
        ],
        capture_output=True,
        text=True,
    )
    # ffmpeg writes filter output to stderr and exits 0; a non-zero exit means the
    # file could not be decoded at all.
    if result.returncode != 0:
        raise MediaError(
            f"silence detection failed for {path}: {result.stderr.strip()[:300]}",
            code="SILENCE_FAILED",
        )

    starts = [float(m) for m in _START.findall(result.stderr)]
    ends = [float(m) for m in _END.findall(result.stderr)]

    # A file ending mid-silence reports a start with no matching end. Pairing by
    # index and truncating would silently drop that range, so it is closed by the
    # caller instead, which knows the duration.
    ranges = [(s, e) for s, e in zip(starts, ends)]
    if len(starts) > len(ends):
        ranges.append((starts[-1], float("inf")))
    return ranges


def speech_ranges(silences: list[Range], duration_s: float) -> list[Range]:
    """Invert silence into the ranges worth keeping."""
    kept: list[Range] = []
    cursor = 0.0
    for start, end in sorted(silences):
        start = min(start, duration_s)
        if start > cursor:
            kept.append((cursor, start))
        cursor = max(cursor, min(end, duration_s) if end != float("inf") else duration_s)
    if cursor < duration_s:
        kept.append((cursor, duration_s))
    return kept


def measure_loudness(path: Path | str) -> dict[str, float | None]:
    """Integrated loudness (LUFS) and loudness range (LU), via EBU R128."""
    require_ffmpeg()
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
         "-af", "ebur128", "-f", "null", "-"],
        capture_output=True,
        text=True,
    )
    integrated = _LOUDNESS.findall(result.stderr)
    lra = _RANGE.findall(result.stderr)
    return {
        "integrated_lufs": float(integrated[-1]) if integrated else None,
        "loudness_range_lu": float(lra[-1]) if lra else None,
    }
