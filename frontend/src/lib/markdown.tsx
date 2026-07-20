// A tiny, dependency-free markdown renderer for owner-authored docs (skill
// SKILL.md bodies). It builds React nodes directly — never HTML strings — so
// there is no XSS surface, and it covers the constructs skills actually use:
// headings, bold/italic, inline code, fenced code, links, blockquotes, rules,
// ordered/unordered lists, and GFM pipe tables. Not a spec-complete parser;
// unknown constructs fall through as plain text.
import React from 'react';

type Node = React.ReactNode;

const INLINE: { re: RegExp; render: (m: RegExpMatchArray, key: string) => Node }[] = [
  { re: /`([^`]+)`/, render: (m, k) => <code key={k} className="md-code">{m[1]}</code> },
  { re: /\*\*([^*]+)\*\*/, render: (m, k) => <strong key={k}>{inline(m[1], k)}</strong> },
  { re: /\[([^\]]+)\]\(([^)]+)\)/, render: (m, k) => {
      const href = safeHref(m[2]);
      return href ? <a key={k} href={href} target="_blank" rel="noreferrer noopener">{m[1]}</a> : <span key={k}>{m[1]}</span>;
    } },
  { re: /\*([^*]+)\*/, render: (m, k) => <em key={k}>{inline(m[1], k)}</em> },
  { re: /\b_([^_]+)_\b/, render: (m, k) => <em key={k}>{inline(m[1], k)}</em> },
];

function safeHref(url: string): string | null {
  const u = url.trim();
  if (/^(https?:|mailto:|#|\/)/i.test(u)) return u;
  return null; // block javascript:/data: and other schemes
}

function inline(text: string, keyPrefix: string): Node[] {
  const out: Node[] = [];
  let rest = text;
  let n = 0;
  while (rest) {
    let best: { idx: number; len: number; node: Node } | null = null;
    for (const p of INLINE) {
      const m = rest.match(p.re);
      if (m && m.index !== undefined && (!best || m.index < best.idx)) {
        best = { idx: m.index, len: m[0].length, node: p.render(m, `${keyPrefix}-${n}`) };
      }
    }
    if (!best) { out.push(rest); break; }
    if (best.idx > 0) out.push(rest.slice(0, best.idx));
    out.push(best.node);
    rest = rest.slice(best.idx + best.len);
    n += 1;
  }
  return out;
}

const splitRow = (line: string): string[] =>
  line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((c) => c.trim());

const isBlockStart = (line: string): boolean =>
  /^```/.test(line) || /^#{1,6}\s/.test(line) || /^>\s?/.test(line) ||
  /^\s*([-*+]|\d+\.)\s+/.test(line) || /^(-{3,}|\*{3,}|_{3,})$/.test(line.trim());

function parseBlocks(md: string): Node[] {
  const lines = md.replace(/\r\n/g, '\n').split('\n');
  const blocks: Node[] = [];
  let i = 0;
  let key = 0;
  const k = () => `b${key++}`;

  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i += 1; continue; }

    if (/^```/.test(line)) {                                   // fenced code
      const buf: string[] = [];
      i += 1;
      while (i < lines.length && !/^```/.test(lines[i])) { buf.push(lines[i]); i += 1; }
      i += 1;
      blocks.push(<pre key={k()} className="md-pre"><code>{buf.join('\n')}</code></pre>);
      continue;
    }

    const h = line.match(/^(#{1,6})\s+(.*)$/);                 // heading (styled, non-semantic)
    if (h) {
      blocks.push(<div key={k()} className={`md-h md-h${h[1].length}`}>{inline(h[2], k())}</div>);
      i += 1;
      continue;
    }

    if (/^(-{3,}|\*{3,}|_{3,})$/.test(line.trim())) {          // horizontal rule
      blocks.push(<hr key={k()} className="md-hr" />);
      i += 1;
      continue;
    }

    if (line.includes('|') && i + 1 < lines.length &&          // GFM table
        /-/.test(lines[i + 1]) && /^\s*\|?[\s:|-]+\|?\s*$/.test(lines[i + 1])) {
      const header = splitRow(line);
      i += 2;
      const rows: string[][] = [];
      while (i < lines.length && lines[i].includes('|') && lines[i].trim()) { rows.push(splitRow(lines[i])); i += 1; }
      blocks.push(
        <div key={k()} className="md-table-wrap">
          <table className="md-table">
            <thead><tr>{header.map((c, ci) => <th key={ci}>{inline(c, k())}</th>)}</tr></thead>
            <tbody>{rows.map((r, ri) => (
              <tr key={ri}>{header.map((_, ci) => <td key={ci}>{inline(r[ci] ?? '', k())}</td>)}</tr>
            ))}</tbody>
          </table>
        </div>,
      );
      continue;
    }

    if (/^>\s?/.test(line)) {                                  // blockquote
      const buf: string[] = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) { buf.push(lines[i].replace(/^>\s?/, '')); i += 1; }
      blocks.push(<blockquote key={k()} className="md-quote">{parseBlocks(buf.join('\n'))}</blockquote>);
      continue;
    }

    if (/^\s*([-*+]|\d+\.)\s+/.test(line)) {                   // list
      const ordered = /^\s*\d+\.\s+/.test(line);
      const items: Node[] = [];
      while (i < lines.length && /^\s*([-*+]|\d+\.)\s+/.test(lines[i])) {
        items.push(<li key={k()}>{inline(lines[i].replace(/^\s*([-*+]|\d+\.)\s+/, ''), k())}</li>);
        i += 1;
      }
      blocks.push(ordered
        ? <ol key={k()} className="md-ol">{items}</ol>
        : <ul key={k()} className="md-ul">{items}</ul>);
      continue;
    }

    const buf = [line];                                        // paragraph
    i += 1;
    while (i < lines.length && lines[i].trim() && !isBlockStart(lines[i])) { buf.push(lines[i]); i += 1; }
    blocks.push(<p key={k()} className="md-p">{inline(buf.join(' '), k())}</p>);
  }
  return blocks;
}

export function MarkdownLite({ text }: { text: string }) {
  return <div className="md">{parseBlocks(text || '')}</div>;
}
