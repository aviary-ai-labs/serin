from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend import (
    analytics,
    audit,
    auth,
    backup,
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
from backend.config import APP_VERSION, REPO_ROOT, get_ai_status, settings
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
    """One middleware, two production duties.

    1. App lock: when SERIN_AUTH_PASSWORD is set, /api/* (minus the public
       allowlist) requires the bearer token or session cookie.
    2. Request log: one line per API request — method, path, status, ms.
       Static asset chatter is skipped. No bodies, no query strings with
       user data, no telemetry.
    """
    import logging
    import time as _time

    if request.method != "OPTIONS" and not auth.is_public_path(request.url.path):
        if not auth.request_is_authorized(request.headers, request.cookies):
            from fastapi.responses import JSONResponse

            return JSONResponse({"detail": "Locked — sign in first."}, status_code=401)

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


def _api_pairing_info(request: Request):
    """Payload the web app renders as the mobile-pairing QR."""
    token = auth.session_token() if auth.auth_enabled() else ""
    base = str(request.base_url).rstrip("/")
    return {"serin": 1, "url": base, "token": token, "auth_enabled": auth.auth_enabled()}


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
        "ai_model": ai_status.get("model") or settings.ai_model,
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
        "email_configured": settings.email_configured,
        "email_to": settings.email_to if settings.email_configured else "",
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
    tickers = [
        position.symbol
        for position in db.list_positions()
        if position.asset_type != "cash" and position.symbol != "CASH"
    ]
    return await fetch_news(tickers)


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
    if not settings.email_configured:
        raise HTTPException(
            503,
            "Email is not configured. Set SERIN_SMTP_HOST, SERIN_SMTP_USERNAME, "
            "SERIN_SMTP_PASSWORD, and SERIN_EMAIL_TO in .env, then restart Serin.",
        )
    briefing = db.get_briefing(briefing_id)
    if not briefing:
        raise HTTPException(404, "briefing not found")
    if briefing.status != "done":
        raise HTTPException(400, "Only completed briefings can be emailed")
    try:
        recipient = await asyncio.to_thread(emailer.send_briefing_email, briefing)
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


@app.get("/api/broker/status")
async def api_broker_status():
    return await asyncio.to_thread(snaptrade.status)


@app.post("/api/broker/connect")
async def api_broker_connect(body: ConnectRequest | None = None):
    if not snaptrade.snaptrade_available():
        raise HTTPException(
            503,
            "SnapTrade is not configured. Set SNAPTRADE_CLIENT_ID and "
            "SNAPTRADE_CONSUMER_KEY in .env, then restart Serin.",
        )
    try:
        url = await asyncio.to_thread(snaptrade.connection_portal_url, body.redirect if body else None)
    except Exception as exc:
        raise HTTPException(502, f"SnapTrade connect failed: {exc}") from exc
    return {"redirect_uri": url}


@app.post("/api/broker/sync")
async def api_broker_sync():
    if not snaptrade.snaptrade_available():
        raise HTTPException(503, "SnapTrade is not configured.")
    try:
        return await asyncio.to_thread(snaptrade.sync)
    except snaptrade.SnapTradeError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"SnapTrade sync failed: {exc}") from exc


@app.delete("/api/broker/connections/{authorization_id}")
async def api_broker_disconnect(authorization_id: str):
    if not snaptrade.snaptrade_available():
        raise HTTPException(503, "SnapTrade is not configured.")
    try:
        removed = await asyncio.to_thread(snaptrade.disconnect, authorization_id)
    except Exception as exc:
        raise HTTPException(502, f"SnapTrade disconnect failed: {exc}") from exc
    return {"ok": True, "removed_positions": removed}


class BackfillRequest(BaseModel):
    days: int = 365


@app.post("/api/broker/backfill")
async def api_broker_backfill(body: BackfillRequest | None = None):
    """Import broker transaction history into the transactions table.

    Idempotent — already-imported activity ids are skipped, so this is safe
    to re-run any time.
    """
    if not snaptrade.snaptrade_available():
        raise HTTPException(503, "SnapTrade is not configured.")
    days = max(1, min((body.days if body else 365), 3650))
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
_alias_v1("performance", _api_performance)
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
    cls = connector_registry.get_class(manifest.id)
    return {
        "manifest": manifest.to_dict(),
        "enabled": connector_registry.is_enabled(manifest.id),
        "config": connector_registry.public_config(manifest.id),
        "configured": connector_registry.has_setting(manifest.id),
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

def _api_transactions(symbol: str | None = None, action: str | None = None, limit: int = 500):
    return {"transactions": db.list_transactions(symbol=symbol, action=action, limit=limit)}


def _api_create_transaction(body: TransactionIn):
    try:
        return db.create_transaction(body)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


def _api_delete_transaction(transaction_id: int):
    if not db.delete_transaction(transaction_id):
        raise HTTPException(404, "transaction not found")
    return {"ok": True}


def _api_transaction_summary():
    return db.transaction_summary()


_alias_v1("transactions", _api_transactions)
_alias_v1("transactions", _api_create_transaction, methods=["POST"])
_alias_v1("transactions/{transaction_id}", _api_delete_transaction, methods=["DELETE"])
_alias_v1("transactions/summary", _api_transaction_summary)


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
_alias_v1("pairing", _api_pairing_info)
_alias_v1("entitlements", _api_entitlements)
_alias_v1("license", _api_get_license)
_alias_v1("license", _api_put_license, methods=["PUT"])
_alias_v1("license", _api_delete_license, methods=["DELETE"])
_alias_v1("admin/install-pack", _api_install_pack, methods=["POST"])
_alias_v1("billing/checkout", _api_billing_checkout, methods=["POST"])
_alias_v1("cloud/migrate", _api_cloud_migrate, methods=["POST"])


# ---------------------------------------------------------------------------
# Smart import — AI-extracted positions with mandatory review.
# The extract endpoint is idempotent (no DB writes). The bulk endpoint
# commits user-confirmed rows via the existing position-creation path.
# ---------------------------------------------------------------------------


_IMAGE_MIME_TYPES = {
    "image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif",
}


async def _api_import_extract(
    file: UploadFile | None = File(default=None),
    text: str | None = Form(default=None),
    hint: str | None = Form(default=None),
):
    """Parse a dropped file or pasted text into a preview of positions.

    No DB writes. Returns ``{rows, warnings, provider, model, cost_usd, notice}``.
    """
    image_bytes: bytes | None = None
    image_mime: str | None = None
    extracted_text: str | None = text

    if file is not None:
        content = await file.read()
        mime = (file.content_type or "").lower()
        filename = (file.filename or "").lower()
        if mime in _IMAGE_MIME_TYPES or filename.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
            image_bytes = content
            image_mime = mime if mime in _IMAGE_MIME_TYPES else f"image/{filename.rsplit('.', 1)[-1]}"
        else:
            # CSV / text / TSV / PDF-as-text — pass through as text.
            try:
                extracted_text = content.decode("utf-8", errors="replace")
            except Exception as exc:
                raise HTTPException(
                    400, "Could not decode file as text. PDF support is limited — paste contents instead."
                ) from exc

    if not extracted_text and image_bytes is None:
        raise HTTPException(400, "Provide a file or paste text to extract from.")

    try:
        result = await smart_import.extract(
            text=extracted_text,
            image_bytes=image_bytes,
            image_mime=image_mime,
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


_alias_v1("import/extract", _api_import_extract, methods=["POST"])
_alias_v1("positions/bulk", _api_positions_bulk, methods=["POST"])


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
            return FileResponse(landing)
        return RedirectResponse("/app", status_code=302)

    @app.get("/app")
    def app_page():
        """The portfolio app (hash-routed SPA)."""
        return FileResponse(dist_dir / "index.html")

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
        nav = " · ".join(
            f'<a href="/{s}">{t}</a>' for s, (_p, t) in _DOC_PAGES.items() if s != slug
        )
        page = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>{title} — Serin</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg"/>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&display=swap" rel="stylesheet"/>
<style>
  :root {{ --bg:#f3f4f7; --ink:#171b26; --sec:#4c5566; --mut:#8a93a6; --acc:#2f6bed; --bd:#e4e7ee; }}
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ background:var(--bg); color:var(--ink); font:16px/1.65 'Manrope',ui-sans-serif,system-ui,sans-serif;
          -webkit-font-smoothing:antialiased; }}
  .page {{ max-width:760px; margin:0 auto; padding:40px 24px 80px; }}
  .top {{ display:flex; align-items:baseline; gap:14px; margin-bottom:34px; flex-wrap:wrap; }}
  .top a {{ color:var(--sec); text-decoration:none; font-size:13.5px; font-weight:600; }}
  .top a:hover {{ color:var(--acc); }}
  .top .home {{ font-weight:800; font-size:17px; color:var(--ink); }}
  .doc h1 {{ font-size:30px; letter-spacing:-0.02em; margin:0 0 18px; }}
  .doc h2 {{ font-size:20px; letter-spacing:-0.01em; margin:32px 0 10px; }}
  .doc h3 {{ font-size:16px; margin:24px 0 8px; }}
  .doc p, .doc li {{ color:var(--sec); margin-bottom:12px; }}
  .doc li {{ margin-left:22px; margin-bottom:6px; }}
  .doc a {{ color:var(--acc); }}
  .doc code {{ background:#e9ecf2; border-radius:5px; padding:1px 5px; font-size:14px; }}
  .doc pre {{ background:#171b26; color:#e6e9f0; border-radius:10px; padding:14px 16px; overflow-x:auto; margin-bottom:14px; }}
  .doc pre code {{ background:none; padding:0; color:inherit; }}
  .doc table {{ border-collapse:collapse; margin-bottom:14px; }}
  .doc th, .doc td {{ border:1px solid var(--bd); padding:7px 11px; font-size:14.5px; color:var(--sec); text-align:left; }}
  .doc blockquote {{ border-left:3px solid var(--bd); padding-left:14px; color:var(--mut); margin-bottom:12px; }}
</style></head>
<body><div class="page">
  <nav class="top"><a class="home" href="/">serin</a><span style="color:var(--mut)">·</span>{nav}</nav>
  <main class="doc">{body}</main>
</div></body></html>"""
        _doc_cache[slug] = (mtime, page)
        return HTMLResponse(page)

    @app.get("/security")
    def security_page():
        return _doc_page("security")

    @app.get("/privacy")
    def privacy_page():
        return _doc_page("privacy")

    @app.get("/terms")
    def terms_page():
        return _doc_page("terms")

    @app.get("/deploy")
    def deploy_page():
        return _doc_page("deploy")

    @app.get("/refund")
    def refund_page():
        """The refund policy is a section of the terms; keep the short URL."""
        return RedirectResponse("/terms", status_code=301)

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
