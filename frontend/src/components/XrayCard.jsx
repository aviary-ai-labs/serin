import React, { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { api } from '../api.js';

// The X-ray is pack-driven: the frontend holds no paid logic and renders
// whatever POST /api/connectors/xray/run returns. Three surfaces share one
// fetch (useXray in App):
//   - a nav tab that only exists when the pack is installed,
//   - XrayView, the full-page report (or upsell when unlicensed),
//   - XrayTeaser, a compact Overview-sidebar bridge that deep-links to the tab.

export function useXray() {
  const [state, setState] = useState({ status: 'loading' });
  // A recompute takes seven to eight seconds against the live portfolio. With
  // no pending state the button looked broken: you clicked, nothing moved, and
  // the same numbers eventually reappeared — indistinguishable from a dead
  // control. It also accepted a second click, which sent a second request.
  const [running, setRunning] = useState(false);
  const [ranAt, setRanAt] = useState(null);

  const reload = useCallback(() => {
    let alive = true;
    setRunning(true);
    api('/api/connectors/xray/run', { method: 'POST', body: JSON.stringify({}) })
      .then(data => {
        if (!alive) return;
        setState({ status: 'ok', data });
        setRanAt(new Date());
      })
      .catch(err => { if (alive) setState({ status: err.status === 404 ? 'absent' : 'error' }); })
      .finally(() => { if (alive) setRunning(false); });
    return () => { alive = false; };
  }, []);

  useEffect(() => reload(), [reload]);
  return { ...state, reload, running, ranAt };
}

const pct = x => `${(Math.max(0, x || 0) * 100).toFixed(1)}%`;
const pctFine = x => `${(Math.max(0, x || 0) * 100).toFixed(2)}%`; // fee-sized numbers
const SIZE_LABELS = { mega: 'Mega cap', large: 'Large cap', mid: 'Mid cap', small: 'Small cap' };
const CONCENTRATION = hhi => (hhi > 0.25 ? 'Concentrated' : hhi > 0.15 ? 'Moderate' : 'Diversified');
// A verdict is worth a colour: red for a portfolio riding on a few names.
const CONCENTRATION_TONE = hhi => (hhi > 0.25 ? 'negative' : hhi > 0.15 ? 'warn' : 'positive');

// A stat value that shrinks until it fits its card. These cards hold both
// "0.0%" and "Concentrated"; one type size cannot serve both, and the long
// words ran past the border. Measuring beats guessing at a width that depends
// on the viewport, the label, and how many cards the report decided to render.
function FitValue({ children, className = '', max = 30, min = 15 }) {
  const ref = useRef(null);
  const fittedAt = useRef(-1);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return undefined;
    const fit = () => {
      el.style.fontSize = `${max}px`;
      const avail = el.clientWidth;   // content box — what we have
      const needed = el.scrollWidth;  // the nowrap text — what we want
      if (needed > avail && avail > 0) {
        el.style.fontSize = `${Math.max(min, Math.floor((max * avail) / needed))}px`;
      }
      fittedAt.current = avail;
    };
    fit();
    if (typeof ResizeObserver === 'undefined') return undefined;
    // Width-guarded: shrinking the type changes the element's height, and an
    // observer that reacted to that would chase its own tail.
    const observer = new ResizeObserver(() => {
      if (el.clientWidth !== fittedAt.current) fit();
    });
    observer.observe(el);
    return () => observer.disconnect();
  });

  return <div ref={ref} className={`stat-value ${className}`.trim()}>{children}</div>;
}

// --- Overview sidebar teaser -------------------------------------------------

export function XrayTeaser({ xray, onOpen }) {
  if (xray.status !== 'ok') return null; // no pack → no ad, per the pledge
  const { data } = xray;
  const flags = data.flags || [];
  // The reason to open the tab. Risk flags describe a standing condition the
  // reader has usually already seen; the drift line is the thing that is
  // different from last time they looked.
  const drifted = data.drift?.available ? (data.drift.flags || [])[0] : null;

  return (
    <section className="panel xray-card xray-teaser">
      <div className="xray-head"><h3>Portfolio X-ray</h3><span className="xray-badge">Intelligence</span></div>
      {data.entitled ? (
        <>
          <div className="xray-teaser-stats">
            {flags.length > 0 ? `${flags.length} risk flag${flags.length > 1 ? 's' : ''}` : 'No risk flags'}
            {data.effective_holdings != null && ` · ${data.effective_holdings} effective holdings`}
          </div>
          {drifted && <div className="xray-teaser-drift">{drifted}</div>}
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
  return (
    <XrayReport
      data={data}
      onReload={xray.reload}
      running={xray.running}
      ranAt={xray.ranAt}
    />
  );
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

// Weights, unclamped: `pct` floors at zero, and a margin balance is a real
// negative cash weight that the drift panel has to be able to print.
const weightPct = x => `${((x || 0) * 100).toFixed(1)}%`;
// A weight moved from 41% to 54% moved thirteen *points*, not thirteen
// percent. Percent of a percent is the ambiguity that makes people distrust
// a number, so the unit is on the page.
const points = x => `${x >= 0 ? '+' : '\u2212'}${Math.abs((x || 0) * 100).toFixed(1)} pts`;
// Effective holdings is a count, so its move is a count too — signedPct would
// print "−0.4%" for four-tenths of a holding.
const signedCount = x => `${x >= 0 ? '+' : '\u2212'}${Math.abs(x || 0).toFixed(1)}`;
// Same minus sign as the deltas beside it. A row reading "−4.0 pts · price
// -48.1%" mixes a typographic minus with a hyphen at the same size.
const signedMove = x => `${x >= 0 ? '+' : '\u2212'}${Math.abs(x || 0).toFixed(1)}%`;
const DRIFT_FORMAT = { pct: weightPct, num: x => (x || 0).toFixed(1) };
const DRIFT_DELTA = { pct: points, num: signedCount };

// --- drift: what the portfolio has become -----------------------------------

// A sparkline, not a chart: the shape of a slow change, at a size that fits
// under the number it describes. `vector-effect` keeps the stroke honest —
// preserveAspectRatio="none" stretches geometry, and a stretched stroke is
// twice as thick at one end as the other.
function Spark({ values }) {
  if (!values || values.length < 3) return null;
  const low = Math.min(...values);
  const high = Math.max(...values);
  const span = high - low || 1;
  const y = v => 26 - ((v - low) / span) * 24;
  const path = values.map((v, i) => `${(i / (values.length - 1)) * 100},${y(v)}`).join(' ');
  return (
    <svg className="xray-spark" viewBox="0 0 100 28" preserveAspectRatio="none" aria-hidden="true">
      {/* Every spark is scaled to its own range, so four-tenths of a holding
          fills the same box as thirty points of cash. The baseline is where
          the window started: without it the shape is dramatic and unanchored. */}
      <line x1="0" x2="100" y1={y(values[0])} y2={y(values[0])} vectorEffect="non-scaling-stroke" />
      <polyline points={path} vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

// One mix row, drawn as where the weight was and where it went. The solid
// stretch is the part that was already there; the cap is the change. Reading
// two numbers off a row is work — reading a bar that grew is not.
function DriftRow({ row, peak, ledgerBacked }) {
  const base = Math.min(row.then, row.now);
  const tip = Math.max(row.then, row.now);
  const grew = row.now >= row.then;
  const tag = row.traded
    ? 'you traded'
    : row.price_change_pct != null
      ? `price ${signedMove(row.price_change_pct)}`
      : ledgerBacked ? 'untouched' : null;

  return (
    <div className="xray-drift-row">
      <span className="xray-bar-sym">{row.name}</span>
      <span className="xray-bar-track">
        <span className="xray-bar-fill" style={{ width: `${(base / peak) * 100}%` }} />
        <span
          className={`xray-drift-cap ${grew ? 'grew' : 'shrank'}`}
          style={{ width: `${((tip - base) / peak) * 100}%` }}
        />
      </span>
      <span className="xray-drift-then">{weightPct(row.then)} →</span>
      <span className="xray-bar-val">{weightPct(row.now)}</span>
      <span className="xray-drift-delta">{grew ? '↑' : '↓'} {points(row.delta)}</span>
      <span className="xray-drift-tag">{tag}</span>
    </div>
  );
}

function DriftRows({ title, rows, ledgerBacked }) {
  if (!rows || rows.length === 0) return null;
  const peak = Math.max(...rows.map(r => Math.max(r.then, r.now)), 0.0001);
  return (
    <div className="xray-drift-col">
      <div className="xray-drift-subtitle">{title}</div>
      {rows.map(row => (
        <DriftRow key={row.name} row={row} peak={peak} ledgerBacked={ledgerBacked} />
      ))}
    </div>
  );
}

// The panel the tab leads with. Everything below it describes the portfolio as
// it is today — true, and identical every morning to anyone who trades a few
// times a year. This is the part that moves on its own.
function XrayDrift({ drift }) {
  if (!drift) return null;
  if (!drift.available) {
    return (
      <section className="panel xray-drift xray-drift-quiet">
        <div className="xray-drift-head">
          <div className="xray-bars-title">What's changed</div>
        </div>
        <div className="xray-foot">{drift.reason}</div>
      </section>
    );
  }

  const trace = drift.series || [];
  const sparks = {
    top5_weight: trace.map(p => p.top5),
    cash_drag: trace.map(p => p.cash),
    effective_holdings: trace.map(p => p.effective),
  };

  return (
    <section className="panel xray-drift">
      <div className="xray-drift-head">
        <div className="xray-bars-title">What's changed</div>
        <span className="xray-drift-span">
          {drift.span_label} · {drift.from} → {drift.to}
        </span>
      </div>

      {drift.flags.length > 0 && (
        <ul className="xray-drift-lede">
          {drift.flags.map((flag, i) => <li key={i}>{flag}</li>)}
        </ul>
      )}

      <div className="xray-drift-metrics">
        {drift.metrics.map(metric => {
          const format = DRIFT_FORMAT[metric.format] || DRIFT_FORMAT.num;
          const rising = metric.delta >= 0;
          return (
            <div className="xray-drift-metric" key={metric.key}>
              <div className="stat-label">{metric.label}</div>
              <div className="xray-drift-metric-value">
                <span className="xray-drift-then">{format(metric.then)} →</span>
                <strong>{format(metric.now)}</strong>
                <span className="xray-drift-delta">
                  {rising ? '↑' : '↓'} {(DRIFT_DELTA[metric.format] || signedCount)(metric.delta)}
                </span>
              </div>
              <Spark values={sparks[metric.key]} />
            </div>
          );
        })}
      </div>

      <div className="xray-drift-cols">
        <DriftRows title="By sector" rows={drift.sectors} ledgerBacked={drift.ledger_backed} />
        <DriftRows title="By holding" rows={drift.positions} ledgerBacked={drift.ledger_backed} />
      </div>

      {/* What the reconstruction is and is not. The weights are rebuilt from
          today's holdings and the transaction ledger, so how much of that
          ledger exists decides how much of this is measured rather than
          inferred. The basis note matters more than it looks: this panel
          counts a symbol once across accounts and the cards above count each
          account's row, which is a three-point gap on a real portfolio — and
          an unexplained three-point gap between two numbers on one screen
          reads as a bug in both of them. */}
      <div className="xray-foot">
        Weights rebuilt from your holdings and transaction history.
        {!drift.ledger_backed && ' No trades are on record, so nothing here can tell a price move from a purchase you never imported.'}
        {drift.ledger_backed && drift.coverage_note && ` ${drift.coverage_note}`}
        {drift.symbols_in_two_accounts?.length > 0 &&
          ` ${drift.symbols_in_two_accounts.join(', ')} ${drift.symbols_in_two_accounts.length > 1 ? 'are' : 'is'} held in more than one account and counted once here, so these weights differ from the cards above, which weigh each account's row separately.`}
        {drift.price_coverage < 0.999 &&
          ` Covers ${pct(drift.price_coverage)} of today's invested value — the rest has no price history to rebuild.`}
        {drift.sector_coverage < 0.999 &&
          ` Sectors known for ${pct(drift.sector_coverage)} of today's holdings.`}
        {' '}Valuation and fee drift are not shown: those come from current fundamentals,
        which have no history to compare against.
      </div>
    </section>
  );
}

function XrayReport({ data, onReload, running = false, ranAt = null }) {
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
        {/* The timestamp is what makes a successful run visible at all: the
            numbers usually come back identical, so without it a working
            recompute and a broken one look the same. */}
        {ranAt && !running && (
          <span className="xray-ran-at">Updated {ranAt.toLocaleTimeString()}</span>
        )}
        <button
          className="btn btn-ghost btn-sm"
          onClick={onReload}
          disabled={running}
        >
          {running ? 'Recomputing…' : 'Recompute'}
        </button>
      </header>

      {flags.length > 0 && (
        <ul className="xray-flags">
          {flags.map((f, i) => <li key={i}>{f}</li>)}
        </ul>
      )}

      {/* Above the stat band on purpose. The bands and mixes answer "what is
          this portfolio", which for a long-term holder is the same answer
          every day; this answers "what has it become", which is the only
          question on the page whose answer changed since the last visit. */}
      <XrayDrift drift={data.drift} />

      <section className="stats-grid xray-band">
        <div className="stat-card">
          <div className="stat-label">Concentration</div>
          <FitValue className={CONCENTRATION_TONE(hhi)}>{CONCENTRATION(hhi)}</FitValue>
          <div className="stat-sub">
            {data.effective_holdings != null
              ? `${data.effective_holdings} effective of ${data.holdings_count} holdings`
              : `HHI ${hhi.toFixed(2)}`}
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Top 5 weight</div>
          <FitValue>{pct(data.top5_weight)}</FitValue>
          <div className="stat-sub">of total value</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Cash drag</div>
          <FitValue>{pct(data.cash_drag)}</FitValue>
          <div className="stat-sub">uninvested</div>
        </div>
        {fees && (
          <div className="stat-card">
            <div className="stat-label">Fund fees</div>
            <FitValue>{pctFine(fees.weighted_expense_ratio)}</FitValue>
            <div className="stat-sub">
              {fees.funds?.length ? `≈ $${Math.round(fees.annual_cost).toLocaleString()}/yr` : 'none detected'}
            </div>
          </div>
        )}
        {factor?.weighted_beta != null && (
          <div className="stat-card">
            <div className="stat-label">Beta</div>
            <FitValue>{factor.weighted_beta.toFixed(2)}</FitValue>
            <div className="stat-sub">portfolio-weighted vs market</div>
          </div>
        )}
        {factor?.weighted_dividend_yield != null && (
          <div className="stat-card">
            <div className="stat-label">Dividend yield</div>
            <FitValue>{pct(factor.weighted_dividend_yield)}</FitValue>
            <div className="stat-sub">trailing, weighted</div>
          </div>
        )}
        {benchHead && (
          <div className="stat-card">
            <div className="stat-label">vs {benchmark.symbol} · {PERIOD_LABELS[benchHead.period] || benchHead.period}</div>
            <FitValue className={benchHead.delta_pct >= 0 ? 'positive' : 'negative'}>
              {signedPct(benchHead.delta_pct)}
            </FitValue>
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
            {/* The boundary, not just the method. This measures the invested
                sleeve; the Holdings tab measures the whole portfolio with cash
                in it. On a book that is a third cash the same year reads 3.5%
                here and 2.3% there, and two screens disagreeing about one
                number reads as a bug rather than as two questions. */}
            <div className="xray-foot">
              Your holdings against the index, cash excluded — assumes today's
              positions held all period; deposits ignored. Coverage{' '}
              {pct((benchmark.periods[benchmark.periods.length - 1] || {}).coverage)}.
              The Holdings tab measures the whole portfolio, so its figure is
              this one diluted by whatever share you hold in cash.
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
