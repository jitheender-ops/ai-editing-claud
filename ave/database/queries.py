"""
Every SQL statement in the system lives here.

Keeping SQL in one module is the other half of the `Database` protocol's promise:
a Postgres adapter later means reviewing this file, not grepping the codebase for
string literals.
"""

from __future__ import annotations

import json
import time
from typing import Any

from ave.database.adapter import get_db
from ave.lib.ids import new_id


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# ── media ────────────────────────────────────────────────────────────────────


def get_media_by_hash(content_hash: str, kind: str) -> dict[str, Any] | None:
    row = get_db().get(
        "SELECT * FROM media WHERE content_hash = ? AND kind = ?", content_hash, kind
    )
    return _decode_media(row) if row else None


def upsert_media(
    *, path: str, content_hash: str, kind: str, probe: dict[str, Any], proxy_path: str | None
) -> tuple[str, bool]:
    """Returns (media_id, created). Existing media is *not* re-probed — that is the
    cache doing its job. Only the path is refreshed, so moving a file on disk
    relinks it instead of creating a duplicate row."""
    existing = get_media_by_hash(content_hash, kind)
    if existing:
        if existing["path"] != path:
            get_db().run("UPDATE media SET path = ? WHERE id = ?", path, existing["id"])
        if proxy_path and not existing.get("proxy_path"):
            get_db().run(
                "UPDATE media SET proxy_path = ? WHERE id = ?", proxy_path, existing["id"]
            )
        return existing["id"], False

    media_id = new_id("med")
    get_db().run(
        """INSERT INTO media (id, path, content_hash, kind, probe, proxy_path, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        media_id,
        path,
        content_hash,
        kind,
        json.dumps(probe),
        proxy_path,
        _now(),
    )
    return media_id, True


def set_proxy_path(media_id: str, proxy_path: str) -> None:
    get_db().run("UPDATE media SET proxy_path = ? WHERE id = ?", proxy_path, media_id)


def list_media(kind: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
    if kind:
        rows = get_db().all(
            "SELECT * FROM media WHERE kind = ? ORDER BY created_at DESC LIMIT ?", kind, limit
        )
    else:
        rows = get_db().all("SELECT * FROM media ORDER BY created_at DESC LIMIT ?", limit)
    return [_decode_media(r) for r in rows]


def count_media() -> int:
    row = get_db().get("SELECT COUNT(*) AS n FROM media")
    return int(row["n"]) if row else 0


def _decode_media(row: dict[str, Any]) -> dict[str, Any]:
    return {**row, "probe": json.loads(row["probe"])}


# ── analysis cache ───────────────────────────────────────────────────────────


def get_analysis(media_id: str, kind: str, analyzer_version: str) -> dict[str, Any] | None:
    """A hit here is why the same media is never analysed twice. Bumping
    `analyzer_version` invalidates one analyzer without touching the others."""
    row = get_db().get(
        """SELECT * FROM analysis
           WHERE media_id = ? AND kind = ? AND analyzer_version = ?""",
        media_id,
        kind,
        analyzer_version,
    )
    return {**row, "data": json.loads(row["data"])} if row else None


def save_analysis(
    *,
    media_id: str,
    kind: str,
    analyzer_version: str,
    data: dict[str, Any],
    duration_ms: int | None = None,
) -> str:
    analysis_id = new_id("anl")
    get_db().run(
        """INSERT OR REPLACE INTO analysis
             (id, media_id, kind, analyzer_version, data, duration_ms, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        analysis_id,
        media_id,
        kind,
        analyzer_version,
        json.dumps(data),
        duration_ms,
        _now(),
    )
    return analysis_id
