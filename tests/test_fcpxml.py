"""
FCPXML writer tests.

Resolve is not scriptable here, so this file is the only automated defence
against a malformed timeline. It checks the properties Resolve actually rejects
on: exact rational time, contiguous offsets, resolvable references, and source
ranges that fit inside the asset.
"""

import xml.etree.ElementTree as ET

import pytest

from ave.executors.fcpxml import frame_duration, frames_to_time, to_fcpxml
from ave.plan.models import EDL, Clip, Marker, Op, Summary, Timebase, Track


def build_edl(tmp_path, clips=None, markers=None, fps=(30, 1)):
    source = tmp_path / "src.mov"
    source.write_bytes(b"0")
    timebase = Timebase(fps_num=fps[0], fps_den=fps[1], width=1920, height=1080)
    clips = clips or [
        Clip(id="clip_a", source_media_id="m", source_path=str(source),
             source_in_frames=0, source_out_frames=60, timeline_start_frames=0),
        Clip(id="clip_b", source_media_id="m", source_path=str(source),
             source_in_frames=120, source_out_frames=210, timeline_start_frames=60),
    ]
    return EDL(
        project="p", timebase=timebase,
        tracks=[Track(name="V1", clips=clips)],
        markers=markers or [],
        summary=Summary(source_duration_s=10, output_duration_s=5, clip_count=len(clips)),
    )


# ── time arithmetic ──────────────────────────────────────────────────────────


def test_zero_is_plain_zero():
    assert frames_to_time(0, Timebase(fps_num=30, fps_den=1, width=1, height=1)) == "0s"


def test_integer_rate_time():
    tb = Timebase(fps_num=30, fps_den=1, width=1, height=1)
    assert frames_to_time(30, tb) == "30/30s"
    assert frame_duration(tb) == "1/30s"


def test_drop_frame_rate_stays_exact():
    """29.97 is 30000/1001. Writing 29.97 as a decimal is how a timeline ends up
    a frame short over a long edit."""
    tb = Timebase(fps_num=30000, fps_den=1001, width=1, height=1)
    assert frames_to_time(1, tb) == "1001/30000s"
    assert frames_to_time(1800, tb) == "1801800/30000s"  # exactly 60.06s
    assert frame_duration(tb) == "1001/30000s"


def test_time_is_exact_for_every_frame_count():
    tb = Timebase(fps_num=30000, fps_den=1001, width=1, height=1)
    for frames in (1, 7, 999, 100_000):
        num, den = frames_to_time(frames, tb).rstrip("s").split("/")
        assert int(num) / int(den) == pytest.approx(frames * 1001 / 30000, rel=1e-12)


# ── document structure ───────────────────────────────────────────────────────


def test_document_is_well_formed_and_versioned(tmp_path):
    root = ET.fromstring(to_fcpxml(build_edl(tmp_path)))
    assert root.tag == "fcpxml"
    # Resolve 21's own export tops out at 1.10, so that is the dialect it reads best.
    assert root.get("version") == "1.10"


def test_every_clip_reference_resolves(tmp_path):
    root = ET.fromstring(to_fcpxml(build_edl(tmp_path)))
    asset_ids = {a.get("id") for a in root.iter("asset")}
    for clip in root.iter("asset-clip"):
        assert clip.get("ref") in asset_ids, "a dangling ref makes Resolve reject the import"


def test_clips_are_contiguous_on_the_timeline(tmp_path):
    """A gap renders as black; an overlap silently hides a clip."""
    root = ET.fromstring(to_fcpxml(build_edl(tmp_path)))
    tb = Timebase(fps_num=30, fps_den=1, width=1, height=1)

    def frames(value):
        if value == "0s":
            return 0
        num, den = value.rstrip("s").split("/")
        return int(num) * tb.fps_num // (int(den) * tb.fps_den)

    cursor = 0
    for clip in root.iter("asset-clip"):
        assert frames(clip.get("offset")) == cursor
        cursor += frames(clip.get("duration"))


def test_asset_is_long_enough_for_every_clip_that_reads_it(tmp_path):
    """A clip reading past the declared asset duration is rejected as out of range."""
    root = ET.fromstring(to_fcpxml(build_edl(tmp_path)))
    asset = next(root.iter("asset"))
    assert asset.get("duration") == "210/30s", "must cover the furthest source_out"


def test_one_asset_per_file_however_many_clips(tmp_path):
    edl = build_edl(tmp_path)
    root = ET.fromstring(to_fcpxml(edl))
    assert len(list(root.iter("asset"))) == 1
    assert len(list(root.iter("asset-clip"))) == 2


def test_media_path_is_percent_encoded(tmp_path):
    """This project lives under a directory containing a space, so an unencoded
    src is not a hypothetical failure."""
    spaced = tmp_path / "a folder with spaces"
    spaced.mkdir()
    source = spaced / "my clip.mov"
    source.write_bytes(b"0")
    clip = Clip(id="c", source_media_id="m", source_path=str(source),
                source_in_frames=0, source_out_frames=30, timeline_start_frames=0)

    root = ET.fromstring(to_fcpxml(build_edl(tmp_path, clips=[clip])))
    src = next(root.iter("media-rep")).get("src")

    assert src.startswith("file:///")
    assert " " not in src
    assert "%20" in src


def test_markers_carry_the_reason_into_the_timeline(tmp_path):
    markers = [Marker(frame=60, name="cut -1.5s", note="because the speaker paused")]
    root = ET.fromstring(to_fcpxml(build_edl(tmp_path, markers=markers)))

    found = list(root.iter("marker"))
    assert len(found) == 1
    assert found[0].get("value") == "cut -1.5s"
    assert "speaker paused" in found[0].get("note")
    # Marker time is in the asset's own space, so it must equal the clip's in-point.
    assert found[0].get("start") == "120/30s"


# ── operations ───────────────────────────────────────────────────────────────


def test_allowed_zoom_becomes_a_transform(tmp_path):
    clip = Clip(
        id="c", source_media_id="m", source_path=str(tmp_path / "src.mov"),
        source_in_frames=0, source_out_frames=30, timeline_start_frames=0,
        ops=[Op(id="op1", type="zoom", params={"value": 1.12}, decision="ALLOW")],
    )
    (tmp_path / "src.mov").write_bytes(b"0")
    root = ET.fromstring(to_fcpxml(build_edl(tmp_path, clips=[clip])))

    transform = next(root.iter("adjust-transform"))
    assert transform.get("scale") == "1.12 1.12"


@pytest.mark.parametrize("decision", ["DENY", "REQUIRE_APPROVAL"])
def test_undecided_operations_never_reach_the_file(tmp_path, decision):
    """Writing an op that is awaiting approval would apply a decision nobody made."""
    (tmp_path / "src.mov").write_bytes(b"0")
    clip = Clip(
        id="c", source_media_id="m", source_path=str(tmp_path / "src.mov"),
        source_in_frames=0, source_out_frames=30, timeline_start_frames=0,
        ops=[Op(id="op1", type="zoom", params={"value": 1.12}, decision=decision)],
    )
    root = ET.fromstring(to_fcpxml(build_edl(tmp_path, clips=[clip])))
    assert not list(root.iter("adjust-transform"))
