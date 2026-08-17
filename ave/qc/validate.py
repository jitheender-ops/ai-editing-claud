"""
Quality control on a finished plan.

Runs before anything is written, because every problem here is cheaper to catch
in the EDL than in Resolve. The distinction that matters is between an *error* —
the timeline is wrong and will not behave — and a *warning*, where the timeline is
valid but the edit may not be what you wanted. Only errors block a build.

Deliberately not checked yet: black frames, audio clipping and caption overflow
all need the rendered result, so they arrive with the render pass. Claiming them
here would mean reporting "no black frames found" without having looked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ave.plan.models import EDL

#: A cut faster than this is almost always a detector artefact rather than intent.
RAPID_CUT_S = 0.25

#: Keeping less than this of the source usually means the noise floor is wrong.
SUSPICIOUS_KEPT_RATIO = 0.15


@dataclass
class QCReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def confidence(self) -> float:
        """A blunt score: errors dominate, warnings shade it."""
        return round(max(0.0, 1.0 - 0.4 * len(self.errors) - 0.05 * len(self.warnings)), 2)

    def to_dict(self) -> dict:
        return {
            "errors": self.errors,
            "warnings": self.warnings,
            "recommendations": self.recommendations,
            "confidence": self.confidence,
            "ok": self.ok,
        }

    def render(self) -> str:
        lines = []
        for item in self.errors:
            lines.append(f"  ERROR    {item}")
        for item in self.warnings:
            lines.append(f"  warning  {item}")
        for item in self.recommendations:
            lines.append(f"  hint     {item}")
        return "\n".join(lines) or "  clean"


def run_qc(edl: EDL) -> QCReport:
    report = QCReport()
    timebase = edl.timebase
    clips = sorted(edl.all_clips(), key=lambda c: c.timeline_start_frames)

    if not clips:
        report.errors.append("plan contains no clips — nothing would be built")
        # The planner's diagnosis of *why* is the actionable half.
        report.errors.extend(edl.summary.diagnostics)
        return report

    seen_missing: set[str] = set()
    for clip in clips:
        if clip.duration_frames <= 0:
            report.errors.append(f"{clip.id}: zero-length clip")
        if clip.source_in_frames < 0:
            report.errors.append(f"{clip.id}: negative source in-point")
        if clip.source_path not in seen_missing and not Path(clip.source_path).exists():
            seen_missing.add(clip.source_path)
            report.errors.append(f"missing media: {clip.source_path}")

    # Contiguity. A gap is black frames on the timeline; an overlap is one clip
    # silently covering another. Both are almost always frame-arithmetic bugs.
    for previous, current in zip(clips, clips[1:]):
        delta = current.timeline_start_frames - previous.timeline_end_frames
        if delta > 0:
            report.errors.append(
                f"{delta} frame gap between {previous.id} and {current.id} "
                f"(would render as black)"
            )
        elif delta < 0:
            report.errors.append(
                f"{-delta} frame overlap between {previous.id} and {current.id}"
            )

    rapid = [c for c in clips if timebase.frames_to_seconds(c.duration_frames) < RAPID_CUT_S]
    if rapid:
        report.warnings.append(
            f"{len(rapid)} clips are shorter than {RAPID_CUT_S:g}s — likely detector artefacts"
        )
        report.recommendations.append(
            "raise pacing.min_clip_duration_s, or lower the silence threshold (--noise)"
        )

    kept = edl.summary.kept_ratio
    if 0 < kept < SUSPICIOUS_KEPT_RATIO:
        report.warnings.append(
            f"only {kept:.0%} of the source was kept — the noise floor may be too high"
        )
        report.recommendations.append("try a lower --noise value, e.g. -35 or -40 dB")
    elif kept > 0.98:
        report.warnings.append(
            f"{kept:.0%} of the source was kept — almost nothing was cut"
        )
        report.recommendations.append(
            "try a higher --noise value, or lower pacing.dead_air_tolerance_s"
        )

    if edl.summary.output_duration_s < 1.0:
        report.errors.append(
            f"output is only {edl.summary.output_duration_s:.2f}s long"
        )

    denied = [op for op in edl.all_ops() if op.decision == "DENY"]
    if denied:
        report.warnings.append(f"{len(denied)} operations were denied by the validation gate")
    if pending := edl.pending_approval():
        report.recommendations.append(
            f"{len(pending)} operations await your approval — run `ave approvals`"
        )

    return report
