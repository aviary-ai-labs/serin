"""Market data providers.

Each provider exposes the same three-function interface so the orchestrator in
``backend.prices`` can route refresh and history requests without caring about
the upstream. New providers slot in by adding a module and registering it in
``PROVIDERS``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from backend.providers import fmp, yahoo


class _Provider(Protocol):
    name: str

    def refresh_prices(self, positions): ...  # type: ignore[no-untyped-def]
    def fetch_history(self, period: str, symbols, positions_by_symbol): ...  # type: ignore[no-untyped-def]
    def quote(self, symbol: str, asset_type: str): ...  # type: ignore[no-untyped-def]


PROVIDERS: dict[str, Callable[[], _Provider]] = {
    "fmp": fmp.provider,
    "yahoo": yahoo.provider,
}


def get_provider(name: str):
    factory = PROVIDERS.get(name)
    if factory is None:
        return None
    return factory()


__all__ = ["PROVIDERS", "get_provider", "fmp", "yahoo"]
