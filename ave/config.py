"""
Configuration and secrets.

Paths are constants because they are verified facts about this machine, not
guesses — in particular RESOLVE_SCRIPT_LIB, which is *not* the path in
Blackmagic's own README. Their docs say:

    /Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/...

and that directory does not exist on a normal install; the app sits directly in
/Applications. Copying the documented path verbatim is the single most likely
way to get "Could not locate module dependencies" and blame the wrong thing.

Secrets are never hardcoded and never written to the repo: environment variable
first, then the macOS keychain. That covers "secure configuration storage"
without taking a dependency on a keyring library.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

AVE_HOME = Path(os.environ.get("AVE_HOME", Path.home() / "Library/Application Support/ave"))
DB_PATH = AVE_HOME / "ave.db"
PROXY_DIR = AVE_HOME / "proxies"
BUILD_DIR = AVE_HOME / "builds"  # generated .fcpxml, caption overlays, QC reports

REPO_ROOT = Path(__file__).resolve().parent.parent
RESEARCH_DIR = REPO_ROOT / "research"

# ── DaVinci Resolve ──────────────────────────────────────────────────────────
RESOLVE_APP = Path("/Applications/DaVinci Resolve.app")
RESOLVE_SCRIPT_API = Path(
    "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting"
)
#: Corrected for the real install layout — see the module docstring.
RESOLVE_SCRIPT_LIB = RESOLVE_APP / "Contents/Libraries/Fusion/fusionscript.so"
#: Scripts here appear under Workspace -> Scripts and run in Resolve's own Python,
#: which is how tier 2 works on the free edition without external scripting.
RESOLVE_USER_SCRIPTS = (
    Path.home()
    / "Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility"
)

# ── Media analysis ───────────────────────────────────────────────────────────
PROXY_HEIGHT = 480  # analyse proxies, never originals
PROXY_CRF = 28
SAMPLE_FPS = 4.0  # frames per second handed to computer vision


def ensure_dirs() -> None:
    for d in (AVE_HOME, PROXY_DIR, BUILD_DIR):
        d.mkdir(parents=True, exist_ok=True)


def tool(name: str) -> str | None:
    return shutil.which(name)


def secret(name: str) -> str | None:
    """Environment first, then the macOS keychain. Returns None if unset —
    callers decide whether that is fatal, because the free-tier LLM path and the
    offline analysis path both need to work without any key at all."""
    if value := os.environ.get(name):
        return value
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", f"ave:{name}", "-w"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None
