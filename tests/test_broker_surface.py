"""The brokerage surface a hosted customer can actually reach.

Two things went wrong before this existed, and both are the kind that look
fine in code review: the endpoints were never aliased to /api/v1, so the
mobile client could not call them at all; and the only UI entry point sat
inside Connectors, which is hidden from hosted accounts — the exact customers
brokerage sync is sold to.
"""

from __future__ import annotations

import pathlib

import pytest
from backend import db, entitlements, snaptrade
from backend.config import settings
from backend.main import app
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    db.set_db_path(tmp_path / "broker.db")
    db.init_db()
    monkeypatch.setattr(settings, "snaptrade_client_id", "test-client")
    monkeypatch.setattr(settings, "snaptrade_consumer_key", "test-consumer")
    yield TestClient(app)
    entitlements.set_verifier(None)


BROKER_ROUTES = [
    ("GET", "/api/v1/broker/status"),
    ("POST", "/api/v1/broker/connect"),
    ("POST", "/api/v1/broker/sync"),
    ("POST", "/api/v1/broker/backfill"),
    ("DELETE", "/api/v1/broker/connections/abc"),
]


def test_every_broker_route_exists_under_the_versioned_prefix():
    """The mobile client only speaks /api/v1. These lived on /api/broker/*
    alone, which made brokerage connection unreachable from the phone."""
    paths = {getattr(r, "path", "") for r in app.routes}
    for _, path in BROKER_ROUTES:
        template = path.replace("abc", "{authorization_id}")
        assert template in paths, f"{template} is not registered"


def test_the_unversioned_routes_still_work():
    """The web app already calls /api/broker/*; aliasing must add, not move."""
    paths = {getattr(r, "path", "") for r in app.routes}
    for path in ("/api/broker/status", "/api/broker/connect", "/api/broker/sync"):
        assert path in paths


def test_status_is_readable_without_a_connection(client):
    body = client.get("/api/v1/broker/status").json()
    assert body["configured"] is True
    assert body["registered"] is False
    assert body["connections"] == []


# --- the add-on gate applies to every mutating route ----------------------


@pytest.mark.parametrize("method,path", [r for r in BROKER_ROUTES if r[0] != "GET"])
def test_a_hosted_plan_without_the_add_on_is_refused(client, method, path):
    """Every route that costs money must be gated, not just the obvious one.
    A single ungated endpoint is a free connection, billed to us."""
    entitlements.set_verifier(lambda: {"plan": "cloud", "features": []})
    response = client.request(method, path, json={})
    assert response.status_code == 402, f"{method} {path} was not gated"
    assert "add-on" in response.json()["detail"]


@pytest.mark.parametrize("method,path", [r for r in BROKER_ROUTES if r[0] != "GET"])
def test_the_add_on_unlocks_every_route(client, method, path):
    entitlements.set_verifier(
        lambda: {"plan": "cloud", "features": [snaptrade.BROKER_SYNC_FEATURE]}
    )
    response = client.request(method, path, json={})
    # Past the gate. What happens next depends on SnapTrade being reachable,
    # which is not what this test is about — it must simply not be a 402.
    assert response.status_code != 402


@pytest.mark.parametrize("method,path", [r for r in BROKER_ROUTES if r[0] != "GET"])
def test_self_host_is_never_gated(client, method, path):
    """Self-hosters bring their own SnapTrade credentials and owe us nothing."""
    entitlements.set_verifier(None)
    assert client.request(method, path, json={}).status_code != 402


# --- the UI entry point ---------------------------------------------------


def test_brokerages_is_not_hidden_from_hosted_accounts():
    """Connectors configures the server, which a hosted customer does not own,
    so it is hidden from them. Brokerages must not inherit that rule — doing so
    left Cloud customers unable to connect a brokerage at all, which is the
    feature they would be paying extra for."""
    app_source = (
        __import__("pathlib").Path("frontend/src/App.jsx").read_text()
    )
    assert "'brokerages'" in app_source, "no Brokerages tab"
    # The hosted redirect names connectors only.
    redirect_line = next(
        line for line in app_source.splitlines()
        if "setTab('overview')" in line and "tab ===" in line
    )
    assert "brokerages" not in redirect_line, (
        "hosted accounts are being bounced off Brokerages, recreating the hole"
    )


# --- the contact page -----------------------------------------------------
# SnapTrade's production review asks for a contact page and App Store Connect
# requires a Support URL. Before this, /contact and /support returned the SPA
# shell: a 200 that satisfies a link checker and shows a reviewer the app.


@pytest.fixture
def public_client(tmp_path):
    db.set_db_path(tmp_path / "docs.db")
    db.init_db()
    return TestClient(app)


def test_contact_serves_a_real_page_not_the_app_shell(public_client):
    response = public_client.get("/contact")
    assert response.status_code == 200
    body = response.text
    assert "support@serin.money" in body
    assert "security@serin.money" in body
    # The SPA shell is ~1.9kB of bootstrap with no prose in it.
    assert len(body) > 4000, "this looks like the app shell, not a contact page"


def test_support_and_help_reach_the_same_page(public_client):
    """Both get typed by people guessing a URL rather than following a link,
    and App Store Connect specifically wants a 'Support URL'."""
    for path in ("/support", "/help"):
        response = public_client.get(path, follow_redirects=False)
        assert response.status_code == 301
        assert response.headers["location"] == "/contact"


def test_contact_names_the_self_serve_routes(public_client):
    """Half of what lands in a support inbox has a faster path in the product.
    Saying so on the page is cheaper than answering it twice a week."""
    body = public_client.get("/contact").text
    for route in ("/cancel", "/privacy", "/terms"):
        assert route in body


def test_contact_states_the_brokerage_boundary(public_client):
    """A reviewer assessing a brokerage integration will look for exactly this:
    what the app can and cannot do with a connected account."""
    body = public_client.get("/contact").text.lower()
    assert "read-only" in body or "read only" in body
    assert "cannot place trades" in body or "never receives your brokerage" in body


def test_every_served_doc_is_actually_shipped_in_the_image():
    """The Dockerfile copies an explicit list of docs, so a page can be routed,
    tested green locally, and still 404 in production — which is what /contact
    did. Anything _DOC_PAGES serves must be on that list."""
    import pathlib
    import re

    dockerfile = pathlib.Path("Dockerfile").read_text()
    copied = set(re.findall(r"docs/([A-Za-z0-9._-]+\.md)", dockerfile))
    main_source = pathlib.Path("backend/main.py").read_text()
    served = set(re.findall(r'REPO_ROOT / "docs" / "([A-Za-z0-9._-]+\.md)"', main_source))
    missing = served - copied
    assert not missing, f"routed but never copied into the image: {sorted(missing)}"


# --- the policy pages wear the site's clothes ------------------------------
# These render from markdown into a template that had drifted: a cool grey-and-
# blue palette in a different typeface, reached from a jade-and-cream footer.
# Following "Terms" felt like leaving the site, which is the opposite of what a
# policy page is for.

def _served_slugs() -> tuple[str, ...]:
    """Read the registry rather than restating it, so a page added without a
    theme or a way home fails here instead of shipping unstyled."""
    import re

    source = pathlib.Path("backend/main.py").read_text()
    block = re.search(r"_DOC_PAGES = \{(.*?)\n    \}", source, re.S)
    assert block, "the _DOC_PAGES registry moved"
    return tuple(re.findall(r'"([a-z-]+)": \(REPO_ROOT', block.group(1)))


_SERVED_SLUGS = _served_slugs()


@pytest.mark.parametrize("slug", _SERVED_SLUGS)
def test_doc_pages_use_the_landing_palette(public_client, slug):
    body = public_client.get(f"/{slug}").text
    assert "--jade:#016558" in body, "policy pages drifted off the site palette"
    assert "DM+Sans" in body and "Instrument+Serif" in body, (
        "policy pages are not loading the site's typefaces"
    )
    for stale in ("#f3f4f7", "#2f6bed", "Manrope"):
        assert stale not in body, f"old doc theme token {stale} came back"


@pytest.mark.parametrize("slug", _SERVED_SLUGS)
def test_doc_pages_offer_a_way_home_and_to_each_other(public_client, slug):
    """A visitor who lands here from a search result has no site around them.
    The wordmark goes home and the footer carries the rest."""
    body = public_client.get(f"/{slug}").text
    assert '<a class="home" href="/">serin</a>' in body
    assert '<a class="brand" href="/">serin</a>' in body
    for other in _SERVED_SLUGS:
        if other != slug:
            assert f'href="/{other}"' in body, f"/{slug} does not link to /{other}"


# --- the connect response's field name -------------------------------------
# SnapTrade returned a valid portal link, the route answered 200 carrying it,
# and the page still showed "SnapTrade did not return a connection URL" —
# because the client destructured `url` from a body that says `redirect_uri`,
# and threw a message identical to the backend's own. The failure therefore
# read as the vendor's rather than ours. Both halves are pinned here.


def test_connect_returns_the_portal_link_as_redirect_uri(client, monkeypatch):
    from backend import snaptrade

    monkeypatch.setattr(snaptrade, "broker_sync_entitled", lambda: True)
    monkeypatch.setattr(snaptrade, "snaptrade_available", lambda: True)
    monkeypatch.setattr(
        snaptrade, "connection_portal_url",
        lambda redirect=None: "https://app.snaptrade.com/portal?token=abc",
    )
    payload = client.post("/api/v1/broker/connect", json={}).json()
    assert "redirect_uri" in payload, (
        "the field the browser reads was renamed; Brokerages.jsx destructures "
        "redirect_uri and will silently get undefined"
    )
    assert payload["redirect_uri"].startswith("https://")


def test_the_client_destructures_the_field_the_route_returns():
    """A contract across two languages that no type checker spans. Cheap to
    assert, and the mismatch it catches costs an afternoon of blaming SnapTrade."""
    import pathlib
    import re

    source = pathlib.Path("frontend/src/components/Brokerages.jsx").read_text()
    call = re.search(
        r"const \{([^}]*)\}\s*=\s*await api\('/api/v1/broker/connect'", source
    )
    assert call, "the connect call's destructuring moved — re-point this test"
    assert "redirect_uri" in call.group(1), (
        f"Brokerages.jsx destructures {{{call.group(1).strip()}}} from a body "
        "whose field is redirect_uri, so it will silently read undefined"
    )


def test_the_client_error_is_distinguishable_from_the_servers():
    """Two identical strings on either side of the wire made a client-side bug
    look like a vendor outage. Whatever they say, they must not match."""
    import pathlib

    client_source = pathlib.Path("frontend/src/components/Brokerages.jsx").read_text()
    server_source = pathlib.Path("backend/snaptrade.py").read_text()
    assert "SnapTrade did not return a connection URL." in server_source
    assert "SnapTrade did not return a connection URL." not in client_source, (
        "the client is echoing the server's exact error again"
    )


def test_the_sync_route_returns_the_counts_the_page_displays(client, monkeypatch):
    """"Synced 0 holdings" was shown after a sync that pulled ten across four
    accounts: the page read `upserted`, the route returns `positions`, and the
    `?? 0` fallback turned a missing field into a plausible number."""
    from backend import snaptrade

    monkeypatch.setattr(snaptrade, "broker_sync_entitled", lambda: True)
    monkeypatch.setattr(snaptrade, "snaptrade_available", lambda: True)
    monkeypatch.setattr(snaptrade, "sync", lambda: {
        "at": "2026-08-26T05:02:17+00:00", "accounts": 4, "positions": 10,
        "removed": 0, "repriced": 4, "error": "",
    })
    payload = client.post("/api/v1/broker/sync").json()
    assert payload["positions"] == 10 and payload["accounts"] == 4
    assert "upserted" not in payload, "the page's old field name came back"


def test_last_sync_is_a_summary_object_not_a_timestamp(client, monkeypatch):
    """The page rendered "Invalid Date" by handing this whole dict to a date
    formatter. It carries the timestamp under `at`."""
    from backend import snaptrade

    monkeypatch.setattr(snaptrade, "snaptrade_available", lambda: True)
    monkeypatch.setattr(snaptrade, "get_stored_user", lambda: {"userId": "u", "userSecret": "s"})
    monkeypatch.setattr(snaptrade, "list_connections", lambda: [])
    monkeypatch.setattr(snaptrade, "get_last_sync", lambda: {
        "at": "2026-08-26T05:02:17+00:00", "accounts": 4, "positions": 10,
    })
    last = client.get("/api/v1/broker/status").json()["last_sync"]
    assert isinstance(last, dict) and "at" in last


#: Every field Brokerages.jsx depends on, and the route that supplies it.
#: Three separate bugs in this one component came from reading a name the
#: server does not send — each silently, because JS answers `undefined` rather
#: than raising. This is the cheapest guard against a fourth.
_CLIENT_CONTRACT = {
    "redirect_uri": "connect → the portal link",
    "positions": "sync → holdings written",
    "accounts": "sync → accounts seen",
    "imported": "backfill → transactions imported",
    "skipped_existing": "backfill → already on record",
    "configured": "status → credentials present",
    "connections": "status → connection list",
    "last_sync": "status → sync summary (timestamp under .at)",
}


def test_the_page_reads_only_field_names_the_routes_send():
    import pathlib

    source = pathlib.Path("frontend/src/components/Brokerages.jsx").read_text()
    missing = [f"{name} ({why})" for name, why in _CLIENT_CONTRACT.items() if name not in source]
    assert not missing, "Brokerages.jsx stopped referencing: " + "; ".join(missing)
    # The names it must NOT read, because no route sends them. Each of these
    # was a real bug that rendered as plausible output rather than an error.
    for wrong in ("result.upserted", "dateShort(status.last_sync)", "const { url }"):
        assert wrong not in source, f"the page is reading {wrong} again"


# --- connected, but not yet synced -----------------------------------------
# Connecting a broker and pulling its holdings are two separate acts: the
# portal hands back an authorization and nothing fetches positions until
# something asks. A freshly connected Fidelity account therefore showed an
# empty page and an unchanged dashboard, which reads as a failed connection
# rather than an unfinished one.


def test_status_says_whether_a_connection_has_produced_holdings(client, monkeypatch):
    from backend import db, snaptrade
    from backend.models import PositionIn

    monkeypatch.setattr(snaptrade, "snaptrade_available", lambda: True)
    monkeypatch.setattr(snaptrade, "get_stored_user", lambda: {"userId": "u", "userSecret": "s"})
    monkeypatch.setattr(snaptrade, "get_last_sync", lambda: None)
    monkeypatch.setattr(snaptrade, "list_connections", lambda: [
        {"id": "a", "institution": "Robinhood", "disabled": False},
        {"id": "b", "institution": "Fidelity", "disabled": False},
    ])
    created = db.create_position(PositionIn(symbol="TQQQ", broker="robinhood",
                                            asset_type="stock", quantity=10,
                                            average_cost=50, current_price=70))
    with db.connect() as conn:
        conn.execute("UPDATE positions SET source='snaptrade' WHERE id=?", (created.id,))

    payload = client.get("/api/v1/broker/status").json()
    by_institution = {c["institution"]: c for c in payload["connections"]}
    assert by_institution["Robinhood"]["synced"] is True
    assert by_institution["Fidelity"]["synced"] is False
    assert payload["pending_sync"] is True, "the page needs this to know to sync"


def test_pending_sync_is_false_once_every_connection_has_holdings(client, monkeypatch):
    from backend import db, snaptrade
    from backend.models import PositionIn

    monkeypatch.setattr(snaptrade, "snaptrade_available", lambda: True)
    monkeypatch.setattr(snaptrade, "get_stored_user", lambda: {"userId": "u", "userSecret": "s"})
    monkeypatch.setattr(snaptrade, "get_last_sync", lambda: None)
    monkeypatch.setattr(snaptrade, "list_connections", lambda: [
        {"id": "a", "institution": "Robinhood", "disabled": False},
    ])
    created = db.create_position(PositionIn(symbol="TQQQ", broker="robinhood",
                                            asset_type="stock", quantity=10,
                                            average_cost=50, current_price=70))
    with db.connect() as conn:
        conn.execute("UPDATE positions SET source='snaptrade' WHERE id=?", (created.id,))

    payload = client.get("/api/v1/broker/status").json()
    assert payload["pending_sync"] is False, "a synced account would re-sync on every page load"


def test_a_manual_position_does_not_count_as_a_synced_connection(client, monkeypatch):
    """The AFRM shape: a hand-entered row at a connected broker must not make
    the connection look synced, or the holdings never get pulled."""
    from backend import db, snaptrade
    from backend.models import PositionIn

    monkeypatch.setattr(snaptrade, "snaptrade_available", lambda: True)
    monkeypatch.setattr(snaptrade, "get_stored_user", lambda: {"userId": "u", "userSecret": "s"})
    monkeypatch.setattr(snaptrade, "get_last_sync", lambda: None)
    monkeypatch.setattr(snaptrade, "list_connections", lambda: [
        {"id": "a", "institution": "Robinhood", "disabled": False},
    ])
    db.create_position(PositionIn(symbol="AFRM", broker="robinhood", asset_type="stock",
                                  quantity=519, average_cost=40, current_price=77))

    payload = client.get("/api/v1/broker/status").json()
    assert payload["connections"][0]["synced"] is False
    assert payload["pending_sync"] is True
