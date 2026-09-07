import React, { useCallback, useEffect, useState } from 'react';
import { api } from '../api.js';
import { money } from '../format.js';

/**
 * What is missing from the data, and the thing that would fix it.
 *
 * Serin could already say a figure was estimated — "AAPL, BTC, CRML, FBALX…
 * have no trades on record" — and that turned out to be worse than useless.
 * It tells a reader something is wrong and leaves them to deduce that the
 * remedy is a button on another tab. Three such labels in a row and a reader
 * reasonably concluded the numbers were simply broken.
 *
 * So every actionable gap names its action, and a complete book renders
 * nothing at all. A panel that is always on screen is one nobody reads on the
 * day it matters.
 */
export function DataGaps({ onGoTo, onRecordCost, onImportStatement, refreshKey }) {
  const [data, setData] = useState(null);
  const [busy, setBusy] = useState('');

  const load = useCallback(() => {
    let alive = true;
    api('/api/v1/data-gaps')
      .then(payload => { if (alive) setData(payload); })
      .catch(() => { if (alive) setData(null); });   // never block the page
    return () => { alive = false; };
  }, []);

  useEffect(load, [load, refreshKey]);

  async function dismiss(gap) {
    setBusy(gap.id);
    try {
      setData(await api('/api/v1/data-gaps/dismiss', {
        method: 'POST',
        body: JSON.stringify({ id: gap.id, dismissed: true }),
      }));
    } catch { /* leave it on screen; a reminder that will not go is better
                 than one that vanishes without being recorded */ }
    finally { setBusy(''); }
  }

  if (!data || data.complete) return null;
  const active = data.active || [];
  const actionable = active.filter(gap => gap.severity === 'high');
  const notes = active.filter(gap => gap.severity !== 'high');
  if (!actionable.length && !notes.length) return null;

  return (
    <section className="gaps">
      <div className="gaps-head">
        <h3>Your returns are partly estimated</h3>
        {/* The panel-level Dismiss is gone. It hid four unrelated items at
            once, which is why the only safe thing to do with it was leave it
            alone. Each card now closes itself. */}
        {data.dismissed_count > 0 && (
          <button type="button" className="gaps-hub-link"
                  onClick={() => onGoTo?.('actions')}>
            {data.dismissed_count} put aside
          </button>
        )}
      </div>
      <p className="gaps-sub">
        {actionable.length > 0 ? (
          <>
            Serin is guessing about <b>{money(data.value_affected)}</b> of
            holdings because it has no trade history for them.
          </>
        ) : (
          <>Some holdings cannot be priced. Nothing to do — the detail is below.</>
        )}
      </p>

      <ul className="gaps-list">
        {[...actionable, ...notes].map(gap => (
          <GapCard key={gap.id} gap={gap} busy={busy === gap.id}
                   onGoTo={onGoTo} onRecordCost={onRecordCost}
                   onImportStatement={onImportStatement}
                   onDismiss={() => dismiss(gap)} />
        ))}
      </ul>
    </section>
  );
}

/**
 * One gap.
 *
 * The layout is a three-column grid rather than a wrapping flex row: on a
 * phone the old one squeezed the prose into a column a few words wide beside
 * a button that kept its full width, leaving most of the card empty and the
 * sentence eight lines tall. Here the text spans the full width and the
 * action sits beneath it, which is the shape a narrow screen wants.
 */
function GapCard({ gap, busy, onGoTo, onRecordCost, onImportStatement, onDismiss }) {
  const actionable = gap.severity === 'high';
  return (
    <li className={`gap${actionable ? '' : ' gap-note'}`}>
      <div className="gap-body">
        <b>{gap.title}</b>
        <span>{gap.detail}</span>
      </div>
      {/* The action is the whole reason this panel exists, so it is a control
          rather than a sentence to be read and forgotten — and where the fix
          is data the reader has to type, it opens the form with everything
          else already in it rather than dropping them on a tab of 984 rows to
          find the right ones themselves. */}
      {actionable ? (
        <button type="button" className="btn btn-sm gap-action"
                onClick={() => gapAction(gap, { onGoTo, onRecordCost, onImportStatement })}
                title={gap.action_hint}>
          {gap.action}
        </button>
      ) : (
        <span className="gap-nothing" title={gap.action_hint}>nothing to do</span>
      )}
      <button type="button" className="gap-close" onClick={onDismiss}
              disabled={busy} aria-label={`Put aside: ${gap.title}`}
              title="Put aside — stays in Actions">
        ×
      </button>
    </li>
  );
}

/**
 * Everything outstanding, including what was put aside.
 *
 * Dismissing used to mean losing: the panel hid and the work stayed undone
 * with nowhere to find it again. A reminder someone closes on a phone at
 * breakfast is usually one they mean to deal with later, not one they have
 * decided against, so it goes here rather than nowhere.
 */
export function ActionHub({ onGoTo, onRecordCost, onImportStatement,
                            refreshKey, onChanged }) {
  const [data, setData] = useState(null);
  const [busy, setBusy] = useState('');

  const load = useCallback(() => {
    let alive = true;
    api('/api/v1/data-gaps')
      .then(payload => { if (alive) setData(payload); })
      .catch(() => { if (alive) setData(null); });
    return () => { alive = false; };
  }, []);

  useEffect(load, [load, refreshKey]);

  async function setDismissed(gap, dismissed) {
    setBusy(gap.id);
    try {
      setData(await api('/api/v1/data-gaps/dismiss', {
        method: 'POST',
        body: JSON.stringify({ id: gap.id, dismissed }),
      }));
      onChanged?.();
    } catch { /* the row stays as it was */ }
    finally { setBusy(''); }
  }

  if (!data) return <p className="drill-empty">Loading…</p>;
  const gaps = data.gaps || [];
  const open = gaps.filter(gap => !gap.dismissed);
  const aside = gaps.filter(gap => gap.dismissed);

  if (!gaps.length) {
    return (
      <section className="hub">
        <div className="hub-clear">
          <h3>Nothing outstanding</h3>
          <p>Every holding has trade history and every sale has a purchase
             behind it. There is nothing here to act on.</p>
        </div>
      </section>
    );
  }

  return (
    <section className="hub">
      {/* No heading here: the page already has one from PAGE_META, and two
          "Actions" stacked with near-identical subtitles reads as a bug. */}
      {data.value_affected > 0 && (
        <header className="hub-head">
          <p className="hub-sub">
            Items you put aside stay here until they are done.
          </p>
          <div className="hub-figure">
            <b>{money(data.value_affected)}</b>
            <span>of holdings still estimated</span>
          </div>
        </header>
      )}

      <HubGroup
        title="To do" count={open.length}
        empty="Nothing waiting — anything outstanding has been put aside below."
        gaps={open} busy={busy} onGoTo={onGoTo} onRecordCost={onRecordCost}
        onImportStatement={onImportStatement}
        onSetDismissed={setDismissed} dismissed={false} />

      {aside.length > 0 && (
        <HubGroup
          title="Put aside" count={aside.length}
          gaps={aside} busy={busy} onGoTo={onGoTo} onRecordCost={onRecordCost}
          onImportStatement={onImportStatement}
          onSetDismissed={setDismissed} dismissed />
      )}
    </section>
  );
}

function HubGroup({ title, count, empty, gaps, busy, dismissed,
                    onGoTo, onRecordCost, onImportStatement, onSetDismissed }) {
  return (
    <div className="hub-group">
      <h3>{title} <span className="hub-count">{count}</span></h3>
      {!gaps.length && <p className="hub-empty">{empty}</p>}
      <ul className="gaps-list">
        {gaps.map(gap => (
          <li key={gap.id}
              className={`gap${gap.severity === 'high' ? '' : ' gap-note'}${dismissed ? ' gap-aside' : ''}`}>
            <div className="gap-body">
              <b>{gap.title}</b>
              <span>{gap.detail}</span>
            </div>
            {gap.severity === 'high' ? (
              <button type="button" className="btn btn-sm gap-action"
                      onClick={() => gapAction(gap, { onGoTo, onRecordCost, onImportStatement })}
                      title={gap.action_hint}>
                {gap.action}
              </button>
            ) : (
              <span className="gap-nothing" title={gap.action_hint}>nothing to do</span>
            )}
            {/* Put aside and bring back are the same control in two states,
                so nothing that is filed here can be filed away permanently by
                accident. */}
            <button type="button" className="gap-close"
                    disabled={busy === gap.id}
                    onClick={() => onSetDismissed(gap, !dismissed)}
                    aria-label={dismissed
                      ? `Bring back: ${gap.title}` : `Put aside: ${gap.title}`}
                    title={dismissed ? 'Bring back to the overview' : 'Put aside'}>
              {dismissed ? '↩' : '×'}
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

/**
 * Where a gap's button goes.
 *
 * One place rather than two copies, and each branch answers "what do I do"
 * rather than "which tab". Landing someone on Transactions in front of 984
 * rows told them where the answer lived and not what it was.
 */
function gapAction(gap, { onGoTo, onRecordCost, onImportStatement }) {
  if (gap.code === 'transferred_without_cost' && gap.lots?.length) {
    onRecordCost?.(gap);
    return;
  }
  if (gap.code === 'sales_without_purchase' && onImportStatement) {
    onImportStatement(gap);
    return;
  }
  onGoTo?.(gap.code === 'broker_without_ledger' ? 'brokerages' : 'transactions');
}
