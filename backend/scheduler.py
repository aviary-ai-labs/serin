"""Daily-briefing scheduler.

A single asyncio loop wakes every CHECK_INTERVAL_SECONDS and asks the pure
`decide()` function whether a scheduled briefing should run now. All state
lives in the database (the schedule settings plus the briefings table itself),
so the scheduler survives restarts:

- Catch-up: if the app was down at the scheduled time, the run fires on the
  next check that day instead of being skipped.
- Dedupe: a scheduled run that is running or done for the local day blocks
  further runs; any in-flight briefing (manual included) also blocks.
- Retries: failed scheduled runs retry after RETRY_DELAY, at most
  MAX_ATTEMPTS_PER_DAY per local day. Every attempt is a visible row in the
  briefing history, so failures are never silent.

This is single-instance by design (SQLite, one process). Running multiple
backend replicas would need a shared lock — out of scope for now.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta, tzinfo
from datetime import time as dtime

from backend import db, scope
from backend.briefings import build_portfolio_snapshot, run_daily_briefing
from backend.config import get_ai_status, settings
from backend.models import Briefing

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 30
MAX_ATTEMPTS_PER_DAY = 3
RETRY_DELAY = timedelta(minutes=10)


def resolve_timezone(name: str) -> tzinfo:
    if not name or name == "local":
        return datetime.now().astimezone().tzinfo or UTC
    from zoneinfo import ZoneInfo

    try:
        return ZoneInfo(name)
    except Exception:
        logger.warning("Unknown timezone %r; falling back to local", name)
        return datetime.now().astimezone().tzinfo or UTC


def parse_schedule_time(value: str) -> dtime:
    try:
        hour, minute = value.strip().split(":")
        return dtime(int(hour), int(minute))
    except Exception:
        return dtime(7, 30)


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def decide(
    now_utc: datetime,
    schedule: dict,
    todays_attempts: list[Briefing],
    any_running: bool,
) -> bool:
    """Return True when a scheduled briefing should start right now."""
    if not schedule.get("enabled"):
        return False
    if any_running:
        return False

    tz = resolve_timezone(schedule.get("timezone", "local"))
    now_local = now_utc.astimezone(tz)
    at = parse_schedule_time(schedule.get("time", "07:30"))
    target = now_local.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
    if now_local < target:
        return False

    if any(item.status in ("running", "done") for item in todays_attempts):
        return False
    if len(todays_attempts) >= MAX_ATTEMPTS_PER_DAY:
        return False
    if todays_attempts:
        last_attempt = max(_parse_iso(item.created_at) for item in todays_attempts)
        if now_utc < last_attempt + RETRY_DELAY:
            return False
    return True


def todays_scheduled_attempts(now_utc: datetime, schedule: dict) -> list[Briefing]:
    tz = resolve_timezone(schedule.get("timezone", "local"))
    local_midnight = now_utc.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    since_utc_iso = local_midnight.astimezone(UTC).isoformat()
    return db.list_briefings_since(since_utc_iso, trigger="scheduled")


def next_run_iso(schedule: dict, now_utc: datetime | None = None) -> str | None:
    """Best-effort display value for when the next scheduled run will fire."""
    if not schedule.get("enabled"):
        return None
    now_utc = now_utc or datetime.now(UTC)
    tz = resolve_timezone(schedule.get("timezone", "local"))
    now_local = now_utc.astimezone(tz)
    at = parse_schedule_time(schedule.get("time", "07:30"))
    target = now_local.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)

    attempts = todays_scheduled_attempts(now_utc, schedule)
    done_today = any(item.status in ("running", "done") for item in attempts)
    if done_today or len(attempts) >= MAX_ATTEMPTS_PER_DAY:
        return (target + timedelta(days=1)).isoformat()
    if now_local < target:
        return target.isoformat()
    if attempts:
        next_retry = max(_parse_iso(item.created_at) for item in attempts) + RETRY_DELAY
        return max(next_retry, now_utc).astimezone(tz).isoformat()
    return now_local.isoformat()


async def maybe_sync_brokers() -> None:
    """Best-effort broker sync so the morning briefing sees fresh holdings.

    Never raises: a sync failure must not block the briefing.
    """
    try:
        from backend import snaptrade

        if not snaptrade.snaptrade_available():
            return
        if snaptrade.get_stored_user() is None:
            return
        await asyncio.to_thread(snaptrade.sync)
    except Exception:
        logger.warning("Pre-briefing broker sync failed; using existing holdings", exc_info=True)


# --- Per-connector daily auto-sync -------------------------------------------
# Holdings connectors opt in via an `auto_sync_daily` boolean in their portal
# config. One sync per connector per UTC day, with launch catch-up (same
# survive-restarts posture as the briefing schedule). State lives in
# app_settings so it is inspectable and reset-able.

AUTO_SYNC_STATE_KEY = "connector_auto_sync_state"


def _auto_sync_state() -> dict:
    import json as _json

    raw = db.get_setting(AUTO_SYNC_STATE_KEY, "")
    try:
        state = _json.loads(raw) if raw else {}
        return state if isinstance(state, dict) else {}
    except ValueError:
        return {}


async def maybe_auto_sync_connectors(now_utc: datetime | None = None) -> list[str]:
    """Run daily syncs for opted-in holdings connectors. Returns synced ids."""
    import json as _json

    from backend.connectors import registry

    now_utc = now_utc or datetime.now(UTC)
    today = now_utc.date().isoformat()
    synced: list[str] = []
    for manifest in registry.manifests_by_kind("holdings"):
        try:
            if not registry.is_enabled(manifest.id):
                continue
            config = registry.get_config(manifest.id) or {}
            if not config.get("auto_sync_daily"):
                continue
            state = _auto_sync_state()
            if state.get(manifest.id) == today:
                continue
            connector = registry.instantiate(manifest.id)
            if connector is None or not getattr(connector, "supports_sync", False):
                continue
            await asyncio.to_thread(connector.sync)
            state = _auto_sync_state()
            state[manifest.id] = today
            db.set_setting(AUTO_SYNC_STATE_KEY, _json.dumps(state))
            synced.append(manifest.id)
            logger.info("Auto-synced holdings connector %s", manifest.id)
        except Exception:
            logger.warning("Auto-sync failed for connector %s", manifest.id, exc_info=True)
    return synced


async def maybe_refresh_prices() -> None:
    """Best-effort full price refresh so the morning briefing is current.

    Routine syncs only price new holdings; this once-a-day full refresh keeps
    everything fresh for the briefing. Never raises.
    """
    try:
        from backend.prices import refresh_prices

        await asyncio.to_thread(refresh_prices)
    except Exception:
        logger.warning("Pre-briefing price refresh failed; using existing prices", exc_info=True)


async def run_scheduled_briefing() -> None:
    await maybe_sync_brokers()
    await maybe_refresh_prices()
    style = db.get_briefing_preferences().get("style", "operator")
    snapshot = build_portfolio_snapshot()
    snapshot["briefing_style"] = style
    briefing = db.create_briefing(snapshot=snapshot, model=settings.ai_model, trigger="scheduled")
    # The CLI provider's health check shells out; keep it off the event loop.
    ai_status = await asyncio.to_thread(get_ai_status)
    if not ai_status["ready"]:
        db.finish_briefing(
            briefing.id,
            status="error",
            error=str(ai_status["error"] or "No AI provider configured."),
        )
        logger.warning("Scheduled briefing %s failed: AI provider not ready", briefing.id)
        return
    logger.info("Starting scheduled briefing %s", briefing.id)
    await run_daily_briefing(briefing.id, style)
    await maybe_email_briefing(briefing.id)
    await maybe_push_briefing(briefing.id)


async def maybe_push_briefing(briefing_id: int) -> None:
    """Push 'briefing ready' to registered mobile devices. Never raises."""
    briefing = db.get_briefing(briefing_id)
    if not briefing or briefing.status != "done":
        return
    try:
        from backend import push

        sent = await asyncio.to_thread(push.send_briefing_ready, briefing.summary)
        if sent:
            logger.info("Pushed briefing %s to %d device(s)", briefing_id, sent)
    except Exception:
        logger.warning("Briefing push failed; briefing unaffected", exc_info=True)


async def maybe_email_briefing(briefing_id: int) -> None:
    """Email a finished scheduled briefing if the user opted in.

    Email failures are logged, never raised — the briefing itself succeeded
    and stays available in the app.
    """
    schedule = db.get_schedule()
    if not schedule.get("email_enabled") or not settings.email_configured:
        return
    briefing = db.get_briefing(briefing_id)
    if not briefing or briefing.status != "done":
        return
    try:
        from backend import emailer

        recipient = await asyncio.to_thread(emailer.send_briefing_email, briefing)
        db.mark_briefing_emailed(briefing.id)
        logger.info("Emailed scheduled briefing %s to %s", briefing.id, recipient)
    except Exception:
        logger.exception("Failed to email scheduled briefing %s", briefing_id)


async def check_once(now_utc: datetime | None = None) -> bool:
    now_utc = now_utc or datetime.now(UTC)
    schedule = db.get_schedule()
    attempts = todays_scheduled_attempts(now_utc, schedule)
    if not decide(now_utc, schedule, attempts, db.any_briefing_running()):
        return False
    await run_scheduled_briefing()
    return True


async def scheduler_loop() -> None:
    logger.info("Briefing scheduler started (checking every %ss)", CHECK_INTERVAL_SECONDS)
    while True:
        try:
            # Briefings are per-user: one schedule and one portfolio each. On
            # self-host this is a single pass; with accounts it visits everyone.
            for owner in scope.all_scopes():
                with scope.using(owner):
                    await check_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Briefing scheduler check failed")
        try:
            for owner in scope.all_scopes():
                with scope.using(owner):
                    await maybe_auto_sync_connectors()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Connector auto-sync check failed")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
