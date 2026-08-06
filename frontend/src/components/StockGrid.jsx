import React, { useMemo, useState } from 'react';
import { MiniSparkline, filterHistoryByRange } from './Charts.jsx';
import { dayChangeFor } from './Positions.jsx';
import { sectorColor } from './Treemap.jsx';
import { money, signedPct, positionKey } from '../format.js';

/**
 * Holdings Explorer — the Stocks tab's per-stock lens. Unlike the Overview
 * (portfolio-level totals + positions table), this is a grid of individual
 * stock cards you scan and click into for price history, day range, and quote
 * details via <StockDetail>.
 */
const SORTS = {
  weight: { label: 'Weight', fn: (a, b) => b.market_value - a.market_value },
  gain: { label: 'Gain %', fn: (a, b) => b.unrealized_gain_pct - a.unrealized_gain_pct },
  az: { label: 'A–Z', fn: (a, b) => a.symbol.localeCompare(b.symbol) },
};

export function StockGrid({ positions = [], priceHistory = {}, dateRange, totalValue = 0, onSelect }) {
  const [sort, setSort] = useState('weight');
  const [query, setQuery] = useState('');

  const stocks = useMemo(
    () => positions.filter(p => p.asset_type !== 'cash' && p.market_value > 0),
    [positions],
  );

  const dayFn = useMemo(() => {
    if (sort !== 'day') return null;
    return (a, b) => (dayChangeFor(b, priceHistory)?.pct ?? -Infinity) - (dayChangeFor(a, priceHistory)?.pct ?? -Infinity);
  }, [sort, priceHistory]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    let list = stocks;
    if (q) list = list.filter(p => p.symbol.toLowerCase().includes(q) || (p.name || '').toLowerCase().includes(q));
    return [...list].sort(dayFn || SORTS[sort].fn);
  }, [stocks, query, sort, dayFn]);

  if (!stocks.length) {
    return (
      <section className="panel">
        <div className="empty-box">Add positions to explore individual stocks.</div>
      </section>
    );
  }

  return (
    <section className="panel stock-explorer">
      <header className="panel-header stock-explorer-head">
        <div>
          <h2 className="panel-title">Holdings</h2>
          <p className="panel-sub">Every stock in your book — click a card for price history, day range, and details.</p>
        </div>
        <div className="stock-explorer-controls">
          <input
            className="search-input"
            placeholder="Filter symbol or name…"
            value={query}
            onChange={event => setQuery(event.target.value)}
          />
          <label className="stock-sort">Sort
            <select value={sort} onChange={event => setSort(event.target.value)}>
              <option value="weight">Weight</option>
              <option value="day">Day change</option>
              <option value="gain">Gain %</option>
              <option value="az">A–Z</option>
            </select>
          </label>
        </div>
      </header>

      <div className="stock-grid">
        {filtered.map(position => (
          <StockCard
            key={positionKey(position)}
            position={position}
            priceHistory={priceHistory}
            dateRange={dateRange}
            totalValue={totalValue}
            onSelect={onSelect}
          />
        ))}
      </div>
    </section>
  );
}

function StockCard({ position, priceHistory, dateRange, totalValue, onSelect }) {
  const day = dayChangeFor(position, priceHistory);
  const weight = totalValue > 0 ? (position.market_value / totalValue) * 100 : 0;
  const history = filterHistoryByRange(priceHistory[position.symbol], dateRange);
  const closes = history?.closes || [];
  const gainPositive = (position.unrealized_gain ?? 0) >= 0;

  return (
    <button type="button" className="stock-card" onClick={() => onSelect?.(position)}>
      <div className="stock-card-top">
        <div className="stock-card-id">
          <span className="stock-card-sym">{position.symbol}</span>
          <span className="stock-card-name">{position.name || position.symbol}</span>
        </div>
        <span className="stock-card-sector">
          <i style={{ background: sectorColor(position.sector || 'Unknown') }} />
          {position.sector || 'Unknown'}
        </span>
      </div>

      <div className="stock-card-price-row">
        <span className="stock-card-price">{money(position.current_price)}</span>
        {day
          ? <span className={`stock-card-day ${day.pct >= 0 ? 'up' : 'down'}`}>{signedPct(day.pct)} today</span>
          : <span className="stock-card-day muted">no day data</span>}
      </div>

      <div className="stock-card-spark">
        {closes.length >= 2
          ? <MiniSparkline dates={history.dates} values={closes} baseline={closes[0]} formatValue={money} />
          : <span className="stock-card-nohist">No price history yet</span>}
      </div>

      <div className="stock-card-foot">
        <span><em>Weight</em>{weight.toFixed(1)}%</span>
        <span className={gainPositive ? 'up' : 'down'}><em>Gain</em>{signedPct(position.unrealized_gain_pct)}</span>
      </div>
    </button>
  );
}
