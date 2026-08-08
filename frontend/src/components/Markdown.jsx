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

// Pictographs and emoji-presentation symbols, at the very start of the item.
const LEADING_EMOJI = /^\s*(\p{Extended_Pictographic}|\p{Emoji_Presentation})/u;

export function MarkdownRenderer({ content }) {
  if (!content) return <div className="markdown-empty">Nothing to show yet.</div>;
  const lines = String(content).split('\n');
  const blocks = [];
  let bullets = [];
  let numbers = [];

  const flushBullets = () => {
    if (bullets.length) {
      // A model that opens a bullet with 🔴 has already drawn the bullet;
      // ours next to it reads as two markers for one item.
      blocks.push(
        <ul key={`ul-${blocks.length}`}>
          {bullets.map((item, idx) => (
            <li key={idx} className={LEADING_EMOJI.test(item) ? 'has-own-marker' : undefined}>
              <InlineText text={item} />
            </li>
          ))}
        </ul>,
      );
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

  // Briefings hold their position tables in pipe markdown. Without this every
  // row printed as its own paragraph of pipes and dashes.
  const cells = row => row.trim().replace(/^\||\|$/g, '').split('|').map(cell => cell.trim());
  const isDivider = row => /^\s*\|?[\s:-]*-[\s:|-]*\|?\s*$/.test(row) && row.includes('-');
  const takeTable = start => {
    if (!lines[start]?.trim().startsWith('|') || !isDivider(lines[start + 1] || '')) return null;
    const body = [];
    let cursor = start + 2;
    while (cursor < lines.length && lines[cursor].trim().startsWith('|')) {
      body.push(cells(lines[cursor]));
      cursor += 1;
    }
    return { header: cells(lines[start]), body, next: cursor };
  };

  let skipUntil = -1;

  lines.forEach((raw, index) => {
    if (index < skipUntil) return;
    const line = raw.trim();
    if (!line) {
      flushLists();
      return;
    }
    if (line.startsWith('|')) {
      const table = takeTable(index);
      if (table) {
        flushLists();
        skipUntil = table.next;
        blocks.push(
          <div className="md-table-wrap" key={`table-${index}`}>
            <table className="md-table">
              <thead>
                <tr>{table.header.map((cell, i) => <th key={i}><InlineText text={cell} /></th>)}</tr>
              </thead>
              <tbody>
                {table.body.map((row, r) => (
                  <tr key={r}>{row.map((cell, i) => <td key={i}><InlineText text={cell} /></td>)}</tr>
                ))}
              </tbody>
            </table>
          </div>,
        );
        return;
      }
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
