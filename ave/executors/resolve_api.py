"""
DaVinci Resolve scripting: capability probe (tier 2/3).

Nothing in this system depends on Resolve scripting. Tier 1 — writing a timeline
file that Resolve imports — is the path that always works, and it is chosen
precisely because this probe may come back negative:

  tier 1  write .fcpxml, user does File -> Import -> Timeline.  Always available.
  tier 2  a script under Workspace -> Scripts, running in Resolve's own Python.
          Not "external scripting", so it should work on the free edition.
  tier 3  external scripting from our own interpreter. Studio-only in practice.

This machine runs DaVinci Resolve 21.0.2 *free* (bundle id
com.blackmagic-design.DaVinciResolveLite), so tier 3 is expected to fail. The
probe exists to record that as a fact rather than an assumption, and to
distinguish the three reasons it can fail — Resolve not running, the edition
refusing the connection, or the native module not loading into this Python.

The import runs in a subprocess: fusionscript.so is a native extension built
against a specific CPython, and a bad load can abort the interpreter rather than
raise. A crash in a subprocess is a return code; a crash in-process would take
the CLI with it.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from ave.config import RESOLVE_APP, RESOLVE_SCRIPT_API, RESOLVE_SCRIPT_LIB, RESOLVE_USER_SCRIPTS

_PROBE = r"""
import sys
try:
    import DaVinciResolveScript as dvr
except BaseException as exc:
    print("IMPORT_FAILED:%s:%s" % (type(exc).__name__, str(exc)[:200]))
    raise SystemExit(0)
try:
    resolve = dvr.scriptapp("Resolve")
except BaseException as exc:
    print("SCRIPTAPP_FAILED:%s:%s" % (type(exc).__name__, str(exc)[:200]))
    raise SystemExit(0)
print("CONNECTED" if resolve else "NO_HANDLE")
"""


@dataclass
class ResolveStatus:
    app_installed: bool
    running: bool
    tier2_available: bool  # can we install a script into Resolve's Scripts menu
    tier3_available: bool  # can we drive Resolve from our own interpreter
    detail: str

    @property
    def summary(self) -> str:
        if self.tier3_available:
            return "tier 3 (external scripting) available"
        if self.tier2_available:
            return "tier 1 + 2 (file import, in-app script); tier 3 unavailable"
        return "tier 1 only (file import)"


def resolve_running() -> bool:
    result = subprocess.run(
        ["pgrep", "-f", "DaVinci Resolve"], capture_output=True, text=True
    )
    return result.returncode == 0


def probe() -> ResolveStatus:
    app_installed = RESOLVE_APP.exists()
    running = resolve_running()

    # Tier 2 needs only a writable scripts directory; it does not need Resolve
    # running, and it does not need Studio.
    tier2 = RESOLVE_SCRIPT_API.exists() and (
        RESOLVE_USER_SCRIPTS.exists() or RESOLVE_USER_SCRIPTS.parent.parent.exists()
    )

    if not RESOLVE_SCRIPT_LIB.exists():
        return ResolveStatus(
            app_installed, running, tier2, False,
            f"fusionscript.so not at {RESOLVE_SCRIPT_LIB}",
        )

    env = {
        "RESOLVE_SCRIPT_API": str(RESOLVE_SCRIPT_API),
        "RESOLVE_SCRIPT_LIB": str(RESOLVE_SCRIPT_LIB),
        "PYTHONPATH": str(RESOLVE_SCRIPT_API / "Modules"),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
    }
    try:
        result = subprocess.run(
            ["python3", "-c", _PROBE], capture_output=True, text=True, env=env, timeout=30
        )
    except subprocess.TimeoutExpired:
        return ResolveStatus(app_installed, running, tier2, False, "probe timed out after 30s")

    out = (result.stdout or "").strip().splitlines()
    marker = out[-1] if out else f"no output (exit {result.returncode})"

    if marker.startswith("CONNECTED"):
        return ResolveStatus(app_installed, running, tier2, True, "connected to a running Resolve")
    if marker.startswith("IMPORT_FAILED"):
        return ResolveStatus(
            app_installed, running, tier2, False,
            f"fusionscript did not load into this Python ({marker.split(':', 2)[-1]})",
        )
    if marker.startswith("NO_HANDLE") or marker.startswith("SCRIPTAPP_FAILED"):
        why = (
            "Resolve is not running, so this is inconclusive — start Resolve and re-run"
            if not running
            else "Resolve is running but refused the connection, which is the expected "
            "behaviour on the free edition (external scripting is Studio-only)"
        )
        return ResolveStatus(app_installed, running, tier2, False, why)
    return ResolveStatus(app_installed, running, tier2, False, f"unexpected probe result: {marker}")
