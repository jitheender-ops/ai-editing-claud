"""
FCP7 XML (xmeml) writer — the fallback interchange path.

FCPXML 1.10 is the primary format. This exists because import fidelity is the
one significant risk that cannot be tested from here: Resolve is not scriptable
on the free edition, so nothing in this repo can confirm that Resolve honours
every attribute the FCPXML writer emits. Having a second, structurally different
format hedges that instead of merely documenting it — and it is the same hedge
auto-editor ships, which offers `--export resolve-fcp7` alongside its FCPXML.

xmeml is older and cruder than FCPXML, and that is exactly why it is the useful
fallback: Resolve's importer for it is ancient and very well worn.

Two format differences carry real risk, and both are handled here rather than
left to chance:

**Time is integer frames, not rationals.** No `1001/30000s` — just `66`. That
makes the arithmetic trivial, since the EDL already stores integer frames.

**Non-integer rates are expressed by an NTSC flag, not by the timebase.** 29.97
is written as timebase 30 with `<ntsc>TRUE</ntsc>`, *not* as timebase 29.97.
Writing 29.97 into the timebase is the classic way to produce a timeline that
imports and then drifts, because the importer rounds it back to 30 and keeps the
frame numbers.

**Scale is a percentage.** A 1.12 punch-in is `112`, not `1.12`. Writing the raw
factor produces a clip scaled to one percent of frame — visually obvious, but
only after a round trip through Resolve.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from xml.dom import minidom

from ave.plan.models import EDL, Clip, Timebase


def rate_element(parent: ET.Element, timebase: Timebase) -> ET.Element:
    """<rate> as xmeml wants it: a whole-number timebase plus an NTSC flag."""
    rate = ET.SubElement(parent, "rate")
    # 30000/1001 and 24000/1001 are the NTSC rates; their timebase is the rounded
    # integer and the flag carries the /1.001 pulldown.
    ntsc = timebase.fps_den != 1
    ET.SubElement(rate, "timebase").text = str(round(timebase.fps_num / timebase.fps_den))
    ET.SubElement(rate, "ntsc").text = "TRUE" if ntsc else "FALSE"
    return rate


def _file_element(parent: ET.Element, clip: Clip, timebase: Timebase, file_id: str,
                  defined: set[str], asset_frames: int, channels: int = 2) -> None:
    """xmeml defines a file once, then refers to it by id alone.

    Re-emitting the full definition for every clip is not merely wasteful — some
    importers treat the second definition as a different file and relink one of
    them wrongly.
    """
    if file_id in defined:
        ET.SubElement(parent, "file", id=file_id)
        return

    defined.add(file_id)
    source = Path(clip.source_path)
    element = ET.SubElement(parent, "file", id=file_id)
    ET.SubElement(element, "name").text = source.name
    ET.SubElement(element, "pathurl").text = source.absolute().as_uri()
    rate_element(element, timebase)
    ET.SubElement(element, "duration").text = str(asset_frames)

    media = ET.SubElement(element, "media")
    video = ET.SubElement(media, "video")
    characteristics = ET.SubElement(video, "samplecharacteristics")
    rate_element(characteristics, timebase)
    ET.SubElement(characteristics, "width").text = str(timebase.width)
    ET.SubElement(characteristics, "height").text = str(timebase.height)
    audio = ET.SubElement(media, "audio")
    ET.SubElement(audio, "channelcount").text = str(channels)


def _motion_filter(parent: ET.Element, scale_percent: float) -> None:
    filter_element = ET.SubElement(parent, "filter")
    effect = ET.SubElement(filter_element, "effect")
    ET.SubElement(effect, "name").text = "Basic Motion"
    ET.SubElement(effect, "effectid").text = "basic"
    ET.SubElement(effect, "effectcategory").text = "motion"
    ET.SubElement(effect, "effecttype").text = "motion"
    ET.SubElement(effect, "mediatype").text = "video"
    parameter = ET.SubElement(effect, "parameter")
    ET.SubElement(parameter, "parameterid").text = "scale"
    ET.SubElement(parameter, "name").text = "Scale"
    ET.SubElement(parameter, "valuemin").text = "0"
    ET.SubElement(parameter, "valuemax").text = "1000"
    # Percent, not a factor.
    ET.SubElement(parameter, "value").text = f"{scale_percent:g}"


def to_fcp7(edl: EDL, *, sequence_name: str | None = None) -> str:
    timebase = edl.timebase
    clips = sorted(edl.all_clips(), key=lambda c: c.timeline_start_frames)
    total = max((c.timeline_end_frames for c in clips), default=0)

    root = ET.Element("xmeml", version="5")
    sequence = ET.SubElement(root, "sequence")
    ET.SubElement(sequence, "name").text = sequence_name or f"{edl.project}_v{edl.version:03d}"
    ET.SubElement(sequence, "duration").text = str(total)
    rate_element(sequence, timebase)

    media = ET.SubElement(sequence, "media")
    video = ET.SubElement(media, "video")
    characteristics = ET.SubElement(video, "format")
    sample = ET.SubElement(characteristics, "samplecharacteristics")
    rate_element(sample, timebase)
    ET.SubElement(sample, "width").text = str(timebase.width)
    ET.SubElement(sample, "height").text = str(timebase.height)

    file_ids: dict[str, str] = {}
    for clip in clips:
        if clip.source_path not in file_ids:
            file_ids[clip.source_path] = f"file{len(file_ids) + 1}"

    asset_frames = {
        path: max(c.source_out_frames for c in clips if c.source_path == path)
        for path in file_ids
    }

    defined: set[str] = set()
    video_track = ET.SubElement(video, "track")
    for index, clip in enumerate(clips, start=1):
        item = ET.SubElement(video_track, "clipitem", id=f"clipitem{index}")
        ET.SubElement(item, "name").text = Path(clip.source_path).stem
        ET.SubElement(item, "enabled").text = "TRUE"
        ET.SubElement(item, "duration").text = str(asset_frames[clip.source_path])
        rate_element(item, timebase)
        # start/end are the timeline; in/out are the source. Conflating the two
        # pairs is the single most common way to produce a plausible-looking
        # xmeml that cuts the wrong footage.
        ET.SubElement(item, "start").text = str(clip.timeline_start_frames)
        ET.SubElement(item, "end").text = str(clip.timeline_end_frames)
        ET.SubElement(item, "in").text = str(clip.source_in_frames)
        ET.SubElement(item, "out").text = str(clip.source_out_frames)
        _file_element(item, clip, timebase, file_ids[clip.source_path], defined,
                      asset_frames[clip.source_path], edl.audio_format.channels)

        for op in clip.ops:
            if op.type == "zoom" and op.applied:
                value = op.params.get("value")
                if isinstance(value, (int, float)):
                    _motion_filter(item, float(value) * 100.0)

    # An audio track, or the timeline imports silent.
    audio = ET.SubElement(media, "audio")
    audio_track = ET.SubElement(audio, "track")
    for index, clip in enumerate(clips, start=1):
        item = ET.SubElement(audio_track, "clipitem", id=f"clipitem-a{index}")
        ET.SubElement(item, "name").text = Path(clip.source_path).stem
        ET.SubElement(item, "enabled").text = "TRUE"
        ET.SubElement(item, "duration").text = str(asset_frames[clip.source_path])
        rate_element(item, timebase)
        ET.SubElement(item, "start").text = str(clip.timeline_start_frames)
        ET.SubElement(item, "end").text = str(clip.timeline_end_frames)
        ET.SubElement(item, "in").text = str(clip.source_in_frames)
        ET.SubElement(item, "out").text = str(clip.source_out_frames)
        ET.SubElement(item, "file", id=file_ids[clip.source_path])

    raw = ET.tostring(root, encoding="unicode")
    body = minidom.parseString(raw).documentElement.toprettyxml(indent="    ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n' + body


def write_fcp7(edl: EDL, path: Path | str) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(to_fcp7(edl), encoding="utf-8")
    return out
