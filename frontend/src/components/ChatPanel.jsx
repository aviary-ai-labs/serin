import React, { useCallback, useEffect, useRef, useState } from 'react';
import { api, getAuthToken } from '../api.js';
import { MarkdownRenderer } from './Markdown.jsx';

// Chat is pack-driven, exactly as the X-ray is: this file holds no prompt, no
// model call and no entitlement logic. It probes one endpoint and renders
// whatever comes back.
//
//   absent (404)      → no tab, no teaser, no trace
//   present, unlicensed → the pack's own upsell copy
//   present, licensed   → the conversation, streamed from the pack
//
// The probe response carries the stream URL rather than this file hardcoding
// one. That keeps the wire protocol the pack's business: core knows "ask, then
// read the stream you are handed", which is the least it can know and still
// draw a chat window.
//
// The stream is newline-delimited JSON events, one per `data:` line:
//   {"type":"token","text":"…"}   incremental assistant text
//   {"type":"tool","name":"…"}    a tool the model called, shown as a chip
//   {"type":"error","message":"…"}
//   {"type":"done"}
// Anything unrecognised is ignored, so the pack can add event kinds without
// waiting for a core release.

export function useChat() {
  const [state, setState] = useState({ status: 'loading' });

  const reload = useCallback(() => {
    let alive = true;
    api('/api/connectors/chat/run', { method: 'POST', body: JSON.stringify({}) })
      .then(data => alive && setState({ status: 'ok', data }))
      .catch(err => alive && setState({ status: err.status === 404 ? 'absent' : 'error' }));
    return () => { alive = false; };
  }, []);

  useEffect(() => reload(), [reload]);
  return { ...state, reload };
}

function ToolChip({ name }) {
  return <span className="chat-tool-chip" title={`Serin ran ${name}`}>{name}</span>;
}

function Message({ message }) {
  return (
    <div className={`chat-message chat-message-${message.role}`}>
      {message.tools?.length > 0 && (
        <div className="chat-tools">
          {message.tools.map((tool, index) => <ToolChip key={`${tool}-${index}`} name={tool} />)}
        </div>
      )}
      {message.role === 'assistant'
        ? <MarkdownRenderer content={message.content || ''} />
        : <p>{message.content}</p>}
    </div>
  );
}

function Conversation({ streamUrl, disclaimer }) {
  const [messages, setMessages] = useState([]);
  const [draft, setDraft] = useState('');
  const [streaming, setStreaming] = useState(false);
  const [error, setError] = useState('');
  const abortRef = useRef(null);
  const bottomRef = useRef(null);

  useEffect(() => () => abortRef.current?.abort(), []);
  useEffect(() => { bottomRef.current?.scrollIntoView({ block: 'end' }); }, [messages, streaming]);

  const send = async event => {
    event.preventDefault();
    const question = draft.trim();
    if (!question || streaming) return;

    const history = [...messages, { role: 'user', content: question }];
    setMessages([...history, { role: 'assistant', content: '', tools: [] }]);
    setDraft('');
    setError('');
    setStreaming(true);

    const controller = new AbortController();
    abortRef.current = controller;

    const patchLast = update => setMessages(current => {
      const next = [...current];
      next[next.length - 1] = { ...next[next.length - 1], ...update(next[next.length - 1]) };
      return next;
    });

    try {
      const token = getAuthToken();
      const response = await fetch(streamUrl, {
        method: 'POST',
        signal: controller.signal,
        headers: {
          'Content-Type': 'application/json',
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify({ messages: history }),
      });
      if (!response.ok || !response.body) {
        throw new Error(`Chat is unavailable (${response.status}).`);
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      // Read line-wise rather than chunk-wise: a chunk boundary lands in the
      // middle of a JSON event often enough that parsing chunks drops tokens.
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() ?? '';
        for (const line of lines) {
          const payload = line.startsWith('data:') ? line.slice(5).trim() : line.trim();
          if (!payload) continue;
          let event;
          try { event = JSON.parse(payload); } catch { continue; }
          if (event.type === 'token') {
            patchLast(last => ({ content: (last.content || '') + (event.text || '') }));
          } else if (event.type === 'tool') {
            patchLast(last => ({ tools: [...(last.tools || []), event.name] }));
          } else if (event.type === 'error') {
            setError(event.message || 'Chat failed.');
          }
        }
      }
    } catch (err) {
      if (err.name !== 'AbortError') setError(err.message);
    } finally {
      setStreaming(false);
      abortRef.current = null;
    }
  };

  const stop = () => abortRef.current?.abort();

  return (
    <section className="panel chat-panel">
      <div className="chat-log">
        {messages.length === 0 && (
          <div className="empty-box chat-empty">
            Ask about your holdings, returns, or realised gains. Serin reads your
            portfolio to answer — it does not give financial advice.
          </div>
        )}
        {messages.map((message, index) => <Message key={index} message={message} />)}
        {streaming && <div className="chat-thinking">Thinking…</div>}
        <div ref={bottomRef} />
      </div>

      {error && <p className="panel-note chat-error">{error}</p>}

      <form className="chat-composer" onSubmit={send}>
        <textarea
          value={draft}
          onChange={event => setDraft(event.target.value)}
          onKeyDown={event => {
            if (event.key === 'Enter' && !event.shiftKey) send(event);
          }}
          placeholder="Ask about your portfolio…"
          rows={2}
          disabled={streaming}
        />
        {streaming
          ? <button type="button" className="btn btn-ghost" onClick={stop}>Stop</button>
          : <button type="submit" className="btn btn-primary" disabled={!draft.trim()}>Send</button>}
      </form>

      <p className="chat-disclaimer">
        {disclaimer || 'Portfolio context and organisation — never trade directives.'}
      </p>
    </section>
  );
}

function ChatUpsell({ message }) {
  return (
    <section className="panel chat-upsell">
      <h3 className="panel-title">Chat</h3>
      <p className="panel-sub">
        {message || 'Chat is a Serin Intelligence feature — add a license key to unlock it.'}
      </p>
      <p className="panel-note">
        Prefer your own assistant? The same portfolio tools are available free over
        MCP and plain HTTP — see <strong>Connectors → Agent access</strong>.
      </p>
    </section>
  );
}

export function ChatView({ chat }) {
  if (chat.status === 'loading') return <section className="panel"><div className="empty-box">Loading…</div></section>;
  if (chat.status !== 'ok') {
    return <section className="panel"><div className="empty-box">Chat is unavailable — is the Intelligence pack installed?</div></section>;
  }
  const { data } = chat;
  if (!data.entitled) return <ChatUpsell message={data.message} />;
  return <Conversation streamUrl={data.stream_url} disclaimer={data.disclaimer} />;
}

export default ChatView;
