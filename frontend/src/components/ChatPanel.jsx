import React, { useCallback, useEffect, useRef, useState } from 'react';
import { api, getAuthToken } from '../api.js';
import { MarkdownRenderer } from './Markdown.jsx';

// Chat is pack-driven, exactly as the X-ray is: this file holds no prompt, no
// model call and no entitlement logic. It probes one endpoint and renders
// whatever comes back.
//
//   absent (404)        → no tab, no launcher, no trace
//   present, unlicensed → the pack's own upsell copy
//   present, licensed   → the conversation, streamed from the pack
//
// The probe response carries the URLs rather than this file hardcoding them,
// so the wire protocol stays the pack's business: core knows "ask, then read
// the stream you are handed", which is the least it can know and still draw a
// chat window.
//
// The stream is newline-delimited JSON events, one per `data:` line:
//   {"type":"token","text":"…"}   incremental assistant text
//   {"type":"tool","name":"…"}    a tool the model called, shown as a chip
//   {"type":"error","message":"…"}
//   {"type":"done"}
// Anything unrecognised is ignored, so the pack can add event kinds without
// waiting for a core release.
//
// One conversation, two surfaces. The state lives in this hook — called once
// in App and handed to both the tab and the floating dock — because with
// history persisted, two independent threads on the same screen would be a
// bug the user could see.

export function useChat() {
  const [probe, setProbe] = useState({ status: 'loading' });
  const [messages, setMessages] = useState([]);
  const [streaming, setStreaming] = useState(false);
  const [error, setError] = useState('');
  const abortRef = useRef(null);
  // The send closure would otherwise capture whichever messages array existed
  // when it was built, and replay a stale conversation to the model.
  const messagesRef = useRef(messages);
  messagesRef.current = messages;

  const reload = useCallback(() => {
    let alive = true;
    api('/api/connectors/chat/run', { method: 'POST', body: JSON.stringify({}) })
      .then(data => alive && setProbe({ status: 'ok', data }))
      .catch(err => alive && setProbe({ status: err.status === 404 ? 'absent' : 'error' }));
    return () => { alive = false; };
  }, []);

  useEffect(() => reload(), [reload]);

  // Past conversation, once we know where to ask for it.
  const historyUrl = probe.data?.entitled ? probe.data?.history_url : null;
  useEffect(() => {
    if (!historyUrl) return undefined;
    let alive = true;
    api(historyUrl)
      .then(body => alive && setMessages(body.messages || []))
      .catch(() => {/* no history is a fine state — say nothing */});
    return () => { alive = false; };
  }, [historyUrl]);

  useEffect(() => () => abortRef.current?.abort(), []);

  const send = useCallback(async text => {
    const question = (text || '').trim();
    if (!question || streaming) return;
    const streamUrl = probe.data?.stream_url;
    if (!streamUrl) return;

    const history = [...messagesRef.current, { role: 'user', content: question }];
    setMessages([...history, { role: 'assistant', content: '', tools: [] }]);
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
      if (!response.ok || !response.body) throw new Error(`Chat is unavailable (${response.status}).`);

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
  }, [probe.data, streaming]);

  const clear = useCallback(async () => {
    const historyPath = probe.data?.history_url;
    setMessages([]);
    setError('');
    if (historyPath) {
      try { await api(historyPath, { method: 'DELETE' }); } catch { /* already gone */ }
    }
  }, [probe.data]);

  const stop = useCallback(() => abortRef.current?.abort(), []);

  return { ...probe, messages, streaming, error, send, clear, stop, reload };
}

function Message({ message }) {
  const content = message.content || '';
  // An assistant turn exists from the moment it is asked for, and is empty
  // until the first token lands. Handing that to the markdown renderer made it
  // answer "Nothing to show yet." — which reads as a reply. The composer's
  // thinking indicator already says what is happening.
  //
  // Which tools ran is not shown. It is the model's plumbing, not the answer,
  // and a chip reading `get_performance` asks the reader to care about an
  // implementation detail. The names are still streamed and stored, so they
  // remain available for diagnostics.
  if (message.role === 'assistant' && !content) return null;

  return (
    <div className={`chat-message chat-message-${message.role}`}>
      {message.role === 'assistant'
        ? <MarkdownRenderer content={content} />
        : <p>{content}</p>}
    </div>
  );
}

function Conversation({ chat, compact = false }) {
  const [draft, setDraft] = useState('');
  const bottomRef = useRef(null);
  const { messages, streaming, error } = chat;
  const retention = chat.data?.retention_days;

  useEffect(() => { bottomRef.current?.scrollIntoView({ block: 'end' }); }, [messages, streaming]);

  const submit = event => {
    event.preventDefault();
    const text = draft;
    setDraft('');
    chat.send(text);
  };

  return (
    <section className={`panel chat-panel${compact ? ' chat-panel-compact' : ''}`}>
      {messages.length > 0 && (
        <div className="chat-toolbar">
          <span className="chat-retention">
            {retention ? `Kept ${retention} days` : ''}
          </span>
          <button type="button" className="btn btn-ghost btn-tiny" onClick={chat.clear}>
            Clear history
          </button>
        </div>
      )}

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

      <form className="chat-composer" onSubmit={submit}>
        <textarea
          value={draft}
          onChange={event => setDraft(event.target.value)}
          onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey) submit(event); }}
          placeholder="Ask about your portfolio…"
          rows={2}
          disabled={streaming}
        />
        {streaming
          ? <button type="button" className="btn btn-ghost" onClick={chat.stop}>Stop</button>
          : <button type="submit" className="btn btn-primary" disabled={!draft.trim()}>Send</button>}
      </form>

      <p className="chat-disclaimer">
        {chat.data?.disclaimer || 'Portfolio context and organisation — never trade directives.'}
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

export function ChatView({ chat, compact = false }) {
  if (chat.status === 'loading') return <section className="panel"><div className="empty-box">Loading…</div></section>;
  if (chat.status !== 'ok') {
    return <section className="panel"><div className="empty-box">Chat is unavailable — is the Intelligence pack installed?</div></section>;
  }
  if (!chat.data.entitled) return <ChatUpsell message={chat.data.message} />;
  return <Conversation chat={chat} compact={compact} />;
}

// A one-tap way into chat from anywhere. The tab still exists; this is for the
// moment you are looking at a holding and want to ask about it, where walking
// to the nav and losing the page is the whole friction.

function ChatIcon() {
  return (
    <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor"
         strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M21 11.5a8.4 8.4 0 0 1-9 8.4 9.9 9.9 0 0 1-3.6-.7L3 21l1.9-4.9A8.3 8.3 0 0 1 3.6 11.5a8.4 8.4 0 0 1 8.7-8.4 8.4 8.4 0 0 1 8.7 8.4z" />
    </svg>
  );
}

export function ChatLauncher({ chat, hidden = false }) {
  const [open, setOpen] = useState(false);
  const [everOpened, setEverOpened] = useState(false);

  useEffect(() => {
    if (!open) return undefined;
    const onKey = event => { if (event.key === 'Escape') setOpen(false); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open]);

  if (chat.status !== 'ok' || hidden) return null;

  return (
    <>
      {everOpened && (
        <div className="chat-dock" hidden={!open} role="dialog" aria-label="Chat">
          <div className="chat-dock-head">
            <strong>Chat</strong>
            <button type="button" className="btn btn-ghost btn-sm" onClick={() => setOpen(false)}>
              Close
            </button>
          </div>
          <ChatView chat={chat} compact />
        </div>
      )}
      <button
        type="button"
        className={`chat-fab${open ? ' chat-fab-open' : ''}`}
        onClick={() => { setEverOpened(true); setOpen(value => !value); }}
        aria-label={open ? 'Close chat' : 'Ask about your portfolio'}
        aria-expanded={open}
      >
        {open ? '×' : <ChatIcon />}
      </button>
    </>
  );
}

export default ChatView;
