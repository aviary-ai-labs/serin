import React, { useCallback, useRef, useState } from 'react';
import { api } from '../api.js';
import { brokerLabel, brokerOptions, normalizeBroker } from '../format.js';

const ACCEPTED = '.csv,.tsv,.txt,.pdf,application/pdf,image/png,image/jpeg,image/webp,image/gif';
const ASSET_TYPES = ['stock', 'etf', 'crypto', 'cash', 'option'];
const MISSING_UPLOAD_MESSAGE = 'Provide a file or paste text to extract from.';

function fileAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = String(reader.result || '');
      const comma = result.indexOf(',');
      if (comma < 0) reject(new Error(`Could not read ${file.name}.`));
      else resolve(result.slice(comma + 1));
    };
    reader.onerror = () => reject(new Error(`Could not read ${file.name}.`));
    reader.readAsDataURL(file);
  });
}

function snapshotFile(file) {
  // Detach the Blob from the transient <input> selection before that control
  // is cleared. This avoids another iOS failure mode where the filename and
  // size remain visible in React state but the original picker-backed File is
  // no longer readable when the user taps Extract.
  try {
    return new File([file], file.name, { type: file.type, lastModified: file.lastModified });
  } catch {
    return file;
  }
}

async function extractFile(file, hint) {
  const form = new FormData();
  form.append('file', file, file.name);
  if (hint) form.append('hint', hint);
  try {
    return await api('/api/v1/import/extract', { method: 'POST', body: form });
  } catch (error) {
    // Mobile Safari occasionally loses a selected File while forwarding a
    // multipart request through the installed PWA service worker. Retry only
    // the server's explicit "no file received" response, before any AI call
    // could have happened, using the JSON path that is reliable on iOS.
    if (error.status !== 400 || !String(error.message || '').includes(MISSING_UPLOAD_MESSAGE)) throw error;
    return api('/api/v1/import/extract', {
      method: 'POST',
      body: JSON.stringify({
        filename: file.name,
        content_type: file.type || 'application/octet-stream',
        file_base64: await fileAsBase64(file),
        hint,
      }),
    });
  }
}

/**
 * Smart Import modal — drop one or more files (CSV / images), or fill in a
 * blank template manually → AI extracts positions from each file → user reviews
 * the merged preview table → confirm to write to DB.
 *
 * The extract endpoint is idempotent; nothing reaches the DB until the user
 * clicks "Import".
 */
// Deposits and withdrawals first: they are the ones that decide whether
// money you added shows up as money you made, and the ones a statement most
// often labels ambiguously.
// Must cover every action the backend can produce. A value with no matching
// <option> does not render as itself: the browser shows the first option
// instead, so a parsed stock split appeared in this table as a deposit — and
// one touch of that dropdown would have made it one, inventing a contribution
// that never happened and corrupting every return computed after it.
const TXN_ACTIONS = [
  'deposit', 'withdrawal', 'buy', 'sell', 'dividend',
  'interest', 'fee', 'tax', 'transfer', 'split', 'fx', 'adjustment',
];

export function SmartImport({ onClose, onImported, addToast, brokers = [], brief = null }) {
  const availableBrokers = brokerOptions(brokers);
  const [stage, setStage] = useState('intake'); // intake | reviewing | importing
  const [files, setFiles] = useState([]);
  const [hint, setHint] = useState('');
  const [dragOver, setDragOver] = useState(false);
  const [extract, setExtract] = useState(null);
  const [rows, setRows] = useState([]);
  // Activity statements carry transactions and often no positions at all.
  // They were parsed and then discarded, because nothing rendered them.
  const [txns, setTxns] = useState([]);
  const [txnSelected, setTxnSelected] = useState(new Set());
  const [selected, setSelected] = useState(new Set());
  const [replace, setReplace] = useState(false);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState('');
  const [progress, setProgress] = useState(null); // { current, total } while extracting
  const fileInputRef = useRef(null);

  // Append files, de-duping by name+size so an accidental double-drop is a no-op.
  const addFiles = useCallback(list => {
    const incoming = Array.from(list || []).map(snapshotFile);
    if (!incoming.length) return;
    setFiles(prev => {
      const seen = new Set(prev.map(f => `${f.name}:${f.size}`));
      const merged = [...prev];
      incoming.forEach(f => { if (!seen.has(`${f.name}:${f.size}`)) merged.push(f); });
      return merged;
    });
  }, []);

  const onDrop = useCallback(event => {
    event.preventDefault();
    setDragOver(false);
    addFiles(event.dataTransfer.files);
  }, [addFiles]);

  function removeFile(idx) {
    setFiles(prev => prev.filter((_, i) => i !== idx));
  }

  async function runExtract() {
    if (files.length === 0) {
      setError('Drop at least one file first.');
      return;
    }
    setError('');
    setBusy('extract');
    try {
      // Each file is a separate extraction (an image needs its own vision call),
      // so loop and merge every file's rows into one review table.
      const allRows = [];
      const allTxns = [];
      const notesList = [];
      // Files Serin parses exactly rather than inferring — worth telling the
      // user, because it is the difference between a ledger that reproduces
      // and one that is a good guess.
      const parsedFormats = new Set();
      const skipped = [];
      for (let i = 0; i < files.length; i += 1) {
        setProgress({ current: i + 1, total: files.length });
        let payload;
        try {
          payload = await extractFile(files[i], hint.trim());
        } catch (error) {
          throw new Error(`${files[i].name}: ${error.message || 'Extraction failed.'}`);
        }
        (payload.rows || []).forEach(row => allRows.push({ ...row, _source: files[i].name }));
        (payload.transactions || []).forEach(t => allTxns.push({ ...t, _source: files[i].name }));
        if (payload.notes) notesList.push(payload.notes);
        if (payload.broker_format) parsedFormats.add(payload.broker_format);
        (payload.unknown_codes || []).forEach(row => skipped.push(row));
      }
      setExtract({
        fileCount: files.length,
        notes: notesList.join(' · '),
        parsedFormats: [...parsedFormats],
        skipped,
      });
      setRows(allRows);
      setTxns(allTxns);
      // Transactions come from a statement of what already happened, so
      // there is nothing to second-guess — default them all on.
      setTxnSelected(new Set(allTxns.map((_, idx) => idx)));
      // Default-select rows with no warnings
      const initialSelected = new Set();
      allRows.forEach((row, idx) => {
        if (!row.warnings || row.warnings.length === 0) initialSelected.add(idx);
      });
      setSelected(initialSelected);
      setStage('reviewing');
    } catch (err) {
      setError(err.message || 'Extraction failed.');
    } finally {
      setBusy('');
      setProgress(null);
    }
  }

  function updateTxn(idx, field, value) {
    setTxns(prev => prev.map((t, i) => (i === idx ? { ...t, [field]: value } : t)));
  }

  async function runImport() {
    const chosen = rows
      .filter((_, idx) => selected.has(idx))
      .filter(row => row.symbol && String(row.symbol).trim());
    const chosenTxns = txns.filter((_, idx) => txnSelected.has(idx));
    // An activity statement is all transactions and no positions. Requiring a
    // position here is what made a trade-history screenshot un-importable.
    if (chosen.length === 0 && chosenTxns.length === 0) {
      setError('Select at least one position or transaction to import.');
      return;
    }
    setError('');
    setBusy('import');
    try {
      const parts = [];
      if (chosen.length > 0) {
        const result = await api('/api/v1/positions/bulk', {
          method: 'POST',
          body: JSON.stringify({ rows: chosen, replace }),
        });
        parts.push(`${result.inserted} position${result.inserted === 1 ? '' : 's'}`);
        if (result.tax_lots_inserted > 0) {
          parts.push(`${result.tax_lots_inserted} tax lot${result.tax_lots_inserted === 1 ? '' : 's'}`);
        }
        if (result.skipped > 0) parts.push(`skipped ${result.skipped}`);
      }
      if (chosenTxns.length > 0) {
        const result = await api('/api/v1/transactions/bulk', {
          method: 'POST',
          body: JSON.stringify({ transactions: chosenTxns }),
        });
        parts.push(`${result.inserted} transaction${result.inserted === 1 ? '' : 's'}`);
        // Re-importing a statement is normal and safe, so say what happened
        // rather than implying every run should add something.
        if (result.skipped > 0) parts.push(`${result.skipped} already on record`);
      }
      addToast?.('success', `Imported ${parts.join(' · ')}`);
      onImported?.();
      onClose?.();
    } catch (err) {
      setError(err.message || 'Bulk insert failed.');
    } finally {
      setBusy('');
    }
  }

  function updateRow(idx, key, value) {
    setRows(prev => prev.map((row, i) => (i === idx ? { ...row, [key]: value } : row)));
  }

  function updateTaxLot(rowIdx, lotIdx, key, value) {
    setRows(prev => prev.map((row, i) => {
      if (i !== rowIdx) return row;
      const taxLots = (row.tax_lots || []).map((lot, j) => (
        j === lotIdx ? { ...lot, [key]: value } : lot
      ));
      return { ...row, tax_lots: taxLots };
    }));
  }

  function addTaxLot(rowIdx) {
    setRows(prev => prev.map((row, i) => (
      i === rowIdx
        ? { ...row, tax_lots: [...(row.tax_lots || []), { acquired_at: '', quantity: 0, cost_basis: 0 }] }
        : row
    )));
  }

  function removeTaxLot(rowIdx, lotIdx) {
    setRows(prev => prev.map((row, i) => (
      i === rowIdx
        ? { ...row, tax_lots: (row.tax_lots || []).filter((_, j) => j !== lotIdx) }
        : row
    )));
  }

  function toggleRow(idx) {
    setSelected(prev => {
      const next = new Set(prev);
      if (next.has(idx)) next.delete(idx); else next.add(idx);
      return next;
    });
  }

  function toggleAll() {
    if (selected.size === rows.length) setSelected(new Set());
    else setSelected(new Set(rows.map((_, idx) => idx)));
  }

  function blankRow() {
    return { symbol: '', name: '', broker: brokers[0] || 'manual', asset_type: 'stock', quantity: 0, average_cost: 0, warnings: [] };
  }

  // Jump straight to the review table as a blank, fillable template — no upload
  // and no AI call — for users who'd rather type positions in directly.
  function startManual() {
    setError('');
    const seed = [blankRow(), blankRow(), blankRow()];
    setRows(seed);
    setSelected(new Set(seed.map((_, i) => i)));
    setExtract({ manual: true });
    setStage('reviewing');
  }

  function addRow() {
    setRows(prev => [...prev, blankRow()]);
    setSelected(prev => new Set([...prev, rows.length]));
  }

  // Rows that will actually be written: selected AND have a symbol (blank
  // template rows are ignored so they don't produce junk positions).
  const importable = rows.filter((row, idx) => selected.has(idx) && row.symbol && String(row.symbol).trim()).length
    + txns.filter((_, idx) => txnSelected.has(idx)).length;

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal smart-import-modal" onClick={event => event.stopPropagation()}>
        <div className="smart-import-head">
          <h2>Smart Import</h2>
          <button className="btn btn-ghost btn-sm" onClick={onClose}>Close</button>
        </div>

        {stage === 'intake' && (
          <div className="smart-import-intake">
            {/* Arriving from a gap card, the reader has already been told
                something is missing. What they had not been told is which file
                closes it — the card's button used to land them on a tab of 984
                rows to work that out for themselves. */}
            {brief && (
              <div className="smart-import-brief">
                <b>{brief.title}</b>
                <p>{brief.detail}</p>
                {brief.steps?.length > 0 && (
                  <ol>{brief.steps.map(step => <li key={step}>{step}</li>)}</ol>
                )}
              </div>
            )}
            <p className="smart-import-blurb">
              Drop one or more CSVs, screenshots, or PDF statements of your
              positions — add several at once. AI extracts the rows; you review
              and confirm before anything is saved. Prefer to type them in? Use{' '}
              <strong>Enter manually</strong>.
            </p>
            {/* The single highest-value thing a new user can upload, and the
                one they are least likely to know exists. A holdings import
                cannot see anything you have already sold; an activity export
                can. */}
            <p className="smart-import-blurb">
              Want returns that account for closed positions, dividends and
              deposits? Import your broker's <strong>activity export</strong> —
              Serin reads Robinhood's exactly, no AI involved.{' '}
              <a href="/exports" target="_blank" rel="noreferrer">
                Where to download it →
              </a>
            </p>

            <div
              className={`smart-dropzone ${dragOver ? 'drag-over' : ''} ${files.length ? 'has-file' : ''}`}
              onDragOver={event => { event.preventDefault(); setDragOver(true); }}
              onDragLeave={() => setDragOver(false)}
              onDrop={onDrop}
              onClick={() => fileInputRef.current?.click()}
            >
              <input
                ref={fileInputRef}
                type="file"
                accept={ACCEPTED}
                multiple
                style={{ display: 'none' }}
                onChange={event => { addFiles(event.target.files); event.target.value = ''; }}
              />
              <strong>{files.length ? 'Drop more, or click to add' : 'Drop files here, or click to browse'}</strong>
              <span className="smart-dropzone-meta">CSV · TSV · TXT · PDF · PNG · JPG · WEBP · multiple allowed</span>
            </div>

            {files.length > 0 && (
              <ul className="smart-file-list">
                {files.map((f, i) => (
                  <li key={`${f.name}:${f.size}:${i}`} className="smart-file-item">
                    <span className="smart-file-name">{f.name}</span>
                    <span className="smart-file-size">{(f.size / 1024).toFixed(1)} KB</span>
                    <button
                      type="button"
                      className="smart-file-remove"
                      aria-label={`Remove ${f.name}`}
                      onClick={() => removeFile(i)}
                    >
                      ×
                    </button>
                  </li>
                ))}
              </ul>
            )}

            <input
              className="smart-import-hint"
              type="text"
              placeholder="Optional hint to the model — e.g. 'broker: Fidelity Roth IRA'"
              value={hint}
              onChange={event => setHint(event.target.value)}
            />

            <div className="smart-import-notice" role="note">
              <span aria-hidden="true">🔒</span>
              <span>
                Recognised broker exports are parsed on this server and go
                nowhere else. Everything else is sent to a cloud AI provider
                (configured in the AI briefing connector) for parsing —{' '}
                <strong>crop or redact anything sensitive</strong> in those:
                account numbers, names, addresses.
              </span>
            </div>

            {error && <div className="smart-import-error">{error}</div>}

            <div className="smart-import-actions">
              <button className="btn btn-ghost" onClick={onClose}>Cancel</button>
              <div className="smart-import-actions-right">
                <button className="btn btn-ghost" onClick={startManual}>Enter manually</button>
                <button
                  className="btn btn-primary"
                  disabled={busy === 'extract' || files.length === 0}
                  onClick={runExtract}
                >
                  {busy === 'extract'
                    ? (progress ? `Extracting ${progress.current}/${progress.total}…` : 'Extracting…')
                    : `Extract positions${files.length > 1 ? ` (${files.length} files)` : ''}`}
                </button>
              </div>
            </div>
          </div>
        )}

        {stage === 'reviewing' && extract && (
          <div className="smart-import-review">
            <div className="smart-import-summary">
              {extract.manual ? (
                <>Fill in the template below — edit any cell and add rows as needed. Nothing is saved until you import.</>
              ) : (
                <>
                  Found <strong>{rows.length}</strong> position{rows.length === 1 ? '' : 's'}
                  {txns.length > 0 && (
                    <> and <strong>{txns.length}</strong> transaction{txns.length === 1 ? '' : 's'}</>
                  )}
                  {extract.fileCount > 1 ? <> across <strong>{extract.fileCount}</strong> files</> : null}
                  {extract.notes && <div className="smart-import-notes">{extract.notes}</div>}
                  {extract.skipped?.length > 0 && (
                    <div className="smart-import-skipped" role="note">
                      <strong>
                        {extract.skipped.length === 1
                          ? '1 row was left out rather than guessed at:'
                          : `${extract.skipped.length} rows were left out rather than guessed at:`}
                      </strong>
                      <ul>
                        {[...new Map(extract.skipped.map(r => [r.code + r.reason, r])).values()]
                          .slice(0, 6)
                          .map((row, i) => (
                            <li key={i}>
                              <code>{row.code || '—'}</code> — {row.reason}
                            </li>
                          ))}
                      </ul>
                      Send an unknown code to support@serin.money and it gets added.
                    </div>
                  )}
                </>
              )}
            </div>

            <div className="smart-import-table-wrap">
              <table className="smart-import-table">
                <thead>
                  <tr>
                    <th>
                      <input
                        type="checkbox"
                        checked={selected.size === rows.length && rows.length > 0}
                        onChange={toggleAll}
                      />
                    </th>
                    <th>Symbol</th>
                    <th>Name</th>
                    <th>Broker</th>
                    <th>Type</th>
                    <th className="num">Qty</th>
                    <th className="num">Avg cost</th>
                    <th>Warnings</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row, idx) => {
                    const hasWarnings = row.warnings && row.warnings.length > 0;
                    return (
                      <React.Fragment key={`${row._source || 'row'}:${idx}`}>
                        <tr className={`smart-row ${hasWarnings ? 'has-warning' : ''} ${selected.has(idx) ? 'selected' : ''}`}>
                          <td>
                            <input
                              type="checkbox"
                              checked={selected.has(idx)}
                              onChange={() => toggleRow(idx)}
                            />
                          </td>
                          <td>
                            <input
                              className="smart-cell-input mono"
                              value={row.symbol}
                              onChange={event => updateRow(idx, 'symbol', event.target.value.toUpperCase())}
                            />
                          </td>
                          <td>
                            <input
                              className="smart-cell-input"
                              value={row.name}
                              onChange={event => updateRow(idx, 'name', event.target.value)}
                            />
                          </td>
                          <td>
                            <select
                              className="smart-cell-input"
                              value={normalizeBroker(row.broker)}
                              onChange={event => updateRow(idx, 'broker', event.target.value)}
                            >
                              {!row.broker && <option value="">— select —</option>}
                              {brokerOptions([row.broker, ...availableBrokers]).map(b => (
                                <option key={b} value={b}>{brokerLabel(b)}</option>
                              ))}
                            </select>
                          </td>
                          <td>
                            <select
                              className="smart-cell-input"
                              value={row.asset_type}
                              onChange={event => updateRow(idx, 'asset_type', event.target.value)}
                            >
                              {ASSET_TYPES.map(t => <option key={t} value={t}>{t}</option>)}
                            </select>
                          </td>
                          <td className="num">
                            <input
                              className="smart-cell-input num"
                              type="number"
                              step="any"
                              value={row.quantity}
                              onChange={event => updateRow(idx, 'quantity', parseFloat(event.target.value) || 0)}
                            />
                          </td>
                          <td className="num">
                            <input
                              className="smart-cell-input num"
                              type="number"
                              step="any"
                              value={row.average_cost}
                              onChange={event => updateRow(idx, 'average_cost', parseFloat(event.target.value) || 0)}
                            />
                          </td>
                          <td className="smart-warnings">
                            {hasWarnings ? row.warnings.map((w, i) => (
                              <span key={i} className="warning-chip">{w}</span>
                            )) : <span className="muted-cell">—</span>}
                          </td>
                        </tr>
                        {(row.tax_lots || []).length > 0 && (
                          <tr className={`smart-tax-lot-row ${selected.has(idx) ? 'selected' : ''}`}>
                            <td colSpan="8">
                              <section className="smart-tax-lots" aria-label={`${row.symbol} tax lots`}>
                                <div className="smart-tax-lots-head">
                                  <div>
                                    <strong>{row.tax_lots.length} tax lot{row.tax_lots.length === 1 ? '' : 's'} detected</strong>
                                    <span>Review purchase dates, shares, and per-share cost before importing.</span>
                                  </div>
                                  <button type="button" className="btn btn-ghost btn-sm" onClick={() => addTaxLot(idx)}>+ Add lot</button>
                                </div>
                                <div className="smart-tax-lot-list">
                                  {row.tax_lots.map((lot, lotIdx) => (
                                    <div className="smart-tax-lot" key={lotIdx}>
                                      <label>
                                        <span>Purchase date</span>
                                        <input
                                          type="date"
                                          value={lot.acquired_at || ''}
                                          onChange={event => updateTaxLot(idx, lotIdx, 'acquired_at', event.target.value)}
                                        />
                                      </label>
                                      <label>
                                        <span>Shares</span>
                                        <input
                                          type="number"
                                          step="any"
                                          value={lot.quantity}
                                          onChange={event => updateTaxLot(idx, lotIdx, 'quantity', parseFloat(event.target.value) || 0)}
                                        />
                                      </label>
                                      <label>
                                        <span>Price paid / share</span>
                                        <input
                                          type="number"
                                          step="any"
                                          value={lot.cost_basis}
                                          onChange={event => updateTaxLot(idx, lotIdx, 'cost_basis', parseFloat(event.target.value) || 0)}
                                        />
                                      </label>
                                      <button
                                        type="button"
                                        className="smart-tax-lot-remove"
                                        aria-label={`Remove tax lot ${lotIdx + 1}`}
                                        onClick={() => removeTaxLot(idx, lotIdx)}
                                      >×</button>
                                    </div>
                                  ))}
                                </div>
                              </section>
                            </td>
                          </tr>
                        )}
                      </React.Fragment>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <button type="button" className="btn btn-ghost btn-sm smart-add-row" onClick={addRow}>
              + Add row
            </button>

            {txns.length > 0 && (
              <section className="smart-txn-review">
                <h3>Transactions</h3>
                <p className="smart-txn-blurb">
                  This is what makes returns transaction-accurate rather than estimated —
                  it is how Serin learns about deposits, withdrawals, and positions you
                  have already sold. Re-importing the same statement is safe; rows
                  already on record are skipped.
                </p>
                <div className="smart-import-table-wrap">
                  <table className="smart-import-table">
                    <thead>
                      <tr>
                        <th>
                          <input
                            type="checkbox"
                            checked={txnSelected.size === txns.length && txns.length > 0}
                            onChange={() => setTxnSelected(
                              txnSelected.size === txns.length
                                ? new Set()
                                : new Set(txns.map((_, i) => i))
                            )}
                          />
                        </th>
                        <th>Date</th>
                        <th>Action</th>
                        <th>Symbol</th>
                        <th>Quantity</th>
                        <th>Price / amount</th>
                        <th>Fee</th>
                      </tr>
                    </thead>
                    <tbody>
                      {txns.map((txn, idx) => (
                        <tr key={idx} className={txnSelected.has(idx) ? '' : 'row-muted'}>
                          <td>
                            <input
                              type="checkbox"
                              checked={txnSelected.has(idx)}
                              onChange={() => setTxnSelected(prev => {
                                const next = new Set(prev);
                                if (next.has(idx)) next.delete(idx); else next.add(idx);
                                return next;
                              })}
                            />
                          </td>
                          <td>
                            <input
                              type="date"
                              value={txn.occurred_at || ''}
                              onChange={e => updateTxn(idx, 'occurred_at', e.target.value)}
                            />
                          </td>
                          <td>
                            <select
                              value={txn.action || 'buy'}
                              onChange={e => updateTxn(idx, 'action', e.target.value)}
                            >
                              {TXN_ACTIONS.map(a => <option key={a} value={a}>{a}</option>)}
                            </select>
                          </td>
                          <td>
                            <input
                              value={txn.symbol || ''}
                              placeholder="—"
                              onChange={e => updateTxn(idx, 'symbol', e.target.value.toUpperCase())}
                            />
                          </td>
                          <td>
                            <input
                              type="number" step="any"
                              value={txn.quantity ?? 0}
                              onChange={e => updateTxn(idx, 'quantity', parseFloat(e.target.value) || 0)}
                            />
                          </td>
                          <td>
                            <input
                              type="number" step="any"
                              value={txn.price ?? 0}
                              onChange={e => updateTxn(idx, 'price', parseFloat(e.target.value) || 0)}
                            />
                          </td>
                          <td>
                            <input
                              type="number" step="any"
                              value={txn.fee ?? 0}
                              onChange={e => updateTxn(idx, 'fee', parseFloat(e.target.value) || 0)}
                            />
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </section>
            )}

            <label className="smart-import-replace">
              <input
                type="checkbox"
                checked={replace}
                onChange={event => setReplace(event.target.checked)}
              />
              <span>Replace existing positions on duplicate (otherwise skip)</span>
            </label>

            {error && <div className="smart-import-error">{error}</div>}

            <div className="smart-import-actions">
              <button className="btn btn-ghost" onClick={() => setStage('intake')}>← Back</button>
              <button
                className="btn btn-primary"
                disabled={busy === 'import' || importable === 0}
                onClick={runImport}
              >
                {busy === 'import' ? 'Importing…' : `Import ${importable}`}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
