import React, { useEffect, useMemo, useRef, useState } from 'react';
import { money, signedMoney, signedPct, brokerLabel, allocColor } from '../format.js';

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
  const filtered = dates.reduce((acc, date, idx) => {
    if (date >= cutoffStr) {
      acc.dates.push(date);
      acc.closes.push(closes[idx]);
    }
    return acc;
  }, { dates: [], closes: [] });
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

export function PortfolioTrendChart({ positions, priceHistory, dateRange, onRangeChange }) {
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
    const tracked = positions.filter(position => (
      !['cash', 'option'].includes(position.asset_type)
      && priceHistory[position.symbol]?.dates?.length
      && (brokerFilter === 'all' || position.broker === brokerFilter)
    ));
    if (!tracked.length) return null;
    const bySymbol = {};
    tracked.forEach(position => {
      const filtered = filterHistoryByRange(priceHistory[position.symbol], dateRange);
      bySymbol[position.symbol] = Object.fromEntries(filtered.dates.map((date, idx) => [date, filtered.closes[idx]]));
    });
    const dates = [...new Set(Object.values(bySymbol).flatMap(item => Object.keys(item)))].sort();
    const series = [];
    dates.forEach(date => {
      let value = 0;
      let count = 0;
      tracked.forEach(position => {
        const close = bySymbol[position.symbol]?.[date];
        if (close != null) {
          value += close * position.quantity;
          count += 1;
        }
      });
      if (count >= Math.max(1, tracked.length * 0.5)) series.push({ date, value });
    });
    if (series.length < 3) return null;
    const start = series[0].value;
    const currentCost = tracked.reduce((sum, position) => sum + position.total_cost, 0);
    return {
      currentCost,
      trackedCount: tracked.length,
      series: series.map(point => ({
        ...point,
        change: point.value - start,
        changePct: start > 0 ? ((point.value - start) / start) * 100 : 0,
      })),
    };
  }, [positions, priceHistory, dateRange, brokerFilter]);

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
  return (
    <div className="chart-readout">
      <span>{series[0].date} – {active.date}</span>
      <span>{data.trackedCount} tracked · cost <b>{money(data.currentCost)}</b></span>
      <span className={`readout-main ${tone}`}>
        {signedMoney(active.change)} ({signedPct(active.changePct)})
      </span>
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
