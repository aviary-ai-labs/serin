import React, { useEffect, useMemo, useRef, useState } from 'react';
import { MarkdownRenderer } from './Markdown.jsx';
import { IconSparkles } from './Icons.jsx';
import { SerinBird } from './SerinBird.jsx';
import { dateShort, dateDay, timeAgo, durationLabel, moneyPrecise } from '../format.js';

function StatusChip({ status }) {
  return (
    <span className={`status-chip ${status}`}>
      {status === 'running' && <span className="pulse-dot" style={{ width: 6, height: 6 }} />}
      {status}
    </span>
  );
}

// The provider, not the model. Which model runs a briefing is an
// implementation detail that changes whenever a cheaper or better one lands,
// and a version string on screen invites people to treat it as a promise. The
// provider is the part that is actually a commitment — it says where the
// portfolio goes — so that stays, and stays truthful.
function aiProviderLabel(config, hosted) {
  if (!config) return '';
  // Managed AI first: on Intelligence/Cloud the provider behind the proxy is
  // Serin's implementation detail, same as the model. Any hosted account
  // gets the same treatment even when the backend's strict `ai_managed` flag
  // is false — a Cloud instance can be running on a plain operator-set key
  // (not the metered proxy) and the provider is still not the customer's to
  // know or choose, exactly like the market-data source.
  if (config.ai_managed || hosted) return 'Serin AI';
  if (config.ai_provider === 'claude_cli') return 'Claude CLI (dev only)';
  if (config.ai_provider === 'anthropic_api') return 'Anthropic';
  if (config.ai_provider === 'deepseek') return 'DeepSeek';
  if (config.ai_provider === 'managed') return 'Serin AI';
  if (config.ai_provider && config.ai_provider !== 'none') {
    return config.ai_provider.charAt(0).toUpperCase() + config.ai_provider.slice(1);
  }
  return 'Not configured';
}

const TIMEZONE_OPTIONS = [
  ['local', 'System timezone'],
  ['UTC', 'UTC'],
  ['America/New_York', 'US Eastern'],
  ['America/Chicago', 'US Central'],
  ['America/Denver', 'US Mountain'],
  ['America/Los_Angeles', 'US Pacific'],
  ['Europe/London', 'London'],
  ['Asia/Shanghai', 'China'],
  ['Asia/Hong_Kong', 'Hong Kong'],
  ['Asia/Singapore', 'Singapore'],
  ['Asia/Tokyo', 'Tokyo'],
  ['Australia/Sydney', 'Sydney'],
];

const BRIEFING_STYLES = [
  {
    id: 'operator',
    label: 'Operator',
    title: 'Structured daily review',
    description: 'What changed, what needs attention, and what data is incomplete.',
  },
  {
    id: 'analyst',
    label: 'Analyst',
    title: 'Deeper context and themes',
    description: 'Exposure, news links, concentration, and interpretation without directives.',
  },
  {
    id: 'executive',
    label: 'Executive',
    title: 'Fast summary only',
    description: 'The few signals worth reading first when time is tight.',
  },
];

function styleLabel(styleId) {
  return BRIEFING_STYLES.find(style => style.id === styleId)?.label || 'Operator';
}

function stripMarkdown(text) {
  return String(text || '')
    .replace(/\*\*(.*?)\*\*/g, '$1')
    .replace(/\*(.*?)\*/g, '$1')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/^\s*[-*]\s+/, '')
    .replace(/^\s*\d+\.\s+/, '')
    .replace(/^#+\s*/, '')
    .trim();
}

function splitBriefingMarkdown(content) {
  const lines = String(content || '').split('\n');
  let title = 'Daily Briefing';
  const intro = [];
  const sections = [];
  let current = null;

  lines.forEach(raw => {
    const line = raw.trimEnd();
    if (line.startsWith('# ')) {
      title = stripMarkdown(line);
      return;
    }
    if (line.startsWith('## ')) {
      current = { title: stripMarkdown(line), lines: [] };
      sections.push(current);
      return;
    }
    if (current) current.lines.push(line);
    else intro.push(line);
  });

  return {
    title,
    intro: intro.join('\n').trim(),
    sections: sections.map(section => ({
      ...section,
      body: section.lines.join('\n').trim(),
    })).filter(section => section.body || section.title),
  };
}

function sectionTone(title) {
  const value = title.toLowerCase();
  if (value.includes('risk')) return 'risk';
  if (value.includes('watch') || value.includes('signal')) return 'watch';
  if (value.includes('market')) return 'market';
  if (value.includes('portfolio') || value.includes('exposure')) return 'portfolio';
  if (value.includes('question') || value.includes('review')) return 'review';
  if (value.includes('summary')) return 'summary';
  return 'neutral';
}

function firstReadableLine(section) {
  if (!section?.body) return '';
  const lines = section.body
    .split('\n')
    .map(stripMarkdown)
    .filter(line => line && !/^-{3,}$/.test(line));
  return lines[0] || '';
}

function firstParagraph(section) {
  if (!section?.body) return '';
  const paragraphs = section.body
    .split(/\n\s*\n/g)
    .map(stripMarkdown)
    .filter(Boolean);
  return paragraphs[0] || firstReadableLine(section);
}

function countListItems(section) {
  if (!section?.body) return 0;
  return section.body
    .split('\n')
    .filter(line => /^\s*([-*]|\d+\.)\s+/.test(line))
    .length;
}

function findSection(sections, includes) {
  return sections.find(section => includes.some(term => section.title.toLowerCase().includes(term)));
}

function sectionSummary(section, fallback) {
  const text = firstParagraph(section) || fallback || '';
  return text.length > 210 ? `${text.slice(0, 207).trim()}...` : text;
}

// Same words, ignoring punctuation and case — enough to catch a lead that is
// simply the summary section repeated.
const sameText = (a, b) => {
  const norm = s => String(s || '').replace(/[^a-z0-9]+/gi, ' ').trim().toLowerCase();
  const x = norm(a);
  const y = norm(b);
  return Boolean(x) && Boolean(y) && (x.startsWith(y.slice(0, 120)) || y.startsWith(x.slice(0, 120)));
};

function BriefingPresentation({ briefing }) {
  const parsed = useMemo(() => splitBriefingMarkdown(briefing?.output_markdown), [briefing?.output_markdown]);
  const summarySection = findSection(parsed.sections, ['summary']);
  const watchSection = findSection(parsed.sections, ['watch', 'signal']);
  const riskSection = findSection(parsed.sections, ['risk']);
  const reviewSection = findSection(parsed.sections, ['question', 'review']);

  // The cover already prints the summary, so a "Summary" section repeating it
  // is the same paragraph twice, two inches apart. Only drop it when the whole
  // section *is* that paragraph — and then print the cover's copy in full,
  // since the tail no longer appears anywhere else.
  const summaryBody = (summarySection?.body || '').trim();
  const summaryIsOneParagraph =
    Boolean(summaryBody) &&
    !/^\s*([-*]|\d+\.)\s+/m.test(summaryBody) &&
    summaryBody.split(/\n\s*\n/).filter(part => part.trim()).length === 1;
  const leadFull = firstParagraph(summarySection) || briefing?.summary || parsed.intro || '';
  const dropSummary = summaryIsOneParagraph && sameText(leadFull, summaryBody);
  const lead = dropSummary ? leadFull : sectionSummary(summarySection, briefing?.summary || parsed.intro);

  const sections = dropSummary
    ? parsed.sections.filter(section => section !== summarySection)
    : parsed.sections;

  // Counts only where there is something to count — a row of dashes described
  // nothing, and "Sections: 3" was never a fact about the portfolio.
  const metrics = [
    { label: 'Watch items', value: countListItems(watchSection) },
    { label: 'Risk flags', value: countListItems(riskSection) },
    { label: 'Review items', value: countListItems(reviewSection) },
  ].filter(metric => metric.value > 0);

  if (!briefing?.output_markdown) {
    return <div className="markdown-empty">Nothing to show yet.</div>;
  }

  return (
    <article className="briefing-deck">
      <header className="briefing-cover">
        <SerinBird className="briefing-bird" />
        <div className="briefing-cover-main">
          <span className="briefing-kicker">Serin Daily Brief</span>
          <h1>{parsed.title}</h1>
          {lead && <p>{lead}</p>}
        </div>
        {metrics.length > 0 && (
          <div className="briefing-metric-row" aria-label="Briefing structure">
            {metrics.map(metric => (
              <div className="briefing-metric" key={metric.label}>
                <strong>{metric.value}</strong>
                <span>{metric.label}</span>
              </div>
            ))}
          </div>
        )}
      </header>

      <div className="briefing-slide-stack">
        {sections.map((section, index) => (
          <section className={`briefing-slide tone-${sectionTone(section.title)}`} key={`${section.title}-${index}`}>
            <div className="briefing-slide-head">
              <span className="briefing-slide-number">{String(index + 1).padStart(2, '0')}</span>
              <h2>{section.title}</h2>
            </div>
            <MarkdownRenderer content={section.body} />
          </section>
        ))}
      </div>
    </article>
  );
}

function formatNextRun(iso) {
  if (!iso) return '';
  try {
    return new Date(iso).toLocaleString('en-US', {
      weekday: 'short',
      month: 'short',
      day: 'numeric',
      hour: 'numeric',
      minute: '2-digit',
    });
  } catch {
    return iso;
  }
}

function timeLabel(value) {
  const [hours, minutes] = String(value || '').split(':').map(Number);
  if (!Number.isFinite(hours) || !Number.isFinite(minutes)) return value || '';
  const suffix = hours >= 12 ? 'PM' : 'AM';
  const hour12 = hours % 12 === 0 ? 12 : hours % 12;
  return `${hour12}:${String(minutes).padStart(2, '0')} ${suffix}`;
}

function ScheduleCard({ schedule, config, aiReady, busy, onSave, hosted }) {
  const [form, setForm] = useState({ enabled: false, time: '07:30', timezone: 'local', email_enabled: false });
  const [justSaved, setJustSaved] = useState(false);
  // Local edits the server hasn't been told about yet — a ref, not state,
  // because the incoming-schedule effect must see the CURRENT value, not the
  // one captured when it last ran, or a save round-trip clobbers live typing.
  const unsaved = useRef(false);
  const pendingSave = useRef(null);
  const emailConfigured = Boolean(config?.email_configured);

  useEffect(() => {
    if (schedule && !unsaved.current) {
      setForm({
        enabled: schedule.enabled,
        time: schedule.time,
        timezone: schedule.timezone,
        email_enabled: Boolean(schedule.email_enabled),
      });
    }
  }, [schedule]);

  useEffect(() => () => clearTimeout(pendingSave.current), []);
  useEffect(() => {
    if (!justSaved) return undefined;
    const timer = setTimeout(() => setJustSaved(false), 2000);
    return () => clearTimeout(timer);
  }, [justSaved]);

  // Four small values with an obvious meaning — nothing here is worth making
  // someone press Save for. Flipping a switch commits at once; typing in a
  // time waits for a pause, so we don't POST once per keystroke.
  function commit(next, immediate = false) {
    setForm(next);
    unsaved.current = true;
    clearTimeout(pendingSave.current);
    const flush = () => {
      unsaved.current = false;
      setJustSaved(true);
      onSave(next);
    };
    if (immediate) flush();
    else pendingSave.current = setTimeout(flush, 700);
  }

  const setField = (key, value, immediate) => commit({ ...form, [key]: value }, immediate);

  const timezoneOptions = TIMEZONE_OPTIONS.some(([value]) => value === form.timezone)
    ? TIMEZONE_OPTIONS
    : [...TIMEZONE_OPTIONS, [form.timezone, form.timezone]];
  const timezoneLabel = (timezoneOptions.find(([value]) => value === form.timezone) || [])[1] || form.timezone;

  const status = busy
    ? 'Saving…'
    : justSaved
      ? 'Saved'
      : schedule?.enabled && schedule?.next_run
        ? `next: ${formatNextRun(schedule.next_run)}`
        : '';

  return (
    <section className="panel">
      <div className="panel-header">
        <h2>Morning Schedule</h2>
        {status && <span className={`panel-note ${justSaved && !busy ? 'is-saved' : ''}`}>{status}</span>}
      </div>
      <div className="schedule-body">
        <label className="switch schedule-master">
          <input
            type="checkbox"
            checked={form.enabled}
            onChange={event => setField('enabled', event.target.checked, true)}
          />
          <span className="switch-track"><span className="switch-thumb" /></span>
          <span className="schedule-master-text">
            <b>Daily briefing</b>
            <em>
              {form.enabled
                ? `Every morning at ${timeLabel(form.time)} · ${timezoneLabel}`
                : 'Off — run a briefing yourself with the button above.'}
            </em>
          </span>
        </label>

        {/* Everything below belongs to the switch above, so it lives inside
            it rather than beside it — the old layout put a second checkbox at
            the same level as the master, which read as an unrelated option
            and had to be greyed out to hint otherwise. */}
        {form.enabled && (
          <div className="schedule-detail">
            <div className="schedule-fields">
              <label className="form-label">Time
                <input
                  type="time"
                  value={form.time}
                  onChange={event => setField('time', event.target.value)}
                />
              </label>
              <label className="form-label">Timezone
                <select
                  value={form.timezone}
                  onChange={event => setField('timezone', event.target.value, true)}
                >
                  {timezoneOptions.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                </select>
              </label>
            </div>

            {emailConfigured ? (
              <label className="switch schedule-email">
                <input
                  type="checkbox"
                  checked={form.email_enabled}
                  onChange={event => setField('email_enabled', event.target.checked, true)}
                />
                <span className="switch-track"><span className="switch-thumb" /></span>
                <span className="switch-label">Email it to <b>{config.email_to}</b></span>
              </label>
            ) : (
              <p className="schedule-email-note">
                {/* The .env instructions are for an operator's own machine — a
                    hosted account has no file to edit and nothing to restart. */}
                {hosted
                  ? <>Email delivery isn't available on Serin Cloud yet.</>
                  : <>Email delivery — set <code>SERIN_SMTP_*</code> and <code>SERIN_EMAIL_TO</code> in <code>.env</code> to enable.</>}
              </p>
            )}
          </div>
        )}

        {form.enabled && !aiReady && (
          <div className="notice">
            Scheduling is on, but no AI provider is ready — scheduled runs will record an error
            until a provider is configured.
          </div>
        )}
        <p className="schedule-hint">
          Runs while Serin is open; if it was closed at the scheduled time, the briefing catches up
          on launch. Failed runs retry up to 3× with a 10-minute pause, and every attempt shows in
          the history.
        </p>
      </div>
    </section>
  );
}

function BriefingStylePicker({ value, disabled, onChange }) {
  return (
    <div className="briefing-style-picker" role="radiogroup" aria-label="Briefing style">
      {BRIEFING_STYLES.map(style => (
        <button
          key={style.id}
          type="button"
          className={value === style.id ? 'style-option active' : 'style-option'}
          role="radio"
          aria-checked={value === style.id}
          disabled={disabled}
          onClick={() => onChange(style.id)}
        >
          <span className="style-option-label">{style.label}</span>
          <strong>{style.title}</strong>
          <span>{style.description}</span>
        </button>
      ))}
    </div>
  );
}

export function BriefingsView({
  config,
  briefings,
  selectedId,
  onSelectBriefing,
  onRun,
  onDelete,
  onEmail,
  busy,
  preferences,
  onSavePreferences,
  schedule,
  onSaveSchedule,
  hosted = false,
}) {
  const [copied, setCopied] = useState(false);
  const [readerMode, setReaderMode] = useState('presentation');
  const [estimate, setEstimate] = useState(null);
  const selected = useMemo(
    () => briefings.find(item => item.id === selectedId) || briefings[0] || null,
    [briefings, selectedId],
  );
  const anyRunning = briefings.some(item => item.status === 'running');
  const aiReady = Boolean(config?.ai_ready);
  const selectedStyle = preferences?.style || 'operator';

  // Cost guard: show the model + expected cost before the user commits a run,
  // so a provider change (e.g. Auto upgrading to Sonnet) is never a surprise.
  useEffect(() => {
    let cancelled = false;
    fetch('/api/briefings/estimate')
      .then(response => (response.ok ? response.json() : null))
      .then(payload => { if (!cancelled) setEstimate(payload); })
      .catch(() => { if (!cancelled) setEstimate(null); });
    return () => { cancelled = true; };
  }, [config?.ai_provider, config?.ai_model, briefings.length]);

  function selectStyle(style) {
    if (style === selectedStyle) return;
    onSavePreferences({ ...(preferences || {}), style });
  }

  async function copyMarkdown() {
    if (!selected?.output_markdown) return;
    try {
      await navigator.clipboard.writeText(selected.output_markdown);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch {
      // Clipboard may be unavailable; the copy button is a convenience only.
    }
  }

  return (
    <>
      <section className="briefing-hero">
        <div>
          <h2>Daily Briefing</h2>
          <p>
            Serin reads your live portfolio snapshot and the latest market headlines, then writes the
            briefing in the style you choose. Organization and context only — never trade directives.
          </p>
          <BriefingStylePicker
            value={selectedStyle}
            disabled={busy === 'briefing-preferences'}
            onChange={selectStyle}
          />
        </div>
        <div className="briefing-run-controls">
          <span
            className={`ai-chip ${aiReady ? '' : 'off'}`}
            title={!hosted && estimate?.basis ? `Estimate basis: ${estimate.basis}` : ''}
          >
            <i />{aiProviderLabel(config, hosted)}
            {/* The cost guard exists so a self-hoster paying per token isn't
                surprised by a provider change — a hosted account's briefings
                are bundled into the subscription, so there is no per-run
                number that means anything to them. */}
            {!hosted && estimate?.estimated_cost_usd != null && (
              <em className="ai-chip-cost">~${estimate.estimated_cost_usd.toFixed(4)}/run</em>
            )}
          </span>
          <button className="btn btn-primary" onClick={() => onRun(selectedStyle)} disabled={!aiReady || anyRunning || busy === 'briefing'}>
            <IconSparkles /> {anyRunning ? 'Briefing in progress…' : busy === 'briefing' ? 'Starting…' : `Run ${styleLabel(selectedStyle)} Brief`}
          </button>
        </div>
      </section>

      {!aiReady && (
        <div className="notice" style={{ marginBottom: 20 }}>
          {hosted
            // A hosted account can't set an API key or restart the machine —
            // an unready provider here is on the operator, not the customer.
            ? (config?.ai_error || "AI briefings aren't available right now — this is on us, not something you need to fix.")
            : (
              <>
                {config?.ai_error || 'No AI provider configured.'}{' '}
                Set <code>ANTHROPIC_API_KEY</code> or <code>DEEPSEEK_API_KEY</code> in <code>.env</code> and
                restart Serin. For local development you can instead run{' '}
                <code>claude auth login --claudeai</code>.
              </>
            )}
        </div>
      )}

      <div style={{ marginBottom: 20 }}>
        <ScheduleCard
          schedule={schedule}
          config={config}
          aiReady={aiReady}
          busy={busy === 'schedule'}
          onSave={onSaveSchedule}
          hosted={hosted}
        />
      </div>

      <div className="briefing-grid">
        <section className="panel briefing-history">
          <div className="panel-header">
            <h2>History</h2>
            <span className="panel-note">{briefings.length} run{briefings.length === 1 ? '' : 's'}</span>
          </div>
          <div className="briefing-list">
            {briefings.length === 0 && (
              <div className="empty-box">
                No briefings yet.{aiReady ? ' Run your first one above.' : ''}
              </div>
            )}
            {briefings.map(item => (
              <button
                key={item.id}
                className={selected?.id === item.id ? 'briefing-row active' : 'briefing-row'}
                onClick={() => onSelectBriefing(item.id)}
              >
                <span className="briefing-row-top">
                  <span style={{ display: 'inline-flex', gap: 6 }}>
                    <StatusChip status={item.status} />
                    {item.trigger === 'scheduled' && <span className="trigger-chip">auto</span>}
                  </span>
                  <time>{timeAgo(item.created_at)}</time>
                </span>
                <p>{item.summary || (item.status === 'error' ? item.error : dateShort(item.created_at))}</p>
              </button>
            ))}
          </div>
        </section>

        <section className="panel briefing-reader">
          {selected ? (
            <>
              <div className="briefing-meta">
                <span>{dateDay(selected.created_at)}</span>
                {selected.completed_at && <span>took {durationLabel(selected.created_at, selected.completed_at)}</span>}
                {/* Managed AI is included in the plan, so the run's list-price
                    cost is our bookkeeping, not a charge — showing it reads as
                    one. Self-hosters pay it and should see it. */}
                {selected.model_cost_usd > 0 && !config?.ai_managed && !hosted && (
                  <span>~{moneyPrecise(selected.model_cost_usd)}</span>
                )}
                {selected.emailed_at && <span>emailed {timeAgo(selected.emailed_at)}</span>}
                <span className="spacer" />
                {selected.status === 'done' && config?.email_configured && (
                  <button className="link-btn" disabled={busy === `email-${selected.id}`} onClick={() => onEmail(selected)}>
                    {busy === `email-${selected.id}` ? 'Sending…' : selected.emailed_at ? 'Email again' : 'Email'}
                  </button>
                )}
                {selected.status === 'done' && (
                  <span className="segmented reader-mode-toggle" role="group" aria-label="Briefing view">
                    <button
                      type="button"
                      className={readerMode === 'presentation' ? 'active' : ''}
                      onClick={() => setReaderMode('presentation')}
                    >
                      Deck
                    </button>
                    <button
                      type="button"
                      className={readerMode === 'markdown' ? 'active' : ''}
                      onClick={() => setReaderMode('markdown')}
                    >
                      Markdown
                    </button>
                  </span>
                )}
                {selected.status === 'done' && (
                  <button className="link-btn" onClick={copyMarkdown}>{copied ? 'Copied ✓' : 'Copy markdown'}</button>
                )}
                <button className="link-btn danger" onClick={() => onDelete(selected)}>Delete</button>
              </div>
              {selected.status === 'running' ? (
                <div className="briefing-running">
                  <div className="spinner" />
                  <span>Reading your portfolio and today's headlines…</span>
                </div>
              ) : selected.status === 'error' ? (
                <div className="briefing-content">
                  <div className="notice">{selected.error || 'The briefing failed.'}</div>
                </div>
              ) : (
                <div className="briefing-content">
                  {readerMode === 'presentation' ? (
                    <BriefingPresentation briefing={selected} />
                  ) : (
                    <MarkdownRenderer content={selected.output_markdown} />
                  )}
                </div>
              )}
            </>
          ) : (
            <div className="briefing-running" style={{ padding: '120px 20px' }}>
              <IconSparkles size={30} />
              <span>Your briefings will appear here.</span>
            </div>
          )}
        </section>
      </div>
    </>
  );
}
