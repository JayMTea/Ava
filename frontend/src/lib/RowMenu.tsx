import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Icon } from './icons';

// A row's "⋯" overflow menu. Secondary and destructive actions live here so a
// list row keeps one primary button and a couple of quiet ones — no button
// pile-up that overflows the card. Portalled to <body> (same as the rail flyout)
// so a scrolling panel can never clip it; opens below-right of the kebab and
// flips above when it would run off the bottom. Closes on outside-click, Escape,
// or any scroll (a fixed menu would otherwise drift away from its trigger).
// Shared by the Connectors and Memory rows so both read as one system.
export interface MenuAction {
  label: string;
  icon: string;
  onClick: () => void;
  danger?: boolean;
  disabled?: boolean;
}

export function RowMenu({ actions, disabled, label = 'More actions' }: {
  actions: MenuAction[]; disabled?: boolean; label?: string;
}) {
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<{ top?: number; bottom?: number; right: number }>({ right: 0 });
  const btnRef = useRef<HTMLButtonElement>(null);

  const place = () => {
    const r = btnRef.current?.getBoundingClientRect();
    if (!r) return;
    const right = window.innerWidth - r.right;
    const estH = actions.length * 38 + 12;
    if (r.bottom + estH > window.innerHeight - 8 && r.top > estH) {
      setPos({ bottom: window.innerHeight - r.top + 6, right });
    } else {
      setPos({ top: r.bottom + 6, right });
    }
  };

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false); };
    const onDown = (e: PointerEvent) => {
      const t = e.target as Element;
      if (btnRef.current?.contains(t) || t.closest?.('.hub-rowmenu')) return;
      setOpen(false);
    };
    const onScroll = () => setOpen(false);
    document.addEventListener('keydown', onKey);
    document.addEventListener('pointerdown', onDown);
    window.addEventListener('scroll', onScroll, true);
    window.addEventListener('resize', onScroll);
    return () => {
      document.removeEventListener('keydown', onKey);
      document.removeEventListener('pointerdown', onDown);
      window.removeEventListener('scroll', onScroll, true);
      window.removeEventListener('resize', onScroll);
    };
  }, [open]);

  if (!actions.length) return null;

  return (
    <>
      <button
        ref={btnRef}
        className="hub-btn ghost sm hub-kebab"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={label}
        title={label}
        disabled={disabled}
        onClick={() => { if (open) setOpen(false); else { place(); setOpen(true); } }}
      >
        <Icon name="more" />
      </button>
      {open && createPortal(
        <div className="rail-menu hub-rowmenu" role="menu"
          style={{ top: pos.top, bottom: pos.bottom, right: pos.right }}>
          {actions.map((a) => (
            <button
              key={a.label}
              role="menuitem"
              className={'rail-menu-item' + (a.danger ? ' danger' : '')}
              disabled={a.disabled}
              onClick={() => { setOpen(false); a.onClick(); }}
            >
              <Icon name={a.icon} /><span>{a.label}</span>
            </button>
          ))}
        </div>,
        document.body,
      )}
    </>
  );
}
