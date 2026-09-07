import React, { useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../api.js';
import { money, signedMoney, dateLedger, brokerLabel } from '../format.js';

/** Shares to four places, but only when they are not whole: 1500 reads as a
 *  round number and 1500.0000 reads as a machine. */
function round4(value) {
  const rounded = Math.round(value * 1e4) / 1e4;
  return Number.isInteger(rounded)
    ? rounded.toLocaleString()
    : rounded.toLocaleString(undefined, { maximumFractionDigits: 4 });
}

const TITLES = {
  net: 'Net',
  gains: 'Realized gains',
  income: 'Income',
  costs: 'Costs',
};

const BLURBS = {
  net: 'Every event behind the figure: matched gains, plus income, less costs.',
  gains: 'Sales matched to a recorded purchase, and what each one earned.',
  income: 'Dividends and interest received.',
  costs: 'Fees and withholding paid.',
};

/**
 * The same events, collapsed to one row per symbol.
 *
 * Built from the lines the events view already has rather than from a second
 * endpoint, so the two tabs cannot disagree: the aggregate sums to the same
 * total by construction. Thirty-odd TQQQ sales is a list nobody reads, and
 * the question underneath it — which holdings actually drove this figure — is
 * not answerable by scrolling.
 */
function aggregateBySymbol(lines) {
  const groups = new Map();
  for (const line of lines) {
    const symbol = line.symbol || '—';
    let group = groups.get(symbol);
    if (!group) {
      group = {
        symbol, events: 0, contribution: 0, disposals: 0,
        shares: 0, cost_basis: 0, proceeds: 0, brokers: new Set(),
      };
      groups.set(symbol, group);
    }
    group.events += 1;
    group.contribution += line.contribution || 0;
    if (line.broker) group.brokers.add(line.broker);
    // Only a disposal carries share mechanics. Summing them across dividends
    // would invent a cost basis that no row claims.
    if (line.kind === 'disposal') {
      group.disposals += 1;
      group.shares += line.matched_shares || 0;
      group.cost_basis += line.cost_basis || 0;
      group.proceeds += line.proceeds || 0;
    }
  }
  // Biggest mover first, by size rather than by sign: for costs every row is
  // negative, and ordering by value would bury the largest one at the bottom.
  return [...groups.values()].sort(
    (a, b) => Math.abs(b.contribution) - Math.abs(a.contribution));
}

/**
 * The events behind one headline figure.
 *
 * A summary card invites the question "which trades?" and, until now, left
 * nowhere to ask it. The rows come from the same FIFO walk as the card, so
 * they add up to it by construction rather than by coincidence — a drill-in
 * that disagreed with the number it opened from would read as broken data.
 */
export function RealizedDetail({ kind, year, onClose }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [view, setView] = useState('events');
  const closeRef = useRef(null);

  useEffect(() => {
    let alive = true;
    setData(null);
    setError(null);
    const suffix = year && year !== 'all' ? `?year=${encodeURIComponent(year)}` : '';
    api(`/api/v1/transactions/realized/${kind}${suffix}`)
      .then(payload => { if (alive) setData(payload); })
      .catch(() => { if (alive) setError('Could not load the detail.'); });
    return () => { alive = false; };
  }, [kind, year]);

  // Escape closes, and focus lands on the close button: a panel that traps a
  // keyboard user is worse than no panel.
  useEffect(() => {
    const onKey = event => { if (event.key === 'Escape') onClose(); };
    document.addEventListener('keydown', onKey);
    closeRef.current?.focus();
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose]);

  const lines = data?.lines || [];
  const grouped = useMemo(() => aggregateBySymbol(lines), [lines]);

  return (
    <div className="drill-backdrop" onClick={onClose} role="presentation">
      <div className="drill" role="dialog" aria-modal="true"
           aria-label={`${TITLES[kind] || kind} detail`}
           onClick={event => event.stopPropagation()}>
        <div className="drill-head">
          <div>
            <h3>{TITLES[kind] || kind}</h3>
            <p className="drill-sub">{BLURBS[kind]}</p>
          </div>
          <button type="button" className="btn btn-ghost btn-tiny"
                  ref={closeRef} onClick={onClose}>Close</button>
        </div>

        {error && <p className="drill-empty">{error}</p>}
        {!data && !error && <p className="drill-empty">Loading…</p>}

        {data && !lines.length && (
          <p className="drill-empty">
            Nothing contributed to this figure{year && year !== 'all' ? ` in ${year}` : ''}.
          </p>
        )}

        {data && lines.length > 0 && (
          <>
            <div className="drill-total">
              <span>
                {data.count} {data.count === 1 ? 'event' : 'events'}
                {grouped.length > 1 && ` · ${grouped.length} symbols`}
              </span>
              <b className={data.total >= 0 ? 'pos' : 'neg'}>{signedMoney(data.total)}</b>
            </div>

            {/* Offered only when collapsing would actually change anything. A
                tab strip above four rows, one per symbol, is furniture. */}
            {grouped.length < lines.length && (
              <div className="segmented drill-tabs">
                <button type="button" className={view === 'events' ? 'active' : ''}
                        onClick={() => setView('events')}>Events</button>
                <button type="button" className={view === 'symbol' ? 'active' : ''}
                        onClick={() => setView('symbol')}>By symbol</button>
              </div>
            )}

            {view === 'symbol' && grouped.length < lines.length ? (
            <div className="drill-scroll">
              <table className="table drill-table">
                <thead>
                  <tr>
                    <th>Symbol</th>
                    <th>Account</th>
                    <th className="num">Events</th>
                    <th className="num">Shares</th>
                    <th className="num">Cost basis</th>
                    <th className="num">Proceeds</th>
                    <th className="num">Contribution</th>
                  </tr>
                </thead>
                <tbody>
                  {grouped.map(group => (
                    <tr key={group.symbol}>
                      <td><b>{group.symbol}</b></td>
                      {/* Lots are matched inside the account that holds them,
                          so a symbol spanning two accounts is two different
                          cost bases. Naming the count beats picking one. */}
                      <td className="drill-what">
                        {group.brokers.size === 1
                          ? brokerLabel([...group.brokers][0])
                          : group.brokers.size > 1
                            ? `${group.brokers.size} accounts`
                            : '—'}
                      </td>
                      <td className="num">{group.events}</td>
                      <td className="num">
                        {group.disposals ? round4(group.shares) : ''}
                      </td>
                      <td className="num">
                        {group.disposals ? money(group.cost_basis) : ''}
                      </td>
                      <td className="num">
                        {group.disposals ? money(group.proceeds) : ''}
                      </td>
                      <td className={`num ${group.contribution >= 0 ? 'pos' : 'neg'}`}>
                        {signedMoney(group.contribution)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            ) : (
            <div className="drill-scroll">
              <table className="table drill-table">
                <thead>
                  <tr>
                    <th>Date</th>
                    <th>Symbol</th>
                    <th>What</th>
                    <th className="num">Shares</th>
                    <th className="num">Cost basis</th>
                    <th className="num">Proceeds</th>
                    <th className="num">Contribution</th>
                  </tr>
                </thead>
                <tbody>
                  {lines.map(line => (
                    <tr key={`${line.kind}-${line.id}`}>
                      <td>{dateLedger(line.date)}</td>
                      <td>{line.symbol || '—'}</td>
                      <td className="drill-what">
                        {line.kind === 'disposal' ? 'sold' : line.action}
                        {line.broker && (
                          <span className="drill-broker">{brokerLabel(line.broker)}</span>
                        )}
                      </td>
                      {/* Only a disposal has share mechanics. A dividend has an
                          amount and nothing else, and inventing columns for it
                          would imply a cost basis it does not have. */}
                      <td className="num">
                        {line.kind === 'disposal' ? line.matched_shares : ''}
                      </td>
                      <td className="num">
                        {line.kind === 'disposal' ? money(line.cost_basis) : ''}
                      </td>
                      <td className="num">
                        {line.kind === 'disposal' ? money(line.proceeds) : ''}
                      </td>
                      <td className={`num ${line.contribution >= 0 ? 'pos' : 'neg'}`}>
                        {signedMoney(line.contribution)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            )}
            {/* Sales whose purchase predates the ledger are absent by design:
                they contributed nothing to this figure, and listing them here
                would imply their proceeds were profit. */}
            {kind === 'gains' && (
              <p className="drill-foot">
                Sales with no purchase on record are not listed — they have
                proceeds, not gains, and are excluded from this figure.
              </p>
            )}
          </>
        )}
      </div>
    </div>
  );
}
