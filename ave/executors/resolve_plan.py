"""
The plan handed to the in-Resolve companion script (tier 2).

A deliberately flat, dependency-free JSON: the script that reads it runs inside
DaVinci Resolve's own bundled Python, where pydantic is not installed and cannot
be. So the EDL is flattened here, where the schema lives, rather than the
companion script having to understand it.

Only ALLOW-ed operations are written out, for the same reason they are the only
ones that reach the FCPXML: an operation awaiting approval has not been approved.
"""

from __future__ import annotations

import json
from pathlib import Path

from ave.plan.models import EDL


def to_plan_dict(edl: EDL) -> dict:
    timebase = edl.timebase
    clips = []
    for clip in sorted(edl.all_clips(), key=lambda c: c.timeline_start_frames):
        zoom = next(
            (
                op.params["value"]
                for op in clip.ops
                if op.type == "zoom" and op.applied and isinstance(op.params.get("value"), (int, float))
            ),
            None,
        )
        clips.append(
            {
                "id": clip.id,
                "path": str(Path(clip.source_path).absolute()),
                # Resolve's clipInfo takes source frames; endFrame is inclusive,
                # which is why one is subtracted here rather than in the script.
                "start_frame": clip.source_in_frames,
                "end_frame": clip.source_out_frames - 1,
                "timeline_start": clip.timeline_start_frames,
                "zoom": zoom,
                "reason": clip.reason,
            }
        )

    return {
        "schema": "ave-resolve-plan/1",
        "project": edl.project,
        "version": edl.version,
        "timeline_name": f"AI_EDIT_v{edl.version:03d}",
        "fps": round(timebase.fps, 6),
        "width": timebase.width,
        "height": timebase.height,
        "clips": clips,
        "markers": [
            {"frame": m.frame, "name": m.name, "note": m.note, "color": m.color}
            for m in edl.markers
        ],
    }


def write_plan(edl: EDL, path: Path | str) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(to_plan_dict(edl), indent=2), encoding="utf-8")
    return out
