"""
Golden-reference tests.

FCPXML import fidelity is the one significant risk that cannot be tested on this
machine: Resolve is not scriptable on the free edition, so nothing here can
confirm that Resolve accepts what we write.

`tests/golden/auto-editor-resolve.fcpxml` is the substitute. auto-editor has ~5k
stars and its `--export resolve` is used daily, so its element and attribute
*vocabulary* is real evidence about what Resolve accepts. Values are not
compared — our cut points legitimately differ because the padding defaults differ.
What must match is the shape of the document.

This comparison already earned its place: it caught us declaring stereo/48kHz
for every source while auto-editor correctly reported the file's actual mono
44.1kHz. Most screen capture is mono, so that was not a corner case.
"""

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from ave.executors.fcpxml import to_fcpxml
from ave.plan.models import EDL, AudioFormat, Clip, Summary, Timebase, Track

GOLDEN = Path(__file__).parent / "golden" / "auto-editor-resolve.fcpxml"


@pytest.fixture
def golden():
    if not GOLDEN.exists():
        pytest.skip("golden reference not present")
    return ET.parse(GOLDEN).getroot()


@pytest.fixture
def ours(tmp_path):
    source = tmp_path / "speech_demo.mp4"
    source.write_bytes(b"0")
    clips = [
        Clip(id="a", source_media_id="m", source_path=str(source),
             source_in_frames=0, source_out_frames=67, timeline_start_frames=0),
        Clip(id="b", source_media_id="m", source_path=str(source),
             source_in_frames=114, source_out_frames=187, timeline_start_frames=67),
    ]
    edl = EDL(
        project="speech_demo",
        timebase=Timebase(fps_num=30, fps_den=1, width=640, height=360),
        audio_format=AudioFormat(channels=1, sample_rate=44100),
        tracks=[Track(name="V1", clips=clips)],
        summary=Summary(source_duration_s=10, output_duration_s=4.6, clip_count=2),
    )
    return ET.fromstring(to_fcpxml(edl))


def tags(root):
    return {element.tag for element in root.iter()}


def test_we_use_the_same_element_vocabulary(golden, ours):
    """Every element the reference uses, we use. Extra elements of ours are fine
    — markers, for one, which auto-editor does not emit."""
    missing = tags(golden) - tags(ours)
    assert not missing, f"reference uses elements we do not: {missing}"


def test_the_resource_structure_matches(golden, ours):
    for root in (golden, ours):
        assert root.find("resources/format") is not None
        assert root.find("resources/asset") is not None
        assert root.find("resources/asset/media-rep") is not None
        assert root.find("library/event/project/sequence/spine") is not None


@pytest.mark.parametrize("attribute", ["id", "frameDuration", "width", "height", "colorSpace"])
def test_format_carries_the_same_attributes(golden, ours, attribute):
    assert golden.find("resources/format").get(attribute) is not None
    assert ours.find("resources/format").get(attribute) is not None


@pytest.mark.parametrize(
    "attribute", ["id", "start", "duration", "hasVideo", "hasAudio", "format", "audioChannels"]
)
def test_asset_carries_the_same_attributes(golden, ours, attribute):
    assert golden.find("resources/asset").get(attribute) is not None
    assert ours.find("resources/asset").get(attribute) is not None


@pytest.mark.parametrize("attribute", ["offset", "duration", "start", "name", "ref"])
def test_asset_clip_carries_the_same_attributes(golden, ours, attribute):
    for root in (golden, ours):
        clip = root.find(".//spine/asset-clip")
        assert clip.get(attribute) is not None, f"{attribute} missing"


def test_audio_metadata_matches_the_real_source(golden, ours):
    """The bug this file caught. The clip is mono 44.1kHz; both must say so."""
    assert golden.find("resources/asset").get("audioChannels") == "1"
    assert ours.find("resources/asset").get("audioChannels") == "1"

    golden_sequence = golden.find(".//sequence")
    our_sequence = ours.find(".//sequence")
    assert golden_sequence.get("audioLayout") == our_sequence.get("audioLayout") == "mono"
    assert golden_sequence.get("audioRate") == our_sequence.get("audioRate") == "44.1k"


def test_both_express_time_as_rationals(golden, ours):
    """Never decimals — 29.97 has no decimal representation."""
    for root in (golden, ours):
        duration = root.find(".//spine/asset-clip").get("duration")
        assert duration.endswith("s")
        assert "/" in duration or duration == "0s"


def test_media_paths_are_file_urls(golden, ours):
    for root in (golden, ours):
        assert root.find(".//media-rep").get("src").startswith("file:///")
