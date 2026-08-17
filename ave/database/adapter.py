"""
Database adapter.

One implementation ships: SQLite through the stdlib `sqlite3`. `Database` is the
seam a hosted Postgres adapter would implement later, and it is deliberately
narrow — all / get / run / exec / transaction — so a remote adapter is a small
surface rather than a rewrite. Keeping SQL in `queries.py` and out of the rest of
the codebase is the other half of that promise.

Ported from commerce-os `database/db.ts`, including its nested-transaction rule:
an inner `transaction()` joins the outer one rather than failing, so a helper
that wraps its own writes stays composable inside a larger transaction.

`PRAGMA foreign_keys` is per-connection in SQLite, not stored in the file, so it
is set on every connect — running it once in schema.sql would silently do
nothing for every later connection.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Protocol

Row = dict[str, Any]

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


class Database(Protocol):
    def all(self, sql: str, *params: Any) -> list[Row]: ...
    def get(self, sql: str, *params: Any) -> Row | None: ...
    def run(self, sql: str, *params: Any) -> int: ...
    def exec(self, sql: str) -> None: ...
    @contextmanager
    def transaction(self) -> Iterator[None]: ...


class SqliteDatabase:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None puts us in autocommit mode so `transaction()` owns
        # BEGIN/COMMIT explicitly instead of fighting the driver's implicit one.
        self._conn = sqlite3.connect(self.path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._depth = 0

    def all(self, sql: str, *params: Any) -> list[Row]:
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def get(self, sql: str, *params: Any) -> Row | None:
        row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    def run(self, sql: str, *params: Any) -> int:
        """Returns rows changed, which is how callers detect a no-op UPDATE."""
        return self._conn.execute(sql, params).rowcount

    def exec(self, sql: str) -> None:
        self._conn.executescript(sql)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        if self._depth > 0:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return

        self._depth += 1
        self._conn.execute("BEGIN")
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")
        finally:
            self._depth -= 1

    def migrate(self) -> None:
        # ponytail: schema.sql is idempotent (every statement is IF NOT EXISTS), so
        # "migration" is replaying it. Add a schema_version table the first time a
        # change needs to alter or backfill an existing column.
        self.exec(SCHEMA_PATH.read_text())

    def close(self) -> None:
        self._conn.close()


_db: SqliteDatabase | None = None


def get_db(path: Path | str | None = None) -> SqliteDatabase:
    """Process-wide handle. Pass a path on first call, or let config decide."""
    global _db
    if _db is None:
        if path is None:
            from ave.config import DB_PATH

            path = DB_PATH
        _db = SqliteDatabase(path)
        _db.migrate()
    return _db


def reset_db_for_tests(path: Path | str) -> SqliteDatabase:
    global _db
    if _db is not None:
        _db.close()
    _db = SqliteDatabase(path)
    _db.migrate()
    return _db
