import React, { useCallback, useEffect, useMemo, useState } from 'react';
import QRCode from 'qrcode';
import { api } from '../api.js';
import { MarkdownRenderer } from './Markdown.jsx';

const KIND_META = {
  market_data: { label: 'Market data', blurb: 'Where prices, quotes and history come from.' },
  holdings: { label: 'Holdings sources', blurb: 'Where your positions come from — safest connection methods first.' },
  insight: { label: 'Insights', blurb: 'Optional features that act on your portfolio.' },
};
const KIND_ORDER = ['market_data', 'holdings', 'insight'];

// Blank means "leave the stored key alone" (the field renders a mask), so
// removing one needs its own signal — see CLEAR_SECRET in the registry.
const CLEAR_SECRET = '__serin_clear__';

// Trust posture for a holdings connector (see docs/CONNECTOR-TRUST.md).
const POSTURE = {
  oauth: { label: 'Connect', cls: 'safe' },
  file: { label: 'Import', cls: 'safe' },
  api_key: { label: 'API key', cls: 'advanced' },
  none: { label: 'No key', cls: 'safe' },
};

function monogram(name) {
  return (name || '?').trim().slice(0, 1).toUpperCase();
}

export function ConnectorsView({ addToast, onChanged }) {
  const [cards, setCards] = useState([]);
  const [loaded, setLoaded] = useState(false);
  const [openId, setOpenId] = useState(null);
  const [drafts, setDrafts] = useState({}); // id -> {field: value}
  const [busy, setBusy] = useState('');
  const [tests, setTests] = useState({}); // id -> {ok, message}
  const [docs, setDocs] = useState(null); // {id, name, markdown}
  const [pairQr, setPairQr] = useState(null); // {dataUrl, authEnabled}
  const [plan, setPlan] = useState(null); // /api/entitlements — open-core seam
  const [license, setLicense] = useState(null); // /api/license — activation state
  const [licenseKey, setLicenseKey] = useState(''); // paste-box draft
  const [showAdvanced, setShowAdvanced] = useState(false); // demote API-key sources

  const loadLicense = useCallback(() => {
    api('/api/license')
      .then(status => { setLicense(status); setPlan({ plan: status.plan, features: status.features }); })
      .catch(() => setLicense(null));
  }, []);

  useEffect(() => { loadLicense(); }, [loadLicense]);

  async function saveLicense() {
    const key = licenseKey.trim();
    if (!key) return;
    setBusy('license');
    try {
      const status = await api('/api/license', { method: 'PUT', body: JSON.stringify({ key }) });
      setLicense(status);
      setPlan({ plan: status.plan, features: status.features });
      setLicenseKey('');
      if (status.active) addToast?.('success', `Intelligence active — ${status.plan}.`);
      else if (status.installed) addToast?.('success', 'Key saved. Install the Intelligence pack to activate it.');
    } catch (error) {
      addToast?.('error', `License: ${error.message}`);
    } finally {
      setBusy('');
    }
  }

  async function installPack() {
    setBusy('license');
    try {
      const result = await api('/api/admin/install-pack', {
        method: 'POST',
        body: JSON.stringify({ key: licenseKey.trim() }),
      });
      addToast?.('success', result.restart_required
        ? 'Intelligence pack installed — restart Serin to finish activation.'
        : 'Intelligence pack installed.');
      loadLicense();
    } catch (error) {
      addToast?.('error', `Install: ${error.message}`);
    } finally {
      setBusy('');
    }
  }

  async function removeLicense() {
    setBusy('license');
    try {
      const status = await api('/api/license', { method: 'DELETE' });
      setLicense(status);
      setPlan({ plan: status.plan, features: status.features });
      addToast?.('success', 'License removed — back to open source.');
    } catch (error) {
      addToast?.('error', `License: ${error.message}`);
    } finally {
      setBusy('');
    }
  }

  async function showPairingQr() {
    setBusy('pair');
    try {
      const info = await api('/api/pairing');
      const dataUrl = await QRCode.toDataURL(JSON.stringify(info), { width: 280, margin: 1 });
      setPairQr({ dataUrl, authEnabled: info.auth_enabled });
    } catch (error) {
      addToast?.('error', `Pairing: ${error.message}`);
    } finally {
      setBusy('');
    }
  }

  async function openDocs(card) {
    const id = card.manifest.id;
    setBusy(`docs-${id}`);
    try {
      const payload = await api(`/api/connectors/${id}/docs`);
      setDocs({ id, name: card.manifest.name, markdown: payload.markdown });
    } catch (error) {
      addToast?.('error', `Docs: ${error.message}`);
    } finally {
      setBusy('');
    }
  }

  const load = useCallback(async () => {
    try {
      const data = await api('/api/connectors');
      const list = data.connectors || [];
      setCards(list);
      return list;
    } catch (error) {
      addToast?.('error', `Connectors: ${error.message}`);
      return [];
    } finally {
      setLoaded(true);
    }
  }, [addToast]);

  useEffect(() => { load(); }, [load]);

  const grouped = useMemo(() => {
    const by = { market_data: [], holdings: [], insight: [] };
    cards.forEach(card => { (by[card.manifest.kind] || (by[card.manifest.kind] = [])).push(card); });
    return by;
  }, [cards]);

  // A field is editable when it belongs to the person, or when this is a
  // single-user install where the person at the keyboard is also the operator
  // (the API reports that as config_editable). On Cloud, the deployment's own
  // settings — market-data keys, the managed AI key — come from the server's
  // environment, so the form shows them read-only rather than accepting a save
  // the backend will discard.
  function fieldIsEditable(card, field) {
    return field.owner === 'user' || card.instance_config_editable !== false;
  }

  function startEdit(card) {
    if (openId === card.manifest.id) { setOpenId(null); return; }
    const draft = {};
    card.manifest.config_schema.forEach(field => {
      if (field.type === 'provider_list') {
        draft[field.key] = normalizeProviderRows(card, field);
        return;
      }
      draft[field.key] = field.secret ? '' : (card.config?.[field.key] ?? field.default ?? '');
    });
    setDrafts(prev => ({ ...prev, [card.manifest.id]: draft }));
    setOpenId(card.manifest.id);
  }

  // The saved rows, or — for configs that predate the list — rows derived
  // from whichever keys are already set, in the same order the backend's
  // legacy fallback uses. What you see is what will run.
  function normalizeProviderRows(card, field) {
    const saved = card.config?.[field.key];
    if (Array.isArray(saved) && saved.length) {
      return saved.filter(r => r && r.id).map(r => ({ id: r.id, model: r.model || '', base_url: r.base_url || '' }));
    }
    const rows = [];
    ['deepseek', 'anthropic'].forEach(id => {
      const opt = (field.options || []).find(o => o.value === id);
      if (opt && card.config?.[`${opt.key_field}__is_set`]) rows.push({ id, model: '', base_url: '' });
    });
    return rows;
  }

  function setField(id, key, value) {
    setDrafts(prev => ({ ...prev, [id]: { ...prev[id], [key]: value } }));
  }

  async function toggleEnabled(card, enabled) {
    setBusy(`enable-${card.manifest.id}`);
    try {
      await api(`/api/connectors/${card.manifest.id}/enable`, {
        method: 'POST',
        body: JSON.stringify({ enabled }),
      });
      const fresh = await load();
      onChanged?.();
      const updated = fresh.find(c => c.manifest.id === card.manifest.id);
      if (enabled && updated?.needs_setup) {
        // On is a wish, configured is a fact — don't let the green toggle
        // read as "all set". Open the form on the missing pieces.
        addToast?.('error', `${card.manifest.name} is on, but not set up yet — finish the configuration below.`);
        if (openId !== card.manifest.id) startEdit(updated);
      } else {
        addToast?.('success', `${card.manifest.name} ${enabled ? 'enabled' : 'disabled'}.`);
      }
    } catch (error) {
      addToast?.('error', error.message);
    } finally {
      setBusy('');
    }
  }

  async function saveConfig(card) {
    const id = card.manifest.id;
    setBusy(`save-${id}`);
    try {
      // Drop blank secret fields so we don't wipe a stored secret.
      const draft = drafts[id] || {};
      const config = {};
      card.manifest.config_schema.forEach(field => {
        // Operator-owned fields on a shared deployment are set in the server's
        // environment; the API discards them. Don't send what can't land.
        if (!fieldIsEditable(card, field)) return;
        const value = draft[field.key];
        if (field.secret && (value === '' || value == null)) return;
        config[field.key] = value;
      });
      await api(`/api/connectors/${id}/config`, { method: 'PUT', body: JSON.stringify({ config }) });
      await load();
      onChanged?.();
      addToast?.('success', `${card.manifest.name} settings saved.`);
    } catch (error) {
      addToast?.('error', error.message);
    } finally {
      setBusy('');
    }
  }

  async function runTest(card) {
    const id = card.manifest.id;
    setBusy(`test-${id}`);
    setTests(prev => ({ ...prev, [id]: null }));
    try {
      const result = await api(`/api/connectors/${id}/test`, { method: 'POST' });
      setTests(prev => ({ ...prev, [id]: result }));
    } catch (error) {
      setTests(prev => ({ ...prev, [id]: { ok: false, message: error.message } }));
    } finally {
      setBusy('');
    }
  }

  async function runSync(card) {
    const id = card.manifest.id;
    setBusy(`sync-${id}`);
    try {
      const summary = await api(`/api/connectors/${id}/sync`, { method: 'POST' });
      const bits = [];
      if (summary.positions != null) bits.push(`${summary.positions} position${summary.positions === 1 ? '' : 's'}`);
      if (summary.removed) bits.push(`${summary.removed} removed`);
      addToast?.('success', `${card.manifest.name} synced — ${bits.join(' · ') || 'up to date'}.`);
      onChanged?.();
    } catch (error) {
      addToast?.('error', `Sync: ${error.message}`);
    } finally {
      setBusy('');
    }
  }

  if (!loaded) {
    return <section className="panel"><div className="empty-box">Loading connectors…</div></section>;
  }

  const liveCount = cards.length;

  return (
    <div className="connectors-view">
      <div className="connectors-intro">
        <h2>Connectors</h2>
        <p>Wire up where Serin gets your data — and add your own. Every connector is an in-tree plugin; Serin renders this portal automatically from each plugin's manifest. Adding a source is a ~40-line module and a pull request.</p>
        <div className="ossbar">
          <span className="osschip"><b>OSS</b> AGPLv3</span>
          {plan && plan.plan && plan.plan !== 'opensource' && (
            <span className="osschip plan-chip" title={`Active features: ${(plan.features || []).join(', ') || 'none'}`}>
              <b>PLAN</b> {plan.plan}
            </span>
          )}
          <span className="osschip"><b>{liveCount}</b> connectors live</span>
          <span className="osschip">No telemetry · your data stays local</span>
          <a className="osscta" href="https://github.com/" target="_blank" rel="noreferrer">Write a connector ↗</a>
        </div>
      </div>

      <section className="panel license-card">
        <div className="license-head">
          <div className="license-titlerow">
            <h3>Serin Intelligence</h3>
            <span className={`statuspill ${license?.active ? 'on' : license?.installed ? 'pending' : 'off'}`}>
              {license?.active ? 'Active' : license?.installed ? 'Pending' : 'Off'}
            </span>
          </div>
          <p className="license-sub">
            {license?.active
              ? `${license.plan}${license.features?.length ? ` · ${license.features.join(', ')}` : ''}`
              : license?.installed
                ? 'License saved, but the Intelligence pack isn’t loaded yet — install it, then restart.'
                : 'Deep X-ray, benchmark, managed AI. Paste a license key to unlock, or keep the free core.'}
          </p>
        </div>

        {license?.source === 'env' ? (
          <p className="license-note">Key provided via <code>SERIN_LICENSE_KEY</code> — managed in your environment.</p>
        ) : license?.active ? (
          <div className="license-actions">
            <a className="btn btn-sm" href="/pricing" target="_blank" rel="noreferrer">Manage plan ↗</a>
            <a className="btn btn-sm btn-ghost" href="https://serin.money/cancel" target="_blank" rel="noreferrer">Cancel subscription ↗</a>
            <button className="btn btn-sm btn-ghost" disabled={busy === 'license'} onClick={removeLicense}>Remove key</button>
          </div>
        ) : (
          <div className="license-entry">
            <input
              type="text"
              className="license-input"
              placeholder="Paste your license key"
              value={licenseKey}
              onChange={e => setLicenseKey(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter') saveLicense(); }}
              spellCheck={false}
              autoComplete="off"
            />
            {license?.installed ? (
              // Key already saved, pack not loaded: the one useful action is
              // installing — clicking redeems the stored key, no re-paste.
              <button className="btn btn-primary btn-sm" disabled={busy === 'license'} onClick={installPack}>
                {busy === 'license' ? 'Installing…' : 'Install pack'}
              </button>
            ) : (
              <button className="btn btn-primary btn-sm" disabled={busy === 'license' || !licenseKey.trim()} onClick={saveLicense}>
                {busy === 'license' ? 'Saving…' : 'Activate'}
              </button>
            )}
            {/* /pricing, not /#pricing: self-host has no landing page — the
                route sends the buyer to the public site's pricing instead. */}
            <a className="btn btn-sm btn-ghost" href="/pricing" target="_blank" rel="noreferrer">Get a key ↗</a>
          </div>
        )}
        {license?.claimed?.email && !license?.active && (
          <p className="license-note">Key for {license.claimed.email}{license.claimed.exp ? ` · expires ${license.claimed.exp}` : ''} (unverified until the pack loads).</p>
        )}
      </section>

      {KIND_ORDER.map(kind => {
        const list = grouped[kind] || [];
        if (!list.length) return null;
        const meta = KIND_META[kind];
        const renderCard = card => (
          <ConnectorCard
            key={card.manifest.id}
            card={card}
            open={openId === card.manifest.id}
            draft={drafts[card.manifest.id]}
            test={tests[card.manifest.id]}
            busy={busy}
            onToggleEnabled={toggleEnabled}
            onStartEdit={startEdit}
            onSetField={setField}
            onSave={saveConfig}
            fieldIsEditable={fieldIsEditable}
            onTest={runTest}
            onSync={runSync}
            onDocs={openDocs}
          />
        );
        return (
          <section className="connector-group" key={kind}>
            <header className="connector-group-head">
              <h3>{meta.label}</h3>
              <span>{meta.blurb}</span>
            </header>
            {kind === 'holdings'
              ? <HoldingsBuckets list={list} renderCard={renderCard} showAdvanced={showAdvanced} setShowAdvanced={setShowAdvanced} />
              : <div className="connector-list">{list.map(renderCard)}</div>}
          </section>
        );
      })}

      <section className="connector-group">
        <header className="connector-group-head">
          <h3>Community</h3>
          <span>contributed and in review — or build your own</span>
        </header>
        <div className="connector-list">
          <div className="dashcard">
            <div className="dashmono">+</div>
            <div>
              <div className="dashname">Build your own connector</div>
              <div className="dashdesc">Subclass the base manifest, declare your config schema, implement fetch(). Serin renders the form and card for you.</div>
            </div>
            <a className="dashgo" href="/docs/CONNECTORS.md" target="_blank" rel="noreferrer">Read the guide ↗</a>
          </div>
          <div className="dashcard">
            <div className="dashmono">⌥</div>
            <div>
              <div className="dashname">Plaid · Coinbase · IBKR Flex</div>
              <div className="dashdesc">Three community connectors are open as pull requests. Review, test, and help merge them.</div>
            </div>
            <a className="dashgo" href="https://github.com/" target="_blank" rel="noreferrer">View PRs ↗</a>
          </div>
        </div>
      </section>

      <section className="connector-group">
        <header className="connector-group-head">
          <h3>Data</h3>
          <span>your portfolio is one file — take it with you</span>
        </header>
        <div className="connector-list">
          <div className="dashcard">
            <div className="dashmono">⬇</div>
            <div>
              <div className="dashname">Backup &amp; export</div>
              <div className="dashdesc">
                Full JSON backup (positions, lots, transactions, accounts, briefings, settings —
                secrets stay encrypted) or a plain positions CSV.
              </div>
              <div className="data-panel-actions" style={{ marginTop: 10 }}>
                <a className="btn btn-ghost btn-sm" href="/api/backup" download>Download backup</a>
                <a className="btn btn-ghost btn-sm" href="/api/backup/positions.csv" download>Positions CSV</a>
              </div>
            </div>
          </div>
          <div className="dashcard">
            <div className="dashmono">▣</div>
            <div>
              <div className="dashname">Pair mobile app</div>
              <div className="dashdesc">
                Scan from the Serin app (Settings → Scan pairing QR). Carries this server's URL
                {` `}and — when the app lock is on — your session token.
              </div>
              <div className="data-panel-actions" style={{ marginTop: 10 }}>
                <button className="btn btn-ghost btn-sm" disabled={busy === 'pair'} onClick={showPairingQr}>
                  {busy === 'pair' ? 'Generating…' : 'Show pairing QR'}
                </button>
              </div>
            </div>
          </div>
          <div className="dashcard">
            <div className="dashmono">⬆</div>
            <div>
              <div className="dashname">Restore</div>
              <div className="dashdesc">
                Upload a Serin backup JSON. Replaces all current data — restore is all-or-nothing.
              </div>
              <div className="data-panel-actions" style={{ marginTop: 10 }}>
                <label className="btn btn-ghost btn-sm" style={{ cursor: 'pointer' }}>
                  {busy === 'restore' ? 'Restoring…' : 'Choose backup file…'}
                  <input
                    type="file"
                    accept="application/json,.json"
                    style={{ display: 'none' }}
                    disabled={busy === 'restore'}
                    onChange={async event => {
                      const file = event.target.files?.[0];
                      event.target.value = '';
                      if (!file) return;
                      if (!window.confirm('Replace ALL current Serin data with this backup?')) return;
                      setBusy('restore');
                      try {
                        const form = new FormData();
                        form.append('file', file);
                        const result = await api('/api/restore', { method: 'POST', body: form });
                        addToast?.('success', `Restored ${result.restored.positions ?? 0} positions, ${result.restored.transactions ?? 0} transactions.`);
                        onChanged?.();
                        load();
                      } catch (error) {
                        addToast?.('error', `Restore failed: ${error.message}`);
                      } finally {
                        setBusy('');
                      }
                    }}
                  />
                </label>
              </div>
            </div>
          </div>
        </div>
      </section>

      {pairQr && (
        <div className="modal-backdrop" onClick={() => setPairQr(null)}>
          <div className="modal" style={{ maxWidth: 380, textAlign: 'center' }} onClick={event => event.stopPropagation()}>
            <div className="modal-head">
              <h2>Pair mobile app</h2>
              <button className="btn btn-ghost btn-sm" onClick={() => setPairQr(null)}>Close</button>
            </div>
            <img src={pairQr.dataUrl} alt="Serin mobile pairing QR code" style={{ width: 260, height: 260, margin: '0 auto' }} />
            <p className="schedule-hint" style={{ marginTop: 12 }}>
              {pairQr.authEnabled
                ? 'Contains your server URL and session token — treat it like a password.'
                : 'Contains your server URL. Tip: set SERIN_AUTH_PASSWORD to add an app lock before exposing Serin beyond localhost.'}
            </p>
          </div>
        </div>
      )}

      {docs && (
        <div className="modal-backdrop" onClick={() => setDocs(null)}>
          <div className="modal connector-docs-modal" onClick={event => event.stopPropagation()}>
            <div className="modal-head">
              <h2>{docs.name} — docs</h2>
              <button className="btn btn-ghost btn-sm" onClick={() => setDocs(null)}>Close</button>
            </div>
            <div className="modal-body connector-docs-body">
              <MarkdownRenderer content={docs.markdown} />
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// Holdings sources, grouped by trust posture: Connect and Import lead; the
// API-key sources are demoted behind an "Advanced" disclosure.
function HoldingsBuckets({ list, renderCard, showAdvanced, setShowAdvanced }) {
  const connect = list.filter(c => c.manifest.connect_method === 'oauth');
  const importers = list.filter(c => c.manifest.connect_method === 'file');
  const advanced = list.filter(c => !['oauth', 'file'].includes(c.manifest.connect_method));
  return (
    <div className="holdings-buckets">
      {connect.length > 0 && (
        <div className="posture-block">
          <p className="posture-head">
            <span className="posture-chip safe">Connect</span>
            Sign in on the broker&apos;s own site — Serin only receives a revocable token, never your password or keys.
          </p>
          <div className="connector-list">{connect.map(renderCard)}</div>
        </div>
      )}
      {importers.length > 0 && (
        <div className="posture-block">
          <p className="posture-head">
            <span className="posture-chip safe">Import</span>
            Hand over a statement (CSV, or a screenshot via Smart Import). No account access at all.
          </p>
          <div className="connector-list">{importers.map(renderCard)}</div>
        </div>
      )}
      {advanced.length > 0 && (
        <div className="posture-block">
          <button type="button" className="posture-toggle" aria-expanded={showAdvanced} onClick={() => setShowAdvanced(v => !v)}>
            {showAdvanced ? '▾' : '▸'} Advanced — API-key sources ({advanced.length}) · best for self-hosting
          </button>
          {showAdvanced && (
            <>
              <p className="posture-head muted">
                You paste a read-only API key. Fine when you self-host; on a hosted plan, prefer a Connect source above.
              </p>
              <div className="connector-list">{advanced.map(renderCard)}</div>
            </>
          )}
        </div>
      )}
    </div>
  );
}

function ConnectorCard({ card, open, draft, test, busy, onToggleEnabled, onStartEdit, onSetField, onSave, onTest, onSync, onDocs, fieldIsEditable }) {
  const { manifest, enabled, config, configured } = card;
  const posture = manifest.kind === 'holdings' ? POSTURE[manifest.connect_method] : null;
  const id = manifest.id;
  const hasConfig = manifest.config_schema.length > 0;
  // Nothing here is yours to change: this connector is run by whoever operates
  // the deployment. Say so once, rather than disabling six fields silently.
  const operatorRun = hasConfig && !manifest.config_schema.some(f => fieldIsEditable(card, f));
  const anyEditable = hasConfig && !operatorRun;
  // needs_setup outranks everything: an enabled connector that can't work is
  // the state most worth being loud about. And a market-data source serving
  // every quote from env config must not read "Off".
  const servingViaEnv = card.serving_prices && !enabled;
  const statusTone = enabled ? (card.needs_setup ? 'warn' : 'on') : (servingViaEnv ? 'on' : 'off');
  const statusLabel = enabled
    ? (card.needs_setup ? 'Needs setup' : (configured ? 'Active' : 'On (defaults)'))
    : (servingViaEnv ? 'Active (env)' : 'Off');
  const providerListField = manifest.config_schema.find(f => f.type === 'provider_list');
  // Keys live inside their provider rows; the generic loop must not render
  // them a second time below the list.
  const providerKeyFields = new Set(
    (providerListField?.options || []).map(o => o.key_field).filter(Boolean)
  );

  return (
    <div className={`connector-card ${enabled ? 'is-on' : ''}`}>
      <div className="connector-card-main">
        <div className="connector-mono" aria-hidden="true">{monogram(manifest.name)}</div>
        <div className="connector-body">
          <div className="connector-title-row">
            <h4>{manifest.name}</h4>
            {posture && <span className={`posture-chip ${posture.cls}`}>{posture.label}</span>}
            <span className={`connector-status ${statusTone}`}>{statusLabel}</span>
          </div>
          <p className="connector-desc">{manifest.description}</p>
          <div className="connector-actions">
            <label className="switch" title={operatorRun ? 'Run by this deployment for everyone' : undefined}>
              <input
                type="checkbox"
                checked={enabled}
                disabled={busy === `enable-${id}` || operatorRun}
                onChange={event => onToggleEnabled(card, event.target.checked)}
              />
              <span className="switch-track"><span className="switch-thumb" /></span>
              <span className="switch-label">{enabled ? 'Enabled' : 'Disabled'}</span>
            </label>
            {hasConfig && (
              <button className="btn btn-ghost btn-sm" onClick={() => onStartEdit(card)}>
                {open ? 'Close' : 'Configure'}
              </button>
            )}
            {card.supports_sync && enabled && (
              <button
                className="btn btn-sm btn-primary"
                disabled={busy === `sync-${id}`}
                onClick={() => onSync(card)}
              >
                {busy === `sync-${id}` ? 'Syncing…' : 'Sync now'}
              </button>
            )}
            <button
              className="btn btn-ghost btn-sm"
              disabled={busy === `test-${id}`}
              onClick={() => onTest(card)}
            >
              {busy === `test-${id}` ? 'Testing…' : 'Test'}
            </button>
            <button
              className="btn btn-ghost btn-sm"
              disabled={busy === `docs-${id}`}
              onClick={() => onDocs(card)}
            >
              {busy === `docs-${id}` ? 'Loading…' : 'Docs'}
            </button>
            {manifest.docs_url && (
              <a className="connector-doclink" href={manifest.docs_url} target="_blank" rel="noreferrer">Site ↗</a>
            )}
          </div>
          {enabled && card.needs_setup && (
            <div className="connector-needs-setup">
              Enabled, but it can&apos;t run yet — open Configure and finish the setup.
            </div>
          )}
          {test && (
            <div className={`connector-test ${test.ok ? 'ok' : 'err'}`}>
              {test.ok ? '✓ ' : '✕ '}{test.message}
            </div>
          )}
        </div>
      </div>

      {open && hasConfig && (
        <div className="connector-config">
          {operatorRun && (
            <p className="connector-managed-note">
              Provided by Serin Cloud — these settings are managed for you, and there's
              nothing to fill in.
            </p>
          )}
          {manifest.config_schema.map(field => {
            if (providerKeyFields.has(field.key)) return null;
            if (field.type === 'provider_list') {
              // On a shared deployment this is the operator's to configure —
              // the managed note above says so, and rendering read-only rows
              // (with model names) here would only contradict it.
              if (!fieldIsEditable(card, field)) return null;
              return (
                <ProviderListEditor
                  key={field.key}
                  field={field}
                  rows={Array.isArray(draft?.[field.key]) ? draft[field.key] : []}
                  draft={draft || {}}
                  config={config || {}}
                  editable={fieldIsEditable(card, field)}
                  onRows={rows => onSetField(id, field.key, rows)}
                  onKey={(keyField, value) => onSetField(id, keyField, value)}
                />
              );
            }
            return (
              <ConfigInput
                key={field.key}
                field={field}
                value={draft?.[field.key] ?? ''}
                isSet={field.secret ? config?.[`${field.key}__is_set`] : false}
                editable={fieldIsEditable(card, field)}
                onChange={value => onSetField(id, field.key, value)}
              />
            );
          })}
          {anyEditable && (
            <div className="connector-config-actions">
              <button className="btn btn-primary btn-sm" disabled={busy === `save-${id}`} onClick={() => onSave(card)}>
                {busy === `save-${id}` ? 'Saving…' : 'Save settings'}
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function ProviderListEditor({ field, rows, draft, config, editable, onRows, onKey }) {
  const [dragIndex, setDragIndex] = React.useState(null);
  const options = field.options || [];
  const used = new Set(rows.map(r => r.id));
  const available = options.filter(o => !used.has(o.value));

  function optionFor(id) {
    return options.find(o => o.value === id) || { label: id, needs_key: true };
  }

  function move(from, to) {
    if (to < 0 || to >= rows.length || from === to) return;
    const next = rows.slice();
    const [row] = next.splice(from, 1);
    next.splice(to, 0, row);
    onRows(next);
  }

  return (
    <div className="config-field provider-list">
      <span className="config-field-label">{field.label}</span>
      {rows.length === 0 && (
        <p className="provider-empty">No AI providers yet — add one below to power briefings and Smart Import.</p>
      )}
      {rows.map((row, index) => {
        const opt = optionFor(row.id);
        const keySet = opt.key_field ? config[`${opt.key_field}__is_set`] : false;
        return (
          <div
            key={row.id}
            className={`provider-row ${dragIndex === index ? 'dragging' : ''}`}
            draggable={editable}
            onDragStart={() => setDragIndex(index)}
            onDragEnd={() => setDragIndex(null)}
            onDragOver={e => e.preventDefault()}
            onDrop={() => { if (dragIndex !== null) move(dragIndex, index); setDragIndex(null); }}
          >
            <span className="provider-grip" title="Drag to reorder" aria-hidden="true">⠿</span>
            <span className="provider-rank num">{index + 1}</span>
            <div className="provider-row-body">
              <div className="provider-row-head">
                <b>{opt.label}</b>
                {opt.vision && <span className="provider-tag">images</span>}
                {index === 0 && <span className="provider-tag first">tried first</span>}
              </div>
              <div className="provider-row-inputs">
                {opt.needs_key && (
                  <input
                    type="password"
                    disabled={!editable}
                    value={draft[opt.key_field] ?? ''}
                    placeholder={keySet ? '•••••••• (set — blank keeps it, × removes it)' : 'API key'}
                    aria-label={`${opt.label} API key`}
                    onChange={e => onKey(opt.key_field, e.target.value)}
                  />
                )}
                {row.id === 'ollama' && (
                  <input
                    type="text"
                    disabled={!editable}
                    value={row.base_url ?? ''}
                    placeholder={opt.base_url || 'http://localhost:11434/v1'}
                    aria-label="Ollama URL"
                    onChange={e => onRows(rows.map((r, i) => (i === index ? { ...r, base_url: e.target.value } : r)))}
                  />
                )}
                <input
                  type="text"
                  className="provider-model"
                  disabled={!editable}
                  value={row.model ?? ''}
                  placeholder={opt.default_model ? `${opt.default_model} (default)` : 'model'}
                  aria-label={`${opt.label} model`}
                  onChange={e => onRows(rows.map((r, i) => (i === index ? { ...r, model: e.target.value } : r)))}
                />
              </div>
              {opt.help && <span className="config-field-help">{opt.help}</span>}
            </div>
            {editable && (
              <button
                type="button"
                className="provider-remove"
                aria-label={`Remove ${opt.label}`}
                onClick={() => {
                  // Take the key with the row. Leaving it behind was invisible
                  // and consequential: a stored Anthropic key keeps managed AI
                  // switched off, so the provider you just removed goes on
                  // deciding what happens.
                  if (keySet && opt.key_field) onKey(opt.key_field, CLEAR_SECRET);
                  onRows(rows.filter((_, i) => i !== index));
                }}
              >×</button>
            )}
          </div>
        );
      })}
      {editable && available.length > 0 && (
        <div className="provider-add">
          <select
            value=""
            aria-label="Add AI provider"
            onChange={e => {
              const id = e.target.value;
              if (id) onRows([...rows, { id, model: '', base_url: '' }]);
            }}
          >
            <option value="">+ Add AI provider…</option>
            {available.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
          </select>
        </div>
      )}
      {field.help && <span className="config-field-help">{field.help}</span>}
    </div>
  );
}

function ConfigInput({ field, value, isSet, onChange, editable = true }) {
  const id = `cfg-${field.key}`;
  const ro = !editable;
  return (
    <label className={`config-field ${ro ? 'is-managed' : ''}`} htmlFor={id}>
      <span className="config-field-label">
        {field.label}{field.required && !ro && <em className="req"> *</em>}
        {ro && <span className="config-managed-chip">Managed</span>}
      </span>
      {field.type === 'select' ? (
        <select id={id} value={value} disabled={ro} onChange={e => onChange(e.target.value)}>
          {field.options.map(opt => <option key={opt.value} value={opt.value}>{opt.label}</option>)}
        </select>
      ) : field.type === 'boolean' ? (
        <span className="config-bool">
          <input
            id={id}
            type="checkbox"
            checked={value === true || value === 'true' || value === 1}
            disabled={ro}
            onChange={e => onChange(e.target.checked)}
          />
          <span>{field.label}</span>
        </span>
      ) : field.type === 'textarea' ? (
        <textarea id={id} value={value} disabled={ro} placeholder={field.placeholder} onChange={e => onChange(e.target.value)} />
      ) : (
        <input
          id={id}
          type={field.secret ? 'password' : (field.type === 'number' ? 'number' : 'text')}
          value={ro ? '' : value}
          disabled={ro}
          placeholder={
            ro
              ? 'Set by this deployment'
              : (field.secret && isSet ? '•••••••• (set — leave blank to keep)' : field.placeholder)
          }
          onChange={e => onChange(e.target.value)}
        />
      )}
      {field.help && !ro && <span className="config-field-help">{field.help}</span>}
    </label>
  );
}
