"""
Style similarity.

Compares two Edit DNAs section by section and reports how alike they are. The
intended use is checking whether a generated edit came out in the style it was
asked for — and, later, comparing two references to see what actually differs.

The honesty rule from the rest of the system applies with particular force here,
because a similarity score is exactly the kind of number people quote without
reading the caveats. Two *unmeasured* sections are identical, since both hold the
same defaults — reporting that as 100% similar would be worse than useless. So a
section where either side lacks a real measurement is reported as **not
comparable** rather than as a perfect match, and it is excluded from the overall
score instead of inflating it.

Fields are compared as normalised distances rather than raw differences: 0.2s
apart means something very different for shot duration than for dead-air
tolerance, and a percentage of a sensible scale travels between them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ave.style.models import EditDNA

#: A section needs at least this much confidence on both sides to be compared.
COMPARABLE_CONFIDENCE = 0.15


@dataclass
class SectionScore:
    name: str
    score: float | None  # None = not comparable
    detail: list[str] = field(default_factory=list)
    reason: str = ""

    @property
    def comparable(self) -> bool:
        return self.score is not None


@dataclass
class SimilarityReport:
    sections: list[SectionScore] = field(default_factory=list)
    a_name: str = ""
    b_name: str = ""

    @property
    def comparable_sections(self) -> list[SectionScore]:
        return [s for s in self.sections if s.comparable]

    @property
    def overall(self) -> float | None:
        """Mean of the sections that could actually be compared.

        None when nothing could be — which is a real answer, not a zero.
        """
        scored = self.comparable_sections
        if not scored:
            return None
        return round(sum(s.score for s in scored) / len(scored), 3)

    def render(self) -> str:
        lines = []
        overall = self.overall
        if overall is None:
            lines.append("Overall: not comparable — neither style has enough measured")
        else:
            lines.append(f"Overall: {overall:.0%}")
        lines.append("")
        for section in self.sections:
            if section.comparable:
                bar = "#" * int(section.score * 20)
                lines.append(f"  {section.name:<12} {section.score:>5.0%}  {bar}")
            else:
                lines.append(f"  {section.name:<12}     -  not comparable ({section.reason})")
        return "\n".join(lines)


def _closeness(a: float, b: float, scale: float) -> float:
    """1.0 when identical, falling to 0 as the gap approaches `scale`.

    `scale` is what counts as "completely different" for that quantity, which is
    why it is passed per field rather than assumed.
    """
    if scale <= 0:
        return 1.0
    return max(0.0, 1.0 - abs(a - b) / scale)


def _section(
    name: str, a: EditDNA, b: EditDNA, comparisons: list[tuple[str, float, float, float]]
) -> SectionScore:
    """`comparisons` is (label, a_value, b_value, scale)."""
    a_confidence = a.confidence_of(name)
    b_confidence = b.confidence_of(name)
    if a_confidence < COMPARABLE_CONFIDENCE or b_confidence < COMPARABLE_CONFIDENCE:
        weakest = a.style_name if a_confidence < b_confidence else b.style_name
        return SectionScore(
            name=name,
            score=None,
            reason=f"not measured in '{weakest}'",
        )

    scores = []
    detail = []
    for label, left, right, scale in comparisons:
        value = _closeness(left, right, scale)
        scores.append(value)
        detail.append(f"{label}: {left:g} vs {right:g} -> {value:.0%}")

    return SectionScore(
        name=name,
        score=round(sum(scores) / len(scores), 3) if scores else None,
        detail=detail,
    )


def compare(a: EditDNA, b: EditDNA) -> SimilarityReport:
    report = SimilarityReport(a_name=a.style_name, b_name=b.style_name)

    report.sections.append(
        _section(
            "pacing", a, b,
            [
                # 3s apart in average shot length is a completely different feel.
                ("average shot", a.pacing.average_shot_duration_s, b.pacing.average_shot_duration_s, 3.0),
                ("median shot", a.pacing.median_shot_duration_s, b.pacing.median_shot_duration_s, 3.0),
                ("cuts/min", a.pacing.cuts_per_minute, b.pacing.cuts_per_minute, 40.0),
                ("dead air", a.pacing.dead_air_tolerance_s, b.pacing.dead_air_tolerance_s, 1.0),
            ],
        )
    )
    report.sections.append(
        _section(
            "motion", a, b,
            [
                ("punch-ins/min", a.motion.punch_in_rate_per_minute, b.motion.punch_in_rate_per_minute, 20.0),
                ("zoom travel", a.motion.zoom_range[1] - a.motion.zoom_range[0],
                 b.motion.zoom_range[1] - b.motion.zoom_range[0], 0.4),
                ("static ratio", a.motion.static_ratio, b.motion.static_ratio, 1.0),
            ],
        )
    )
    report.sections.append(
        _section(
            "audio", a, b,
            [("loudness", a.audio.integrated_lufs, b.audio.integrated_lufs, 12.0)],
        )
    )
    report.sections.append(
        _section(
            "color", a, b,
            [
                ("saturation", a.color.saturation_mean, b.color.saturation_mean, 0.5),
                ("contrast", a.color.contrast, b.color.contrast, 0.5),
                ("warmth", a.color.temperature_bias, b.color.temperature_bias, 0.3),
            ],
        )
    )
    # Transitions are deliberately not scored. The analyser measures transition
    # *density*, which is cut density under another name — the very same number
    # already compared under pacing. Scoring it here would count one signal twice
    # and quietly drag the overall figure toward whatever pacing said. Until
    # transition *types* are actually detected, this section has no independent
    # information to contribute, and saying so is more useful than a number.
    report.sections.append(
        SectionScore(
            name="transitions",
            score=None,
            reason="only cut density is measured, which pacing already covers",
        )
    )
    report.sections.append(
        _section(
            "captions", a, b,
            [
                ("words/card", float(a.captions.words_per_card), float(b.captions.words_per_card), 8.0),
                ("height", a.captions.height_ratio, b.captions.height_ratio, 0.1),
            ],
        )
    )
    return report
