"""
Prefixed, roughly-sortable identifiers: `op_lz4k1c_8f2a`.

The millisecond timestamp comes first so ids sort chronologically in logs and
`ORDER BY id` is close enough to `ORDER BY created_at` for debugging. The random
suffix makes a collision inside the same millisecond a non-issue.

Stable ids are load-bearing here, not cosmetic: "reduce zooms by 50%" is a
filter over op ids, so an op that keeps its id across a regeneration is what
makes incremental feedback possible instead of a full re-plan.
"""

import random
import time
import uuid

_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def new_id(prefix: str) -> str:
    return f"{prefix}_{_b36(int(time.time() * 1000))}_{uuid.uuid4().hex[:4]}"


def new_run_id() -> str:
    """Correlation id tying one trigger to every downstream job and log line."""
    return new_id("run")


def new_seed() -> int:
    """A fresh seed to store on a new plan, so regenerating it is byte-identical."""
    return random.SystemRandom().getrandbits(32)


def _b36(n: int) -> str:
    out = ""
    while n:
        n, r = divmod(n, 36)
        out = _B36[r] + out
    return out or "0"


def demo() -> None:
    assert _b36(0) == "0"
    assert _b36(35) == "z"
    assert _b36(36) == "10"
    a, b = new_id("op"), new_id("op")
    assert a != b, "ids must be unique inside one millisecond"
    assert a.startswith("op_") and len(a.split("_")) == 3
    # Base36 sorts lexicographically only at equal width, which holds for every
    # timestamp between 36^7 (year 1972) and 36^8 (year 2059) — so ids minted in
    # this system's lifetime are all 8 digits and all sort correctly.
    assert len(_b36(int(time.time() * 1000))) == 8
    assert _b36(1_000_000_000_000) < _b36(2_000_000_000_000)
    print("ids ok")


if __name__ == "__main__":
    demo()
