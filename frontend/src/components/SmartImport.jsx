import React, { useCallback, useRef, useState } from 'react';
import { api } from '../api.js';

const ACCEPTED = '.csv,.tsv,.txt,.pdf,application/pdf,image/png,image/jpeg,image/webp,image/gif';
const ASSET_TYPES = ['stock', 'etf', 'crypto', 'cash', 'option'];
const COMMON_BROKERS = [
  'robinhood', 'etrade', 'fidelity', 'schwab', 'vanguard',
  'webull', 'ibkr', 'sofi', 'coinbase', 'manual',
];

/**
 * Smart Import modal — drop one or more files (CSV / images), or fill in a
 * blank template manually → AI extracts positions from each file → user reviews
 * the merged preview table → confirm to write to DB.
 *
 * The extract endpoint is idempotent; nothing reaches the DB until the user
 * clicks "Import".
 */
export function SmartImport({ onClose, onImported, addToast, brokers = [] }) {
  // Broker is a free-form field in Serin, so offer a dropdown of known brokers
  // (the user's existing ones first, then common defaults) while still allowing
  // a typed custom value via <datalist>.
  const brokerOptions = [...new Set(
    [...brokers, ...COMMON_BROKERS].map(b => String(b || '').trim()).filter(Boolean)
  )];
  const [stage, setStage] = useState('intake'); // intake | reviewing | importing
  const [files, setFiles] = useState([]);
  const [hint, setHint] = useState('');
  const [dragOver, setDragOver] = useState(false);
  const [extract, setExtract] = useState(null);
  const [rows, setRows] = useState([]);
  const [selected, setSelected] = useState(new Set());
  const [replace, setReplace] = useState(false);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState('');
  const [progress, setProgress] = useState(null); // { current, total } while extracting
  const fileInputRef = useRef(null);

  // Append files, de-duping by name+size so an accidental double-drop is a no-op.
  const addFiles = useCallback(list => {
    const incoming = Array.from(list || []);
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
      let totalCost = 0;
      const models = new Set();
      const notesList = [];
      for (let i = 0; i < files.length; i += 1) {
        setProgress({ current: i + 1, total: files.length });
        const form = new FormData();
        form.append('file', files[i]);
        if (hint.trim()) form.append('hint', hint.trim());
        const result = await fetch('/api/v1/import/extract', { method: 'POST', body: form });
        if (!result.ok) {
          const text = await result.text();
          throw new Error(`${files[i].name}: ${extractError(text) || `${result.status} ${result.statusText}`}`);
        }
        const payload = await result.json();
        (payload.rows || []).forEach(row => allRows.push({ ...row, _source: files[i].name }));
        totalCost += payload.cost_usd || 0;
        if (payload.model) models.add(payload.model);
        if (payload.notes) notesList.push(payload.notes);
      }
      setExtract({
        model: [...models].join(', ') || 'ai',
        cost_usd: totalCost,
        fileCount: files.length,
        notes: notesList.join(' · '),
      });
      setRows(allRows);
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

  async function runImport() {
    const chosen = rows
      .filter((_, idx) => selected.has(idx))
      .filter(row => row.symbol && String(row.symbol).trim());
    if (chosen.length === 0) {
      setError('Add at least one row with a symbol.');
      return;
    }
    setError('');
    setBusy('import');
    try {
      const result = await api('/api/v1/positions/bulk', {
        method: 'POST',
        body: JSON.stringify({ rows: chosen, replace }),
      });
      const insertedMsg = `Imported ${result.inserted} position${result.inserted === 1 ? '' : 's'}`;
      const skippedMsg = result.skipped > 0 ? ` · skipped ${result.skipped}` : '';
      addToast?.('success', insertedMsg + skippedMsg);
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
    setExtract({ model: 'manual entry', cost_usd: 0, manual: true });
    setStage('reviewing');
  }

  function addRow() {
    setRows(prev => [...prev, blankRow()]);
    setSelected(prev => new Set([...prev, rows.length]));
  }

  // Rows that will actually be written: selected AND have a symbol (blank
  // template rows are ignored so they don't produce junk positions).
  const importable = rows.filter((row, idx) => selected.has(idx) && row.symbol && String(row.symbol).trim()).length;

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal smart-import-modal" onClick={event => event.stopPropagation()}>
        <div className="smart-import-head">
          <h2>Smart Import</h2>
          <button className="btn btn-ghost btn-sm" onClick={onClose}>Close</button>
        </div>

        {stage === 'intake' && (
          <div className="smart-import-intake">
            <p className="smart-import-blurb">
              Drop one or more CSVs, screenshots, or PDF statements of your
              positions — add several at once. AI extracts the rows; you review
              and confirm before anything is saved. Prefer to type them in? Use{' '}
              <strong>Enter manually</strong>.
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
                Your content will be sent to a cloud AI provider (configured in
                the AI briefing connector) for parsing. <strong>Crop or redact
                anything sensitive</strong> — account numbers, names, addresses —
                before uploading.
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
                  {extract.fileCount > 1 ? <> across <strong>{extract.fileCount}</strong> files</> : null} ·
                  {' '}<span className="muted-cell">{extract.model}</span> ·
                  {' '}<span className="muted-cell">~${extract.cost_usd?.toFixed?.(4) ?? '0.0000'}</span>
                  {extract.notes && <div className="smart-import-notes">{extract.notes}</div>}
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
                      <tr key={idx} className={`smart-row ${hasWarnings ? 'has-warning' : ''} ${selected.has(idx) ? 'selected' : ''}`}>
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
                            value={row.broker || ''}
                            onChange={event => updateRow(idx, 'broker', event.target.value)}
                          >
                            {!row.broker && <option value="">— select —</option>}
                            {[...new Set([row.broker, ...brokerOptions].filter(Boolean))].map(b => (
                              <option key={b} value={b}>{b}</option>
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
                    );
                  })}
                </tbody>
              </table>
            </div>
            <button type="button" className="btn btn-ghost btn-sm smart-add-row" onClick={addRow}>
              + Add row
            </button>

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

function extractError(text) {
  if (!text) return '';
  try {
    const parsed = JSON.parse(text);
    return typeof parsed.detail === 'string' ? parsed.detail : text;
  } catch {
    return text;
  }
}
