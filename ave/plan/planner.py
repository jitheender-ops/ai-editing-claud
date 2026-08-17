"""
The cut planner.

Turns silence into an Edit Decision List, honouring the style's pacing numbers.
Everything here is deterministic arithmetic: given the same media, the same DNA
and the same seed, it produces a byte-identical EDL. No model is consulted — the
LLM's job (from M5) is to label *which sentences matter*, never to decide where a
frame boundary falls.

The pipeline, in order, and each step exists because skipping it produces a
visibly worse edit:

  detect silence   -> at the style's dead-air tolerance, so natural beats survive
  invert           -> the ranges worth keeping
  pad              -> breaths and consonants live either side of speech; cutting
                      flush to the detector's boundary clips them audibly
  merge            -> padding can make neighbours touch, and two clips with no
                      gap between them is a cut the viewer sees for no reason
  drop tiny        -> a 200 ms island is a glitch, not a shot
  lay end to end   -> integer frames, accumulated, so nothing drifts
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from ave.lib.ids import new_id
from ave.lib.rng import rng
from ave.media.ffmpeg import summarise
from ave.media.silence import Range, detect_silence, measure_loudness, speech_ranges
from ave.plan.models import EDL, Clip, Marker, Op, Summary, Timebase, Track
from ave.style.models import EditDNA

PLANNER_VERSION = "1.0"


@dataclass
class PlanInputs:
    media_id: str
    path: str
    probe: dict
    dna: EditDNA
    project: str
    version: int = 1
    seed: int = 0
    noise_db: float = -30.0


def timebase_from_probe(probe: dict) -> Timebase:
    info = probe.get("summary") or summarise(probe)
    fps_num, fps_den = info["fps_num"], info["fps_den"]
    if fps_num <= 0:
        # Audio-only or a container that hides its rate. 30fps is arbitrary but
        # must be *something* for frame maths; the timeline is retimed on import.
        fps_num, fps_den = 30, 1
    return Timebase(
        fps_num=fps_num,
        fps_den=fps_den,
        width=info["width"] or 1920,
        height=info["height"] or 1080,
    )


def pad_and_merge(
    ranges: list[Range], *, lead_in: float, lead_out: float, duration: float, min_gap: float
) -> list[Range]:
    """Pad each range, then merge any that now touch or nearly touch.

    Merging is not cosmetic. Without it, padding turns a 0.4 s pause into two
    clips separated by nothing, which is a cut the viewer registers as a glitch.
    """
    padded = [
        (max(0.0, start - lead_in), min(duration, end + lead_out)) for start, end in ranges
    ]
    merged: list[Range] = []
    for start, end in sorted(padded):
        if merged and start - merged[-1][1] <= min_gap:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def plan_cut(inputs: PlanInputs) -> EDL:
    dna = inputs.dna
    pacing = dna.pacing
    timebase = timebase_from_probe(inputs.probe)
    info = inputs.probe.get("summary") or summarise(inputs.probe)
    duration = info["duration_s"]

    silences = detect_silence(
        inputs.path, noise_db=inputs.noise_db, min_duration_s=pacing.dead_air_tolerance_s
    )
    kept = speech_ranges(silences, duration)
    kept = pad_and_merge(
        kept,
        lead_in=pacing.lead_in_s,
        lead_out=pacing.lead_out_s,
        duration=duration,
        # Anything shorter than a frame-ish gap is not a cut worth making.
        min_gap=pacing.lead_in_s + pacing.lead_out_s,
    )
    kept = [(s, e) for s, e in kept if e - s >= pacing.min_clip_duration_s]

    # An empty plan is worthless without the reason. Measuring loudness costs an
    # extra ffmpeg pass, so it happens only on this failure path — where the
    # difference between "your audio is silent" and "your threshold is wrong" is
    # the whole answer.
    diagnostics: list[str] = []
    if not kept:
        loudness = measure_loudness(inputs.path)
        integrated = loudness.get("integrated_lufs")
        if integrated is not None and integrated < -60:
            diagnostics.append(
                f"The entire source is silent: integrated loudness is {integrated:.0f} LUFS "
                f"(anything below about -60 is digital silence). This footage has no audio "
                f"to cut on — silence-based editing cannot work on it."
            )
        else:
            level = f"{integrated:.0f} LUFS" if integrated is not None else "unknown"
            diagnostics.append(
                f"Nothing survived the cut: every segment fell below the {inputs.noise_db:g} dB "
                f"threshold or under the {pacing.min_clip_duration_s:g}s minimum clip length. "
                f"Source loudness is {level}; try a lower --noise value."
            )

    clips: list[Clip] = []
    markers: list[Marker] = []
    cursor = 0  # timeline position, in frames

    for index, (start_s, end_s) in enumerate(kept):
        source_in = timebase.seconds_to_frames(start_s)
        source_out = timebase.seconds_to_frames(end_s)
        if source_out <= source_in:
            continue  # rounded away to nothing at this frame rate

        removed_before = start_s - (kept[index - 1][1] if index else 0.0)
        reason = (
            f"Kept {end_s - start_s:.2f}s of speech; "
            f"{removed_before:.2f}s of silence removed before it "
            f"(style dead-air tolerance {pacing.dead_air_tolerance_s:.2f}s)"
        )

        clips.append(
            Clip(
                id=new_id("clip"),
                source_media_id=inputs.media_id,
                source_path=inputs.path,
                source_in_frames=source_in,
                source_out_frames=source_out,
                timeline_start_frames=cursor,
                track="V1",
                reason=reason,
            )
        )
        if index and removed_before > 0:
            markers.append(
                Marker(
                    frame=cursor,
                    name=f"cut -{removed_before:.2f}s",
                    note=reason,
                    color="blue",
                )
            )
        cursor += source_out - source_in

    output_s = timebase.frames_to_seconds(cursor)
    edl = EDL(
        project=inputs.project,
        version=inputs.version,
        seed=inputs.seed,
        style_name=dna.style_name,
        dna_schema_version=dna.schema_version,
        timebase=timebase,
        inputs_hash=inputs_hash(inputs),
        tracks=[Track(name="V1", kind="video", clips=clips)],
        markers=markers,
        summary=Summary(
            source_duration_s=round(duration, 3),
            output_duration_s=round(output_s, 3),
            clip_count=len(clips),
            removed_s=round(max(0.0, duration - output_s), 3),
            diagnostics=diagnostics,
        ),
    )
    add_punch_ins(edl, dna, seed=inputs.seed)
    return edl


def add_punch_ins(edl: EDL, dna: EditDNA, *, seed: int) -> int:
    """Apply the style's punch-in behaviour to the cut.

    Two properties matter here and both come from decisions made earlier.

    It is reproducible: every random choice — which clips, how much zoom — is
    drawn from `random.Random(seed)` with the seed stored on the plan, so
    regenerating a version reproduces it exactly rather than merely similarly.

    And it is self-limiting: the confidence of each operation is inherited from
    how well motion was actually measured in the reference. A style derived from
    footage where motion could not be tracked produces punch-ins below the
    approval floor, so they queue for a human instead of being applied on the
    strength of a guess. Nothing extra is needed to get that behaviour — the
    validation gate already does it.
    """
    motion = dna.motion
    if motion.punch_in_rate_per_minute <= 0:
        return 0

    clips = edl.all_clips()
    if not clips:
        return 0

    timebase = edl.timebase
    minutes = timebase.frames_to_seconds(
        sum(c.duration_frames for c in clips)
    ) / 60
    wanted = int(round(motion.punch_in_rate_per_minute * minutes))
    if wanted <= 0:
        return 0

    generator = rng(seed)
    low, high = motion.zoom_range
    confidence = dna.confidence_of("motion")

    # Longest clips first: a punch-in needs room to be read as a choice rather
    # than a glitch. Ties broken by id so the order never depends on dict
    # iteration or on which clips happen to be equal length.
    candidates = sorted(clips, key=lambda c: (-c.duration_frames, c.id))[:wanted]

    added = 0
    for clip in candidates:
        # Jitter so a run of punch-ins is not mechanically identical, which reads
        # as an automated effect rather than an edit.
        value = round(generator.uniform(low, high), 4)
        if value <= 1.0:
            continue
        clip.ops.append(
            Op(
                id=new_id("op"),
                type="zoom",
                params={"value": value},
                confidence=confidence,
                priority=10,
                source="rule",
                reasons=[],
            )
        )
        added += 1

    return added


def inputs_hash(inputs: PlanInputs) -> str:
    """Identifies the inputs that produced a plan.

    Same media, same style, same planner version and seed must give the same
    hash — and therefore the same EDL. A change to the planner itself counts,
    which is why PLANNER_VERSION is in here.
    """
    digest = hashlib.blake2b(digest_size=16)
    for part in (
        Path(inputs.path).name,
        inputs.dna.model_dump_json(),
        str(inputs.seed),
        PLANNER_VERSION,
        f"{inputs.noise_db}",
    ):
        digest.update(part.encode())
    return digest.hexdigest()
