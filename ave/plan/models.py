"""
The Edit Decision List — the deterministic plan an executor turns into a timeline.

Four decisions make this schema work in practice.

**Integer frames everywhere.** FCPXML expresses time as exact rationals
(`1001/30000s`). Seconds are a float, and floats drift: accumulate a few hundred
cuts in seconds and clips land a frame apart, which QC then reports as an
"accidental gap" that is really a rounding bug. So every time in this file is an
integer frame count at the timeline rate, and conversion to rational happens once,
at XML write.

**Stable ids.** `Clip.id` and `Op.id` survive regeneration, which is what makes
"reduce zooms by 50%" a filter over ops — a patch — instead of a full re-plan
that would discard everything else you liked.

**Every op carries its reasons.** Not one sentence but the whole audit trail:
which check ran, what it decided, under which rule id, and the numbers it saw.
That is what turns "explainable" from a claim into something you can read.

**Ops are decided, never silently dropped.** An op the planner is unsure about
comes out `REQUIRE_APPROVAL` and lands in a review queue. The alternative — apply
it anyway, or quietly discard it — is how automated editing loses trust.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from ave.style.models import EditDNA

SCHEMA_VERSION = "1.0"

Decision = Literal["ALLOW", "REQUIRE_APPROVAL", "DENY"]
Check = Literal["SCHEMA", "MEDIA", "BOUNDS", "CONFLICT", "CONFIDENCE", "AUTONOMY"]
OpType = Literal["zoom", "pan", "crop", "caption", "sfx", "music", "transition", "speed", "freeze"]

#: Worst decision wins when several checks disagree.
SEVERITY: dict[str, int] = {"ALLOW": 0, "REQUIRE_APPROVAL": 1, "DENY": 2}


class Timebase(BaseModel):
    """Frame rate as an exact rational. 29.97 is 30000/1001, not 29.97."""

    fps_num: int = Field(gt=0)
    fps_den: int = Field(default=1, gt=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)

    @property
    def fps(self) -> float:
        """For display only. Never compute timeline positions from this."""
        return self.fps_num / self.fps_den

    def seconds_to_frames(self, seconds: float) -> int:
        return round(seconds * self.fps_num / self.fps_den)

    def frames_to_seconds(self, frames: int) -> float:
        return frames * self.fps_den / self.fps_num


class OpReason(BaseModel):
    check: Check
    decision: Decision
    rule_id: str
    message: str
    inputs: dict[str, Any] = Field(default_factory=dict)


class Op(BaseModel):
    id: str
    type: OpType
    params: dict[str, Any] = Field(default_factory=dict)
    at_frames: int | None = None  # relative to clip start; None = whole clip
    duration_frames: int | None = None
    confidence: float = Field(default=1.0, ge=0, le=1)
    priority: int = 0
    source: Literal["rule", "llm", "user"] = "rule"
    decision: Decision = "ALLOW"
    reasons: list[OpReason] = Field(default_factory=list)

    @property
    def applied(self) -> bool:
        return self.decision == "ALLOW"


class Clip(BaseModel):
    id: str
    source_media_id: str
    source_path: str
    source_in_frames: int = Field(ge=0)
    source_out_frames: int = Field(gt=0)
    timeline_start_frames: int = Field(ge=0)
    track: str = "V1"
    ops: list[Op] = Field(default_factory=list)
    #: Why this range survived the cut, in one human sentence.
    reason: str = ""

    @model_validator(mode="after")
    def _out_after_in(self) -> "Clip":
        if self.source_out_frames <= self.source_in_frames:
            raise ValueError(
                f"clip {self.id}: source_out ({self.source_out_frames}) must exceed "
                f"source_in ({self.source_in_frames})"
            )
        return self

    @property
    def duration_frames(self) -> int:
        return self.source_out_frames - self.source_in_frames

    @property
    def timeline_end_frames(self) -> int:
        return self.timeline_start_frames + self.duration_frames


class Marker(BaseModel):
    """Written into the Resolve timeline so the reason for a cut is readable
    while scrubbing. Costs almost nothing and is the most direct form the
    explainability requirement can take."""

    frame: int = Field(ge=0)
    name: str
    note: str = ""
    color: str = "blue"


class Track(BaseModel):
    name: str = "V1"
    kind: Literal["video", "audio", "caption"] = "video"
    clips: list[Clip] = Field(default_factory=list)


class Summary(BaseModel):
    source_duration_s: float = 0.0
    output_duration_s: float = 0.0
    clip_count: int = 0
    removed_s: float = 0.0
    #: Findings from planning that a human needs, in plain English. An empty plan
    #: is useless without the reason it came out empty.
    diagnostics: list[str] = Field(default_factory=list)

    @property
    def kept_ratio(self) -> float:
        return self.output_duration_s / self.source_duration_s if self.source_duration_s else 0.0


class EDL(BaseModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    project: str
    version: int = 1
    seed: int = 0
    style_name: str = "Default"
    dna_schema_version: str = "1.0"
    #: The exact style this plan was built from. Carried rather than referenced by
    #: name so a stored plan stays reproducible even after the style is edited —
    #: "every version is preserved" is only true if the inputs are preserved too.
    dna: EditDNA | None = None
    timebase: Timebase
    #: hash(footage + DNA + target). Same inputs and seed must give the same EDL.
    inputs_hash: str = ""
    tracks: list[Track] = Field(default_factory=list)
    markers: list[Marker] = Field(default_factory=list)
    summary: Summary = Field(default_factory=Summary)

    def track(self, name: str) -> Track | None:
        return next((t for t in self.tracks if t.name == name), None)

    def all_clips(self) -> list[Clip]:
        return [clip for track in self.tracks for clip in track.clips]

    def all_ops(self) -> list[Op]:
        return [op for clip in self.all_clips() for op in clip.ops]

    def pending_approval(self) -> list[Op]:
        return [op for op in self.all_ops() if op.decision == "REQUIRE_APPROVAL"]


def worst(decisions: list[Decision]) -> Decision:
    """Worst decision wins — the same reduction commerce-os uses so that no single
    passing check can mask a failing one."""
    if not decisions:
        return "ALLOW"
    return max(decisions, key=lambda d: SEVERITY[d])
