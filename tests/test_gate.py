"""
Validation gate tests.

The behaviour under test is that an uncertain operation is neither applied nor
silently dropped — it comes out REQUIRE_APPROVAL with its reasons attached. The
other half is that worst-decision-wins, so no passing check can mask a failing
one.
"""

import pytest

from ave.plan.models import EDL, Clip, Op, Summary, Timebase, Track, worst
from ave.policies.validate import evaluate_op, validate_edl
from ave.style.models import default_dna


def make_clip(tmp_path, ops=None, exists=True):
    source = tmp_path / "src.mov"
    if exists:
        source.write_bytes(b"0")
    return Clip(
        id="clip_a", source_media_id="m", source_path=str(source),
        source_in_frames=0, source_out_frames=90, timeline_start_frames=0,
        ops=ops or [],
    )


def make_edl(clips):
    return EDL(
        project="p",
        timebase=Timebase(fps_num=30, fps_den=1, width=1920, height=1080),
        tracks=[Track(name="V1", clips=clips)],
        summary=Summary(source_duration_s=10, output_duration_s=3, clip_count=len(clips)),
    )


def decide(op, clip, autonomy=2):
    reasons = evaluate_op(op, clip, default_dna(), autonomy=autonomy)
    return worst([r.decision for r in reasons]), reasons


# ── worst decision wins ──────────────────────────────────────────────────────


def test_worst_decision_wins():
    assert worst(["ALLOW", "ALLOW"]) == "ALLOW"
    assert worst(["ALLOW", "REQUIRE_APPROVAL"]) == "REQUIRE_APPROVAL"
    assert worst(["REQUIRE_APPROVAL", "DENY", "ALLOW"]) == "DENY"
    assert worst([]) == "ALLOW"


# ── individual checks ────────────────────────────────────────────────────────


def test_zoom_within_limits_is_allowed(tmp_path):
    clip = make_clip(tmp_path)
    decision, _ = decide(Op(id="o", type="zoom", params={"value": 1.15}), clip)
    assert decision == "ALLOW"


def test_zoom_beyond_the_limit_is_denied(tmp_path):
    """The DNA asking for a 3x punch-in does not make it sane."""
    clip = make_clip(tmp_path)
    decision, reasons = decide(Op(id="o", type="zoom", params={"value": 3.0}), clip)
    assert decision == "DENY"
    assert any(r.check == "BOUNDS" and r.rule_id == "BND-001" for r in reasons)


def test_operation_without_a_value_is_denied(tmp_path):
    clip = make_clip(tmp_path)
    decision, reasons = decide(Op(id="o", type="zoom", params={}), clip)
    assert decision == "DENY"
    assert reasons[0].check == "SCHEMA"


def test_operation_outside_the_clip_is_denied(tmp_path):
    clip = make_clip(tmp_path)  # 90 frames long
    op = Op(id="o", type="zoom", params={"value": 1.1}, at_frames=80, duration_frames=30)
    decision, reasons = decide(op, clip)
    assert decision == "DENY"
    assert any(r.check == "MEDIA" for r in reasons)


def test_low_confidence_asks_for_a_human(tmp_path):
    clip = make_clip(tmp_path)
    op = Op(id="o", type="zoom", params={"value": 1.1}, confidence=0.4)
    decision, reasons = decide(op, clip)
    assert decision == "REQUIRE_APPROVAL"
    assert any(r.rule_id == "CNF-002" for r in reasons)


def test_very_low_confidence_is_not_even_worth_reviewing(tmp_path):
    clip = make_clip(tmp_path)
    op = Op(id="o", type="zoom", params={"value": 1.1}, confidence=0.05)
    decision, _ = decide(op, clip)
    assert decision == "DENY"


def test_a_failing_check_is_not_masked_by_a_passing_one(tmp_path):
    """Bounds pass, confidence fails — the op must still not apply."""
    clip = make_clip(tmp_path)
    op = Op(id="o", type="zoom", params={"value": 1.1}, confidence=0.3)
    decision, reasons = decide(op, clip)
    assert any(r.decision == "ALLOW" for r in reasons), "bounds should have passed"
    assert decision == "REQUIRE_APPROVAL"


# ── autonomy ─────────────────────────────────────────────────────────────────


def test_model_proposed_ops_need_approval_below_full_autonomy(tmp_path):
    clip = make_clip(tmp_path)
    op = Op(id="o", type="zoom", params={"value": 1.1}, source="llm")
    assert decide(op, clip, autonomy=2)[0] == "REQUIRE_APPROVAL"


def test_full_autonomy_allows_model_proposed_ops(tmp_path):
    clip = make_clip(tmp_path)
    op = Op(id="o", type="zoom", params={"value": 1.1}, source="llm")
    assert decide(op, clip, autonomy=3)[0] == "ALLOW"


def test_autonomy_can_only_tighten(tmp_path):
    """A rule-sourced op is unaffected by autonomy; autonomy never loosens a
    decision that another check already made."""
    clip = make_clip(tmp_path)
    op = Op(id="o", type="zoom", params={"value": 9.0}, source="rule")
    assert decide(op, clip, autonomy=3)[0] == "DENY"


# ── whole-EDL validation ─────────────────────────────────────────────────────


def test_missing_media_is_caught_at_plan_time(tmp_path):
    """Named here, where the message identifies the clip — not at XML write,
    where it surfaces as an exception far from anything recognisable."""
    edl = make_edl([make_clip(tmp_path, exists=False)])
    report = validate_edl(edl, default_dna())

    assert not report.ok
    assert "source media missing" in report.clip_errors[0][1]


def test_valid_plan_passes(tmp_path):
    report = validate_edl(make_edl([make_clip(tmp_path)]), default_dna())
    assert report.ok
    assert report.denied == [] and report.needs_approval == []


def test_decisions_and_reasons_are_written_back_onto_the_ops(tmp_path):
    """The stored EDL must be the one that explains itself."""
    op = Op(id="o", type="zoom", params={"value": 5.0})
    edl = make_edl([make_clip(tmp_path, ops=[op])])
    validate_edl(edl, default_dna())

    assert op.decision == "DENY"
    assert op.reasons and op.reasons[0].message
    assert op in edl.all_ops()


def test_overlapping_same_type_ops_are_flagged_not_dropped(tmp_path):
    ops = [
        Op(id="o1", type="zoom", params={"value": 1.1}, at_frames=0, duration_frames=30, priority=5),
        Op(id="o2", type="zoom", params={"value": 1.3}, at_frames=10, duration_frames=30, priority=1),
    ]
    edl = make_edl([make_clip(tmp_path, ops=ops)])
    validate_edl(edl, default_dna())

    assert ops[0].decision == "ALLOW", "the higher-priority op survives"
    assert ops[1].decision == "REQUIRE_APPROVAL", "the loser is flagged, not silently discarded"
    assert any(r.check == "CONFLICT" for r in ops[1].reasons)


def test_non_overlapping_ops_do_not_conflict(tmp_path):
    ops = [
        Op(id="o1", type="zoom", params={"value": 1.1}, at_frames=0, duration_frames=10),
        Op(id="o2", type="zoom", params={"value": 1.2}, at_frames=40, duration_frames=10),
    ]
    edl = make_edl([make_clip(tmp_path, ops=ops)])
    validate_edl(edl, default_dna())
    assert [op.decision for op in ops] == ["ALLOW", "ALLOW"]


def test_pending_approval_is_queryable(tmp_path):
    op = Op(id="o", type="zoom", params={"value": 1.1}, confidence=0.3)
    edl = make_edl([make_clip(tmp_path, ops=[op])])
    validate_edl(edl, default_dna())
    assert edl.pending_approval() == [op]
