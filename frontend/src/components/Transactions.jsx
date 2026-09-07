import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { api } from '../api.js';
import { brokerLabel, dateDay, money, quantityLabel, signedMoney } from '../format.js';
import { RealizedGains } from './RealizedGains.jsx';

const ACTIONS = [
  'buy', 'sell', 'dividend', 'interest', 'fee', 'tax',
  'deposit', 'withdrawal', 'transfer', 'fx', 'split', 'adjustment',
];
const ASSET_TYPES = ['stock', 'etf', 'crypto', 'cash', 'option'];
const PAGE_SIZE = 100;

/* Actions that move shares. Everything else is a cash event, and showing a
   quantity column full of zeros for dividends and interest reads as missing
   data rather than as "this row has no quantity by nature". */
const SHARE_ACTIONS = new Set(['buy', 'sell', 'split', 'transfer']);

const SOURCE_LABEL = {
  manual: 'Typed in',
  csv: 'CSV',
  import: 'Imported',
  snaptrade: 'Brokerage sync',
};

function actionTone(action) {
  if (action === 'buy') return 'txn-buy';
  if (action === 'sell') return 'txn-sell';
  if (['dividend', 'interest', 'deposit'].includes(action)) return 'txn-in';
  if (['fee', 'tax', 'withdrawal'].includes(action)) return 'txn-out';
  return 'txn-neutral';
}

const EMPTY_FILTERS = { symbol: '', action: '', broker: '', source: '', since: '', until: '' };

export function TransactionsView({ addToast, onChanged }) {
  const [filters, setFilters] = useState(EMPTY_FILTERS);
  const [page, setPage] = useState(0);
  const [data, setData] = useState(null);
  const [facets, setFacets] = useState({ symbols: [], brokers: [], sources: [], actions: [] });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [editing, setEditing] = useState(null);
  const [confirmDelete, setConfirmDelete] = useState(null);
  const [busy, setBusy] = useState(false);

  const query = useMemo(() => {
    const params = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(page * PAGE_SIZE) });
    Object.entries(filters).forEach(([key, value]) => { if (value) params.set(key, value); });
    return params.toString();
  }, [filters, page]);

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      setData(await api(`/api/v1/transactions?${query}`));
    } catch (err) {
      setError(err?.message || 'Could not load transactions.');
    } finally {
      setLoading(false);
    }
  }, [query]);

  useEffect(() => { load(); }, [load]);

  useEffect(() => {
    api('/api/v1/transactions/facets')
      .then(setFacets)
      .catch(() => { /* Filter menus are a convenience; the table stands without them. */ });
  }, [data?.total]);

  function setFilter(key, value) {
    setPage(0);            // A filter that keeps you on page 4 of 1 looks empty.
    setFilters(prev => ({ ...prev, [key]: value }));
  }

  async function save(row) {
    setBusy(true);
    try {
      await api(`/api/v1/transactions/${row.id}`, {
        method: 'PUT',
        body: JSON.stringify({
          symbol: row.symbol, broker: row.broker, asset_type: row.asset_type,
          action: row.action, quantity: Number(row.quantity) || 0,
          price: Number(row.price) || 0, fee: Number(row.fee) || 0,
          currency: row.currency || 'USD', occurred_at: row.occurred_at,
          notes: row.notes || '',
        }),
      });
      setEditing(null);
      addToast?.('success', 'Transaction updated.');
      await load();
      onChanged?.();
    } catch (err) {
      addToast?.('error', err?.message || 'Could not save that change.');
    } finally {
      setBusy(false);
    }
  }

  async function remove(row) {
    setBusy(true);
    try {
      await api(`/api/v1/transactions/${row.id}`, { method: 'DELETE' });
      setConfirmDelete(null);
      addToast?.('success', 'Transaction deleted.');
      await load();
      onChanged?.();
    } catch (err) {
      addToast?.('error', err?.message || 'Could not delete that row.');
    } finally {
      setBusy(false);
    }
  }

  const rows = data?.transactions || [];
  const total = data?.total || 0;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const filtered = Object.values(filters).some(Boolean);
  const from = total === 0 ? 0 : page * PAGE_SIZE + 1;
  const to = Math.min(total, (page + 1) * PAGE_SIZE);

  return (
    <div className="panel transactions">
      {/* No heading here: the page title above the panel already says
          "Transactions" and states what the ledger is for. Repeating it was the
          same duplication the Brokerages card had. The count earns the space
          instead — an import writes hundreds of rows and this is the only place
          that confirms they landed. */}
      <div className="panel-header">
        <span className="txn-count">
          <b>{total.toLocaleString()}</b> {total === 1 ? 'transaction' : 'transactions'}
          {filtered && ' matching these filters'}
        </span>
      </div>

      {/* Above the ledger, because it is what the ledger is *for*: the rows
          are evidence, this is the conclusion. */}
      <RealizedGains />

      <div className="txn-filters">
        <label>
          <span>Symbol</span>
          <select value={filters.symbol} onChange={e => setFilter('symbol', e.target.value)}>
            <option value="">All</option>
            {facets.symbols.map(s => <option key={s} value={s}>{s}</option>)}
          </select>
        </label>
        <label>
          <span>Action</span>
          <select value={filters.action} onChange={e => setFilter('action', e.target.value)}>
            <option value="">All</option>
            {facets.actions.map(a => <option key={a} value={a}>{a}</option>)}
          </select>
        </label>
        {facets.brokers.length > 1 && (
          <label>
            <span>Broker</span>
            <select value={filters.broker} onChange={e => setFilter('broker', e.target.value)}>
              <option value="">All</option>
              {facets.brokers.map(b => <option key={b} value={b}>{brokerLabel(b)}</option>)}
            </select>
          </label>
        )}
        {facets.sources.length > 1 && (
          <label>
            <span>Source</span>
            <select value={filters.source} onChange={e => setFilter('source', e.target.value)}>
              <option value="">All</option>
              {facets.sources.map(s => <option key={s} value={s}>{SOURCE_LABEL[s] || s}</option>)}
            </select>
          </label>
        )}
        <label>
          <span>From</span>
          <input type="date" value={filters.since} onChange={e => setFilter('since', e.target.value)} />
        </label>
        <label>
          <span>To</span>
          <input type="date" value={filters.until} onChange={e => setFilter('until', e.target.value)} />
        </label>
        {filtered && (
          <button type="button" className="btn btn-ghost txn-clear"
                  onClick={() => { setFilters(EMPTY_FILTERS); setPage(0); }}>
            Clear filters
          </button>
        )}
      </div>

      {error && <div className="txn-error" role="alert">{error}</div>}

      <div className="table-scroll">
        <table className="txn-table">
          <thead>
            <tr>
              <th>Date</th>
              <th>Action</th>
              <th>Symbol</th>
              <th className="num">Quantity</th>
              <th className="num">Price</th>
              <th className="num">Cash impact</th>
              <th>Source</th>
              <th aria-label="Row actions"></th>
            </tr>
          </thead>
          <tbody>
            {loading && rows.length === 0 && (
              <tr><td colSpan="8" className="empty-cell">Loading transactions…</td></tr>
            )}
            {!loading && rows.length === 0 && (
              <tr>
                <td colSpan="8" className="empty-cell">
                  {filtered
                    ? 'No transactions match these filters.'
                    : 'No transactions yet. Import a broker activity export from Smart Import, or add one by hand.'}
                </td>
              </tr>
            )}
            {rows.map(row => (
              <tr key={row.id}>
                <td className="txn-date">{dateDay(row.occurred_at)}</td>
                <td><span className={`txn-action ${actionTone(row.action)}`}>{row.action}</span></td>
                <td className="sym-cell">
                  {row.symbol
                    ? <strong>{row.symbol}</strong>
                    : <span className="txn-cash-row">cash</span>}
                  {row.asset_type === 'option' && <span className="txn-tag">option</span>}
                </td>
                <td className="num">{SHARE_ACTIONS.has(row.action) ? quantityLabel(row.quantity) : '—'}</td>
                <td className="num">{row.price ? money(row.price, row.currency) : '—'}</td>
                <td className={`num ${row.amount >= 0 ? 'pos' : 'neg'}`}>{signedMoney(row.amount)}</td>
                <td className="txn-source">{SOURCE_LABEL[row.source] || row.source}</td>
                <td className="txn-row-actions">
                  <button type="button" className="btn btn-ghost btn-tiny"
                          onClick={() => setEditing({ ...row })}>Edit</button>
                  <button type="button" className="btn btn-ghost btn-tiny danger"
                          onClick={() => setConfirmDelete(row)}>Delete</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {total > PAGE_SIZE && (
        <div className="txn-pager">
          <button type="button" className="btn btn-ghost" disabled={page === 0 || loading}
                  onClick={() => setPage(p => Math.max(0, p - 1))}>← Newer</button>
          <span>Showing {from.toLocaleString()}–{to.toLocaleString()} of {total.toLocaleString()}</span>
          <button type="button" className="btn btn-ghost" disabled={page >= pages - 1 || loading}
                  onClick={() => setPage(p => p + 1)}>Older →</button>
        </div>
      )}

      {editing && (
        <EditModal row={editing} busy={busy} onChange={setEditing}
                   onClose={() => setEditing(null)} onSave={() => save(editing)} />
      )}

      {confirmDelete && (
        <div className="modal-backdrop" onClick={() => !busy && setConfirmDelete(null)}>
          <div className="modal" onClick={e => e.stopPropagation()}>
            <h2>Delete this transaction?</h2>
            <p className="txn-confirm-line">
              <b>{confirmDelete.action}</b>{' '}
              {confirmDelete.symbol || 'cash'}{' '}
              on {dateDay(confirmDelete.occurred_at)} — {signedMoney(confirmDelete.amount)}
            </p>
            <p className="txn-confirm-note">
              Returns and coverage are computed from this ledger, so removing a row
              changes them. It cannot be undone.
            </p>
            <div className="modal-actions">
              <button type="button" className="btn" disabled={busy}
                      onClick={() => setConfirmDelete(null)}>Cancel</button>
              <button type="button" className="btn btn-danger" disabled={busy}
                      onClick={() => remove(confirmDelete)}>
                {busy ? 'Deleting…' : 'Delete'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function EditModal({ row, busy, onChange, onClose, onSave }) {
  const set = (key, value) => onChange({ ...row, [key]: value });
  const showsShares = SHARE_ACTIONS.has(row.action);
  return (
    <div className="modal-backdrop" onClick={() => !busy && onClose()}>
      <form className="modal txn-edit-modal"
            onClick={e => e.stopPropagation()}
            onSubmit={e => { e.preventDefault(); onSave(); }}>
        <h2>Edit transaction</h2>
        {/* Named explicitly: import is fallible, and this is where a broker code
            read as a fee when it was a dividend gets corrected. */}
        <p className="txn-edit-hint">
          Cash impact is recalculated from what you set here.
        </p>
        <div className="txn-edit-grid">
          <label className="field">
            <span>Date</span>
            <input type="date" required value={(row.occurred_at || '').slice(0, 10)}
                   onChange={e => set('occurred_at', e.target.value)} />
          </label>
          <label className="field">
            <span>Action</span>
            <select value={row.action} onChange={e => set('action', e.target.value)}>
              {ACTIONS.map(a => <option key={a} value={a}>{a}</option>)}
            </select>
          </label>
          <label className="field">
            <span>Symbol</span>
            <input value={row.symbol || ''} placeholder="blank for cash rows"
                   onChange={e => set('symbol', e.target.value.toUpperCase())} />
          </label>
          <label className="field">
            <span>Asset type</span>
            <select value={row.asset_type} onChange={e => set('asset_type', e.target.value)}>
              {ASSET_TYPES.map(t => <option key={t} value={t}>{t}</option>)}
            </select>
          </label>
          <label className="field">
            <span>Quantity{!showsShares && ' (not used)'}</span>
            <input type="number" step="any" min="0" value={row.quantity ?? 0}
                   disabled={!showsShares}
                   onChange={e => set('quantity', e.target.value)} />
          </label>
          <label className="field">
            <span>{showsShares ? 'Price' : 'Amount'}</span>
            <input type="number" step="any" value={row.price ?? 0}
                   onChange={e => set('price', e.target.value)} />
          </label>
          <label className="field">
            <span>Fee</span>
            <input type="number" step="any" min="0" value={row.fee ?? 0}
                   onChange={e => set('fee', e.target.value)} />
          </label>
          <label className="field">
            <span>Broker</span>
            <input value={row.broker || ''} onChange={e => set('broker', e.target.value)} />
          </label>
          <label className="field txn-edit-notes">
            <span>Notes</span>
            <input value={row.notes || ''} placeholder="optional"
                   onChange={e => set('notes', e.target.value)} />
          </label>
        </div>
        <div className="modal-actions">
          <button type="button" className="btn" disabled={busy} onClick={onClose}>Cancel</button>
          <button type="submit" className="btn btn-primary" disabled={busy}>
            {busy ? 'Saving…' : 'Save changes'}
          </button>
        </div>
      </form>
    </div>
  );
}
