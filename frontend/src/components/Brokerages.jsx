import React, { useCallback, useEffect, useRef, useState } from 'react';
import { BrokerAccounts } from './BrokerAccounts.jsx';
import { api } from '../api.js';
import { brokerLabel, dateShort } from '../format.js';

/**
 * Brokerage connections.
 *
 * Deliberately separate from the Connectors tab. Connectors configures the
 * *server* — provider keys, AI credentials, pairing — which on a hosted plan
 * is not the customer's to configure, so that whole surface is hidden from
 * them. Connecting your own brokerage is the opposite: it is the most
 * personal thing in the product, and hiding it behind the same flag left
 * Cloud customers with no way to do it at all.
 *
 * Credentials never reach Serin. We ask SnapTrade for a one-time portal URL
 * and hand the user to it; the broker login and MFA happen on SnapTrade's
 * domain, and we get back a read-only authorization.
 */
export function Brokerages({ onError, onChanged, onViewTransactions }) {
  const [status, setStatus] = useState(null);
  const [busy, setBusy] = useState('');
  const [notice, setNotice] = useState('');

  const load = useCallback(async () => {
    try {
      setStatus(await api('/api/v1/broker/status'));
    } catch (error) {
      onError?.(error.message || 'Could not load brokerage status');
    }
  }, [onError]);

  useEffect(() => { load(); }, [load]);

  // Connecting a broker and syncing it are two separate acts: the portal hands
  // back an authorization and nothing pulls holdings until something asks. So
  // a freshly connected account showed an empty Brokerages page and an
  // unchanged dashboard, which reads as a failed connection rather than an
  // unfinished one. Sync it once, automatically, the first time we see a
  // connection that has never produced holdings.
  const autoSynced = useRef(false);
  const previousNotice = useRef('');
  useEffect(() => {
    if (!status?.pending_sync || autoSynced.current || busy) return;
    autoSynced.current = true;   // once per mount — never a retry loop
    // Holdings first, then the ledger. Holdings alone make the dashboard
    // right; the ledger is what makes the *return* right, and a new customer
    // comparing Serin's number against their broker's will not know to press
    // a second button to get it.
    (async () => {
      await sync({ automatic: true });
      await backfill({ automatic: true });
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status?.pending_sync]);

  async function connect() {
    setBusy('connect');
    setNotice('');
    try {
      // The field is `redirect_uri`, matching the route. Reading `url` here
      // cost an afternoon: SnapTrade returned a perfectly good portal link,
      // the route answered 200 with it, and this threw a message identical to
      // the backend's own — so the failure read as SnapTrade's, not ours.
      const { redirect_uri: portalUrl } = await api('/api/v1/broker/connect', {
        method: 'POST',
        body: JSON.stringify({ redirect: window.location.href }),
      });
      if (!portalUrl) throw new Error('Serin could not read the connection link from the server.');
      // A full navigation, not a popup: the portal runs an OAuth-style flow
      // per broker and popup blockers break it in ways users cannot diagnose.
      window.location.href = portalUrl;
    } catch (error) {
      onError?.(error.message || 'Could not start the connection');
      setBusy('');
    }
  }

  async function sync({ automatic = false } = {}) {
    setBusy('sync');
    setNotice(automatic ? 'New connection found — pulling your holdings…' : '');
    try {
      const result = await api('/api/v1/broker/sync', { method: 'POST', body: '{}' });
      // `positions` and `accounts` are what the route returns. Reading
      // `upserted` here reported "Synced 0 holdings" after a sync that had
      // just pulled ten across four accounts — and `?? 0` turned the missing
      // field into a plausible number instead of an obvious undefined.
      const synced = result.positions ?? 0;
      const accounts = result.accounts ?? 0;
      const message =
        `Synced ${synced} ${synced === 1 ? 'holding' : 'holdings'}` +
        (accounts ? ` from ${accounts} ${accounts === 1 ? 'account' : 'accounts'}.` : '.');
      previousNotice.current = message;   // so the backfill can append to it
      setNotice(message);
      onChanged?.();
      await load();
    } catch (error) {
      onError?.(error.message || 'Sync failed');
    } finally {
      setBusy('');
    }
  }

  async function backfill({ automatic = false } = {}) {
    setBusy('backfill');
    // On the automatic path the holdings notice is already on screen and is
    // still true; clearing it would blank the panel mid-flow for the several
    // seconds a full history takes.
    if (!automatic) setNotice('');
    try {
      const result = await api('/api/v1/broker/backfill', {
        // No window: the account's whole history. Asking for a year was our
        // own limit and it is what turned older purchases into sales with no
        // cost basis, which cannot be counted as gains at all.
        method: 'POST', body: JSON.stringify({}),
      });
      // Re-running is safe and normal, so say what actually happened rather
      // than implying every run should import something.
      setNotice(
        (automatic ? `${previousNotice.current} ` : '') +
        `Imported ${result.imported} transactions` +
        (result.skipped_existing ? `, skipped ${result.skipped_existing} already on record` : '') +
        // Named separately from "already on record": these matched a row that
        // arrived from somewhere else (a broker CSV), which is the case people
        // are surprised by and the one worth showing.
        (result.skipped_duplicate ? `, ${result.skipped_duplicate} already imported from a statement` : '') +
        '.'
      );
      onChanged?.();
    } catch (error) {
      onError?.(error.message || 'Backfill failed');
    } finally {
      setBusy('');
    }
  }

  async function disconnect(connection) {
    const label = brokerLabel(connection.institution || 'this brokerage');
    if (!window.confirm(
      `Disconnect ${label}?\n\nSerin stops syncing it and removes the holdings it synced. ` +
      `Your transaction history stays, so past performance is unaffected.`
    )) return;
    setBusy(connection.id);
    try {
      await api(`/api/v1/broker/connections/${encodeURIComponent(connection.id)}`, {
        method: 'DELETE',
      });
      onChanged?.();
      await load();
    } catch (error) {
      onError?.(error.message || 'Could not disconnect');
    } finally {
      setBusy('');
    }
  }

  if (!status) return <section className="panel"><p className="muted">Loading…</p></section>;

  if (!status.configured) {
    return (
      <section className="panel">
        <div className="panel-header"><h2>Not enabled</h2></div>
        <p className="brokerage-empty muted">
          Brokerage sync is not configured on this instance.
        </p>
      </section>
    );
  }

  const connections = status.connections || [];

  return (
    <section className="panel brokerages">
      {/* .panel carries no horizontal padding — every child supplies its own,
          which is why .panel-header exists. The page title above already says
          "Brokerages", so this header states the one thing that is not
          obvious instead of repeating it. */}
      <div className="panel-header">
        <h2>Read-only — Serin can never place a trade</h2>
        <button className="btn btn-primary" disabled={busy === 'connect'} onClick={connect}>
          {busy === 'connect' ? 'Opening…' : 'Connect a brokerage'}
        </button>
      </div>

      {notice && <p className="brokerage-notice">{notice}</p>}

      {connections.length === 0 ? (
        <p className="muted brokerage-empty">
          No brokerages connected yet. Your login goes to your broker, never to Serin —
          we only receive positions and activity.
        </p>
      ) : null}

      {connections.length > 0 && (
        <>
        {/* The connections above say which institutions are linked; this says
            which accounts they actually brought, what each holds, and when
            Serin last heard from it. "Robinhood — Connected" was true of four
            accounts at once and described none of them. */}
        <BrokerAccounts refreshKey={status?.last_sync?.at || ''}
                        onViewTransactions={onViewTransactions}
                        connections={connections}
                        busy={busy}
                        onDisconnect={disconnect} />

        <div className="brokerage-actions">
          <button className="btn btn-ghost btn-sm" disabled={busy === 'sync'} onClick={sync}>
            {busy === 'sync' ? 'Syncing…' : 'Sync holdings now'}
          </button>
          <button className="btn btn-ghost btn-sm" disabled={busy === 'backfill'} onClick={backfill}>
            {busy === 'backfill' ? 'Importing…' : 'Import transaction history'}
          </button>
          {status.last_sync?.at && (
            // last_sync is the whole sync summary, not a timestamp — passing
            // the object to a date formatter rendered "Invalid Date".
            <span className="muted">Last synced {dateShort(status.last_sync.at)}</span>
          )}
        </div>
        </>
      )}

      <p className="muted brokerage-foot">
        Importing transaction history is what makes returns transaction-accurate
        rather than estimated — it is the only way Serin learns about deposits,
        withdrawals and positions you have already sold.
      </p>
    </section>
  );
}
