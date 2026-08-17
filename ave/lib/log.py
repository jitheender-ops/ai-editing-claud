"""
Structured logging.

Two shapes from one call site: a readable line for a human at a terminal, and
one JSON object per line when `AVE_LOG_JSON=1` for the debug mode the spec asks
for (AI requests, timings, Resolve commands). Every line carries the run_id, so
one trigger is traceable through ingest -> analyze -> plan -> execute.

Errors always carry a `code`. "Never silently fail" means a caller can branch on
the code, not parse a sentence.
"""

import json
import os
import sys
import time
from contextvars import ContextVar

_run_id: ContextVar[str] = ContextVar("run_id", default="-")

_JSON = os.environ.get("AVE_LOG_JSON") == "1"


def set_run_id(run_id: str) -> None:
    _run_id.set(run_id)


def get_run_id() -> str:
    return _run_id.get()


def log(event: str, level: str = "info", **fields: object) -> None:
    if _JSON:
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "level": level,
            "event": event,
            "run": _run_id.get(),
            **fields,
        }
        print(json.dumps(record, default=str), file=sys.stderr, flush=True)
        return

    extra = " ".join(f"{k}={v}" for k, v in fields.items())
    mark = {"info": " ", "warn": "!", "error": "x"}.get(level, " ")
    print(f"{mark} {event}{' ' + extra if extra else ''}", file=sys.stderr, flush=True)


def warn(event: str, **fields: object) -> None:
    log(event, level="warn", **fields)


def error(event: str, code: str, **fields: object) -> None:
    """`code` is required: a caller must be able to branch without parsing prose."""
    log(event, level="error", code=code, **fields)
