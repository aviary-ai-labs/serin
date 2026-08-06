import React from 'react';
import { brokerLabel, timeAgo } from '../format.js';
import { IconLink } from './Icons.jsx';

export function ConnectionsPanel({ status, busy, onConnect, onSync, onDisconnect, onBackfill, configured }) {
  const connections = status?.connections || [];
  const lastSync = status?.last_sync;
  const registered = status?.registered;

  if (!configured) {
    return (
      <section className="panel">
        <div className="panel-header">
          <h2>Brokerage Sync</h2>
          <span className="broker-tag broker-manual">Not configured</span>
        </div>
        <div className="side-card-body">
          <p className="schedule-hint">
            Connect your brokerages read-only to sync holdings automatically. Set{' '}
            <code>SNAPTRADE_CLIENT_ID</code> and <code>SNAPTRADE_CONSUMER_KEY</code> in{' '}
            <code>.env</code> (free for one connected user at snaptrade.com), then restart Serin.
          </p>
        </div>
      </section>
    );
  }

  return (
    <section className="panel">
      <div className="panel-header">
        <h2>Brokerage Sync</h2>
        {registered && connections.length > 0 && (
          <span style={{ display: 'inline-flex', gap: 12 }}>
            {onBackfill && (
              <button
                className="link-btn"
                title="Import broker transaction history (dividends, buys, sells) — safe to re-run"
                disabled={busy === 'broker-backfill'}
                onClick={onBackfill}
              >
                {busy === 'broker-backfill' ? 'Importing…' : 'Import history'}
              </button>
            )}
            <button className="link-btn" disabled={busy === 'broker-sync'} onClick={onSync}>
              {busy === 'broker-sync' ? 'Syncing…' : 'Sync now'}
            </button>
          </span>
        )}
      </div>
      <div className="side-card-body">
        {connections.length === 0 ? (
          <p className="schedule-hint">
            No brokerages connected yet. Serin links read-only through SnapTrade — it can see
            holdings and balances, never place trades, and you can disconnect anytime.
          </p>
        ) : (
          <div className="conn-list">
            {connections.map(conn => (
              <div className="conn-row" key={conn.id}>
                <div className="conn-meta">
                  <span className={`broker-tag broker-${brokerSlug(conn.institution)}`}>{conn.institution}</span>
                  {conn.disabled && <span className="conn-warn">reconnect required</span>}
                </div>
                <button
                  className="link-btn danger"
                  disabled={busy === `broker-disconnect-${conn.id}`}
                  onClick={() => onDisconnect(conn)}
                >
                  {busy === `broker-disconnect-${conn.id}` ? 'Removing…' : 'Disconnect'}
                </button>
              </div>
            ))}
          </div>
        )}

        <button className="btn btn-primary" style={{ width: '100%' }} disabled={busy === 'broker-connect'} onClick={onConnect}>
          <IconLink /> {busy === 'broker-connect' ? 'Opening…' : connections.length ? 'Connect another brokerage' : 'Connect a brokerage'}
        </button>

        {lastSync && (
          <div className="conn-sync-note">
            {lastSync.error
              ? <span className="negative">Last sync failed: {lastSync.error}</span>
              : <>Last synced {timeAgo(lastSync.at)} · {lastSync.positions} positions{lastSync.removed ? ` · ${lastSync.removed} closed` : ''}</>}
          </div>
        )}
        <p className="conn-trust">Read-only via SnapTrade · Serin cannot place trades</p>
      </div>
    </section>
  );
}

function brokerSlug(name) {
  return String(name || '').toLowerCase().replace(/[^a-z0-9]+/g, '');
}
