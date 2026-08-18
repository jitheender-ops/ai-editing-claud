"""
FCP7 XML (xmeml) fallback tests.

This format exists to hedge the one risk that cannot be tested from here: whether
Resolve honours everything the FCPXML writer emits. Resolve is not scriptable on
the free edition, so no test in this repo can confirm an import. A second,
structurally different format is the hedge.

Its three sharp edges each get a test, because all three produce a file that
imports successfully and is silently wrong.
"""

import xml.etree.ElementTree as ET

from ave.executors.fcp7 import to_fcp7
from ave.plan.models import EDL, Clip, Op, Summary, Timebase, Track


def build(tmp_path, fps=(30, 1), ops=None, clips=None):
    source = tmp_path / "src.mov"
    source.write_bytes(b"0")
    clips = clips or [
        Clip(id="a", source_media_id="m", source_path=str(source),
             source_in_frames=0, source_out_frames=66, timeline_start_frames=0, ops=ops or []),
        Clip(id="b", source_media_id="m", source_path=str(source),
             source_in_frames=118, source_out_frames=186, timeline_start_frames=66),
    ]
    return EDL(
        project="p", version=1,
        timebase=Timebase(fps_num=fps[0], fps_den=fps[1], width=1920, height=1080),
        tracks=[Track(name="V1", clips=clips)],
        summary=Summary(source_duration_s=10, output_duration_s=4.5, clip_count=len(clips)),
    )


def parse(edl):
    return ET.fromstring(to_fcp7(edl))


# ── the three sharp edges ────────────────────────────────────────────────────


def test_ntsc_rates_use_the_flag_not_a_fractional_timebase(tmp_path):
    """29.97 is timebase 30 with ntsc TRUE. Writing 29.97 into the timebase gives
    a file that imports and then drifts, because the importer rounds it back to
    30 and keeps the frame numbers."""
    root = parse(build(tmp_path, fps=(30000, 1001)))
    rate = root.find(".//sequence/rate")

    assert rate.findtext("timebase") == "30"
    assert rate.findtext("ntsc") == "TRUE"


def test_integer_rates_declare_ntsc_false(tmp_path):
    rate = parse(build(tmp_path)).find(".//sequence/rate")
    assert rate.findtext("timebase") == "30"
    assert rate.findtext("ntsc") == "FALSE"


def test_scale_is_a_percentage_not_a_factor(tmp_path):
    """A 1.12 punch-in is 112. Writing 1.12 scales the clip to one percent of
    frame — obvious on screen, but only after a round trip through Resolve."""
    ops = [Op(id="o", type="zoom", params={"value": 1.12}, decision="ALLOW")]
    root = parse(build(tmp_path, ops=ops))

    parameter = root.find(".//filter/effect/parameter")
    assert parameter.findtext("parameterid") == "scale"
    assert float(parameter.findtext("value")) == 112.0


def test_timeline_and_source_ranges_are_kept_apart(tmp_path):
    """start/end are the timeline; in/out are the source. Conflating the pairs is
    the most common way to produce a plausible xmeml that cuts the wrong footage."""
    items = parse(build(tmp_path)).findall(".//video/track/clipitem")
    second = items[1]

    assert (second.findtext("start"), second.findtext("end")) == ("66", "134")
    assert (second.findtext("in"), second.findtext("out")) == ("118", "186")


# ── structure ────────────────────────────────────────────────────────────────


def test_a_file_is_defined_once_then_referenced(tmp_path):
    """Re-emitting the definition can make an importer treat the second as a
    different file and relink one of them wrongly."""
    files = list(parse(build(tmp_path)).iter("file"))
    defined = [f for f in files if len(list(f))]

    assert len(defined) == 1, "exactly one full definition"
    assert all(f.get("id") == "file1" for f in files), "all references share its id"


def test_the_timeline_is_contiguous(tmp_path):
    items = parse(build(tmp_path)).findall(".//video/track/clipitem")
    cursor = 0
    for item in items:
        assert int(item.findtext("start")) == cursor
        cursor = int(item.findtext("end"))


def test_an_audio_track_is_emitted(tmp_path):
    """Without one the timeline imports silent."""
    root = parse(build(tmp_path))
    assert len(root.findall(".//audio/track/clipitem")) == 2


def test_sequence_duration_matches_the_last_clip(tmp_path):
    root = parse(build(tmp_path))
    assert root.findtext(".//sequence/duration") == "134"


def test_media_path_is_a_percent_encoded_url(tmp_path):
    spaced = tmp_path / "a folder"
    spaced.mkdir()
    source = spaced / "my clip.mov"
    source.write_bytes(b"0")
    clip = Clip(id="a", source_media_id="m", source_path=str(source),
                source_in_frames=0, source_out_frames=30, timeline_start_frames=0)

    url = parse(build(tmp_path, clips=[clip])).find(".//pathurl").text
    assert url.startswith("file:///") and "%20" in url


def test_unapproved_operations_do_not_reach_this_format_either(tmp_path):
    ops = [Op(id="o", type="zoom", params={"value": 1.12}, decision="REQUIRE_APPROVAL")]
    assert parse(build(tmp_path, ops=ops)).find(".//filter") is None


def test_document_is_declared_as_xmeml_5(tmp_path):
    root = parse(build(tmp_path))
    assert root.tag == "xmeml" and root.get("version") == "5"
