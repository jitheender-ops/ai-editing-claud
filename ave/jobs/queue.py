"""
Durable job queue.

Analysis jobs on this machine are long, expensive and interruptible: a fanless
Air throttles, a decode fails on one bad file, the lid closes. So anything
expensive goes through here — a row in `job_queue`, retried with exponential
backoff, dead-lettered after MAX_ATTEMPTS so one poisonous file never blocks the
work behind it. Jobs survive a restart because SQLite does.

The queue is a table rather than Redis or Celery on purpose. This is a
single-user application on one laptop, so a broker would add an install, a
daemon and a second source of truth to gain nothing.

Ported from commerce-os `events/queue.ts`, including the two decisions that make
it testable:

  * `run_due_jobs()` is callable directly, so tests drive the queue with no
    timers and no sleeping — the usual source of flaky queue tests.
  * `backoff_ms()` is exported, so the retry schedule is asserted in a test
    rather than described in a comment.

Failure classification lives with the handler, not here: raise
`PermanentJobError` for something a retry cannot fix (a corrupt file, a rejected
payload); anything else is treated as transient.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable

from ave.database.adapter import get_db
from ave.lib.ids import new_id, new_run_id
from ave.lib.log import error, log, warn

#: Attempts before a job is dead-lettered.
MAX_ATTEMPTS = 3

#: First retry delay; each further attempt doubles it.
BASE_BACKOFF_MS = 2_000


class PermanentJobError(Exception):
    """Raised by a handler when retrying cannot help. Skips straight to the DLQ."""


JobHandler = Callable[[dict[str, Any]], None]

_handlers: dict[str, JobHandler] = {}


def register_job_handler(kind: str, handler: JobHandler) -> None:
    _handlers[kind] = handler


def backoff_ms(attempts: int) -> int:
    """Delay before attempt `n` (1-indexed): 2s, 4s, 8s ...

    Exported so the schedule is asserted in tests rather than described here.
    """
    return BASE_BACKOFF_MS * 2 ** max(0, attempts - 1)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def enqueue(
    kind: str,
    payload: dict[str, Any],
    *,
    correlation_id: str | None = None,
    delay_ms: int = 0,
) -> str:
    job_id = new_id("job")
    now = _now_iso()
    get_db().run(
        """INSERT INTO job_queue (id, kind, payload, status, attempts, last_error,
                                  run_after, correlation_id, created_at, updated_at)
           VALUES (?, ?, ?, 'READY', 0, NULL, ?, ?, ?, ?)""",
        job_id,
        kind,
        json.dumps(payload),
        _now_ms() + delay_ms,
        correlation_id or new_run_id(),
        now,
        now,
    )
    return job_id


def run_due_jobs(limit: int = 10) -> int:
    """Runs every job that is due, once each. Returns how many were attempted."""
    due = _claim_due(limit)
    for job in due:
        _run_job(job)
    return len(due)


def _run_job(job: dict[str, Any]) -> None:
    handler = _handlers.get(job["kind"])
    if handler is None:
        # A job nobody handles is a wiring mistake, not a transient fault.
        # Failing it fast surfaces the bug instead of retrying it three times.
        _dead_letter(job, f'No handler registered for job kind "{job["kind"]}"')
        return

    try:
        handler(job["payload"])
    except PermanentJobError as exc:
        _dead_letter(job, str(exc))
    except Exception as exc:  # noqa: BLE001 — any handler failure is a queue concern
        if job["attempts"] >= MAX_ATTEMPTS:
            _dead_letter(job, str(exc))
            return

        # A vendor that tells us when to come back is more accurate than any
        # schedule we could compute, so `retry_after_ms` on the error wins.
        advised = getattr(exc, "retry_after_ms", None)
        delay = advised if isinstance(advised, int) and advised > 0 else backoff_ms(job["attempts"])
        get_db().run(
            """UPDATE job_queue
               SET status = 'READY', last_error = ?, run_after = ?, updated_at = ?
               WHERE id = ?""",
            str(exc),
            _now_ms() + delay,
            _now_iso(),
            job["id"],
        )
        warn(
            "job.retry",
            job=job["id"],
            kind=job["kind"],
            attempt=job["attempts"],
            retry_in_ms=delay,
            reason=str(exc),
        )
    else:
        get_db().run(
            "UPDATE job_queue SET status = 'DONE', updated_at = ? WHERE id = ?",
            _now_iso(),
            job["id"],
        )
        log("job.done", job=job["id"], kind=job["kind"])


def _claim_due(limit: int) -> list[dict[str, Any]]:
    """Marks jobs RUNNING and returns them. The status flip is what stops the next
    poll picking up a job still in flight — with one process and synchronous
    SQLite that is sufficient; a second worker process would need a claim token."""
    db = get_db()
    with db.transaction():
        rows = db.all(
            """SELECT * FROM job_queue
               WHERE status = 'READY' AND run_after <= ?
               ORDER BY run_after, rowid
               LIMIT ?""",
            _now_ms(),
            limit,
        )
        for row in rows:
            db.run(
                """UPDATE job_queue SET status = 'RUNNING', attempts = attempts + 1,
                   updated_at = ? WHERE id = ?""",
                _now_iso(),
                row["id"],
            )
    # attempts was just incremented; report the value the handler is running as.
    return [{**_map(r), "attempts": r["attempts"] + 1} for r in rows]


def _dead_letter(job: dict[str, Any], message: str) -> None:
    get_db().run(
        "UPDATE job_queue SET status = 'DEAD', last_error = ?, updated_at = ? WHERE id = ?",
        message,
        _now_iso(),
        job["id"],
    )
    error(
        "job.dead_letter",
        code="JOB_DEAD",
        job=job["id"],
        kind=job["kind"],
        attempts=job["attempts"],
        reason=message,
    )


def _map(row: dict[str, Any]) -> dict[str, Any]:
    return {**row, "payload": json.loads(row["payload"])}


# ── Reads ────────────────────────────────────────────────────────────────────


def list_jobs(status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    if status:
        rows = get_db().all(
            "SELECT * FROM job_queue WHERE status = ? ORDER BY created_at DESC LIMIT ?",
            status,
            limit,
        )
    else:
        rows = get_db().all("SELECT * FROM job_queue ORDER BY created_at DESC LIMIT ?", limit)
    return [_map(r) for r in rows]


def list_dead_letters(limit: int = 50) -> list[dict[str, Any]]:
    return list_jobs("DEAD", limit)


def retry_job(job_id: str) -> bool:
    """Puts a dead-lettered job back at the front of the queue with a clean slate."""
    changed = get_db().run(
        """UPDATE job_queue SET status = 'READY', attempts = 0, last_error = NULL,
           run_after = ?, updated_at = ? WHERE id = ? AND status = 'DEAD'""",
        _now_ms(),
        _now_iso(),
        job_id,
    )
    return changed > 0
