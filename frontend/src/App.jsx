import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api, setAuthToken } from './api.js';
import { money, signedMoney, signedPct, dateShort, setDisplayCurrency } from './format.js';
import { COMMON_CURRENCIES } from './components/Positions.jsx';
import { PortfolioTrendChart, filterHistoryByRange } from './components/Charts.jsx';
import { PositionsTable, PositionModal, ConfirmDialog, TaxLotsDrawer, dayChangeFor } from './components/Positions.jsx';
import { AllocationCard, TopHoldings, PositionInspector } from './components/Sidebar.jsx';
import { useXray, XrayTeaser, XrayView } from './components/XrayCard.jsx';
import { BriefingsView } from './components/Briefings.jsx';
import { NewsView } from './components/News.jsx';
import { Brokerages } from './components/Brokerages.jsx';
import { PerformanceMetrics } from './components/PerformanceMetrics.jsx';
import { TransactionsView } from './components/Transactions.jsx';
import { DataGaps, ActionHub } from './components/DataGaps.jsx';
import { CostBasisForm } from './components/CostBasisForm.jsx';
import { primeStockChartCache } from './components/StockChart.jsx';
import { StockDetail } from './components/StockDetail.jsx';
import { StockGrid } from './components/StockGrid.jsx';
import { ConnectorsView } from './components/Connectors.jsx';
import { AgentAccess } from './components/AgentAccess.jsx';
import { useChat, ChatView } from './components/ChatPanel.jsx';
import { SmartImport } from './components/SmartImport.jsx';
import { IconRefresh, IconUpload, IconDownload, IconPlus, IconLink, IconSignOut, IconX, IconBell, IconMore } from './components/Icons.jsx';
import { SerinBird } from './components/SerinBird.jsx';

const TABS = [
  { id: 'overview', label: 'Overview' },
  { id: 'stocks', label: 'Holdings' },
  { id: 'transactions', label: 'Transactions' },
  { id: 'briefings', label: 'Briefing' },
  { id: 'news', label: 'News' },
  { id: 'brokerages', label: 'Brokerages' },
  { id: 'connectors', label: 'Connectors' },
];

const CSV_TEMPLATE = [
  'symbol,name,broker,asset_type,quantity,average_cost,current_price',
  'AAPL,Apple Inc,manual,stock,10,180.50,225.10',
  'VOO,Vanguard S&P 500 ETF,manual,etf,5,420.00,512.30',
  'CASH,Cash,manual,cash,2500,1,1',
].join('\n');

function BrandMark() { return <SerinBird className="brand-bird" />; }

function StatCard({ label, value, sub, meta, subClass, valueClass, bird = false }) {
  return (
    <div className={`stat-card ${bird ? 'has-bird' : ''}`}>
      <div className="stat-label">{label}</div>
      <div className={`stat-value ${valueClass || ''}`}>{value}</div>
      {sub != null && <div className={`stat-sub ${subClass || ''}`}>{sub}</div>}
      {meta != null && <div className="stat-meta">{meta}</div>}
      {bird && <SerinBird className="card-bird" />}
    </div>
  );
}

/**
 * The transaction-aware return, for the headline.
 *
 * The overview used to lead with unrealized gain — market value minus cost
 * basis on today's holdings. That number cannot include a single closed trade,
 * so an active trader comparing Serin against their broker's year-to-date saw
 * two figures that could never agree and reasonably concluded Serin was wrong.
 * This is the comparable one: it rewinds actual holdings through the ledger.
 *
 * Failing quietly is deliberate. It needs transaction history, and a portfolio
 * typed in by hand has none — the card falls back to unrealized gain rather
 * than showing a gap where a number belongs.
 */
function usePortfolioReturn(refreshKey) {
  const [state, setState] = useState(null);
  useEffect(() => {
    let alive = true;
    api('/api/v1/portfolio-history')
      .then(data => { if (alive) setState(data); })
      .catch(() => { if (alive) setState(null); });
    return () => { alive = false; };
  }, [refreshKey]);
  return state;
}

/**
 * The controls that used to ride the header on every tab.
 *
 * Currency, Smart Import and the CSV template are onboarding tools and
 * occasional settings — useful, and not what anyone opens the app for. Four
 * full-width buttons on every screen made the first two inches of the page a
 * toolbar, which is a lot of room to spend on things a person touches twice.
 */
function HeaderMenu({ currency, onCurrency, onSmartImport }) {
  const [open, setOpen] = useState(false);
  const box = useRef(null);

  useEffect(() => {
    if (!open) return undefined;
    const away = event => { if (!box.current?.contains(event.target)) setOpen(false); };
    const esc = event => { if (event.key === 'Escape') setOpen(false); };
    document.addEventListener('mousedown', away);
    document.addEventListener('keydown', esc);
    return () => {
      document.removeEventListener('mousedown', away);
      document.removeEventListener('keydown', esc);
    };
  }, [open]);

  return (
    <div className="header-menu" ref={box}>
      <button type="button" className="icon-btn" onClick={() => setOpen(!open)}
              aria-haspopup="menu" aria-expanded={open} aria-label="More actions"
              title="More">
        <IconMore />
      </button>
      {open && (
        <div className="header-menu-pop" role="menu">
          <button type="button" role="menuitem"
                  onClick={() => { onSmartImport(); setOpen(false); }}>
            <IconUpload /> Smart Import
          </button>
          <button type="button" role="menuitem"
                  onClick={() => { downloadCsvTemplate(); setOpen(false); }}>
            <IconDownload /> CSV template
          </button>
          <label className="header-menu-currency">
            <span>Display currency</span>
            <select value={currency} onChange={event => onCurrency(event.target.value)}>
              {COMMON_CURRENCIES.map(code => <option key={code} value={code}>{code}</option>)}
            </select>
          </label>
        </div>
      )}
    </div>
  );
}

/**
 * What to upload, for a gap that is closed by uploading something.
 *
 * The card already says what is wrong. Its button used to answer "where do I
 * go" and not "what do I do", which left the reader on the Transactions tab in
 * front of 984 rows with no indication which of them it meant.
 */
function briefFor(gap) {
  const broker = (gap.broker || 'your broker').replace(/^\w/, c => c.toUpperCase());
  const symbols = (gap.symbols || []).slice(0, 6).join(', ')
    + ((gap.symbols || []).length > 6 ? ' and others' : '');
  if (gap.code === 'sales_without_purchase') {
    return {
      title: `Upload a ${broker} statement from before ${gap.since}`,
      detail: `${symbols} were sold from shares bought before your ${broker} history `
        + 'starts. The purchase is what turns those proceeds into a gain.',
      steps: [
        `Open ${broker} and find its account activity or statement export.`,
        `Choose a date range that ends on or before ${gap.since}.`,
        'Download it as CSV or PDF, then drop it below.',
      ],
    };
  }
  if (gap.code === 'transferred_without_cost') {
    return {
      title: 'Upload the history of the account these shares came from',
      detail: `${symbols} arrived in ${broker} by transfer, so ${broker} never `
        + 'recorded what they cost. The account that sent them did.',
      steps: [
        'Open the stock-plan or sending brokerage account.',
        'Export its transaction or benefit history.',
        'Drop it below — or close this and use "Record what these shares cost".',
      ],
    };
  }
  return null;
}

function NavIcon({ id }) {
  const common = { fill: 'none', stroke: 'currentColor', strokeWidth: 1.8, strokeLinecap: 'round', strokeLinejoin: 'round' };
  return (
    <svg className="sidebar-nav-icon" viewBox="0 0 24 24" aria-hidden="true" {...common}>
      {id === 'overview' && <><path d="m3 11 9-8 9 8" /><path d="M5.5 9.5V21h13V9.5M9.5 21v-7h5v7" /></>}
      {id === 'actions' && <><path d="M9 11.5 11 13.5 15.5 9" /><path d="M20 12a8 8 0 1 1-8-8" /><path d="M16.5 3.5 20 5l1.5 3.5" /></>}
      {id === 'stocks' && <><path d="M5 20V12M12 20V5M19 20V9" /><path d="M3 20h18" /></>}
      {id === 'xray' && <><circle cx="12" cy="12" r="8.5" /><circle cx="12" cy="12" r="3.8" /><path d="M12 2v2M22 12h-2M12 22v-2M2 12h2" /></>}
      {id === 'transactions' && <><rect x="3.5" y="4" width="17" height="16" rx="2" /><path d="M7 9h6M7 13h10M7 17h4" /><path d="M17 8.5v3M15.5 10h3" /></>}
      {id === 'briefings' && <><path d="M6 3h9l4 4v14H6z" /><path d="M15 3v5h4M9 12h7M9 16h7" /></>}
      {id === 'news' && <><rect x="3" y="5" width="18" height="15" rx="2" /><path d="M7 9h4v4H7zM14 9h3M14 12h3M7 16h10" /></>}
      {/* A bank front: the brokerage itself, distinct from the plug that
          means 'data connector'. */}
      {id === 'brokerages' && <><path d="M3 9.5 12 4l9 5.5" /><path d="M5 10v8M9.5 10v8M14.5 10v8M19 10v8" /><path d="M3 21h18" /></>}
      {id === 'connectors' && <><path d="M8 3v6M16 3v6M6 9h12v3a6 6 0 0 1-6 6v3M4 9h16" /></>}
    </svg>
  );
}

const PAGE_META = {
  overview: ['Good morning', 'A clear view across every account.'],
  stocks: ['Holdings', 'Every position, across every account.'],
  xray: ['Portfolio X-ray', 'Concentration, exposure, and risk in one diagnostic view.'],
  transactions: ['Transactions', 'The ledger your returns are built from.'],
  briefings: ['Daily Briefing', 'What changed, what matters, and what to review.'],
  news: ['News', 'Headlines that intersect with your holdings.'],
  brokerages: ['Brokerages', 'Connect an account and keep it in sync.'],
  actions: ['Actions', 'What Serin cannot work out on its own, and what would fix it.'],
  chat: ['Chat', 'Ask about your portfolio. Context, never trade directives.'],
  connectors: ['Connectors', 'The data layer behind your portfolio.'],
};

function downloadCsvTemplate() {
  const blob = new Blob([CSV_TEMPLATE], { type: 'text/csv' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = 'serin-positions-template.csv';
  link.click();
  URL.revokeObjectURL(url);
}

/* Derived from TABS rather than restated, because a hand-maintained copy
   drifts silently: this list had already lost `brokerages`, so #brokerages
   bounced to the overview and nobody noticed. `xray` is not in TABS — it is
   pack-driven and appears only when the pack is installed — but it is still a
   valid deep link when it is there. */
const TAB_IDS = new Set([...TABS.map(entry => entry.id), 'xray', 'chat', 'actions']);

export default function App() {
  // Tabs are deep-linkable via the URL hash (#xray, #briefings, …) so views
  // can be bookmarked and shared; unknown hashes fall back to the overview.
  const [tab, setTab] = useState(() => {
    const fromHash = window.location.hash.replace('#', '');
    return TAB_IDS.has(fromHash) ? fromHash : 'overview';
  });
  // Each tab switch is a history entry, so Back returns to the tab you came
  // from. This used to replaceState unconditionally: the URL tracked the tab
  // correctly but there was only ever one entry to go back from, so Back out
  // of Holdings left the app entirely and landed on the marketing page.
  //
  // The first sync still replaces — on load the URL already describes the tab,
  // and pushing there would add an entry whose Back goes nowhere visible.
  // The transferred-shares gap opens a form rather than switching tabs; the
  // key forces the panel to re-ask once a cost is recorded, so a gap that has
  // just been closed does not sit there telling the reader to close it.
  const [costGap, setCostGap] = useState(null);
  // What the reader is here to upload, when Smart Import was opened from a gap
  // card rather than from the menu.
  const [importBrief, setImportBrief] = useState(null);
  const openImportFor = useCallback(gap => {
    setImportBrief(briefFor(gap));
    setShowSmartImport(true);
  }, []);
  const [gapsKey, setGapsKey] = useState(0);
  // Drives the nav badge, and whether the tab appears at all. A permanent
  // "Actions (0)" is furniture; the point of the hub is that it is empty
  // once the work is done.
  const [openActions, setOpenActions] = useState(0);
  useEffect(() => {
    let alive = true;
    api('/api/v1/data-gaps')
      .then(payload => { if (alive) setOpenActions((payload.gaps || []).length); })
      .catch(() => { if (alive) setOpenActions(0); });
    return () => { alive = false; };
  }, [gapsKey]);

  const firstTabSync = useRef(true);
  useEffect(() => {
    // Never while an auth fragment is still in flight. #setup / #reset /
    // #auth_error are read from the live hash as a fallback, and rewriting it
    // underneath them would strip a token mid sign-in.
    if (/[#&](setup|reset|auth_error)=/.test(window.location.hash || '')) return;

    const current = window.location.hash.replace('#', '') || 'overview';
    if (current === tab) { firstTabSync.current = false; return; }
    // Overview is the bare URL rather than #overview, but the query string
    // stays: dropping it would discard whatever brought the reader here.
    const target = tab === 'overview'
      ? `${window.location.pathname}${window.location.search}`
      : `#${tab}`;
    if (firstTabSync.current) {
      window.history.replaceState(null, '', target);
      firstTabSync.current = false;
    } else {
      window.history.pushState(null, '', target);
    }
  }, [tab]);

  // Back and Forward move between tabs. Without this the URL would change and
  // the view would not follow it.
  useEffect(() => {
    const onPop = () => {
      const fromHash = window.location.hash.replace('#', '');
      setTab(TAB_IDS.has(fromHash) ? fromHash : 'overview');
    };
    window.addEventListener('popstate', onPop);
    return () => window.removeEventListener('popstate', onPop);
  }, []);
  const xray = useXray(); // pack-driven: absent → no tab, no teaser, no trace
  const chat = useChat(); // same contract as the X-ray: no pack, no chat tab
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
  // Whether this deployment is self-host, confirmed positively rather than
  // inferred from account being absent. Defaults to the hosted-safe branch:
  // operator-only chrome (Connectors tab, data-source labels, AI provider
  // name and cost, env-var setup instructions) stays hidden until self-host
  // is actually confirmed by /api/v1/version, so a Cloud customer never sees
  // it flash on screen while that request is still in flight. Self-hosters
  // see the mirror-image: a brief delay before it appears, harmless on a
  // private single-operator instance.
  const [selfHost, setSelfHost] = useState(false);

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
      setSelfHost(!info.multiuser);
      if (!info.multiuser) return;
      const me = await api('/api/auth/me');
      setAccount(me.authenticated && me.email
        ? { email: me.email, status: me.status || 'active', trialDaysLeft: me.trial_days_left ?? null }
        : null);
    } catch {
      setAccount(null);
      // Leave selfHost as its hosted-safe default — a failed probe is not
      // evidence either way, and the safer wrong guess is "hosted".
    }
  }, []);
  useEffect(() => { probeAccount(); }, [probeAccount]);

  // The tab is also reachable by deep link (#connectors) — bounce hosted
  // accounts to the overview once the probe identifies them.
  useEffect(() => {
    // Hosted accounts have no business configuring the server, so Connectors
    // sends them back. Brokerages is deliberately not in this list: connecting
    // your own account is the one thing on that screen that *is* theirs.
    if (account && tab === 'connectors') setTab('overview');
  }, [account, tab]);

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
    // The dashboard already holds a year of closes for every holding, to
    // draw the sparklines. Seeding the chart cache with it means opening a
    // position renders instantly instead of re-requesting, per symbol, data
    // the page has already downloaded.
    primeStockChartCache(hist.history || {}, '1y');
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
      return true;
    } catch (error) {
      addToast('error', error.message);
      return false;
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
      // One day-change rule for the whole app — the card, the table's DAY
      // column and the stock cards must never disagree about "today".
      const day = dayChangeFor(position, priceHistory);
      if (!day) return;
      change += day.value;
      prevTotal += day.prevValue;
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
  const perf = usePortfolioReturn(portfolio?.last_refresh || 0);
  // TWR, not MWR: it is what brokers report, and what the customer will be
  // holding Serin up against.
  //
  // Shown only where the ledger can support it. The two exclusions are the
  // point of the card, not caveats to it:
  //   holdings_only         — no transactions at all, so the "return" is just
  //                           today's basket priced backwards. That is the
  //                           counterfactual this card exists to stop leading
  //                           with; showing it here would reintroduce the bug
  //                           under a more confident label.
  //   missing_cash_activity — trades recorded but no deposits or withdrawals.
  //                           A time-weighted return has to divide external
  //                           flows out; without them a deposit reads as
  //                           performance, which inflates rather than errs.
  // Both fall back to unrealized gain, which is at least honestly what it is.
  const RETURN_OK = ['complete', 'partial'];
  const returnPct = perf?.available && RETURN_OK.includes(perf.coverage?.quality)
    ? perf.twr_pct ?? null
    : null;
  // The date the return is measured from, which is where the *price series*
  // begins — not coverage.since, which is where the ledger begins. A ledger
  // reaching back to 2016 beside a year of daily closes produced "+75.08%
  // portfolio return since 2016-06-09" for a figure covering twelve months.
  const returnSince = perf?.returns?.all?.from || '';
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

  // Both pack features hang off Briefing: X-ray before it, Chat after. Built
  // as one list rather than two early returns, which silently dropped Chat
  // whenever X-ray was also installed.
  const sectionTabs = TABS.flatMap(item => {
    if (item.id !== 'briefings') return [item];
    return [
      ...(xray.status === 'ok' ? [{ id: 'xray', label: 'X-ray' }] : []),
      item,
      ...(chat.status === 'ok' ? [{ id: 'chat', label: 'Chat' }] : []),
    ];
  }).filter(item => item.id !== 'connectors' || selfHost)
;
  const [pageTitle, pageSubtitle] = PAGE_META[tab] || PAGE_META.overview;
  const overviewDate = new Intl.DateTimeFormat('en-US', {
    weekday: 'long', month: 'long', day: 'numeric', year: 'numeric',
  }).format(new Date());

  return (
    <div className="app-shell">
      <aside className="app-sidebar">
        <div>
          <h1 className="brand sidebar-brand"><span>serin</span><BrandMark /></h1>
          <span className="instance-chip"><i />{selfHost ? 'Local' : 'Cloud'}</span>
        </div>
        <nav className="sidebar-nav" aria-label="Sections">
          {sectionTabs.map(item => (
            <button
              key={item.id}
              className={tab === item.id ? 'active' : ''}
              onClick={() => setTab(item.id)}
            >
              <NavIcon id={item.id} />
              <span>{item.label}</span>
              {item.id === 'xray' && xray.data?.entitled && (xray.data.flags?.length || 0) > 0 && (
                <span className="sidebar-count">{xray.data.flags.length}</span>
              )}
              {item.id === 'actions' && item.badge > 0 && (
                <span className="sidebar-count">{item.badge}</span>
              )}
              {item.id === 'briefings' && runningBriefing && <span className="runningdot">●</span>}
            </button>
          ))}
        </nav>
        <div className="sidebar-foot">
          <div className="sidebar-profile">
            <span className="profile-avatar">{(account?.email || 'Serin').slice(0, 2).toUpperCase()}</span>
            <span>{account?.email || (selfHost ? 'Local instance' : 'Serin Cloud')}</span>
          </div>
          {account && (
            <button className="sidebar-signout" onClick={signOut}><IconSignOut size={16} /> Log out</button>
          )}
        </div>
      </aside>

      <main className="app-container app-main">
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
      <header className="page-header">
        <div className="page-heading">
          <h2>{pageTitle}</h2>
          <p>{tab === 'overview' ? overviewDate : pageSubtitle}</p>
        </div>
        <div className="header-actions">
          {/* Four full-width controls rode every tab, and after onboarding
              most of them are not what anyone came for. What survives at full
              size is the one thing that is always urgent (something needs
              doing) and the one that is always wanted (add a holding). The
              rest moved behind a menu. */}
          <button
            className={`icon-btn bell${openActions > 0 ? ' has-alerts' : ''}`}
            onClick={() => setTab('actions')}
            aria-label={openActions > 0
              ? `${openActions} things need attention`
              : 'Nothing needs attention'}
            title={openActions > 0
              ? `${openActions} things need attention`
              : 'Nothing needs attention'}
          >
            <IconBell />
            {openActions > 0 && <span className="bell-count">{openActions}</span>}
          </button>
          <button className="icon-btn" onClick={refreshPrices}
                  disabled={busy === 'prices' || !hasPositions}
                  aria-label="Refresh prices"
                  title={busy === 'prices' ? 'Refreshing…' : 'Refresh prices'}>
            <IconRefresh />
          </button>
          <HeaderMenu
            currency={config?.display_currency || 'USD'}
            onCurrency={async currency => {
              try {
                await api('/api/settings/display-currency', { method: 'PUT', body: JSON.stringify({ currency }) });
                setDisplayCurrency(currency);
                await loadAll();
              } catch (error) {
                addToast('error', error.message);
              }
            }}
            onSmartImport={() => setShowSmartImport(true)}
          />
          <button className="btn btn-primary" onClick={() => setModal({ mode: 'add' })}>
            <IconPlus /> <span className="btn-label">Add position</span>
          </button>
          {account?.status === 'trialing' && account.trialDaysLeft != null && (
            <button
              className="trial-chip"
              onClick={openPortal}
              title="You have the full product during the trial. Click to add a payment method and continue after it ends."
            >
              Trial · {account.trialDaysLeft}d left
            </button>
          )}
        </div>
      </header>

      {tab === 'overview' && (
        <>
          <section className="stats-grid">
            <StatCard
              label="Total portfolio"
              value={money(portfolio?.total_value)}
              // The return leads; unrealized gain moves to the meta line. It is
              // still worth showing — it is what today's holdings are up — but
              // it is a detail of the portfolio, not a measure of how it did.
              sub={
                returnPct != null
                  ? `${signedPct(returnPct)} portfolio return${returnSince ? ` since ${returnSince}` : ''}`
                  : `${signedMoney(portfolio?.total_gain)} total gain`
              }
              subClass={returnPct != null ? (returnPct >= 0 ? 'positive' : 'negative') : gainTone}
              meta={
                returnPct != null
                  ? `${positions.length} positions · ${signedMoney(portfolio?.total_gain)} unrealized`
                  : `${positions.length} positions`
              }
              bird
            />
            <StatCard
              label="Day Change"
              value={dayChange ? signedMoney(dayChange.value) : '—'}
              valueClass={dayChange ? (dayChange.value >= 0 ? 'positive' : 'negative') : ''}
              sub={dayChange ? `${signedPct(dayChange.pct)} · ${dayChange.tracked} tracked` : 'refresh prices to track'}
              subClass={dayChange ? (dayChange.value >= 0 ? 'positive' : 'negative') : ''}
            />
            <StatCard label="Invested" value={money(portfolio?.total_cost)} sub="Cost basis (ex-cash)" />
            <StatCard label="Cash" value={money(portfolio?.cash_value)} sub="Available to invest" />
          </section>

          {hasPositions ? (
            <>
              {/* Above the chart it explains, not below it. Someone who has
                  just read a number they distrust should not have to scroll to
                  find out why. */}
              <DataGaps onGoTo={setTab} onRecordCost={setCostGap}
                        onImportStatement={openImportFor}
                        refreshKey={gapsKey} />
              <PortfolioTrendChart
                positions={positions}
                priceHistory={priceHistory}
                dateRange={dateRange}
                onRangeChange={setDateRange}
                ledger={perf}
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
                    onDelete={position => setConfirming({ kind: 'position', item: position })}
                    showSource={selfHost}
                  />
                  <AllocationCard portfolio={portfolio} />
                  <TopHoldings positions={positions} total={portfolio?.total_value || 0} onSelect={setSelectedPositionId} />
                  <XrayTeaser xray={xray} onOpen={() => setTab('xray')} />
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

      {tab === 'chat' && <ChatView chat={chat} />}

      {tab === 'stocks' && (
        <section className="stocks-tab">
          {stockDetail ? (
            <StockDetail
              symbol={stockDetail.symbol}
              assetType={stockDetail.assetType}
              onClose={() => setStockDetail(null)}
              hosted={!selfHost}
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
          hosted={!selfHost}
        />
      )}

      {tab === 'news' && (
        <NewsView news={news} loading={newsLoading} onRefresh={refreshNews} />
      )}

      {tab === 'transactions' && (
        <TransactionsView
          addToast={addToast}
          onChanged={() => loadAll().catch(error => addToast('error', error.message))}
        />
      )}

      {tab === 'actions' && (
        <ActionHub
          refreshKey={gapsKey}
          onGoTo={setTab}
          onRecordCost={setCostGap}
          onImportStatement={openImportFor}
          onChanged={() => setGapsKey(key => key + 1)}
        />
      )}

      {tab === 'brokerages' && (
        <Brokerages
          onError={message => addToast('error', message)}
          onChanged={() => loadAll().catch(error => addToast('error', error.message))}
          onViewTransactions={() => setTab('transactions')}
        />
      )}

      {tab === 'connectors' && (
        <>
          <ConnectorsView
            addToast={addToast}
            onChanged={() => loadAll().catch(error => addToast('error', error.message))}
          />
          {/* Agent access sits with the connectors because it is one: the
              data layer, pointed outwards at an assistant instead of inwards
              at a provider. */}
          <AgentAccess addToast={addToast} />
        </>
      )}

      <footer className="footer-note">
        <span>Serin · AI portfolio intelligence</span>
        {/* The public footer carries these for someone deciding whether to
            pay. Inside the app they earn their place for a different reason:
            a subscriber who needs support, or the terms they are billed
            under, should not have to leave for the marketing site to find
            them — and App Review looks for exactly this. Core serves these
            routes, so they resolve on a self-hosted box too. */}
        <nav className="footer-links">
          <a href="/contact">Contact</a>
          <a href="/privacy">Privacy</a>
          <a href="/terms">Terms</a>
        </nav>
        <span>Context &amp; organization — never trade directives</span>
      </footer>

      {modal && (
        <PositionModal
          editing={modal.mode === 'edit' ? modal.position : null}
          brokers={[...new Set(positions.map(position => position.broker).filter(Boolean))]}
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
          onClose={() => { setShowSmartImport(false); setImportBrief(null); }}
          onImported={() => { setGapsKey(key => key + 1); return loadAll(); }}
          addToast={addToast}
          brief={importBrief}
          brokers={[...new Set(positions.map(p => p.broker).filter(Boolean))]}
        />
      )}

      {/* Opened from the gap panel with symbol, account, date and quantity
          already in it, so the only thing left is the price. */}
      {costGap && (
        <CostBasisForm
          gap={costGap}
          addToast={addToast}
          onClose={() => setCostGap(null)}
          onSaved={() => {
            setGapsKey(key => key + 1);      // the gap it just closed
            loadAll().catch(error => addToast('error', error.message));
          }}
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
      </main>
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

// Setup/reset links arrive as URL fragments too, and meet the same fate the
// auth_error above exists to dodge: the shell's tab routing replaceState()s
// the hash away on first render, before LockScreen ever mounts — a paying
// customer's emailed set-your-password link bounced to a bare login screen.
// Capture at module load, which runs before any component.
const _hashToken = name => {
  const match = new RegExp(`[#&]${name}=([^&]+)`).exec(window.location.hash || '');
  return match ? decodeURIComponent(match[1]) : '';
};
const SETUP_TOKEN_ON_LOAD = _hashToken('setup');
const RESET_TOKEN_ON_LOAD = _hashToken('reset');

function LockScreen({ onUnlocked }) {
  const [password, setPassword] = useState('');
  const [email, setEmail] = useState('');
  const [error, setError] = useState(AUTH_ERROR_ON_LOAD);
  const [busy, setBusy] = useState(false);
  // null until the probe answers, so we never flash the wrong form.
  const [multiuser, setMultiuser] = useState(null);
  // A setup link (#setup=…) means the account exists but has no password yet —
  // the customer is arriving from the welcome email after paying. Seeded from
  // the module-load capture; by mount time the hash is already gone.
  const [setupToken, setSetupToken] = useState(SETUP_TOKEN_ON_LOAD);
  // A reset link works like a setup link: prove the email, choose a password.
  const [resetToken, setResetToken] = useState(RESET_TOKEN_ON_LOAD);
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
        {/* The wordmark is the escape hatch every visitor reaches for first,
            and here it was inert — someone who landed on this card with no
            account had only the back button. On a hosted deployment "/" is
            the public site; a self-host build redirects it to the app, which
            is the right destination there too. */}
        <a className="brand lock-home" href="/" aria-label="Serin home">
          <BrandMark /><span>serin</span>
        </a>
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
            when what a newcomer wants is the way in.

            Hosted only: a self-hosted instance has no marketing page and no
            trial, so this offered its owner a pitch for a product they are
            already running, on a link that lands back on this same screen. */}
        {multiuser && !chooseNew && (
          <a className="lock-out" href="/#pricing">New here? What Serin is, and the free trial →</a>
        )}
      </form>
    </div>
  );
}
