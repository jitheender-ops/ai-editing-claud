"""
Transcript-aware refinement of cut ranges.

Silence detection alone knows where sound stops, not where *language* stops, and
that difference is audible. Three things a transcript makes possible:

**Never cut inside a word.** A silence boundary lands wherever the level crossed
a threshold, which is frequently mid-syllable — the detector cannot tell the
quiet part of a word from a pause. Truncating a word is the single most obvious
artefact of an automated edit, so any cut point that falls inside a word is moved
outward to that word's edge. Outward, not to the nearest boundary: the choice is
between keeping a whole word and losing a whole word, and keeping it is right.

**Remove fillers.** "Um" and "uh" carry no content and their removal is the first
thing an editor does by hand. Word timings make it exact rather than approximate.

**Prefer sentence boundaries.** When a cut already lands near the end of a
sentence, moving it onto the boundary makes the edit sound deliberate rather than
merely tight. Only *near* ones move — dragging a cut across half a sentence to
reach a full stop would change the content, not the polish.

Order matters and is fixed: excise fillers, then snap outward to word edges, then
prefer sentences, then re-merge. Snapping before excision would widen ranges back
over the fillers just removed.
"""

from __future__ import annotations

from dataclasses import dataclass

from ave.media.silence import Range
from ave.media.transcribe import Transcript, Word

#: A cut this close to a sentence end gets moved onto it. Beyond this, moving the
#: cut would remove or add words rather than tidy a boundary.
SENTENCE_SNAP_S = 0.45


@dataclass
class Refinement:
    ranges: list[Range]
    fillers_removed: int = 0
    words_rescued: int = 0
    sentences_snapped: int = 0

    @property
    def notes(self) -> list[str]:
        out = []
        if self.fillers_removed:
            out.append(f"removed {self.fillers_removed} filler words using the transcript")
        if self.words_rescued:
            out.append(f"moved {self.words_rescued} cut points off mid-word boundaries")
        if self.sentences_snapped:
            out.append(f"aligned {self.sentences_snapped} cuts to sentence ends")
        return out


def subtract(ranges: list[Range], holes: list[Range]) -> list[Range]:
    """Remove `holes` from `ranges`. A hole inside a range splits it in two."""
    out: list[Range] = []
    for start, end in ranges:
        pieces = [(start, end)]
        for hole_start, hole_end in holes:
            remaining: list[Range] = []
            for piece_start, piece_end in pieces:
                if hole_end <= piece_start or hole_start >= piece_end:
                    remaining.append((piece_start, piece_end))
                    continue
                if hole_start > piece_start:
                    remaining.append((piece_start, hole_start))
                if hole_end < piece_end:
                    remaining.append((hole_end, piece_end))
            pieces = remaining
        out.extend(pieces)
    return out


def merge_overlaps(ranges: list[Range], min_gap: float = 0.0) -> list[Range]:
    merged: list[Range] = []
    for start, end in sorted(ranges):
        if merged and start - merged[-1][1] <= min_gap:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _spans(word: Word, at: float) -> bool:
    """True when `at` falls strictly inside the word, not on its edges."""
    return word.start < at < word.end


def refine(
    ranges: list[Range],
    transcript: Transcript | None,
    *,
    remove_fillers: bool = True,
    min_duration_s: float = 0.4,
) -> Refinement:
    if transcript is None or not transcript.words:
        return Refinement(ranges=ranges)

    words = transcript.words
    result = Refinement(ranges=list(ranges))

    if remove_fillers:
        holes = [(w.start, w.end) for w in words if w.is_filler]
        if holes:
            before = len(result.ranges)
            result.ranges = subtract(result.ranges, holes)
            result.fillers_removed = len(holes)
            # A filler mid-sentence splits one range into two; that split is the
            # jump cut which removes it.
            _ = before

    # Snap outward off mid-word boundaries. Fillers are excluded so a boundary
    # that was just created by removing one is not widened back over it.
    keepable = [w for w in words if not (remove_fillers and w.is_filler)]
    snapped: list[Range] = []
    for start, end in result.ranges:
        for word in keepable:
            if _spans(word, start):
                start = word.start
                result.words_rescued += 1
            if _spans(word, end):
                end = word.end
                result.words_rescued += 1
        if end > start:
            snapped.append((start, end))
    result.ranges = snapped

    # Prefer sentence ends, but only when one is already close.
    sentence_ends = [s.end for s in transcript.segments]
    if sentence_ends:
        aligned: list[Range] = []
        for start, end in result.ranges:
            for boundary in sentence_ends:
                if 0 < abs(boundary - end) <= SENTENCE_SNAP_S and boundary > start:
                    end = boundary
                    result.sentences_snapped += 1
                    break
            aligned.append((start, end))
        result.ranges = aligned

    result.ranges = merge_overlaps(result.ranges)
    result.ranges = [(s, e) for s, e in result.ranges if e - s >= min_duration_s]
    return result
