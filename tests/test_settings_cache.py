"""The app_settings cache.

``get_setting`` was the busiest query in the deployment — ~1.5M calls a month,
97% of them finding no row and returning the caller's default. What these pin
down is that the cache removes those round trips without changing a single
answer, and in particular that it never answers one account with another's
settings: unlike the connector-config cache it sits in front of user-owned
rows, so the scope is part of the key or the cache is a data leak.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from backend import db, scope


@pytest.fixture
def reads(monkeypatch, tmp_path):
    """A live database, plus a count of how many times settings reached it."""
    db.set_db_path(tmp_path / "settings.db")
    db.init_db()
    db.forget_settings_cache()

    counter = {"n": 0}
    real_connect = db.connect

    @contextmanager
    def _counted():
        counter["n"] += 1
        with real_connect() as conn:
            yield conn

    monkeypatch.setattr(db, "connect", _counted)
    return counter


def test_repeated_reads_hit_the_database_once(reads):
    """The regression this exists for."""
    db.set_setting("briefing_schedule", "{}")
    reads["n"] = 0

    values = [db.get_setting("briefing_schedule") for _ in range(10)]

    assert values == ["{}"] * 10
    assert reads["n"] == 1


def test_a_missing_row_is_cached_as_a_miss(reads):
    """The 97% case. A key nobody ever set is the most-read kind there is —
    every provider in the waterfall that has no override, every optional
    setting left at its default — so not caching absence would leave most of
    the traffic exactly where it was."""
    for _ in range(10):
        assert db.get_setting("never-set", "fallback") == "fallback"

    assert reads["n"] == 1


def test_callers_keep_their_own_default_for_the_same_missing_key(reads):
    """What is cached is the row's absence, not one caller's answer to it.
    ``fx`` asks for display_currency with 'USD'; something else may ask with
    ''. Caching the default would hand the second caller the first's."""
    assert db.get_setting("display_currency", "USD") == "USD"
    assert db.get_setting("display_currency", "EUR") == "EUR"
    assert db.get_setting("display_currency") == ""

    assert reads["n"] == 1


def test_a_write_is_visible_to_the_next_read(reads):
    """Immediate, not TTL-delayed: someone who just saved a setting expects
    the next read to use it."""
    assert db.get_setting("briefing_preferences", "operator") == "operator"

    db.set_setting("briefing_preferences", "narrative")

    assert db.get_setting("briefing_preferences") == "narrative"


def test_one_scopes_settings_never_answer_anothers(reads):
    """The security property. A cache keyed on the bare key would serve
    whichever account read first."""
    with scope.using("alice"):
        db.set_setting("display_currency", "GBP")
    with scope.using("bob"):
        db.set_setting("display_currency", "JPY")

    with scope.using("alice"):
        assert db.get_setting("display_currency") == "GBP"
    with scope.using("bob"):
        assert db.get_setting("display_currency") == "JPY"
    with scope.using("alice"):
        assert db.get_setting("display_currency") == "GBP"


def test_a_miss_in_one_scope_is_not_a_miss_in_another(reads):
    """The same trap from the other side: absence is per-account too."""
    with scope.using("alice"):
        assert db.get_setting("push_tokens", "none") == "none"
    with scope.using("bob"):
        db.set_setting("push_tokens", "bob-device")

    with scope.using("bob"):
        assert db.get_setting("push_tokens", "none") == "bob-device"
    with scope.using("alice"):
        assert db.get_setting("push_tokens", "none") == "none"


def test_an_expired_entry_is_re_read(reads, monkeypatch):
    """The TTL exists to bound how long a second machine can serve a value the
    first one has already changed."""
    db.set_setting("briefing_schedule", "{}")
    monkeypatch.setattr(db, "_SETTINGS_TTL_SECONDS", 0.0)
    reads["n"] = 0

    for _ in range(3):
        db.get_setting("briefing_schedule")

    assert reads["n"] == 3


def test_a_purge_stops_the_cache_answering_for_the_account(reads):
    """Deletion has to mean gone — App Store 5.1.1(v) asks for the real thing,
    and a cache still holding the rows would make it a lie."""
    with scope.using("alice"):
        db.set_setting("display_currency", "GBP")
        assert db.get_setting("display_currency") == "GBP"

    db.purge_scope("alice")

    with scope.using("alice"):
        assert db.get_setting("display_currency", "USD") == "USD"
