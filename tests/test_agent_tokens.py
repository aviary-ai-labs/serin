"""Agent tokens — named, scoped, revocable credentials for MCP/OpenAPI clients.

The app lock issues one deterministic token shared by every client, revocable
only by changing the passphrase. Handing that to an agent grants full write
access to the whole API and makes revocation a sign-out for every device. These
tests pin the properties that make an agent credential different: it is stored
hashed, it reaches only the agent surface, it can be revoked alone, and it is
refused outright where it cannot carry an identity.
"""

from __future__ import annotations

import pytest
from backend import agent_tokens, auth, db


@pytest.fixture
def store(tmp_path):
    db.set_db_path(tmp_path / "tokens.db")
    db.init_db()
    return db


@pytest.fixture(autouse=True)
def clear_authorizer():
    yield
    auth.set_authorizer(None)


def test_issued_token_verifies(store):
    record, plaintext = agent_tokens.issue("Claude Desktop")

    assert plaintext.startswith(agent_tokens.TOKEN_PREFIX)
    assert record["name"] == "Claude Desktop"
    assert record["scope"] == agent_tokens.READ_SCOPE
    verified = agent_tokens.verify(plaintext)
    assert verified is not None
    assert verified["id"] == record["id"]


def test_plaintext_is_never_stored(store):
    """A leaked backup must not be a working credential."""
    _, plaintext = agent_tokens.issue("laptop")

    raw = db.get_setting(agent_tokens.SETTINGS_KEY, "")

    assert plaintext not in raw
    assert plaintext.removeprefix(agent_tokens.TOKEN_PREFIX) not in raw


def test_issued_record_never_exposes_the_hash(store):
    record, _ = agent_tokens.issue("laptop")

    assert "token_sha256" not in record
    assert all("token_sha256" not in row for row in agent_tokens.list_tokens())


def test_tokens_are_unique(store):
    _, first = agent_tokens.issue("one")
    _, second = agent_tokens.issue("two")

    assert first != second
    assert agent_tokens.verify(first)["name"] == "one"
    assert agent_tokens.verify(second)["name"] == "two"


def test_wrong_token_does_not_verify(store):
    agent_tokens.issue("real")

    assert agent_tokens.verify(agent_tokens.TOKEN_PREFIX + "not-a-real-token") is None
    assert agent_tokens.verify("") is None
    assert agent_tokens.verify("some-other-scheme") is None


def test_revoke_removes_one_token_and_leaves_the_rest(store):
    keep_record, keep = agent_tokens.issue("keep")
    drop_record, drop = agent_tokens.issue("drop")

    assert agent_tokens.revoke(drop_record["id"]) is True

    assert agent_tokens.verify(drop) is None
    assert agent_tokens.verify(keep) is not None
    assert [t["id"] for t in agent_tokens.list_tokens()] == [keep_record["id"]]


def test_revoking_an_unknown_id_reports_it(store):
    assert agent_tokens.revoke("nope") is False


def test_revoke_all(store):
    agent_tokens.issue("a")
    _, second = agent_tokens.issue("b")

    assert agent_tokens.revoke_all() == 2
    assert agent_tokens.verify(second) is None
    assert agent_tokens.list_tokens() == []


def test_unknown_scope_is_refused(store):
    with pytest.raises(ValueError):
        agent_tokens.issue("bad", scope="write")


def test_last_used_is_recorded_then_throttled(store, monkeypatch):
    _, plaintext = agent_tokens.issue("desktop")
    assert agent_tokens.list_tokens()[0]["last_used_at"] is None

    agent_tokens.verify(plaintext)
    first = agent_tokens.list_tokens()[0]["last_used_at"]
    assert first is not None

    # A second call inside the window must not write again — every authorized
    # request would otherwise put a DB write on the hot path.
    agent_tokens.verify(plaintext)
    assert agent_tokens.list_tokens()[0]["last_used_at"] == first


def test_corrupt_settings_row_fails_closed(store):
    _, plaintext = agent_tokens.issue("desktop")
    db.set_setting(agent_tokens.SETTINGS_KEY, "{not json")

    assert agent_tokens.verify(plaintext) is None
    assert agent_tokens.list_tokens() == []


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/agent"),
        ("GET", "/api/agent/tools"),
        ("POST", "/api/agent/tools/get_portfolio_summary"),
        ("GET", "/api/agent/context.md"),
    ],
)
def test_read_scope_allows_the_agent_surface(store, method, path):
    record, _ = agent_tokens.issue("desktop")

    assert agent_tokens.scope_allows(record, method, path) is True


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/positions"),          # reads, but not the agent surface
        ("POST", "/api/positions"),         # a write
        ("GET", "/api/backup"),             # streams the whole database
        ("DELETE", "/api/positions/1"),
        ("GET", "/api/settings/agent-tokens"),   # must never mint successors
        ("POST", "/api/settings/agent-tokens"),
        ("GET", "/api/briefings"),
        ("GET", "/api/agent-something-else"),     # prefix lookalike
    ],
)
def test_read_scope_refuses_everything_else(store, method, path):
    record, _ = agent_tokens.issue("desktop")

    assert agent_tokens.scope_allows(record, method, path) is False


def test_scope_allows_refuses_an_unknown_scope(store):
    assert agent_tokens.scope_allows({"scope": "write"}, "GET", "/api/agent/tools") is False


# ---------------------------------------------------------------------------
# Multi-user
# ---------------------------------------------------------------------------


def test_a_token_names_the_scope_that_issued_it(store):
    """Verification has to answer *whose* token this is before any query runs
    — a lookup that already knows the scope cannot establish one."""
    from backend import scope

    with scope.using("user-a"):
        _, token = agent_tokens.issue("desktop")

    assert agent_tokens.owner_of(token) == "user-a"
    assert agent_tokens.verify(token)["owner"] == "user-a"


def test_a_token_cannot_be_retargeted_at_another_account(store):
    """The owner segment decides *where to look*; the secret decides whether
    the claim was true. Swapping it finds the other account's hashes, which
    will not match."""
    from backend import scope

    with scope.using("user-a"):
        _, token = agent_tokens.issue("desktop")
    with scope.using("user-b"):
        agent_tokens.issue("theirs")

    secret = token.split(".", 1)[1]
    forged = f"{agent_tokens.TOKEN_PREFIX}{agent_tokens._encode_owner('user-b')}.{secret}"

    assert agent_tokens.verify(forged) is None
    assert agent_tokens.verify(token)["owner"] == "user-a"


def test_a_claim_to_a_scope_with_no_tokens_finds_nothing(store):
    from backend import scope

    with scope.using("user-a"):
        _, token = agent_tokens.issue("desktop")
    secret = token.split(".", 1)[1]
    stranger = f"{agent_tokens.TOKEN_PREFIX}{agent_tokens._encode_owner('nobody')}.{secret}"

    assert agent_tokens.verify(stranger) is None


def test_a_garbled_owner_segment_is_refused(store):
    assert agent_tokens.verify(agent_tokens.TOKEN_PREFIX + "!!!not-base64!!!.secret") is None
    assert agent_tokens.owner_of("not-even-ours") == ""


def test_a_legacy_token_without_an_owner_means_the_local_scope(store):
    """Tokens issued before the format existed have no owner segment. They were
    always single-user, so that is what they still mean."""
    from backend import scope

    assert agent_tokens.owner_of(agent_tokens.TOKEN_PREFIX + "oldstyletoken") == scope.LOCAL_SCOPE


def test_each_account_only_lists_its_own_tokens(store):
    from backend import scope

    with scope.using("user-a"):
        agent_tokens.issue("a-desktop")
    with scope.using("user-b"):
        agent_tokens.issue("b-desktop")

    with scope.using("user-a"):
        assert [t["name"] for t in agent_tokens.list_tokens()] == ["a-desktop"]
    with scope.using("user-b"):
        assert [t["name"] for t in agent_tokens.list_tokens()] == ["b-desktop"]


def test_one_account_cannot_revoke_anothers_token(store):
    from backend import scope

    with scope.using("user-a"):
        record, token = agent_tokens.issue("a-desktop")

    with scope.using("user-b"):
        assert agent_tokens.revoke(record["id"]) is False

    assert agent_tokens.verify(token) is not None
