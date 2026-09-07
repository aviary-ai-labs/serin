#!/usr/bin/env python3
"""Fetch prices where they work, and post them where they are needed.

Serin's providers run wherever Serin runs, which is not always where they
succeed. Yahoo answers a residential address and returns 429 to a datacenter
one — the same code that fails on a Fly machine works on a laptop at home. So
this runs the fetch from a machine with a residential address and posts the
result into Serin's shared quote cache.

It deliberately reuses Serin's own provider code rather than parsing pages
itself. There is no new scraper to keep working when a layout changes: if
Serin can price a symbol, so can this, and any provider improvement lands here
for free.

    export SERIN_URL=https://www.serin.money
    export SERIN_PRICE_RELAY_TOKEN=...        # same value as the server's
    python3 scripts/price_relay.py --interval 60

Run it under launchd (see --install-help) to keep it up across reboots.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

LAUNCHD_HELP = """\
Keep it running across reboots with launchd:

  cat > ~/Library/LaunchAgents/money.serin.relay.plist <<'PLIST'
  <?xml version="1.0" encoding="UTF-8"?>
  <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
  <plist version="1.0"><dict>
    <key>Label</key><string>money.serin.relay</string>
    <key>ProgramArguments</key><array>
      <string>/usr/bin/python3</string>
      <string>REPO/scripts/price_relay.py</string>
      <string>--interval</string><string>60</string>
    </array>
    <key>EnvironmentVariables</key><dict>
      <key>SERIN_URL</key><string>https://www.serin.money</string>
      <key>SERIN_PRICE_RELAY_TOKEN</key><string>PUT-TOKEN-HERE</string>
    </dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>/tmp/serin-relay.log</string>
    <key>StandardErrorPath</key><string>/tmp/serin-relay.err</string>
  </dict></plist>
  PLIST

  launchctl load ~/Library/LaunchAgents/money.serin.relay.plist

The token is read from the environment, never passed on the command line —
an argument is visible to every process on the machine via `ps`.
"""


def _post(url: str, token: str, payload: dict, timeout: int = 30) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode() or "{}")


def _symbols_from(serin_url: str, token: str) -> list[tuple[str, str]]:
    """What this deployment actually holds, so the relay prices that and no more.

    Raises rather than returning an empty list. Swallowing the failure here
    made a wrong token, a missing endpoint, a session gate in the way and a
    DNS failure all print the same "no symbols to price", which is a sentence
    that describes none of them and points at the wrong fix.
    """
    request = urllib.request.Request(
        f"{serin_url.rstrip('/')}/api/v1/prices/tracked",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        rows = json.loads(response.read().decode() or "[]")
    return [(r["symbol"], r.get("asset_type", "stock")) for r in rows]


def sweep(serin_url: str, token: str, symbols: list[tuple[str, str]]) -> dict:
    """One pass: price everything locally, post whatever came back."""
    from backend.models import Position
    from backend.providers import yahoo

    stand_ins = [
        Position(id=0, symbol=symbol, name=symbol, broker="", asset_type=asset_type,
                 quantity=0.0)
        for symbol, asset_type in symbols
    ]
    result = yahoo.provider().refresh_prices(stand_ins)
    prices = result.get("prices") or {}
    by_type = dict(symbols)

    quotes = [
        {
            "symbol": symbol,
            "price": price,
            "asset_type": by_type.get(symbol, "stock"),
            "sector": sector or "",
        }
        for symbol, (price, sector) in prices.items()
    ]
    if not quotes:
        return {"fetched": 0, "written": 0, "errors": result.get("errors", [])[:3]}

    posted = _post(
        f"{serin_url.rstrip('/')}/api/v1/prices/ingest",
        token,
        {"quotes": quotes, "source": "mac-relay"},
    )
    return {
        "fetched": len(quotes),
        "written": posted.get("written", 0),
        "errors": result.get("errors", [])[:3],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=int, default=60,
                        help="seconds between sweeps (default 60)")
    parser.add_argument("--once", action="store_true", help="one sweep, then exit")
    parser.add_argument("--symbols", default="",
                        help="comma-separated override; default asks the server")
    parser.add_argument("--install-help", action="store_true",
                        help="print a launchd plist and exit")
    args = parser.parse_args()

    if args.install_help:
        print(LAUNCHD_HELP.replace("REPO", str(REPO_ROOT)))
        return 0

    serin_url = os.environ.get("SERIN_URL", "").strip()
    token = os.environ.get("SERIN_PRICE_RELAY_TOKEN", "").strip()
    if not serin_url or not token:
        print("Set SERIN_URL and SERIN_PRICE_RELAY_TOKEN in the environment.",
              file=sys.stderr)
        return 2

    while True:
        started = time.time()
        try:
            symbols = (
                [(s.strip().upper(), "stock") for s in args.symbols.split(",") if s.strip()]
                if args.symbols else _symbols_from(serin_url, token)
            )
            if not symbols:
                print("this deployment tracks no symbols yet — add a holding first",
                      flush=True)
            else:
                outcome = sweep(serin_url, token, symbols)
                print(
                    f"{time.strftime('%H:%M:%S')} "
                    f"fetched {outcome['fetched']}/{len(symbols)} "
                    f"written {outcome['written']}"
                    + (f" errors {outcome['errors']}" if outcome["errors"] else ""),
                    flush=True,
                )
        except urllib.error.HTTPError as exc:
            # Each code means something different, and saying which saves the
            # reader from checking the one thing that was already right.
            reason = {
                404: "wrong token, or the relay is not enabled on the server "
                     "(SERIN_PRICE_RELAY_TOKEN unset)",
                401: "the server's session lock is in front of the relay — this "
                     "build predates the exemption for /api/v1/prices/*",
                413: "too many symbols in one post",
            }.get(exc.code, "unexpected response")
            print(f"HTTP {exc.code}: {reason}", flush=True)
        except urllib.error.URLError as exc:
            print(f"cannot reach {serin_url}: {exc.reason}", flush=True)
        except Exception as exc:                      # never let one pass end the loop
            print(f"sweep failed: {exc!r}", flush=True)

        if args.once:
            return 0
        time.sleep(max(5.0, args.interval - (time.time() - started)))


if __name__ == "__main__":
    raise SystemExit(main())
