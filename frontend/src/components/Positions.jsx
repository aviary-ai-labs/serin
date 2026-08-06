import React, { useEffect, useMemo, useState } from 'react';
import { MiniSparkline, filterHistoryByRange } from './Charts.jsx';
import { IconEdit, IconX, IconRefresh } from './Icons.jsx';
import {
  money,
  signedMoney,
  signedPct,
  quantityLabel,
  brokerLabel,
  lotKey,
  groupTaxLots,
} from '../format.js';

function SortTh({ label, col, sortCol, sortDir, onSort, align = 'right' }) {
  const active = sortCol === col;
  return (
    <th className={`sortable ${active ? 'active' : ''}`} onClick={() => onSort(col)} style={{ textAlign: align }}>
      {label}<span>{active ? (sortDir === 'asc' ? '▲' : '▼') : '▲'}</span>
    </th>
  );
}

export function dayChangeFor(position, priceHistory) {
  if (['cash', 'option'].includes(position.asset_type)) return null;
  const closes = priceHistory[position.symbol]?.closes;
  if (!closes || closes.length < 2) return null;
  const last = closes[closes.length - 1];
  const prev = closes[closes.length - 2];
  if (!prev) return null;
  return {
    value: (last - prev) * position.quantity,
    pct: ((last - prev) / prev) * 100,
  };
}

export function PositionsTable({
  positions,
  priceHistory,
  dateRange,
  taxLots,
  selectedId,
  onSelect,
  onEdit,
  onDelete,
  onOpenTaxLots,
}) {
  const [sortCol, setSortCol] = useState('market_value');
  const [sortDir, setSortDir] = useState('desc');
  const [query, setQuery] = useState('');

  const onSort = col => {
    if (col === sortCol) setSortDir(prev => (prev === 'asc' ? 'desc' : 'asc'));
    else {
      setSortCol(col);
      setSortDir('desc');
    }
  };

  const filtered = useMemo(() => {
    const q = query.trim().toUpperCase();
    if (!q) return positions;
    return positions.filter(position => (
      position.symbol.includes(q)
      || (position.name || '').toUpperCase().includes(q)
      || brokerLabel(position.broker).toUpperCase().includes(q)
      || (position.sector || '').toUpperCase().includes(q)
    ));
  }, [positions, query]);

  const sorted = useMemo(() => {
    const valueFor = position => {
      if (sortCol === 'symbol') return position.symbol;
      if (sortCol === 'broker') return position.broker;
      if (sortCol === 'day_change') return dayChangeFor(position, priceHistory)?.value ?? -Infinity;
      return Number(position[sortCol] || 0);
    };
    return [...filtered].sort((a, b) => {
      if (a.asset_type === 'cash' && b.asset_type !== 'cash') return 1;
      if (b.asset_type === 'cash' && a.asset_type !== 'cash') return -1;
      const av = valueFor(a);
      const bv = valueFor(b);
      const cmp = typeof av === 'string' ? av.localeCompare(bv) : av - bv;
      return sortDir === 'asc' ? cmp : -cmp;
    });
  }, [filtered, sortCol, sortDir, priceHistory]);

  const lotsByPosition = useMemo(() => groupTaxLots(taxLots), [taxLots]);

  return (
    <>
      <div className="panel-header">
        <h2>Positions</h2>
        <div className="table-toolbar">
          <input
            className="search-input"
            type="search"
            placeholder="Filter symbol, broker…"
            value={query}
            onChange={event => setQuery(event.target.value)}
            aria-label="Filter positions"
          />
        </div>
      </div>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <SortTh label="Symbol" col="symbol" sortCol={sortCol} sortDir={sortDir} onSort={onSort} align="left" />
              <SortTh label="Broker" col="broker" sortCol={sortCol} sortDir={sortDir} onSort={onSort} align="left" />
              <SortTh label="Qty" col="quantity" sortCol={sortCol} sortDir={sortDir} onSort={onSort} />
              <SortTh label="Avg Cost" col="average_cost" sortCol={sortCol} sortDir={sortDir} onSort={onSort} />
              <SortTh label="Price" col="current_price" sortCol={sortCol} sortDir={sortDir} onSort={onSort} />
              <SortTh label="Day" col="day_change" sortCol={sortCol} sortDir={sortDir} onSort={onSort} />
              <SortTh label="Value" col="market_value" sortCol={sortCol} sortDir={sortDir} onSort={onSort} />
              <SortTh label="Gain" col="unrealized_gain" sortCol={sortCol} sortDir={sortDir} onSort={onSort} />
              <th style={{ textAlign: 'center' }}>Trend</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {sorted.length === 0 && (
              <tr><td colSpan="10" className="empty-cell">{query ? `No positions match “${query}”.` : 'No positions yet.'}</td></tr>
            )}
            {sorted.map(position => {
              const isCash = position.asset_type === 'cash';
              const history = filterHistoryByRange(priceHistory[position.symbol], dateRange);
              const pnlValues = history.closes.map(close => (close - position.average_cost) * position.quantity);
              const day = dayChangeFor(position, priceHistory);
              const lots = lotsByPosition[lotKey(position.symbol, position.broker)] || [];
              return (
                <tr
                  key={position.id}
                  className={`clickable ${selectedId === position.id ? 'selected-row' : ''} ${isCash ? 'cash-row' : ''}`}
                  onClick={() => onSelect(position.id)}
                >
                  <td className="sym-cell">
                    <strong>
                      {position.symbol}
                      {position.source === 'snaptrade' && <span className="sync-dot" title="Synced from your brokerage"><IconRefresh size={10} /></span>}
                      {lots.length > 0 && (
                        <button className="tax-lot-trigger" style={{ marginLeft: 7 }} onClick={event => { event.stopPropagation(); onOpenTaxLots(position); }}>
                          <TaxLotBadges lots={lots} />
                        </button>
                      )}
                    </strong>
                    <small>{isCash ? 'Cash balance' : (position.name !== position.symbol && position.name) || position.sector || position.asset_type}</small>
                  </td>
                  <td><span className={`broker-tag broker-${position.broker}`}>{brokerLabel(position.broker)}</span></td>
                  <td className="num">{isCash ? '—' : quantityLabel(position.quantity)}</td>
                  {/* average cost + price are native-currency values */}
                  <td className="num">{isCash ? '—' : money(position.average_cost, position.currency)}</td>
                  <td className="num">{isCash ? '—' : money(position.current_price, position.currency)}</td>
                  <td className={`num ${day ? (day.value >= 0 ? 'positive' : 'negative') : ''}`}>
                    {day ? <>{signedMoney(day.value)}<span className="sub-pct">{signedPct(day.pct)}</span></> : <span className="muted-cell">—</span>}
                  </td>
                  <td className="num">{money(position.market_value)}</td>
                  <td className={`num ${position.unrealized_gain >= 0 ? 'positive' : 'negative'}`}>
                    {isCash ? <span className="muted-cell">—</span> : <>{signedMoney(position.unrealized_gain)}<span className="sub-pct">{signedPct(position.unrealized_gain_pct)}</span></>}
                  </td>
                  <td style={{ textAlign: 'center' }}>
                    {isCash || position.asset_type === 'option'
                      ? <span className="muted-cell">—</span>
                      : <MiniSparkline dates={history.dates} values={pnlValues} baseline={0} formatValue={signedMoney} />}
                  </td>
                  <td>
                    <span className="row-actions">
                      <button className="icon-btn" title="Edit" aria-label={`Edit ${position.symbol}`} onClick={event => { event.stopPropagation(); onEdit(position); }}><IconEdit size={13} /></button>
                      <button className="icon-btn danger" title="Delete" aria-label={`Delete ${position.symbol}`} onClick={event => { event.stopPropagation(); onDelete(position); }}><IconX size={13} /></button>
                    </span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </>
  );
}

function TaxLotBadges({ lots }) {
  if (!lots?.length) return <span className="tax-empty">+ add lot</span>;
  const longTerm = lots.filter(lot => lot.holding_period === 'long-term').length;
  const shortTerm = lots.length - longTerm;
  return (
    <span className="tax-badges">
      {longTerm > 0 && <span className="tax-badge long">{longTerm} long</span>}
      {shortTerm > 0 && <span className="tax-badge short">{shortTerm} short</span>}
    </span>
  );
}

const EMPTY_FORM = {
  symbol: '',
  name: '',
  broker: 'manual',
  asset_type: 'stock',
  quantity: '',
  average_cost: '',
  current_price: '',
  currency: 'USD',
};

export const COMMON_CURRENCIES = ['USD', 'EUR', 'GBP', 'JPY', 'CAD', 'AUD', 'CHF', 'CNY', 'HKD', 'SGD'];

export function PositionModal({ editing, busy, onClose, onSubmit }) {
  const [form, setForm] = useState(() => (
    editing
      ? {
          symbol: editing.symbol,
          name: editing.name === editing.symbol ? '' : editing.name,
          broker: editing.broker,
          asset_type: editing.asset_type,
          quantity: String(editing.quantity),
          average_cost: String(editing.average_cost),
          current_price: String(editing.current_price),
          currency: editing.currency || 'USD',
        }
      : EMPTY_FORM
  ));

  useEffect(() => {
    const onKey = event => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const setField = (key, value) => setForm(prev => ({ ...prev, [key]: value }));
  const isCash = form.asset_type === 'cash';

  function handleSubmit(event) {
    event.preventDefault();
    const symbol = form.symbol.trim().toUpperCase();
    const sector = editing && symbol === editing.symbol && form.asset_type === editing.asset_type
      ? editing.sector || ''
      : '';
    onSubmit({
      ...form,
      symbol,
      quantity: Number(form.quantity || 0),
      average_cost: Number(isCash ? 1 : form.average_cost || 0),
      current_price: Number(isCash ? 1 : form.current_price || 0),
      sector,
    });
  }

  return (
    <div className="modal-overlay" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}>
      <div className="modal" role="dialog" aria-modal="true" aria-label={editing ? 'Edit position' : 'Add position'}>
        <div className="modal-head">
          <h2>{editing ? `Edit ${editing.symbol}` : 'Add Position'}</h2>
          <button className="icon-btn" onClick={onClose} aria-label="Close"><IconX size={14} /></button>
        </div>
        <form onSubmit={handleSubmit}>
          <div className="modal-body">
            <div className="form-grid">
              <label className="form-label">Symbol
                <input value={form.symbol} onChange={e => setField('symbol', e.target.value)} placeholder="AAPL" required autoFocus={!editing} />
              </label>
              <label className="form-label">Type
                <select value={form.asset_type} onChange={e => setField('asset_type', e.target.value)}>
                  <option value="stock">Stock</option>
                  <option value="etf">ETF</option>
                  <option value="option">Option</option>
                  <option value="crypto">Crypto</option>
                  <option value="cash">Cash</option>
                </select>
              </label>
              <label className="form-label">Name
                <input value={form.name} onChange={e => setField('name', e.target.value)} placeholder="Optional display name" />
              </label>
              <label className="form-label">Broker
                <input value={form.broker} onChange={e => setField('broker', e.target.value)} placeholder="robinhood, etrade…" required />
              </label>
              <label className="form-label">{isCash ? 'Balance ($)' : 'Quantity'}
                <input type="number" min="0" step="any" value={form.quantity} onChange={e => setField('quantity', e.target.value)} placeholder="0" required />
              </label>
              {!isCash && (
                <label className="form-label">Average cost
                  <input type="number" min="0" step="any" value={form.average_cost} onChange={e => setField('average_cost', e.target.value)} placeholder="0.00" required />
                </label>
              )}
              {!isCash && (
                <label className="form-label">Current price
                  <input type="number" min="0" step="any" value={form.current_price} onChange={e => setField('current_price', e.target.value)} placeholder="0.00" required />
                  <span className="form-hint">Refresh prices later to pull the live quote.</span>
                </label>
              )}
              <label className="form-label">Currency
                <select value={form.currency} onChange={e => setField('currency', e.target.value)}>
                  {COMMON_CURRENCIES.map(code => <option key={code} value={code}>{code}</option>)}
                </select>
                <span className="form-hint">Currency of the prices above; totals convert to your display currency.</span>
              </label>
              {form.asset_type === 'option' && (
                <div className="full form-hint">
                  Options use a 100× contract multiplier. Quantity is the number of contracts; prices are per share.
                </div>
              )}
            </div>
          </div>
          <div className="modal-actions">
            <button type="button" className="btn" onClick={onClose}>Cancel</button>
            <button type="submit" className="btn btn-primary" disabled={busy}>
              {busy ? 'Saving…' : editing ? 'Save changes' : 'Add position'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

export function ConfirmDialog({ title, body, confirmLabel, busy, onConfirm, onCancel }) {
  useEffect(() => {
    const onKey = event => {
      if (event.key === 'Escape') onCancel();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onCancel]);

  return (
    <div className="modal-overlay" onMouseDown={event => { if (event.target === event.currentTarget) onCancel(); }}>
      <div className="modal" style={{ width: 'min(440px, 100%)' }} role="alertdialog" aria-modal="true" aria-label={title}>
        <div className="modal-head">
          <h2>{title}</h2>
          <button className="icon-btn" onClick={onCancel} aria-label="Close"><IconX size={14} /></button>
        </div>
        <div className="modal-body">
          <p className="modal-text">{body}</p>
        </div>
        <div className="modal-actions">
          <button className="btn" onClick={onCancel}>Cancel</button>
          <button className="btn btn-danger" onClick={onConfirm} disabled={busy}>
            {busy ? 'Working…' : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

export function TaxLotsDrawer({ position, lots, busy, onClose, onCreate, onDelete }) {
  const today = new Date().toISOString().slice(0, 10);
  const [form, setForm] = useState({ quantity: '', cost_basis: '', acquired_at: today });

  useEffect(() => {
    setForm({
      quantity: '',
      cost_basis: position ? String(position.average_cost || '') : '',
      acquired_at: today,
    });
    // Reset the entry form whenever the drawer switches position.
  }, [position?.id]);

  useEffect(() => {
    const onKey = event => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  if (!position) return null;

  const shortGain = lots.filter(lot => lot.holding_period === 'short-term').reduce((sum, lot) => sum + lot.unrealized_gain, 0);
  const longGain = lots.filter(lot => lot.holding_period === 'long-term').reduce((sum, lot) => sum + lot.unrealized_gain, 0);
  const lossCandidates = lots.filter(lot => lot.unrealized_gain < 0).length;

  function submit(event) {
    event.preventDefault();
    onCreate({
      symbol: position.symbol,
      broker: position.broker,
      quantity: Number(form.quantity || 0),
      cost_basis: Number(form.cost_basis || 0),
      acquired_at: form.acquired_at,
    });
    setForm(prev => ({ ...prev, quantity: '' }));
  }

  return (
    <>
      <div className="drawer-overlay" onClick={onClose} />
      <aside className="drawer" aria-label="Tax lots">
        <div className="drawer-head">
          <div>
            <h2>Tax Lots</h2>
            <strong>{position.symbol}</strong>
            <span className={`broker-tag broker-${position.broker}`}>{brokerLabel(position.broker)}</span>
          </div>
          <button className="icon-btn" onClick={onClose} aria-label="Close tax lots"><IconX size={14} /></button>
        </div>
        <div className="tax-summary">
          <div>
            <span>Short-term</span>
            <strong className={shortGain >= 0 ? 'positive' : 'negative'}>{signedMoney(shortGain)}</strong>
          </div>
          <div>
            <span>Long-term</span>
            <strong className={longGain >= 0 ? 'positive' : 'negative'}>{signedMoney(longGain)}</strong>
          </div>
          <div>
            <span>Loss lots</span>
            <strong>{lossCandidates}</strong>
          </div>
        </div>
        <div className="tax-lot-table">
          <table>
            <thead>
              <tr>
                <th style={{ textAlign: 'left' }}>Acquired</th>
                <th>Shares</th>
                <th>Cost</th>
                <th>Unrealized</th>
                <th>Term</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {lots.length === 0 && <tr><td colSpan="6" className="empty-cell">No tax lots yet — add purchase lots below to track long/short-term holding periods.</td></tr>}
              {lots.map(lot => (
                <tr key={lot.id}>
                  <td className="num" style={{ textAlign: 'left' }}>{lot.acquired_at}</td>
                  <td className="num">{quantityLabel(lot.quantity)}</td>
                  <td className="num">{money(lot.cost_basis)}</td>
                  <td className={`num ${lot.unrealized_gain >= 0 ? 'positive' : 'negative'}`}>{signedMoney(lot.unrealized_gain)}</td>
                  <td>
                    <span className={`tax-badge ${lot.holding_period === 'long-term' ? 'long' : 'short'}`}>
                      {lot.holding_period === 'long-term' ? 'long' : `${lot.days_to_long_term}d to long`}
                    </span>
                  </td>
                  <td>
                    <button className="link-btn danger" disabled={busy === `tax-delete-${lot.id}`} onClick={() => onDelete(lot.id)}>
                      Delete
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <form className="tax-form" onSubmit={submit}>
          <h3>Add Lot</h3>
          <label className="form-label">Shares
            <input type="number" min="0" step="any" value={form.quantity} onChange={event => setForm(prev => ({ ...prev, quantity: event.target.value }))} required />
          </label>
          <label className="form-label">Price paid
            <input type="number" min="0" step="any" value={form.cost_basis} onChange={event => setForm(prev => ({ ...prev, cost_basis: event.target.value }))} required />
          </label>
          <label className="form-label">Purchase date
            <input type="date" value={form.acquired_at} onChange={event => setForm(prev => ({ ...prev, acquired_at: event.target.value }))} required />
          </label>
          <button className="btn btn-primary" disabled={busy === 'tax-lot'}>{busy === 'tax-lot' ? 'Adding…' : 'Add lot'}</button>
        </form>
      </aside>
    </>
  );
}
