from __future__ import annotations

import asyncio
import base64
import binascii
import os
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend import (
    agent_api,
    analytics,
    audit,
    auth,
    backup,
    broker_csv,
    db,
    emailer,
    fundamentals,
    fx,
    push,
    ratelimit,
    scheduler,
    scope,
    smart_import,
    snaptrade,
)
from backend.briefings import (
    build_portfolio_snapshot,
    estimate_briefing_cost,
    normalize_briefing_style,
    run_daily_briefing,
)
from backend.config import APP_VERSION, REPO_ROOT, get_ai_status, reset_ai_status_cache, settings
from backend.connectors import registry as connector_registry
from backend.csv_import import parse_positions_csv
from backend.models import (
    AccountIn,
    BriefingPreferencesIn,
    PositionIn,
    ScheduleIn,
    TaxLotIn,
    TransactionIn,
)
from backend.news import fetch_news
from backend.prices import fetch_price_history, fetch_quote, fetch_symbol_history, refresh_prices


def _assert_multiuser_intact() -> None:
    """Refuse to serve a deployment that meant to have accounts and doesn't.

    A plugin that fails to import is logged and skipped rather than raised,
    which is right for a community connector and dangerous for the pack that
    brings identity. Skipping it leaves no authorizer and no scope provider,
    and since a shared deployment has no ``SERIN_AUTH_PASSWORD`` either, the
    gate opens: every ``/api`` request is served unauthenticated, reading and
    writing one pooled scope. The instance passes its health check throughout.

    Both of yesterday's pack bugs looked exactly like this. The only outward
    sign was ``multiuser: false`` on a version endpoint nobody watches.

    So when the operator has declared accounts (``SERIN_MULTIUSER=1``), treat
    their absence as fatal. A container that will not start is noisy and
    obvious; an open one is neither.
    """
    if os.environ.get("SERIN_MULTIUSER", "").strip() != "1":
        return
    missing = [
        name
        for name, present in (
            ("authorizer", auth.authorizer_installed()),
            ("scope provider", scope.provider_installed()),
        )
        if not present
    ]
    if not missing:
        return
    raise RuntimeError(
        "SERIN_MULTIUSER=1 but no " + " or ".join(missing) + " is installed — the "
        "commercial pack did not load, or loaded without installing its seams. "
        "Serving now would authenticate nobody and pool every account into one "
        "shared dataset, so this deployment is stopping instead. Check the "
        "startup log for a 'Plugin ... failed to load' traceback."
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    from backend.logging_setup import configure_logging

    configure_logging(settings.log_format)
    db.init_db()
    # Open-core seam: load out-of-tree plugins (community connectors or the
    # commercial pack). A broken plugin logs and is skipped, never fatal.
    try:
        from backend.plugins import load_external_plugins

        load_external_plugins()
    except Exception:
        import logging

        logging.getLogger(__name__).exception("External plugin loading failed; core continues")
    _assert_multiuser_intact()
    # One-time migration: encrypt any legacy plaintext connector secrets.
    try:
        migrated = connector_registry.encrypt_existing_secrets()
        if migrated:
            import logging

            logging.getLogger(__name__).info("Encrypted %d legacy plaintext secret(s) at rest", migrated)
    except Exception:
        import logging

        logging.getLogger(__name__).exception("Secret-encryption migration failed; continuing with existing values")
    scheduler_task = asyncio.create_task(scheduler.scheduler_loop())
    try:
        yield
    finally:
        scheduler_task.cancel()
        try:
            await scheduler_task
        except asyncio.CancelledError:
            pass
        # Hand the database its connections back rather than leaving the
        # server to time them out; a no-op on SQLite, which never pools.
        from backend import dbdriver

        dbdriver.close_pool()


app = FastAPI(title="Serin", version=APP_VERSION, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        f"http://127.0.0.1:{settings.frontend_port}",
        f"http://localhost:{settings.frontend_port}",
    ],
    allow_credentials=True,  # web app lock uses a same-site session cookie
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def observability_and_auth(request, call_next):
    """One middleware, three production duties.

    1. App lock: when SERIN_AUTH_PASSWORD is set, /api/* (minus the public
       allowlist) requires the bearer token or session cookie.
    2. Agent-token scope: a credential from backend.agent_tokens authorizes
       the request, but only for /api/agent. Anything else is 403 — a
       different answer from the pack gate's 402, because scope is permanent
       and a lapsed subscription is not.
    3. Request log: one line per API request — method, path, status, ms.
       Static asset chatter is skipped. No bodies, no query strings with
       user data, no telemetry.
    """
    import logging
    import time as _time

    if request.method != "OPTIONS" and not auth.is_public_path(request.url.path):
        if not auth.request_is_authorized(request.headers, request.cookies):
            from fastapi.responses import JSONResponse

            return JSONResponse({"detail": "Locked — sign in first."}, status_code=401)
        scope_denial = auth.agent_denial(request.method, request.url.path, request.headers)
        if scope_denial:
            from fastapi.responses import JSONResponse

            return JSONResponse({"detail": scope_denial}, status_code=403)
        denial = auth.request_denial(request.method, request.url.path)
        if denial:
            from fastapi.responses import JSONResponse

            return JSONResponse({"detail": denial}, status_code=402)

    started = _time.perf_counter()
    response = await call_next(request)
    if request.url.path.startswith("/api"):
        elapsed_ms = (_time.perf_counter() - started) * 1000
        logging.getLogger("serin.request").info(
            "%s %s -> %d (%.1fms)",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
            extra={
                "http_method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": round(elapsed_ms, 1),
            },
        )
    return response


class LoginBody(BaseModel):
    password: str = ""


@app.get("/api/auth/status")
def api_auth_status(request: Request):
    return {
        "auth_enabled": auth.auth_enabled(),
        "authorized": auth.request_is_authorized(request.headers, request.cookies),
    }


# One passphrase, no lockout to trip and no account to enumerate — so the
# only thing standing between an exposed instance and an offline-speed guess
# is a ceiling on attempts.
_LOGIN_LIMIT = ratelimit.RateLimiter(limit=10, window_seconds=300)


@app.post("/api/auth/login")
def api_auth_login(body: LoginBody, request: Request):
    if not auth.auth_enabled():
        return {"ok": True, "token": "", "auth_enabled": False}
    caller = ratelimit.client_ip(request.headers)
    if not _LOGIN_LIMIT.check(caller):
        raise HTTPException(
            429, "Too many attempts. Try again shortly.",
            headers={"Retry-After": str(_LOGIN_LIMIT.retry_after(caller))},
        )
    if not auth.verify_password(body.password):
        raise HTTPException(401, "Wrong passphrase.")
    _LOGIN_LIMIT.reset(caller)  # a legitimate typo-then-success shouldn't throttle
    token = auth.session_token()
    from fastapi.responses import JSONResponse

    response = JSONResponse({"ok": True, "token": token, "auth_enabled": True})
    # SameSite=Strict + HttpOnly: the SPA is same-origin; JS never needs to
    # read the cookie (mobile clients use the returned bearer token instead).
    response.set_cookie(
        auth.COOKIE_NAME, token, httponly=True, samesite="strict",
        max_age=60 * 60 * 24 * 30,
    )
    return response


@app.post("/api/auth/logout")
def api_auth_logout():
    from fastapi.responses import JSONResponse

    response = JSONResponse({"ok": True})
    response.delete_cookie(auth.COOKIE_NAME)
    return response


# ---------------------------------------------------------------------------
# Backup & restore — the /data volume is the source of truth; these endpoints
# give users a portable copy without shell access (see docs/DEPLOY.md).
# ---------------------------------------------------------------------------


@app.get("/api/backup")
def api_backup_download():
    from fastapi.responses import JSONResponse

    payload = backup.export_data()
    stamp = payload["exported_at"][:10]
    return JSONResponse(
        payload,
        headers={"Content-Disposition": f'attachment; filename="serin-backup-{stamp}.json"'},
    )


@app.get("/api/backup/positions.csv")
def api_backup_positions_csv():
    from fastapi.responses import PlainTextResponse

    return PlainTextResponse(
        backup.positions_csv(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="serin-positions.csv"'},
    )


@app.post("/api/restore")
async def api_restore(file: UploadFile = File(...)):
    raw = await file.read()
    try:
        payload = backup.parse_backup_bytes(raw)
        counts = await asyncio.to_thread(backup.restore_data, payload)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "restored": counts}


class _CloudMigrateBody(BaseModel):
    target_url: str  # https://{name}.serin.money
    token: str = ""  # one-time ingest token from the provisioner
    confirm: bool = False  # the UI's itemized consent gate


async def _api_cloud_migrate(body: _CloudMigrateBody):
    """Copy this box's data into a freshly-provisioned Serin Cloud tenant.

    Portfolio, transactions, tax lots, briefing history and settings travel via
    the standard backup bundle → the tenant's /api/restore. Connector *secrets*
    are intentionally NOT included (export_data never emits them) — brokers
    re-auth on the Cloud side. Never automatic: requires ``confirm`` (the UI
    shows the itemized consent) and never touches THIS box's data.
    """
    import json as _json

    import httpx

    if not body.confirm:
        raise HTTPException(400, "Migration requires explicit consent (confirm=true).")
    target = body.target_url.rstrip("/")
    if not target.startswith("https://"):
        raise HTTPException(400, "Target must be an https:// Serin Cloud URL.")
    bundle = _json.dumps(backup.export_data()).encode()
    headers = {"authorization": f"Bearer {body.token}"} if body.token else {}
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{target}/api/restore",
                files={"file": ("serin-backup.json", bundle, "application/json")},
                headers=headers,
            )
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Could not reach the Cloud instance: {exc}") from exc
    if resp.status_code == 401:
        raise HTTPException(401, "The Cloud instance rejected the ingest token.")
    if resp.status_code >= 300:
        raise HTTPException(502, f"Cloud restore failed ({resp.status_code}).")
    return {"ok": True, "target": target, "restored": resp.json().get("restored", {})}


class PushRegisterBody(BaseModel):
    token: str = ""


def _api_push_register(body: PushRegisterBody):
    try:
        tokens = push.register_token(body.token)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "devices": len(tokens)}


def _api_entitlements():
    """Active plan + features (open-core seam; 'opensource' when no pack)."""
    from backend import entitlements

    return entitlements.summary()


class PriceRefreshRequest(BaseModel):
    symbols: list[str] | None = None


def _normalized_refresh_symbols(symbols: list[str] | None) -> set[str] | None:
    if not symbols:
        return None
    cleaned = {symbol.strip().upper() for symbol in symbols if symbol and symbol.strip()}
    return cleaned or None


@app.get("/api/config")
def api_config() -> dict:
    ai_status = get_ai_status()
    return {
        "app_name": settings.app_name,
        "backend_port": settings.backend_port,
        "frontend_port": settings.frontend_port,
        "ai_configured": bool(ai_status["ready"]),
        "ai_ready": bool(ai_status["ready"]),
        "ai_provider": ai_status["provider"],
        "ai_managed": bool(ai_status.get("managed")),
        # Managed AI blanks the model deliberately — which model serves it is
        # ours to change, not a promise. The fallback would have handed it
        # straight back.
        "ai_model": ai_status.get("model") or ("" if ai_status.get("managed") else settings.ai_model),
        "ai_error": ai_status["error"],
        "claude_cli_available": settings.claude_cli_available,
        "claude_cli_configured": settings.claude_cli_configured,
        "anthropic_configured": settings.anthropic_configured,
        "anthropic_model": settings.anthropic_model,
        "deepseek_configured": settings.deepseek_configured,
        "deepseek_model": settings.deepseek_model,
        "market_data_provider": settings.resolved_market_data_provider,
        "market_data_configured": settings.resolved_market_data_provider != "none",
        "fmp_configured": settings.fmp_configured,
        "database_engine": settings.database_engine,
        "database_url_configured": settings.database_url_configured,
        "email_configured": emailer.email_ready(),
        "email_to": (settings.email_to or emailer.alt_recipient()) if emailer.email_ready() else "",
        "snaptrade_configured": snaptrade.snaptrade_available(),
        "display_currency": fx.display_currency(),
        "cloud_managed": settings.cloud_managed,
    }


class _DisplayCurrencyBody(BaseModel):
    currency: str = "USD"


@app.put("/api/settings/display-currency")
def api_set_display_currency(body: _DisplayCurrencyBody):
    try:
        code = fx.set_display_currency(body.currency)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"display_currency": code}


class _LicenseBody(BaseModel):
    key: str = ""


def _api_get_license():
    from backend import licensing

    return licensing.status()


def _api_put_license(body: _LicenseBody):
    from backend import licensing

    try:
        return licensing.install_license(body.key)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def _api_delete_license():
    from backend import licensing

    return licensing.clear_license()


def _api_install_pack(body: _LicenseBody):
    """Redeem a license key for the Intelligence pack: download from billing,
    install locally, save the key. Requires a restart to load."""
    from backend import licensing

    try:
        return licensing.install_pack(body.key)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc


class _CheckoutBody(BaseModel):
    plan: str = "intelligence"


def _api_billing_checkout(body: _CheckoutBody):
    """Same-origin proxy to the billing service's /checkout.

    The pricing page posts here instead of calling the billing host directly,
    so the browser never makes a cross-origin request (no CORS foot-guns).
    503 when no billing origin is configured — the page then degrades to its
    waitlist fallback.
    """
    import httpx

    from backend.config import settings

    if not settings.billing_url:
        raise HTTPException(503, "Billing is not configured (SERIN_BILLING_URL).")
    url = settings.billing_url.rstrip("/") + "/checkout"
    try:
        resp = httpx.post(url, json={"plan": body.plan or "intelligence"}, timeout=15)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Billing unreachable: {exc}") from exc
    if resp.status_code != 200:
        raise HTTPException(502, f"Billing checkout failed ({resp.status_code}).")
    return resp.json()


# Cancellation proxies: same-origin like checkout, so the public cancel page
# never makes a cross-origin call. Rate-limited — the request endpoint sends
# email, so an unthrottled form is also a way to spam somebody's inbox.
_CANCEL_LIMIT = ratelimit.RateLimiter(limit=5, window_seconds=900)


class _CancelRequestBody(BaseModel):
    email: str = ""
    reason: str = ""
    detail: str = ""


def _billing_post(path: str, payload: dict) -> dict:
    import httpx

    from backend.config import settings

    if not settings.billing_url:
        raise HTTPException(503, "Billing is not configured (SERIN_BILLING_URL).")
    try:
        resp = httpx.post(settings.billing_url.rstrip("/") + path, json=payload, timeout=20)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Billing unreachable: {exc}") from exc
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail") or ""
        except ValueError:
            detail = ""
        raise HTTPException(resp.status_code, detail or f"Billing error ({resp.status_code}).")
    return resp.json()


def _api_billing_cancel_request(body: _CancelRequestBody, request: Request):
    for key in (ratelimit.client_ip(request.headers), (body.email or "").strip().lower()):
        if key and not _CANCEL_LIMIT.check(key):
            raise HTTPException(429, "Too many attempts. Try again shortly.")
    return _billing_post("/cancel/request", body.model_dump())


class _CancelConfirmBody(BaseModel):
    token: str = ""


def _api_billing_cancel_confirm(body: _CancelConfirmBody, request: Request):
    ip = ratelimit.client_ip(request.headers)
    if ip and not _CANCEL_LIMIT.check(ip):
        raise HTTPException(429, "Too many attempts. Try again shortly.")
    return _billing_post("/cancel/confirm", body.model_dump())


@app.get("/api/portfolio")
def api_portfolio():
    return db.portfolio_summary()


@app.get("/api/audit")
def api_audit():
    return audit.audit_portfolio()


@app.get("/api/positions")
def api_positions():
    return db.list_positions()


@app.post("/api/positions")
def api_create_position(position: PositionIn):
    try:
        return db.create_position(position)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


@app.put("/api/positions/{position_id}")
def api_update_position(position_id: int, position: PositionIn):
    updated = db.update_position(position_id, position)
    if not updated:
        raise HTTPException(404, "position not found")
    return updated


@app.delete("/api/positions/{position_id}")
def api_delete_position(position_id: int):
    if not db.delete_position(position_id):
        raise HTTPException(404, "position not found")
    return {"ok": True}


@app.get("/api/tax-lots")
def api_tax_lots(symbol: str | None = None, broker: str | None = None):
    return db.list_tax_lots(symbol=symbol, broker=broker)


@app.post("/api/tax-lots")
def api_create_tax_lot(lot: TaxLotIn):
    try:
        return db.create_tax_lot(lot)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


@app.delete("/api/tax-lots/{lot_id}")
def api_delete_tax_lot(lot_id: int):
    if not db.delete_tax_lot(lot_id):
        raise HTTPException(404, "tax lot not found")
    return {"ok": True}


@app.post("/api/import/csv")
async def api_import_csv(
    file: UploadFile = File(...),
    broker: str = Query(default="csv"),
):
    content = (await file.read()).decode("utf-8-sig")
    try:
        positions = parse_positions_csv(content, broker=broker)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    saved = [db.upsert_position(position) for position in positions]
    return {"imported": len(saved), "positions": saved}


@app.post("/api/prices/refresh")
def api_refresh_prices(body: PriceRefreshRequest | None = None):
    return refresh_prices(_normalized_refresh_symbols(body.symbols if body else None))


@app.get("/api/price-history")
def api_price_history(period: str = Query(default="3m"), refresh: bool = Query(default=False)):
    return fetch_price_history(period=period, refresh=refresh)


@app.get("/api/news")
async def api_news():
    held = [
        position
        for position in db.list_positions()
        if position.asset_type != "cash" and position.symbol != "CASH"
    ]
    # Names as well as tickers: feeds write "Netflix", not "NFLX".
    return await fetch_news(
        [position.symbol for position in held],
        {position.symbol: position.name or "" for position in held},
    )


@app.get("/api/briefings")
def api_list_briefings():
    return db.list_briefings()


@app.get("/api/briefings/estimate")
def api_briefing_estimate():
    """Cost guard: expected provider/model/cost for the next briefing run."""
    return estimate_briefing_cost()


@app.get("/api/briefings/preferences")
def api_get_briefing_preferences():
    return db.get_briefing_preferences()


@app.put("/api/briefings/preferences")
def api_put_briefing_preferences(body: BriefingPreferencesIn):
    return db.set_briefing_preferences(body.model_dump())


@app.get("/api/briefings/{briefing_id}")
def api_get_briefing(briefing_id: int):
    briefing = db.get_briefing(briefing_id)
    if not briefing:
        raise HTTPException(404, "briefing not found")
    return briefing


@app.delete("/api/briefings/{briefing_id}")
def api_delete_briefing(briefing_id: int):
    if not db.delete_briefing(briefing_id):
        raise HTTPException(404, "briefing not found")
    return {"ok": True}


class RunBriefingRequest(BaseModel):
    style: str | None = None


@app.post("/api/briefings/run")
async def api_run_briefing(background_tasks: BackgroundTasks, body: RunBriefingRequest | None = None):
    ai_status = get_ai_status(force=True)
    if not ai_status["ready"]:
        raise HTTPException(503, ai_status["error"] or "Set ANTHROPIC_API_KEY or DEEPSEEK_API_KEY to run briefings")
    style = normalize_briefing_style(
        body.style if body and body.style else db.get_briefing_preferences().get("style", "operator")
    )
    snapshot = build_portfolio_snapshot()
    snapshot["briefing_style"] = style
    briefing = db.create_briefing(snapshot=snapshot, model=settings.ai_model, trigger="manual")
    background_tasks.add_task(run_daily_briefing, briefing.id, style)
    return {"briefing_id": briefing.id, "status": briefing.status}


@app.post("/api/briefings/{briefing_id}/email")
async def api_email_briefing(briefing_id: int):
    if not emailer.email_ready():
        raise HTTPException(
            503,
            "Email delivery isn't available for this account right now."
            if emailer.alt_sender_installed()
            else "Email is not configured. Set SERIN_SMTP_HOST, SERIN_SMTP_USERNAME, "
            "SERIN_SMTP_PASSWORD, and SERIN_EMAIL_TO in .env, then restart Serin.",
        )
    briefing = db.get_briefing(briefing_id)
    if not briefing:
        raise HTTPException(404, "briefing not found")
    if briefing.status != "done":
        raise HTTPException(400, "Only completed briefings can be emailed")
    try:
        recipient = await asyncio.to_thread(emailer.deliver_scheduled_briefing_email, briefing)
    except Exception as exc:
        raise HTTPException(502, f"Email failed: {exc}") from exc
    emailed_at = db.mark_briefing_emailed(briefing.id)
    return {"ok": True, "to": recipient, "emailed_at": emailed_at}


@app.get("/api/schedule")
def api_get_schedule():
    schedule = db.get_schedule()
    return {**schedule, "next_run": scheduler.next_run_iso(schedule)}


@app.put("/api/schedule")
def api_put_schedule(body: ScheduleIn):
    schedule = db.set_schedule(body.model_dump())
    return {**schedule, "next_run": scheduler.next_run_iso(schedule)}


class ConnectRequest(BaseModel):
    redirect: str | None = None


async def api_broker_status():
    return await asyncio.to_thread(snaptrade.status)


async def api_broker_connect(body: ConnectRequest | None = None):
    if not snaptrade.broker_sync_entitled():
        raise HTTPException(
            402,
            "Brokerage sync is an add-on for hosted plans. Add it to your "
            "subscription to connect a broker.",
        )
    if not snaptrade.snaptrade_available():
        raise HTTPException(
            503,
            "SnapTrade is not configured. Set SNAPTRADE_CLIENT_ID and "
            "SNAPTRADE_CONSUMER_KEY in .env, then restart Serin.",
        )
    try:
        url = await asyncio.to_thread(snaptrade.connection_portal_url, body.redirect if body else None)
    except Exception as exc:
        raise HTTPException(502, snaptrade.error_message(exc)) from exc
    return {"redirect_uri": url}


async def api_broker_sync():
    if not snaptrade.broker_sync_entitled():
        raise HTTPException(
            402,
            "Brokerage sync is an add-on for hosted plans. Add it to your "
            "subscription to connect a broker.",
        )
    if not snaptrade.snaptrade_available():
        raise HTTPException(503, "SnapTrade is not configured.")
    try:
        return await asyncio.to_thread(snaptrade.sync)
    except snaptrade.SnapTradeError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, snaptrade.error_message(exc)) from exc


async def api_broker_disconnect(authorization_id: str):
    if not snaptrade.broker_sync_entitled():
        raise HTTPException(
            402,
            "Brokerage sync is an add-on for hosted plans. Add it to your "
            "subscription to connect a broker.",
        )
    if not snaptrade.snaptrade_available():
        raise HTTPException(503, "SnapTrade is not configured.")
    try:
        removed = await asyncio.to_thread(snaptrade.disconnect, authorization_id)
    except Exception as exc:
        raise HTTPException(502, snaptrade.error_message(exc)) from exc
    return {"ok": True, "removed_positions": removed}


class BackfillRequest(BaseModel):
    #: None means the account's entire history, which is the sensible default:
    #: a shorter window turns old purchases into sales with no cost basis.
    days: int | None = None


async def api_broker_backfill(body: BackfillRequest | None = None):
    """Import broker transaction history into the transactions table.

    Idempotent — already-imported activity ids are skipped, so this is safe
    to re-run any time.
    """
    if not snaptrade.broker_sync_entitled():
        raise HTTPException(
            402,
            "Brokerage sync is an add-on for hosted plans. Add it to your "
            "subscription to connect a broker.",
        )
    if not snaptrade.snaptrade_available():
        raise HTTPException(503, "SnapTrade is not configured.")
    days = body.days if body else None
    if days is not None:
        days = max(1, min(days, 3650))
    try:
        return await asyncio.to_thread(snaptrade.backfill_transactions, days)
    except snaptrade.SnapTradeError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"SnapTrade backfill failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Quote / history / performance — used by the stock detail view + the mobile
# client. Registered under both /api and /api/v1 so the mobile/SDK can target
# the versioned URL while existing web callers keep working.
# ---------------------------------------------------------------------------


def _alias_v1(path: str, handler, methods: list[str] | None = None) -> None:
    """Register a handler under both /api/{path} and /api/v1/{path}."""
    app.add_api_route(f"/api/{path}", handler, methods=methods or ["GET"])
    app.add_api_route(f"/api/v1/{path}", handler, methods=methods or ["GET"])


async def _api_quote(symbol: str, asset_type: str = "stock"):
    quote = await asyncio.to_thread(fetch_quote, symbol, asset_type)
    if not quote:
        raise HTTPException(404, f"No quote available for {symbol}")
    return quote


async def _api_symbol_history(symbol: str, period: str = "1y", asset_type: str = "stock"):
    return await asyncio.to_thread(fetch_symbol_history, symbol, asset_type, period)


async def _api_performance():
    return await asyncio.to_thread(analytics.period_returns)


async def _api_portfolio_history():
    """Transaction-accurate portfolio performance, with a coverage verdict.

    Separate from /performance, which answers the same question about the
    invested sleeve and treats buys as contributions. This one draws the
    boundary around the whole portfolio — holdings plus cash — so only
    deposits and withdrawals count as flows. The two disagree by design, and
    the coverage block says how much of either is reconstructed rather than
    back-priced.
    """
    from backend import portfolio_history

    return await asyncio.to_thread(portfolio_history.portfolio_performance)


async def _api_fundamentals(symbol: str, asset_type: str = "stock"):
    data = await asyncio.to_thread(
        fundamentals.get_fundamentals, [symbol], {symbol: asset_type}
    )
    row = data.get(symbol.upper())
    if not row:
        raise HTTPException(404, f"No fundamentals available for {symbol}")
    return row


_alias_v1("quote/{symbol}", _api_quote)
_alias_v1("quote/{symbol}/history", _api_symbol_history)
_alias_v1("quote/{symbol}/fundamentals", _api_fundamentals)
# Broker sync. Registered under both prefixes because the mobile client only
# speaks /api/v1 — these lived on /api/broker/* alone, which meant brokerage
# connection was unreachable from the phone entirely.
def api_broker_accounts():
    """Every connected account, one row each — see backend.snaptrade.accounts.

    Serin's own tables key on broker, so six real accounts collapse into three
    rows once their holdings land. This reads the accounts themselves, which is
    what a person recognises: the account they opened, its last four digits,
    and what it holds.
    """
    import logging

    from backend import snaptrade

    try:
        return {"accounts": snaptrade.accounts()}
    except Exception as exc:
        # The connections list must still render if this call fails; it is
        # extra detail on a screen that already works without it.
        logging.getLogger(__name__).warning(
            "broker accounts lookup failed: %s", exc)
        return {"accounts": [], "error": "Could not read account details."}


class _RelayQuote(BaseModel):
    symbol: str
    price: float
    asset_type: str = "stock"
    sector: str = ""


class _RelayBody(BaseModel):
    quotes: list[_RelayQuote]
    source: str = "relay"


def _relay_authorised(authorization: str) -> None:
    """Gate for the relay routes, or a 404 that tells a scanner nothing.

    404 rather than 401 for a bad token on purpose: a scan cannot then tell a
    wrong secret from an endpoint that is switched off, and the two look
    identical from outside.
    """
    import secrets as _secrets

    from backend.config import settings

    token = (settings.price_relay_token or "").strip()
    presented = (authorization or "").removeprefix("Bearer ").strip()
    if not token or not presented or not _secrets.compare_digest(presented, token):
        raise HTTPException(404, "Price relay is not enabled on this deployment.")


def _api_relay_tracked(authorization: str = Header(default="")):
    """What this deployment prices, so a relay knows what to fetch.

    The union across every account, which is the same set the sweep walks —
    quotes are the same number for everyone holding the symbol, so a relay
    priced per customer would be doing the same work repeatedly.
    """
    from backend import db, scope

    _relay_authorised(authorization)
    with scope.using(scope.INSTANCE_SCOPE):
        return [
            {"symbol": symbol, "asset_type": asset_type}
            for symbol, asset_type in db.list_tracked_symbols()
        ]


def _api_ingest_prices(body: _RelayBody, authorization: str = Header(default="")):
    """Accept prices from a feed running outside this deployment.

    Serin's own providers run wherever Serin runs, which is not always where
    they work: Yahoo answers a residential address and 429s a datacenter one,
    so the same code that fails on the server succeeds on a laptop at home.
    This lets that machine do the fetching and post the result in, and it is
    the same seam for anyone who has a feed of their own — a terminal, a
    broker session, a licensed subscription Serin does not integrate.

    Prices land in the shared cache the sweep already reads, so nothing
    downstream needs to know where a number came from. The sweep skips
    whatever the relay is keeping current, which is what turns this into a
    replacement for provider calls rather than an addition to them.
    """
    from backend import db, scope

    _relay_authorised(authorization)

    if len(body.quotes) > 2000:
        raise HTTPException(413, "Too many quotes in one post; send at most 2000.")

    with scope.using(scope.INSTANCE_SCOPE):
        written = db.cache_quotes(
            (q.symbol, q.asset_type, q.price, q.sector) for q in body.quotes
        )
    return {"accepted": len(body.quotes), "written": written, "source": body.source}


_alias_v1("prices/ingest", _api_ingest_prices, methods=["POST"])
_alias_v1("prices/tracked", _api_relay_tracked)
_alias_v1("broker/status", api_broker_status)
_alias_v1("broker/accounts", api_broker_accounts)
_alias_v1("broker/connect", api_broker_connect, methods=["POST"])
_alias_v1("broker/sync", api_broker_sync, methods=["POST"])
_alias_v1("broker/backfill", api_broker_backfill, methods=["POST"])
_alias_v1("broker/connections/{authorization_id}", api_broker_disconnect, methods=["DELETE"])
_alias_v1("performance", _api_performance)
_alias_v1("portfolio-history", _api_portfolio_history)
_alias_v1("briefings/estimate", api_briefing_estimate)


# A tiny version probe lets the mobile/SDK confirm the server speaks v1.
# ``locked`` says whether a passphrase gates /api — not a secret (the lock
# screen announces it on sight), and it lets the landing page tell "someone
# else's public instance" from "your own box" and label its CTA accordingly.
@app.get("/api/v1/version")
def api_v1_version():
    from backend import scope

    return {
        "app": "serin",
        "api_version": "1",
        "build": APP_VERSION,
        "locked": auth.auth_enabled(),
        # Multi-user deployments sign in with an email and password; self-host
        # has one passphrase and no accounts. The login screen needs to know
        # which before it can render, and this probe is already public.
        "multiuser": scope.provider_installed(),
    }


# v1 aliases for the read endpoints the mobile client needs. The existing
# /api/ routes stay for the web app and the test suite; /api/v1/ becomes the
# stable contract for mobile + future SDKs.
app.add_api_route("/api/v1/config", api_config, methods=["GET"])
app.add_api_route("/api/v1/portfolio", api_portfolio, methods=["GET"])
app.add_api_route("/api/v1/positions", api_positions, methods=["GET"])
# The write half of the same resource. Missing until now, which made the whole
# versioned contract read-only without saying so: the mobile client posts here
# to add, edit and delete a holding, and every one of those answered 405. Pull
# to refresh was the quiet one — its re-quote is fire-and-forget, so the 405
# was swallowed and the gesture just never fetched a price.
app.add_api_route("/api/v1/positions", api_create_position, methods=["POST"])
app.add_api_route("/api/v1/positions/{position_id}", api_update_position, methods=["PUT"])
app.add_api_route("/api/v1/positions/{position_id}", api_delete_position, methods=["DELETE"])
app.add_api_route("/api/v1/prices/refresh", api_refresh_prices, methods=["POST"])
app.add_api_route("/api/v1/briefings", api_list_briefings, methods=["GET"])
app.add_api_route("/api/v1/briefings/{briefing_id}", api_get_briefing, methods=["GET"])
app.add_api_route("/api/v1/news", api_news, methods=["GET"])
app.add_api_route("/api/v1/price-history", api_price_history, methods=["GET"])
app.add_api_route("/api/v1/audit", api_audit, methods=["GET"])
app.add_api_route("/api/v1/tax-lots", api_tax_lots, methods=["GET"])
app.add_api_route("/api/v1/schedule", api_get_schedule, methods=["GET"])
app.add_api_route("/api/v1/schedule", api_put_schedule, methods=["PUT"])


# ---------------------------------------------------------------------------
# Connector platform — the catalog + config portal API. Connectors are the
# extensibility primitive: market-data, holdings, and insight plugins. The
# portal renders config forms from each connector's manifest schema.
# ---------------------------------------------------------------------------


def _offered(manifest) -> bool:
    """Whether this deployment offers the connector at all.

    A shared deployment does not accept raw standing broker credentials. An
    API key pasted into a hosted service is a secret we then hold on someone's
    behalf, with no way for them to scope or revoke it from inside Serin —
    a different liability from an OAuth link that is read-only and revocable
    at the broker. So on Cloud the holdings connectors that ask for one are
    not offered; SnapTrade (OAuth) and file import remain.

    Self-host is untouched: it is your machine, your key, your call.
    """
    if manifest.kind == "holdings" and manifest.connect_method == "api_key":
        return connector_registry.instance_config_is_writable()
    return True


def _connector_card(manifest) -> dict:
    from backend import connectors as connectors_pkg

    cls = connector_registry.get_class(manifest.id)
    return {
        # A market-data connector can be serving every quote via env config
        # while its toggle reads Off — say so, or the card lies about who
        # actually answers price requests.
        "serving_prices": (
            manifest.kind == "market_data"
            and getattr(manifest, "asset_scope", "all") == "all"
            and connectors_pkg.active_market_data_id() == manifest.id
        ),
        "manifest": manifest.to_dict(),
        "enabled": connector_registry.is_enabled(manifest.id),
        "config": connector_registry.public_config(manifest.id),
        "configured": connector_registry.has_setting(manifest.id),
        # Enabled is a wish; ready is a fact. The portal warns when they
        # disagree, because a green toggle over missing config reads as done.
        "needs_setup": connector_registry.is_enabled(manifest.id) and not cls.ready(),
        "supports_sync": bool(getattr(cls, "supports_sync", False)),
        # Whether *deployment-owned* fields may be edited here. Strictly about
        # the deployment, never about this card: a connector can mix the two
        # (SnapTrade's partner credentials beside a personal sync preference),
        # so a per-card "editable" would mark those credentials writable on the
        # strength of the preference sitting next to them. The client pairs
        # this with each field's own `owner`.
        "instance_config_editable": connector_registry.instance_config_is_writable(),
    }


def _api_connectors():
    cards = [
        _connector_card(m) for m in connector_registry.all_manifests() if _offered(m)
    ]
    # Stable order: market data, holdings, insight; then by name.
    kind_order = {"market_data": 0, "holdings": 1, "insight": 2}
    cards.sort(key=lambda c: (kind_order.get(c["manifest"]["kind"], 9), c["manifest"]["name"]))
    return {"connectors": cards}


def _api_connector(connector_id: str):
    manifest = next(
        (m for m in connector_registry.all_manifests() if m.id == connector_id), None
    )
    if manifest is None or not _offered(manifest):
        raise HTTPException(404, f"Unknown connector: {connector_id}")
    return _connector_card(manifest)


def _forget_ai_status(connector_id: str) -> None:
    """Whoever just changed the AI connector should see the result of it, not
    the answer cached before they changed it."""
    if connector_id == "ai_briefing":
        reset_ai_status_cache()


connector_registry.on_config_saved(_forget_ai_status)


class ConnectorConfigBody(BaseModel):
    config: dict | None = None


class ConnectorEnableBody(BaseModel):
    enabled: bool


def _api_connector_config(connector_id: str, body: ConnectorConfigBody):
    if not connector_registry.has(connector_id):
        raise HTTPException(404, f"Unknown connector: {connector_id}")
    saved = connector_registry.set_config(connector_id, body.config or {})
    card = _api_connector(connector_id)
    if saved.get("ignored_fields"):
        # 200, because whatever was the caller's to change did change. Named,
        # because silence here reads as success for the rest of it.
        card["ignored_fields"] = saved["ignored_fields"]
    return card


def _api_connector_enable(connector_id: str, body: ConnectorEnableBody):
    if not connector_registry.has(connector_id):
        raise HTTPException(404, f"Unknown connector: {connector_id}")
    try:
        connector_registry.set_enabled(connector_id, body.enabled)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    return _api_connector(connector_id)


async def _api_connector_test(connector_id: str):
    if not connector_registry.has(connector_id):
        raise HTTPException(404, f"Unknown connector: {connector_id}")
    result = await asyncio.to_thread(connector_registry.test, connector_id)
    return result.to_dict()


async def _api_connector_sync(connector_id: str):
    """Generic on-demand pull for any holdings connector with supports_sync."""
    if not connector_registry.has(connector_id):
        raise HTTPException(404, f"Unknown connector: {connector_id}")
    connector = connector_registry.instantiate(connector_id)
    if connector is None or not getattr(connector, "supports_sync", False):
        raise HTTPException(400, f"{connector_id} does not support sync.")
    try:
        return await asyncio.to_thread(connector.sync)
    except Exception as exc:
        raise HTTPException(502, f"{connector_id} sync failed: {exc}") from exc


class ConnectorRunBody(BaseModel):
    context: dict | None = None


async def _api_connector_run(connector_id: str, body: ConnectorRunBody | None = None):
    """Generic run for an insight connector (in-tree or from a plugin pack).

    The connector decides entitlement itself — an out-of-tree pack gates its
    output on the resolved plan, so this endpoint stays open-core neutral.
    """
    if not connector_registry.has(connector_id):
        raise HTTPException(404, f"Unknown connector: {connector_id}")
    connector = connector_registry.instantiate(connector_id)
    run = getattr(connector, "run", None)
    if connector is None or not callable(run):
        raise HTTPException(400, f"{connector_id} is not a runnable insight.")
    try:
        return await asyncio.to_thread(run, body.context if body else None)
    except Exception as exc:
        raise HTTPException(502, f"{connector_id} run failed: {exc}") from exc


def _connector_docs_section(connector_id: str) -> str | None:
    """Extract a connector's section from docs/CONNECTORS.md.

    Sections are `### <id> — <Name>` headings; the section runs until the next
    `###`/`##` heading. Returns None when the doc or section is missing.
    """
    docs_path = REPO_ROOT / "docs" / "CONNECTORS.md"
    try:
        text = docs_path.read_text(encoding="utf-8")
    except OSError:
        return None
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.startswith(f"### {connector_id} ") or line.rstrip() == f"### {connector_id}":
            start = index
            break
    if start is None:
        return None
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].startswith("### ") or lines[index].startswith("## "):
            end = index
            break
    return "\n".join(lines[start:end]).strip()


def _api_connector_docs(connector_id: str):
    if not connector_registry.has(connector_id):
        raise HTTPException(404, f"Unknown connector: {connector_id}")
    markdown = _connector_docs_section(connector_id)
    if markdown is None:
        raise HTTPException(404, "No in-app docs for this connector yet.")
    return {"id": connector_id, "markdown": markdown}


# ---------------------------------------------------------------------------
# Transactions (v0.5) — the BUY/SELL/DIVIDEND log that unlocks dividend
# tracking, accurate cost basis, and real TWR/MWR downstream.
# ---------------------------------------------------------------------------

def _api_transactions(
    symbol: str | None = None,
    action: str | None = None,
    limit: int = 200,
    offset: int = 0,
    broker: str | None = None,
    since: str | None = None,
    until: str | None = None,
    source: str | None = None,
):
    """One page of the ledger, plus the total it is a page of.

    ``total`` is what makes the view honest: a ledger imported from a broker
    export runs to hundreds of rows, and a table that silently shows the first
    200 of 512 is worse than one that says so.
    """
    limit = max(1, min(limit, 500))
    filters = {"symbol": symbol, "action": action, "broker": broker,
               "since": since, "until": until, "source": source}
    return {
        "transactions": db.list_transactions(limit=limit, offset=offset, **filters),
        "total": db.count_transactions(**filters),
        "limit": limit,
        "offset": max(0, offset),
    }


def _api_transaction_facets():
    return db.transaction_facets()


def _api_create_transaction(body: TransactionIn):
    try:
        return db.create_transaction(body)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


def _api_update_transaction(transaction_id: int, body: TransactionIn):
    """Correct a recorded transaction.

    This exists because import is fallible: a broker code read as a fee when it
    was a dividend is invisible until someone can see the row and fix it.
    """
    try:
        updated = db.update_transaction(transaction_id, body)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    if updated is None:
        raise HTTPException(404, "transaction not found")
    return updated


def _api_delete_transaction(transaction_id: int):
    if not db.delete_transaction(transaction_id):
        raise HTTPException(404, "transaction not found")
    return {"ok": True}


def _api_data_gaps():
    """What is missing and what would fix it — see backend/data_gaps.py."""
    from backend import data_gaps

    return data_gaps.data_gaps()


class _GapDismissBody(BaseModel):
    id: str
    dismissed: bool = True


def _api_dismiss_gap(body: _GapDismissBody):
    """Put one reminder away, or bring it back.

    Server-side because the same person reads this on a phone and acts on it
    at a desk; a reminder that reappears on the other device has been hidden
    rather than dealt with.
    """
    from backend import data_gaps

    data_gaps.set_dismissed(body.id, body.dismissed)
    return data_gaps.data_gaps()


def _api_realized(year: str | None = None):
    """Realized results for a tax year, or all time when year is omitted.

    Matched gains and unmatched proceeds are returned as separate figures and
    must stay that way in any caller: a sale whose purchase predates the ledger
    has proceeds, not profit, and adding the two produces a number that looks
    authoritative and is not.
    """
    from backend import realized

    return {**realized.realized_gains(year), "years": realized.available_years()}


def _api_realized_detail(kind: str, year: str | None = None):
    """The individual events behind one figure on the Realized results panel.

    Walks the same lots as the summary, so the rows add up to the card that
    was clicked. ``kind`` is one of net / gains / income / costs; anything else
    answers with nothing rather than with everything.
    """
    from backend import realized

    return realized.realized_detail(kind, year)


def _api_transaction_summary():
    return db.transaction_summary()


_alias_v1("transactions", _api_transactions)
_alias_v1("transactions", _api_create_transaction, methods=["POST"])
_alias_v1("transactions/{transaction_id}", _api_update_transaction, methods=["PUT"])
_alias_v1("transactions/{transaction_id}", _api_delete_transaction, methods=["DELETE"])
_alias_v1("transactions/summary", _api_transaction_summary)
_alias_v1("transactions/realized", _api_realized)
_alias_v1("transactions/realized/{kind}", _api_realized_detail)
_alias_v1("data-gaps", _api_data_gaps)
_alias_v1("data-gaps/dismiss", _api_dismiss_gap, methods=["POST"])
_alias_v1("transactions/facets", _api_transaction_facets)


# ---------------------------------------------------------------------------
# Accounts (v0.5) — first-class taxable/IRA/401k/crypto buckets with
# per-account roll-ups. Backward-compatible with the existing broker label.
# ---------------------------------------------------------------------------

def _api_accounts():
    return {"accounts": db.list_accounts(with_summary=True)}


def _api_create_account(body: AccountIn):
    try:
        return db.create_account(body)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


def _api_delete_account(account_id: int):
    if not db.delete_account(account_id):
        raise HTTPException(404, "account not found")
    return {"ok": True}


_alias_v1("accounts", _api_accounts)
_alias_v1("accounts", _api_create_account, methods=["POST"])
_alias_v1("accounts/{account_id}", _api_delete_account, methods=["DELETE"])


_alias_v1("connectors", _api_connectors)
_alias_v1("connectors/{connector_id}", _api_connector)
_alias_v1("connectors/{connector_id}/config", _api_connector_config, methods=["PUT"])
_alias_v1("connectors/{connector_id}/enable", _api_connector_enable, methods=["POST"])
_alias_v1("connectors/{connector_id}/test", _api_connector_test, methods=["POST"])
_alias_v1("connectors/{connector_id}/sync", _api_connector_sync, methods=["POST"])
_alias_v1("connectors/{connector_id}/run", _api_connector_run, methods=["POST"])
_alias_v1("connectors/{connector_id}/docs", _api_connector_docs)
_alias_v1("push/register", _api_push_register, methods=["POST"])
_alias_v1("entitlements", _api_entitlements)
_alias_v1("license", _api_get_license)
_alias_v1("license", _api_put_license, methods=["PUT"])
_alias_v1("license", _api_delete_license, methods=["DELETE"])
# Agent surface — the tool layer over HTTP, plus token management. Registered
# here rather than in the alias block because these are routers, and they must
# be in place before the SPA catch-all at the bottom of this file, which would
# otherwise swallow /api/agent as a frontend path.
app.include_router(agent_api.agent_router)
app.include_router(agent_api.token_router)


_alias_v1("admin/install-pack", _api_install_pack, methods=["POST"])
_alias_v1("billing/checkout", _api_billing_checkout, methods=["POST"])
_alias_v1("billing/cancel/request", _api_billing_cancel_request, methods=["POST"])
_alias_v1("billing/cancel/confirm", _api_billing_cancel_confirm, methods=["POST"])
_alias_v1("cloud/migrate", _api_cloud_migrate, methods=["POST"])


# ---------------------------------------------------------------------------
# Smart import — AI-extracted positions with mandatory review.
# The extract endpoint is idempotent (no DB writes). The bulk endpoint
# commits user-confirmed rows via the existing position-creation path.
# ---------------------------------------------------------------------------


_IMAGE_MIME_TYPES = {
    "image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif",
}
_MAX_SMART_IMPORT_FILE_BYTES = 15 * 1024 * 1024
_MAX_SMART_IMPORT_BASE64_CHARS = ((_MAX_SMART_IMPORT_FILE_BYTES + 2) // 3) * 4


async def _api_import_extract(
    request: Request,
    file: UploadFile | None = File(default=None),
    text: str | None = Form(default=None),
    hint: str | None = Form(default=None),
):
    """Parse a dropped file or pasted text into a preview of positions.

    No DB writes. Returns ``{rows, warnings, provider, model, cost_usd, notice}``.
    """
    image_bytes: bytes | None = None
    image_mime: str | None = None
    pdf_bytes: bytes | None = None
    extracted_text: str | None = text
    content: bytes | None = None
    mime = ""
    filename = ""

    if file is not None:
        content = await file.read()
        mime = (file.content_type or "").lower()
        filename = (file.filename or "").lower()
    elif "application/json" in request.headers.get("content-type", "").lower():
        # iOS Safari can retain a File in the picker UI while its service
        # worker forwards an empty multipart form. The web client retries only
        # that exact failure as JSON, which uses the same reliable request path
        # as every other Serin mutation. Keep multipart as the primary contract
        # for native clients and ordinary browsers.
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(400, "Could not read the Smart Import request.") from exc
        if isinstance(payload, dict):
            extracted_text = str(payload.get("text") or "") or extracted_text
            hint = str(payload.get("hint") or "") or hint
            encoded = str(payload.get("file_base64") or "")
            if encoded.startswith("data:") and "," in encoded:
                encoded = encoded.split(",", 1)[1]
            if encoded:
                if len(encoded) > _MAX_SMART_IMPORT_BASE64_CHARS:
                    raise HTTPException(413, "Smart Import files must be 15 MB or smaller.")
                try:
                    content = base64.b64decode(encoded, validate=True)
                except (binascii.Error, ValueError) as exc:
                    raise HTTPException(400, "Could not decode the uploaded file.") from exc
                mime = str(payload.get("content_type") or "").lower()
                filename = str(payload.get("filename") or "upload").lower()

    if content is not None:
        if len(content) > _MAX_SMART_IMPORT_FILE_BYTES:
            raise HTTPException(413, "Smart Import files must be 15 MB or smaller.")
        if mime in _IMAGE_MIME_TYPES or filename.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
            image_bytes = content
            image_mime = mime if mime in _IMAGE_MIME_TYPES else f"image/{filename.rsplit('.', 1)[-1]}"
        elif mime == "application/pdf" or filename.endswith(".pdf"):
            # Rasterized locally page by page — see smart_import._pdf_page_images.
            pdf_bytes = content
        else:
            # CSV / text / TSV — pass through as text.
            try:
                extracted_text = content.decode("utf-8", errors="replace")
            except Exception as exc:
                raise HTTPException(400, "Could not decode file as text.") from exc

    if not extracted_text and image_bytes is None and pdf_bytes is None:
        raise HTTPException(400, "Provide a file or paste text to extract from.")

    # A recognised broker export is parsed, not inferred. Sending a structured
    # CSV to a vision model costs tokens proportional to the user's entire
    # trading history and returns a different answer each time — see
    # backend/broker_csv.py. Anything unrecognised falls through to the model.
    if extracted_text:
        parsed_export = await asyncio.to_thread(broker_csv.parse, extracted_text)
        if parsed_export is not None and parsed_export.transactions:
            notes = broker_csv.summarise(parsed_export)
            if parsed_export.warnings:
                notes = f"{notes} {' '.join(parsed_export.warnings[:5])}"
            import logging

            logging.getLogger(__name__).info(
                "broker csv import: broker=%s rows=%d parsed=%d unknown=%d",
                parsed_export.broker, parsed_export.total_rows,
                len(parsed_export.transactions), len(parsed_export.unknown),
            )
            return {
                "rows": [],
                "row_count": 0,
                "transactions": parsed_export.transactions,
                "transaction_count": len(parsed_export.transactions),
                "notes": notes,
                "notice": (
                    f"Parsed on this server from your {parsed_export.label} export. "
                    "Nothing was sent to an AI provider."
                ),
                "broker_format": parsed_export.label,
                "unknown_codes": parsed_export.unknown[:40],
            }

    try:
        result = await smart_import.extract(
            text=extracted_text,
            image_bytes=image_bytes,
            image_mime=image_mime,
            pdf_bytes=pdf_bytes,
            hint=hint,
        )
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc
    return result


class _BulkBody(BaseModel):
    rows: list[dict] = []
    replace: bool = False


async def _api_positions_bulk(body: _BulkBody):
    if not body.rows:
        raise HTTPException(400, "rows is empty — nothing to insert.")
    return await asyncio.to_thread(smart_import.bulk_insert, body.rows, replace=body.replace)


class _TransactionsBulkBody(BaseModel):
    transactions: list[dict] = []


async def _api_transactions_bulk(body: _TransactionsBulkBody):
    """Commit reviewed transactions from a statement import.

    Separate from positions/bulk because the two answer different questions —
    what you hold now, and what happened — and a statement often contains only
    one of them. Re-importing is safe: rows already seen are skipped, not
    duplicated.
    """
    if not body.transactions:
        raise HTTPException(400, "transactions is empty — nothing to import.")
    return await asyncio.to_thread(smart_import.import_transactions, body.transactions)


_alias_v1("import/extract", _api_import_extract, methods=["POST"])
_alias_v1("positions/bulk", _api_positions_bulk, methods=["POST"])
_alias_v1("transactions/bulk", _api_transactions_bulk, methods=["POST"])


dist_dir = REPO_ROOT / "frontend" / "dist"
if dist_dir.exists():
    app.mount("/assets", StaticFiles(directory=dist_dir / "assets"), name="assets")

    @app.get("/")
    def front_door():
        """The app, unless this deployment ships a marketing page.

        The landing page is not part of the open-source build — it sells the
        hosted product, and a self-hoster's own instance greeting them with
        someone else's sales pitch was exactly backwards. A hosted deployment
        bakes landing.html into dist (see the private repo's cloud image);
        its presence is the whole switch.
        """
        landing = dist_dir / "landing.html"
        if landing.exists():
            # Marketing HTML changes independently of the hashed app assets.
            # Force browsers to revalidate it so a release cannot leave the
            # previous front page sitting in a heuristic cache.
            return FileResponse(landing, headers={"Cache-Control": "no-cache"})
        return RedirectResponse("/app", status_code=302)

    @app.get("/app")
    def app_page():
        """The portfolio app (hash-routed SPA)."""
        return FileResponse(
            dist_dir / "index.html", headers={"Cache-Control": "no-cache"}
        )

    @app.get("/welcome")
    def landing_page():
        """Legacy landing URL — the landing moved to the root."""
        return RedirectResponse("/", status_code=301)

    @app.get("/pricing")
    def pricing_page():
        """Pricing lives on the landing page, like every other nav item.

        Kept as a redirect rather than removed: the app links here from
        Connectors and the X-ray teaser, and the URL is in the wild. On a
        self-host build there is no landing page, so the only honest answer
        to an explicit ask for pricing is the public site's.
        """
        if (dist_dir / "landing.html").exists():
            return RedirectResponse("/#pricing", status_code=301)
        return RedirectResponse("https://serin.money/#pricing", status_code=302)

    # Policy pages, served here rather than linked out to GitHub: someone
    # deciding whether to pay should not be handed off to a code-hosting site
    # to read the refund policy. The markdown files stay the single source of
    # truth; this just renders them in the site's own clothes.
    _DOC_PAGES = {
        "security": (REPO_ROOT / "SECURITY.md", "Security"),
        "privacy": (REPO_ROOT / "docs" / "PRIVACY-POLICY.md", "Privacy"),
        "terms": (REPO_ROOT / "docs" / "TERMS.md", "Terms & refunds"),
        "deploy": (REPO_ROOT / "docs" / "DEPLOY.md", "Deploy"),
        "contact": (REPO_ROOT / "docs" / "CONTACT.md", "Contact"),
        "exports": (REPO_ROOT / "docs" / "BROKER-EXPORTS.md", "Broker exports"),
    }
    _doc_cache: dict[str, tuple[float, str]] = {}

    def _doc_page(slug: str) -> HTMLResponse:
        path, title = _DOC_PAGES[slug]
        try:
            mtime = path.stat().st_mtime
        except OSError as exc:
            raise HTTPException(404, "That page is not available on this build.") from exc
        cached = _doc_cache.get(slug)
        if cached and cached[0] == mtime:
            return HTMLResponse(cached[1])
        text = path.read_text(encoding="utf-8")
        try:
            import markdown

            body = markdown.markdown(text, extensions=["tables", "fenced_code", "toc"])
        except ImportError:
            import html as html_mod

            body = f"<pre style='white-space:pre-wrap'>{html_mod.escape(text)}</pre>"
        nav = "".join(
            f'<a href="/{s}">{t}</a>' for s, (_p, t) in _DOC_PAGES.items() if s != slug
        )
        foot = "".join(
            f'<a class="link" href="/{s}">{t}</a>'
            for s, (_p, t) in _DOC_PAGES.items()
            if s != slug
        )
        page = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>{title} — Serin</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg"/>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=Instrument+Serif:ital@0;1&display=swap" rel="stylesheet"/>
<style>
  /* Same tokens as the landing page. These pages used to arrive in a cool
     grey-and-blue theme with a different typeface, so following "Terms" from
     the footer felt like leaving the site. */
  :root {{ --jade:#016558; --jade-deep:#014d43; --cream:#f3f0e8; --paper:#fbf8f1;
           --ink:#17231f; --muted:#5f6b66; --line:#bbb7ab; }}
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  /* Column layout so the jade footer sits at the bottom of the viewport on a
     short page instead of floating with cream beneath it. */
  html {{ height:100%; }}
  body {{ background:var(--cream); color:var(--ink); min-height:100%;
          display:flex; flex-direction:column;
          font:16px/1.65 "DM Sans",ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
          -webkit-font-smoothing:antialiased; }}
  a {{ color:var(--jade); }}
  :focus-visible {{ outline:2px solid var(--jade); outline-offset:3px; }}
  .page {{ width:100%; max-width:760px; margin:0 auto; padding:34px 24px 0; flex:1 0 auto; }}
  .top {{ display:flex; align-items:baseline; gap:18px; flex-wrap:wrap;
          padding-bottom:20px; margin-bottom:34px; border-bottom:1px solid var(--line); }}
  .top a {{ color:var(--muted); text-decoration:none; font-size:13.5px; font-weight:600; }}
  .top a:hover {{ color:var(--jade); }}
  .top .home {{ margin-right:auto; color:var(--jade); font-size:30px; font-weight:700;
                letter-spacing:-1.6px; }}
  .doc h1, .doc h2, .doc h3 {{ font-family:"Instrument Serif",Georgia,"Times New Roman",serif;
                               font-weight:400; letter-spacing:-0.01em; }}
  .doc h1 {{ font-size:44px; line-height:1.1; margin:0 0 20px; }}
  .doc h2 {{ font-size:28px; margin:38px 0 12px; }}
  .doc h3 {{ font-size:20px; margin:26px 0 8px; }}
  .doc p, .doc li {{ color:var(--muted); margin-bottom:12px; }}
  .doc li {{ margin-left:22px; margin-bottom:6px; }}
  .doc strong {{ color:var(--ink); }}
  .doc code {{ background:var(--paper); border:1px solid var(--line); border-radius:5px;
               padding:1px 5px; font-size:14px;
               font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }}
  .doc pre {{ background:var(--jade-deep); color:#e8f1ee; border-radius:13px;
              padding:16px 18px; overflow-x:auto; margin-bottom:14px; }}
  .doc pre code {{ background:none; border:none; padding:0; color:inherit; }}
  .doc table {{ border-collapse:collapse; margin-bottom:14px; width:100%; display:block;
                overflow-x:auto; }}
  .doc th, .doc td {{ border:1px solid var(--line); padding:8px 12px; font-size:14.5px;
                      color:var(--muted); text-align:left; }}
  .doc th {{ color:var(--ink); font-weight:700; background:var(--paper); }}
  .doc blockquote {{ border-left:3px solid var(--jade); padding-left:16px;
                     color:var(--muted); margin-bottom:12px; }}
  .doc hr {{ border:none; border-top:1px solid var(--line); margin:28px 0; }}
  .foot {{ margin-top:60px; background:var(--jade); color:#f7f2e9; flex:0 0 auto; }}
  /* Wordmark and links share the first row; the licence line takes its own,
     because four links plus the tagline do not fit the 760px text column and
     wrapping mid-list looked like a mistake. */
  .foot-in {{ max-width:760px; margin:0 auto; padding:26px 24px;
              display:flex; align-items:center; gap:12px 22px; flex-wrap:wrap; }}
  .foot .brand {{ color:white; font-size:30px; font-weight:700; letter-spacing:-1.6px;
                  text-decoration:none; }}
  .foot-links {{ margin-left:auto; display:flex; gap:20px; flex-wrap:wrap; }}
  .foot .say {{ order:3; width:100%; color:#cfe0da; font-size:13px; }}
  .foot a.link {{ color:#e6efec; font-size:13.5px; font-weight:600; text-decoration:none;
                  white-space:nowrap; }}
  .foot a.link:hover {{ color:white; text-decoration:underline; }}
  @media (max-width:620px) {{
    .doc h1 {{ font-size:34px; }}
    .foot-links {{ margin-left:0; }}
  }}
</style></head>
<body>
  <div class="page">
    <nav class="top"><a class="home" href="/">serin</a>{nav}</nav>
    <main class="doc">{body}</main>
  </div>
  <footer class="foot"><div class="foot-in">
    <a class="brand" href="/">serin</a>
    <div class="foot-links">{foot}</div>
    <span class="say">AGPLv3 · no telemetry · context, never trade directives</span>
  </div></footer>
</body></html>"""
        _doc_cache[slug] = (mtime, page)
        return HTMLResponse(page)

    # Registered from the registry rather than one decorator per page. Keeping
    # two lists in step is the same trap that made /contact 404 in production:
    # adding a page to _DOC_PAGES and *serving* it were two separate acts, and
    # doing only the first returns the SPA shell — a 200 that looks fine to a
    # link checker and shows a reviewer the wrong thing.
    for _slug in _DOC_PAGES:
        app.add_api_route(
            f"/{_slug}",
            (lambda slug=_slug: lambda: _doc_page(slug))(),
            methods=["GET"],
            response_class=HTMLResponse,
            include_in_schema=False,
        )

    @app.get("/support")
    def support_page():
        """App Store Connect requires a Support URL and SnapTrade's review asks
        for a contact page. Same page, two names, because both will be typed by
        people who guessed rather than followed a link."""
        return RedirectResponse("/contact", status_code=301)

    @app.get("/help")
    def help_page():
        return RedirectResponse("/contact", status_code=301)

    @app.get("/refund")
    def refund_page():
        """The refund policy is a section of the terms; keep the short URL."""
        return RedirectResponse("/terms", status_code=301)

    @app.get("/cancel")
    def cancel_page():
        """Self-serve cancellation. Hosted deployments bake cancel.html into
        dist (same switch as the landing page); a self-host box sends the
        subscriber to the public site, where their subscription actually
        lives."""
        page = dist_dir / "cancel.html"
        if page.exists():
            return FileResponse(page)
        return RedirectResponse("https://serin.money/cancel", status_code=302)

    @app.get("/{path:path}")
    def spa_fallback(path: str):
        requested = (dist_dir / path).resolve()
        # Never serve outside dist (path traversal), and unknown paths fall
        # back to the app shell so its hash-routes deep-link cleanly.
        if (
            path
            and requested.is_relative_to(dist_dir.resolve())
            and requested.is_file()
        ):
            return FileResponse(requested)
        return FileResponse(dist_dir / "index.html")
