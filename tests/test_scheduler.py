from __future__ import annotations

from datetime import UTC, datetime, timedelta

from backend import db
from backend.main import app
from backend.models import Briefing
from backend.scheduler import MAX_ATTEMPTS_PER_DAY, decide, next_run_iso
from fastapi.testclient import TestClient

SCHEDULE = {"enabled": True, "time": "07:30", "timezone": "UTC"}


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 6, 9, hour, minute, tzinfo=UTC)


def _attempt(status: str, created: datetime) -> Briefing:
    return Briefing(id=1, status=status, created_at=created.isoformat())


def test_does_not_run_when_disabled():
    assert decide(_at(8), {**SCHEDULE, "enabled": False}, [], False) is False


def test_does_not_run_before_scheduled_time():
    assert decide(_at(7, 29), SCHEDULE, [], False) is False


def test_runs_at_scheduled_time():
    assert decide(_at(7, 30), SCHEDULE, [], False) is True


def test_catches_up_later_in_the_day():
    assert decide(_at(15), SCHEDULE, [], False) is True


def test_does_not_run_twice_after_success():
    done = _attempt("done", _at(7, 30))
    assert decide(_at(9), SCHEDULE, [done], False) is False


def test_does_not_duplicate_while_any_briefing_running():
    assert decide(_at(8), SCHEDULE, [], True) is False


def test_retry_waits_for_backoff():
    failed = _attempt("error", _at(7, 30))
    assert decide(_at(7, 35), SCHEDULE, [failed], False) is False
    assert decide(_at(7, 41), SCHEDULE, [failed], False) is True


def test_stops_after_max_attempts():
    attempts = [
        _attempt("error", _at(7, 30) + timedelta(minutes=11 * i))
        for i in range(MAX_ATTEMPTS_PER_DAY)
    ]
    assert decide(_at(12), SCHEDULE, attempts, False) is False


def test_next_run_disabled_is_none():
    assert next_run_iso({**SCHEDULE, "enabled": False}) is None


def test_next_run_before_target_is_today(tmp_path):
    db.set_db_path(tmp_path / "serin-test.db")
    db.init_db()
    value = next_run_iso(SCHEDULE, now_utc=_at(6))
    assert value is not None
    assert value.startswith("2026-06-09T07:30")


def test_next_run_after_success_is_tomorrow(tmp_path):
    db.set_db_path(tmp_path / "serin-test.db")
    db.init_db()
    briefing = db.create_briefing(snapshot={}, model="m", trigger="scheduled")
    db.finish_briefing(briefing.id, status="done")
    value = next_run_iso(SCHEDULE, now_utc=_at(9))
    assert value is not None
    assert value.startswith("2026-06-10T07:30")


def test_schedule_api_roundtrip(tmp_path):
    db.set_db_path(tmp_path / "serin-test.db")
    db.init_db()
    client = TestClient(app)

    initial = client.get("/api/schedule").json()
    assert initial["enabled"] is False

    saved = client.put(
        "/api/schedule",
        json={"enabled": True, "time": "6:05", "timezone": "America/New_York"},
    ).json()
    assert saved["enabled"] is True
    assert saved["time"] == "06:05"
    assert saved["timezone"] == "America/New_York"
    assert saved["next_run"]

    fetched = client.get("/api/schedule").json()
    assert fetched["enabled"] is True
    assert fetched["time"] == "06:05"


def test_briefing_preferences_api_roundtrip(tmp_path):
    db.set_db_path(tmp_path / "serin-test.db")
    db.init_db()
    client = TestClient(app)

    initial = client.get("/api/briefings/preferences").json()
    assert initial["style"] == "operator"

    saved = client.put("/api/briefings/preferences", json={"style": "analyst"}).json()
    assert saved["style"] == "analyst"
    fetched = client.get("/api/briefings/preferences").json()
    assert fetched["style"] == "analyst"

    bad = client.put("/api/briefings/preferences", json={"style": "verbose"})
    assert bad.status_code == 422


def test_schedule_api_rejects_bad_input(tmp_path):
    db.set_db_path(tmp_path / "serin-test.db")
    db.init_db()
    client = TestClient(app)

    assert client.put("/api/schedule", json={"enabled": True, "time": "25:00"}).status_code == 422
    assert client.put("/api/schedule", json={"enabled": True, "timezone": "Mars/Olympus"}).status_code == 422
