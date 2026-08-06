import React, { useEffect, useState } from 'react';
import { api } from '../api.js';
import { money } from '../format.js';
import { StockChart } from './StockChart.jsx';

function formatPrice(value, currency = 'USD') {
  if (value == null || !Number.isFinite(value) || value === 0) return '—';
  if (currency === 'USD') return `$${value.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
  return `${value.toLocaleString(undefined, { maximumFractionDigits: 2 })} ${currency}`;
}

function formatVolume(value) {
  if (value == null || !Number.isFinite(value) || value === 0) return '—';
  if (value >= 1e9) return `${(value / 1e9).toFixed(2)}B`;
  if (value >= 1e6) return `${(value / 1e6).toFixed(2)}M`;
  if (value >= 1e3) return `${(value / 1e3).toFixed(1)}K`;
  return value.toLocaleString();
}

function formatMarketCap(value) {
  if (value == null || !Number.isFinite(value) || value === 0) return '—';
  if (value >= 1e12) return `$${(value / 1e12).toFixed(2)}T`;
  if (value >= 1e9) return `$${(value / 1e9).toFixed(2)}B`;
  if (value >= 1e6) return `$${(value / 1e6).toFixed(2)}M`;
  return `$${value.toLocaleString()}`;
}

function pctText(value) {
  if (value == null || !Number.isFinite(value)) return '—';
  const sign = value >= 0 ? '+' : '−';
  return `${sign}${Math.abs(value).toFixed(2)}%`;
}

export function StockDetail({ symbol, assetType = 'stock', onClose }) {
  const [quote, setQuote] = useState(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError('');
    api(`/api/v1/quote/${encodeURIComponent(symbol)}?asset_type=${assetType}`)
      .then(payload => {
        if (!cancelled) setQuote(payload);
      })
      .catch(err => {
        if (cancelled) return;
        setError(err?.message || 'Failed to load quote');
        setQuote(null);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [symbol, assetType]);

  const currency = quote?.currency || 'USD';
  const dayPositive = (quote?.day_change ?? 0) >= 0;

  return (
    <div className="stock-detail">
      <div className="stock-detail-head">
        <div className="stock-detail-title">
          <span className="stock-detail-symbol">{symbol}</span>
          <span className="stock-detail-name">
            {loading ? 'Loading…' : quote?.name || (error ? error : '—')}
          </span>
        </div>
        {onClose && (
          <button className="stock-detail-close" onClick={onClose}>
            ← Back
          </button>
        )}
      </div>

      <div className="stock-detail-stats">
        <div className="stock-detail-stat">
          <span className="stock-detail-stat-label">Price</span>
          <span className="stock-detail-stat-value">{formatPrice(quote?.price, currency)}</span>
        </div>
        <div className="stock-detail-stat">
          <span className="stock-detail-stat-label">Day change</span>
          <span
            className="stock-detail-stat-value"
            style={{ color: dayPositive ? 'var(--accent-green)' : 'var(--accent-red)' }}
          >
            {pctText(quote?.day_change_pct)}
          </span>
        </div>
        <div className="stock-detail-stat">
          <span className="stock-detail-stat-label">Prev close</span>
          <span className="stock-detail-stat-value">{formatPrice(quote?.previous_close, currency)}</span>
        </div>
        <div className="stock-detail-stat">
          <span className="stock-detail-stat-label">Day range</span>
          <span className="stock-detail-stat-value">
            {formatPrice(quote?.day_low, currency)} – {formatPrice(quote?.day_high, currency)}
          </span>
        </div>
        <div className="stock-detail-stat">
          <span className="stock-detail-stat-label">52-week</span>
          <span className="stock-detail-stat-value">
            {formatPrice(quote?.year_low, currency)} – {formatPrice(quote?.year_high, currency)}
          </span>
        </div>
        <div className="stock-detail-stat">
          <span className="stock-detail-stat-label">Volume</span>
          <span className="stock-detail-stat-value">{formatVolume(quote?.volume)}</span>
        </div>
        <div className="stock-detail-stat">
          <span className="stock-detail-stat-label">Market cap</span>
          <span className="stock-detail-stat-value">{formatMarketCap(quote?.market_cap)}</span>
        </div>
        <div className="stock-detail-stat">
          <span className="stock-detail-stat-label">Source</span>
          <span className="stock-detail-stat-value">{quote?.provider || '—'}</span>
        </div>
      </div>

      <StockChart symbol={symbol} assetType={assetType} currency={currency} />
    </div>
  );
}
