import React, { useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../api.js';
import { brokerLabel, money, quantityLabel } from '../format.js';

/**
 * The missing half of a transferred holding: what it cost.
 *
 * The gap panel could already say that 696.835 INTU shares arrived by transfer
 * with no cost on record, and its button dropped the reader onto a tab holding
 * 984 rows with no indication which of them it meant. Everything except the
 * price is already known — symbol, account, date, quantity — so the form
 * arrives with those filled in and the cursor in the only field left.
 *
 * One row per vest date rather than per allocation: same-day rows share a
 * price, which turns 46 INTU allocations into a handful of lines. They are
 * separate lines rather than one because a lot is worth what it was worth the
 * day it vested, and a single price across years would be a fiction.
 */
export function CostBasisForm({ gap, onClose, onSaved, addToast }) {
  const lots = useMemo(() => gap?.lots || [], [gap]);
  const [prices, setPrices] = useState({});
  const [busy, setBusy] = useState(false);
  const firstField = useRef(null);
  const closeRef = useRef(null);

  useEffect(() => {
    const onKey = event => { if (event.key === 'Escape' && !busy) onClose(); };
    document.addEventListener('keydown', onKey);
    firstField.current?.focus();
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose, busy]);

  const filled = lots.filter(lot => Number(prices[key(lot)]) > 0);
  const covered = filled.reduce(
    (sum, lot) => sum + lot.quantity * Number(prices[key(lot)]), 0);

  async function save() {
    if (!filled.length) return;
    setBusy(true);
    let written = 0;
    try {
      for (const lot of filled) {
        // A buy, because that is what the ledger is missing: the acquisition
        // these shares never had here. FIFO then matches the sale against it
        // like any other lot.
        await api('/api/v1/transactions', {
          method: 'POST',
          body: JSON.stringify({
            symbol: lot.symbol,
            broker: lot.broker,
            asset_type: 'stock',
            action: 'buy',
            quantity: lot.quantity,
            price: Number(prices[key(lot)]),
            fee: 0,
            currency: 'USD',
            occurred_at: lot.date,
            notes: 'Cost recorded for shares transferred in',
          }),
        });
        written += 1;
      }
      addToast?.('success', written === 1
        ? 'Recorded the cost of 1 lot.'
        : `Recorded the cost of ${written} lots.`);
      onSaved?.();
      onClose();
    } catch (err) {
      // Partial writes are kept, not rolled back: the ones that landed are
      // real and re-entering them would be worse than finishing the rest.
      addToast?.('error', written
        ? `Saved ${written} before failing: ${err?.message || 'could not save.'}`
        : (err?.message || 'Could not save that.'));
      if (written) { onSaved?.(); onClose(); }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="drill-backdrop" onClick={busy ? undefined : onClose} role="presentation">
      <form className="modal cost-basis-modal" role="dialog" aria-modal="true"
            aria-label="Record what these shares cost"
            onClick={event => event.stopPropagation()}
            onSubmit={event => { event.preventDefault(); save(); }}>
        <div className="drill-head">
          <div>
            <h2>Record what these shares cost</h2>
            <p className="drill-sub">
              {quantityLabel(lots.reduce((sum, lot) => sum + lot.quantity, 0))}{' '}
              {gap.symbols.join(', ')} shares arrived in{' '}
              {brokerLabel(gap.broker)} by transfer. Everything but the price is
              already known — enter what they were worth on each date, and the
              sales they cover become gains instead of bare proceeds.
            </p>
          </div>
          <button type="button" className="btn btn-ghost btn-tiny"
                  ref={closeRef} onClick={onClose} disabled={busy}>Close</button>
        </div>

        <div className="drill-scroll">
          <table className="table drill-table">
            <thead>
              <tr>
                <th>Symbol</th>
                <th>Date</th>
                <th className="num">Shares</th>
                <th className="num">Price per share</th>
                <th className="num">Cost</th>
              </tr>
            </thead>
            <tbody>
              {lots.map((lot, index) => {
                const value = prices[key(lot)] || '';
                return (
                  <tr key={key(lot)}>
                    <td><b>{lot.symbol}</b></td>
                    <td>{lot.date}</td>
                    <td className="num">{quantityLabel(lot.quantity)}</td>
                    <td className="num">
                      <input
                        ref={index === 0 ? firstField : null}
                        type="number" step="0.0001" min="0" inputMode="decimal"
                        className="cost-input" placeholder="0.00"
                        value={value} disabled={busy}
                        onChange={event => setPrices(prev => ({
                          ...prev, [key(lot)]: event.target.value,
                        }))}
                      />
                    </td>
                    <td className="num">
                      {Number(value) > 0 ? money(lot.quantity * Number(value)) : ''}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>

        {/* For a vest, this is the price on the vest date — the amount already
            taxed as income. Saying so beats leaving someone to guess, and
            guessing here is how a gain gets overstated by the whole basis. */}
        <p className="drill-foot">
          For shares from a stock plan this is the market price on the vest
          date, which your plan statement lists. Lots you leave blank stay as
          they are, so partial answers are fine.
        </p>

        <div className="modal-actions">
          <span className="cost-running">
            {filled.length
              ? `${filled.length} of ${lots.length} priced · ${money(covered)} of cost`
              : `${lots.length} lot${lots.length === 1 ? '' : 's'} to price`}
          </span>
          <button type="button" className="btn btn-ghost" onClick={onClose}
                  disabled={busy}>Cancel</button>
          <button type="submit" className="btn" disabled={busy || !filled.length}>
            {busy ? 'Saving…' : `Record ${filled.length || ''} lot${filled.length === 1 ? '' : 's'}`.trim()}
          </button>
        </div>
      </form>
    </div>
  );
}

function key(lot) {
  return `${lot.broker}|${lot.symbol}|${lot.date}`;
}
