import React from 'react';

function renderItalics(text, keyBase) {
  const parts = [];
  String(text).split(/(\*[^*\s][^*]*\*)/g).forEach((part, index) => {
    if (!part) return;
    if (part.length > 2 && part.startsWith('*') && part.endsWith('*')) {
      parts.push(<em key={`${keyBase}-${index}`}>{part.slice(1, -1)}</em>);
    } else {
      parts.push(part);
    }
  });
  return parts;
}

function InlineText({ text }) {
  const parts = [];
  // Non-greedy so bold spans survive a literal '*' inside (e.g. "E*Trade").
  String(text).split(/(\*\*.+?\*\*)/g).forEach((part, index) => {
    if (!part) return;
    if (part.startsWith('**') && part.endsWith('**')) {
      parts.push(<strong key={index}>{part.slice(2, -2)}</strong>);
    } else {
      parts.push(...renderItalics(part, index));
    }
  });
  return <>{parts}</>;
}

export function MarkdownRenderer({ content }) {
  if (!content) return <div className="markdown-empty">Nothing to show yet.</div>;
  const lines = String(content).split('\n');
  const blocks = [];
  let bullets = [];
  let numbers = [];

  const flushBullets = () => {
    if (bullets.length) {
      blocks.push(<ul key={`ul-${blocks.length}`}>{bullets.map((item, idx) => <li key={idx}><InlineText text={item} /></li>)}</ul>);
      bullets = [];
    }
  };
  const flushNumbers = () => {
    if (numbers.length) {
      blocks.push(<ol key={`ol-${blocks.length}`}>{numbers.map((item, idx) => <li key={idx}><InlineText text={item} /></li>)}</ol>);
      numbers = [];
    }
  };
  const flushLists = () => {
    flushBullets();
    flushNumbers();
  };

  lines.forEach((raw, index) => {
    const line = raw.trim();
    if (!line) {
      flushLists();
      return;
    }
    if (line.startsWith('- ') || line.startsWith('* ')) {
      flushNumbers();
      bullets.push(line.slice(2));
      return;
    }
    const ordered = line.match(/^\d+\.\s+(.*)$/);
    if (ordered) {
      flushBullets();
      numbers.push(ordered[1]);
      return;
    }
    flushLists();
    if (/^-{3,}$/.test(line) || /^\*{3,}$/.test(line)) blocks.push(<hr key={index} />);
    else if (line.startsWith('### ')) blocks.push(<h3 key={index}>{line.slice(4)}</h3>);
    else if (line.startsWith('## ')) blocks.push(<h2 key={index}>{line.slice(3)}</h2>);
    else if (line.startsWith('# ')) blocks.push(<h1 key={index}>{line.slice(2)}</h1>);
    else blocks.push(<p key={index}><InlineText text={line} /></p>);
  });
  flushLists();
  return <div className="markdown-body">{blocks}</div>;
}
