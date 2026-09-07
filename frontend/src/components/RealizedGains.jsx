import React, { useCallback, useEffect, useState } from 'react';
import { api } from '../api.js';
import { money, quantityLabel, signedMoney, brokerLabel } from '../format.js';
import { RealizedDetail } from './RealizedDetail.jsx';

/**
 * Realized results for a tax year.
 *
 * The design constraint is a single number that must never appear: matched
 * gains plus unmatched proceeds. On a real ledger that sum is $259,205, of
 * which $210,172 is proceeds from shares whose purchase predates the import —
 * counted as if they cost nothing. So the two are laid out as separate things
 * with separate explanations, and the headline total contains only what the
 * ledger can actually prove.
 */
export function RealizedGains() {
  const [year, setYear] = useState(null);   // null = every year
  const [data, setData] = useState(null);
  const [error, setError] = useState('');
  const [open, setOpen] = useState(false);
  const [drill, setDrill] = useState(null);   // which figure is opened

  const load = useCallback(async () => {
    setError('');
    try {
      const query = year ? `?year=${encodeURIComponent(year)}` : '';
      setData(await api(`/api/v1/transactions/realized${query}`));
    } catch (err) {
      setError(err?.message || 'Could not compute realized results.');
    }
  }, [year]);

  useEffect(() => { load(); }, [load]);

  if (error) return <div className="txn-error" role="alert">{error}</div>;
  if (!data) return null;

  const { totals, coverage, by_symbol: rows = [], years = [] } = data;
  const nothing = !totals.realized && !totals.income && !totals.unmatched_proceeds;
  if (nothing) return null;

  return (
    <section className="realized">
      <div className="realized-head">
        <div>
          <h3>Realized results</h3>
          <p className="realized-sub">
            Closed positions and income — what the dashboard&rsquo;s gain figure,
            which counts only what you still hold, cannot show.
          </p>
        </div>
        <label className="realized-year">
          <span>Year</span>
          <select value={year || ''} onChange={event => setYear(event.target.value || null)}>
            <option value="">All time</option>
            {years.map(y => <option key={y} value={y}>{y}</option>)}
          </select>
        </label>
      </div>

      {/* Each figure opens the events behind it. A summary that cannot be
          questioned is one a reader either trusts blindly or not at all, and
          these numbers have already been doubted for good reason. */}
      <div className="realized-figures">
        <button type="button" className="realized-fig" onClick={() => setDrill('net')}>
          <span className="realized-label">Net</span>
          <b className={totals.net >= 0 ? 'pos' : 'neg'}>{signedMoney(totals.net)}</b>
          <span className="realized-note">gains + income − costs</span>
        </button>
        <button type="button" className="realized-fig" onClick={() => setDrill('gains')}>
          <span className="realized-label">Realized gains</span>
          <b className={totals.realized >= 0 ? 'pos' : 'neg'}>{signedMoney(totals.realized)}</b>
          <span className="realized-note">{totals.sales} sale{totals.sales === 1 ? '' : 's'} matched to a purchase</span>
        </button>
        <button type="button" className="realized-fig" onClick={() => setDrill('income')}>
          <span className="realized-label">Income</span>
          <b className="pos">{signedMoney(totals.income)}</b>
          <span className="realized-note">dividends and interest</span>
        </button>
        <button type="button" className="realized-fig" onClick={() => setDrill('costs')}>
          <span className="realized-label">Costs</span>
          <b className="neg">{money(totals.costs)}</b>
          <span className="realized-note">fees and withholding</span>
        </button>
      </div>

      {drill && (
        <RealizedDetail kind={drill} year={year} onClose={() => setDrill(null)} />
      )}

      {/* Never a fifth figure beside the others, and never added into Net.
          Presented as a gap in the record, which is what it is. */}
      {totals.unmatched_proceeds > 0 && (
        <div className="realized-gap">
          <b>{money(totals.unmatched_proceeds)}</b> of sales have no purchase on
          record. Serin cannot tell what they cost, so this is <em>proceeds,
          not profit</em>, and it is excluded from every figure above.
          {/* One line per account, each dated to its own ledger. A single
              global date is right for the earliest broker and wrong for every
              other one — it named E*Trade holdings against Robinhood's 2016
              start, which would send someone hunting for the wrong paperwork. */}
          <ul className="realized-gap-list">
            {(coverage.by_broker || []).map(entry => (
              <li key={entry.broker}>
                <b>{brokerLabel(entry.broker)}</b>: {money(entry.proceeds)} —{' '}
                {quantityLabel(entry.shares)} shares of {entry.symbols.join(', ')}.
                {/* Two different holes wearing one sentence until now. Shares
                    that vested in a stock plan and moved across were reported
                    as predating the account's history, which sent the reader
                    looking for statements that could not contain them. */}
                {entry.transferred_shares > 0 && (
                  <> {quantityLabel(entry.transferred_shares)} arrived by transfer
                  rather than being bought here, so their cost was never recorded.</>
                )}
                {entry.older_shares > 0 && (
                  <> {quantityLabel(entry.older_shares)} were sold from holdings
                  older than this account's history{entry.since ? `, which begins ${entry.since}` : ''}.</>
                )}
              </li>
            ))}
          </ul>
          {/* Two holes, two remedies. A transferred share is not waiting in an
              earlier statement for this account — it was never bought here. */}
          Importing the sending account's history closes the first kind;
          an earlier statement for the account itself closes the second.
        </div>
      )}

      <button type="button" className="btn btn-ghost btn-tiny realized-toggle"
              onClick={() => setOpen(!open)}>
        {open ? 'Hide' : 'Show'} by symbol ({rows.length})
      </button>

      {open && (
        <div className="table-scroll">
          <table className="txn-table realized-table">
            <thead>
              <tr>
                <th>Symbol</th>
                <th className="num">Realized</th>
                <th className="num">Income</th>
                <th className="num">Costs</th>
                <th className="num">No cost basis</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(row => (
                <tr key={row.symbol || 'cash'}>
                  <td className="sym-cell">
                    {row.symbol ? <strong>{row.symbol}</strong>
                                : <span className="txn-cash-row">cash</span>}
                  </td>
                  <td className={`num ${row.realized >= 0 ? 'pos' : 'neg'}`}>
                    {row.realized ? signedMoney(row.realized) : '—'}
                  </td>
                  <td className="num">{row.income ? signedMoney(row.income) : '—'}</td>
                  <td className="num">{row.costs ? money(row.costs) : '—'}</td>
                  <td className="num realized-gap-cell">
                    {row.unmatched_proceeds
                      ? <>{money(row.unmatched_proceeds)}
                          <span className="sub-pct">{quantityLabel(row.unmatched_shares)} sh</span></>
                      : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <p className="realized-basis">
        First-in, first-out. That is the common default, but your broker may
        use another method — check before relying on this for tax.
      </p>
    </section>
  );
}
