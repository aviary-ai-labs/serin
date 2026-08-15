import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api, setAuthToken } from './api.js';
import { money, signedMoney, signedPct, dateShort, setDisplayCurrency } from './format.js';
import { COMMON_CURRENCIES } from './components/Positions.jsx';
import { PortfolioTrendChart, filterHistoryByRange } from './components/Charts.jsx';
import { PositionsTable, PositionModal, ConfirmDialog, TaxLotsDrawer } from './components/Positions.jsx';
import { AllocationCard, TopHoldings, PositionInspector } from './components/Sidebar.jsx';
import { useXray, XrayTeaser, XrayView } from './components/XrayCard.jsx';
import { BriefingsView } from './components/Briefings.jsx';
import { NewsView } from './components/News.jsx';
import { ConnectionsPanel } from './components/Connections.jsx';
import { PerformanceMetrics } from './components/PerformanceMetrics.jsx';
import { StockDetail } from './components/StockDetail.jsx';
import { StockGrid } from './components/StockGrid.jsx';
import { ConnectorsView } from './components/Connectors.jsx';
import { SmartImport } from './components/SmartImport.jsx';
import { IconRefresh, IconUpload, IconDownload, IconPlus, IconLink, IconSignOut, IconX } from './components/Icons.jsx';

const TABS = [
  { id: 'overview', label: 'Overview' },
  { id: 'stocks', label: 'Stocks' },
  { id: 'briefings', label: 'Briefings' },
  { id: 'news', label: 'News' },
  { id: 'connectors', label: 'Connectors' },
];

const CSV_TEMPLATE = [
  'symbol,name,broker,asset_type,quantity,average_cost,current_price',
  'AAPL,Apple Inc,manual,stock,10,180.50,225.10',
  'VOO,Vanguard S&P 500 ETF,manual,etf,5,420.00,512.30',
  'CASH,Cash,manual,cash,2500,1,1',
].join('\n');

function BrandMark() {
  // Three ascending rounded bars (Calm Dashboard mark). Tinted via .brand-mark in CSS.
  return (
    <svg className="brand-mark" viewBox="0 0 24 24" aria-hidden="true">
      <rect x="3" y="13" width="4" height="8" rx="1.2" />
      <rect x="10" y="8" width="4" height="13" rx="1.2" />
      <rect x="17" y="3" width="4" height="18" rx="1.2" />
    </svg>
  );
}

function StatCard({ label, value, sub, subClass, valueClass }) {
  return (
    <div className="stat-card">
      <div className="stat-label">{label}</div>
      <div className={`stat-value ${valueClass || ''}`}>{value}</div>
      {sub != null && <div className={`stat-sub ${subClass || ''}`}>{sub}</div>}
    </div>
  );
}

function downloadCsvTemplate() {
  const blob = new Blob([CSV_TEMPLATE], { type: 'text/csv' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = 'serin-positions-template.csv';
  link.click();
  URL.revokeObjectURL(url);
}

const TAB_IDS = new Set(['overview', 'stocks', 'xray', 'briefings', 'news', 'connectors']);

export default function App() {
  // Tabs are deep-linkable via the URL hash (#xray, #briefings, …) so views
  // can be bookmarked and shared; unknown hashes fall back to the overview.
  const [tab, setTab] = useState(() => {
    const fromHash = window.location.hash.replace('#', '');
    return TAB_IDS.has(fromHash) ? fromHash : 'overview';
  });
  useEffect(() => {
    window.history.replaceState(null, '', tab === 'overview' ? window.location.pathname : `#${tab}`);
  }, [tab]);
  const xray = useXray(); // pack-driven: absent → no tab, no teaser, no trace
  const [config, setConfig] = useState(null);
  const [portfolio, setPortfolio] = useState(null);
  const [positions, setPositions] = useState([]);
  const [taxLots, setTaxLots] = useState([]);
  const [priceHistory, setPriceHistory] = useState({});
  const [auditReport, setAuditReport] = useState(null);
  const [briefings, setBriefings] = useState([]);
  const [briefingPreferences, setBriefingPreferences] = useState({ style: 'operator' });
  const [schedule, setSchedule] = useState(null);
  const [brokerStatus, setBrokerStatus] = useState(null);
  const [news, setNews] = useState(null);
  const [newsLoading, setNewsLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [locked, setLocked] = useState(false);
  const [account, setAccount] = useState(null); // { email } when signed in on a multiuser host

  // App lock: any 401 flips the shell into the lock screen; a successful
  // login flips it back and reloads everything.
  useEffect(() => {
    const onLocked = () => setLocked(true);
    window.addEventListener('serin:locked', onLocked);
    return () => window.removeEventListener('serin:locked', onLocked);
  }, []);

  // Who is signed in, for the header's sign-out control and the trial /
  // lapsed banners. Multiuser only — the single-user passphrase lock has no
  // account to leave and no subscription to run out.
  const probeAccount = useCallback(async () => {
    try {
      const info = await api('/api/v1/version');
      if (!info.multiuser) return;
      const me = await api('/api/auth/me');
      setAccount(me.authenticated && me.email
        ? { email: me.email, status: me.status || 'active', trialDaysLeft: me.trial_days_left ?? null }
        : null);
    } catch {
      setAccount(null);
    }
  }, []);
  useEffect(() => { probeAccount(); }, [probeAccount]);

  async function signOut() {
    try {
      await api('/api/auth/logout', { method: 'POST' });
    } catch {
      // The session cookie may already be dead; clearing local state is what matters.
    }
    // Drop the localStorage bearer mirror too, or requests keep authenticating.
    setAuthToken('');
    window.location.reload();
  }

  // One click from "trial ending" to a card on file: a fresh Stripe portal
  // session, minted server-side so no billing detail ever touches this app.
  async function openPortal() {
    try {
      const { url } = await api('/api/auth/portal', { method: 'POST' });
      if (url) window.location.assign(url);
      else throw new Error('Billing did not answer with a portal link.');
    } catch (error) {
      addToast('error', error.message);
    }
  }

  // The ending-soon banner is dismissible per browser session — nagging once
  // per visit is a reminder, nagging on every render is a hostage note.
  const [trialNoticeDismissed, setTrialNoticeDismissed] = useState(() => {
    try { return sessionStorage.getItem('serin_trial_notice') === '1'; } catch { return false; }
  });
  function dismissTrialNotice() {
    setTrialNoticeDismissed(true);
    try { sessionStorage.setItem('serin_trial_notice', '1'); } catch { /* private mode */ }
  }

  const [toasts, setToasts] = useState([]);
  const [busy, setBusy] = useState('');
  const [dateRange, setDateRange] = useState('3M');
  const [selectedBriefingId, setSelectedBriefingId] = useState(null);
  const [selectedPositionId, setSelectedPositionId] = useState(null);
  const [stockDetail, setStockDetail] = useState(null); // { symbol, assetType }
  const [modal, setModal] = useState(null); // null | { mode: 'add' } | { mode: 'edit', position }
  const [confirming, setConfirming] = useState(null); // null | { kind: 'position'|'briefing', item }
  const [taxDrawerId, setTaxDrawerId] = useState(null);
  const [showSmartImport, setShowSmartImport] = useState(false);

  const toastTimers = useRef({});

  const addToast = useCallback((type, message) => {
    const id = Math.random().toString(36).slice(2);
    setToasts(prev => [...prev.slice(-3), { id, type, message }]);
    toastTimers.current[id] = setTimeout(() => {
      setToasts(prev => prev.filter(item => item.id !== id));
      delete toastTimers.current[id];
    }, type === 'error' ? 9000 : 4500);
  }, []);

  const dismissToast = useCallback(id => {
    clearTimeout(toastTimers.current[id]);
    delete toastTimers.current[id];
    setToasts(prev => prev.filter(item => item.id !== id));
  }, []);

  const loadAll = useCallback(async () => {
    const [cfg, pf, pos, lots, audit, br, prefs, sched, broker, hist] = await Promise.all([
      api('/api/config'),
      api('/api/portfolio'),
      api('/api/positions'),
      api('/api/tax-lots'),
      api('/api/audit'),
      api('/api/briefings'),
      api('/api/briefings/preferences'),
      api('/api/schedule'),
      api('/api/broker/status').catch(() => null),
      api('/api/price-history?period=1y'),
    ]);
    setConfig(cfg);
    setDisplayCurrency(cfg.display_currency || 'USD');
    setPortfolio(pf);
    setPositions(pos);
    setTaxLots(lots);
    setAuditReport(audit);
    setBriefings(br);
    setBriefingPreferences(prefs);
    setSchedule(sched);
    setBrokerStatus(broker);
    setPriceHistory(hist.history || {});
    setLoaded(true);
  }, []);

  useEffect(() => {
    loadAll().catch(error => addToast('error', error.message));
  }, [loadAll, addToast]);

  // Poll while a briefing is running.
  useEffect(() => {
    if (!briefings.some(item => item.status === 'running')) return undefined;
    const timer = setInterval(() => {
      api('/api/briefings')
        .then(next => {
          setBriefings(prev => {
            const wasRunning = prev.some(item => item.status === 'running');
            const stillRunning = next.some(item => item.status === 'running');
            if (wasRunning && !stillRunning) {
              const latest = next[0];
              if (latest?.status === 'done') addToast('success', 'Daily briefing is ready.');
              if (latest?.status === 'error') addToast('error', `Briefing failed: ${latest.error}`);
            }
            return next;
          });
        })
        .catch(() => {});
    }, 2000);
    return () => clearInterval(timer);
  }, [briefings, addToast]);

  // While a schedule is enabled, refresh quietly so scheduled runs appear
  // without a manual reload.
  useEffect(() => {
    if (!schedule?.enabled) return undefined;
    const timer = setInterval(() => {
      Promise.all([api('/api/briefings'), api('/api/schedule')])
        .then(([nextBriefings, nextSchedule]) => {
          setBriefings(nextBriefings);
          setSchedule(nextSchedule);
        })
        .catch(() => {});
    }, 60000);
    return () => clearInterval(timer);
  }, [schedule?.enabled]);

  // Lazy-load news the first time the tab opens.
  useEffect(() => {
    if (tab === 'news' && !news && !newsLoading) refreshNews();
  }, [tab]); // eslint-disable-line react-hooks/exhaustive-deps

  async function refreshNews() {
    setNewsLoading(true);
    try {
      setNews(await api('/api/news'));
    } catch (error) {
      addToast('error', `News: ${error.message}`);
    } finally {
      setNewsLoading(false);
    }
  }

  async function submitPosition(body) {
    setBusy('position');
    try {
      const action = modal?.mode === 'edit' ? 'updated' : 'added';
      if (modal?.mode === 'edit') {
        await api(`/api/positions/${modal.position.id}`, { method: 'PUT', body: JSON.stringify(body) });
      } else {
        await api('/api/positions', { method: 'POST', body: JSON.stringify(body) });
      }
      const enriched = await enrichMarketData([body]);
      addToast('success', `${body.symbol} ${action}.${enriched?.errors?.length ? ' Market data still has gaps.' : ''}`);
      setModal(null);
      await loadAll();
    } catch (error) {
      addToast('error', error.message);
    } finally {
      setBusy('');
    }
  }

  async function confirmAction() {
    if (!confirming) return;
    const { kind, item } = confirming;
    setBusy('confirm');
    try {
      if (kind === 'position') {
        await api(`/api/positions/${item.id}`, { method: 'DELETE' });
        addToast('success', `${item.symbol} deleted.`);
      } else if (kind === 'briefing') {
        await api(`/api/briefings/${item.id}`, { method: 'DELETE' });
        addToast('success', 'Briefing deleted.');
      }
      setConfirming(null);
      await loadAll();
    } catch (error) {
      addToast('error', error.message);
    } finally {
      setBusy('');
    }
  }

  async function createTaxLot(body) {
    setBusy('tax-lot');
    try {
      await api('/api/tax-lots', { method: 'POST', body: JSON.stringify(body) });
      await loadAll();
    } catch (error) {
      addToast('error', error.message);
    } finally {
      setBusy('');
    }
  }

  async function deleteTaxLot(id) {
    setBusy(`tax-delete-${id}`);
    try {
      await api(`/api/tax-lots/${id}`, { method: 'DELETE' });
      await loadAll();
    } catch (error) {
      addToast('error', error.message);
    } finally {
      setBusy('');
    }
  }

  async function importCsv(event) {
    const file = event.target.files?.[0];
    if (!file) return;
    setBusy('csv');
    const formData = new FormData();
    formData.append('file', file);
    try {
      const result = await api('/api/import/csv?broker=csv', { method: 'POST', body: formData });
      const enriched = await enrichMarketData(result.positions || []);
      addToast(
        'success',
        `Imported ${result.imported} position${result.imported === 1 ? '' : 's'}.${enriched?.errors?.length ? ' Market data still has gaps.' : ''}`
      );
      await loadAll();
    } catch (error) {
      addToast('error', error.message);
    } finally {
      event.target.value = '';
      setBusy('');
    }
  }

  async function refreshPrices() {
    setBusy('prices');
    try {
      const result = await api('/api/prices/refresh', { method: 'POST' });
      if (result.errors?.length) {
        addToast('error', `Updated ${result.updated} positions; issues: ${result.errors.slice(0, 3).join(' · ')}`);
      } else {
        addToast('success', `Prices updated for ${result.updated} position${result.updated === 1 ? '' : 's'}.`);
      }
      // Explicit refresh forces a provider pass for history too; the regular
      // loadAll() below then serves the freshly cached data without re-fetching.
      await api('/api/price-history?period=1y&refresh=1').catch(() => null);
      await loadAll();
    } catch (error) {
      addToast('error', error.message);
    } finally {
      setBusy('');
    }
  }

  async function enrichMarketData(items) {
    const symbols = [...new Set((items || [])
      .filter(item => item && !['cash', 'option'].includes(item.asset_type))
      .map(item => String(item.symbol || '').trim().toUpperCase())
      .filter(Boolean))];
    if (!symbols.length) return null;
    try {
      return await api('/api/prices/refresh', {
        method: 'POST',
        body: JSON.stringify({ symbols }),
      });
    } catch {
      return { updated: 0, symbols: [], errors: ['Market data refresh failed'] };
    }
  }

  async function refreshBrokerStatus() {
    try {
      setBrokerStatus(await api('/api/broker/status'));
    } catch {
      // status is best-effort; the panel handles a null gracefully.
    }
  }

  async function connectBroker() {
    setBusy('broker-connect');
    try {
      const { redirect_uri } = await api('/api/broker/connect', { method: 'POST', body: JSON.stringify({}) });
      window.open(redirect_uri, '_blank', 'noopener,noreferrer');
      addToast('success', 'Connect your brokerage in the new tab, then return and click “Sync now”.');
      await refreshBrokerStatus();
    } catch (error) {
      addToast('error', error.message);
    } finally {
      setBusy('');
    }
  }

  async function syncBroker() {
    setBusy('broker-sync');
    try {
      const result = await api('/api/broker/sync', { method: 'POST' });
      const priced = result.repriced ? `, ${result.repriced} repriced` : '';
      addToast('success', `Synced ${result.positions} position${result.positions === 1 ? '' : 's'} from ${result.accounts} account${result.accounts === 1 ? '' : 's'}${priced}.`);
      await Promise.all([loadAll(), refreshBrokerStatus()]);
    } catch (error) {
      addToast('error', error.message);
    } finally {
      setBusy('');
    }
  }

  async function backfillBroker() {
    setBusy('broker-backfill');
    try {
      const result = await api('/api/broker/backfill', { method: 'POST', body: JSON.stringify({ days: 365 }) });
      const skipped = result.skipped_existing ? ` · ${result.skipped_existing} already imported` : '';
      addToast('success', `Imported ${result.imported} transaction${result.imported === 1 ? '' : 's'} from broker history${skipped}.`);
      await loadAll();
    } catch (error) {
      addToast('error', error.message);
    } finally {
      setBusy('');
    }
  }

  async function disconnectBroker(connection) {
    setBusy(`broker-disconnect-${connection.id}`);
    try {
      await api(`/api/broker/connections/${connection.id}`, { method: 'DELETE' });
      addToast('success', `${connection.institution} disconnected.`);
      await Promise.all([loadAll(), refreshBrokerStatus()]);
    } catch (error) {
      addToast('error', error.message);
    } finally {
      setBusy('');
    }
  }

  async function saveSchedule(form) {
    setBusy('schedule');
    try {
      const saved = await api('/api/schedule', { method: 'PUT', body: JSON.stringify(form) });
      setSchedule(saved);
      addToast('success', saved.enabled
        ? `Morning briefing scheduled for ${saved.time}${saved.timezone === 'local' ? '' : ` (${saved.timezone})`}.`
        : 'Scheduled briefing turned off.');
    } catch (error) {
      addToast('error', error.message);
    } finally {
      setBusy('');
    }
  }

  async function saveBriefingPreferences(preferences) {
    setBriefingPreferences(preferences);
    setBusy('briefing-preferences');
    try {
      const saved = await api('/api/briefings/preferences', { method: 'PUT', body: JSON.stringify(preferences) });
      setBriefingPreferences(saved);
    } catch (error) {
      addToast('error', error.message);
    } finally {
      setBusy('');
    }
  }

  async function emailBriefing(briefing) {
    setBusy(`email-${briefing.id}`);
    try {
      const result = await api(`/api/briefings/${briefing.id}/email`, { method: 'POST' });
      addToast('success', `Briefing emailed to ${result.to}.`);
      setBriefings(await api('/api/briefings'));
    } catch (error) {
      addToast('error', error.message);
    } finally {
      setBusy('');
    }
  }

  async function runBriefing(style = briefingPreferences.style) {
    setBusy('briefing');
    try {
      const result = await api('/api/briefings/run', { method: 'POST', body: JSON.stringify({ style }) });
      setSelectedBriefingId(result.briefing_id);
      setBriefings(await api('/api/briefings'));
    } catch (error) {
      addToast('error', error.message);
    } finally {
      setBusy('');
    }
  }

  const dayChange = useMemo(() => {
    let change = 0;
    let prevTotal = 0;
    let tracked = 0;
    positions.forEach(position => {
      if (['cash', 'option'].includes(position.asset_type)) return;
      const closes = priceHistory[position.symbol]?.closes;
      if (!closes || closes.length < 2) return;
      const last = closes[closes.length - 1];
      const prev = closes[closes.length - 2];
      change += (last - prev) * position.quantity;
      prevTotal += prev * position.quantity;
      tracked += 1;
    });
    if (!tracked) return null;
    return { value: change, pct: prevTotal > 0 ? (change / prevTotal) * 100 : 0, tracked };
  }, [positions, priceHistory]);

  const selectedPosition = useMemo(
    () => positions.find(position => position.id === selectedPositionId)
      || positions.find(position => position.asset_type !== 'cash')
      || positions[0],
    [positions, selectedPositionId],
  );

  const taxDrawerPosition = useMemo(
    () => positions.find(position => position.id === taxDrawerId) || null,
    [positions, taxDrawerId],
  );

  const drawerLots = useMemo(() => {
    if (!taxDrawerPosition) return [];
    return taxLots.filter(lot => lot.symbol === taxDrawerPosition.symbol && lot.broker === taxDrawerPosition.broker);
  }, [taxLots, taxDrawerPosition]);

  const runningBriefing = briefings.some(item => item.status === 'running');
  const gainTone = (portfolio?.total_gain || 0) >= 0 ? 'positive' : 'negative';
  const hasPositions = positions.length > 0;

  if (locked) {
    return (
      <LockScreen
        onUnlocked={async () => {
          setLocked(false);
          probeAccount();
          try {
            await loadAll();
          } catch (error) {
            addToast('error', error.message);
          }
        }}
      />
    );
  }

  const trialEndsSoon = account?.status === 'trialing'
    && account.trialDaysLeft != null && account.trialDaysLeft <= 3;

  return (
    <div className="app-container">
      {trialEndsSoon && !trialNoticeDismissed && (
        <div className="notice-banner trial" role="status">
          <span>
            Your free trial ends {account.trialDaysLeft <= 1 ? 'today' : `in ${account.trialDaysLeft} days`} —
            subscribe to keep AI briefings, live prices and imports. Your data stays yours either way.
          </span>
          <div className="notice-actions">
            <button className="btn btn-primary" onClick={openPortal}>Continue with Serin · $8/mo</button>
            <button className="icon-btn" aria-label="Dismiss for this visit" onClick={dismissTrialNotice}>
              <IconX size={15} />
            </button>
          </div>
        </div>
      )}
      {account?.status === 'lapsed' && (
        <div className="notice-banner lapsed" role="status">
          <span>
            Your trial has ended, so briefings, price updates and imports are paused.
            Your portfolio is intact — browse it, export it, or pick up where you left off.
          </span>
          <div className="notice-actions">
            <button className="btn btn-primary" onClick={openPortal}>Continue with Serin · $8/mo</button>
            <a className="btn" href="/api/backup/positions.csv">Download CSV</a>
          </div>
        </div>
      )}
      <header className="header">
        <div className="header-left">
          <h1 className="brand"><BrandMark /><span>serin</span></h1>
          <span className={`subtitle prices-as-of ${busy === 'prices' ? 'is-refreshing' : ''}`}>
            {busy === 'prices'
              ? '· refreshing prices…'
              : portfolio?.last_refresh
                ? `· ${dateShort(portfolio.last_refresh)}`
                // "local" is the self-host privacy brag; a signed-in Cloud
                // account is by definition not local.
                : account ? '· portfolio intelligence' : '· local portfolio intelligence'}
          </span>
        </div>
        <nav className="tab-nav" aria-label="Sections">
          {TABS.flatMap(item => (
            // The X-ray tab slots in after Stocks, and only when the pack answers.
            item.id === 'briefings' && xray.status === 'ok'
              ? [{ id: 'xray', label: 'X-ray' }, item]
              : [item]
          )).map(item => (
            <button
              key={item.id}
              className={`${tab === item.id ? 'active' : ''} ${item.id === 'connectors' ? 'hero' : ''}`}
              onClick={() => setTab(item.id)}
            >
              {item.label}
              {item.id === 'overview' && hasPositions && <span className="tab-count">{positions.length}</span>}
              {item.id === 'xray' && xray.data?.entitled && (xray.data.flags?.length || 0) > 0 && (
                <span className="tab-count warn">{xray.data.flags.length}</span>
              )}
              {item.id === 'briefings' && runningBriefing && <span className="runningdot">●</span>}
            </button>
          ))}
        </nav>
        <div className="header-actions">
          <select
            className="currency-select"
            title="Display currency — totals convert to this"
            aria-label="Display currency"
            value={config?.display_currency || 'USD'}
            onChange={async event => {
              const currency = event.target.value;
              try {
                await api('/api/settings/display-currency', { method: 'PUT', body: JSON.stringify({ currency }) });
                setDisplayCurrency(currency);
                await loadAll();
              } catch (error) {
                addToast('error', error.message);
              }
            }}
          >
            {COMMON_CURRENCIES.map(code => <option key={code} value={code}>{code}</option>)}
          </select>
          <button className="btn" onClick={refreshPrices} disabled={busy === 'prices' || !hasPositions}>
            <IconRefresh /> {busy === 'prices' ? 'Refreshing…' : 'Refresh'}
          </button>
          <button className="btn" onClick={() => setShowSmartImport(true)}>
            <IconUpload /> Smart Import
          </button>
          <button className="btn btn-primary" onClick={() => setModal({ mode: 'add' })}><IconPlus /> Add position</button>
          {account?.status === 'trialing' && account.trialDaysLeft != null && (
            <button
              className="trial-chip"
              onClick={openPortal}
              title="You have the full product during the trial. Click to add a payment method and continue after it ends."
            >
              Trial · {account.trialDaysLeft}d left
            </button>
          )}
          {account && (
            <button
              className="icon-btn"
              onClick={signOut}
              title={`Signed in as ${account.email} — sign out`}
              aria-label={`Sign out (${account.email})`}
            >
              <IconSignOut size={16} />
            </button>
          )}
        </div>
      </header>

      {tab === 'overview' && (
        <>
          <section className="stats-grid">
            <StatCard label="Total Value" value={money(portfolio?.total_value)} sub={`${positions.length} positions · ${money(portfolio?.cash_value)} cash`} />
            <StatCard
              label="Total Gain"
              value={signedMoney(portfolio?.total_gain)}
              valueClass={gainTone}
              sub={signedPct(portfolio?.total_gain_pct)}
              subClass={gainTone}
            />
            <StatCard
              label="Day Change"
              value={dayChange ? signedMoney(dayChange.value) : '—'}
              valueClass={dayChange ? (dayChange.value >= 0 ? 'positive' : 'negative') : ''}
              sub={dayChange ? `${signedPct(dayChange.pct)} · ${dayChange.tracked} tracked` : 'refresh prices to track'}
              subClass={dayChange ? (dayChange.value >= 0 ? 'positive' : 'negative') : ''}
            />
            <StatCard label="Invested Capital" value={money(portfolio?.total_cost)} sub={`cost basis ex-cash`} />
          </section>

          {hasPositions ? (
            <>
              <PortfolioTrendChart
                positions={positions}
                priceHistory={priceHistory}
                dateRange={dateRange}
                onRangeChange={setDateRange}
              />
              <div className="overview-grid">
                <div className="main-col">
                  <section className="panel">
                    <PositionsTable
                      positions={positions}
                      priceHistory={priceHistory}
                      dateRange={dateRange}
                      taxLots={taxLots}
                      selectedId={selectedPosition?.id}
                      onSelect={setSelectedPositionId}
                      onEdit={position => setModal({ mode: 'edit', position })}
                      onDelete={position => setConfirming({ kind: 'position', item: position })}
                      onOpenTaxLots={position => setTaxDrawerId(position.id)}
                    />
                  </section>
                </div>
                <div className="sidebar-col">
                  <PositionInspector
                    position={selectedPosition}
                    history={selectedPosition ? filterHistoryByRange(priceHistory[selectedPosition.symbol], dateRange) : null}
                    audit={auditReport}
                    onEdit={position => setModal({ mode: 'edit', position })}
                    onOpenTaxLots={position => setTaxDrawerId(position.id)}
                  />
                  <AllocationCard portfolio={portfolio} />
                  <TopHoldings positions={positions} total={portfolio?.total_value || 0} onSelect={setSelectedPositionId} />
                  <XrayTeaser xray={xray} onOpen={() => setTab('xray')} />
                  {(config?.snaptrade_configured || (brokerStatus?.connections?.length > 0)) && (
                    <ConnectionsPanel
                      status={brokerStatus}
                      configured={Boolean(config?.snaptrade_configured)}
                      busy={busy}
                      onConnect={connectBroker}
                      onSync={syncBroker}
                      onBackfill={backfillBroker}
                      onDisconnect={disconnectBroker}
                    />
                  )}
                </div>
              </div>
            </>
          ) : loaded ? (
            <section className="panel">
              <div className="onboarding">
                <h3>Welcome to Serin</h3>
                <p>
                  Track every brokerage in one private dashboard, then let the AI briefing keep watch.
                  Start by importing a CSV from your broker or adding a position by hand.
                </p>
                <div className="onboarding-actions">
                  {config?.snaptrade_configured && (
                    <button className="btn btn-primary" disabled={busy === 'broker-connect'} onClick={connectBroker}>
                      <IconLink /> {busy === 'broker-connect' ? 'Opening…' : 'Connect a brokerage'}
                    </button>
                  )}
                  <button className={config?.snaptrade_configured ? 'btn' : 'btn btn-primary'} onClick={() => setModal({ mode: 'add' })}><IconPlus /> Add position</button>
                  <label className="btn file-btn">
                    <IconUpload /> Import CSV
                    <input type="file" accept=".csv,text/csv" onChange={importCsv} disabled={busy === 'csv'} />
                  </label>
                  <button className="btn btn-ghost" onClick={downloadCsvTemplate}><IconDownload /> CSV template</button>
                </div>
              </div>
            </section>
          ) : (
            <section className="panel"><div className="empty-box">Loading portfolio…</div></section>
          )}
        </>
      )}

      {tab === 'xray' && <XrayView xray={xray} />}

      {tab === 'stocks' && (
        <section className="stocks-tab">
          {stockDetail ? (
            <StockDetail
              symbol={stockDetail.symbol}
              assetType={stockDetail.assetType}
              onClose={() => setStockDetail(null)}
            />
          ) : (
            <>
              <PerformanceMetrics refreshKey={portfolio?.last_refresh || 0} />
              <StockGrid
                positions={positions}
                priceHistory={priceHistory}
                dateRange={dateRange}
                totalValue={portfolio?.total_value || 0}
                onSelect={position => setStockDetail({ symbol: position.symbol, assetType: position.asset_type })}
              />
            </>
          )}
        </section>
      )}

      {tab === 'briefings' && (
        <BriefingsView
          config={config}
          briefings={briefings}
          selectedId={selectedBriefingId}
          onSelectBriefing={setSelectedBriefingId}
          onRun={runBriefing}
          onDelete={briefing => setConfirming({ kind: 'briefing', item: briefing })}
          onEmail={emailBriefing}
          busy={busy}
          preferences={briefingPreferences}
          onSavePreferences={saveBriefingPreferences}
          schedule={schedule}
          onSaveSchedule={saveSchedule}
        />
      )}

      {tab === 'news' && (
        <NewsView news={news} loading={newsLoading} onRefresh={refreshNews} />
      )}

      {tab === 'connectors' && (
        <ConnectorsView
          addToast={addToast}
          onChanged={() => loadAll().catch(error => addToast('error', error.message))}
        />
      )}

      <footer className="footer-note">
        <span>Serin · AI portfolio intelligence</span>
        <span>Context &amp; organization — not investment advice</span>
      </footer>

      {modal && (
        <PositionModal
          editing={modal.mode === 'edit' ? modal.position : null}
          busy={busy === 'position'}
          onClose={() => setModal(null)}
          onSubmit={submitPosition}
        />
      )}

      {confirming && (
        <ConfirmDialog
          title={confirming.kind === 'position' ? 'Delete position' : 'Delete briefing'}
          body={confirming.kind === 'position'
            ? <>Delete <strong>{confirming.item.symbol}</strong> ({confirming.item.broker})? This removes it from your portfolio and can't be undone.</>
            : <>Delete the briefing from <strong>{dateShort(confirming.item.created_at)}</strong>? This can't be undone.</>}
          confirmLabel="Delete"
          busy={busy === 'confirm'}
          onConfirm={confirmAction}
          onCancel={() => setConfirming(null)}
        />
      )}

      {taxDrawerPosition && (
        <TaxLotsDrawer
          position={taxDrawerPosition}
          lots={drawerLots}
          busy={busy}
          onClose={() => setTaxDrawerId(null)}
          onCreate={createTaxLot}
          onDelete={deleteTaxLot}
        />
      )}

      {showSmartImport && (
        <SmartImport
          onClose={() => setShowSmartImport(false)}
          onImported={loadAll}
          addToast={addToast}
          brokers={[...new Set(positions.map(p => p.broker).filter(Boolean))]}
        />
      )}

      {toasts.length > 0 && (
        <div className="toast-stack">
          {toasts.map(toast => (
            <button key={toast.id} className={`toast ${toast.type}`} onClick={() => dismissToast(toast.id)}>
              {toast.message}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

// Captured at module load, before React mounts and before the app shell's
// own hash routing can consume the fragment. The Google callback delivers
// its failure reason this way, and the lock screen only mounts after the
// first 401 — by which point the hash is long gone.
const AUTH_ERROR_ON_LOAD = (() => {
  const match = /[#&]auth_error=([^&]+)/.exec(window.location.hash || '');
  if (!match) return '';
  window.history.replaceState(null, '', window.location.pathname);
  return decodeURIComponent(match[1]).replace(/\+/g, ' ');
})();

function LockScreen({ onUnlocked }) {
  const [password, setPassword] = useState('');
  const [email, setEmail] = useState('');
  const [error, setError] = useState(AUTH_ERROR_ON_LOAD);
  const [busy, setBusy] = useState(false);
  // null until the probe answers, so we never flash the wrong form.
  const [multiuser, setMultiuser] = useState(null);
  // A setup link (#setup=…) means the account exists but has no password yet —
  // the customer is arriving from the welcome email after paying.
  const [setupToken, setSetupToken] = useState('');
  // A reset link works like a setup link: prove the email, choose a password.
  const [resetToken, setResetToken] = useState('');
  const [notice, setNotice] = useState('');
  // What the identity layer offers beyond passwords. Defaults off, so a
  // self-host build without the pack shows exactly the screen it always did.
  const [authOpts, setAuthOpts] = useState({ signup_open: false, google: false, trial_signup: false });
  const [mode, setMode] = useState('signin');
  const [emailFormOpen, setEmailFormOpen] = useState(false);

  useEffect(() => {
    const match = /[#&]setup=([^&]+)/.exec(window.location.hash || '');
    if (match) setSetupToken(decodeURIComponent(match[1]));
    const reset = /[#&]reset=([^&]+)/.exec(window.location.hash || '');
    if (reset) setResetToken(decodeURIComponent(reset[1]));
    api('/api/v1/version')
      .then(info => {
        setMultiuser(Boolean(info.multiuser));
        if (info.multiuser) {
          api('/api/auth/me')
            .then(me => setAuthOpts({
              signup_open: Boolean(me.signup_open),
              google: Boolean(me.google),
              trial_signup: Boolean(me.trial_signup),
            }))
            .catch(() => {});
        }
      })
      .catch(() => setMultiuser(false));
  }, []);

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setError('');
    try {
      let result;
      if (resetToken) {
        result = await api('/api/auth/reset', {
          method: 'POST',
          body: JSON.stringify({ token: resetToken, password }),
        });
        window.history.replaceState(null, '', window.location.pathname);
      } else if (setupToken) {
        result = await api('/api/auth/setup', {
          method: 'POST',
          body: JSON.stringify({ token: setupToken, password }),
        });
        // Drop the token from the URL so a shared link or back button can't
        // replay it, and so a refresh doesn't reopen the setup form.
        window.history.replaceState(null, '', window.location.pathname);
      } else if (multiuser && mode === 'signup') {
        result = await api('/api/auth/register', {
          method: 'POST',
          body: JSON.stringify({ email, password }),
        });
      } else if (multiuser) {
        result = await api('/api/auth/login', {
          method: 'POST',
          body: JSON.stringify({ email, password }),
        });
      } else {
        result = await api('/api/auth/login', {
          method: 'POST',
          body: JSON.stringify({ password }),
        });
      }
      if (result.token) setAuthToken(result.token);
      onUnlocked?.();
    } catch (err) {
      setError(err.message || ((setupToken || resetToken) ? 'That link has expired.' : 'Wrong details.'));
    } finally {
      setBusy(false);
    }
  }

  if (multiuser === null) return <div className="lock-screen" />;

  const chooseNew = Boolean(setupToken || resetToken);
  // When Google leads, the password form starts collapsed: on a hosted
  // instance it mostly serves checkout customers whose email isn't a Google
  // account, and shown by default it reads as a signup form that isn't one.
  const googleFirst = multiuser && !chooseNew && authOpts.google;
  const showEmailForm = !googleFirst || emailFormOpen;
  const hint = resetToken
    ? 'Choose a new password for your Serin account.'
    : setupToken
      ? 'Welcome to Serin. Choose a password to finish setting up your account.'
      : multiuser
        ? (mode === 'signup' ? 'Create your Serin account.' : 'Sign in to your Serin account.')
        : 'This Serin instance is private. Enter its passphrase to continue.';

  async function forgot() {
    setError('');
    if (!email) { setError('Enter your email first.'); return; }
    setBusy(true);
    try {
      await api('/api/auth/forgot', { method: 'POST', body: JSON.stringify({ email }) });
      // Deliberately unconditional — the server won't say whether the address
      // has an account, and neither should this.
      setNotice('If that address has an account, a reset link is on its way.');
    } catch (err) {
      setError(err.message || 'Could not start a reset.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="lock-screen">
      <form className="lock-card" onSubmit={submit}>
        <h1 className="brand"><BrandMark /><span>serin</span></h1>
        <p className="lock-hint">{hint}</p>
        {multiuser && !chooseNew && authOpts.google && (
          <>
            <a className="lock-google" href="/api/auth/google/start">
              <svg viewBox="0 0 48 48" aria-hidden="true">
                <path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"/>
                <path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"/>
                <path fill="#FBBC05" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z"/>
                <path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"/>
              </svg>
              Continue with Google
            </a>
            {authOpts.trial_signup && (
              <p className="lock-trial-hint">
                New to Serin? That button also starts your free 7-day trial — no card needed.
              </p>
            )}
          </>
        )}
        {error && <div className="lock-error">{error}</div>}
        {notice && <div className="lock-note">{notice}</div>}
        {!showEmailForm ? (
          // Google leads; passwords exist for customers whose checkout email
          // isn't a Google account, so the form is a click away, not gone.
          <button
            type="button"
            className="lock-link"
            onClick={() => setEmailFormOpen(true)}
          >
            Sign in with email and password instead
          </button>
        ) : (
          <>
            {googleFirst && <div className="lock-or">or</div>}
            {multiuser && !chooseNew && (
              <input
                type="email"
                autoFocus
                value={email}
                placeholder="Email"
                aria-label="Email"
                autoComplete="username"
                onChange={event => setEmail(event.target.value)}
              />
            )}
            <input
              type="password"
              autoFocus={!multiuser || chooseNew}
              value={password}
              placeholder={chooseNew ? 'Choose a password' : multiuser ? (mode === 'signup' ? 'Choose a password (10+ characters)' : 'Password') : 'Passphrase'}
              aria-label={chooseNew ? 'Choose a password' : 'Password'}
              autoComplete={chooseNew || mode === 'signup' ? 'new-password' : 'current-password'}
              onChange={event => setPassword(event.target.value)}
            />
            <button
              className="btn btn-primary"
              type="submit"
              disabled={busy || !password || (multiuser && !chooseNew && !email)}
            >
              {busy ? 'Working…' : chooseNew ? 'Set password' : multiuser ? (mode === 'signup' ? 'Create account' : 'Sign in') : 'Unlock'}
            </button>
            {multiuser && !chooseNew && mode === 'signin' && (
              <button type="button" className="lock-link" onClick={forgot} disabled={busy}>
                Forgot your password?
              </button>
            )}
            {multiuser && !chooseNew && mode === 'signin' && !authOpts.signup_open && (
              // Email registration exists — it runs through the free-trial
              // checkout, whose emailed setup link doubles as verification.
              // Without this pointer the form reads as sign-in-only.
              <a className="lock-link" href="/#pricing">
                New here? Start a free trial with any email →
              </a>
            )}
          </>
        )}
        {multiuser && !chooseNew && authOpts.signup_open && (
          <button
            type="button"
            className="lock-link"
            disabled={busy}
            onClick={() => { setMode(mode === 'signin' ? 'signup' : 'signin'); setError(''); setNotice(''); }}
          >
            {mode === 'signin' ? 'New here? Create an account' : 'Already have an account? Sign in'}
          </button>
        )}
        {/* Strangers reach this screen from the public site's CTA with no
            credentials and no way out but the back button. Deep-link the
            pricing section, not "/" — the bare landing reads as a dead end
            when what a newcomer wants is the way in. */}
        <a className="lock-out" href="/#pricing">New here? What Serin is, and the free trial →</a>
      </form>
    </div>
  );
}
