import React, { useEffect, useMemo, useRef, useState } from 'react';
import { money, signedMoney, signedPct, brokerLabel, allocColor } from '../format.js';

/** The window start for a range, or null for "everything". */
export function rangeCutoff(range) {
  const now = new Date();
  if (range === '1W') return new Date(now.getTime() - 7 * 86400000);
  if (range === '1M') return new Date(now.getTime() - 30 * 86400000);
  if (range === '3M') return new Date(now.getTime() - 90 * 86400000);
  if (range === 'YTD') return new Date(now.getFullYear(), 0, 1);
  return null;
}

/** Index of the last entry at or before `cutoffStr`, else 0. Same boundary as
 *  backend.analytics._find_at_or_before: a period starts at the previous
 *  close, not at the first one inside it. */
export function anchorIndex(dates, cutoffStr) {
  let anchor = -1;
  for (let i = 0; i < dates.length && dates[i] <= cutoffStr; i += 1) anchor = i;
  return anchor >= 0 ? anchor : 0;
}

export function filterHistoryByRange(history, range) {
  const dates = history?.dates || [];
  const closes = history?.closes || [];
  if (!dates.length || dates.length !== closes.length) return { dates: [], closes: [] };
  const now = new Date();
  let cutoff = null;
  if (range === '1W') cutoff = new Date(now.getTime() - 7 * 86400000);
  if (range === '1M') cutoff = new Date(now.getTime() - 30 * 86400000);
  if (range === '3M') cutoff = new Date(now.getTime() - 90 * 86400000);
  if (range === 'YTD') cutoff = new Date(now.getFullYear(), 0, 1);
  if (!cutoff) return { dates, closes };
  const cutoffStr = cutoff.toISOString().slice(0, 10);
  // Anchor the window at the last close AT OR BEFORE the cutoff — "YTD"
  // means since Dec 31's close, not since whatever the first trading day of
  // January happened to be. dates is ascending (backend query: ORDER BY
  // date), so the anchor is the last index that hasn't yet crossed cutoff.
  // The backend's period cards use the same "at or before" boundary
  // (_find_at_or_before); using ">=" here alone started this chart's window
  // one trading day later than the cards and understated every return.
  let anchorIdx = -1;
  for (let i = 0; i < dates.length && dates[i] <= cutoffStr; i += 1) anchorIdx = i;
  const startIdx = anchorIdx >= 0 ? anchorIdx : 0;
  const filtered = { dates: dates.slice(startIdx), closes: closes.slice(startIdx) };
  return filtered.dates.length >= 2 ? filtered : { dates, closes };
}

export function DateRangeControl({ value, onChange }) {
  return (
    <div className="segmented">
      {['1W', '1M', '3M', 'YTD', 'ALL'].map(option => (
        <button key={option} className={value === option ? 'active' : ''} onClick={() => onChange(option)}>
          {option}
        </button>
      ))}
    </div>
  );
}

export function MiniSparkline({ dates = [], values = [], baseline = null, formatValue = money }) {
  const [hover, setHover] = useState(null);
  const ref = useRef(null);
  if (!values || values.length < 2) return <span className="muted-cell">No history</span>;

  const width = 110;
  const height = 36;
  const pad = 3;
  const min = Math.min(...values, baseline ?? values[0]);
  const max = Math.max(...values, baseline ?? values[0]);
  const range = max - min || 1;
  const toX = idx => (idx / (values.length - 1)) * width;
  const toY = value => height - pad - ((value - min) / range) * (height - pad * 2);
  const points = values.map((value, idx) => `${toX(idx).toFixed(1)},${toY(value).toFixed(1)}`).join(' ');
  const final = values[values.length - 1];
  const reference = baseline ?? values[0];
  const color = final >= reference ? 'var(--accent-green)' : 'var(--accent-red)';

  function onMove(event) {
    const rect = ref.current?.getBoundingClientRect();
    if (!rect) return;
    const idx = Math.max(0, Math.min(values.length - 1, Math.round(((event.clientX - rect.left) / rect.width) * (values.length - 1))));
    setHover({ idx, x: event.clientX, y: event.clientY });
  }

  return (
    <span ref={ref} className="sparkline" onMouseMove={onMove} onMouseLeave={() => setHover(null)}>
      <svg viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none">
        {baseline != null && min < baseline && max > baseline && (
          <line x1="0" x2={width} y1={toY(baseline)} y2={toY(baseline)} stroke="rgba(123,140,132,0.4)" strokeDasharray="3 3" />
        )}
        <polyline points={points} fill="none" stroke={color} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
        {hover && <circle cx={toX(hover.idx)} cy={toY(values[hover.idx])} r="3" fill={color} />}
      </svg>
      {hover && (
        <span className="sparkline-tip" style={{ left: hover.x + 12, top: hover.y - 48 }}>
          <em>{dates[hover.idx] || ''}</em>
          <strong>{formatValue(values[hover.idx])}</strong>
        </span>
      )}
    </span>
  );
}

export function PortfolioTrendChart({ positions, priceHistory, dateRange, onRangeChange, ledger }) {
  const [hover, setHover] = useState(null);
  const [brokerFilter, setBrokerFilter] = useState('all');
  const svgRef = useRef(null);

  const brokerOptions = useMemo(() => (
    [...new Set(positions
      .filter(position => !['cash', 'option'].includes(position.asset_type))
      .map(position => position.broker)
      .filter(Boolean))]
      .sort((a, b) => brokerLabel(a).localeCompare(brokerLabel(b)))
  ), [positions]);

  const data = useMemo(() => {
    // The ledger reconstructs what was actually held on each day. The
    // per-symbol path below cannot: it takes *today's* quantities and prices
    // them backwards, which answers "what if I had held exactly this all
    // along" — a counterfactual, and a badly misleading one for anyone who
    // traded. Prefer the real thing wherever it exists.
    //
    // Selecting one account narrows the same reconstruction rather than
    // abandoning it. This used to fall back, on the belief that daily_values
    // could not be filtered — it takes positions and transactions as
    // arguments, so it always could. The fallback showed a Robinhood account
    // that had $145,000 withdrawn from it as +6.29%, computed from a basket it
    // never held, beside a whole-portfolio figure it could not be reconciled
    // with.
    const scoped = brokerFilter === 'all' ? null : ledger?.by_broker?.[brokerFilter];
    const ledgerSeries = (brokerFilter === 'all' ? ledger?.series : scoped?.series) || [];
    const ledgerUsable = ledgerSeries.length >= 3
      && (brokerFilter === 'all' ? Boolean(ledger?.coverage?.quality
          && ['complete', 'partial', 'missing_cash_activity'].includes(ledger.coverage.quality))
        : Boolean(scoped));
    if (ledgerUsable) {
      const cutoff = rangeCutoff(dateRange);
      const dates = ledgerSeries.map(point => point.date);
      let from = cutoff ? anchorIndex(dates, cutoff.toISOString().slice(0, 10)) : 0;

      // Where the ledger contradicts the holdings, the reconstruction before
      // that point is not a rougher estimate of this portfolio — it is a
      // different one. Start after it rather than plot it.
      const reliable = scoped ? scoped.reliable_from : ledger?.coverage?.reliable_from;
      if (reliable) from = Math.max(from, anchorIndex(dates, reliable));

      const window = ledgerSeries.slice(from);
      // Portfolio value, cash included — not the securities sleeve alone.
      // Buying does not make you richer and selling does not make you poorer;
      // both move value between cash and stock, so plotting only the stock
      // half draws a transfer as a gain. Moving $226k of cash into shares
      // over one year read as "+$186,332.02 (+49.55%)" while the portfolio
      // itself was down $40,130. Same boundary portfolio_history reconstructs
      // against, so the line answers the question its own numbers answer.
      const start = window.length ? window[0].total : 0;

      // A start of zero used to yield changePct: 0 while `change` kept the
      // full end value — which is how a $585k portfolio came to report
      // "+$1,059,137.85 (+0.00%)". There is no honest percentage to show
      // against a zero base, so this declines the ledger basis instead of
      // inventing one, and the price-history path below answers instead.
      if (window.length >= 3 && start > 0) {
        return {
          basis: 'ledger',
          truncatedTo: reliable && from > 0 ? window[0].date : null,
          // Time-weighted return for this range, computed server-side. The
          // headline measure: it breaks the series at every deposit and
          // withdrawal, so the owner's own transfers cannot read as
          // performance. A value change cannot do that, and called a year
          // that earned $22,645 a 12.62% loss because $145,000 was withdrawn.
          ranged: (scoped ? scoped.returns : ledger?.returns)?.[dateRange.toLowerCase()] || null,
          // Scoped to the selected account, so the cost and count under the
          // line describe the same thing the line does.
          currentCost: positions
            .filter(p => brokerFilter === 'all' || p.broker === brokerFilter)
            .reduce((sum, p) => sum + (p.total_cost || 0), 0),
          trackedCount: positions.filter(p => !['cash', 'option'].includes(p.asset_type)
            && (brokerFilter === 'all' || p.broker === brokerFilter)).length,
          lateSymbols: [],
          series: window.map(point => ({
            date: point.date,
            value: point.total,
            change: point.total - start,
            changePct: ((point.total - start) / start) * 100,
          })),
        };
      }
    }

    const tracked = positions.filter(position => (
      !['cash', 'option'].includes(position.asset_type)
      && priceHistory[position.symbol]?.dates?.length
      && (brokerFilter === 'all' || position.broker === brokerFilter)
    ));
    if (!tracked.length) return null;
    const bySymbol = {};
    const plottable = [];
    tracked.forEach(position => {
      const filtered = filterHistoryByRange(priceHistory[position.symbol], dateRange);
      if (!filtered.dates.length) return; // nothing in this window — can't plot it
      plottable.push(position);
      bySymbol[position.symbol] = Object.fromEntries(filtered.dates.map((date, idx) => [date, filtered.closes[idx]]));
    });
    if (!plottable.length) return null;
    const symbols = new Set(plottable.map(position => position.symbol));
    const dates = [...new Set(Object.values(bySymbol).flatMap(item => Object.keys(item)))].sort();
    // A symbol missing a date means "no close reported yet", never "the
    // position vanished" — free providers finalize some symbols a day late,
    // so tails are routinely ragged. Carry each symbol's last close forward;
    // summing without it used to draw a cliff the size of the whole holding.
    // The series starts once every symbol has reported at least one close,
    // for the same reason in mirror: a partial early sum is a fake ramp-up.
    // Waiting for *every* symbol before plotting anything means the newest
    // listing decides how far back the chart goes. One holding worth $141 of
    // $872,355 — a recent IPO with 53 days of history — cut eight months off
    // the portfolio trend, which is a far bigger lie than the ramp-up the rule
    // was written to prevent.
    //
    // So wait for enough *value* rather than every symbol. Below the
    // threshold the sum really would be a fake ramp; above it, what is
    // missing is bounded by construction, and the alternative is showing no
    // history at all.
    const COVERAGE = 0.95;
    const valueBySymbol = {};
    plottable.forEach(position => {
      valueBySymbol[position.symbol] =
        (valueBySymbol[position.symbol] || 0) + Math.abs(position.market_value || 0);
    });
    const totalValue = Object.values(valueBySymbol).reduce((sum, v) => sum + v, 0);

    const seen = {};
    const series = [];
    dates.forEach(date => {
      symbols.forEach(symbol => {
        const close = bySymbol[symbol]?.[date];
        if (close != null) seen[symbol] = close;
      });
      const covered = Object.keys(seen).reduce((sum, s) => sum + (valueBySymbol[s] || 0), 0);
      // No total to weigh against (every holding priced at zero) — fall back
      // to the original all-symbols rule rather than dividing by zero.
      if (totalValue > 0 ? covered / totalValue < COVERAGE
                         : Object.keys(seen).length < symbols.size) return;
      let value = 0;
      plottable.forEach(position => {
        const close = seen[position.symbol];
        if (close != null) value += close * position.quantity;
      });
      series.push({ date, value });
    });
    if (series.length < 3) return null;
    // Which holdings could not be shown for the whole span, so the header can
    // say so instead of the reader wondering why the line starts in June.
    const lateSymbols = [...symbols].filter(
      symbol => (bySymbol[symbol] && Object.keys(bySymbol[symbol])[0] > series[0].date)
    );
    const start = series[0].value;
    const currentCost = plottable.reduce((sum, position) => sum + position.total_cost, 0);
    return {
      basis: 'holdings',
      currentCost,
      trackedCount: plottable.length,
      lateSymbols,
      series: series.map(point => ({
        ...point,
        change: point.value - start,
        changePct: start > 0 ? ((point.value - start) / start) * 100 : 0,
      })),
    };
  }, [positions, priceHistory, dateRange, brokerFilter, ledger]);

  useEffect(() => {
    if (brokerFilter !== 'all' && !brokerOptions.includes(brokerFilter)) setBrokerFilter('all');
  }, [brokerFilter, brokerOptions]);

  const head = (
    <div className="chart-head">
      <div className="chart-title-controls">
        <h2>{brokerFilter === 'all' ? 'Holdings Trend' : `${brokerLabel(brokerFilter)} Trend`}</h2>
        <DateRangeControl value={dateRange} onChange={onRangeChange} />
        {brokerOptions.length > 1 && (
          <label className="broker-filter">Broker
            <select value={brokerFilter} onChange={event => setBrokerFilter(event.target.value)}>
              <option value="all">All brokers</option>
              {brokerOptions.map(broker => <option key={broker} value={broker}>{brokerLabel(broker)}</option>)}
            </select>
          </label>
        )}
      </div>
      {data && <ChartReadout data={data} hover={hover} />}
    </div>
  );

  if (!data) {
    return (
      <section className="panel chart-panel">
        {head}
        <div className="chart-empty">No price history yet — refresh prices or add tracked positions to see the trend.</div>
      </section>
    );
  }

  const series = data.series;
  const width = 1000;
  const height = 190;
  const pad = { top: 16, right: 20, bottom: 20, left: 20 };
  const changes = series.map(point => point.change);
  const min = Math.min(0, ...changes);
  const max = Math.max(0, ...changes);
  const range = max - min || 1;
  const toX = idx => pad.left + (idx / (series.length - 1)) * (width - pad.left - pad.right);
  const toY = value => height - pad.bottom - ((value - min) / range) * (height - pad.top - pad.bottom);
  const zeroY = toY(0);
  const points = changes.map((value, idx) => `${toX(idx).toFixed(1)},${toY(value).toFixed(1)}`).join(' ');
  const area = `M${toX(0)},${zeroY} ${changes.map((value, idx) => `L${toX(idx)},${toY(value)}`).join(' ')} L${toX(changes.length - 1)},${zeroY} Z`;
  const latest = series[series.length - 1];
  const active = hover != null ? series[hover] : latest;
  const lineColor = active.change >= 0 ? 'var(--accent-green)' : 'var(--accent-red)';

  function onMove(event) {
    const rect = svgRef.current?.getBoundingClientRect();
    if (!rect) return;
    const idx = Math.max(0, Math.min(series.length - 1, Math.round(((event.clientX - rect.left) / rect.width) * (series.length - 1))));
    setHover(idx);
  }

  return (
    <section className="panel chart-panel">
      {head}
      <svg ref={svgRef} className="trend-svg" viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" onMouseMove={onMove} onMouseLeave={() => setHover(null)}>
        <line x1={pad.left} x2={width - pad.right} y1={zeroY} y2={zeroY} stroke="rgba(123,140,132,0.35)" />
        <path d={area} fill={active.change >= 0 ? 'rgba(15,138,95,0.08)' : 'rgba(194,65,59,0.08)'} />
        <polyline points={points} fill="none" stroke={lineColor} strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" />
        {hover != null && (
          <>
            <line x1={toX(hover)} x2={toX(hover)} y1={pad.top} y2={height - pad.bottom} stroke="rgba(47,111,159,0.35)" strokeDasharray="4 4" />
            <circle cx={toX(hover)} cy={toY(active.change)} r="5" fill={lineColor} stroke="#fff" strokeWidth="2" />
          </>
        )}
      </svg>
      <div className="chart-axis">
        <span>{series[0].date}</span>
        <span>{series[Math.floor(series.length / 2)].date}</span>
        <span>{latest.date}</span>
      </div>
    </section>
  );
}

function ChartReadout({ data, hover }) {
  const series = data.series;
  const active = hover != null ? series[hover] : series[series.length - 1];
  const tone = active.change >= 0 ? 'positive' : 'negative';
  // Only when the flows are actually known. Without the ledger there is
  // nothing to neutralise against, and a "return" that quietly counts
  // transfers as performance is the error this replaces.
  const ret = data.basis === 'ledger' && data.ranged?.twr_pct != null
    ? data.ranged : null;
  const retTone = ret && ret.twr_pct >= 0 ? 'positive' : 'negative';
  return (
    <div className="chart-readout">
      <span>{series[0].date} – {active.date}</span>
      <span>
        {data.trackedCount} tracked · cost <b>{money(data.currentCost)}</b>
        {/* Named, not just counted: without this the line simply starts in
            June and the reader has no way to know a recent listing is why. */}
        {data.lateSymbols?.length > 0 && (
          <span className="chart-note" title={
            `${data.lateSymbols.join(', ')} joined the chart later — their price history `
            + 'does not cover the whole period.'
          }>
            {' · '}{data.lateSymbols.length} joined later
          </span>
        )}
        {/* Which question the line answers. The two are not interchangeable
            and the difference is large for anyone who trades. */}
        <span className="chart-note" title={
          data.basis === 'ledger'
            ? 'Reconstructed from your transactions: the value of everything you held '
              + 'on each day, cash included. Buying and selling do not move it — they '
              + 'shift value between cash and stock — but deposits and withdrawals do, '
              + 'so the change below is still not a return.'
            : 'No transaction history for this view, so today\u2019s holdings are priced '
              + 'backwards. It shows what this basket would have done, not what you did.'
        }>
          {' · '}{data.basis === 'ledger' ? 'from your transactions' : 'today\u2019s holdings, priced back'}
        </span>
        {/* The window was cut short because the ledger and the holdings
            disagree before this date — more shares sold or bought than were
            ever held. Saying so beats a line that silently starts late. */}
        {data.truncatedTo && (
          <span className="chart-note" title={
            'Your transactions and your current holdings disagree before '
            + `${data.truncatedTo}, so history earlier than that cannot be `
            + 'reconstructed. Check the Transactions tab for duplicated or '
            + 'missing trades.'
          }>
            {' · '}starts {data.truncatedTo}
          </span>
        )}
      </span>
      <span className={`readout-main ${ret ? retTone : tone}`}>
        {/* Return leads where we can measure one. A withdrawal is not a loss
            and a deposit is not a gain; only a flow-neutralised return says
            so, and it is the figure a brokerage statement shows. The value
            change stays underneath, where it explains the gap rather than
            standing in for performance. */}
        {ret ? signedPct(ret.twr_pct) : `${signedMoney(active.change)} (${signedPct(active.changePct)})`}
        <em className="readout-basis">
          {ret ? 'return' : (data.basis === 'ledger' ? 'portfolio value change' : 'price change')}
        </em>
      </span>
      {/* Where the two numbers differ, say why in the same breath. A reader
          who sees a positive return above a falling line is owed the reason
          on the spot, not left to work out that they withdrew the difference. */}
      {ret && Math.abs(ret.net_external) > 0.5 && (
        <span className="readout-flow">
          {signedMoney(ret.value_change)} in value ·{' '}
          {ret.net_external < 0
            ? `${money(Math.abs(ret.net_external))} withdrawn`
            : `${money(ret.net_external)} added`}
        </span>
      )}
    </div>
  );
}

export function AllocationDonut({ entries, total, centerLabel }) {
  // entries: [[label, value], ...] sorted desc
  const size = 124;
  const stroke = 16;
  const radius = (size - stroke) / 2;
  const circumference = 2 * Math.PI * radius;
  let offset = 0;
  const segments = entries.map(([label, value], idx) => {
    const fraction = total > 0 ? value / total : 0;
    const segment = {
      label,
      color: allocColor(idx),
      dash: `${Math.max(0, fraction * circumference - 1.5)} ${circumference}`,
      rotation: (offset / total) * 360 - 90,
    };
    offset += value;
    return segment;
  });

  return (
    <div className="donut-wrap">
      <svg viewBox={`0 0 ${size} ${size}`}>
        <circle cx={size / 2} cy={size / 2} r={radius} fill="none" stroke="var(--bg-inset)" strokeWidth={stroke} />
        {segments.map(segment => (
          <circle
            key={segment.label}
            cx={size / 2}
            cy={size / 2}
            r={radius}
            fill="none"
            stroke={segment.color}
            strokeWidth={stroke}
            strokeDasharray={segment.dash}
            transform={`rotate(${segment.rotation} ${size / 2} ${size / 2})`}
            strokeLinecap="butt"
          />
        ))}
      </svg>
      <div className="donut-center">
        <strong>{money(total)}</strong>
        <span>{centerLabel}</span>
      </div>
    </div>
  );
}
