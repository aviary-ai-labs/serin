import React, { useEffect, useMemo, useState } from 'react';
import { MarkdownRenderer } from './Markdown.jsx';
import { IconSparkles } from './Icons.jsx';
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
function aiProviderLabel(config) {
  if (!config) return '';
  if (config.ai_provider === 'claude_cli') return 'Claude CLI (dev only)';
  if (config.ai_provider === 'anthropic_api') return 'Anthropic';
  if (config.ai_provider === 'deepseek') return 'DeepSeek';
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
    description: 'Exposure, news links, concentration, and interpretation without advice.',
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

function BriefingPresentation({ briefing }) {
  const parsed = useMemo(() => splitBriefingMarkdown(briefing?.output_markdown), [briefing?.output_markdown]);
  const summarySection = findSection(parsed.sections, ['summary']);
  const portfolioSection = findSection(parsed.sections, ['portfolio', 'exposure']);
  const marketSection = findSection(parsed.sections, ['market']);
  const watchSection = findSection(parsed.sections, ['watch', 'signal']);
  const riskSection = findSection(parsed.sections, ['risk']);
  const reviewSection = findSection(parsed.sections, ['question', 'review']);
  const lead = sectionSummary(summarySection, briefing?.summary || parsed.intro);
  const spotlight = [
    { label: 'Portfolio', section: portfolioSection, tone: 'portfolio' },
    { label: 'Market', section: marketSection, tone: 'market' },
    { label: 'Watch', section: watchSection, tone: 'watch' },
  ].filter(item => item.section);
  const metrics = [
    { label: 'Sections', value: parsed.sections.length || '0' },
    { label: 'Watch Items', value: countListItems(watchSection) || '-' },
    { label: 'Risk Flags', value: countListItems(riskSection) || '-' },
    { label: 'Review Items', value: countListItems(reviewSection) || '-' },
  ];

  if (!briefing?.output_markdown) {
    return <div className="markdown-empty">Nothing to show yet.</div>;
  }

  return (
    <article className="briefing-deck">
      <header className="briefing-cover">
        <div className="briefing-cover-main">
          <span className="briefing-kicker">Serin Daily Brief</span>
          <h1>{parsed.title}</h1>
          {lead && <p>{lead}</p>}
        </div>
      </header>

      <div className="briefing-metric-row" aria-label="Briefing structure">
        {metrics.map(metric => (
          <div className="briefing-metric" key={metric.label}>
            <span>{metric.label}</span>
            <strong>{metric.value}</strong>
          </div>
        ))}
      </div>

      {spotlight.length > 0 && (
        <section className="briefing-spotlight" aria-label="At a glance">
          {spotlight.map(item => (
            <div className={`briefing-spotlight-cell tone-${item.tone}`} key={item.label}>
              <span>{item.label}</span>
              <p>{sectionSummary(item.section)}</p>
            </div>
          ))}
        </section>
      )}

      <div className="briefing-slide-stack">
        {parsed.sections.map((section, index) => (
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

function ScheduleCard({ schedule, config, aiReady, busy, onSave }) {
  const [form, setForm] = useState({ enabled: false, time: '07:30', timezone: 'local', email_enabled: false });
  const [dirty, setDirty] = useState(false);
  const emailConfigured = Boolean(config?.email_configured);

  useEffect(() => {
    if (schedule && !dirty) {
      setForm({
        enabled: schedule.enabled,
        time: schedule.time,
        timezone: schedule.timezone,
        email_enabled: Boolean(schedule.email_enabled),
      });
    }
  }, [schedule, dirty]);

  const setField = (key, value) => {
    setForm(prev => ({ ...prev, [key]: value }));
    setDirty(true);
  };

  const timezoneOptions = TIMEZONE_OPTIONS.some(([value]) => value === form.timezone)
    ? TIMEZONE_OPTIONS
    : [...TIMEZONE_OPTIONS, [form.timezone, form.timezone]];

  return (
    <section className="panel">
      <div className="panel-header">
        <h2>Morning Schedule</h2>
        {schedule?.enabled && schedule?.next_run && (
          <span className="panel-note">next: {formatNextRun(schedule.next_run)}</span>
        )}
      </div>
      <div className="schedule-body">
        <label className="schedule-toggle">
          <input
            type="checkbox"
            checked={form.enabled}
            onChange={event => setField('enabled', event.target.checked)}
          />
          <span>Run the briefing automatically every morning</span>
        </label>
        <div className="schedule-fields">
          <label className="form-label">Time
            <input
              type="time"
              value={form.time}
              onChange={event => setField('time', event.target.value)}
              disabled={!form.enabled}
            />
          </label>
          <label className="form-label">Timezone
            <select
              value={form.timezone}
              onChange={event => setField('timezone', event.target.value)}
              disabled={!form.enabled}
            >
              {timezoneOptions.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </select>
          </label>
          <button
            className="btn btn-primary"
            disabled={!dirty || busy}
            onClick={() => { onSave(form); setDirty(false); }}
          >
            {busy ? 'Saving…' : 'Save'}
          </button>
        </div>
        <label className={`schedule-toggle ${form.enabled && emailConfigured ? '' : 'is-disabled'}`}>
          <input
            type="checkbox"
            checked={form.email_enabled && emailConfigured}
            disabled={!form.enabled || !emailConfigured}
            onChange={event => setField('email_enabled', event.target.checked)}
          />
          <span>
            {emailConfigured
              ? <>Email each scheduled briefing to <b>{config.email_to}</b></>
              : <>Email delivery — set <code>SERIN_SMTP_*</code> and <code>SERIN_EMAIL_TO</code> in <code>.env</code> to enable</>}
          </span>
        </label>
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
            briefing in the style you choose. Organization and context only — never trade advice.
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
            title={estimate?.basis ? `Estimate basis: ${estimate.basis}` : ''}
          >
            <i />{aiProviderLabel(config)}
            {estimate?.estimated_cost_usd != null && (
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
          {config?.ai_error || 'No AI provider configured.'}{' '}
          Set <code>ANTHROPIC_API_KEY</code> or <code>DEEPSEEK_API_KEY</code> in <code>.env</code> and
          restart Serin. For local development you can instead run{' '}
          <code>claude auth login --claudeai</code>.
        </div>
      )}

      <div style={{ marginBottom: 20 }}>
        <ScheduleCard
          schedule={schedule}
          config={config}
          aiReady={aiReady}
          busy={busy === 'schedule'}
          onSave={onSaveSchedule}
        />
      </div>

      <div className="briefing-grid">
        <section className="panel">
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
                {selected.model_cost_usd > 0 && <span>~{moneyPrecise(selected.model_cost_usd)}</span>}
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
