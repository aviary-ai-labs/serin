"""Keep credentials out of anything that gets written down.

A provider error carried an API key into the application log:

    BTC: FMP request failed: HTTPStatusError("Client error '429 Too Many
    Requests' for url 'https://financialmodelingprep.com/stable/profile
    ?symbol=BTCUSD&apikey=<the real key>'")

httpx puts the request URL in its exception repr, the key rides in the query
string, and the repr went straight to the logger. Nobody wrote code to log a
secret; it arrived as a substring of an error message, which is how this
almost always happens.

So redaction lives in two places on purpose. Providers scrub the message they
build, so the text handed back to callers and shown in the UI is clean at the
source. A logging filter scrubs everything on the way out, because the next
leak will come from a library nobody has thought about yet.
"""

from __future__ import annotations

import logging
import re

#: Query parameters whose values are credentials. Matched case-insensitively.
_SECRET_PARAMS = (
    "apikey", "api_key", "access_token", "token", "secret",
    "client_secret", "consumer_key", "password", "signature",
)

_QUERY_SECRET = re.compile(
    r"([?&](?:" + "|".join(_SECRET_PARAMS) + r")=)([^&\s'\"\\)]+)",
    re.IGNORECASE,
)

REDACTED = "***"


def redact(text: str) -> str:
    """Replace credential values in ``text`` with ``***``.

    Deliberately leaves the parameter *name* in place: "apikey=***" tells a
    reader which credential the request used, which is what makes the log
    useful, while "***" alone tells them nothing.
    """
    if not text:
        return text
    return _QUERY_SECRET.sub(lambda m: m.group(1) + REDACTED, text)


class RedactingFilter(logging.Filter):
    """Scrub credentials from log records, whatever produced them.

    Formats the record's own arguments first: a message logged as
    ``("failed: %s", url)`` keeps the secret in ``record.args`` where a scan
    of ``record.msg`` alone would never see it.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True  # a broken record is the logger's problem, not ours
        cleaned = redact(message)
        if cleaned != message:
            record.msg = cleaned
            record.args = ()
        return True
