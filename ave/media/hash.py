"""
Content hashing for the analysis cache.

Hashes the file size plus its first and last 4 MB, not the whole file. Reading
40 GB of card footage to decide whether to skip work is itself the cost the cache
exists to avoid — and for video the head carries container metadata while the
tail moves on any re-encode, trim or re-export, so a change that matters is
essentially always visible in one of the three.

The tradeoff is explicit rather than hidden: a file edited only in its middle,
to exactly the same byte length, hashes the same. Camera originals and screen
recordings are written once and never patched in place, so that case does not
arise for the media this tool ingests.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

CHUNK = 4 * 1024 * 1024


def content_hash(path: Path | str) -> str:
    # ponytail: head+tail+size, not a full read. If this ever needs to hash files
    # that are edited in place, switch to full-file blake2b and eat the I/O.
    p = Path(path)
    size = p.stat().st_size
    digest = hashlib.blake2b(digest_size=16)
    digest.update(str(size).encode())

    with p.open("rb") as handle:
        if size <= 2 * CHUNK:
            digest.update(handle.read())
        else:
            digest.update(handle.read(CHUNK))
            handle.seek(-CHUNK, 2)
            digest.update(handle.read(CHUNK))

    return digest.hexdigest()
