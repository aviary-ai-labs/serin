"""Which account an AI request is billed to.

Serin's managed-AI proxy meters on the licence key. That is right for a
self-hosted licence, where the holder and the person are the same — and wrong
for a shared deployment, which holds one licence for its whole fleet, so every
customer's usage lands in one pool and the first heavy user exhausts the month
for everybody.

The request has to say who it is for. Anthropic ignores the extra header; the
proxy meters on it.
"""

from __future__ import annotations

import pytest
from backend import ai_provider, scope


@pytest.fixture(autouse=True)
def no_provider():
    yield
    scope.set_scope_provider(None)


def test_single_user_install_sends_no_subject():
    """Nothing to disambiguate, and nothing gained by labelling it."""
    assert ai_provider.billing_subject() == ""
    assert "x-serin-subject" not in ai_provider.anthropic_headers()


def test_shared_deployment_names_the_account():
    scope.set_scope_provider(lambda: "u_abc123")
    with scope.using("u_abc123"):
        assert ai_provider.anthropic_headers()["x-serin-subject"] == "u_abc123"


def test_two_accounts_are_billed_separately():
    scope.set_scope_provider(lambda: "unused")
    with scope.using("user-a"):
        a = ai_provider.anthropic_headers()["x-serin-subject"]
    with scope.using("user-b"):
        b = ai_provider.anthropic_headers()["x-serin-subject"]
    assert a != b


def test_background_work_without_a_user_still_sends_the_request():
    """The scheduler runs with nobody in context. A metering label is not worth
    failing a briefing over — it meters against the licence as a whole, which
    is where that work genuinely belongs."""

    def raises() -> str:
        raise RuntimeError("no authenticated user in this request context")

    scope.set_scope_provider(raises)
    assert ai_provider.billing_subject() == ""
    headers = ai_provider.anthropic_headers()
    assert "x-serin-subject" not in headers
    assert "x-api-key" in headers  # the request still goes


def test_the_usual_headers_are_still_there():
    headers = ai_provider.anthropic_headers()
    assert headers["anthropic-version"] == "2023-06-01"
    assert headers["content-type"] == "application/json"
