"""Briefing push notifications via Expo's push API.

The mobile app registers its Expo push token here; when a scheduled briefing
finishes, the backend sends one "briefing ready" message per registered
device. Tokens live in app_settings (local only). Delivery is best-effort —
push must never break a briefing — and tokens Expo reports as
DeviceNotRegistered are pruned automatically.
"""

from __future__ import annotations

import json
import logging

from backend import db

logger = logging.getLogger(__name__)

TOKENS_KEY = "expo_push_tokens"
EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"


def list_tokens() -> list[str]:
    raw = db.get_setting(TOKENS_KEY, "")
    try:
        tokens = json.loads(raw) if raw else []
        return [t for t in tokens if isinstance(t, str) and t]
    except ValueError:
        return []


def register_token(token: str) -> list[str]:
    token = (token or "").strip()
    if not token.startswith(("ExponentPushToken[", "ExpoPushToken[")):
        raise ValueError("Not an Expo push token.")
    tokens = list_tokens()
    if token not in tokens:
        tokens.append(token)
        db.set_setting(TOKENS_KEY, json.dumps(tokens))
    return tokens


def _prune(bad: set[str]) -> None:
    if not bad:
        return
    remaining = [t for t in list_tokens() if t not in bad]
    db.set_setting(TOKENS_KEY, json.dumps(remaining))


def send_briefing_ready(summary: str = "") -> int:
    """Send 'briefing ready' to every registered device. Returns sends attempted."""
    tokens = list_tokens()
    if not tokens:
        return 0
    body = (summary or "Your daily portfolio briefing is ready.")[:170]
    messages = [
        {"to": token, "title": "Serin briefing ready", "body": body, "sound": "default"}
        for token in tokens
    ]
    try:
        import httpx

        response = httpx.post(EXPO_PUSH_URL, json=messages, timeout=15)
        response.raise_for_status()
        payload = response.json()
        bad: set[str] = set()
        for token, ticket in zip(tokens, payload.get("data") or [], strict=False):
            if isinstance(ticket, dict) and ticket.get("status") == "error":
                details = ticket.get("details") or {}
                if details.get("error") == "DeviceNotRegistered":
                    bad.add(token)
                logger.warning("Expo push error for %s…: %s", token[:24], ticket.get("message"))
        _prune(bad)
    except Exception:
        logger.warning("Briefing push failed; briefing itself is unaffected", exc_info=True)
    return len(tokens)
