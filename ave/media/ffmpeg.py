"""
ffmpeg and ffprobe wrappers.

Two rules hold the thermal budget on a fanless Air:

  * Proxies are encoded with `h264_videotoolbox`, the hardware encoder. It costs
    almost no CPU, which is the difference between a proxy pass you can run on
    battery and one that throttles the machine.
  * Everything downstream analyses the 480p proxy, never the original. At 480p
    and 4 sampled frames per second that is roughly two orders of magnitude less
    decode than full-rate full-resolution, and it is the single biggest saving in
    the whole pipeline.

Proxy filenames are keyed on the content hash, so a re-ingest of the same media
finds its proxy already built no matter where the file has been moved to.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ave import config


class MediaError(Exception):
    """Carries an error code so callers branch on `code`, not on prose."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


def require_ffmpeg() -> None:
    for binary in ("ffmpeg", "ffprobe"):
        if not shutil.which(binary):
            raise MediaError(f"{binary} not found on PATH", code="FFMPEG_MISSING")


def probe(path: Path | str) -> dict[str, Any]:
    """Container and stream metadata: duration, fps, codec, resolution, audio."""
    require_ffmpeg()
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-print_format", "json",
            "-show_format", "-show_streams",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise MediaError(f"ffprobe failed for {path}: {result.stderr.strip()}", code="PROBE_FAILED")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise MediaError(f"ffprobe returned invalid JSON for {path}", code="PROBE_FAILED") from exc


def summarise(probed: dict[str, Any]) -> dict[str, Any]:
    """The handful of fields the rest of the system actually branches on.

    `fps` stays a (numerator, denominator) pair on purpose: 30000/1001 is not
    29.97, and rounding it here is how off-by-one frame drift gets into a
    timeline three layers later.
    """
    video = next((s for s in probed.get("streams", []) if s.get("codec_type") == "video"), None)
    audio = next((s for s in probed.get("streams", []) if s.get("codec_type") == "audio"), None)
    fmt = probed.get("format", {})

    fps_num, fps_den = 0, 1
    if video and (rate := video.get("r_frame_rate")):
        try:
            n, d = rate.split("/")
            fps_num, fps_den = int(n), int(d) or 1
        except ValueError:
            pass

    return {
        "duration_s": float(fmt.get("duration") or 0.0),
        "width": int(video.get("width") or 0) if video else 0,
        "height": int(video.get("height") or 0) if video else 0,
        "fps_num": fps_num,
        "fps_den": fps_den,
        "video_codec": video.get("codec_name") if video else None,
        "audio_codec": audio.get("codec_name") if audio else None,
        "has_audio": audio is not None,
        "size_bytes": int(fmt.get("size") or 0),
    }


def proxy_path_for(content_hash: str) -> Path:
    # Referenced through the module, not imported as a constant, so tests can
    # redirect it to a tmp dir.
    return config.PROXY_DIR / f"{content_hash}.mp4"


def make_proxy(
    source: Path | str, content_hash: str, *, force: bool = False
) -> tuple[Path, bool]:
    """Build the 480p analysis proxy.

    Returns (path, encoded) where `encoded` is True only when this call did the
    work — callers report progress from that rather than guessing from mtimes.
    """
    require_ffmpeg()
    out = proxy_path_for(content_hash)
    if out.exists() and not force:
        return out, False

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".partial.mp4")

    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(source),
            # min() so a clip already below the proxy height is never upscaled;
            # -2 keeps width even, which H.264 requires.
            "-vf", f"scale=-2:'min({config.PROXY_HEIGHT},ih)'",
            "-c:v", "h264_videotoolbox", "-b:v", "1200k",
            "-c:a", "aac", "-b:a", "96k",
            str(tmp),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise MediaError(
            f"proxy encode failed for {source}: {result.stderr.strip()[:400]}",
            code="PROXY_FAILED",
        )

    # Rename only on success, so a killed encode never leaves a half proxy that a
    # later run would trust and analyse.
    tmp.replace(out)
    return out, True
