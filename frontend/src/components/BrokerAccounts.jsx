import React, { useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../api.js';
import { brokerLabel, money } from '../format.js';

/**
 * The accounts behind a connection, one row each.
 *
 * Serin's own tables key on broker, so three institutions collapsed six real
 * accounts into three rows: an IRA, a crypto account and a taxable account at
 * one broker were indistinguishable once their holdings landed, and the screen
 * could only say "Robinhood — Connected". That is not what anyone recognises.
 * What they recognise is the account they opened, its last four digits, and
 * what it holds — so that is what this shows.
 */
export function BrokerAccounts({ refreshKey, onViewTransactions,
                                 connections = [], busy, onDisconnect }) {
  const [data, setData] = useState(null);
  const [open, setOpen] = useState(null);

  useEffect(() => {
    let alive = true;
    api('/api/v1/broker/accounts')
      .then(payload => { if (alive) setData(payload); })
      .catch(() => { if (alive) setData({ accounts: [] }); });
    return () => { alive = false; };
  }, [refreshKey]);

  // Keyed by connection, not by account, so an institution that is linked but
  // has produced nothing yet still appears — that is precisely the state worth
  // seeing, and grouping by account would hide it.
  const grouped = useMemo(() => {
    const byInstitution = new Map();
    for (const connection of connections) {
      byInstitution.set(slug(connection.institution), { connection, accounts: [] });
    }
    for (const account of data?.accounts || []) {
      const key = slug(account.institution);
      if (!byInstitution.has(key)) byInstitution.set(key, { connection: null, accounts: [] });
      byInstitution.get(key).accounts.push(account);
    }
    return [...byInstitution.entries()];
  }, [data, connections]);

  if (!data) return null;
  if (!grouped.length) return null;

  const total = (data.accounts || []).reduce((sum, a) => sum + (a.value || 0), 0);

  return (
    <div className="accounts">
      {data.accounts?.length > 0 && (
        <div className="accounts-head">
          <span className="accounts-total">
            {money(total)} across {data.accounts.length} account
            {data.accounts.length === 1 ? '' : 's'}
          </span>
        </div>
      )}
      {data.error && <p className="muted brokerage-meta">{data.error}</p>}

      {grouped.map(([institution, { connection, accounts }]) => (
        <div key={institution} className="accounts-group">
          {/* The institution and its Disconnect share one line with the
              accounts beneath it. They used to be separate rows in separate
              sections, so every broker cost two rows to say one thing. */}
          <div className="accounts-group-head">
            <div className="accounts-group-id">
              <b>{brokerLabel(institution)}</b>
              <span className="account-sub">{connectionState(connection, accounts)}</span>
            </div>
            {connection && onDisconnect && (
              <button type="button" className="btn btn-ghost btn-sm"
                      disabled={busy === connection.id}
                      onClick={() => onDisconnect(connection)}>
                {busy === connection.id ? 'Removing…' : 'Disconnect'}
              </button>
            )}
          </div>
          <ul className="accounts-list">
            {accounts.map(account => (
              <li key={account.id}>
                <button type="button" className="account-row"
                        onClick={() => setOpen(account)}>
                  <span className="account-id">
                    <b>{account.name}</b>
                    <span className="account-sub">
                      {[account.type, account.number].filter(Boolean).join(' \u00b7 ')}
                    </span>
                  </span>
                  <span className="account-value">
                    <b>{money(account.value)}</b>
                    <span className="account-sub">{syncedLabel(account)}</span>
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      ))}

      {open && (
        <AccountDetail account={open} onClose={() => setOpen(null)}
                       onViewTransactions={onViewTransactions} />
      )}
    </div>
  );
}

/**
 * One account's details.
 *
 * Two blocks, because they answer two different worries: what this account is,
 * and whether Serin is still hearing from it. The second is the one that
 * matters when a number looks stale, and it was previously unanswerable —
 * "Connected" said nothing about when anything last arrived.
 */
function AccountDetail({ account, onClose, onViewTransactions }) {
  const closeRef = useRef(null);

  useEffect(() => {
    const onKey = event => { if (event.key === 'Escape') onClose(); };
    document.addEventListener('keydown', onKey);
    closeRef.current?.focus();
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose]);

  return (
    <div className="drill-backdrop" onClick={onClose} role="presentation">
      <div className="drill account-drill" role="dialog" aria-modal="true"
           aria-label={`${account.name} details`}
           onClick={event => event.stopPropagation()}>
        <div className="drill-head">
          <div>
            <h3>{account.name}</h3>
            <p className="drill-sub">{account.institution_name}</p>
          </div>
          <button type="button" className="btn btn-ghost btn-tiny"
                  ref={closeRef} onClick={onClose}>Close</button>
        </div>

        <div className="account-value-big">
          <b>{money(account.value)}</b>
          <span>{account.currency || 'USD'}</span>
        </div>

        <dl className="account-facts">
          <dt>Financial institution</dt>
          <dd>{account.institution_name}</dd>
          <dt>Account number</dt>
          <dd>{account.number || 'Not reported'}</dd>
          <dt>Account type</dt>
          <dd>{account.type || 'Not reported'}</dd>
          <dt>Status</dt>
          <dd className={account.status === 'open' ? 'pos' : ''}>
            {account.status ? titleCase(account.status) : 'Unknown'}
          </dd>
        </dl>

        <h4 className="account-section">Connection</h4>
        <dl className="account-facts">
          <dt>Linked</dt>
          <dd>{account.linked_at || 'Unknown'}</dd>
          {/* Holdings and transactions sync on separate schedules, and it is
              routine for one to be days behind the other. Reporting a single
              "last synced" hid exactly the case worth seeing: positions
              current, ledger stale. */}
          <dt>Holdings</dt>
          <dd>{agoLabel(account.holdings_synced_at)}</dd>
          <dt>Transactions</dt>
          <dd>{agoLabel(account.transactions_synced_at)}</dd>
        </dl>

        <div className="modal-actions">
          <button type="button" className="btn btn-ghost"
                  onClick={() => { onViewTransactions?.(account.institution); onClose(); }}>
            View {brokerLabel(account.institution)} transactions
          </button>
        </div>
      </div>
    </div>
  );
}

function titleCase(value) {
  return String(value).charAt(0).toUpperCase() + String(value).slice(1);
}

/** "Synced 2 hours ago" — the question a stale number actually raises. */
function agoLabel(iso) {
  if (!iso) return 'Never';
  const then = new Date(iso);
  if (Number.isNaN(then.getTime())) return 'Unknown';
  const minutes = Math.max(0, Math.round((Date.now() - then.getTime()) / 60000));
  if (minutes < 2) return 'Just now';
  if (minutes < 60) return `${minutes} minutes ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} hour${hours === 1 ? '' : 's'} ago`;
  const days = Math.round(hours / 24);
  return `${days} day${days === 1 ? '' : 's'} ago`;
}

function syncedLabel(account) {
  const latest = [account.holdings_synced_at, account.transactions_synced_at]
    .filter(Boolean).sort().pop();
  return latest ? `Synced ${agoLabel(latest).toLowerCase()}` : 'Not synced yet';
}

/** What to say beside an institution, given what it has actually produced. */
function connectionState(connection, accounts) {
  if (connection?.disabled) return 'Needs reconnecting';
  if (!accounts.length) return 'Connected \u00b7 nothing pulled yet';
  const since = connection?.created_at
    ? ` \u00b7 since ${String(connection.created_at).slice(0, 10)}` : '';
  return `${accounts.length} account${accounts.length === 1 ? '' : 's'}${since}`;
}

/** Mirrors backend.snaptrade._slug_broker: "E*TRADE" and "E-Trade" are one
 *  broker. Connections carry the institution's display name and accounts carry
 *  the slug, so grouping on either alone listed every broker twice — once with
 *  its Disconnect and once with its accounts, which is the pair of rows this
 *  merge existed to remove. */
function slug(name) {
  return String(name || '').toLowerCase().replace(/[^a-z0-9]+/g, '') || 'brokerage';
}
