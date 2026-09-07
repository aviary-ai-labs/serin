"""``scheduler_loop`` — the shape of one tick, not the decisions inside it.

``decide()`` is tested on its own. What this file pins down is the pass around
it: who gets listed, how many times, and what still happens when part of the
pass fails. This loop is the thing that spends money unprompted, so its
failure modes are worth more coverage than its happy path.

The loop is a ``while True`` whose tick ends at the sleep, so raising there is
how a test gets exactly one pass and no more.
"""

from __future__ import annotations

import asyncio

import pytest
from backend import scheduler, scope


class _Recorder:
    """What one tick did, in the order it did it."""

    def __init__(self) -> None:
        self.listed = 0
        self.priced: list[str] = []
        self.briefed: list[str] = []
        self.synced: list[str] = []


def _run_one_tick() -> None:
    async def _go() -> None:
        try:
            await scheduler.scheduler_loop()
        except asyncio.CancelledError:
            pass  # the stubbed sleep, i.e. the tick finished

    asyncio.run(_go())


@pytest.fixture
def tick(monkeypatch):
    """One tick with every job stubbed, recording who it was run for."""
    rec = _Recorder()

    def _all_scopes() -> list[str]:
        rec.listed += 1
        return ["alice", "bob"]

    async def _noop() -> None:
        pass

    async def _price(owner: str) -> None:
        rec.priced.append(owner)

    async def _check() -> bool:
        rec.briefed.append(scope.current())
        return False

    async def _sync() -> list[str]:
        rec.synced.append(scope.current())
        return []

    async def _stop(_seconds: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(scope, "all_scopes", _all_scopes)
    monkeypatch.setattr(scheduler, "maybe_refresh_tracked_quotes", _noop)
    monkeypatch.setattr(scheduler, "maybe_refresh_tracked_history", _noop)
    monkeypatch.setattr(scheduler, "maybe_sync_position_prices", _price)
    monkeypatch.setattr(scheduler, "check_once", _check)
    monkeypatch.setattr(scheduler, "maybe_auto_sync_connectors", _sync)
    monkeypatch.setattr(asyncio, "sleep", _stop)
    return rec


def test_a_tick_lists_the_scopes_once(tick):
    """The regression this exists for.

    Both passes used to call the lister themselves. On Cloud that lister is a
    database round trip that also sweeps lapsed trials, so a tick paid for it
    twice to get the same answer.
    """
    _run_one_tick()

    assert tick.listed == 1


def test_both_passes_still_visit_every_owner(tick):
    """Sharing one list must not cost anyone their pass."""
    _run_one_tick()

    assert tick.priced == ["alice", "bob"]
    assert tick.briefed == ["alice", "bob"]
    assert tick.synced == ["alice", "bob"]


def test_a_failing_scope_list_skips_the_tick_rather_than_killing_the_loop(tick, monkeypatch):
    """There is nobody to act for, which is what each pass concluded on its
    own before. The loop has to survive to try again next tick — a scheduler
    that dies on one bad listing stops briefing everyone, forever."""
    def _boom() -> list[str]:
        raise scope.ScopeError("lister exploded")

    monkeypatch.setattr(scope, "all_scopes", _boom)

    _run_one_tick()  # reaching the sleep at all is the assertion

    assert tick.briefed == []
    assert tick.synced == []


def test_a_briefing_failure_still_lets_the_connector_pass_run(tick, monkeypatch):
    """The two passes keep separate error isolation: holdings still sync on a
    day the briefing check throws."""
    async def _explode() -> bool:
        raise RuntimeError("briefing check failed")

    monkeypatch.setattr(scheduler, "check_once", _explode)

    _run_one_tick()

    assert tick.briefed == []
    assert tick.synced == ["alice", "bob"]
