import { useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import type { FixAction } from './fixes';

// The fix-it link: click navigates, hover/focus shows a popover saying where
// it leads (reuses the dashboard's .info-pop, portalled to <body> so the chat
// bubble can't clip it). Fixes are derived from the error code's PATTERN by
// lib/fixes.ts, so any newly registered capability gets this automatically.
export function FixLink({ fix }: { fix: FixAction }) {
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState({ x: 0, y: 0 });
  const ref = useRef<HTMLAnchorElement>(null);
  const show = () => {
    const r = ref.current?.getBoundingClientRect();
    if (r) setPos({ x: r.left + r.width / 2, y: r.top });
    setOpen(true);
  };
  return (
    <>
      <a
        ref={ref}
        className="gen-fix"
        href={`#${fix.hash}`}
        onMouseEnter={show}
        onMouseLeave={() => setOpen(false)}
        onFocus={show}
        onBlur={() => setOpen(false)}
      >
        {fix.label} →
      </a>
      {open && createPortal(
        <div className="info-pop" role="tooltip" style={{ left: pos.x, top: pos.y }}>{fix.tip}</div>,
        document.body,
      )}
    </>
  );
}
