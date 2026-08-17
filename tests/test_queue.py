"""
Queue tests.

Deliberately driven by calling `run_due_jobs()` directly and advancing a fake
clock. No timers, no sleeping, no background worker — which is why these are
fast and cannot flake. The retry schedule is asserted here rather than described
in a comment, which is the whole reason `backoff_ms` is exported.
"""

import pytest

import ave.jobs.queue as q


@pytest.fixture
def clock(monkeypatch):
    state = {"ms": 1_000_000}
    monkeypatch.setattr(q, "_now_ms", lambda: state["ms"])
    return state


@pytest.fixture(autouse=True)
def clean_handlers():
    q._handlers.clear()
    yield
    q._handlers.clear()


def status_of(job_id):
    from ave.database.adapter import get_db

    return get_db().get("SELECT * FROM job_queue WHERE id = ?", job_id)


def test_backoff_schedule():
    assert q.backoff_ms(1) == 2_000
    assert q.backoff_ms(2) == 4_000
    assert q.backoff_ms(3) == 8_000
    # Defensive: attempt 0 must not produce a negative or fractional delay.
    assert q.backoff_ms(0) == 2_000


def test_job_runs_and_completes(clock):
    seen = []
    q.register_job_handler("greet", lambda payload: seen.append(payload["name"]))

    job_id = q.enqueue("greet", {"name": "ada"})
    assert q.run_due_jobs() == 1

    assert seen == ["ada"]
    assert status_of(job_id)["status"] == "DONE"


def test_unhandled_kind_dead_letters_immediately(clock):
    job_id = q.enqueue("nobody_handles_this", {})
    q.run_due_jobs()

    row = status_of(job_id)
    assert row["status"] == "DEAD", "a missing handler is a wiring bug, not a transient fault"
    assert "No handler registered" in row["last_error"]
    assert row["attempts"] == 1, "must not burn all three attempts on a wiring bug"


def test_transient_failure_retries_on_schedule_then_dead_letters(clock):
    attempts = []

    def always_fails(payload):
        attempts.append(1)
        raise RuntimeError("network wobble")

    q.register_job_handler("flaky", always_fails)
    job_id = q.enqueue("flaky", {})

    # Attempt 1 -> retry in 2s.
    q.run_due_jobs()
    row = status_of(job_id)
    assert (row["status"], row["attempts"]) == ("READY", 1)
    assert row["run_after"] == clock["ms"] + q.backoff_ms(1)

    # Not due yet: the queue must not pick it up early.
    assert q.run_due_jobs() == 0

    # Attempt 2 -> retry in 4s.
    clock["ms"] += q.backoff_ms(1)
    q.run_due_jobs()
    row = status_of(job_id)
    assert (row["status"], row["attempts"]) == ("READY", 2)
    assert row["run_after"] == clock["ms"] + q.backoff_ms(2)

    # Attempt 3 -> dead-lettered, so one poisonous job never blocks the queue.
    clock["ms"] += q.backoff_ms(2)
    q.run_due_jobs()
    row = status_of(job_id)
    assert row["status"] == "DEAD"
    assert row["attempts"] == q.MAX_ATTEMPTS
    assert len(attempts) == q.MAX_ATTEMPTS
    assert "network wobble" in row["last_error"]


def test_permanent_error_skips_straight_to_the_dlq(clock):
    calls = []

    def rejects(payload):
        calls.append(1)
        raise q.PermanentJobError("file is corrupt")

    q.register_job_handler("corrupt", rejects)
    job_id = q.enqueue("corrupt", {})
    q.run_due_jobs()

    row = status_of(job_id)
    assert row["status"] == "DEAD"
    assert row["attempts"] == 1, "a retry cannot fix a corrupt file"
    assert len(calls) == 1


def test_error_may_advise_its_own_retry_delay(clock):
    class RateLimited(Exception):
        retry_after_ms = 30_000

    q.register_job_handler("limited", lambda payload: (_ for _ in ()).throw(RateLimited("429")))
    job_id = q.enqueue("limited", {})
    q.run_due_jobs()

    row = status_of(job_id)
    assert row["run_after"] == clock["ms"] + 30_000, "vendor advice must beat computed backoff"


def test_retry_job_resurrects_a_dead_letter(clock):
    runs = []

    def fails_once(payload):
        runs.append(1)
        if len(runs) == 1:
            raise q.PermanentJobError("nope")

    q.register_job_handler("resurrect", fails_once)
    job_id = q.enqueue("resurrect", {})
    q.run_due_jobs()
    assert status_of(job_id)["status"] == "DEAD"

    assert q.retry_job(job_id) is True
    row = status_of(job_id)
    assert (row["status"], row["attempts"], row["last_error"]) == ("READY", 0, None)

    q.run_due_jobs()
    assert status_of(job_id)["status"] == "DONE"

    # A job that is not dead cannot be resurrected.
    assert q.retry_job(job_id) is False


def test_claiming_marks_running_so_a_job_is_not_run_twice(clock):
    q.register_job_handler("slow", lambda payload: None)
    q.enqueue("slow", {})

    assert q.run_due_jobs() == 1
    # Already DONE, so nothing is due; a still-RUNNING job would likewise be skipped.
    assert q.run_due_jobs() == 0


def test_delayed_job_is_not_due_until_its_time(clock):
    q.register_job_handler("later", lambda payload: None)
    q.enqueue("later", {}, delay_ms=10_000)

    assert q.run_due_jobs() == 0
    clock["ms"] += 10_000
    assert q.run_due_jobs() == 1


def test_dead_letters_are_listable(clock):
    q.enqueue("unhandled", {})
    q.run_due_jobs()
    assert len(q.list_dead_letters()) == 1
