import React, { useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../api.js';
import { money } from '../format.js';

const PERIODS = ['1w', '1m', '3m', 'ytd', '1y', 'max'];
const PERIOD_LABEL = { '1w': '1W', '1m': '1M', '3m': '3M', ytd: 'YTD', '1y': '1Y', max: 'MAX' };

// Simple moving average over `window` closes; emits nulls for indices < window.
function movingAverage(closes, window = 50) {
  if (!Array.isArray(closes) || closes.length < window) return new Array(closes?.length || 0).fill(null);
  const out = new Array(closes.length).fill(null);
  let sum = 0;
  for (let i = 0; i < closes.length; i += 1) {
    sum += closes[i];
    if (i >= window) sum -= closes[i - window];
    if (i >= window - 1) out[i] = sum / window;
  }
  return out;
}

function formatPrice(value, currency = 'USD') {
  if (value == null || !Number.isFinite(value)) return '—';
  if (currency === 'USD') return `$${value.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
  return `${value.toLocaleString(undefined, { maximumFractionDigits: 2 })} ${currency}`;
}

function signedPrice(absolute, currency = 'USD') {
  if (absolute == null) return '—';
  const sign = absolute >= 0 ? '+' : '−';
  return `${sign}${formatPrice(Math.abs(absolute), currency)}`;
}

function signedPctText(pct) {
  if (pct == null || !Number.isFinite(pct)) return '—';
  const sign = pct >= 0 ? '+' : '−';
  return `${sign}${Math.abs(pct).toFixed(2)}%`;
}

export function StockChart({ symbol, assetType = 'stock', currency = 'USD', height = 280 }) {
  const [period, setPeriod] = useState('3m');
  const [showMA, setShowMA] = useState(true);
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [hover, setHover] = useState(null);
  const svgRef = useRef(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError('');
    api(`/api/v1/quote/${encodeURIComponent(symbol)}/history?period=${period}&asset_type=${assetType}`)
      .then(payload => {
        if (cancelled) return;
        setData(payload);
      })
      .catch(err => {
        if (cancelled) return;
        setError(err?.message || 'Failed to load price history');
        setData(null);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [symbol, period, assetType]);

  const series = useMemo(() => {
    const dates = data?.dates || [];
    const closes = data?.closes || [];
    if (!dates.length || dates.length !== closes.length) return null;
    const ma = showMA ? movingAverage(closes, 50) : new Array(closes.length).fill(null);
    return { dates, closes, ma };
  }, [data, showMA]);

  if (loading && !series) {
    return (
      <div className="stock-chart">
        <StockChartControls
          period={period}
          onPeriod={setPeriod}
          showMA={showMA}
          onToggleMA={() => setShowMA(v => !v)}
        />
        <div className="stock-chart-empty" style={{ height }}>Loading {PERIOD_LABEL[period]} chart…</div>
      </div>
    );
  }

  if (error || !series || series.dates.length < 2) {
    return (
      <div className="stock-chart">
        <StockChartControls
          period={period}
          onPeriod={setPeriod}
          showMA={showMA}
          onToggleMA={() => setShowMA(v => !v)}
        />
        <div className="stock-chart-empty" style={{ height }}>
          {/* The endpoint answers 200 with an empty series and its reason in
              `errors` — saying only "no history" left the reader to guess
              between "new symbol" and "the provider is refusing us". */}
          {error || data?.errors?.[0] || `No price history for ${symbol} in this window.`}
        </div>
      </div>
    );
  }

  const { dates, closes, ma } = series;
  const width = 1000;
  const pad = { top: 18, right: 60, bottom: 22, left: 12 };

  const minClose = Math.min(...closes);
  const maxClose = Math.max(...closes);
  const valueRange = maxClose - minClose || maxClose * 0.01 || 1;
  // Add 4% headroom above & below for breathing room.
  const yMin = minClose - valueRange * 0.04;
  const yMax = maxClose + valueRange * 0.04;
  const range = yMax - yMin || 1;

  const toX = idx => pad.left + (idx / (closes.length - 1)) * (width - pad.left - pad.right);
  const toY = value => height - pad.bottom - ((value - yMin) / range) * (height - pad.top - pad.bottom);

  const linePoints = closes.map((value, idx) => `${toX(idx).toFixed(1)},${toY(value).toFixed(1)}`).join(' ');
  const areaPath = `M${toX(0).toFixed(1)},${toY(yMin).toFixed(1)} ${closes
    .map((value, idx) => `L${toX(idx).toFixed(1)},${toY(value).toFixed(1)}`)
    .join(' ')} L${toX(closes.length - 1).toFixed(1)},${toY(yMin).toFixed(1)} Z`;

  const maPoints = ma
    .map((value, idx) => (value != null ? `${toX(idx).toFixed(1)},${toY(value).toFixed(1)}` : null))
    .filter(Boolean)
    .join(' ');

  const first = closes[0];
  const last = closes[closes.length - 1];
  const activeIdx = hover != null ? hover : closes.length - 1;
  const activeClose = closes[activeIdx];
  const activeMA = ma[activeIdx];
  const periodChange = last - first;
  const periodChangePct = first > 0 ? (periodChange / first) * 100 : 0;
  const positive = periodChange >= 0;
  const lineColor = positive ? 'var(--accent-green, #2eb887)' : 'var(--accent-red, #d2493d)';
  const areaFill = positive ? 'rgba(46,184,135,0.10)' : 'rgba(210,73,61,0.10)';

  // Gridlines: 4 horizontal at quartiles.
  const gridY = [0.25, 0.5, 0.75].map(t => yMin + range * t);

  function onMove(event) {
    const rect = svgRef.current?.getBoundingClientRect();
    if (!rect) return;
    const ratio = (event.clientX - rect.left) / rect.width;
    const idx = Math.max(0, Math.min(closes.length - 1, Math.round(ratio * (closes.length - 1))));
    setHover(idx);
  }

  return (
    <div className="stock-chart">
      <div className="stock-chart-head">
        <div className="stock-chart-readout">
          <div className="stock-chart-price">{formatPrice(activeClose, currency)}</div>
          <div className={`stock-chart-change ${positive ? 'pos' : 'neg'}`}>
            {signedPrice(activeClose - first, currency)} · {signedPctText(((activeClose - first) / first) * 100)}
          </div>
          <div className="stock-chart-meta">
            {dates[activeIdx]} · {dates[0]} → {dates[dates.length - 1]}
          </div>
        </div>
        <StockChartControls
          period={period}
          onPeriod={setPeriod}
          showMA={showMA}
          onToggleMA={() => setShowMA(v => !v)}
        />
      </div>

      <svg
        ref={svgRef}
        className="stock-chart-svg"
        viewBox={`0 0 ${width} ${height}`}
        preserveAspectRatio="none"
        onMouseMove={onMove}
        onMouseLeave={() => setHover(null)}
        role="img"
        aria-label={`Price chart for ${symbol}`}
      >
        <defs>
          <linearGradient id={`fill-${symbol}`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={positive ? '#2eb887' : '#d2493d'} stopOpacity="0.18" />
            <stop offset="100%" stopColor={positive ? '#2eb887' : '#d2493d'} stopOpacity="0" />
          </linearGradient>
        </defs>

        {gridY.map(value => (
          <g key={value}>
            <line
              x1={pad.left}
              x2={width - pad.right}
              y1={toY(value)}
              y2={toY(value)}
              stroke="rgba(123,140,132,0.15)"
              strokeDasharray="2 4"
            />
            <text
              x={width - pad.right + 6}
              y={toY(value) + 4}
              fontSize="11"
              fill="rgba(123,140,132,0.85)"
            >
              {formatPrice(value, currency)}
            </text>
          </g>
        ))}

        <path d={areaPath} fill={`url(#fill-${symbol})`} stroke="none" />
        <polyline
          points={linePoints}
          fill="none"
          stroke={lineColor}
          strokeWidth="2.25"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
        {showMA && maPoints && (
          <polyline
            points={maPoints}
            fill="none"
            stroke="rgba(185,130,23,0.85)"
            strokeWidth="1.5"
            strokeDasharray="4 4"
            strokeLinecap="round"
          />
        )}

        {hover != null && (
          <g>
            <line
              x1={toX(hover)}
              x2={toX(hover)}
              y1={pad.top}
              y2={height - pad.bottom}
              stroke="rgba(47,111,159,0.45)"
              strokeDasharray="3 3"
            />
            <circle cx={toX(hover)} cy={toY(activeClose)} r="4.5" fill={lineColor} stroke="#fff" strokeWidth="1.5" />
            {showMA && activeMA != null && (
              <circle cx={toX(hover)} cy={toY(activeMA)} r="3" fill="rgba(185,130,23,0.95)" stroke="#fff" strokeWidth="1" />
            )}
          </g>
        )}
      </svg>

      <div className="stock-chart-axis">
        <span>{dates[0]}</span>
        <span>{dates[Math.floor(dates.length / 4)]}</span>
        <span>{dates[Math.floor(dates.length / 2)]}</span>
        <span>{dates[Math.floor((3 * dates.length) / 4)]}</span>
        <span>{dates[dates.length - 1]}</span>
      </div>

      <div className="stock-chart-foot">
        <span className="stock-chart-period-summary">
          {PERIOD_LABEL[period]} · <strong className={positive ? 'pos' : 'neg'}>{signedPctText(periodChangePct)}</strong> ({signedPrice(periodChange, currency)})
        </span>
        {showMA && <span className="stock-chart-legend"><i className="ma-swatch" /> 50-day MA</span>}
        {data?.provider && <span className="stock-chart-source">Source: {data.provider}</span>}
      </div>
    </div>
  );
}

function StockChartControls({ period, onPeriod, showMA, onToggleMA }) {
  return (
    <div className="stock-chart-controls">
      <div className="segmented">
        {PERIODS.map(option => (
          <button
            key={option}
            className={period === option ? 'active' : ''}
            onClick={() => onPeriod(option)}
          >
            {PERIOD_LABEL[option]}
          </button>
        ))}
      </div>
      <button className={`pill-toggle ${showMA ? 'on' : ''}`} onClick={onToggleMA}>
        50-MA
      </button>
    </div>
  );
}

// Compact inline sparkline for use in tables — built on top of /api/v1/quote/{symbol}/history.
export function InlineSparkline({ symbol, assetType = 'stock', width = 96, height = 28 }) {
  const [closes, setCloses] = useState([]);
  useEffect(() => {
    let cancelled = false;
    api(`/api/v1/quote/${encodeURIComponent(symbol)}/history?period=1m&asset_type=${assetType}`)
      .then(payload => {
        if (!cancelled) setCloses(payload?.closes || []);
      })
      .catch(() => {
        if (!cancelled) setCloses([]);
      });
    return () => {
      cancelled = true;
    };
  }, [symbol, assetType]);

  if (closes.length < 2) return <span className="muted-cell">—</span>;
  const min = Math.min(...closes);
  const max = Math.max(...closes);
  const range = max - min || 1;
  const points = closes
    .map((value, idx) => {
      const x = (idx / (closes.length - 1)) * width;
      const y = height - 2 - ((value - min) / range) * (height - 4);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(' ');
  const positive = closes[closes.length - 1] >= closes[0];
  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="inline-spark" preserveAspectRatio="none">
      <polyline
        points={points}
        fill="none"
        stroke={positive ? 'var(--accent-green, #2eb887)' : 'var(--accent-red, #d2493d)'}
        strokeWidth="1.75"
        strokeLinecap="round"
      />
    </svg>
  );
}
