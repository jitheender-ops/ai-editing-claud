"""
Edit DNA — a reference video's editing style, as numbers.

Two rules hold this schema together.

**Every measured field carries a confidence.** Half of what an editor does cannot
be recovered reliably from a rendered video: font family is not recoverable at
all, speed ramps are barely detectable, and an SFX can be classified by onset
shape but never identified. A profile that quietly guesses at those is worse than
one that says it does not know, because the planner would then apply the guess
with full conviction.

**`notes` says out loud what could not be measured.** It is a feature, not a
disclaimer — it is how you know whether to trust a style before you build with it.

Sections are independent so that "pacing from A, captions from B" is a section
copy rather than a merge algorithm.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1.0"

Level = Literal["none", "low", "medium", "high"]


class Pacing(BaseModel):
    """Shot rhythm. The only section M1 uses, and the only one it measures."""

    average_shot_duration_s: float = Field(default=3.0, gt=0)
    median_shot_duration_s: float = Field(default=3.0, gt=0)
    cuts_per_minute: float = Field(default=20.0, ge=0)

    #: Silence longer than this gets cut. The single most load-bearing number in
    #: the whole system for a talking-head edit.
    dead_air_tolerance_s: float = Field(default=0.35, ge=0)

    #: Padding kept either side of a kept range, so cuts do not clip breaths and
    #: consonants. Asymmetric because trailing padding reads far more natural.
    lead_in_s: float = Field(default=0.08, ge=0)
    lead_out_s: float = Field(default=0.18, ge=0)

    #: A kept segment shorter than this is not worth a cut; merge it instead.
    min_clip_duration_s: float = Field(default=0.4, gt=0)

    hook_duration_s: float = Field(default=0.0, ge=0)
    intro_length_s: float = Field(default=0.0, ge=0)
    #: Relative cut density per 10% bucket of the runtime. Empty until measured.
    pacing_curve: list[float] = Field(default_factory=list)


class Motion(BaseModel):
    punch_in_rate_per_minute: float = Field(default=0.0, ge=0)
    zoom_range: tuple[float, float] = (1.0, 1.15)
    static_vs_moving_ratio: float = Field(default=1.0, ge=0)


class Captions(BaseModel):
    enabled: bool = False
    words_per_card: int = Field(default=3, ge=1)
    position_norm: tuple[float, float] = (0.5, 0.82)
    height_ratio: float = Field(default=0.055, gt=0)
    all_caps: bool = True
    animation: Literal["none", "pop", "slide", "karaoke"] = "karaoke"
    highlight_color: str = "#FFE953"


class Audio(BaseModel):
    integrated_lufs: float = -14.0
    music_presence_ratio: float = Field(default=0.0, ge=0, le=1)
    sfx_per_minute: float = Field(default=0.0, ge=0)


class Transitions(BaseModel):
    #: Modern fast-paced editing is hard cuts, and Resolve's scripting API cannot
    #: add a transition at all, so "minimal" is both the common case and the one
    #: that survives every execution path.
    style: Literal["minimal", "moderate", "heavy"] = "minimal"
    density_per_minute: float = Field(default=0.0, ge=0)


class EditDNA(BaseModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    style_name: str
    derived_from: list[str] = Field(default_factory=list)  # content hashes

    pacing: Pacing = Field(default_factory=Pacing)
    motion: Motion = Field(default_factory=Motion)
    captions: Captions = Field(default_factory=Captions)
    audio: Audio = Field(default_factory=Audio)
    transitions: Transitions = Field(default_factory=Transitions)

    #: Per-section, 0..1. A section absent here has not been measured at all.
    confidence: dict[str, float] = Field(default_factory=dict)
    #: Plain English, for a human: what this profile does *not* know.
    notes: list[str] = Field(default_factory=list)

    def confidence_of(self, section: str) -> float:
        return self.confidence.get(section, 0.0)


def default_dna(name: str = "Default") -> EditDNA:
    """A neutral, honest starting profile.

    Every number here is a sensible default rather than a measurement, so the
    confidences are zero and `notes` says so. M3 replaces this with a profile
    measured from a real reference.
    """
    return EditDNA(
        style_name=name,
        confidence={},
        notes=[
            "Not measured from any reference — these are neutral defaults.",
            "Run `ave reference add <file>` (M3) to derive a real profile.",
        ],
    )
