"""Logging configuration — plain text by default, JSON lines on request.

``SERIN_LOG_FORMAT=json`` switches every record to a single JSON object per
line (timestamp, level, logger, message, plus any structured extras such as
the request middleware's method/path/status/duration). No bodies are ever
logged and nothing leaves the machine — this is for *your* log collector.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

_STANDARD_ATTRS = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "taskName", "message",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(log_format: str = "text") -> None:
    root = logging.getLogger()
    if root.handlers:  # uvicorn already installed handlers — restyle them
        handlers = root.handlers
    else:
        handler = logging.StreamHandler()
        root.addHandler(handler)
        handlers = [handler]
    formatter: logging.Formatter
    if (log_format or "").lower() == "json":
        formatter = JsonFormatter()
    else:
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    for handler in handlers:
        handler.setFormatter(formatter)
    root.setLevel(logging.INFO)
    # httpx logs every request URL at INFO — and providers that authenticate
    # via query string (FMP) would put their API key in the log with it.
    # Warnings and errors still come through; routine request lines don't.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
