"""Chat history — storage, scoping, and the 30-day promise.

Core owns this table even though chat is a paid, out-of-tree feature, and the
reason is deletion: purge_scope and backup walk core's table lists, so a
pack-owned table would be invisible to both. Account deletion would leave
transcripts behind and "export everything" would quietly stop being true.

Retention is promised in the privacy policy, so it is enforced twice — by a
sweep and, independently, by a cutoff on every read.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from backend import db, scope


@pytest.fixture
def store(tmp_path):
    db.set_db_path(tmp_path / "chat-history.db")
    db.init_db()
    return db


def age(role: str, days: float) -> None:
    """Backdate one message, to test retention without waiting a month."""
    stamp = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    with db.connect() as conn:
        conn.execute("UPDATE chat_messages SET created_at=? WHERE role=?", (stamp, role))


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def test_a_turn_round_trips(store):
    db.append_chat_message("user", "how am I doing?")
    db.append_chat_message("assistant", "Up 12%.", ["get_performance"])

    messages = db.list_chat_messages()

    assert [(m["role"], m["content"]) for m in messages] == [
        ("user", "how am I doing?"), ("assistant", "Up 12%."),
    ]
    assert messages[1]["tools"] == ["get_performance"]


def test_messages_come_back_oldest_first(store):
    for i in range(5):
        db.append_chat_message("user", f"q{i}")

    assert [m["content"] for m in db.list_chat_messages()] == ["q0", "q1", "q2", "q3", "q4"]


def test_the_limit_keeps_the_most_recent(store):
    for i in range(10):
        db.append_chat_message("user", f"q{i}")

    assert [m["content"] for m in db.list_chat_messages(limit=3)] == ["q7", "q8", "q9"]


def test_a_message_with_no_tools_reads_back_as_an_empty_list(store):
    db.append_chat_message("assistant", "no tools needed")

    assert db.list_chat_messages()[0]["tools"] == []


# ---------------------------------------------------------------------------
# Retention — the policy promises 30 days
# ---------------------------------------------------------------------------


def test_a_message_past_the_window_is_not_returned_even_before_the_sweep(store):
    """A deployment whose scheduler has been down must not start answering with
    older messages and make the policy false."""
    db.append_chat_message("user", "ancient")
    db.append_chat_message("assistant", "recent")
    age("user", days=31)

    assert [m["content"] for m in db.list_chat_messages()] == ["recent"]


def test_a_message_inside_the_window_survives(store):
    db.append_chat_message("user", "last week")
    age("user", days=29)

    assert [m["content"] for m in db.list_chat_messages()] == ["last week"]


def test_the_sweep_deletes_what_the_read_was_already_hiding(store):
    db.append_chat_message("user", "ancient")
    db.append_chat_message("assistant", "recent")
    age("user", days=45)

    assert db.purge_expired_chat_messages() == 1

    with db.connect() as conn:
        remaining = [row["content"] for row in conn.execute("SELECT content FROM chat_messages")]
    assert remaining == ["recent"]


def test_the_sweep_leaves_current_messages_alone(store):
    db.append_chat_message("user", "today")

    assert db.purge_expired_chat_messages() == 0
    assert len(db.list_chat_messages()) == 1


def test_the_retention_window_is_thirty_days(store):
    assert db.CHAT_RETENTION_DAYS == 30


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


def test_one_account_cannot_read_anothers_conversation(store):
    with scope.using("user-a"):
        db.append_chat_message("user", "a's question")
    with scope.using("user-b"):
        db.append_chat_message("user", "b's question")

    with scope.using("user-a"):
        assert [m["content"] for m in db.list_chat_messages()] == ["a's question"]
    with scope.using("user-b"):
        assert [m["content"] for m in db.list_chat_messages()] == ["b's question"]


def test_clearing_one_account_leaves_the_other(store):
    with scope.using("user-a"):
        db.append_chat_message("user", "a")
    with scope.using("user-b"):
        db.append_chat_message("user", "b")

    with scope.using("user-a"):
        assert db.clear_chat_history() == 1
        assert db.list_chat_messages() == []
    with scope.using("user-b"):
        assert len(db.list_chat_messages()) == 1


def test_the_sweep_is_per_scope(store):
    with scope.using("user-a"):
        db.append_chat_message("user", "old-a")
    with scope.using("user-b"):
        db.append_chat_message("user", "old-b")
    with db.connect() as conn:
        conn.execute("UPDATE chat_messages SET created_at=?",
                     ((datetime.now(UTC) - timedelta(days=40)).isoformat(),))

    with scope.using("user-a"):
        assert db.purge_expired_chat_messages() == 1
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM chat_messages").fetchone()["c"] == 1


# ---------------------------------------------------------------------------
# Deletion and export — the wiring that makes stated controls true
# ---------------------------------------------------------------------------


def test_deleting_an_account_deletes_its_transcripts(store):
    """purge_scope cites App Store 5.1.1(v): delete has to mean gone."""
    with scope.using("user-a"):
        db.append_chat_message("user", "something private")

    removed = db.purge_scope("user-a")

    assert removed.get("chat_messages") == 1
    with scope.using("user-a"):
        assert db.list_chat_messages() == []


def test_chat_messages_is_a_scoped_table(store):
    """The purge audit walks SCOPED_TABLES; a user_id table missing from it
    would leave rows behind on deletion."""
    assert "chat_messages" in db.SCOPED_TABLES


def test_export_includes_transcripts(store):
    """'Export everything' is a stated control and is never a paid feature."""
    from backend import backup

    assert "chat_messages" in backup._TABLES


# ---------------------------------------------------------------------------
# A database without the table — Postgres gets its schema from an owner-run
# migration the app role may not perform, so this state is real.
# ---------------------------------------------------------------------------


def test_a_missing_table_costs_history_not_chat(store):
    with db.connect() as conn:
        conn.execute("DROP TABLE chat_messages")

    db.append_chat_message("user", "still works")     # must not raise
    assert db.list_chat_messages() == []
    assert db.purge_expired_chat_messages() == 0


# ---------------------------------------------------------------------------
# The scheduled sweep
# ---------------------------------------------------------------------------


def test_the_scheduler_sweeps_every_scope(store, monkeypatch):
    import asyncio

    from backend import scheduler

    for owner in ("user-a", "user-b"):
        with scope.using(owner):
            db.append_chat_message("user", f"old-{owner}")
    with db.connect() as conn:
        conn.execute("UPDATE chat_messages SET created_at=?",
                     ((datetime.now(UTC) - timedelta(days=40)).isoformat(),))
    monkeypatch.setattr(scheduler, "_last_chat_sweep", None)

    removed = asyncio.run(scheduler.maybe_sweep_chat_history(["user-a", "user-b"]))

    assert removed == 2


def test_the_sweep_is_rate_limited(store, monkeypatch):
    """It reclaims space; the read cutoff is what keeps the promise. So it does
    not need to run on every scheduler tick."""
    import asyncio

    from backend import scheduler

    monkeypatch.setattr(scheduler, "_last_chat_sweep", datetime.now(UTC))

    assert asyncio.run(scheduler.maybe_sweep_chat_history(["user-a"])) == 0


def test_one_broken_scope_does_not_stop_the_sweep(store, monkeypatch):
    import asyncio

    from backend import scheduler

    with scope.using("user-b"):
        db.append_chat_message("user", "old")
    with db.connect() as conn:
        conn.execute("UPDATE chat_messages SET created_at=?",
                     ((datetime.now(UTC) - timedelta(days=40)).isoformat(),))
    monkeypatch.setattr(scheduler, "_last_chat_sweep", None)

    real = db.purge_expired_chat_messages

    def explode_for_a(*args, **kwargs):
        if scope.current() == "user-a":
            raise RuntimeError("scope a is broken")
        return real(*args, **kwargs)

    monkeypatch.setattr(db, "purge_expired_chat_messages", explode_for_a)

    assert asyncio.run(scheduler.maybe_sweep_chat_history(["user-a", "user-b"])) == 1
