"""
Operation limits.

Numbers that bound what an automated edit is allowed to do, kept apart from the
logic that enforces them so a limit can be changed without reading the gate.

These are ceilings, not targets. A style's DNA asks for a zoom; this decides
whether the ask is sane.
"""

from __future__ import annotations

#: Below this, an op is not applied without a human looking at it. Set where it
#: is: B-roll matching and emphasis detection are the two subsystems that produce
#: confident-sounding nonsense, and both sit under this line when unsure.
CONFIDENCE_FLOOR = 0.55

#: Below this an op is not worth showing to anyone.
CONFIDENCE_DENY = 0.2

OP_LIMITS: dict[str, dict[str, float]] = {
    # A punch-in beyond ~1.4x on 1080p source is visibly soft.
    "zoom": {"min": 1.0, "max": 1.4},
    # Normalised frame units; beyond this the subject leaves frame.
    "pan": {"min": -0.5, "max": 0.5},
    "crop": {"min": 0.0, "max": 0.45},
    # Speed changes outside this read as an error rather than an effect.
    "speed": {"min": 0.25, "max": 4.0},
    "freeze": {"min": 0.1, "max": 5.0},
}

#: A transition shorter than this is a cut with extra steps; longer drags.
TRANSITION_FRAMES = {"min": 2, "max": 48}

#: Autonomy ceilings, matching commerce-os: a lower level can only tighten the
#: outcome, never loosen it.
AUTONOMY_PROPOSE_ONLY = 1
AUTONOMY_BUILD_AND_FLAG = 2
AUTONOMY_AUTONOMOUS = 3
