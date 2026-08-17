"""
Natural-language feedback, as deterministic edits to a plan.

"Make it faster." "Reduce zooms by 50%." "Remove the zooms." These are turned
into parameter changes by a rule table, not by a model. Two reasons, and the
second is the one that matters:

  * These commands are a small, closed vocabulary. A regex table handles them
    exactly, every time, and can say precisely what it changed.
  * A model in this position would be nondeterministic at the point where the
    user is trying to converge on something. "Reduce zooms by 50%" must halve
    the zooms, not usually halve them.

The important distinction is between a **patch** and a **replan**:

  patch    Operates on the existing EDL, keeping every clip id. Your cuts stay
           exactly where they were and only the named thing changes. This is what
           makes iterating feel like adjusting rather than rerolling.
  replan   Changes the style and cuts again from scratch. Necessary when the
           request is about pacing, because where the cuts fall *is* the pacing.

Anything the table does not recognise is reported as unrecognised. Guessing at an
unclear instruction and silently doing something adjacent is worse than saying so.

Known limitation: a replan recomputes everything, punch-ins included, so a
zoom patch made in an earlier version is not carried forward through a
subsequent "make it faster". Every version is preserved, so nothing is lost —
but the two commands do not compose, and the fix is to re-apply the patch after
the replan. Making them compose properly means replaying the patch history onto
each new plan, which is worth doing once there are more patch types than zooms.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from ave.plan.models import EDL
from ave.style.models import EditDNA


@dataclass
class Change:
    """What a command did, in terms a human can check."""

    kind: str  # "patch" | "replan"
    description: str
    dna: EditDNA | None = None  # set when kind == "replan"
    touched_ops: int = 0


@dataclass
class FeedbackResult:
    changes: list[Change] = field(default_factory=list)
    unrecognised: list[str] = field(default_factory=list)

    @property
    def needs_replan(self) -> bool:
        return any(change.kind == "replan" for change in self.changes)

    @property
    def ok(self) -> bool:
        return bool(self.changes)


#: How much a "faster"/"slower" request moves the dead-air tolerance. Chosen so
#: one command makes an audible difference but three do not collapse the edit.
PACE_STEP = 0.6


def _percent(text: str, default: float) -> float:
    match = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
    return float(match.group(1)) / 100 if match else default


def apply_feedback(command: str, edl: EDL, dna: EditDNA) -> FeedbackResult:
    """Interpret one instruction. `edl` is patched in place where applicable."""
    result = FeedbackResult()
    text = command.lower().strip()
    if not text:
        result.unrecognised.append(command)
        return result

    for pattern, handler in _RULES:
        if re.search(pattern, text):
            change = handler(text, edl, dna)
            if change:
                result.changes.append(change)
            return result

    result.unrecognised.append(command)
    return result


# ── handlers ─────────────────────────────────────────────────────────────────


def _faster(text: str, edl: EDL, dna: EditDNA) -> Change:
    updated = dna.model_copy(deep=True)
    before = updated.pacing.dead_air_tolerance_s
    updated.pacing.dead_air_tolerance_s = round(max(0.05, before * PACE_STEP), 3)
    return Change(
        kind="replan",
        dna=updated,
        description=(
            f"dead-air tolerance {before:.2f}s -> "
            f"{updated.pacing.dead_air_tolerance_s:.2f}s (cuts tighten)"
        ),
    )


def _slower(text: str, edl: EDL, dna: EditDNA) -> Change:
    updated = dna.model_copy(deep=True)
    before = updated.pacing.dead_air_tolerance_s
    updated.pacing.dead_air_tolerance_s = round(before / PACE_STEP, 3)
    return Change(
        kind="replan",
        dna=updated,
        description=(
            f"dead-air tolerance {before:.2f}s -> "
            f"{updated.pacing.dead_air_tolerance_s:.2f}s (more pauses survive)"
        ),
    )


def _scale_zooms(text: str, edl: EDL, dna: EditDNA) -> Change:
    """Scale how *far* each punch-in travels, not the raw scale value.

    A zoom of 1.20 reduced by 50% is 1.10, not 0.60 — the travel is the 0.20
    above an untouched frame. Halving the value itself would turn every punch-in
    into a zoom *out*, which is the kind of bug that looks correct in a diff.
    """
    factor = _percent(text, 0.5)
    if re.search(r"\b(increase|more|stronger|bigger)\b", text):
        multiplier = 1 + factor
        verb = "increased"
    else:
        multiplier = 1 - factor
        verb = "reduced"

    touched = 0
    for op in edl.all_ops():
        if op.type != "zoom":
            continue
        value = op.params.get("value")
        if not isinstance(value, (int, float)):
            continue
        op.params["value"] = round(1.0 + (value - 1.0) * multiplier, 4)
        touched += 1

    return Change(
        kind="patch",
        touched_ops=touched,
        description=f"{touched} punch-ins {verb} by {factor:.0%} (cuts unchanged)",
    )


def _remove_zooms(text: str, edl: EDL, dna: EditDNA) -> Change:
    touched = 0
    for clip in edl.all_clips():
        before = len(clip.ops)
        clip.ops = [op for op in clip.ops if op.type != "zoom"]
        touched += before - len(clip.ops)
    return Change(
        kind="patch",
        touched_ops=touched,
        description=f"removed {touched} punch-ins (cuts unchanged)",
    )


def _more_zooms(text: str, edl: EDL, dna: EditDNA) -> Change:
    updated = dna.model_copy(deep=True)
    before = updated.motion.punch_in_rate_per_minute
    updated.motion.punch_in_rate_per_minute = round(max(1.0, before) * 1.5, 2)
    return Change(
        kind="replan",
        dna=updated,
        description=(
            f"punch-in rate {before:.1f} -> "
            f"{updated.motion.punch_in_rate_per_minute:.1f} per minute"
        ),
    )


def _minimal_transitions(text: str, edl: EDL, dna: EditDNA) -> Change:
    updated = dna.model_copy(deep=True)
    updated.transitions.style = "minimal"
    return Change(kind="replan", dna=updated, description="transitions set to minimal")


Handler = Callable[[str, EDL, EditDNA], Change]

#: Order matters — the first match wins, so more specific patterns come first.
_RULES: list[tuple[str, Handler]] = [
    (r"\b(remove|no|drop|kill)\b.{0,20}\b(zoom|punch)", _remove_zooms),
    (r"\b(zoom|punch).{0,20}\b(remove|off)\b", _remove_zooms),
    (r"\b(reduce|decrease|less|increase|more|stronger|bigger)\b.{0,20}\b(zoom|punch)", _scale_zooms),
    (r"\b(zoom|punch)\w*\b.{0,20}\b(by|to)\b.{0,10}\d+\s*%", _scale_zooms),
    (r"\bpunchier\b", _more_zooms),
    (r"\b(faster|tighter|snappier|aggressive|tighten|speed up)\b", _faster),
    (r"\b(slower|looser|breathe|relax|calmer|loosen)\b", _slower),
    (r"\b(minimal|fewer|simpler)\b.{0,20}\btransition", _minimal_transitions),
]
