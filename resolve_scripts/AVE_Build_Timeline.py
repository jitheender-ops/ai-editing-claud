"""
AVE — Build Timeline (run from inside DaVinci Resolve)

Install to:
    ~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility/
then run it from Workspace -> Scripts -> AVE_Build_Timeline.

Why this exists. DaVinci Resolve's *external* scripting is a Studio feature, and
this machine runs the free edition. But a script placed in the Scripts folder runs
inside Resolve's own Python, which is not external scripting — so this path works
on the free edition, at the cost of being started from Resolve's menu instead of
from a button in the app.

It is also an independent route to a timeline. The FCPXML writer is the primary
path; this one drives the API directly and never parses XML at all, so if Resolve
turns out to be fussy about some corner of the FCPXML dialect, the edit can still
be built. The two paths share nothing but the plan.

Standard library only. Resolve's bundled Python has no third-party packages and
cannot be given any, which is why the plan it reads is flat JSON rather than the
project's pydantic models.

Known API limits, designed around rather than fought:
  * There is no transition API in Resolve at all — the word does not appear once
    in Blackmagic's scripting README. Dissolves can only come from the FCPXML path.
  * SetProperty exposes static transforms only, so punch-ins are applied as a
    fixed zoom per clip. That is what a jump-cut punch-in is; an animated ramp
    needs the FCPXML path or a Fusion comp.
"""

import glob
import json
import os
import sys

PLAN_DIR = os.path.expanduser("~/Library/Application Support/ave/builds")


def get_resolve():
    """Resolve injects `resolve` when a script runs from the Scripts menu; the
    import is the fallback for the console and for external use on Studio."""
    if "resolve" in globals():
        return globals()["resolve"]
    try:
        import DaVinciResolveScript as dvr

        return dvr.scriptapp("Resolve")
    except ImportError:
        return None


def newest_plan():
    plans = sorted(glob.glob(os.path.join(PLAN_DIR, "*.plan.json")), key=os.path.getmtime)
    return plans[-1] if plans else None


def fail(message):
    print("AVE: " + message)
    return 1


def main():
    resolve_app = get_resolve()
    if resolve_app is None:
        return fail(
            "could not reach Resolve. Run this from Workspace -> Scripts inside "
            "DaVinci Resolve; external scripting needs Studio."
        )

    path = sys.argv[1] if len(sys.argv) > 1 else newest_plan()
    if not path or not os.path.exists(path):
        return fail("no plan found in %s. Run `ave edit` first." % PLAN_DIR)

    with open(path) as handle:
        plan = json.load(handle)
    if plan.get("schema") != "ave-resolve-plan/1":
        return fail("unrecognised plan schema: %s" % plan.get("schema"))

    print("AVE: building %s from %s" % (plan["timeline_name"], os.path.basename(path)))

    project_manager = resolve_app.GetProjectManager()
    project = project_manager.GetCurrentProject()
    if project is None:
        return fail("no project is open. Create or open one first.")

    media_pool = project.GetMediaPool()

    # Import each distinct source once and keep the mapping, so a plan with fifty
    # clips off one file does not import that file fifty times.
    pool_items = {}
    for clip in plan["clips"]:
        source = clip["path"]
        if source in pool_items:
            continue
        if not os.path.exists(source):
            return fail("source media is missing: %s" % source)
        imported = media_pool.ImportMedia([source])
        if not imported:
            return fail("Resolve refused to import %s" % source)
        pool_items[source] = imported[0]

    clip_infos = []
    for clip in plan["clips"]:
        clip_infos.append(
            {
                "mediaPoolItem": pool_items[clip["path"]],
                "startFrame": int(clip["start_frame"]),
                "endFrame": int(clip["end_frame"]),
            }
        )

    if not clip_infos:
        return fail("the plan contains no clips")

    # A new timeline every time, never a modification of an existing one. That is
    # the safety rule: an automated edit must never be able to destroy work.
    timeline = media_pool.CreateTimelineFromClips(plan["timeline_name"], clip_infos)
    if timeline is None:
        return fail(
            "Resolve would not create the timeline. A timeline named %s may already "
            "exist — rename or delete it and run again." % plan["timeline_name"]
        )

    items = timeline.GetItemListInTrack("video", 1) or []
    applied = 0
    for clip, item in zip(plan["clips"], items):
        zoom = clip.get("zoom")
        if zoom:
            # Static zoom: SetProperty has no keyframe support, so this is a
            # punch-in held for the clip rather than a ramp.
            item.SetProperty("ZoomX", float(zoom))
            item.SetProperty("ZoomY", float(zoom))
            applied += 1

    markers = 0
    for marker in plan.get("markers", []):
        ok = timeline.AddMarker(
            int(marker["frame"]),
            marker.get("color", "Blue"),
            marker.get("name", "AVE"),
            marker.get("note", ""),
            1,
        )
        if ok:
            markers += 1

    print(
        "AVE: built %s — %d clips, %d punch-ins, %d markers"
        % (plan["timeline_name"], len(clip_infos), applied, markers)
    )
    print("AVE: your existing timelines were not touched.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
