import React, { useEffect, useState } from 'react';
import { api } from '../api.js';
import { money, signedMoney } from '../format.js';

const PERIOD_ORDER = ['WTD', 'MTD', 'YTD', '1Y', 'MAX'];

function pctText(value) {
  if (value == null || !Number.isFinite(value)) return '—';
  const sign = value >= 0 ? '+' : '−';
  return `${sign}${Math.abs(value).toFixed(2)}%`;
}

export function PerformanceMetrics({ refreshKey = 0, onError }) {
  const [data, setData] = useState(null);
  // What the numbers above are actually built from. Fetched separately so a
  // coverage failure never blanks the performance grid.
  const [coverage, setCoverage] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError('');
    api('/api/v1/portfolio-history')
      .then(payload => { if (!cancelled) setCoverage(payload?.coverage || null); })
      .catch(() => { /* the grid stands on its own */ });
    api('/api/v1/performance')
      .then(payload => {
        if (!cancelled) setData(payload);
      })
      .catch(err => {
        if (cancelled) return;
        const message = err?.message || 'Failed to load performance';
        setError(message);
        if (onError) onError(message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [refreshKey, onError]);

  if (loading && !data) {
    return (
      <section className="performance-panel">
        <header className="performance-head">
          <h2>Performance</h2>
          <span className="performance-meta">Loading…</span>
        </header>
        <div className="performance-grid">
          {PERIOD_ORDER.map(p => (
            <div className="performance-card placeholder" key={p}>
              <span className="performance-card-label">{p}</span>
              <span className="performance-card-value">—</span>
            </div>
          ))}
        </div>
      </section>
    );
  }

  if (error) {
    return (
      <section className="performance-panel">
        <header className="performance-head">
          <h2>Performance</h2>
          <span className="performance-meta error">{error}</span>
        </header>
      </section>
    );
  }

  const periodsByLabel = Object.fromEntries((data?.periods || []).map(p => [p.period, p]));
  const today = {
    abs: data?.today_change ?? 0,
    pct: data?.today_change_pct ?? 0,
  };
  const todayPositive = today.abs >= 0;

  return (
    <section className="performance-panel">
      <header className="performance-head">
        <div>
          <h2>Performance</h2>
          {data?.indicative && (
            <span className="performance-meta hint" title={data.note || ''}>
              indicative · today exact
            </span>
          )}
        </div>
      </header>
      <div className="performance-grid">
        <div className={`performance-card today ${todayPositive ? 'pos' : 'neg'}`}>
          <span className="performance-card-label">Today</span>
          <span className="performance-card-value">{pctText(today.pct)}</span>
          <span className="performance-card-sub">{signedMoney(today.abs)}</span>
        </div>
        {PERIOD_ORDER.map(label => {
          const row = periodsByLabel[label];
          if (!row) {
            return (
              <div className="performance-card placeholder" key={label}>
                <span className="performance-card-label">{label}</span>
                <span className="performance-card-value">—</span>
              </div>
            );
          }
          const positive = row.return_pct >= 0;
          const delta = row.end_value - row.start_value;
          return (
            <div className={`performance-card ${positive ? 'pos' : 'neg'}`} key={label}>
              <span className="performance-card-label">{label}</span>
              <span className="performance-card-value">{pctText(row.return_pct)}</span>
              <span className="performance-card-sub">{signedMoney(delta)}</span>
            </div>
          );
        })}
      </div>
      {coverage && (
        <div className={`coverage-note coverage-${coverage.quality}`} role="note">
          <span className="coverage-badge">
            {coverage.estimated ? 'Estimated' : 'Transaction-accurate'}
          </span>
          {/* This date is where the *ledger* starts, not where any figure on
              this card is measured from. Calling it "Performance since
              2016-06-09" put a decade on a badge whose own returns say "since
              2025-08-31" two lines below it. */}
          <span className="coverage-since">
            {coverage.since ? `Ledger starts ${coverage.since}` : 'No history recorded yet'}
          </span>
          <span className="coverage-message">{coverage.message}</span>
        </div>
      )}
      {/* Named for the question it answers, not for its method.
          `analytics.transaction_returns` draws the boundary at the invested
          sleeve, where a buy is a contribution and a sell a withdrawal — "how
          did my invested capital do". The overview headline draws it around
          the whole portfolio, where buying and selling only move value between
          cash and securities — "how did my portfolio do". Both are legitimate
          and they disagree, so both being labelled TWR on adjacent screens
          read as one of them being broken. */}
      {data?.accurate?.available && (
        <div className="accurate-returns"
             title={
               'Measured at the invested sleeve: money going into a position counts as a '
               + 'contribution and money coming out as a withdrawal. The overview\u2019s '
               + 'return measures the whole portfolio instead, where buying and selling '
               + 'just move value between cash and holdings \u2014 so the two differ.'
             }>
          <span className="accurate-badge">Invested capital</span>
          <span className="accurate-metric">
            TWR <b className={data.accurate.twr_pct >= 0 ? 'pos' : 'neg'}>{pctText(data.accurate.twr_pct)}</b>
          </span>
          {data.accurate.mwr_period_pct != null && (
            <span className="accurate-metric">
              MWR <b className={data.accurate.mwr_period_pct >= 0 ? 'pos' : 'neg'}>{pctText(data.accurate.mwr_period_pct)}</b>
            </span>
          )}
          {data.accurate.mwr_annualized_pct != null && (
            <span className="accurate-metric muted">{pctText(data.accurate.mwr_annualized_pct)}/yr money-weighted</span>
          )}
          <span className="accurate-window">
            since {data.accurate.start_date} · {data.accurate.trade_count} trade{data.accurate.trade_count === 1 ? '' : 's'}
          </span>
        </div>
      )}
      {data?.indicative && data?.note && (
        <p className="performance-note">
          {data.note}
          {data?.accurate?.available === false && data?.accurate?.reason
            ? ` Invested-capital returns unlock once transactions are recorded (${data.accurate.reason.toLowerCase()})`
            : ''}
        </p>
      )}
    </section>
  );
}
