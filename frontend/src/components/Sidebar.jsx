import React, { useMemo, useState } from 'react';
import { AllocationDonut, MiniSparkline } from './Charts.jsx';
import { money, signedMoney, signedPct, brokerLabel, allocColor, positionKey, quantityLabel } from '../format.js';

export function AllocationCard({ portfolio }) {
  const [mode, setMode] = useState('sector');
  const breakdown = mode === 'sector' ? portfolio?.sector_breakdown : portfolio?.broker_breakdown;
  const entries = useMemo(() => {
    const rows = Object.entries(breakdown || {}).filter(([, value]) => value > 0).sort((a, b) => b[1] - a[1]);
    if (rows.length <= 6) return rows;
    const top = rows.slice(0, 5);
    const rest = rows.slice(5).reduce((sum, [, value]) => sum + value, 0);
    return [...top, ['Other', rest]];
  }, [breakdown]);
  const total = entries.reduce((sum, [, value]) => sum + value, 0);

  return (
    <section className="panel">
      <div className="panel-header">
        <h2>Allocation</h2>
        <div className="alloc-toggle">
          <button className={mode === 'sector' ? 'active' : ''} onClick={() => setMode('sector')}>Sector</button>
          <button className={mode === 'broker' ? 'active' : ''} onClick={() => setMode('broker')}>Broker</button>
        </div>
      </div>
      <div className="side-card-body">
        {entries.length === 0 ? (
          <div className="empty-box">No data yet.</div>
        ) : (
          <div className="donut-row">
            <AllocationDonut entries={entries} total={total} centerLabel={mode} />
            <div className="donut-legend">
              {entries.map(([label, value], idx) => (
                <div className="legend-item" key={label}>
                  <i style={{ background: allocColor(idx) }} />
                  <span>{mode === 'broker' ? brokerLabel(label) : label}</span>
                  <b>{total > 0 ? `${((value / total) * 100).toFixed(1)}%` : '0%'}</b>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </section>
  );
}

export function TopHoldings({ positions, total, onSelect }) {
  const top = [...positions]
    .filter(position => position.asset_type !== 'cash')
    .sort((a, b) => b.market_value - a.market_value)
    .slice(0, 6);
  const maxValue = top[0]?.market_value || 1;

  return (
    <section className="panel">
      <div className="panel-header"><h2>Top Holdings</h2></div>
      <div className="side-card-body">
        {top.length === 0 && <div className="empty-box">No holdings yet.</div>}
        {top.map(position => (
          <div
            className="holding-row"
            key={positionKey(position)}
            onClick={() => onSelect?.(position.id)}
            style={onSelect ? { cursor: 'pointer' } : undefined}
          >
            <strong>{position.symbol}</strong>
            <div className="holding-track"><div style={{ width: `${(position.market_value / maxValue) * 100}%` }} /></div>
            <em>{money(position.market_value)} · {total > 0 ? `${((position.market_value / total) * 100).toFixed(1)}%` : '0%'}</em>
          </div>
        ))}
      </div>
    </section>
  );
}

function auditSeverityRank(severity) {
  if (severity === 'critical') return 0;
  if (severity === 'warning') return 1;
  return 2;
}

function auditSeverityLabel(severity) {
  if (severity === 'critical') return 'Fix';
  if (severity === 'warning') return 'Review';
  return 'Note';
}

function auditIssueMatchesPosition(issue, position) {
  if (!issue || !position) return false;
  if (issue.symbol && issue.broker && issue.asset_type) {
    return issue.symbol === position.symbol
      && issue.broker === position.broker
      && issue.asset_type === position.asset_type;
  }
  if (issue.code === 'high_cash_allocation') return position.asset_type === 'cash';
  if (issue.code === 'cross_broker_duplicate') {
    return issue.evidence?.symbol === position.symbol
      && issue.evidence?.asset_type === position.asset_type;
  }
  return false;
}

function auditIssuesForPosition(audit, position) {
  return (audit?.issues || [])
    .filter(issue => auditIssueMatchesPosition(issue, position))
    .sort((a, b) => auditSeverityRank(a.severity) - auditSeverityRank(b.severity));
}

function DataNotes({ issues }) {
  if (!issues.length) return null;
  return (
    <div className="inspector-data-notes" aria-label="Data notes">
      {issues.slice(0, 3).map(issue => (
        <div className={`data-note data-note-${issue.severity || 'info'}`} key={issue.id}>
          <span>{auditSeverityLabel(issue.severity)}</span>
          <strong>{issue.title}</strong>
          <p>{issue.suggested_action}</p>
        </div>
      ))}
    </div>
  );
}

export function PositionInspector({ position, history, audit, onEdit, onOpenTaxLots }) {
  if (!position) {
    return (
      <section className="panel">
        <div className="panel-header"><h2>Position Detail</h2></div>
        <div className="empty-box">Select a position in the table.</div>
      </section>
    );
  }

  const values = history?.closes || [];
  const first = values[0];
  const last = values[values.length - 1];
  const move = first ? ((last - first) / first) * 100 : 0;
  const isCash = position.asset_type === 'cash';
  const dataIssues = auditIssuesForPosition(audit, position);

  return (
    <section className="panel">
      <div className="panel-header">
        <h2>{position.symbol}</h2>
        <div style={{ display: 'flex', gap: 8 }}>
          {!isCash && <button className="link-btn" onClick={() => onOpenTaxLots(position)}>Tax lots</button>}
          <button className="link-btn" onClick={() => onEdit(position)}>Edit</button>
        </div>
      </div>
      <div className="side-card-body">
        {values.length >= 2 && (
          <div className="inspector-spark">
            <MiniSparkline dates={history.dates} values={values} baseline={position.average_cost || null} />
          </div>
        )}
        <DataNotes issues={dataIssues} />
        <div className="kv-row"><span>Broker</span><strong>{brokerLabel(position.broker)}</strong></div>
        <div className="kv-row"><span>Type</span><strong>{position.asset_type}</strong></div>
        {!isCash && <div className="kv-row"><span>Quantity</span><strong>{quantityLabel(position.quantity)}</strong></div>}
        {!isCash && <div className="kv-row"><span>Avg cost</span><strong>{money(position.average_cost)}</strong></div>}
        {!isCash && <div className="kv-row"><span>Price</span><strong>{money(position.current_price)}</strong></div>}
        <div className="kv-row"><span>Market value</span><strong>{money(position.market_value)}</strong></div>
        {!isCash && (
          <div className="kv-row">
            <span>Unrealized</span>
            <strong className={position.unrealized_gain >= 0 ? 'positive' : 'negative'}>
              {signedMoney(position.unrealized_gain)} ({signedPct(position.unrealized_gain_pct)})
            </strong>
          </div>
        )}
        {!isCash && (
          <div className="kv-row">
            <span>Range move</span>
            <strong className={move >= 0 ? 'positive' : 'negative'}>{values.length ? signedPct(move) : '—'}</strong>
          </div>
        )}
      </div>
    </section>
  );
}
