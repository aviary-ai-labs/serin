import React, { useCallback, useEffect, useState } from 'react';
import { api } from '../api.js';

// The X-ray is pack-driven: the frontend holds no paid logic and renders
// whatever POST /api/connectors/xray/run returns. Three surfaces share one
// fetch (useXray in App):
//   - a nav tab that only exists when the pack is installed,
//   - XrayView, the full-page report (or upsell when unlicensed),
//   - XrayTeaser, a compact Overview-sidebar bridge that deep-links to the tab.

export function useXray() {
  const [state, setState] = useState({ status: 'loading' });

  const reload = useCallback(() => {
    let alive = true;
    api('/api/connectors/xray/run', { method: 'POST', body: JSON.stringify({}) })
      .then(data => { if (alive) setState({ status: 'ok', data }); })
      .catch(err => { if (alive) setState({ status: err.status === 404 ? 'absent' : 'error' }); });
    return () => { alive = false; };
  }, []);

  useEffect(() => reload(), [reload]);
  return { ...state, reload };
}

const pct = x => `${(Math.max(0, x || 0) * 100).toFixed(1)}%`;
const pctFine = x => `${(Math.max(0, x || 0) * 100).toFixed(2)}%`; // fee-sized numbers
const SIZE_LABELS = { mega: 'Mega cap', large: 'Large cap', mid: 'Mid cap', small: 'Small cap' };
const CONCENTRATION = hhi => (hhi > 0.25 ? 'Concentrated' : hhi > 0.15 ? 'Moderate' : 'Diversified');

// --- Overview sidebar teaser -------------------------------------------------

export function XrayTeaser({ xray, onOpen }) {
  if (xray.status !== 'ok') return null; // no pack → no ad, per the pledge
  const { data } = xray;
  const flags = data.flags || [];

  return (
    <section className="panel xray-card xray-teaser">
      <div className="xray-head"><h3>Portfolio X-ray</h3><span className="xray-badge">Intelligence</span></div>
      {data.entitled ? (
        <>
          <div className="xray-teaser-stats">
            {flags.length > 0 ? `${flags.length} risk flag${flags.length > 1 ? 's' : ''}` : 'No risk flags'}
            {data.effective_holdings != null && ` · ${data.effective_holdings} effective holdings`}
          </div>
          {flags.length > 0 && <div className="xray-teaser-flag">{flags[0]}</div>}
        </>
      ) : (
        <p className="xray-upsell">{data.message || 'Unlock the full portfolio X-ray.'}</p>
      )}
      <button className="btn btn-sm" onClick={onOpen}>Open X-ray →</button>
    </section>
  );
}

// --- Full-page report ----------------------------------------------------------

export function XrayView({ xray }) {
  if (xray.status === 'loading') {
    return <section className="panel"><div className="empty-box">Running X-ray…</div></section>;
  }
  if (xray.status !== 'ok') {
    return <section className="panel"><div className="empty-box">X-ray is unavailable — is the Intelligence pack installed?</div></section>;
  }
  const { data } = xray;
  if (!data.entitled) return <XrayUpsell message={data.message} />;
  return <XrayReport data={data} onReload={xray.reload} />;
}

function XrayUpsell({ message }) {
  return (
    <section className="xray-page">
      <div className="panel xray-hero">
        <span className="xray-badge">Intelligence</span>
        <h2>See what your portfolio is really made of</h2>
        <p>{message || 'Add a license key to unlock the full X-ray report.'}</p>
        <ul className="xray-hero-list">
          <li>Concentration &amp; effective holdings</li>
          <li>Sector · asset · account · currency mixes</li>
          <li>Cash drag &amp; fund fees</li>
          <li>Factor snapshot — beta, P/E, size</li>
          <li>Cross-account overlap</li>
          <li>Actionable risk flags</li>
        </ul>
        <a className="btn btn-primary" href="/#pricing" target="_blank" rel="noreferrer">See plans →</a>
        <p className="xray-foot">Everything else in Serin stays free — the X-ray only adds.</p>
      </div>
    </section>
  );
}

const signedPct = x => `${x >= 0 ? '+' : ''}${(x || 0).toFixed(1)}%`;
const PERIOD_LABELS = { '1m': '1M', '3m': '3M', ytd: 'YTD', '1y': '1Y' };

function XrayReport({ data, onReload }) {
  const largest = data.largest || [];
  const flags = data.flags || [];
  const overlap = data.cross_broker_overlap || [];
  const fees = data.fee_drag;
  const factor = data.factor_snapshot;
  const benchmark = data.benchmark;
  const benchHead = benchmark?.periods?.find(p => p.period === '3m') || benchmark?.periods?.[0];
  const sizeMix = Object.entries(factor?.size_mix || {});
  const hhi = data.concentration_hhi || 0;
  const peak = largest[0]?.weight || 1;

  return (
    <section className="xray-page">
      <header className="xray-page-head">
        <div className="xray-titlewrap">
          <h2>Portfolio X-ray</h2>
          <span className="xray-badge">Intelligence</span>
        </div>
        <button className="btn btn-ghost btn-sm" onClick={onReload}>Recompute</button>
      </header>

      {flags.length > 0 && (
        <ul className="xray-flags">
          {flags.map((f, i) => <li key={i}>{f}</li>)}
        </ul>
      )}

      <section className="stats-grid xray-band">
        <div className="stat-card">
          <div className="stat-label">Concentration</div>
          <div className="stat-value">{CONCENTRATION(hhi)}</div>
          <div className="stat-sub">
            {data.effective_holdings != null
              ? `${data.effective_holdings} effective of ${data.holdings_count} holdings`
              : `HHI ${hhi.toFixed(2)}`}
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Top 5 weight</div>
          <div className="stat-value">{pct(data.top5_weight)}</div>
          <div className="stat-sub">of total value</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Cash drag</div>
          <div className="stat-value">{pct(data.cash_drag)}</div>
          <div className="stat-sub">uninvested</div>
        </div>
        {fees && (
          <div className="stat-card">
            <div className="stat-label">Fund fees</div>
            <div className="stat-value">{pctFine(fees.weighted_expense_ratio)}</div>
            <div className="stat-sub">
              {fees.funds?.length ? `≈ $${Math.round(fees.annual_cost).toLocaleString()}/yr` : 'none detected'}
            </div>
          </div>
        )}
        {factor?.weighted_beta != null && (
          <div className="stat-card">
            <div className="stat-label">Beta</div>
            <div className="stat-value">{factor.weighted_beta.toFixed(2)}</div>
            <div className="stat-sub">portfolio-weighted vs market</div>
          </div>
        )}
        {factor?.weighted_dividend_yield != null && (
          <div className="stat-card">
            <div className="stat-label">Dividend yield</div>
            <div className="stat-value">{pct(factor.weighted_dividend_yield)}</div>
            <div className="stat-sub">trailing, weighted</div>
          </div>
        )}
        {benchHead && (
          <div className="stat-card">
            <div className="stat-label">vs {benchmark.symbol} · {PERIOD_LABELS[benchHead.period] || benchHead.period}</div>
            <div className={`stat-value ${benchHead.delta_pct >= 0 ? 'positive' : 'negative'}`}>
              {signedPct(benchHead.delta_pct)}
            </div>
            <div className="stat-sub">
              you {signedPct(benchHead.portfolio_pct)} · {benchmark.symbol} {signedPct(benchHead.benchmark_pct)}
            </div>
          </div>
        )}
      </section>

      <div className="xray-grid">
        {largest.length > 0 && (
          <section className="panel">
            <div className="xray-bars-title">Largest positions</div>
            {largest.map(item => (
              <div className="xray-bar-row" key={item.symbol}>
                <span className="xray-bar-sym">{item.symbol}</span>
                <span className="xray-bar-track">
                  <span className="xray-bar-fill" style={{ width: `${Math.min(100, (item.weight / peak) * 100)}%` }} />
                </span>
                <span className="xray-bar-val">{pct(item.weight)}</span>
              </div>
            ))}
          </section>
        )}

        {factor && (
          <section className="panel">
            <div className="xray-bars-title">Factor snapshot</div>
            <div className="xray-factor-grid">
              {factor.weighted_pe != null && (
                <div className="xray-factor-cell"><span>{factor.weighted_pe}</span><small>P/E</small></div>
              )}
              {factor.weighted_pb != null && (
                <div className="xray-factor-cell"><span>{factor.weighted_pb}</span><small>P/B</small></div>
              )}
              {factor.weighted_beta != null && (
                <div className="xray-factor-cell"><span>{factor.weighted_beta.toFixed(2)}</span><small>Beta</small></div>
              )}
              {factor.weighted_dividend_yield != null && (
                <div className="xray-factor-cell"><span>{pct(factor.weighted_dividend_yield)}</span><small>Div yield</small></div>
              )}
            </div>
            {sizeMix.length > 0 && <MixBars entries={sizeMix.map(([k, w]) => [SIZE_LABELS[k] || k, w])} />}
            <div className="xray-foot">Based on {pct(factor.coverage)} of invested value</div>
          </section>
        )}

        {benchmark && (
          <section className="panel">
            <div className="xray-bars-title">Vs {benchmark.symbol === 'SPY' ? 'S&P 500' : benchmark.symbol}</div>
            <div className="xray-bench-row xray-bench-head">
              <span /><span>You</span><span>{benchmark.symbol}</span><span>Δ</span>
            </div>
            {benchmark.periods.map(row => (
              <div className="xray-bench-row" key={row.period}>
                <span className="xray-bar-sym">{PERIOD_LABELS[row.period] || row.period}</span>
                <span>{signedPct(row.portfolio_pct)}</span>
                <span>{signedPct(row.benchmark_pct)}</span>
                <span className={row.delta_pct >= 0 ? 'positive' : 'negative'}>{signedPct(row.delta_pct)}</span>
              </div>
            ))}
            <div className="xray-foot">
              Holdings-based — assumes today's positions held all period; deposits ignored.
              Coverage {pct((benchmark.periods[benchmark.periods.length - 1] || {}).coverage)}.
            </div>
          </section>
        )}

        <MixPanel title="Sector mix" mix={data.sector_mix} />
        <MixPanel title="Asset mix" mix={data.asset_mix} capitalize />
        <MixPanel title="By account" mix={data.broker_mix} capitalize />
        <MixPanel title="By currency" mix={data.currency_mix} />

        {overlap.length > 0 && (
          <section className="panel">
            <div className="xray-bars-title">Held across accounts</div>
            {overlap.map(o => (
              <div className="xray-sector-row" key={o.symbol}>
                <span className="xray-bar-sym">{o.symbol}</span>
                <span>{o.brokers.join(' · ')}</span>
              </div>
            ))}
            <div className="xray-foot">The same symbol in more than one account — worth checking for accidental doubling-up.</div>
          </section>
        )}
      </div>
    </section>
  );
}

function MixPanel({ title, mix, capitalize }) {
  // Drop slices that would render as 0.0% (e.g. an options-only sector).
  const entries = Object.entries(mix || {}).filter(([, w]) => w >= 0.0005);
  if (entries.length < 2) return null; // a single-slice mix says nothing
  return (
    <section className="panel">
      <div className="xray-bars-title">{title}</div>
      <MixBars entries={entries} capitalize={capitalize} />
    </section>
  );
}

function MixBars({ entries, capitalize }) {
  const peak = Math.max(...entries.map(([, w]) => w), 0.0001);
  return (
    <>
      {entries.map(([name, w]) => (
        <div className="xray-mix-row" key={name}>
          <span className="xray-mix-name" style={capitalize ? { textTransform: 'capitalize' } : undefined}>{name}</span>
          <span className="xray-bar-track">
            <span className="xray-bar-fill" style={{ width: `${Math.min(100, (w / peak) * 100)}%` }} />
          </span>
          <span className="xray-bar-val">{pct(w)}</span>
        </div>
      ))}
    </>
  );
}
