import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';

// A code viewer/editor built from a textarea, a gutter and an overlay.
//
// WHY NOT CODEMIRROR
// ------------------
// Against a THREE runtime dependency budget, a `prebuild` gate that pins
// node_modules to package-lock, and a CI job that byte-compares `dist/`,
// CodeMirror is not one dependency: `@codemirror/state`, `view`, `language`,
// `commands`, `search` plus a language pack per syntax — realistically 6–10
// packages and ~350–500 kB minified, for one panel.
//
// Every stated Review requirement is met without it: line numbers, search,
// jump-to-line, edit, Cmd-S save, and compare-and-swap conflict detection
// (which is protocol work, not editor work). The ONLY thing lost is syntax
// highlighting, which was not in the requirements.
//
// Pre-agreed escape hatch, recorded so it does not become an argument later: if
// highlighting becomes non-negotiable, the one acceptable addition is a single
// zero-dependency highlighter loaded inside Review's own chunk, with a ceiling
// of **40 kB gzipped**. A number makes that decision testable.
//
// Lives under `src/lib/` on purpose: `vite.config.ts` puts
// `/src/components/agent/` in a manual chunk, and manualChunks WINS over nested
// lazy() — anything captured there cannot be split out again.

export interface CodeAreaProps {
  value: string;
  onChange?: (next: string) => void;
  readOnly?: boolean;
  onSave?: () => void;
  /** 1-based; scrolls that line into view when it changes. */
  jumpToLine?: number | null;
  label?: string;
}

export function CodeArea({
  value, onChange, readOnly, onSave, jumpToLine, label = 'File contents',
}: CodeAreaProps) {
  const taRef = useRef<HTMLTextAreaElement>(null);
  const gutterRef = useRef<HTMLDivElement>(null);
  const [find, setFind] = useState('');
  const [findOpen, setFindOpen] = useState(false);
  const [hit, setHit] = useState(0);

  const lines = value.split('\n');
  const matches = useCounted(value, find);

  // The gutter is a separate element, so it has to be scrolled in lockstep.
  // Doing it on scroll rather than by transform keeps the two in step even when
  // the textarea scrolls for a reason we did not cause (a caret move, a find).
  const syncScroll = useCallback(() => {
    if (gutterRef.current && taRef.current) {
      gutterRef.current.scrollTop = taRef.current.scrollTop;
    }
  }, []);

  // `value` is the TRIGGER, not an input: when the content changes the
  // textarea's scroll height changes with it, and the gutter has to catch up in
  // the same frame or the numbers visibly lag the code.
  // biome-ignore lint/correctness/useExhaustiveDependencies: see above
  useLayoutEffect(syncScroll, [value, syncScroll]);

  useEffect(() => {
    if (!jumpToLine || !taRef.current) return;
    const ta = taRef.current;
    const before = lines.slice(0, Math.max(0, jumpToLine - 1)).join('\n').length;
    ta.focus();
    ta.setSelectionRange(before, before);
    // Approximate, and deliberately so: measuring the real line height means
    // reading layout on every jump. Being one line out is invisible; a layout
    // thrash on a large file is not.
    const lineH = ta.scrollHeight / Math.max(1, lines.length);
    ta.scrollTop = Math.max(0, (jumpToLine - 3) * lineH);
    syncScroll();
  }, [jumpToLine, lines, syncScroll]);

  const onKey = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    const mod = e.metaKey || e.ctrlKey;
    if (mod && e.key.toLowerCase() === 's') {
      e.preventDefault();
      onSave?.();
    } else if (mod && e.key.toLowerCase() === 'f') {
      // Ava's own find bar, not the browser's: the browser's cannot search a
      // textarea's value, so Cmd-F over a code panel would silently do nothing.
      e.preventDefault();
      setFindOpen(true);
    } else if (e.key === 'Escape' && findOpen) {
      setFindOpen(false);
    }
  };

  const step = (dir: 1 | -1) => {
    if (!matches.length || !taRef.current) return;
    const next = (hit + dir + matches.length) % matches.length;
    setHit(next);
    const at = matches[next];
    taRef.current.focus();
    taRef.current.setSelectionRange(at, at + find.length);
  };

  return (
    <div className="code-area">
      {findOpen && (
        <div className="code-find">
          {/* Autofocused deliberately: this bar exists only because the user
              just pressed Cmd-F, so focusing it IS the action they asked for.
              Making them click into it afterwards would be the bug. */}
          <input
            autoFocus
            value={find}
            onChange={(e) => { setFind(e.target.value); setHit(0); }}
            placeholder="Find"
            aria-label="Find in file"
          />
          <span className="code-find-count">
            {find ? `${matches.length ? hit + 1 : 0}/${matches.length}` : ''}
          </span>
          <button type="button" onClick={() => step(-1)} aria-label="Previous match">↑</button>
          <button type="button" onClick={() => step(1)} aria-label="Next match">↓</button>
          <button type="button" onClick={() => setFindOpen(false)} aria-label="Close find">✕</button>
        </div>
      )}
      <div className="code-body">
        <div className="code-gutter" ref={gutterRef} aria-hidden="true">
          {/* biome-ignore lint/suspicious/noArrayIndexKey: a gutter line IS its
              index — there is no other identity a line number could have. */}
          {lines.map((_, i) => <span key={i}>{i + 1}</span>)}
        </div>
        <textarea
          ref={taRef}
          className="code-text"
          value={value}
          readOnly={readOnly}
          spellCheck={false}
          aria-label={label}
          onScroll={syncScroll}
          onKeyDown={onKey}
          onChange={(e) => onChange?.(e.target.value)}
        />
      </div>
    </div>
  );
}

/** Every offset where `needle` occurs. Empty needle matches nothing. */
function useCounted(hay: string, needle: string): number[] {
  const [out, setOut] = useState<number[]>([]);
  useEffect(() => {
    if (!needle) { setOut([]); return; }
    const found: number[] = [];
    let i = hay.toLowerCase().indexOf(needle.toLowerCase());
    while (i !== -1 && found.length < 5000) {
      found.push(i);
      i = hay.toLowerCase().indexOf(needle.toLowerCase(), i + needle.length);
    }
    setOut(found);
  }, [hay, needle]);
  return out;
}
