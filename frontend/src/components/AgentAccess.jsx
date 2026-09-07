import React, { useCallback, useEffect, useState } from 'react';
import { api } from '../api.js';

// Agent access: the tokens an MCP client or an OpenAPI-speaking framework uses
// to read this portfolio, and the setup snippet that goes with them.
//
// Two things this panel exists to make impossible to get wrong. A token is
// shown exactly once — it is stored hashed, so "copy it later" is not an
// option and the UI has to say so at the moment it matters. And the scope is
// stated plainly next to the button: these reach /api/agent and nothing else,
// which is what makes handing one to a third-party client reasonable at all.

const CONFIG_SNIPPET = (token, origin) => `{
  "mcpServers": {
    "serin": {
      "command": "python",
      "args": ["-m", "backend.mcp_server"],
      "env": {
        "SERIN_URL": "${origin}",
        "SERIN_AGENT_TOKEN": "${token || 'serin_at_…'}"
      }
    }
  }
}`;

function useAgentTokens() {
  const [state, setState] = useState({ status: 'loading', tokens: [] });

  const reload = useCallback(() => {
    let alive = true;
    api('/api/settings/agent-tokens')
      .then(data => alive && setState({ status: 'ok', ...data }))
      .catch(err => alive && setState({ status: 'error', message: err.message, tokens: [] }));
    return () => { alive = false; };
  }, []);

  useEffect(() => reload(), [reload]);
  return { ...state, reload };
}

function TokenReveal({ token, onDismiss }) {
  const [copied, setCopied] = useState(false);
  const copy = () => {
    navigator.clipboard?.writeText(token).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }).catch(() => {});
  };
  return (
    <div className="agent-token-reveal">
      <p className="agent-token-warning">
        <strong>Copy this now.</strong> Serin stores only a hash of it, so this is
        the only time it can be shown.
      </p>
      <code className="agent-token-value">{token}</code>
      <div className="card-actions">
        <button type="button" className="btn btn-primary btn-sm" onClick={copy}>
          {copied ? 'Copied' : 'Copy token'}
        </button>
        <button type="button" className="btn btn-ghost btn-sm" onClick={onDismiss}>
          Done
        </button>
      </div>
    </div>
  );
}

export function AgentAccess({ addToast }) {
  const { status, tokens = [], message, reload } = useAgentTokens();
  const [name, setName] = useState('');
  const [busy, setBusy] = useState(false);
  const [issued, setIssued] = useState(null);
  const origin = typeof window === 'undefined' ? 'http://127.0.0.1:8890' : window.location.origin;

  const create = async event => {
    event.preventDefault();
    setBusy(true);
    try {
      const created = await api('/api/settings/agent-tokens', {
        method: 'POST',
        body: JSON.stringify({ name: name.trim() || 'Agent token' }),
      });
      setIssued(created.token);
      setName('');
      reload();
    } catch (err) {
      addToast?.('error', err.message);
    } finally {
      setBusy(false);
    }
  };

  const revoke = async token => {
    try {
      await api(`/api/settings/agent-tokens/${token.id}`, { method: 'DELETE' });
      addToast?.('success', `Revoked “${token.name}”.`);
      reload();
    } catch (err) {
      addToast?.('error', err.message);
    }
  };

  return (
    <section className="panel agent-access">
      <div className="panel-head">
        <div>
          <h3 className="panel-title">Agent access</h3>
          <p className="panel-sub">
            Let an AI assistant read this portfolio — Claude Desktop, Claude Code, or
            anything that speaks MCP or OpenAPI. Nothing leaves your server except
            the answers you ask for.
          </p>
        </div>
      </div>

      {status === 'error' && <p className="panel-note">{message}</p>}

      <form className="agent-token-form" onSubmit={create}>
        <label className="field">
          <span className="form-label">Token name</span>
          <input
            value={name}
            onChange={event => setName(event.target.value)}
            placeholder="Claude Desktop"
            maxLength={80}
          />
        </label>
        <button type="submit" className="btn btn-primary" disabled={busy}>
          {busy ? 'Creating…' : 'Create token'}
        </button>
      </form>
      <p className="panel-note">
        Read-only, and scoped to <code>/api/agent</code> — a token cannot change
        a position, download a backup, or create another token. It reads your
        account and no one else’s.
      </p>

      {issued && <TokenReveal token={issued} onDismiss={() => setIssued(null)} />}

      {status === 'ok' && tokens.length > 0 && (
        <ul className="agent-token-list">
          {tokens.map(token => (
            <li key={token.id} className="agent-token-row">
              <div>
                <strong>{token.name}</strong>
                <span className="muted-cell">
                  {' '}created {(token.created_at || '').slice(0, 10)}
                  {token.last_used_at
                    ? ` · last used ${token.last_used_at.slice(0, 10)}`
                    : ' · never used'}
                </span>
              </div>
              <button type="button" className="btn btn-danger btn-sm" onClick={() => revoke(token)}>
                Revoke
              </button>
            </li>
          ))}
        </ul>
      )}

      {status === 'ok' && tokens.length === 0 && (
        <div className="empty-box">No agent tokens yet.</div>
      )}

      <details className="agent-setup">
        <summary>Connect an assistant</summary>
        <p className="panel-note">
          If your client supports remote MCP servers, point it straight at this
          URL with the token as a bearer credential — nothing to install:
        </p>
        <pre className="connector-mono">{`${origin}/api/agent/mcp`}</pre>
        <p className="panel-note">
          Otherwise run the bridge locally from a checkout of Serin, and add
          this to your MCP client’s config:
        </p>
        <pre className="connector-mono">{CONFIG_SNIPPET(issued, origin)}</pre>
        <p className="panel-note">
          Not using MCP? The same tools are plain HTTP under{' '}
          <code>/api/agent</code>, published in <code>/openapi.json</code>. For an
          agent with no tool support at all, <code>/api/agent/context.md</code>{' '}
          returns the whole portfolio as Markdown.
        </p>
      </details>
    </section>
  );
}

export default AgentAccess;
