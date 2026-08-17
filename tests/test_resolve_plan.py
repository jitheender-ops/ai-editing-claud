"""
Tier-2 plan tests.

The companion script runs inside Resolve's bundled Python, where nothing from
this project is importable. So this JSON is a real contract between two programs
that share no code, and the inclusive/exclusive frame convention is the part most
likely to be got wrong silently — an off-by-one here is one extra frame on every
clip in the timeline.
"""

from ave.executors.resolve_plan import to_plan_dict
from ave.plan.models import EDL, Clip, Marker, Op, Summary, Timebase, Track


def build(tmp_path, ops=None):
    source = tmp_path / "src.mov"
    source.write_bytes(b"0")
    clip = Clip(
        id="clip_a", source_media_id="m", source_path=str(source),
        source_in_frames=100, source_out_frames=190, timeline_start_frames=0,
        ops=ops or [], reason="because the speaker paused",
    )
    return EDL(
        project="p", version=3,
        timebase=Timebase(fps_num=30, fps_den=1, width=1920, height=1080),
        tracks=[Track(name="V1", clips=[clip])],
        markers=[Marker(frame=0, name="cut", note="why")],
        summary=Summary(source_duration_s=10, output_duration_s=3, clip_count=1),
    )


def test_end_frame_is_inclusive(tmp_path):
    """Resolve's clipInfo endFrame is inclusive; the EDL's source_out is
    exclusive. Getting this wrong adds a frame to every single clip."""
    clip = to_plan_dict(build(tmp_path))["clips"][0]
    assert clip["start_frame"] == 100
    assert clip["end_frame"] == 189


def test_timeline_name_is_versioned(tmp_path):
    assert to_plan_dict(build(tmp_path))["timeline_name"] == "AI_EDIT_v003"


def test_schema_is_declared(tmp_path):
    """The script refuses a plan it does not recognise rather than guessing."""
    assert to_plan_dict(build(tmp_path))["schema"] == "ave-resolve-plan/1"


def test_an_allowed_zoom_is_carried(tmp_path):
    ops = [Op(id="o", type="zoom", params={"value": 1.2}, decision="ALLOW")]
    assert to_plan_dict(build(tmp_path, ops))["clips"][0]["zoom"] == 1.2


def test_an_unapproved_zoom_is_not_carried(tmp_path):
    """Same rule as the FCPXML path: an operation awaiting approval has not been
    approved, and must not reach either executor."""
    ops = [Op(id="o", type="zoom", params={"value": 1.2}, decision="REQUIRE_APPROVAL")]
    assert to_plan_dict(build(tmp_path, ops))["clips"][0]["zoom"] is None


def test_a_denied_zoom_is_not_carried(tmp_path):
    ops = [Op(id="o", type="zoom", params={"value": 9.0}, decision="DENY")]
    assert to_plan_dict(build(tmp_path, ops))["clips"][0]["zoom"] is None


def test_reasons_and_markers_survive(tmp_path):
    plan = to_plan_dict(build(tmp_path))
    assert "paused" in plan["clips"][0]["reason"]
    assert plan["markers"][0]["note"] == "why"


def test_paths_are_absolute(tmp_path):
    """Resolve resolves media by path and has no notion of our working directory."""
    assert to_plan_dict(build(tmp_path))["clips"][0]["path"].startswith("/")


def test_plan_is_json_serialisable_with_stdlib_only(tmp_path):
    import json

    # The reader has no pydantic, so anything exotic here would be unreadable.
    json.loads(json.dumps(to_plan_dict(build(tmp_path))))
