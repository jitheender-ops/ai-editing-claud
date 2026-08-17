"""
Power and thermal policy.

The MacBook Air M4 is fanless. Sustained video decode plus GPU inference
throttles it within minutes, so the real constraint on this pipeline is heat,
not speed — and the answer is to make work small and interruptible rather than
parallel. Hence two workers, not three, and `nice` on every one of them.

`WORKERS = 2` is measured-by-reasoning, not by benchmark: two decode processes
plus the OS keeps the package under sustained-throttle on an Air. Raise it on a
machine with a fan.
"""

import os
import subprocess

# ponytail: fixed worker count, no thermal feedback loop. If throttling still
# shows up in job durations, read `powermetrics` and scale down dynamically.
WORKERS = 2

#: Analysis must never make the UI stutter, so workers run below normal priority.
NICE = 10


def on_ac_power() -> bool:
    """True when plugged in. `--when-idle` refuses heavy work on battery."""
    try:
        out = subprocess.run(
            ["pmset", "-g", "batt"], capture_output=True, text=True, timeout=5
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return True  # Can't tell -> don't block the user's work.
    return "AC Power" in out


def deprioritise() -> None:
    """Call in a worker process before doing anything expensive."""
    try:
        os.nice(NICE)
    except OSError:
        pass  # Already niced, or not permitted. Not worth failing a job over.


def battery_percent() -> int | None:
    try:
        out = subprocess.run(
            ["pmset", "-g", "batt"], capture_output=True, text=True, timeout=5
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for token in out.replace(";", " ").split():
        if token.endswith("%"):
            try:
                return int(token[:-1])
            except ValueError:
                return None
    return None
