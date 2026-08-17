"""
Deterministic randomness.

Every stochastic choice in the planner — tie-breaks between equally-scored
B-roll candidates, jitter so punch-ins aren't mechanically identical — draws
from the seed stored on the plan row. Regenerating v003 then reproduces it
byte for byte, which is what makes "the same inputs give the same edit" true
rather than aspirational.

commerce-os ships a mulberry32 class because JS has no seedable RNG. Python's
`random.Random` already is one, so this is a wrapper, not a reimplementation.
Never use the module-level `random.*` functions in planning code — they draw
from shared global state and would break reproducibility.
"""

import random

__all__ = ["rng"]


def rng(seed: int) -> random.Random:
    """A generator private to one plan. `random.Random` gives us random(),
    randint(), choice(), choices() (weighted), shuffle() and uniform()."""
    return random.Random(seed)


def demo() -> None:
    assert [rng(7).random() for _ in range(3)] == [rng(7).random() for _ in range(3)]
    assert rng(7).random() != rng(8).random()
    # Sequences, not just first draws, must match — this is what regeneration relies on.
    a, b = rng(42), rng(42)
    assert [a.randint(0, 999) for _ in range(50)] == [b.randint(0, 999) for _ in range(50)]
    print("rng ok")


if __name__ == "__main__":
    demo()
