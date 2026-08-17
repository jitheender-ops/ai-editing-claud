"""
Adapter and cache tests.

The nested-transaction rule is the subtle one: an inner `transaction()` joins the
outer one instead of opening a second, so a helper that wraps its own writes stays
safe to call from inside a larger transaction. If that regressed, a failure deep
in a call chain would commit half its work.
"""

import pytest

from ave.database.adapter import get_db
from ave.database.queries import get_analysis, save_analysis, upsert_media
from ave.lib.ids import demo as ids_demo
from ave.lib.rng import demo as rng_demo


def _media() -> str:
    media_id, _ = upsert_media(
        path="/tmp/x.mov", content_hash="deadbeef", kind="source", probe={}, proxy_path=None
    )
    return media_id


def test_lib_self_checks():
    ids_demo()
    rng_demo()


def test_rollback_on_exception():
    db = get_db()
    with pytest.raises(RuntimeError):
        with db.transaction():
            _media()
            raise RuntimeError("boom")

    assert db.get("SELECT COUNT(*) AS n FROM media")["n"] == 0


def test_nested_transaction_joins_the_outer_one():
    db = get_db()
    with pytest.raises(RuntimeError):
        with db.transaction():
            _media()
            with db.transaction():  # inner scope must not commit independently
                pass
            raise RuntimeError("boom")

    assert db.get("SELECT COUNT(*) AS n FROM media")["n"] == 0, (
        "the inner transaction must not have committed the outer one's work"
    )


def test_successful_transaction_commits():
    db = get_db()
    with db.transaction():
        _media()
    assert db.get("SELECT COUNT(*) AS n FROM media")["n"] == 1


def test_foreign_keys_are_enforced():
    """PRAGMA foreign_keys is per-connection, so this fails if the adapter ever
    stops setting it on connect."""
    with pytest.raises(Exception):
        get_db().run(
            """INSERT INTO analysis (id, media_id, kind, analyzer_version, data, created_at)
               VALUES ('anl_x', 'med_does_not_exist', 'scenes', '1', '{}', '2026-01-01')"""
        )


def test_analysis_cache_round_trip():
    media_id = _media()
    save_analysis(media_id=media_id, kind="scenes", analyzer_version="1", data={"cuts": [1, 2]})

    hit = get_analysis(media_id, "scenes", "1")
    assert hit is not None and hit["data"] == {"cuts": [1, 2]}


def test_bumping_analyzer_version_invalidates_only_that_analyzer():
    media_id = _media()
    save_analysis(media_id=media_id, kind="scenes", analyzer_version="1", data={"cuts": [1]})
    save_analysis(media_id=media_id, kind="color", analyzer_version="1", data={"sat": 0.5})

    assert get_analysis(media_id, "scenes", "2") is None, "new version must miss"
    assert get_analysis(media_id, "color", "1") is not None, "siblings must be untouched"


def test_run_reports_rows_changed():
    db = get_db()
    assert db.run("UPDATE media SET path = 'x' WHERE id = 'nope'") == 0
    media_id = _media()
    assert db.run("UPDATE media SET path = 'y' WHERE id = ?", media_id) == 1
