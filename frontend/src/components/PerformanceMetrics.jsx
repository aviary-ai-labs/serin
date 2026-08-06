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
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError('');
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
      {data?.accurate?.available && (
        <div className="accurate-returns" title={data.accurate.note || ''}>
          <span className="accurate-badge">Real returns</span>
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
            ? ` Real TWR/MWR unlocks once transactions are recorded (${data.accurate.reason.toLowerCase()})`
            : ''}
        </p>
      )}
    </section>
  );
}
