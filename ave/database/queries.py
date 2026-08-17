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


# ── styles ───────────────────────────────────────────────────────────────────


def upsert_style(name: str, dna: dict[str, Any], *, category: str | None = None) -> str:
    """Styles are versioned by row, never overwritten — an edit built against
    version 2 must still be reproducible after version 3 exists."""
    row = get_db().get(
        "SELECT * FROM styles WHERE name = ? ORDER BY version DESC LIMIT 1", name
    )
    version = (row["version"] + 1) if row else 1
    style_id = new_id("sty")
    get_db().run(
        """INSERT INTO styles (id, name, version, parent_id, dna, dna_schema_version,
                               category, tags, source_media_ids, confidence, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, '[]', ?, ?, ?)""",
        style_id,
        name,
        version,
        row["id"] if row else None,
        json.dumps(dna),
        dna.get("schema_version", "1.0"),
        category,
        json.dumps(dna.get("derived_from", [])),
        json.dumps(dna.get("confidence", {})),
        _now(),
    )
    return style_id


def get_style(name: str, version: int | None = None) -> dict[str, Any] | None:
    if version is None:
        row = get_db().get(
            "SELECT * FROM styles WHERE name = ? ORDER BY version DESC LIMIT 1", name
        )
    else:
        row = get_db().get("SELECT * FROM styles WHERE name = ? AND version = ?", name, version)
    return {**row, "dna": json.loads(row["dna"])} if row else None


def list_styles() -> list[dict[str, Any]]:
    rows = get_db().all(
        """SELECT * FROM styles WHERE (name, version) IN
             (SELECT name, MAX(version) FROM styles GROUP BY name)
           ORDER BY name"""
    )
    return [{**r, "dna": json.loads(r["dna"])} for r in rows]


# ── projects and plans ───────────────────────────────────────────────────────


def upsert_project(
    name: str, *, style_id: str | None = None, footage_dir: str | None = None,
    target: dict[str, Any] | None = None, autonomy: int = 2,
) -> str:
    existing = get_db().get("SELECT * FROM projects WHERE name = ?", name)
    if existing:
        return existing["id"]
    project_id = new_id("prj")
    get_db().run(
        """INSERT INTO projects (id, name, style_id, footage_dir, target, autonomy, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        project_id,
        name,
        style_id,
        footage_dir,
        json.dumps(target or {}),
        autonomy,
        _now(),
    )
    return project_id


def get_project(name: str) -> dict[str, Any] | None:
    row = get_db().get("SELECT * FROM projects WHERE name = ?", name)
    return {**row, "target": json.loads(row["target"])} if row else None


def next_plan_version(project_id: str) -> int:
    row = get_db().get(
        "SELECT MAX(version) AS v FROM plans WHERE project_id = ?", project_id
    )
    return (row["v"] or 0) + 1 if row else 1


def save_plan(
    *, project_id: str, version: int, seed: int, edl: dict[str, Any],
    qc: dict[str, Any] | None, origin: str, run_id: str,
    origin_detail: str | None = None, parent_version: int | None = None,
) -> str:
    """Plans are append-only. Nothing here ever updates a previous version, which
    is what makes every earlier edit recoverable."""
    plan_id = new_id("pln")
    get_db().run(
        """INSERT INTO plans (id, project_id, version, parent_version, seed, edl, qc,
                              origin, origin_detail, run_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        plan_id,
        project_id,
        version,
        parent_version,
        seed,
        json.dumps(edl),
        json.dumps(qc) if qc else None,
        origin,
        origin_detail,
        run_id,
        _now(),
    )
    return plan_id


def get_plan(project_id: str, version: int) -> dict[str, Any] | None:
    row = get_db().get(
        "SELECT * FROM plans WHERE project_id = ? AND version = ?", project_id, version
    )
    if not row:
        return None
    return {**row, "edl": json.loads(row["edl"]), "qc": json.loads(row["qc"]) if row["qc"] else None}


def list_plans(project_id: str) -> list[dict[str, Any]]:
    rows = get_db().all(
        "SELECT id, version, parent_version, origin, origin_detail, created_at "
        "FROM plans WHERE project_id = ? ORDER BY version",
        project_id,
    )
    return rows


# ── approvals ────────────────────────────────────────────────────────────────


def save_approvals(plan_id: str, ops: list[dict[str, Any]]) -> int:
    for op in ops:
        get_db().run(
            """INSERT INTO approvals (id, plan_id, op_id, reasons, state, created_at)
               VALUES (?, ?, ?, ?, 'PENDING', ?)""",
            new_id("apr"),
            plan_id,
            op["id"],
            json.dumps(op.get("reasons", [])),
            _now(),
        )
    return len(ops)


def list_approvals(state: str = "PENDING") -> list[dict[str, Any]]:
    rows = get_db().all(
        "SELECT * FROM approvals WHERE state = ? ORDER BY created_at DESC", state
    )
    return [{**r, "reasons": json.loads(r["reasons"])} for r in rows]


def set_approval_state(approval_id: str, state: str) -> bool:
    return get_db().run(
        "UPDATE approvals SET state = ?, decided_at = ? WHERE id = ? AND state = 'PENDING'",
        state,
        _now(),
        approval_id,
    ) > 0
