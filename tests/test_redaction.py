"""Credentials must not reach the log.

A live FMP API key was written to the application log in production:

    BTC: FMP request failed: HTTPStatusError("Client error '429 Too Many
    Requests' for url '.../stable/profile?symbol=BTCUSD&apikey=<real key>'")

Nobody wrote code to log a secret. httpx puts the request URL in its exception
repr, the key rides in the query string, and the repr was interpolated into a
message. That is how this almost always happens — as a substring of something
else — which is why the guard is a filter on the way out rather than care at
each call site.
"""

from __future__ import annotations

import logging

import pytest
from backend.providers import fmp
from backend.redaction import REDACTED, RedactingFilter, redact

SECRET = "TosJK9SULSLdfPaBxSNmVBcv9Y0ofkjW"


def test_the_exact_production_leak_is_scrubbed():
    leaked = (
        "FMP request failed: HTTPStatusError(\"Client error '429 Too Many Requests' "
        f"for url 'https://financialmodelingprep.com/stable/profile?symbol=BTCUSD&apikey={SECRET}'\")"
    )
    cleaned = redact(leaked)
    assert SECRET not in cleaned
    assert "apikey=***" in cleaned


def test_the_parameter_name_survives():
    """"apikey=***" tells a reader which credential the call used, which is what
    makes the line worth keeping. Blanking the whole URL would not."""
    cleaned = redact("https://x.com/a?symbol=AAPL&apikey=abc123")
    assert "apikey=" in cleaned and "symbol=AAPL" in cleaned


def test_non_secret_parameters_are_left_alone():
    url = "https://x.com/a?symbol=AAPL&period=1y&limit=100"
    assert redact(url) == url


@pytest.mark.parametrize("param", [
    "apikey", "api_key", "token", "access_token", "secret",
    "client_secret", "consumer_key", "password", "signature",
])
def test_every_credential_parameter_is_covered(param):
    assert SECRET not in redact(f"https://x.com/a?{param}={SECRET}&symbol=AAPL")


def test_matching_is_case_insensitive():
    assert SECRET not in redact(f"https://x.com/a?APIKey={SECRET}")


def test_a_secret_at_the_end_of_a_url_is_still_caught():
    """No trailing ampersand to anchor against — a naive pattern misses it."""
    assert SECRET not in redact(f"https://x.com/a?symbol=AAPL&apikey={SECRET}")


def test_redaction_stops_at_the_quote_that_ends_the_url():
    """The leak arrived wrapped in an exception repr, so the value is followed
    by a quote and a paren rather than whitespace. Swallowing those would
    mangle the rest of the message."""
    cleaned = redact(f"error for url 'https://x.com/a?apikey={SECRET}') and then more text")
    assert SECRET not in cleaned
    assert "and then more text" in cleaned
    # The delimiters have to survive, or the message stops being parseable and
    # a reader cannot tell where the URL ended. A pattern that stops only at
    # whitespace eats them and still passes the assertions above.
    assert cleaned.endswith("apikey=***') and then more text"), cleaned


# --- the provider stops handing the secret onward --------------------------


def test_fmp_request_errors_no_longer_carry_the_key(monkeypatch):
    """The message goes to callers and into the UI as well as the log, so
    scrubbing only at the logger would still expose it on screen."""
    import httpx

    class Boom:
        @staticmethod
        def get(url, params=None, timeout=None):
            raise httpx.HTTPStatusError(
                f"Client error '429' for url '{url}?apikey={params['apikey']}'",
                request=None, response=None,
            )

    monkeypatch.setattr(fmp, "httpx", Boom, raising=False)
    monkeypatch.setitem(__import__("sys").modules, "httpx", Boom)
    payload, error = fmp._get("stable/profile", {"symbol": "BTCUSD"}, api_key=SECRET)
    assert payload is None
    assert SECRET not in (error or ""), "the provider handed the key back to its caller"


# --- the net ---------------------------------------------------------------


def test_the_log_filter_scrubs_a_formatted_message():
    record = logging.LogRecord(
        name="t", level=logging.WARNING, pathname="", lineno=0,
        msg="failed: %s", args=(f"https://x.com/a?apikey={SECRET}",), exc_info=None,
    )
    RedactingFilter().filter(record)
    assert SECRET not in record.getMessage()


def test_the_filter_reads_args_not_just_the_template():
    """The secret lives in record.args until the message is formatted. A scan
    of record.msg alone would never see it — which is the whole trap."""
    record = logging.LogRecord(
        name="t", level=logging.WARNING, pathname="", lineno=0,
        msg="Quote sweep finished with errors: %s",
        args=([f"BTC: for url 'https://f.com/p?symbol=BTCUSD&apikey={SECRET}'"],),
        exc_info=None,
    )
    RedactingFilter().filter(record)
    assert SECRET not in record.getMessage()
    assert REDACTED in record.getMessage()


def test_a_clean_record_is_left_exactly_as_it_was():
    record = logging.LogRecord(
        name="t", level=logging.INFO, pathname="", lineno=0,
        msg="synced %d positions", args=(21,), exc_info=None,
    )
    RedactingFilter().filter(record)
    assert record.getMessage() == "synced 21 positions"
    assert record.args == (21,), "an untouched record should keep its args"


def test_the_filter_never_drops_a_record():
    """A filter returning False silently deletes the log line. Losing
    observability to protect a secret that was not there would be a poor trade."""
    record = logging.LogRecord(
        name="t", level=logging.INFO, pathname="", lineno=0,
        msg="hello", args=(), exc_info=None,
    )
    assert RedactingFilter().filter(record) is True


def test_a_record_that_cannot_be_formatted_is_still_passed_through():
    record = logging.LogRecord(
        name="t", level=logging.INFO, pathname="", lineno=0,
        msg="needs %d args", args=(), exc_info=None,
    )
    assert RedactingFilter().filter(record) is True
