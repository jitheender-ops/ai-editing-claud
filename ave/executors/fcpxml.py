"""
FCPXML writer — the primary execution path.

This, not the scripting API, is how an edit reaches Resolve. Three reasons, all
verified rather than assumed:

  * Resolve here is the free edition, where external scripting is Studio-only.
  * The scripting API cannot add a transition at all — the word does not occur
    once in Blackmagic's 101 KB scripting README — and `SetProperty` exposes only
    static transforms, so a file expresses strictly *more* than the API can.
  * A file makes the whole pipeline a pure function, testable without launching
    Resolve. That is what lets the edit logic have real tests.

Version 1.10 specifically: Resolve 21's own export constants stop at
EXPORT_FCPXML_1_10, so that is the dialect Resolve itself writes and therefore
the one it reads most reliably.

**Time is exact rational arithmetic.** FCPXML writes durations as `1001/30000s`,
not as decimals, precisely because 29.97 fps cannot be represented in decimal.
Every value here is derived from an integer frame count, so nothing accumulates
drift across a few hundred cuts.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from xml.dom import minidom

from ave.plan.models import EDL, Clip, Timebase

FCPXML_VERSION = "1.10"


def frames_to_time(frames: int, timebase: Timebase) -> str:
    """N frames as an exact FCPXML rational.

    N frames = N * fps_den / fps_num seconds. Left unreduced on purpose: FCPXML
    accepts it, and reducing invites a divide-by-common-factor bug for zero gain.
    """
    if frames == 0:
        return "0s"
    return f"{frames * timebase.fps_den}/{timebase.fps_num}s"


def frame_duration(timebase: Timebase) -> str:
    return f"{timebase.fps_den}/{timebase.fps_num}s"


def _format_name(timebase: Timebase) -> str:
    fps = timebase.fps
    label = f"{fps:.2f}".rstrip("0").rstrip(".")
    return f"FFVideoFormat{timebase.height}p{label}"


def to_fcpxml(edl: EDL, *, event_name: str = "ave") -> str:
    timebase = edl.timebase
    root = ET.Element("fcpxml", version=FCPXML_VERSION)
    resources = ET.SubElement(root, "resources")

    ET.SubElement(
        resources,
        "format",
        id="r1",
        name=_format_name(timebase),
        frameDuration=frame_duration(timebase),
        width=str(timebase.width),
        height=str(timebase.height),
        colorSpace="1-1-1 (Rec. 709)",
    )

    # One asset per distinct source file, however many clips reference it.
    assets: dict[str, str] = {}
    for clip in edl.all_clips():
        if clip.source_path in assets:
            continue
        asset_id = f"r{len(assets) + 2}"
        assets[clip.source_path] = asset_id

        source = Path(clip.source_path)
        # The asset must be at least as long as the furthest point any clip reads
        # from it, or Resolve rejects the clip as out of range.
        needed = max(
            c.source_out_frames for c in edl.all_clips() if c.source_path == clip.source_path
        )
        asset = ET.SubElement(
            resources,
            "asset",
            id=asset_id,
            name=source.stem,
            start="0s",
            duration=frames_to_time(needed, timebase),
            hasVideo="1",
            hasAudio="1",
            format="r1",
            audioSources="1",
            audioChannels=str(edl.audio_format.channels),
            audioRate=str(edl.audio_format.sample_rate),
        )
        # as_uri() percent-encodes, which matters here: this project lives under a
        # path containing a space.
        ET.SubElement(asset, "media-rep", kind="original-media", src=source.absolute().as_uri())

    library = ET.SubElement(root, "library")
    event = ET.SubElement(library, "event", name=event_name)
    project = ET.SubElement(event, "project", name=f"{edl.project}_v{edl.version:03d}")

    total = max((c.timeline_end_frames for c in edl.all_clips()), default=0)
    sequence = ET.SubElement(
        project,
        "sequence",
        format="r1",
        duration=frames_to_time(total, timebase),
        tcStart="0s",
        tcFormat="NDF",
        audioLayout=edl.audio_format.layout,
        audioRate=edl.audio_format.rate_label,
    )
    spine = ET.SubElement(sequence, "spine")

    marker_by_frame = {m.frame: m for m in edl.markers}

    for clip in sorted(edl.all_clips(), key=lambda c: c.timeline_start_frames):
        element = ET.SubElement(
            spine,
            "asset-clip",
            ref=assets[clip.source_path],
            offset=frames_to_time(clip.timeline_start_frames, timebase),
            name=Path(clip.source_path).stem,
            start=frames_to_time(clip.source_in_frames, timebase),
            duration=frames_to_time(clip.duration_frames, timebase),
            format="r1",
            tcFormat="NDF",
        )
        _add_ops(element, clip, timebase)

        # Markers carry the reason for the cut into the Resolve timeline, so it is
        # readable while scrubbing. Marker time is in the asset's own space, so a
        # marker at the clip's in-point lands exactly on the cut.
        if marker := marker_by_frame.get(clip.timeline_start_frames):
            ET.SubElement(
                element,
                "marker",
                start=frames_to_time(clip.source_in_frames, timebase),
                duration=frame_duration(timebase),
                value=marker.name,
                note=marker.note,
            )

    return _pretty(root)


def _add_ops(element: ET.Element, clip: Clip, timebase: Timebase) -> None:
    """Only ALLOW-ed operations reach the file.

    Denied and awaiting-approval ops stay in the stored EDL with their reasons
    intact — they are visible in the plan and in the approval queue, just not in
    the timeline. Writing them out would apply decisions nobody made.
    """
    scale_x = scale_y = 1.0
    offset_x = offset_y = 0.0
    touched = False

    for op in clip.ops:
        if not op.applied:
            continue
        value = op.params.get("value")
        if op.type == "zoom" and isinstance(value, (int, float)):
            scale_x = scale_y = float(value)
            touched = True
        elif op.type == "pan" and isinstance(value, (int, float)):
            offset_x = float(value)
            touched = True

    if touched:
        ET.SubElement(
            element,
            "adjust-transform",
            scale=f"{scale_x:g} {scale_y:g}",
            position=f"{offset_x:g} {offset_y:g}",
        )


def _pretty(root: ET.Element) -> str:
    raw = ET.tostring(root, encoding="unicode")
    body = minidom.parseString(raw).documentElement.toprettyxml(indent="    ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n' + body


def write_fcpxml(edl: EDL, path: Path | str) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(to_fcpxml(edl), encoding="utf-8")
    return out
