"""
The operation validation gate.

One entry point evaluates every operation through checks in a fixed order:

    schema -> media -> bounds -> conflict -> confidence -> autonomy

They are merged into a single pipeline deliberately. When schema, bounds and
confidence are separate subsystems it becomes possible for an op to satisfy one
and silently skip another; here an op gets exactly one decision and the executor
has one thing to obey. Worst decision wins, so no passing check can mask a
failing one.

No model is consulted. Every decision below is deterministic code — which is what
makes "why did this happen?" answerable with a rule id rather than a guess.

The three-way outcome is the point. `REQUIRE_APPROVAL` exists so that an op the
planner is unsure about is neither applied on a coin-flip nor silently discarded:
it goes to a human. Silently dropping uncertain work is the failure mode that
makes an automated editor untrustworthy, because you cannot see what it decided
not to tell you.

Adapted from commerce-os `policies/governance.ts`.
"""

from __future__ import annotations

from pathlib import Path

from ave.plan.models import EDL, Clip, Decision, Op, OpReason, worst
from ave.policies.rules import (
    AUTONOMY_AUTONOMOUS,
    AUTONOMY_BUILD_AND_FLAG,
    CONFIDENCE_DENY,
    CONFIDENCE_FLOOR,
    OP_LIMITS,
)
from ave.style.models import EditDNA


def evaluate_op(
    op: Op, clip: Clip, dna: EditDNA, *, autonomy: int = AUTONOMY_BUILD_AND_FLAG
) -> list[OpReason]:
    reasons: list[OpReason] = []

    # 1 — Schema. A malformed op never reaches the later checks, because their
    # answers would be meaningless.
    if op.type in OP_LIMITS and "value" not in op.params:
        reasons.append(
            OpReason(
                check="SCHEMA",
                decision="DENY",
                rule_id="SCH-001",
                message=f"{op.type} operation carries no 'value' parameter",
                inputs={"params": op.params},
            )
        )
        return reasons

    # 2 — Media. Frames must lie inside the clip they claim to modify.
    if op.at_frames is not None:
        end = op.at_frames + (op.duration_frames or 0)
        if op.at_frames < 0 or end > clip.duration_frames:
            reasons.append(
                OpReason(
                    check="MEDIA",
                    decision="DENY",
                    rule_id="MED-002",
                    message=(
                        f"operation spans frames {op.at_frames}..{end} but the clip is only "
                        f"{clip.duration_frames} frames long"
                    ),
                    inputs={"at": op.at_frames, "clip_frames": clip.duration_frames},
                )
            )
            return reasons

    # 3 — Bounds. A value outside the physical limit is denied outright; the DNA
    # asking for it does not make it sane.
    limits = OP_LIMITS.get(op.type)
    if limits and isinstance(op.params.get("value"), (int, float)):
        value = float(op.params["value"])
        if not (limits["min"] <= value <= limits["max"]):
            reasons.append(
                OpReason(
                    check="BOUNDS",
                    decision="DENY",
                    rule_id="BND-001",
                    message=(
                        f"{op.type} of {value:g} is outside the allowed "
                        f"{limits['min']:g}..{limits['max']:g}"
                    ),
                    inputs={"value": value, **limits},
                )
            )
        else:
            reasons.append(
                OpReason(
                    check="BOUNDS",
                    decision="ALLOW",
                    rule_id="BND-001",
                    message=f"{op.type} of {value:g} is within range",
                    inputs={"value": value},
                )
            )

    # 4 — Confidence.
    if op.confidence < CONFIDENCE_DENY:
        reasons.append(
            OpReason(
                check="CONFIDENCE",
                decision="DENY",
                rule_id="CNF-001",
                message=f"confidence {op.confidence:.2f} is too low to be worth reviewing",
                inputs={"confidence": op.confidence, "floor": CONFIDENCE_DENY},
            )
        )
    elif op.confidence < CONFIDENCE_FLOOR:
        reasons.append(
            OpReason(
                check="CONFIDENCE",
                decision="REQUIRE_APPROVAL",
                rule_id="CNF-002",
                message=(
                    f"confidence {op.confidence:.2f} is below the {CONFIDENCE_FLOOR:.2f} "
                    f"floor for unreviewed application"
                ),
                inputs={"confidence": op.confidence, "floor": CONFIDENCE_FLOOR},
            )
        )

    # 5 — Autonomy. Can only tighten.
    if autonomy < AUTONOMY_AUTONOMOUS and op.source == "llm":
        reasons.append(
            OpReason(
                check="AUTONOMY",
                decision="REQUIRE_APPROVAL",
                rule_id="AUT-001",
                message=(
                    f"autonomy level {autonomy}: model-proposed operations need a human"
                ),
                inputs={"autonomy": autonomy, "source": op.source},
            )
        )

    if not reasons:
        reasons.append(
            OpReason(
                check="SCHEMA",
                decision="ALLOW",
                rule_id="SCH-000",
                message="no specific policy applies",
            )
        )
    return reasons


def check_conflicts(clip: Clip) -> dict[str, OpReason]:
    """Two ops of the same type over the same frames cannot both apply.

    The lower-priority one is flagged rather than dropped, because which of them
    is wanted is a judgement the planner should not make silently.
    """
    flagged: dict[str, OpReason] = {}
    for i, first in enumerate(clip.ops):
        for second in clip.ops[i + 1 :]:
            if first.type != second.type or not _overlaps(first, second, clip):
                continue
            loser = second if first.priority >= second.priority else first
            winner = first if loser is second else second
            flagged[loser.id] = OpReason(
                check="CONFLICT",
                decision="REQUIRE_APPROVAL",
                rule_id="CFL-001",
                message=(
                    f"overlaps another {loser.type} operation with "
                    f"{'higher' if winner.priority > loser.priority else 'equal'} priority"
                ),
                inputs={"other_op": winner.id, "priority": loser.priority},
            )
    return flagged


def _overlaps(a: Op, b: Op, clip: Clip) -> bool:
    a_start = a.at_frames if a.at_frames is not None else 0
    a_end = a_start + (a.duration_frames or (clip.duration_frames if a.at_frames is None else 0))
    b_start = b.at_frames if b.at_frames is not None else 0
    b_end = b_start + (b.duration_frames or (clip.duration_frames if b.at_frames is None else 0))
    return a_start < b_end and b_start < a_end


class ValidationReport:
    def __init__(self) -> None:
        self.clip_errors: list[tuple[str, str]] = []  # (clip id, message)
        self.denied: list[Op] = []
        self.needs_approval: list[Op] = []

    @property
    def ok(self) -> bool:
        return not self.clip_errors

    def summary(self) -> str:
        return (
            f"{len(self.clip_errors)} clip errors, {len(self.denied)} denied, "
            f"{len(self.needs_approval)} awaiting approval"
        )


def validate_edl(
    edl: EDL, dna: EditDNA, *, autonomy: int = AUTONOMY_BUILD_AND_FLAG
) -> ValidationReport:
    """Decide every operation, and check every clip's media is actually usable.

    Mutates `edl` in place: each op ends up with its decision and full reason
    trail attached, so the EDL that gets stored is the one that explains itself.
    """
    report = ValidationReport()
    seen_media: dict[str, bool] = {}

    for clip in edl.all_clips():
        # Media availability is checked here, at plan time, where the message can
        # name the clip — rather than at XML write, where it surfaces as an
        # exception three layers from anything the user recognises.
        exists = seen_media.get(clip.source_path)
        if exists is None:
            exists = Path(clip.source_path).exists()
            seen_media[clip.source_path] = exists
        if not exists:
            report.clip_errors.append((clip.id, f"source media missing: {clip.source_path}"))

        if clip.source_in_frames < 0 or clip.duration_frames <= 0:
            report.clip_errors.append((clip.id, "clip has a non-positive duration"))

        conflicts = check_conflicts(clip)
        for op in clip.ops:
            reasons = evaluate_op(op, clip, dna, autonomy=autonomy)
            if conflict := conflicts.get(op.id):
                reasons.append(conflict)
            op.reasons = reasons
            op.decision = worst([r.decision for r in reasons])

            if op.decision == "DENY":
                report.denied.append(op)
            elif op.decision == "REQUIRE_APPROVAL":
                report.needs_approval.append(op)

    return report
